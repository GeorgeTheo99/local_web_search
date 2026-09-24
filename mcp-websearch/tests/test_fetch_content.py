"""Offline regressions for title/script shells and legacy HTML cache entries."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

import server as srv
from cache import WebCache


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    cache = WebCache(tmp_path / "cache")
    telemetry = srv.TelemetryStore(tmp_path / "telemetry")
    monkeypatch.setattr(srv, "LOCAL_SEARCH_DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "_cache", cache)
    monkeypatch.setattr(srv, "_telemetry", telemetry)
    monkeypatch.setattr(srv, "DECODO_FALLBACK_ENABLED", False)
    monkeypatch.setattr(srv, "JINA_FALLBACK_ENABLED", False)
    # No test in this module may open a real network connection.
    async def no_network(*args, **kwargs):
        raise AssertionError("unexpected network request")
    monkeypatch.setattr(srv, "_fetch_public_body", no_network)
    yield cache, telemetry
    cache.close()
    telemetry.close()


SHELL = (
    '<html><head><title>Reddit</title><script>' + ('x' * 8500) +
    '</script></head><body><div id="root"></div></body></html>'
)
URL = "https://example.com/synthetic-shell"


@pytest.mark.parametrize("html", [
    pytest.param(SHELL, id="large-title-only-shell"),
    "<title>Another brand</title><script>boot()</script>",
    "<html><head><title>Brand</title></head><body>Loading...</body></html>",
    "<p>You need to enable JavaScript to run this app.</p>",
    "<html><body> \n\t </body></html>",
])
@pytest.mark.asyncio
async def test_shell_fails_without_success_or_cache(monkeypatch, isolated_runtime, html):
    cache, telemetry = isolated_runtime
    async def fetch(url):
        return url, html.encode(), "text/html", 200
    monkeypatch.setattr(srv, "_fetch_public_body", fetch)
    result = await srv._web_fetch_impl(URL)
    assert result.error and "no substantive body content" in result.error
    assert result.provider == "direct"
    assert cache.get_content(URL) is None
    assert telemetry.flush()
    with sqlite3.connect(telemetry.db_path) as conn:
        assert conn.execute("SELECT outcome, provider, trigger FROM fetch_events").fetchall() == [
            ("extraction_error", "direct", "extraction_error")
        ]
        assert conn.execute("SELECT outcome, trigger FROM fetch_operations").fetchall() == [
            ("error", "extraction_error")
        ]


@pytest.mark.parametrize("text,content_type", [
    ("<p>OK</p>", "text/html"),
    ("<title>Status</title><p>42</p>", "text/html; charset=utf-8"),
    ("<p>Open until 5.</p>", "application/xhtml+xml"),
    ("<head><title>Status</title><p>Open until 5.</p>", "text/html"),
    ("<head><title>Status</title></head><p>Open until 5.</p>", "text/html"),
    ("<head><title>Status</title><p>Open until 5.</p>" + "<!-- padding -->" * 30, "text/html"),
    ('<head><base href="/files/"><title>Files</title></head>'
     '<a href="report.pdf">Report</a>', "text/html"),
    ("Reddit", "text/plain"),
    ('{"ok":true}', "application/json"),
    ("<status>OK</status>", "application/xml"),
])
@pytest.mark.asyncio
async def test_short_documents_are_successful_and_cacheable(monkeypatch, text, content_type):
    calls = []
    async def fetch(url):
        calls.append(url)
        return url, text.encode(), content_type, 200
    monkeypatch.setattr(srv, "_fetch_public_body", fetch)
    first = await srv._web_fetch_impl(URL)
    second = await srv._web_fetch_impl(URL)
    assert first.error is None and first.provider == "direct" and first.text
    assert second.error is None and second.provider == "cache" and second.text == first.text
    assert calls == [URL]
    if "report.pdf" in text:
        assert "https://example.com/files/report.pdf" in first.text
    if "Open until 5." in text:
        assert first.text == "Open until 5."


@pytest.mark.asyncio
async def test_caller_truncation_does_not_invalidate_html(monkeypatch):
    async def fetch(url):
        return url, b"<p>A useful short fact.</p>", "text/html", 200
    monkeypatch.setattr(srv, "_fetch_public_body", fetch)
    short = await srv._web_fetch_impl(URL, max_chars=1)
    full = await srv._web_fetch_impl(URL)
    assert short.error is None and len(short.text) == 1
    assert full.text == "A useful short fact." and full.cache_hit


@pytest.mark.parametrize("decodo_succeeds", [True, False])
@pytest.mark.asyncio
async def test_shell_uses_ordered_fallback_and_caches_provider_result(
    monkeypatch, isolated_runtime, decodo_succeeds
):
    cache, telemetry = isolated_runtime
    attempts = []
    async def fetch(url):
        return url, SHELL.encode(), "text/html", 200
    async def decodo(url, max_chars, trigger, *, timeout):
        attempts.append("decodo")
        assert trigger == "extraction_error" and 0 < timeout <= srv.FETCH_OPERATION_TIMEOUT
        if not decodo_succeeds:
            return srv._FetchResult("failed", "failed", False, 0, url, "", "decodo")
        return srv._FetchResult("Recovered article. " * 20, None, False, 0, url, "text/markdown", "decodo")
    async def jina(url, max_chars, trigger, *, timeout):
        attempts.append("jina")
        assert trigger == "extraction_error" and 0 < timeout <= srv.FETCH_OPERATION_TIMEOUT
        return srv._FetchResult("Recovered article. " * 20, None, False, 0, url, "text/markdown", "jina")
    monkeypatch.setattr(srv, "_fetch_public_body", fetch)
    monkeypatch.setattr(srv, "DECODO_FALLBACK_ENABLED", True)
    monkeypatch.setattr(srv, "JINA_FALLBACK_ENABLED", True)
    monkeypatch.setattr(srv, "_resolve_decodo_key", lambda: "offline-test-key")
    monkeypatch.setattr(srv, "_decodo_scraper_fetch", decodo)
    monkeypatch.setattr(srv, "_jina_reader_fetch", jina)
    first = await srv._web_fetch_impl(URL, max_chars=10)
    second = await srv._web_fetch_impl(URL)
    assert first.error is None and first.provider == ("decodo" if decodo_succeeds else "jina")
    assert attempts == (["decodo"] if decodo_succeeds else ["decodo", "jina"])
    assert len(first.text) == 10
    assert second.cache_hit and second.text == "Recovered article. " * 20
    assert cache.get_content(URL).content_type == "text/markdown"
    assert telemetry.flush()
    with sqlite3.connect(telemetry.db_path) as conn:
        assert conn.execute("SELECT outcome FROM fetch_events WHERE provider='direct'").fetchall() == [
            ("extraction_error",)
        ]


@pytest.mark.parametrize("old_content", ["Reddit", "", "A legitimate old short document."])
@pytest.mark.asyncio
async def test_legacy_html_is_refetched_without_touching_other_entries(monkeypatch, isolated_runtime, old_content):
    cache, _ = isolated_runtime
    cache.put_content(URL, old_content, content_type="text/html")
    cache.put_content("https://example.com/other", "Other", content_type="text/html")
    cache.put_search("offline", {"results": []}, variant="test")
    async def fetch(url):
        return url, b"<p>Actual body.</p>", "text/html", 200
    monkeypatch.setattr(srv, "_fetch_public_body", fetch)
    first = await srv._web_fetch_impl(URL)
    second = await srv._web_fetch_impl(URL)
    assert first.text == "Actual body." and first.provider == "direct"
    assert second.text == first.text and second.cache_hit
    assert cache.get_content("https://example.com/other").content == b"Other"
    assert cache.get_search("offline", variant="test") is not None


@pytest.mark.asyncio
async def test_invalid_legacy_blob_removed_even_when_refetch_fails(monkeypatch, isolated_runtime):
    cache, _ = isolated_runtime
    cache.put_content(URL, "Reddit", content_type="text/html")
    with sqlite3.connect(cache.db_path) as conn:
        blob = cache.root / conn.execute("SELECT blob_relpath FROM content_entries").fetchone()[0]
    async def fetch(url):
        return url, SHELL.encode(), "text/html", 200
    monkeypatch.setattr(srv, "_fetch_public_body", fetch)
    assert (await srv._web_fetch_impl(URL)).error
    assert cache.get_content(URL) is None and not blob.exists()


def test_v1_cache_migration_preserves_data_and_versions_are_explicit(tmp_path):
    root = tmp_path / "v1-cache"
    cache = WebCache(root)
    cache.put_content(URL, "Reddit", content_type="text/html")
    cache.put_content("https://example.com/plain", "OK", content_type="text/plain")
    cache.put_search("offline", {"results": []}, variant="test")
    cache.close()
    with sqlite3.connect(root / "cache.sqlite3") as conn:
        conn.execute("ALTER TABLE content_entries DROP COLUMN html_extraction_version")
        conn.execute("PRAGMA user_version = 1")
    def concurrent_open(_index):
        instance = WebCache(root)
        try:
            return instance.available and instance.get_search("offline", variant="test") is not None
        finally:
            instance.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(concurrent_open, range(4)))
    migrated = WebCache(root)
    try:
        assert migrated.available
        with sqlite3.connect(migrated.db_path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM content_entries").fetchone()[0] == 2
        assert migrated.get_content(URL, html_extraction_version=1) is None
        assert migrated.get_content("https://example.com/plain", html_extraction_version=1).content == b"OK"
        assert migrated.get_search("offline", variant="test") is not None
        migrated.put_content(URL, "OK", content_type="text/html", html_extraction_version=1)
        assert migrated.get_content(URL, html_extraction_version=1).content == b"OK"
        migrated.put_content(URL, "unvalidated overwrite", content_type="text/html")
        assert migrated.get_content(URL, html_extraction_version=1) is None
    finally:
        migrated.close()
