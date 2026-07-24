"""Unit tests for the standalone private web cache."""

from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import cache as cache_module
from cache import (
    CODE_CONTENT_TTL_SECONDS,
    DOCUMENTATION_CONTENT_TTL_SECONDS,
    GENERAL_CONTENT_TTL_SECONDS,
    NEWS_SEARCH_TTL_SECONDS,
    PDF_CONTENT_TTL_SECONDS,
    SEARCH_TTL_SECONDS,
    WebCache,
    canonicalize_url,
)


class Clock:
    def __init__(self, now: float = 2_000_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_query_normalization_hashes_only_and_variants_are_isolated(tmp_path):
    cache = WebCache(tmp_path / "cache")
    try:
        secret_query = "  ＰＹＴＨＯＮ\tStraße  "
        cache.put_search(
            secret_query,
            {"query": secret_query, "text": secret_query, "results": [{"rank": 1}]},
            variant="normal:8",
        )
        equivalent = cache.get_search("python STRASSE", variant="normal:8")
        assert equivalent is not None
        assert equivalent.payload == {"results": [{"rank": 1}]}
        assert cache.get_search("python STRASSE", variant="normal:3") is None

        with sqlite3.connect(cache.db_path) as conn:
            row = conn.execute(
                "SELECT query_hash, variant_hash, payload_json FROM search_entries"
            ).fetchone()
        assert len(row[0]) == 64
        assert len(row[1]) == 64
        assert secret_query not in repr(row)
        assert "python strasse" not in repr(row).lower()
    finally:
        cache.close()


def test_url_canonicalization_removes_tracking_and_preserves_semantics(tmp_path):
    canonical = canonicalize_url(
        "HTTPS://Example.COM:443/Case/Path?utm_source=x&keep=One&gclid=y&blank=#frag"
    )
    assert canonical == "https://example.com/Case/Path?keep=One&blank="

    cache = WebCache(tmp_path / "cache")
    try:
        cache.put_content(
            "HTTPS://Example.COM/Case?utm_campaign=sale&id=7#top",
            "content",
            content_type="text/plain",
        )
        hit = cache.get_content("https://example.com/Case?id=7&fbclid=ignored")
        assert hit is not None
        assert hit.content == b"content"
        with sqlite3.connect(cache.db_path) as conn:
            stored = conn.execute("SELECT canonical_url FROM content_entries").fetchone()[0]
        assert stored == "https://example.com/Case?id=7"
    finally:
        cache.close()


def test_search_ttls_expire_general_and_news_entries(tmp_path):
    clock = Clock()
    cache = WebCache(tmp_path / "cache", clock=clock)
    try:
        cache.put_search("general", {"results": [1]}, variant="v")
        cache.put_search("news", {"results": [2]}, variant="v", news=True)
        with sqlite3.connect(cache.db_path) as conn:
            ttls = sorted(
                expires - created
                for created, expires in conn.execute(
                    "SELECT created_at, expires_at FROM search_entries"
                )
            )
        assert ttls == [NEWS_SEARCH_TTL_SECONDS, SEARCH_TTL_SECONDS]

        clock.now += NEWS_SEARCH_TTL_SECONDS
        assert cache.get_search("news", variant="v") is None
        assert cache.get_search("general", variant="v") is not None
        clock.now += SEARCH_TTL_SECONDS - NEWS_SEARCH_TTL_SECONDS
        assert cache.get_search("general", variant="v") is None
    finally:
        cache.close()


def test_content_ttls_are_classified_by_type_and_url(tmp_path):
    clock = Clock()
    cache = WebCache(tmp_path / "cache", clock=clock)
    try:
        entries = [
            ("https://example.com/file", "application/pdf", PDF_CONTENT_TTL_SECONDS),
            ("https://github.com/org/repo/blob/main/a.py", "text/plain", CODE_CONTENT_TTL_SECONDS),
            ("https://docs.python.org/3/library/sqlite3.html", "text/html", DOCUMENTATION_CONTENT_TTL_SECONDS),
            ("https://example.com/article", "text/html", GENERAL_CONTENT_TTL_SECONDS),
        ]
        for index, (url, content_type, _ttl) in enumerate(entries):
            cache.put_content(url, f"body-{index}", content_type=content_type)
        with sqlite3.connect(cache.db_path) as conn:
            rows = dict(
                conn.execute(
                    "SELECT canonical_url, expires_at - created_at FROM content_entries"
                ).fetchall()
            )
        for url, _content_type, ttl in entries:
            assert rows[canonicalize_url(url)] == ttl

        clock.now += GENERAL_CONTENT_TTL_SECONDS
        assert cache.get_content(entries[1][0]) is None
        assert cache.get_content(entries[3][0]) is None
        assert cache.get_content(entries[2][0]) is not None
        assert cache.get_content(entries[0][0]) is not None
    finally:
        cache.close()


def test_hits_report_payload_content_and_age_and_misses_return_none(tmp_path):
    clock = Clock()
    cache = WebCache(tmp_path / "cache", clock=clock)
    try:
        cache.put_search("query", {"results": [{"title": "A"}]}, variant="v")
        cache.put_content(
            "https://example.com/a",
            b"body",
            content_type="text/plain",
            final_url="https://www.example.com/a#fragment",
        )
        clock.now += 12.75
        search = cache.get_search("query", variant="v")
        content = cache.get_content("https://example.com/a")
        assert search is not None and search.age_seconds == 12.75
        assert search.payload["results"][0]["title"] == "A"
        assert content is not None and content.age_seconds == 12.75
        assert content.content == b"body"
        assert content.content_type == "text/plain"
        assert content.final_url == "https://www.example.com/a"
        assert cache.get_search("missing", variant="v") is None
        assert cache.get_content("https://example.com/missing") is None
    finally:
        cache.close()


def test_corrupt_database_fails_open(tmp_path):
    root = tmp_path / "cache"
    root.mkdir(mode=0o700)
    (root / "cache.sqlite3").write_bytes(b"not a sqlite database")
    cache = WebCache(root)
    try:
        assert cache.available is False
        assert cache.get_search("query", variant="v") is None
        assert cache.get_content("https://example.com/") is None
        cache.put_search("query", {"results": [1]}, variant="v")
        cache.put_content("https://example.com/", b"x", content_type="text/plain")
    finally:
        cache.close()


def test_blob_write_is_atomic_and_failed_replace_leaves_no_partial_files(monkeypatch, tmp_path):
    cache = WebCache(tmp_path / "cache")
    try:
        def failed_replace(_source, _destination):
            raise OSError("simulated crash before replace")

        monkeypatch.setattr(cache_module.os, "replace", failed_replace)
        cache.put_content("https://example.com/a", b"partial", content_type="text/plain")
        assert cache.get_content("https://example.com/a") is None
        assert list(cache.content_dir.rglob("*tmp")) == []
        assert [path for path in cache.content_dir.rglob("*") if path.is_file()] == []
    finally:
        cache.close()


def test_lru_eviction_removes_oldest_content_first(tmp_path):
    clock = Clock()
    cache = WebCache(
        tmp_path / "cache",
        max_content_bytes=20,
        eviction_watermark_bytes=8,
        clock=clock,
    )
    try:
        cache.put_content("https://example.com/old", b"12345", content_type="text/plain")
        clock.now += 1
        cache.put_content("https://example.com/new", b"67890", content_type="text/plain")
        assert cache.get_content("https://example.com/old") is None
        assert cache.get_content("https://example.com/new") is not None
    finally:
        cache.close()


def test_cleanup_removes_orphan_blobs(tmp_path):
    cache = WebCache(tmp_path / "cache")
    try:
        orphan_dir = cache.content_dir / "ff"
        orphan_dir.mkdir(mode=0o700)
        orphan = orphan_dir / ("f" * 64)
        orphan.write_bytes(b"orphan")
        stats = cache.cleanup()
        assert stats.orphaned_blobs == 1
        assert stats.bytes_removed == len(b"orphan")
        assert not orphan.exists()
    finally:
        cache.close()


def test_private_directory_database_and_blob_permissions(tmp_path):
    cache = WebCache(tmp_path / "cache")
    try:
        cache.put_content("https://example.com/a", b"private", content_type="text/plain")
        assert os.stat(cache.root).st_mode & 0o777 == 0o700
        assert os.stat(cache.content_dir).st_mode & 0o777 == 0o700
        private_files = [path for path in cache.root.rglob("*") if path.is_file()]
        assert private_files
        assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in private_files)
        private_dirs = [path for path in cache.root.rglob("*") if path.is_dir()]
        assert all(os.stat(path).st_mode & 0o777 == 0o700 for path in private_dirs)
    finally:
        cache.close()


def test_concurrent_get_and_put_is_thread_safe(tmp_path):
    cache = WebCache(
        tmp_path / "cache",
        max_content_bytes=1024 * 1024,
        eviction_watermark_bytes=1024 * 1024,
    )
    try:
        def exercise(index: int) -> bool:
            query = f"query-{index % 10}"
            url = f"https://example.com/{index % 10}"
            cache.put_search(query, {"results": [index]}, variant="v")
            cache.put_content(url, f"body-{index}", content_type="text/plain")
            return (
                cache.get_search(query, variant="v") is not None
                and cache.get_content(url) is not None
            )

        with ThreadPoolExecutor(max_workers=12) as executor:
            assert all(executor.map(exercise, range(100)))
    finally:
        cache.close()
