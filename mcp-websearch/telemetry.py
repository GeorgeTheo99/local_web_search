#!/usr/bin/env python3
"""Private, query-free SQLite telemetry for local-search.

Only bounded operational fields from :class:`SearchEvent` and URL hostnames
from :class:`FetchEvent` are persisted. Search queries, full URLs, paths, result
content, headers, credentials, and API keys are never accepted by this module's
schema or event types.
"""

from __future__ import annotations

import argparse
import collections
import ipaddress
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("websearch-mcp.telemetry")

DB_FILENAME = "telemetry.sqlite3"
SCHEMA_VERSION = 8
VALID_WINDOWS = {"24h": 24 * 60 * 60, "7d": 7 * 24 * 60 * 60, "30d": 30 * 24 * 60 * 60}
ACTIVITY_BUCKET_SECONDS = {"24h": 60 * 60, "7d": 6 * 60 * 60, "30d": 24 * 60 * 60}

_SEARCH_STATUSES = {"ok", "empty", "degraded", "error", "timeout"}
_BACKENDS = {"none", "brave"}
_MODES = {"normal", "sensitive", "maximum_recall", "disabled"}
_FALLBACK_REASONS = {
    "brave_degraded",
    "brave_empty",
    "brave_timeout",
    "brave_circuit_open",
    "brave_error",
    "batch_deadline",
}
_PROVIDER_STATES = {"ok", "empty", "degraded", "error", "timeout", "circuit_open"}
_CIRCUIT_STATES = {"closed", "open", "half_open", "unknown"}
_CREDENTIAL_MODES = {"keyed", "keyless", "none"}
_ERROR_KINDS = {"http", "timeout", "connection", "invalid_response", "internal_error", "none"}
_CIRCUIT_TRANSITIONS = {"none", "opened", "reopened", "recovered"}
_FETCH_OUTCOMES = {
    "success",
    "http_error",
    "connection_error",
    "timeout",
    "challenge",
    "extraction_error",
    "proxy_success",
    "proxy_error",
    "unsafe_url",
    "policy_error",
}
_FETCH_TIERS = {"direct", "proxy", "browser"}
_FETCH_PROVIDERS = {"cache", "direct", "jina", "decodo", "none"}
# Mirrors server._FALLBACK_HTTP_STATUSES plus the non-HTTP trigger kinds. Keep
# in sync when the fallback eligibility matrix changes.
_FETCH_TRIGGERS = {
    "none",
    "empty",
    "antibot",
    "http_403",
    "http_408",
    "http_429",
    "http_500",
    "http_502",
    "http_503",
    "http_504",
    "http_520",
    "http_521",
    "http_522",
    "http_523",
    "http_524",
    "http_527",
    "timeout",
    "connection_error",
    "tls_error",
    "extraction_error",
    "unsafe_url",
}
_FETCH_OPERATION_OUTCOMES = {"success", "error"}


class InvalidWindow(ValueError):
    """Raised when a stats request uses an unsupported aggregate window."""


class TelemetryUnavailable(RuntimeError):
    """Raised when telemetry is disabled or its private store cannot be opened."""


@dataclass(frozen=True)
class ProviderEvent:
    provider: str
    state: str
    attempts: int = 0
    result_count: int = 0
    latency_ms: float = 0.0
    error_kind: str = "none"
    http_status: int | None = None
    credential_mode: str = "none"
    circuit_before: str = "unknown"
    circuit_after: str = "unknown"
    circuit_transition: str = "none"
    circuit_failures: int = 0


@dataclass(frozen=True)
class SearchEvent:
    status: str
    backend: str
    mode: str
    requested_count: int
    result_count: int
    total_latency_ms: float
    fallback_reason: str | None = None
    providers: tuple[ProviderEvent, ...] = field(default_factory=tuple)
    created_at: int = field(default_factory=lambda: int(time.time()))
    created_at_ns: int = field(default_factory=time.time_ns)
    cache_hit: bool = False


@dataclass(frozen=True)
class FetchEvent:
    url_host: str
    http_status: int | None
    outcome: str
    tier_used: str
    bytes: int | None
    latency_ms: float
    created_at: int = field(default_factory=lambda: int(time.time()))
    created_at_ns: int = field(default_factory=time.time_ns)
    cache_hit: bool = False
    provider: str = "direct"
    trigger: str = "none"
    provider_http_status: int | None = None


@dataclass(frozen=True)
class FetchOperation:
    """One terminal, host-only outcome for an entire web_fetch operation."""

    url_host: str
    outcome: str
    provider: str
    trigger: str
    http_status: int | None
    bytes: int | None
    latency_ms: float
    created_at: int = field(default_factory=lambda: int(time.time()))
    created_at_ns: int = field(default_factory=time.time_ns)
    cache_hit: bool = False


@dataclass
class _FlushMarker:
    completed: threading.Event = field(default_factory=threading.Event)


