"""Persistence, aggregation, concurrency, privacy, and reset tests."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from telemetry import (
    DB_FILENAME,
    InvalidWindow,
    ProviderEvent,
    SearchEvent,
    TelemetryStore,
    TelemetryUnavailable,
    classify_error,
    normalize_engine_failures,
)


def _event(*, created_at: int, status: str = "ok", tavily: bool = False) -> SearchEvent:
    providers = [
        ProviderEvent(
            provider="searxng",
            state="degraded" if tavily else "ok",
            attempts=1,
            result_count=2,
            latency_ms=10.0,
            http_status=200,
            circuit_before="closed",
            circuit_after="closed",
        )
    ]
    if tavily:
        providers.append(
            ProviderEvent(
                provider="tavily",
                state="error",
                attempts=1,
                latency_ms=20.0,
                error_kind="http",
                http_status=429,
                credential_mode="keyless",
                circuit_before="closed",
                circuit_after="open",
                circuit_transition="opened",
                circuit_failures=3,
            )
        )
    return SearchEvent(
        created_at=created_at,
        status="degraded" if tavily else status,
        backend="searxng" if not tavily else "searxng",
        mode="fallback",
        requested_count=5,
        result_count=2,
        total_latency_ms=30.0 if tavily else 10.0,
        fallback_reason="searxng_degraded" if tavily else None,
        providers=tuple(providers),
        engine_failures=(("bing", "rate_limited"),) if tavily else (),
    )


def test_persists_across_reopen_and_uses_private_permissions(tmp_path):
    store = TelemetryStore(tmp_path / "data")
    assert store.record(_event(created_at=2_000_000_000))
    # close() drains the background queue, matching a graceful service restart.
    store.close()

    assert (tmp_path / "data" / DB_FILENAME).exists()
    assert os.stat(tmp_path / "data").st_mode & 0o777 == 0o700
    telemetry_files = list((tmp_path / "data").glob(f"{DB_FILENAME}*"))
    assert telemetry_files
    assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in telemetry_files)

    reopened = TelemetryStore(tmp_path / "data")
    try:
        stats = reopened.stats("24h", now=2_000_000_100)
        assert stats["searches"]["total"] == 1
        assert stats["providers"]["searxng"]["attempts"] == 1
    finally:
        reopened.close()


def test_aggregation_windows_and_tavily_operational_counts(tmp_path):
    now = 2_000_000_000
    store = TelemetryStore(tmp_path / "data")
    try:
        store.record(_event(created_at=now - 60, tavily=True))
        store.record(_event(created_at=now - 2 * 24 * 60 * 60))
        store.record(_event(created_at=now - 10 * 24 * 60 * 60))
        assert store.flush()

        daily = store.stats("24h", now=now)
        assert daily["searches"]["total"] == 1
        assert daily["fallback"]["rate"] == 1.0
        assert daily["providers"]["tavily"]["attempted_searches"] == 1
        assert daily["providers"]["tavily"]["rate_limited_429s"] == 1
        assert daily["providers"]["tavily"]["circuit_trips"] == 1
        assert daily["providers"]["tavily"]["credential_modes"] == {"keyless": 1}
        assert daily["searxng_engine_failures"][0]["reason"] == "rate_limited"

        assert store.stats("7d", now=now)["searches"]["total"] == 2
        assert store.stats("30d", now=now)["searches"]["total"] == 3
    finally:
        store.close()


def test_schema_and_rows_cannot_contain_sensitive_search_fields(tmp_path):
    store = TelemetryStore(tmp_path / "data")
    try:
        store.record(_event(created_at=2_000_000_000, tavily=True))
        assert store.flush()
        with sqlite3.connect(store.db_path) as conn:
            schema_rows = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            ).fetchall()
            schema = json.dumps(schema_rows).lower()
            row_values = [
                value
                for table in ("search_events", "provider_events", "engine_failures")
                for row in conn.execute(f"SELECT * FROM {table}")
                for value in row
            ]
        for forbidden in ("query", "url", "title", "snippet", "content", "api_key", "credential_value"):
            assert forbidden not in schema
        assert not any("secret" in str(value).lower() for value in row_values)
    finally:
        store.close()


def test_concurrent_recording_is_lossless_under_normal_load(tmp_path):
    store = TelemetryStore(tmp_path / "data")
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            accepted = list(
                executor.map(
                    lambda index: store.record(_event(created_at=2_000_000_000 + index)),
                    range(200),
                )
            )
        assert all(accepted)
        assert store.flush(timeout=5.0)
        assert store.stats("24h", now=2_000_000_500)["searches"]["total"] == 200
        assert store.status()["dropped_events"] == 0
    finally:
        store.close()


def test_invalid_window_disabled_and_unavailable_store(tmp_path):
    store = TelemetryStore(tmp_path / "data")
    try:
        with pytest.raises(InvalidWindow, match="24h, 7d, 30d"):
            store.stats("1h")
    finally:
        store.close()

    disabled = TelemetryStore(tmp_path / "disabled", enabled=False)
    assert disabled.record(_event(created_at=2_000_000_000)) is False
    with pytest.raises(TelemetryUnavailable):
        disabled.stats("24h")

    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("x")
    unavailable = TelemetryStore(blocking_file / "child")
    assert unavailable.available is False
    assert unavailable.record(_event(created_at=2_000_000_000)) is False
    with pytest.raises(TelemetryUnavailable):
        unavailable.stats("24h")


def test_explicit_reset_clears_events_but_keeps_database(tmp_path):
    store = TelemetryStore(tmp_path / "data")
    try:
        store.record(_event(created_at=2_000_000_000))
        store.record(_event(created_at=2_000_000_001, tavily=True))
        assert store.flush()
        assert store.reset() == 2
        assert store.db_path.exists()
        assert store.stats("30d", now=2_000_000_100)["searches"]["total"] == 0
    finally:
        store.close()


def test_stats_marks_dropped_events_incomplete_and_reset_requires_flush(monkeypatch, tmp_path):
    store = TelemetryStore(tmp_path / "data")
    try:
        store._note_drop()
        stats = store.stats("24h")
        assert stats["status"] == "incomplete"
        assert stats["complete"] is False
        assert stats["write_health"] == {
            "flush_complete": True,
            "dropped_events": 1,
            "process_dropped_events": 1,
        }

        monkeypatch.setattr(store, "flush", lambda timeout=2.0: False)
        with pytest.raises(TelemetryUnavailable, match="could not be drained"):
            store.reset()
    finally:
        store.close()


def test_reset_epoch_rejects_pre_reset_events_and_drop_markers_from_other_writer(tmp_path):
    data_dir = tmp_path / "data"
    writer = TelemetryStore(data_dir)
    resetter = TelemetryStore(data_dir)
    try:
        stale = _event(created_at=2_000_000_000)
        writer._note_drop()
        assert resetter.reset() == 0
        assert writer.record(stale)
        assert writer.record(_event(created_at=2_000_000_001))
        assert writer.flush()
        stats = writer.stats("24h", now=2_000_000_100)
        assert stats["searches"]["total"] == 1
        assert stats["complete"] is True
        assert stats["write_health"]["dropped_events"] == 0
    finally:
        writer.close()
        resetter.close()


def test_pending_drop_flush_is_serialized_without_double_count(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    store = TelemetryStore(data_dir)
    try:
        store._note_drop()
        first_mark_entered = threading.Event()
        release_first_mark = threading.Event()
        second_lock_attempted = threading.Event()
        original_mark = store._mark_drops_persisted

        class ObservedLock:
            def __init__(self):
                self._lock = threading.Lock()
                self._attempts = 0
                self._counter_lock = threading.Lock()

            def __enter__(self):
                with self._counter_lock:
                    self._attempts += 1
                    if self._attempts == 2:
                        second_lock_attempted.set()
                self._lock.acquire()
                return self

            def __exit__(self, *args):
                self._lock.release()

        def delayed_mark(count):
            first_mark_entered.set()
            assert release_first_mark.wait(timeout=2.0)
            original_mark(count)

        monkeypatch.setattr(store, "_operation_lock", ObservedLock())
        monkeypatch.setattr(store, "_mark_drops_persisted", delayed_mark)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(store._persist_pending_drops)
            assert first_mark_entered.wait(timeout=2.0)
            second = executor.submit(store._persist_pending_drops)
            assert second_lock_attempted.wait(timeout=2.0)
            assert second.done() is False
            release_first_mark.set()
            first.result(timeout=2.0)
            second.result(timeout=2.0)
        with sqlite3.connect(store.db_path) as conn:
            assert conn.execute(
                "SELECT COALESCE(SUM(dropped_events), 0) FROM telemetry_health_events"
            ).fetchone()[0] == 1
    finally:
        store.close()


def test_drop_marker_survives_store_restart(tmp_path):
    data_dir = tmp_path / "data"
    store = TelemetryStore(data_dir)
    store._note_drop()
    assert store.stats("24h")["complete"] is False
    store.close()

    reopened = TelemetryStore(data_dir)
    try:
        stats = reopened.stats("24h")
        assert stats["complete"] is False
        assert stats["write_health"]["dropped_events"] == 1
        assert stats["write_health"]["process_dropped_events"] == 0
    finally:
        reopened.close()


def test_error_classification_does_not_mark_successful_http_as_error():
    assert classify_error(None, 200) == "none"
    assert classify_error("invalid JSON payload", 200) == "invalid_response"
    assert classify_error("HTTP 429", 429) == "http"


def test_engine_failure_normalization_discards_raw_reason_text():
    normalized = normalize_engine_failures(
        [
            ["SECRET QUERY 4cb09b", "429 for SECRET_QUERY"],
            ["bing", "Suspended: access denied for SECRET_QUERY"],
        ]
    )
    assert normalized == (("unknown", "rate_limited"), ("bing", "suspended"))
    assert "SECRET_QUERY" not in repr(normalized)
    assert "4cb09b" not in repr(normalized)
