"""Private, process-safe web search and extracted-content cache.

The cache is intentionally independent of the MCP broker and uses only the
Python standard library. Search keys are one-way digests; query text is never
stored in SQLite.
"""

from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import json
import os
import sqlite3
import threading
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

CACHE_DB_FILENAME = "cache.sqlite3"
SCHEMA_VERSION = 1
SEARCH_TTL_SECONDS = 2 * 60 * 60
NEWS_SEARCH_TTL_SECONDS = 30 * 60
GENERAL_CONTENT_TTL_SECONDS = 24 * 60 * 60
CODE_CONTENT_TTL_SECONDS = 24 * 60 * 60
DOCUMENTATION_CONTENT_TTL_SECONDS = 7 * 24 * 60 * 60
PDF_CONTENT_TTL_SECONDS = 30 * 24 * 60 * 60

_TRACKING_PARAMETERS = {
    "_ga",
    "_gl",
    "dclid",
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "msclkid",
    "ref",
    "ref_",
    "referrer",
    "vero_conv",
    "vero_id",
    "yclid",
}
_CODE_HOSTS = {
    "bitbucket.org",
    "gist.github.com",
    "github.com",
    "gitlab.com",
    "raw.githubusercontent.com",
}
_DOCUMENTATION_HOSTS = {
    "developer.apple.com",
    "developer.mozilla.org",
    "docs.github.com",
    "docs.microsoft.com",
    "docs.python.org",
    "go.dev",
    "kotlinlang.org",
    "learn.microsoft.com",
    "nodejs.org",
    "pkg.go.dev",
    "readthedocs.io",
    "rust-lang.github.io",
}
_CODE_MEDIA_TYPES = {
    "application/javascript",
    "application/x-httpd-php",
    "application/x-sh",
    "text/css",
    "text/javascript",
    "text/x-c",
    "text/x-c++",
    "text/x-go",
    "text/x-java-source",
    "text/x-python",
    "text/x-rust",
    "text/x-shellscript",
}


@dataclass(frozen=True)
class SearchCacheHit:
    payload: dict[str, Any]
    created_at: int
    age_seconds: float


@dataclass(frozen=True)
class ContentCacheHit:
    content: bytes
    content_type: str
    final_url: str | None
    created_at: int
    age_seconds: float


@dataclass(frozen=True)
class CleanupStats:
    expired_search_entries: int = 0
    expired_content_entries: int = 0
    missing_blobs: int = 0
    orphaned_blobs: int = 0
    evicted_entries: int = 0
    bytes_removed: int = 0


def normalize_query(query: str) -> str:
    """Normalize equivalent query spellings without retaining the source text."""
    return " ".join(unicodedata.normalize("NFKC", str(query)).split()).casefold()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_tracking_parameter(name: str) -> bool:
    lowered = name.casefold()
    return lowered.startswith("utm_") or lowered in _TRACKING_PARAMETERS


def _canonical_hostname(hostname: str) -> str:
    """Return one ASCII form for equivalent DNS names and IP literals."""
    host = hostname.rstrip(".")
    try:
        return ipaddress.ip_address(host).compressed.lower()
    except ValueError:
        return host.encode("idna").decode("ascii").lower()


def canonicalize_url(url: str) -> str:
    """Canonicalize identity details without changing path/query semantics."""
    parsed = urllib.parse.urlsplit(str(url).strip())
    scheme = parsed.scheme.lower()
    hostname = _canonical_hostname(parsed.hostname or "")
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = parsed.port
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = hostname
    if port is not None and not default_port:
        netloc = f"{netloc}:{port}"

    query_items = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    filtered_query = urllib.parse.urlencode(
        [(key, value) for key, value in query_items if not _is_tracking_parameter(key)],
        doseq=True,
    )
    return urllib.parse.urlunsplit(
        (scheme, netloc, parsed.path or "/", filtered_query, "")
    )


def content_ttl_seconds(url: str, content_type: str) -> int:
    """Classify extracted content into the configured freshness window."""
    canonical = canonicalize_url(url)
    parsed = urllib.parse.urlsplit(canonical)
    host = (parsed.hostname or "").lower()
    media_type = str(content_type).split(";", 1)[0].strip().lower()
    if media_type == "application/pdf" or parsed.path.lower().endswith(".pdf"):
        return PDF_CONTENT_TTL_SECONDS
    if (
        host in _DOCUMENTATION_HOSTS
        or host.endswith(".readthedocs.io")
        or host.startswith("docs.")
        or host.startswith("developer.")
    ):
        return DOCUMENTATION_CONTENT_TTL_SECONDS
    if host in _CODE_HOSTS or host.endswith(".github.com") or media_type in _CODE_MEDIA_TYPES:
        return CODE_CONTENT_TTL_SECONDS
    return GENERAL_CONTENT_TTL_SECONDS