def classify_error(error: str | None, http_status: int | None = None) -> str:
    """Reduce provider errors to a fixed, query-free category."""
    if (http_status is not None and http_status >= 400) or (error or "").startswith("HTTP "):
        return "http"
    lowered = (error or "").lower()
    if not lowered:
        return "none"
    if "timeout" in lowered or "deadline" in lowered:
        return "timeout"
    if "connect" in lowered:
        return "connection"
    if "json" in lowered or "payload" in lowered:
        return "invalid_response"
    return "internal_error"


def _enum(value: str | None, allowed: set[str], default: str) -> str:
    return value if value in allowed else default


def _url_host(value: str) -> str:
    """Normalize a bare hostname and reject values that could contain URL details."""
    raw = str(value).strip().lower().rstrip(".")
    if not raw or len(raw) > 253 or any(char in raw for char in "/?#@"):
        return "unknown"
    try:
        return ipaddress.ip_address(raw).compressed
    except ValueError:
        pass
    try:
        ascii_host = raw.encode("idna").decode("ascii")
    except UnicodeError:
        return "unknown"
    labels = ascii_host.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or re.fullmatch(r"[a-z0-9-]+", label) is None
        for label in labels
    ):
        return "unknown"
    return ascii_host


def _count_map(rows: list[sqlite3.Row], key: str = "name") -> dict[str, int]:
    return {str(row[key]): int(row["count"]) for row in rows}