class WebCache:
    """SQLite metadata plus private atomic blob storage for web data.

    All public operations are best effort. An unavailable or corrupt cache is
    treated as a miss so cache failures can never break broker requests.
    """

    def __init__(
        self,
        root: Path,
        max_content_bytes: int = 50 * 1024**3,
        eviction_watermark_bytes: int = 45 * 1024**3,
        clock: Callable[[], float] = time.time,
    ):
        self.root = Path(root).expanduser()
        self.db_path = self.root / CACHE_DB_FILENAME
        self.content_dir = self.root / "content"
        self.max_content_bytes = max(0, int(max_content_bytes))
        self.eviction_watermark_bytes = max(
            0, min(int(eviction_watermark_bytes), self.max_content_bytes)
        )
        self._clock = clock
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._cleanup_thread: threading.Thread | None = None
        self._conn: sqlite3.Connection | None = None
        self.available = False
        try:
            self._initialize()
        except Exception:
            self._discard_connection()

    def _initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.content_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.content_dir, 0o700)
        conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
        self._conn = conn
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current_version > SCHEMA_VERSION:
            raise RuntimeError("cache schema is newer than this local-search version")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS search_entries (
                query_hash TEXT NOT NULL,
                variant_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                last_accessed_at INTEGER NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0,
                size_bytes INTEGER NOT NULL,
                PRIMARY KEY(query_hash, variant_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_search_expires ON search_entries(expires_at);
            CREATE INDEX IF NOT EXISTS idx_search_accessed ON search_entries(last_accessed_at);

            CREATE TABLE IF NOT EXISTS content_entries (
                url_hash TEXT PRIMARY KEY,
                canonical_url TEXT NOT NULL,
                final_url TEXT,
                blob_relpath TEXT NOT NULL UNIQUE,
                content_type TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                last_accessed_at INTEGER NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0,
                size_bytes INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_content_expires ON content_entries(expires_at);
            CREATE INDEX IF NOT EXISTS idx_content_accessed ON content_entries(last_accessed_at);
            """
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        self._secure_files()
        self.available = True

    def _discard_connection(self) -> None:
        conn, self._conn = self._conn, None
        self.available = False
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _secure_files(self) -> None:
        for path in self.root.glob(f"{CACHE_DB_FILENAME}*"):
            if path.is_file():
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass

    def get_search(self, query: str, *, variant: str) -> SearchCacheHit | None:
        if not self.available or self._conn is None:
            return None
        now = self._clock()
        query_hash = _digest(normalize_query(query))
        variant_hash = _digest(str(variant))
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT payload_json, created_at, expires_at FROM search_entries "
                    "WHERE query_hash = ? AND variant_hash = ?",
                    (query_hash, variant_hash),
                ).fetchone()
                if row is None:
                    return None
                if int(row[2]) <= now:
                    self._conn.execute(
                        "DELETE FROM search_entries WHERE query_hash = ? AND variant_hash = ?",
                        (query_hash, variant_hash),
                    )
                    self._conn.commit()
                    return None
                payload = json.loads(str(row[0]))
                if not isinstance(payload, dict):
                    return None
                self._conn.execute(
                    "UPDATE search_entries SET last_accessed_at = ?, hit_count = hit_count + 1 "
                    "WHERE query_hash = ? AND variant_hash = ?",
                    (int(now), query_hash, variant_hash),
                )
                self._conn.commit()
            return SearchCacheHit(
                payload=payload,
                created_at=int(row[1]),
                age_seconds=max(0.0, now - int(row[1])),
            )
        except Exception:
            return None

    def put_search(
        self,
        query: str,
        payload: dict[str, Any],
        *,
        variant: str,
        news: bool = False,
    ) -> None:
        if not self.available or self._conn is None:
            return
        try:
            safe_payload = dict(payload)
            safe_payload.pop("query", None)
            safe_payload.pop("text", None)
            payload_json = json.dumps(
                safe_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
            now = int(self._clock())
            ttl = NEWS_SEARCH_TTL_SECONDS if news else SEARCH_TTL_SECONDS
            values = (
                _digest(normalize_query(query)),
                _digest(str(variant)),
                payload_json,
                now,
                now + ttl,
                now,
                len(payload_json.encode("utf-8")),
            )
            with self._lock:
                assert self._conn is not None
                self._conn.execute(
                    """
                    INSERT INTO search_entries(
                        query_hash, variant_hash, payload_json, created_at,
                        expires_at, last_accessed_at, hit_count, size_bytes
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                    ON CONFLICT(query_hash, variant_hash) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at,
                        last_accessed_at = excluded.last_accessed_at,
                        hit_count = 0,
                        size_bytes = excluded.size_bytes
                    """,
                    values,
                )
                self._conn.commit()
                self._secure_files()
        except Exception:
            return

    def get_content(self, url: str) -> ContentCacheHit | None:
        if not self.available or self._conn is None:
            return None
        now = self._clock()
        url_hash = _digest(canonicalize_url(url))
        try:
            with self._lock:
                assert self._conn is not None
                row = self._conn.execute(
                    "SELECT blob_relpath, content_type, final_url, created_at, expires_at "
                    "FROM content_entries WHERE url_hash = ?",
                    (url_hash,),
                ).fetchone()
                if row is None:
                    return None
                blob_path = self.root / str(row[0])
                if int(row[4]) <= now or not blob_path.is_file():
                    self._conn.execute("DELETE FROM content_entries WHERE url_hash = ?", (url_hash,))
                    self._conn.commit()
                    try:
                        blob_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return None
                content = blob_path.read_bytes()
                self._conn.execute(
                    "UPDATE content_entries SET last_accessed_at = ?, hit_count = hit_count + 1 "
                    "WHERE url_hash = ?",
                    (int(now), url_hash),
                )
                self._conn.commit()
            return ContentCacheHit(
                content=content,
                content_type=str(row[1]),
                final_url=str(row[2]) if row[2] is not None else None,
                created_at=int(row[3]),
                age_seconds=max(0.0, now - int(row[3])),
            )
        except Exception:
            return None

    def put_content(
        self,
        url: str,
        content: bytes | str,
        *,
        content_type: str,
        final_url: str | None = None,
    ) -> None:
        if not self.available or self._conn is None:
            return
        try:
            content_bytes = content.encode("utf-8") if isinstance(content, str) else bytes(content)
            if len(content_bytes) > self.max_content_bytes:
                return
            canonical_url = canonicalize_url(url)
            canonical_final_url = canonicalize_url(final_url) if final_url else None
            url_hash = _digest(canonical_url)
            blob_relpath = f"content/{url_hash[:2]}/{url_hash}"
            blob_path = self.root / blob_relpath
            now = int(self._clock())
            expires_at = now + content_ttl_seconds(canonical_final_url or canonical_url, content_type)
            with self._lock:
                assert self._conn is not None
                blob_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(blob_path.parent, 0o700)
                temp_path = blob_path.with_name(
                    f".{blob_path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
                )
                try:
                    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(content_bytes)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(temp_path, 0o600)
                    os.replace(temp_path, blob_path)
                    os.chmod(blob_path, 0o600)
                finally:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                self._conn.execute(
                    """
                    INSERT INTO content_entries(
                        url_hash, canonical_url, final_url, blob_relpath,
                        content_type, created_at, expires_at, last_accessed_at,
                        hit_count, size_bytes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    ON CONFLICT(url_hash) DO UPDATE SET
                        canonical_url = excluded.canonical_url,
                        final_url = excluded.final_url,
                        blob_relpath = excluded.blob_relpath,
                        content_type = excluded.content_type,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at,
                        last_accessed_at = excluded.last_accessed_at,
                        hit_count = 0,
                        size_bytes = excluded.size_bytes
                    """,
                    (
                        url_hash,
                        canonical_url,
                        canonical_final_url,
                        blob_relpath,
                        str(content_type),
                        now,
                        expires_at,
                        now,
                        len(content_bytes),
                    ),
                )
                self._conn.commit()
                self._secure_files()
                self._lru_evict_locked()
        except Exception:
            return

    def _delete_content_rows_locked(self, rows: list[tuple[str, str, int]]) -> tuple[int, int]:
        assert self._conn is not None
        removed = 0
        bytes_removed = 0
        for url_hash, blob_relpath, size_bytes in rows:
            self._conn.execute("DELETE FROM content_entries WHERE url_hash = ?", (url_hash,))
            try:
                (self.root / blob_relpath).unlink(missing_ok=True)
            except OSError:
                pass
            removed += 1
            bytes_removed += max(0, int(size_bytes))
        self._conn.commit()
        return removed, bytes_removed

    def _lru_evict_locked(self) -> tuple[int, int]:
        assert self._conn is not None
        total = int(
            self._conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM content_entries"
            ).fetchone()[0]
        )
        if total <= self.eviction_watermark_bytes:
            return 0, 0
        rows = self._conn.execute(
            "SELECT url_hash, blob_relpath, size_bytes FROM content_entries "
            "ORDER BY last_accessed_at ASC, created_at ASC, url_hash ASC"
        ).fetchall()
        victims: list[tuple[str, str, int]] = []
        for row in rows:
            if total <= self.eviction_watermark_bytes:
                break
            victim = (str(row[0]), str(row[1]), int(row[2]))
            victims.append(victim)
            total -= max(0, victim[2])
        return self._delete_content_rows_locked(victims)

    def cleanup(self) -> CleanupStats:
        if not self.available or self._conn is None:
            return CleanupStats()
        expired_search_entries = 0
        expired_content_entries = 0
        missing_blobs = 0
        orphaned_blobs = 0
        evicted_entries = 0
        bytes_removed = 0
        lock_handle = None
        try:
            with self._lock:
                assert self._conn is not None
                lock_path = self.root / ".cleanup.lock"
                lock_handle = open(lock_path, "a+b")
                os.chmod(lock_path, 0o600)
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                now = int(self._clock())
                expired_search_entries = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM search_entries WHERE expires_at <= ?", (now,)
                    ).fetchone()[0]
                )
                self._conn.execute("DELETE FROM search_entries WHERE expires_at <= ?", (now,))

                expired_rows = [
                    (str(row[0]), str(row[1]), int(row[2]))
                    for row in self._conn.execute(
                        "SELECT url_hash, blob_relpath, size_bytes FROM content_entries "
                        "WHERE expires_at <= ?",
                        (now,),
                    ).fetchall()
                ]
                expired_content_entries, expired_bytes = self._delete_content_rows_locked(
                    expired_rows
                )
                bytes_removed += expired_bytes

                tracked_rows = self._conn.execute(
                    "SELECT url_hash, blob_relpath, size_bytes FROM content_entries"
                ).fetchall()
                missing_rows = [
                    (str(row[0]), str(row[1]), int(row[2]))
                    for row in tracked_rows
                    if not (self.root / str(row[1])).is_file()
                ]
                missing_blobs, missing_bytes = self._delete_content_rows_locked(missing_rows)
                bytes_removed += missing_bytes

                tracked_paths = {
                    str(row[0])
                    for row in self._conn.execute(
                        "SELECT blob_relpath FROM content_entries"
                    ).fetchall()
                }
                if self.content_dir.exists():
                    for path in self.content_dir.rglob("*"):
                        if not path.is_file():
                            continue
                        relpath = path.relative_to(self.root).as_posix()
                        if relpath in tracked_paths:
                            continue
                        try:
                            orphan_size = path.stat().st_size
                            path.unlink()
                        except OSError:
                            continue
                        orphaned_blobs += 1
                        bytes_removed += orphan_size

                evicted_entries, evicted_bytes = self._lru_evict_locked()
                bytes_removed += evicted_bytes
                self._conn.commit()
                self._secure_files()
        except Exception:
            pass
        finally:
            if lock_handle is not None:
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                    lock_handle.close()
                except OSError:
                    pass
        return CleanupStats(
            expired_search_entries=expired_search_entries,
            expired_content_entries=expired_content_entries,
            missing_blobs=missing_blobs,
            orphaned_blobs=orphaned_blobs,
            evicted_entries=evicted_entries,
            bytes_removed=bytes_removed,
        )

    def start_cleanup(self, interval_seconds: int = 900) -> None:
        if not self.available or self._cleanup_thread is not None:
            return
        interval = max(1, int(interval_seconds))

        def run_cleanup() -> None:
            while not self._stop_event.wait(interval):
                self.cleanup()

        self._cleanup_thread = threading.Thread(
            target=run_cleanup,
            name="local-search-cache-cleanup",
            daemon=True,
        )
        self._cleanup_thread.start()

    def close(self) -> None:
        self._stop_event.set()
        thread, self._cleanup_thread = self._cleanup_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            conn, self._conn = self._conn, None
            self.available = False
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