def _iso_timestamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class TelemetryStore:
    """Best-effort SQLite writer with a non-blocking, bounded event queue."""

    def __init__(self, data_dir: str | Path, *, enabled: bool = True, queue_size: int = 10_000):
        self.data_dir = Path(data_dir).expanduser()
        self.db_path = self.data_dir / DB_FILENAME
        self.enabled = enabled
        self.available = False
        self._reason = "disabled" if not enabled else "unavailable"
        self._queue: queue.Queue[
            SearchEvent | FetchEvent | FetchOperation | _FlushMarker | None
        ] = queue.Queue(
            maxsize=queue_size
        )
        self._operation_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._dropped_events = 0
        self._pending_drop_times: collections.deque[int] = collections.deque()
        self._worker: threading.Thread | None = None
        if enabled:
            try:
                self._initialize()
            except Exception as exc:
                self._reason = "unavailable"
                logger.warning("Telemetry unavailable during initialization (%s)", type(exc).__name__)
            else:
                self.available = True
                self._reason = "ok"
                self._worker = threading.Thread(
                    target=self._writer_loop,
                    name="local-search-telemetry",
                    daemon=True,
                )
                self._worker.start()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=0.25)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 250")
        conn.execute("PRAGMA temp_store = MEMORY")
        return conn

    def _initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        with self._connect() as conn:
            current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError("telemetry schema is newer than this local-search version")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS search_events (
                    id INTEGER PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    requested_count INTEGER NOT NULL,
                    result_count INTEGER NOT NULL,
                    fallback_reason TEXT,
                    total_latency_ms REAL NOT NULL,
                    cache_hit INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_search_events_created_at
                    ON search_events(created_at);

                CREATE TABLE IF NOT EXISTS provider_events (
                    id INTEGER PRIMARY KEY,
                    search_event_id INTEGER NOT NULL REFERENCES search_events(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    result_count INTEGER NOT NULL,
                    latency_ms REAL NOT NULL,
                    error_kind TEXT NOT NULL,
                    http_status INTEGER,
                    credential_mode TEXT NOT NULL,
                    circuit_before TEXT NOT NULL,
                    circuit_after TEXT NOT NULL,
                    circuit_transition TEXT NOT NULL,
                    circuit_failures INTEGER NOT NULL,
                    UNIQUE(search_event_id, provider)
                );
                CREATE INDEX IF NOT EXISTS idx_provider_events_search
                    ON provider_events(search_event_id);
                CREATE INDEX IF NOT EXISTS idx_provider_events_provider_state
                    ON provider_events(provider, state);

                CREATE TABLE IF NOT EXISTS fetch_events (
                    id INTEGER PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    url_host TEXT NOT NULL,
                    http_status INTEGER,
                    outcome TEXT NOT NULL,
                    tier_used TEXT NOT NULL,
                    bytes INTEGER,
                    latency_ms REAL NOT NULL,
                    cache_hit INTEGER NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL DEFAULT 'direct',
                    trigger TEXT NOT NULL DEFAULT 'none',
                    provider_http_status INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_fetch_events_created_at
                    ON fetch_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_fetch_events_host
                    ON fetch_events(url_host);

                CREATE TABLE IF NOT EXISTS fetch_operations (
                    id INTEGER PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    url_host TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    http_status INTEGER,
                    bytes INTEGER,
                    latency_ms REAL NOT NULL,
                    cache_hit INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_fetch_operations_created_at
                    ON fetch_operations(created_at);
                CREATE INDEX IF NOT EXISTS idx_fetch_operations_provider
                    ON fetch_operations(provider, created_at);

                CREATE TABLE IF NOT EXISTS telemetry_health_events (
                    id INTEGER PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    dropped_events INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_telemetry_health_created_at
                    ON telemetry_health_events(created_at);

                CREATE TABLE IF NOT EXISTS telemetry_meta (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    reset_before_ns INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO telemetry_meta(singleton, reset_before_ns)
                    VALUES (1, 0);
                """
            )
            # v2 -> v3: cache outcomes are explicit and provider-free. Checking
            # table columns also makes initialization safe for a fresh schema.
            if current_version < 3:
                search_columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(search_events)")
                }
                if "cache_hit" not in search_columns:
                    conn.execute(
                        "ALTER TABLE search_events "
                        "ADD COLUMN cache_hit INTEGER NOT NULL DEFAULT 0"
                    )
                fetch_columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(fetch_events)")
                }
                if "cache_hit" not in fetch_columns:
                    conn.execute(
                        "ALTER TABLE fetch_events "
                        "ADD COLUMN cache_hit INTEGER NOT NULL DEFAULT 0"
                    )

            if current_version < 7:
                fetch_columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(fetch_events)")
                }
                if "provider" not in fetch_columns:
                    conn.execute(
                        "ALTER TABLE fetch_events "
                        "ADD COLUMN provider TEXT NOT NULL DEFAULT 'direct'"
                    )
                    conn.execute(
                        "UPDATE fetch_events SET provider = CASE "
                        "WHEN cache_hit = 1 THEN 'cache' "
                        "WHEN tier_used = 'proxy' THEN 'jina' "
                        "ELSE 'direct' END"
                    )
                if "trigger" not in fetch_columns:
                    conn.execute(
                        "ALTER TABLE fetch_events "
                        "ADD COLUMN trigger TEXT NOT NULL DEFAULT 'none'"
                    )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_fetch_events_provider "
                    "ON fetch_events(provider, created_at)"
                )

            if current_version < 8:
                fetch_columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(fetch_events)")
                }
                if "provider_http_status" not in fetch_columns:
                    conn.execute(
                        "ALTER TABLE fetch_events ADD COLUMN provider_http_status INTEGER"
                    )

            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._secure_files()

    def _secure_files(self) -> None:
        for path in self.data_dir.glob(f"{DB_FILENAME}*"):
            if path.is_file():
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass

    def status(self) -> dict[str, Any]:
        with self._counter_lock:
            dropped = self._dropped_events
        return {
            "enabled": self.enabled,
            "available": self.available,
            "storage": "sqlite" if self.enabled else "disabled",
            "dropped_events": dropped,
            "reason": self._reason,
        }

    def _note_drop(self) -> None:
        with self._counter_lock:
            self._dropped_events += 1
            self._pending_drop_times.append(time.time_ns())

    def record(self, event: SearchEvent) -> bool:
        """Queue an event without waiting for SQLite or delaying a search response."""
        if not self.available:
            return False
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            self._note_drop()
            logger.warning("Telemetry queue full; dropping operational event")
            return False

    def record_fetch(self, event: FetchEvent) -> bool:
        """Queue a host-only fetch attempt without delaying the fetch response."""
        if not self.available:
            return False
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            self._note_drop()
            logger.warning("Telemetry queue full; dropping operational event")
            return False

    def record_fetch_operation(self, operation: FetchOperation) -> bool:
        """Queue one terminal host-only outcome for an entire fetch operation."""
        if not self.available:
            return False
        try:
            self._queue.put_nowait(operation)
            return True
        except queue.Full:
            self._note_drop()
            logger.warning("Telemetry queue full; dropping operational event")
            return False

    def _writer_loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                if isinstance(item, _FlushMarker):
                    item.completed.set()
                    continue
                try:
                    with self._operation_lock:
                        if isinstance(item, FetchEvent):
                            self._write_fetch_event(item)
                        elif isinstance(item, FetchOperation):
                            self._write_fetch_operation(item)
                        else:
                            self._write_event(item)
                except Exception as exc:
                    self._note_drop()
                    logger.warning("Telemetry write failed (%s)", type(exc).__name__)
            finally:
                self._queue.task_done()

    def _pending_drops(self) -> tuple[int, ...]:
        with self._counter_lock:
            return tuple(self._pending_drop_times)

    def _mark_drops_persisted(self, count: int) -> None:
        if count <= 0:
            return
        with self._counter_lock:
            for _ in range(min(count, len(self._pending_drop_times))):
                self._pending_drop_times.popleft()

    @staticmethod
    def _persist_drop_batch(
        conn: sqlite3.Connection,
        pending: tuple[int, ...],
        reset_before_ns: int,
    ) -> None:
        eligible = [timestamp for timestamp in pending if timestamp > reset_before_ns]
        if eligible:
            conn.execute(
                """
                INSERT INTO telemetry_health_events(
                    created_at, created_at_ns, dropped_events
                ) VALUES (?, ?, ?)
                """,
                (eligible[0] // 1_000_000_000, eligible[0], len(eligible)),
            )

    def _write_event(self, event: SearchEvent) -> None:
        status = _enum(event.status, _SEARCH_STATUSES, "error")
        backend = _enum(event.backend, _BACKENDS, "none")
        mode = _enum(event.mode, _MODES, "disabled")
        fallback_reason = (
            None
            if event.cache_hit
            else (_enum(event.fallback_reason, _FALLBACK_REASONS, "") or None)
        )
        with self._connect() as conn:
            # Serialize with reset across every process. Events queued before a
            # reset carry an older nanosecond timestamp and are discarded.
            conn.execute("BEGIN IMMEDIATE")
            reset_before_ns = int(
                conn.execute(
                    "SELECT reset_before_ns FROM telemetry_meta WHERE singleton = 1"
                ).fetchone()[0]
            )
            pending_drops = self._pending_drops()
            self._persist_drop_batch(conn, pending_drops, reset_before_ns)
            if int(event.created_at_ns) > reset_before_ns:
                cursor = conn.execute(
                    """
                    INSERT INTO search_events(
                        created_at, status, backend, mode, requested_count,
                        result_count, fallback_reason, total_latency_ms, cache_hit
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        max(0, int(event.created_at)),
                        status,
                        backend,
                        mode,
                        max(0, int(event.requested_count)),
                        max(0, int(event.result_count)),
                        fallback_reason,
                        max(0.0, float(event.total_latency_ms)),
                        int(bool(event.cache_hit)),
                    ),
                )
                search_id = int(cursor.lastrowid)
                seen_providers: set[str] = set()
                providers = () if event.cache_hit else event.providers
                for provider in providers:
                    if provider.provider in seen_providers:
                        continue
                    seen_providers.add(provider.provider)
                    http_status = provider.http_status
                    if http_status is not None and not 100 <= int(http_status) <= 599:
                        http_status = None
                    conn.execute(
                        """
                        INSERT INTO provider_events(
                            search_event_id, provider, state, attempts, result_count,
                            latency_ms, error_kind, http_status, credential_mode,
                            circuit_before, circuit_after, circuit_transition,
                            circuit_failures
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            search_id,
                            provider.provider,
                            _enum(provider.state, _PROVIDER_STATES, "error"),
                            max(0, int(provider.attempts)),
                            max(0, int(provider.result_count)),
                            max(0.0, float(provider.latency_ms)),
                            _enum(provider.error_kind, _ERROR_KINDS, "internal_error"),
                            http_status,
                            _enum(provider.credential_mode, _CREDENTIAL_MODES, "none"),
                            _enum(provider.circuit_before, _CIRCUIT_STATES, "unknown"),
                            _enum(provider.circuit_after, _CIRCUIT_STATES, "unknown"),
                            _enum(provider.circuit_transition, _CIRCUIT_TRANSITIONS, "none"),
                            max(0, int(provider.circuit_failures)),
                        ),
                    )
        self._mark_drops_persisted(len(pending_drops))
        self._secure_files()

    def _write_fetch_event(self, event: FetchEvent) -> None:
        http_status = event.http_status
        if http_status is not None and not 100 <= int(http_status) <= 599:
            http_status = None
        provider_http_status = event.provider_http_status
        if provider_http_status is not None and not 100 <= int(provider_http_status) <= 599:
            provider_http_status = None
        body_bytes = None if event.bytes is None else max(0, int(event.bytes))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reset_before_ns = int(
                conn.execute(
                    "SELECT reset_before_ns FROM telemetry_meta WHERE singleton = 1"
                ).fetchone()[0]
            )
            pending_drops = self._pending_drops()
            self._persist_drop_batch(conn, pending_drops, reset_before_ns)
            if int(event.created_at_ns) > reset_before_ns:
                conn.execute(
                    """
                    INSERT INTO fetch_events(
                        created_at, url_host, http_status, outcome, tier_used,
                        bytes, latency_ms, cache_hit, provider, trigger,
                        provider_http_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        max(0, int(event.created_at)),
                        _url_host(event.url_host),
                        http_status,
                        _enum(event.outcome, _FETCH_OUTCOMES, "connection_error"),
                        _enum(event.tier_used, _FETCH_TIERS, "direct"),
                        body_bytes,
                        max(0.0, float(event.latency_ms)),
                        int(bool(event.cache_hit)),
                        _enum(event.provider, _FETCH_PROVIDERS, "none"),
                        _enum(event.trigger, _FETCH_TRIGGERS, "none"),
                        provider_http_status,
                    ),
                )
        self._mark_drops_persisted(len(pending_drops))
        self._secure_files()

    def _write_fetch_operation(self, operation: FetchOperation) -> None:
        http_status = operation.http_status
        if http_status is not None and not 100 <= int(http_status) <= 599:
            http_status = None
        body_bytes = None if operation.bytes is None else max(0, int(operation.bytes))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reset_before_ns = int(
                conn.execute(
                    "SELECT reset_before_ns FROM telemetry_meta WHERE singleton = 1"
                ).fetchone()[0]
            )
            pending_drops = self._pending_drops()
            self._persist_drop_batch(conn, pending_drops, reset_before_ns)
            if int(operation.created_at_ns) > reset_before_ns:
                conn.execute(
                    """
                    INSERT INTO fetch_operations(
                        created_at, url_host, outcome, provider, trigger,
                        http_status, bytes, latency_ms, cache_hit
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        max(0, int(operation.created_at)),
                        _url_host(operation.url_host),
                        _enum(operation.outcome, _FETCH_OPERATION_OUTCOMES, "error"),
                        _enum(operation.provider, _FETCH_PROVIDERS, "none"),
                        _enum(operation.trigger, _FETCH_TRIGGERS, "none"),
                        http_status,
                        body_bytes,
                        max(0.0, float(operation.latency_ms)),
                        int(bool(operation.cache_hit)),
                    ),
                )
        self._mark_drops_persisted(len(pending_drops))
        self._secure_files()

    def _persist_pending_drops(self) -> None:
        with self._operation_lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                pending_drops = self._pending_drops()
                if not pending_drops:
                    return
                reset_before_ns = int(
                    conn.execute(
                        "SELECT reset_before_ns FROM telemetry_meta WHERE singleton = 1"
                    ).fetchone()[0]
                )
                self._persist_drop_batch(conn, pending_drops, reset_before_ns)
            # Keep the in-memory dequeue in the same operation critical section
            # as the committed insert so a concurrent stats call cannot replay it.
            self._mark_drops_persisted(len(pending_drops))
        self._secure_files()

    def flush(self, timeout: float = 2.0) -> bool:
        if not self.available:
            return False
        marker = _FlushMarker()
        try:
            self._queue.put(marker, timeout=max(0.01, timeout))
        except queue.Full:
            return False
        return marker.completed.wait(timeout=max(0.01, timeout))

    def stats(self, window: str = "24h", *, now: int | None = None) -> dict[str, Any]:
        if window not in VALID_WINDOWS:
            raise InvalidWindow(f"window must be one of: {', '.join(VALID_WINDOWS)}")
        if not self.available:
            raise TelemetryUnavailable(self._reason)
        flush_complete = self.flush()
        if flush_complete:
            try:
                self._persist_pending_drops()
            except Exception as exc:
                flush_complete = False
                logger.warning("Telemetry health marker write failed (%s)", type(exc).__name__)
        with self._counter_lock:
            pending_drops = len(self._pending_drop_times)
            process_drops = self._dropped_events
        until = int(time.time()) if now is None else int(now)
        since = until - VALID_WINDOWS[window]
        with self._operation_lock, self._connect() as conn:
            # Keep all aggregate SELECTs on one cross-process snapshot.
            conn.execute("BEGIN")
            search_row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       AVG(total_latency_ms) AS avg_latency,
                       MIN(total_latency_ms) AS min_latency,
                       MAX(total_latency_ms) AS max_latency
                FROM search_events WHERE created_at >= ? AND created_at <= ?
                """,
                (since, until),
            ).fetchone()
            status_rows = conn.execute(
                "SELECT status AS name, COUNT(*) AS count FROM search_events "
                "WHERE created_at >= ? AND created_at <= ? GROUP BY status ORDER BY status",
                (since, until),
            ).fetchall()
            backend_rows = conn.execute(
                "SELECT backend AS name, COUNT(*) AS count FROM search_events "
                "WHERE created_at >= ? AND created_at <= ? GROUP BY backend ORDER BY backend",
                (since, until),
            ).fetchall()
            fallback_rows = conn.execute(
                """
                SELECT COALESCE(s.fallback_reason, 'unspecified') AS name, COUNT(*) AS count
                FROM search_events s
                WHERE s.created_at >= ? AND s.created_at <= ?
                  AND s.fallback_reason IS NOT NULL
                GROUP BY COALESCE(s.fallback_reason, 'unspecified')
                ORDER BY count DESC, name
                """,
                (since, until),
            ).fetchall()
            provider_rows = conn.execute(
                """
                SELECT p.provider,
                       COUNT(*) AS selected_searches,
                       SUM(CASE WHEN p.attempts > 0 THEN 1 ELSE 0 END) AS attempted_searches,
                       SUM(p.attempts) AS attempts,
                       SUM(CASE WHEN p.state IN ('error', 'timeout') THEN 1 ELSE 0 END) AS errors,
                       SUM(CASE WHEN p.attempts > 0 AND p.state IN ('error', 'timeout')
                                THEN 1 ELSE 0 END) AS attempt_errors,
                       SUM(CASE WHEN p.http_status = 401 THEN 1 ELSE 0 END) AS http_401s,
                       SUM(CASE WHEN p.http_status = 402 THEN 1 ELSE 0 END) AS payment_required_402s,
                       SUM(CASE WHEN p.http_status = 403 THEN 1 ELSE 0 END) AS http_403s,
                       SUM(CASE WHEN p.http_status = 429 THEN 1 ELSE 0 END) AS rate_limited_429s,
                       SUM(CASE WHEN p.state = 'circuit_open' THEN 1 ELSE 0 END) AS circuit_open_skips,
                       SUM(CASE WHEN p.circuit_transition IN ('opened', 'reopened')
                                THEN 1 ELSE 0 END) AS circuit_trips,
                       SUM(CASE WHEN p.circuit_transition = 'recovered'
                                THEN 1 ELSE 0 END) AS circuit_recoveries,
                       AVG(p.latency_ms) AS avg_latency,
                       MIN(p.latency_ms) AS min_latency,
                       MAX(p.latency_ms) AS max_latency
                FROM provider_events p
                JOIN search_events s ON s.id = p.search_event_id
                WHERE s.created_at >= ? AND s.created_at <= ?
                GROUP BY p.provider ORDER BY p.provider
                """,
                (since, until),
            ).fetchall()
            provider_state_rows = conn.execute(
                """
                SELECT p.provider, p.state, COUNT(*) AS count
                FROM provider_events p
                JOIN search_events s ON s.id = p.search_event_id
                WHERE s.created_at >= ? AND s.created_at <= ?
                GROUP BY p.provider, p.state ORDER BY p.provider, p.state
                """,
                (since, until),
            ).fetchall()
            search_cache_row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(cache_hit), 0) AS hits
                FROM search_events WHERE created_at >= ? AND created_at <= ?
                """,
                (since, until),
            ).fetchone()
            fetch_cache_row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(cache_hit), 0) AS hits
                FROM fetch_operations WHERE created_at >= ? AND created_at <= ?
                """,
                (since, until),
            ).fetchone()
            fetch_operation_row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(outcome = 'success'), 0) AS successes,
                       AVG(latency_ms) AS avg_latency,
                       MIN(latency_ms) AS min_latency,
                       MAX(latency_ms) AS max_latency
                FROM fetch_operations WHERE created_at >= ? AND created_at <= ?
                """,
                (since, until),
            ).fetchone()
            fetch_provider_rows = conn.execute(
                """
                SELECT provider,
                       COUNT(*) AS operations,
                       COALESCE(SUM(outcome = 'success'), 0) AS successes,
                       AVG(latency_ms) AS avg_latency
                FROM fetch_operations
                WHERE created_at >= ? AND created_at <= ?
                GROUP BY provider ORDER BY provider
                """,
                (since, until),
            ).fetchall()
            fetch_attempt_rows = conn.execute(
                """
                SELECT provider,
                       COUNT(*) AS attempts,
                       COALESCE(SUM(outcome IN ('success', 'proxy_success')), 0) AS successes,
                       COALESCE(SUM(outcome NOT IN ('success', 'proxy_success')), 0) AS errors,
                       SUM(CASE WHEN provider_http_status = 401 THEN 1 ELSE 0 END) AS http_401s,
                       SUM(CASE WHEN provider_http_status = 402 THEN 1 ELSE 0 END) AS payment_required_402s,
                       SUM(CASE WHEN provider_http_status = 403 THEN 1 ELSE 0 END) AS http_403s,
                       SUM(CASE WHEN provider_http_status = 429 THEN 1 ELSE 0 END) AS rate_limited_429s,
                       AVG(latency_ms) AS avg_latency
                FROM fetch_events
                WHERE created_at >= ? AND created_at <= ?
                  AND provider IN ('direct', 'decodo', 'jina')
                GROUP BY provider ORDER BY provider
                """,
                (since, until),
            ).fetchall()
            health_row = conn.execute(
                """
                SELECT COALESCE(SUM(dropped_events), 0) AS dropped_events
                FROM telemetry_health_events
                WHERE created_at >= ? AND created_at <= ?
                """,
                (since, until),
            ).fetchone()

        durable_drops = int(health_row["dropped_events"] or 0)
        dropped_events = durable_drops + pending_drops
        total = int(search_row["total"] or 0)
        fallback_count = sum(int(row["count"]) for row in fallback_rows)
        search_cache_total = int(search_cache_row["total"] or 0)
        search_cache_hits = int(search_cache_row["hits"] or 0)
        fetch_cache_total = int(fetch_cache_row["total"] or 0)
        fetch_cache_hits = int(fetch_cache_row["hits"] or 0)
        fetch_total = int(fetch_operation_row["total"] or 0)
        fetch_successes = int(fetch_operation_row["successes"] or 0)
        fetch_attempts = {
            str(row["provider"]): {
                "attempts": int(row["attempts"] or 0),
                "successes": int(row["successes"] or 0),
                "errors": int(row["errors"] or 0),
                "success_rate": round(
                    int(row["successes"] or 0) / int(row["attempts"] or 1), 4
                ),
                "http_401s": int(row["http_401s"] or 0),
                "payment_required_402s": int(row["payment_required_402s"] or 0),
                "http_403s": int(row["http_403s"] or 0),
                "rate_limited_429s": int(row["rate_limited_429s"] or 0),
                "average_latency_ms": round(float(row["avg_latency"] or 0.0), 1),
            }
            for row in fetch_attempt_rows
        }
        fetch_providers = {
            str(row["provider"]): {
                "operations": int(row["operations"] or 0),
                "successes": int(row["successes"] or 0),
                "success_rate": round(
                    int(row["successes"] or 0) / int(row["operations"] or 1), 4
                ),
                "average_latency_ms": round(float(row["avg_latency"] or 0.0), 1),
            }
            for row in fetch_provider_rows
        }
        provider_states: dict[str, dict[str, int]] = {}
        for row in provider_state_rows:
            provider_states.setdefault(str(row["provider"]), {})[str(row["state"])] = int(row["count"])
        providers: dict[str, Any] = {}
        for row in provider_rows:
            provider = str(row["provider"])
            providers[provider] = {
                "selected_searches": int(row["selected_searches"] or 0),
                "attempted_searches": int(row["attempted_searches"] or 0),
                "attempts": int(row["attempts"] or 0),
                "errors": int(row["errors"] or 0),
                "attempt_errors": int(row["attempt_errors"] or 0),
                "http_401s": int(row["http_401s"] or 0),
                "payment_required_402s": int(row["payment_required_402s"] or 0),
                "http_403s": int(row["http_403s"] or 0),
                "rate_limited_429s": int(row["rate_limited_429s"] or 0),
                "circuit_open_skips": int(row["circuit_open_skips"] or 0),
                "circuit_trips": int(row["circuit_trips"] or 0),
                "circuit_recoveries": int(row["circuit_recoveries"] or 0),
                "states": provider_states.get(provider, {}),
                "latency_ms": {
                    "average": round(float(row["avg_latency"] or 0.0), 1),
                    "minimum": round(float(row["min_latency"] or 0.0), 1),
                    "maximum": round(float(row["max_latency"] or 0.0), 1),
                },
            }
        return {
            "status": "ok" if flush_complete and dropped_events == 0 else "incomplete",
            "available": True,
            "complete": flush_complete and dropped_events == 0,
            "write_health": {
                "flush_complete": flush_complete,
                "dropped_events": dropped_events,
                "process_dropped_events": process_drops,
            },
            "window": window,
            "from": _iso_timestamp(since),
            "to": _iso_timestamp(until),
            "searches": {
                "total": total,
                "statuses": _count_map(status_rows),
                "backends": _count_map(backend_rows),
                "latency_ms": {
                    "average": round(float(search_row["avg_latency"] or 0.0), 1),
                    "minimum": round(float(search_row["min_latency"] or 0.0), 1),
                    "maximum": round(float(search_row["max_latency"] or 0.0), 1),
                },
            },
            "fallback": {
                "searches": fallback_count,
                "rate": round(fallback_count / total, 4) if total else 0.0,
                "reasons": _count_map(fallback_rows),
            },
            "fetches": {
                "total": fetch_total,
                "successes": fetch_successes,
                "errors": fetch_total - fetch_successes,
                "success_rate": round(fetch_successes / fetch_total, 4) if fetch_total else 0.0,
                "latency_ms": {
                    "average": round(float(fetch_operation_row["avg_latency"] or 0.0), 1),
                    "minimum": round(float(fetch_operation_row["min_latency"] or 0.0), 1),
                    "maximum": round(float(fetch_operation_row["max_latency"] or 0.0), 1),
                },
                "providers": fetch_providers,
                "attempts": fetch_attempts,
            },
            "cache": {
                "search": {
                    "hits": search_cache_hits,
                    "misses": search_cache_total - search_cache_hits,
                    "hit_rate": (
                        round(search_cache_hits / search_cache_total, 4)
                        if search_cache_total else 0.0
                    ),
                },
                "fetch": {
                    "hits": fetch_cache_hits,
                    "misses": fetch_cache_total - fetch_cache_hits,
                    "hit_rate": (
                        round(fetch_cache_hits / fetch_cache_total, 4)
                        if fetch_cache_total else 0.0
                    ),
                },
            },
            "providers": providers,
            "privacy": {"query_data_stored": False},
        }

    def activity(
        self, window: str = "24h", *, now: int | None = None, limit: int = 25
    ) -> dict[str, Any]:
        """Return a bucketed timeline plus recent failed fetch attempts.

        Unlike :meth:`stats`, the failure list includes destination hostnames
        (never paths or queries) so operators can see which sites need fallbacks.
        """
        if window not in VALID_WINDOWS:
            raise InvalidWindow(f"window must be one of: {', '.join(VALID_WINDOWS)}")
        if not self.available:
            raise TelemetryUnavailable(self._reason)
        self.flush()
        bucket_seconds = ACTIVITY_BUCKET_SECONDS[window]
        buckets = VALID_WINDOWS[window] // bucket_seconds
        until = int(time.time()) if now is None else int(now)
        # Buckets tile exactly the same [since, until] range as stats(); an event
        # at `until` itself folds into the final bucket.
        since = until - VALID_WINDOWS[window]
        bounds = (since, bucket_seconds, buckets - 1, since, until)
        with self._operation_lock, self._connect() as conn:
            conn.execute("BEGIN")
            search_rows = conn.execute(
                """
                SELECT MIN((created_at - ?) / ?, ?) AS bucket,
                       COUNT(*) AS total,
                       COALESCE(SUM(status IN ('error', 'timeout')), 0) AS failures
                FROM search_events WHERE created_at >= ? AND created_at <= ?
                GROUP BY bucket
                """,
                bounds,
            ).fetchall()
            fetch_rows = conn.execute(
                """
                SELECT MIN((created_at - ?) / ?, ?) AS bucket,
                       COUNT(*) AS total,
                       COALESCE(SUM(outcome != 'success'), 0) AS failures
                FROM fetch_operations WHERE created_at >= ? AND created_at <= ?
                GROUP BY bucket
                """,
                bounds,
            ).fetchall()
            # Rejected destinations (unsafe/policy) are not fallback diagnostics and
            # may name internal hosts, so they never leave the store.
            failure_rows = conn.execute(
                """
                SELECT created_at, url_host, provider, outcome, trigger,
                       http_status, provider_http_status, latency_ms
                FROM fetch_events
                WHERE created_at >= ? AND created_at <= ?
                  AND provider IN ('direct', 'decodo', 'jina')
                  AND outcome NOT IN ('success', 'proxy_success', 'unsafe_url', 'policy_error')
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (since, until, max(1, min(100, int(limit)))),
            ).fetchall()

        searches = {int(row["bucket"]): row for row in search_rows}
        fetches = {int(row["bucket"]): row for row in fetch_rows}
        timeline = []
        for index in range(buckets):
            search = searches.get(index)
            fetch = fetches.get(index)
            timeline.append({
                "start": _iso_timestamp(since + index * bucket_seconds),
                "searches": int(search["total"]) if search else 0,
                "search_failures": int(search["failures"]) if search else 0,
                "fetches": int(fetch["total"]) if fetch else 0,
                "fetch_failures": int(fetch["failures"]) if fetch else 0,
            })
        return {
            "status": "ok",
            "window": window,
            "bucket_seconds": bucket_seconds,
            "timeline": timeline,
            "recent_fetch_failures": [
                {
                    "at": _iso_timestamp(int(row["created_at"])),
                    "host": str(row["url_host"]),
                    "provider": str(row["provider"]),
                    "outcome": str(row["outcome"]),
                    "trigger": str(row["trigger"]),
                    "http_status": row["http_status"],
                    "provider_http_status": row["provider_http_status"],
                    "latency_ms": round(float(row["latency_ms"] or 0.0), 1),
                }
                for row in failure_rows
            ],
            "privacy": {"query_data_stored": False, "hostnames_included": True},
        }

    def reset(self) -> int:
        if not self.available:
            raise TelemetryUnavailable(self._reason)
        if not self.flush(timeout=5.0):
            raise TelemetryUnavailable("telemetry queue could not be drained")
        with self._operation_lock, self._connect() as conn:
            conn.execute("PRAGMA secure_delete = ON")
            # The database write lock orders reset epochs across every process.
            conn.execute("BEGIN IMMEDIATE")
            reset_candidate_ns = time.time_ns()
            count = int(conn.execute("SELECT COUNT(*) FROM search_events").fetchone()[0])
            conn.execute(
                """
                UPDATE telemetry_meta
                SET reset_before_ns = MAX(reset_before_ns, ?)
                WHERE singleton = 1
                """,
                (reset_candidate_ns,),
            )
            reset_before_ns = int(
                conn.execute(
                    "SELECT reset_before_ns FROM telemetry_meta WHERE singleton = 1"
                ).fetchone()[0]
            )
            conn.execute("DELETE FROM search_events")
            conn.execute("DELETE FROM fetch_events")
            conn.execute("DELETE FROM fetch_operations")
            conn.execute("DELETE FROM telemetry_health_events")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with self._counter_lock:
            self._pending_drop_times = collections.deque(
                timestamp
                for timestamp in self._pending_drop_times
                if timestamp > reset_before_ns
            )
            self._dropped_events = len(self._pending_drop_times)
        self._secure_files()
        return count

    def close(self) -> None:
        if not self.available or self._worker is None:
            return
        if self.flush():
            try:
                self._persist_pending_drops()
            except Exception as exc:
                logger.warning("Telemetry health marker flush failed (%s)", type(exc).__name__)
        try:
            self._queue.put(None, timeout=1.0)
        except queue.Full:
            return
        self._worker.join(timeout=2.0)
        self._worker = None


def _main() -> int:
    parser = argparse.ArgumentParser(description="Manage local-search query-free telemetry")
    parser.add_argument("command", choices=["reset"])
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    store = TelemetryStore(args.data_dir)
    try:
        if not store.available:
            print(json.dumps({"status": "error", "message": "telemetry unavailable"}))
            return 1
        deleted = store.reset()
        print(json.dumps({"status": "ok", "deleted_search_events": deleted}))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(_main())
