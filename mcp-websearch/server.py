#!/usr/bin/env python3
"""Policy-aware MCP web search broker for Brave Search.

Tools:
  - web_search(query, num_results=8, intent="general")
  - batch_web_search(queries, num_results=8, intent="general")
  - image_search(query, num_results=8)
  - web_fetch(url, max_chars=20000)           direct fetch with SSRF guard
  - verify_url(url)                           direct-fetch verification

HTTP diagnostics:
  - GET /live    dependency-free process liveness
  - GET /ready   provider readiness (503 when no backend is usable)
  - GET /health  compatibility diagnostics (always HTTP 200)
  - GET /stats   query-free aggregate telemetry (24h, 7d, or 30d)

Brave Search is the sole raw-search provider (independent index, strong
privacy posture, $0.005/query).

Searches use one bounded 18-second-or-less budget, overfetch before URL dedupe,
and return additive status/backend/fallback/timing metadata. Brave uses the
per-call X-Brave-Key header, then BRAVE_API_KEY, then a mode-0600 secret file.
Broker Authorization credentials are intentionally separate and never treated
as provider credentials.

Key configuration:
  WEBSEARCH_FETCH_MAX_BYTES           default 20 MiB
  WEBSEARCH_HTML_EXTRACT_TIMEOUT      default 8 seconds
  WEBSEARCH_PDF_MAX_PAGES             default 20
  WEBSEARCH_TOTAL_TIMEOUT             capped at 18 seconds
  WEBSEARCH_BRAVE_TIMEOUT             default 8 seconds
  WEBSEARCH_SEARCH_MAX_BYTES          default 2 MiB per provider response
  LOCAL_SEARCH_DATA_DIR               default ../data beside this package
  LOCAL_SEARCH_TELEMETRY_ENABLED      default true
  BRAVE_API_KEY                       optional stdio/server fallback key
  BRAVE_BASE_URL                      default https://api.search.brave.com
  DECODO_FALLBACK_ENABLED             default true; requires data/decodo_key
  DECODO_TIMEOUT                      default 30 seconds
  WEBSEARCH_FETCH_OPERATION_TIMEOUT   default 60 seconds
  MCP_PORT                            default 8889 (HTTP transport only)
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import importlib.util
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import socket
import ssl
import sys
import tempfile
import time
import urllib.parse
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Awaitable, Protocol

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.tools.tool import ToolResult
from pydantic import WithJsonSchema
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse

import html_extraction as htmlx
from cache import WebCache, canonicalize_url
from telemetry import (
    FetchEvent,
    FetchOperation,
    InvalidWindow,
    ProviderEvent,
    SearchEvent,
    TelemetryStore,
    TelemetryUnavailable,
    classify_error,
)

logger = logging.getLogger("websearch-mcp")

BRAVE_BASE_URL = os.environ.get("BRAVE_BASE_URL", "https://api.search.brave.com").rstrip("/")
# Brave Search API key. HTTP clients should forward it via X-Brave-Key; the
# env var is a stdio/server-side fallback, then a mode-0600 secret file.
BRAVE_API_KEY_ENV = os.environ.get("BRAVE_API_KEY", "").strip()
_DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOCAL_SEARCH_DATA_DIR = Path(
    os.environ.get("LOCAL_SEARCH_DATA_DIR", str(_DEFAULT_DATA_DIR))
).expanduser()
TELEMETRY_ENABLED = os.environ.get("LOCAL_SEARCH_TELEMETRY_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


def _bounded_float(name: str, default: float | str, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    return min(maximum, max(minimum, value))


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    return min(maximum, max(minimum, value))


# Search policy and bounded latency budget. The broker stays below Pi's 20s
# client deadline and has one provider to call.
SEARCH_TOTAL_TIMEOUT = _bounded_float("WEBSEARCH_TOTAL_TIMEOUT", 18.0, minimum=1.0, maximum=18.0)
BRAVE_TIMEOUT = _bounded_float(
    "WEBSEARCH_BRAVE_TIMEOUT", 8.0, minimum=0.25, maximum=SEARCH_TOTAL_TIMEOUT
)
# Default search routing mode. Per-call overrides come via the web_search
# `mode` argument. Sensitive mode refuses because no local corpus is wired;
# maximum_recall remains a compatible single-provider mode.
_SEARCH_MODE = os.environ.get("WEBSEARCH_SEARCH_MODE", "normal").strip().lower()
if _SEARCH_MODE not in {"normal", "sensitive", "maximum_recall"}:
    raise RuntimeError("WEBSEARCH_SEARCH_MODE must be normal, sensitive, or maximum_recall")
# Kept as a compatibility input for existing launchers; Brave is always used.
if os.environ.get("WEBSEARCH_PROVIDER_STACK", "brave").strip().lower() not in {"", "brave"}:
    logger.warning("WEBSEARCH_PROVIDER_STACK is ignored; Brave is the only provider")
_PROVIDER_STACK = "brave"
# Per-provider cost rates (USD per billable search request). Used for the
# estimated_cost_usd field in search responses.
_PROVIDER_COST_USD: dict[str, float] = {
    "brave": 0.005,
    "none": 0.0,
}
MAX_NUM_RESULTS = 20
RESULT_OVERFETCH_FACTOR = 2
MAX_QUERY_CHARS = 512
SEARCH_INTENT_VALUES = ("general", "current", "news")
SEARCH_INTENTS = frozenset(SEARCH_INTENT_VALUES)
SearchIntent = Annotated[
    str,
    WithJsonSchema({"type": "string", "enum": list(SEARCH_INTENT_VALUES)}),
]
_BRAVE_FRESHNESS = {"current": "pm", "news": "pd"}
SEARCH_RESPONSE_MAX_BYTES = _bounded_int(
    "WEBSEARCH_SEARCH_MAX_BYTES", 2 * 1024 * 1024, minimum=4096, maximum=10 * 1024 * 1024
)
BATCH_MAX_QUERIES = 3
BATCH_MAX_QUERY_CHARS = MAX_QUERY_CHARS
BATCH_MAX_CONCURRENCY = 2
BatchQueries = Annotated[
    list[str],
    WithJsonSchema(
        {
            "type": "array",
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": BATCH_MAX_QUERY_CHARS,
            },
            "minItems": 1,
            "maxItems": BATCH_MAX_QUERIES,
        }
    ),
]

# Other tunables.
# Direct fetch is budgeted below the operation deadline so that the Decodo and
# Jina fallback tiers retain a meaningful share after a direct timeout. Defaults:
# direct 20s -> Decodo 25s -> Jina up to the remaining ~15s under one 60s cap.
FETCH_TIMEOUT = _bounded_float("WEBSEARCH_FETCH_TIMEOUT", 20.0, minimum=1.0, maximum=60.0)
# Bounded DNS resolution so a hung lookup fails closed as a validation error
# (no fallback) instead of consuming the whole direct-fetch budget.
DNS_RESOLVE_TIMEOUT = _bounded_float("WEBSEARCH_DNS_TIMEOUT", 8.0, minimum=1.0, maximum=30.0)
DECODO_FALLBACK_ENABLED = os.environ.get("DECODO_FALLBACK_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
DECODO_TIMEOUT = _bounded_float("DECODO_TIMEOUT", 25.0, minimum=1.0, maximum=60.0)
DECODO_RESPONSE_MAX_BYTES = _bounded_int(
    "DECODO_RESPONSE_MAX_BYTES", 5 * 1024 * 1024, minimum=4096, maximum=20 * 1024 * 1024
)
DECODO_API_URL = "https://scraper-api.decodo.com/v2/scrape"
JINA_TIMEOUT = _bounded_float("JINA_TIMEOUT", 30.0, minimum=1.0, maximum=60.0)
JINA_RESPONSE_MAX_BYTES = _bounded_int(
    "JINA_RESPONSE_MAX_BYTES", 5 * 1024 * 1024, minimum=4096, maximum=20 * 1024 * 1024
)
FETCH_OPERATION_TIMEOUT = _bounded_float(
    "WEBSEARCH_FETCH_OPERATION_TIMEOUT", 60.0, minimum=1.0, maximum=120.0
)
JINA_FALLBACK_ENABLED = os.environ.get("JINA_FALLBACK_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
FETCH_MAX_REDIRECTS = _bounded_int("WEBSEARCH_FETCH_MAX_REDIRECTS", 6, minimum=0, maximum=12)
FETCH_MAX_BYTES = _bounded_int("WEBSEARCH_FETCH_MAX_BYTES", 20 * 1024 * 1024, minimum=1024, maximum=50 * 1024 * 1024)
PDF_MAX_PAGES = _bounded_int("WEBSEARCH_PDF_MAX_PAGES", 20, minimum=1, maximum=50)
PDF_OCR_MAX_DIMENSION = _bounded_int("WEBSEARCH_PDF_OCR_MAX_DIMENSION", 2500, minimum=1000, maximum=4000)
PDF_EXTRACT_TIMEOUT = _bounded_float("WEBSEARCH_PDF_EXTRACT_TIMEOUT", 45.0, minimum=5.0, maximum=120.0)
PDF_MIN_TEXT_CHARS = _bounded_int("WEBSEARCH_PDF_MIN_TEXT_CHARS", 200, minimum=20, maximum=2000)
PDF_TEXT_MAX_BYTES = _bounded_int("WEBSEARCH_PDF_TEXT_MAX_BYTES", 2 * 1024 * 1024, minimum=65536, maximum=10 * 1024 * 1024)
PDF_RENDER_MAX_BYTES = _bounded_int("WEBSEARCH_PDF_RENDER_MAX_BYTES", 64 * 1024 * 1024, minimum=1024 * 1024, maximum=256 * 1024 * 1024)
PDF_STDERR_MAX_BYTES = 256 * 1024
HTML_EXTRACT_TIMEOUT = _bounded_float(
    "WEBSEARCH_HTML_EXTRACT_TIMEOUT", 8.0, minimum=1.0, maximum=20.0
)
HTML_EXTRACT_STDERR_MAX_BYTES = 64 * 1024
HTML_EXTRACT_SCRIPT = Path(__file__).with_name("html_extraction.py")
HTML_EXTRACT_MAX_ADMITTED = 4
PDF_EXTRACT_MAX_ADMITTED = 4
PDFTOTEXT = shutil.which("pdftotext") or next(
    (path for path in ("/opt/homebrew/bin/pdftotext", "/usr/local/bin/pdftotext") if Path(path).is_file()),
    None,
)
PDFTOPPM = shutil.which("pdftoppm") or next(
    (path for path in ("/opt/homebrew/bin/pdftoppm", "/usr/local/bin/pdftoppm") if Path(path).is_file()),
    None,
)
SWIFT = "/usr/bin/swift" if Path("/usr/bin/swift").is_file() else None
MACOS_VISION_OCR = Path(__file__).with_name("macos_vision_ocr.swift")
# Circuit breaker: trip after this many consecutive failures, skip for cooldown.
BREAKER_FAIL_THRESHOLD = _bounded_int(
    "WEBSEARCH_BREAKER_FAIL_THRESHOLD", 3, minimum=1, maximum=20
)
BREAKER_COOLDOWN = _bounded_float(
    "WEBSEARCH_BREAKER_COOLDOWN", 60.0, minimum=1.0, maximum=3600.0
)

def _admission_queue(size: int) -> asyncio.Queue[None]:
    queue: asyncio.Queue[None] = asyncio.Queue(maxsize=size)
    for _ in range(size):
        queue.put_nowait(None)
    return queue


_http_client: httpx.AsyncClient | None = None
# HTTP/2 is used when available. Public fetches explicitly request an
# unencoded representation so the raw-byte cap is enforced before allocation
# by any transparent decompressor.
_HTTP2_AVAILABLE = importlib.util.find_spec("h2") is not None
_FETCH_ACCEPT_ENCODING = "identity"
_pdf_extract_semaphore = asyncio.Semaphore(2)
_html_extract_semaphore = asyncio.Semaphore(2)
_pdf_extract_admission = _admission_queue(PDF_EXTRACT_MAX_ADMITTED)
_html_extract_admission = _admission_queue(HTML_EXTRACT_MAX_ADMITTED)

# Preserve the existing testable parser helpers while production web_fetch runs
# the same extraction logic in a bounded child process.
_TextExtractor = htmlx.TextExtractor
_extract_with_text_parser = htmlx.extract_with_text_parser
_extract_html_content = htmlx.extract_html_content


async def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=FETCH_TIMEOUT,
            trust_env=False,
            http2=_HTTP2_AVAILABLE,
        )
    return _http_client


async def _public_fetch_client() -> httpx.AsyncClient:
    """Return a one-hop client so TLS pools cannot cross original hostnames."""
    return httpx.AsyncClient(
        timeout=FETCH_TIMEOUT,
        trust_env=False,
        http2=_HTTP2_AVAILABLE,
    )


mcp = FastMCP(
    "websearch",
    instructions=(
        "Web and image search plus page fetching via Brave Search. "
        "Use web_search for one focused query and batch_web_search for two or three independent query angles that can run together. "
        "Use image_search to find public image and source-page URLs without downloading image bytes. "
        "Use web_fetch to retrieve the main text and attachment links from a specific URL."
    ),
)


# --------------------------------------------------------------------------- #
# Circuit breaker (per backend).
# --------------------------------------------------------------------------- #

class _CircuitBreaker:
    """Thread-safe closed/open/half-open breaker with a single recovery probe."""

    def __init__(self) -> None:
        self._fails: dict[str, int] = {}
        self._tripped: dict[str, float] = {}  # backend -> tripped-at monotonic
        self._half_open: set[str] = set()
        self._lock = Lock()

    def allow(self, backend: str) -> bool:
        with self._lock:
            tripped_at = self._tripped.get(backend)
            if tripped_at is None:
                return True
            if time.monotonic() - tripped_at < BREAKER_COOLDOWN:
                return False
            # Cooldown elapsed: admit exactly one half-open recovery probe.
            if backend in self._half_open:
                return False
            self._half_open.add(backend)
            return True

    def record_success(self, backend: str) -> str:
        with self._lock:
            recovered = backend in self._tripped
            self._fails.pop(backend, None)
            self._tripped.pop(backend, None)
            self._half_open.discard(backend)
            return "recovered" if recovered else "none"

    def record_failure(self, backend: str) -> str:
        with self._lock:
            was_tripped = backend in self._tripped
            was_half_open = backend in self._half_open
            self._half_open.discard(backend)
            fails = self._fails.get(backend, 0) + 1
            self._fails[backend] = fails
            transition = "none"
            if was_half_open:
                self._tripped[backend] = time.monotonic()
                transition = "reopened"
            elif not was_tripped and fails >= BREAKER_FAIL_THRESHOLD:
                self._tripped[backend] = time.monotonic()
                transition = "opened"
            if transition != "none":
                logger.warning(
                    "Circuit breaker tripped for %s after %d failures (cooldown %ds)",
                    backend,
                    fails,
                    int(BREAKER_COOLDOWN),
                )
            return transition

    def record_aborted(self, backend: str) -> None:
        """Release a half-open probe when caller cancellation aborts the request."""
        with self._lock:
            self._half_open.discard(backend)

    def snapshot(self, backend: str) -> dict[str, Any]:
        """Return non-mutating, credential-free diagnostics."""
        with self._lock:
            now = time.monotonic()
            tripped_at = self._tripped.get(backend)
            if tripped_at is None:
                state = "closed"
                cooldown_remaining = 0.0
            else:
                cooldown_remaining = max(0.0, BREAKER_COOLDOWN - (now - tripped_at))
                state = "half_open" if backend in self._half_open or cooldown_remaining == 0 else "open"
            return {
                "state": state,
                "consecutive_failures": self._fails.get(backend, 0),
                "cooldown_remaining_s": round(cooldown_remaining, 1),
            }


_breaker = _CircuitBreaker()


class _BraveAuthState:
    """Track known-bad credentials without retaining or exposing their keys.

    Process-local salted digests let readiness recover immediately when a
    configured credential rotates. Only a 401/403 marks that credential
    unusable; a successful request clears only that credential's failure.
    """

    _MAX_FAILED_FINGERPRINTS = 32

    def __init__(self) -> None:
        self._salt = os.urandom(32)
        self._failed_fingerprints: OrderedDict[bytes, None] = OrderedDict()
        self._lock = Lock()

    def _fingerprint(self, api_key: str) -> bytes:
        return hashlib.sha256(self._salt + api_key.encode("utf-8")).digest()

    def usable(self, api_key: str) -> bool:
        if not api_key:
            return False
        fingerprint = self._fingerprint(api_key)
        with self._lock:
            if fingerprint not in self._failed_fingerprints:
                return True
            self._failed_fingerprints.move_to_end(fingerprint)
            return False

    def record_auth_failure(self, api_key: str) -> None:
        if not api_key:
            return
        fingerprint = self._fingerprint(api_key)
        with self._lock:
            self._failed_fingerprints[fingerprint] = None
            self._failed_fingerprints.move_to_end(fingerprint)
            if len(self._failed_fingerprints) > self._MAX_FAILED_FINGERPRINTS:
                self._failed_fingerprints.popitem(last=False)

    def record_success(self, api_key: str) -> None:
        if not api_key:
            return
        fingerprint = self._fingerprint(api_key)
        with self._lock:
            self._failed_fingerprints.pop(fingerprint, None)


_brave_auth_state = _BraveAuthState()


# --------------------------------------------------------------------------- #
# Provider abstraction. The raw ranked-search contract remains small so the
# broker and tests can use the same Brave implementation seam.
# --------------------------------------------------------------------------- #


class SearchProvider(Protocol):
    """Contract for a raw ranked-search backend."""

    name: str
    output: str
    timeout: float

    def search(
        self, query: str, num_results: int, intent: str = "general"
    ) -> Awaitable[_BackendOutcome]:
        ...


class _BraveProvider:
    """Brave Search raw-search provider (independent index; requires API key)."""

    name = "brave"
    output = "raw"

    @property
    def timeout(self) -> float:
        return BRAVE_TIMEOUT

    @staticmethod
    def search(
        query: str, num_results: int, intent: str = "general"
    ) -> Awaitable[_BackendOutcome]:
        if intent == "general":
            return _brave_search(query, num_results, _resolve_brave_key())
        return _brave_search(
            query, num_results, _resolve_brave_key(), intent=intent
        )

    @staticmethod
    def credential_label() -> str:
        return "keyed" if _resolve_brave_key() else "none"


# Brave is the only active provider. The compatibility environment input above
# is intentionally not allowed to alter this list.
def _build_provider_stack() -> list[SearchProvider]:
    return [_BraveProvider()]


_PROVIDERS: list[SearchProvider] = _build_provider_stack()


def _provider_credential_configured(name: str) -> bool:
    """Return whether a provider's credential is resolvable without leaking it."""
    return name == "brave" and bool(_resolve_brave_key())


def _provider_credential_usable(name: str) -> bool:
    """Return whether the current credential has no known authentication failure."""
    return name == "brave" and _brave_auth_state.usable(_resolve_brave_key())


def _default_timings_ms() -> dict[str, float | None]:
    """Baseline timings dict with every provider's key present and unset."""
    timings: dict[str, float | None] = {"total": 0.0}
    for provider in _PROVIDERS:
        timings[provider.name] = None
    return timings


@dataclass
class _FetchResult:
    text: str
    error: str | None
    cache_hit: bool
    cache_age_seconds: float
    url: str
    content_type: str
    provider: str = "direct"


@dataclass
class _BackendOutcome:
    backend: str
    results: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    ok: bool = False
    state: str = "error"
    elapsed_ms: float = 0.0
    attempts: int = 0
    unresponsive_engines: list[Any] = field(default_factory=list)
    error: str | None = None
    http_status: int | None = None
    credential_mode: str = "none"
    circuit_before: str = "unknown"
    circuit_after: str = "unknown"
    circuit_transition: str = "none"
    circuit_failures: int = 0
    # Generated-output providers carry a synthesized answer and citation URLs
    # alongside their ranked results. Empty for raw providers so the contract
    # stays single-type.
    answer: str = ""
    citations: list[str] = field(default_factory=list)


_last_search: dict[str, Any] | None = None
_telemetry: TelemetryStore | None = None
_telemetry_lock = Lock()
_cache: WebCache | None = None
_cache_lock = Lock()


# --------------------------------------------------------------------------- #
# Error / payload helpers.
# --------------------------------------------------------------------------- #

def _attempted_providers(
    outcomes: list[_BackendOutcome | None] | tuple[_BackendOutcome | None, ...],
) -> list[str]:
    """Return providers that issued at least one request, preserving order."""
    names: list[str] = []
    for outcome in outcomes:
        if outcome is not None and outcome.attempts > 0 and outcome.backend not in names:
            names.append(outcome.backend)
    return names


def _estimate_search_cost(*, backend: str, attempted: list[str] | None) -> float:
    """Estimate USD cost from providers that issued a billable request.

    ``backend`` identifies the source of returned results. ``attempted`` is
    intentionally narrower: providers skipped for missing credentials, an open
    circuit, or an exhausted total deadline are excluded because no request was
    issued. Contacted providers are counted even when their request failed.
    """
    if not attempted:
        return 0.0
    return sum(_PROVIDER_COST_USD.get(p, 0.0) for p in attempted)


def _search_metadata(
    *,
    status: str,
    backend: str,
    attempted: list[str] | None,
    fallback_reason: str | None,
    timings_ms: dict[str, float | None] | None,
    unresponsive_engines: list[Any] | None,
    provider_states: dict[str, str] | None,
    mode: str | None = None,
    search_mode: str | None = None,
    cache_hit: bool = False,
    cache_age_seconds: float = 0.0,
) -> dict[str, Any]:
    meta = {
        "status": status,
        "backend": backend,
        "attempted": attempted or [],
        "fallback_reason": fallback_reason,
        "timings_ms": timings_ms or _default_timings_ms(),
        "mode": mode or "normal",
        "unresponsive_engines": (unresponsive_engines or [])[:10],
        "provider_states": provider_states or {},
        "estimated_cost_usd": (
            0.0
            if cache_hit
            else round(_estimate_search_cost(backend=backend, attempted=attempted), 4)
        ),
        "cache_hit": bool(cache_hit),
        "cache_age_seconds": max(0, int(cache_age_seconds)),
    }
    if search_mode:
        meta["search_mode"] = search_mode
    return meta


def _record_last_search(payload: dict[str, Any]) -> None:
    """Record safe operational metadata; never retain query text, URLs, or keys."""
    global _last_search
    _last_search = {
        "completed_at": int(time.time()),
        "status": payload.get("status"),
        "backend": payload.get("backend"),
        "attempted": payload.get("attempted", []),
        "fallback_reason": payload.get("fallback_reason"),
        "timings_ms": payload.get("timings_ms", {}),
        "result_count": len(payload.get("results", [])),
        "unresponsive_engine_count": len(payload.get("unresponsive_engines", [])),
        "provider_states": payload.get("provider_states", {}),
        "mode": payload.get("mode"),
        "estimated_cost_usd": payload.get("estimated_cost_usd", 0.0),
        "cache_hit": bool(payload.get("cache_hit", False)),
        "cache_age_seconds": int(payload.get("cache_age_seconds", 0) or 0),
    }


def _get_telemetry() -> TelemetryStore:
    global _telemetry
    if _telemetry is None:
        with _telemetry_lock:
            if _telemetry is None:
                _telemetry = TelemetryStore(
                    LOCAL_SEARCH_DATA_DIR,
                    enabled=TELEMETRY_ENABLED,
                )
                atexit.register(_telemetry.close)
    return _telemetry


def _get_cache() -> WebCache | None:
    """Lazily initialize the private cache and fail open if it is unavailable."""
    global _cache
    desired_root = LOCAL_SEARCH_DATA_DIR / "cache"
    try:
        if _cache is None or _cache.root != desired_root:
            with _cache_lock:
                if _cache is None or _cache.root != desired_root:
                    if _cache is not None:
                        _cache.close()
                    _cache = WebCache(desired_root)
                    _cache.start_cleanup()
                    atexit.register(_cache.close)
        return _cache
    except Exception as exc:
        logger.warning("Web cache unavailable (%s)", type(exc).__name__)
        return None


def _provider_event(outcome: _BackendOutcome) -> ProviderEvent:
    snapshot = _breaker.snapshot(outcome.backend)
    return ProviderEvent(
        provider=outcome.backend,
        state=outcome.state,
        attempts=outcome.attempts,
        result_count=len(outcome.results),
        latency_ms=outcome.elapsed_ms,
        error_kind=classify_error(outcome.error, outcome.http_status),
        http_status=outcome.http_status,
        credential_mode=outcome.credential_mode,
        circuit_before=outcome.circuit_before,
        circuit_after=(
            outcome.circuit_after
            if outcome.circuit_after != "unknown"
            else str(snapshot["state"])
        ),
        circuit_transition=outcome.circuit_transition,
        circuit_failures=(
            outcome.circuit_failures
            if outcome.circuit_after != "unknown"
            else int(snapshot["consecutive_failures"])
        ),
    )


def _record_fetch_telemetry(
    *,
    url: str,
    outcome: str,
    started: float,
    http_status: int | None = None,
    body_bytes: int | None = None,
    tier_used: str = "direct",
    cache_hit: bool = False,
    provider: str = "direct",
    trigger: str = "none",
    provider_http_status: int | None = None,
) -> None:
    """Queue a host-only fetch attempt; never retain a full URL or path."""
    try:
        url_host = urllib.parse.urlsplit(url).hostname or "unknown"
        _get_telemetry().record_fetch(
            FetchEvent(
                url_host=url_host,
                http_status=http_status,
                outcome=outcome,
                tier_used=tier_used,
                bytes=body_bytes,
                latency_ms=(time.monotonic() - started) * 1000,
                cache_hit=cache_hit,
                provider=provider,
                trigger=trigger,
                provider_http_status=provider_http_status,
            )
        )
    except Exception as exc:
        # Monitoring must never break or delay fetch behavior.
        logger.warning("Fetch telemetry event dropped (%s)", type(exc).__name__)


def _record_fetch_operation(
    *,
    url: str,
    result: _FetchResult,
    started: float,
    trigger: str = "none",
    http_status: int | None = None,
    body_bytes: int | None = None,
) -> None:
    """Queue exactly one terminal, host-only outcome for a fetch operation."""
    try:
        url_host = urllib.parse.urlsplit(url).hostname or "unknown"
        _get_telemetry().record_fetch_operation(
            FetchOperation(
                url_host=url_host,
                outcome="success" if result.error is None else "error",
                provider=result.provider,
                trigger=trigger,
                http_status=http_status,
                bytes=body_bytes,
                latency_ms=(time.monotonic() - started) * 1000,
                cache_hit=result.cache_hit,
            )
        )
    except Exception as exc:
        logger.warning("Fetch operation telemetry dropped (%s)", type(exc).__name__)


def _record_search_telemetry(
    *,
    status: str,
    backend: str,
    requested_count: int,
    result_count: int,
    fallback_reason: str | None,
    total_latency_ms: float,
    outcomes: list[_BackendOutcome | None] | None = None,
    mode: str | None = None,
    cache_hit: bool = False,
) -> None:
    """Queue only explicitly allowlisted operational fields, never a search payload."""
    try:
        outcomes = [] if cache_hit else (outcomes or [])
        fallback_reason = None if cache_hit else fallback_reason
        providers = tuple(
            _provider_event(outcome)
            for outcome in outcomes
            if outcome is not None
        )
        _get_telemetry().record(
            SearchEvent(
                status=status,
                backend=backend,
                mode=mode,
                requested_count=requested_count,
                result_count=result_count,
                fallback_reason=fallback_reason,
                total_latency_ms=total_latency_ms,
                providers=providers,
                cache_hit=cache_hit,
            )
        )
    except Exception as exc:
        # Monitoring must never break or delay search behavior.
        logger.warning("Telemetry event dropped (%s)", type(exc).__name__)


_SEARCH_CACHE_PAYLOAD_KEYS = {
    "results",
    "suggestions",
    "status",
    "backend",
    "fallback_reason",
    "unresponsive_engines",
    "provider_states",
    "answer",
    "citations",
    "mode",
    "search_mode",
}


def _search_cache_variant(
    requested: int, mode: str, intent: str = "general"
) -> str:
    """Describe every policy input that can change a cached search result."""
    return json.dumps(
        {
            "num_results": requested,
            "search_mode": mode,
            "intent": intent,
            "provider": "brave",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _put_search_cache(
    query: str, variant: str, rendered: str, *, news: bool = False
) -> None:
    """Persist only the fields needed to reconstruct a successful response."""
    try:
        payload = json.loads(rendered)
        if not isinstance(payload, dict):
            return
        if not payload.get("results") and not payload.get("answer"):
            return
        allowlisted = {
            key: payload[key]
            for key in _SEARCH_CACHE_PAYLOAD_KEYS
            if key in payload
        }
        cache = _get_cache()
        if cache is not None:
            cache.put_search(query, allowlisted, variant=variant, news=news)
    except Exception:
        return


def _finish_search(
    rendered: str,
    *,
    status: str,
    backend: str,
    requested_count: int,
    result_count: int,
    fallback_reason: str | None = None,
    timings_ms: dict[str, float | None] | None = None,
    outcomes: list[_BackendOutcome | None] | None = None,
    mode: str | None = None,
    cache_hit: bool = False,
    cache_query: str | None = None,
    cache_variant: str | None = None,
    cache_news: bool = False,
) -> str:
    total_latency_ms = float((timings_ms or {}).get("total") or 0.0)
    _record_search_telemetry(
        status=status,
        backend=backend,
        requested_count=requested_count,
        result_count=result_count,
        fallback_reason=fallback_reason,
        total_latency_ms=total_latency_ms,
        outcomes=outcomes,
        mode=mode,
        cache_hit=cache_hit,
    )
    if (
        not cache_hit
        and cache_query is not None
        and cache_variant is not None
        and status in {"ok", "degraded"}
    ):
        _put_search_cache(
            cache_query, cache_variant, rendered, news=cache_news
        )
    return rendered


def _search_error_payload(
    query: str,
    message: str,
    suggestions: list[str] | None = None,
    *,
    backend: str = "none",
    attempted: list[str] | None = None,
    fallback_reason: str | None = None,
    timings_ms: dict[str, float | None] | None = None,
    unresponsive_engines: list[Any] | None = None,
    provider_states: dict[str, str] | None = None,
    mode: str | None = None,
    search_mode: str | None = None,
    safe_search: str | None = None,
) -> str:
    payload = {
        "query": query,
        "results": [],
        "suggestions": (suggestions or [])[:5],
        "error": message,
        "text": message,
        **_search_metadata(
            status="error",
            backend=backend,
            attempted=attempted,
            fallback_reason=fallback_reason,
            timings_ms=timings_ms,
            unresponsive_engines=unresponsive_engines,
            provider_states=provider_states,
            mode=mode,
            search_mode=search_mode,
        ),
    }
    if safe_search:
        payload["safe_search"] = safe_search
    _record_last_search(payload)
    return json.dumps(payload)


def _fetch_error(message: str) -> str:
    return f"Fetch error: {message}"


# --------------------------------------------------------------------------- #
# Result normalization + dedupe.
# --------------------------------------------------------------------------- #

def _public_http_url(value: Any) -> str | None:
    """Return a syntactically public-looking HTTP(S) URL without resolving or fetching it."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or any(char.isspace() for char in url):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
        canonical = urllib.parse.urlsplit(canonicalize_url(url))
        host = canonical.hostname or ""
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        # Accessing port validates malformed/out-of-range values.
        _ = parsed.port
        if host in {"localhost", "local"} or host.endswith((".localhost", ".local")):
            return None
        try:
            if _is_private_ip(ipaddress.ip_address(host)):
                return None
        except ValueError:
            # Browsers and URL stacks may reinterpret abbreviated, octal, or
            # hexadecimal IPv4 forms (for example 127.1 or 0x7f.0.0.1).
            if re.fullmatch(
                r"(?i)(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*",
                host,
            ):
                return None
            labels = host.split(".")
            if len(labels) < 2 or any(
                not label
                or len(label) > 63
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                for label in labels
            ):
                return None
    except (TypeError, ValueError):
        return None
    return url


def _normalize_brave_result(r: dict) -> dict[str, Any] | None:
    """Normalize one Brave Search API `web.results` entry.

    Brave returns a `web.results` array whose entries carry `url`, `title`,
    `description` (the snippet), and optional `age`/`page_age`/`publish_date`.
    Thumbnail and meta_url fields are ignored; only the public HTTP URL and
    text fields are retained.
    """
    url = _public_http_url(r.get("url"))
    if url is None:
        return None
    snippet = str(r.get("description") or "")
    published = r.get("page_age") or r.get("publish_date") or r.get("age")
    if isinstance(published, str) and published and not snippet.endswith(published):
        snippet = f"{snippet} (published {published})".strip()
    return {
        "title": r.get("title") or "Untitled",
        "url": url,
        "domain": _domain(url),
        "snippet": snippet,
        "engine": "brave",
        "provider": "brave",
        "score": None,
    }


def _normalize_brave_image_result(r: dict) -> dict[str, Any] | None:
    """Normalize one Brave Image Search API `results` entry.

    Brave returns a `results` array whose entries carry `title`, `url` (the
    page where the image was found), `source`, `thumbnail` (with `src`,
    `width`, `height`), and `properties` (with the actual image `url`).
    """
    properties = r.get("properties")
    image_url = _public_http_url(
        properties.get("url") if isinstance(properties, dict) else None
    )
    if image_url is None:
        return None
    page_url = _public_http_url(r.get("url"))
    thumbnail = r.get("thumbnail")
    thumbnail_url = _public_http_url(
        thumbnail.get("src") if isinstance(thumbnail, dict) else None
    )
    width = thumbnail.get("width") if isinstance(thumbnail, dict) else None
    height = thumbnail.get("height") if isinstance(thumbnail, dict) else None
    source = str(r.get("source") or "") or (_domain(page_url) if page_url else "")
    return {
        "title": str(r.get("title") or "Untitled"),
        "image_url": image_url,
        "thumbnail_url": thumbnail_url,
        "page_url": page_url,
        "source": source,
        "engine": "brave",
        "width": int(width) if isinstance(width, (int, float)) else None,
        "height": int(height) if isinstance(height, (int, float)) else None,
        "mime_type": None,
        "creator": None,
        "license": None,
        "license_url": None,
    }


def _domain(url: str) -> str:
    """Return the canonical hostname used by result identity comparisons."""
    if not url:
        return ""
    try:
        return urllib.parse.urlsplit(canonicalize_url(url)).hostname or ""
    except (TypeError, UnicodeError, ValueError):
        return ""


def _canonical_result_url(url: str) -> str:
    """Use the cache's canonical URL identity contract for result comparisons."""
    try:
        return canonicalize_url(url)
    except (TypeError, UnicodeError, ValueError):
        return ""


def _dedupe_and_rank(results: list[dict[str, Any]], num_results: int) -> list[dict[str, Any]]:
    """Dedupe before truncation while preserving provider order and URL path case."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for result in results:
        url = _public_http_url(result.get("url"))
        if url is None:
            continue
        key = _canonical_result_url(url)
        if not key or key in seen:
            continue
        seen.add(key)
        normalized = dict(result)
        normalized["url"] = url
        normalized["domain"] = _domain(url)
        unique.append(normalized)
        if len(unique) >= num_results:
            break
    for index, result in enumerate(unique, 1):
        result["rank"] = index
    return unique


def _dedupe_and_rank_images(
    results: list[dict[str, Any]], num_results: int
) -> list[dict[str, Any]]:
    """Dedupe canonical image URLs before truncation while preserving provider order."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for result in results:
        key = _canonical_result_url(str(result.get("image_url") or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(dict(result))
        if len(unique) >= num_results:
            break
    for index, result in enumerate(unique, 1):
        result["rank"] = index
    return unique


def _format_results(
    query: str,
    results: list[dict[str, Any]],
    suggestions: list[str],
    *,
    status: str | None = None,
    backend: str = "none",
    attempted: list[str] | None = None,
    fallback_reason: str | None = None,
    timings_ms: dict[str, float | None] | None = None,
    unresponsive_engines: list[Any] | None = None,
    provider_states: dict[str, str] | None = None,
    answer: str | None = None,
    citations: list[str] | None = None,
    mode: str | None = None,
    search_mode: str | None = None,
    cache_hit: bool = False,
    cache_age_seconds: float = 0.0,
) -> str:
    """Format results as the legacy contract plus additive provider metadata."""
    effective_status = status or ("ok" if results else "empty")
    if results:
        lines = [f"## Search: {query}\n"]
        for result in results:
            title = result.get("title", "Untitled")
            domain = result.get("domain", "")
            snippet = (result.get("snippet") or "").strip()
            lines.append(f"{result['rank']}. **{title}** — {domain}")
            if snippet:
                lines.append(f"   {snippet}")
            lines.append(f"   {result.get('url', '')}")
            lines.append("")
        if suggestions:
            lines.append(f"Related: {', '.join(suggestions[:5])}")
        text = "\n".join(lines)
    else:
        text = f"No results found for: {query}"
    # Generated-output providers surface a synthesized answer that is
    # never merged into the raw result list; it travels as its own field.
    if answer:
        text = f"{answer}\n\n---\n\n{text}" if results else answer

    payload = {
        "query": query,
        "results": results,
        "suggestions": suggestions[:5],
        "text": text,
        **_search_metadata(
            status=effective_status,
            backend=backend,
            attempted=attempted,
            fallback_reason=fallback_reason,
            timings_ms=timings_ms,
            unresponsive_engines=unresponsive_engines,
            provider_states=provider_states,
            mode=mode,
            search_mode=search_mode,
            cache_hit=cache_hit,
            cache_age_seconds=cache_age_seconds,
        ),
    }
    if answer:
        payload["answer"] = answer
    if citations:
        payload["citations"] = list(citations)
    _record_last_search(payload)
    return json.dumps(payload)


def _format_image_results(
    query: str,
    results: list[dict[str, Any]],
    suggestions: list[str],
    *,
    status: str,
    backend: str,
    attempted: list[str],
    safe_search: str,
    fallback_reason: str | None,
    timings_ms: dict[str, float | None],
    unresponsive_engines: list[Any],
    provider_states: dict[str, str],
) -> str:
    if results:
        lines = [f"## Image search: {query}\n"]
        for result in results:
            source = result.get("source") or result.get("engine") or ""
            lines.append(f"{result['rank']}. **{result['title']}** — {source}")
            if result.get("page_url"):
                lines.append(f"   Page: {result['page_url']}")
            lines.append(f"   Image: {result['image_url']}")
            lines.append("")
        if suggestions:
            lines.append(f"Related: {', '.join(suggestions[:5])}")
        text = "\n".join(lines)
    else:
        text = f"No image results found for: {query}"

    payload = {
        "query": query,
        "results": results,
        "suggestions": suggestions[:5],
        "text": text,
        "safe_search": safe_search,
        **_search_metadata(
            status=status,
            backend=backend,
            attempted=attempted,
            fallback_reason=fallback_reason,
            timings_ms=timings_ms,
            unresponsive_engines=unresponsive_engines,
            provider_states=provider_states,
            mode="disabled",
        ),
    }
    _record_last_search(payload)
    return json.dumps(payload)


def _meaningful_text(text: str) -> bool:
    return len(re.sub(r"\s+", "", text)) >= PDF_MIN_TEXT_CHARS


class _OutputLimitExceeded(RuntimeError):
    pass


async def _read_stream_limited(stream: asyncio.StreamReader, limit: int, label: str) -> bytes:
    chunks = bytearray()
    while True:
        chunk = await stream.read(min(65536, limit + 1 - len(chunks)))
        if not chunk:
            return bytes(chunks)
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise _OutputLimitExceeded(f"{label} exceeds {limit} byte limit")


async def _write_process_stdin(writer: asyncio.StreamWriter, data: bytes) -> None:
    try:
        writer.write(data)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


@asynccontextmanager
async def _bounded_extraction_slot(
    admission: asyncio.Queue[None],
    semaphore: asyncio.Semaphore,
    timeout: float,
    label: str,
):
    """Bound active plus queued extraction work under one absolute deadline."""
    try:
        admission.get_nowait()
    except asyncio.QueueEmpty as exc:
        raise RuntimeError(f"{label} extraction capacity exhausted") from exc
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=max(0.001, deadline - time.monotonic()))
        acquired = True
        yield deadline
    finally:
        if acquired:
            semaphore.release()
        admission.put_nowait(None)


async def _extract_html_content_isolated(html: str, base_url: str, max_chars: int) -> str:
    """Run untrusted HTML parsing in a resource-limited, cancellable child process."""
    encoded = html.encode("utf-8")
    output_limit = max_chars * 4 + 4096
    async with _bounded_extraction_slot(
        _html_extract_admission,
        _html_extract_semaphore,
        HTML_EXTRACT_TIMEOUT,
        "HTML",
    ) as deadline:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(HTML_EXTRACT_SCRIPT),
            "--base-url",
            base_url,
            "--max-chars",
            str(max_chars),
            "--max-input-bytes",
            str(max(len(encoded), FETCH_MAX_BYTES)),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(
            _read_stream_limited(process.stdout, output_limit, "HTML extraction output")
        )
        stderr_task = asyncio.create_task(
            _read_stream_limited(
                process.stderr,
                HTML_EXTRACT_STDERR_MAX_BYTES,
                "HTML extraction error output",
            )
        )
        stdin_task = asyncio.create_task(_write_process_stdin(process.stdin, encoded))
        wait_task = asyncio.create_task(process.wait())
        tasks = (stdout_task, stderr_task, stdin_task, wait_task)
        try:
            stdout, stderr, _, returncode = await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=max(0.001, deadline - time.monotonic()),
            )
        except (asyncio.CancelledError, asyncio.TimeoutError, _OutputLimitExceeded):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        if returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()[:200]
            raise RuntimeError(detail or f"extractor exited with status {returncode}")
        return stdout.decode("utf-8", "replace")


async def _run_pdf_command(
    args: list[str],
    deadline: float,
    *,
    stdout_limit: int = PDF_TEXT_MAX_BYTES,
) -> tuple[int, bytes, bytes]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("PDF extraction deadline exceeded")
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout_task = asyncio.create_task(_read_stream_limited(process.stdout, stdout_limit, "PDF command output"))
    stderr_task = asyncio.create_task(_read_stream_limited(process.stderr, PDF_STDERR_MAX_BYTES, "PDF command error output"))
    wait_task = asyncio.create_task(process.wait())
    try:
        stdout, stderr, _ = await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task, wait_task),
            timeout=remaining,
        )
    except (asyncio.CancelledError, asyncio.TimeoutError, _OutputLimitExceeded):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        for task in (stdout_task, stderr_task, wait_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, wait_task, return_exceptions=True)
        raise
    return process.returncode or 0, stdout, stderr


async def _extract_pdf_text(
    pdf_bytes: bytes,
    deadline: float | None = None,
) -> tuple[str, str] | tuple[None, str]:
    """Extract bounded PDF text under one deadline, then try macOS Vision OCR."""
    deadline = deadline or (time.monotonic() + PDF_EXTRACT_TIMEOUT)
    with tempfile.TemporaryDirectory(prefix="local-search-pdf-") as temp_dir:
        root = Path(temp_dir)
        pdf_path = root / "document.pdf"
        pdf_path.write_bytes(pdf_bytes)
        fallback_text = ""

        if PDFTOTEXT:
            code, stdout, stderr = await _run_pdf_command(
                [PDFTOTEXT, "-layout", "-f", "1", "-l", str(PDF_MAX_PAGES), str(pdf_path), "-"],
                deadline,
            )
            if code == 0:
                fallback_text = stdout.decode("utf-8", "replace").strip()
                if _meaningful_text(fallback_text):
                    return fallback_text, "pdftotext"
            elif code != 0:
                logger.info("pdftotext could not extract PDF: %s", stderr.decode("utf-8", "replace")[:120])

        can_ocr = (
            sys.platform == "darwin"
            and PDFTOPPM is not None
            and SWIFT is not None
            and MACOS_VISION_OCR.is_file()
        )
        if not can_ocr:
            if fallback_text:
                return fallback_text, "pdftotext_partial"
            return None, "PDF contains no extractable text and OCR is unavailable"

        images: list[Path] = []
        rendered_bytes = 0
        for page_number in range(1, PDF_MAX_PAGES + 1):
            page_prefix = root / f"page-{page_number:03d}"
            code, _, stderr = await _run_pdf_command(
                [
                    PDFTOPPM,
                    "-f", str(page_number),
                    "-l", str(page_number),
                    "-singlefile",
                    "-jpeg",
                    "-scale-to", str(PDF_OCR_MAX_DIMENSION),
                    str(pdf_path),
                    str(page_prefix),
                ],
                deadline,
            )
            image = page_prefix.with_suffix(".jpg")
            if code != 0 or not image.exists():
                if images:
                    break
                if fallback_text:
                    return fallback_text, "pdftotext_partial"
                return None, f"PDF page rendering failed: {stderr.decode('utf-8', 'replace')[:200]}"
            rendered_bytes += image.stat().st_size
            if rendered_bytes > PDF_RENDER_MAX_BYTES:
                image.unlink(missing_ok=True)
                if fallback_text:
                    return fallback_text, "pdftotext_partial"
                return None, f"rendered PDF pages exceed {PDF_RENDER_MAX_BYTES} byte limit"
            images.append(image)
        if not images:
            if fallback_text:
                return fallback_text, "pdftotext_partial"
            return None, "PDF page rendering produced no images"
        code, stdout, stderr = await _run_pdf_command(
            [SWIFT, str(MACOS_VISION_OCR), *(str(image) for image in images)],
            deadline,
        )
        if code != 0:
            if fallback_text:
                return fallback_text, "pdftotext_partial"
            return None, f"PDF OCR failed: {stderr.decode('utf-8', 'replace')[:200]}"
        text = stdout.decode("utf-8", "replace").strip()
        if not _meaningful_text(text):
            if fallback_text:
                return fallback_text, "pdftotext_partial"
            return None, "PDF OCR produced no meaningful text"
        return text, "macos_vision_ocr"


def _decode_text_body(body: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset=([^;\s]+)", content_type, flags=re.IGNORECASE)
    charset = charset_match.group(1).strip('"\'') if charset_match else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _looks_like_text(body: bytes, content_type: str) -> bool:
    sample = body[:4096]
    if b"\x00" in sample:
        return False
    decoded = _decode_text_body(sample, content_type)
    if not decoded:
        return True
    bad = sum(character == "\ufffd" or (ord(character) < 32 and character not in "\n\r\t") for character in decoded)
    return bad / len(decoded) < 0.02


def _truncate_text(text: str, max_chars: int, suffix: str) -> str:
    if len(text) <= max_chars:
        return text
    if len(suffix) >= max_chars:
        return suffix[:max_chars]
    return text[:max_chars - len(suffix)] + suffix


def _pdf_fetch_text(url: str, text: str, method: str, max_chars: int) -> str:
    name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name or "PDF document"
    header = (
        f"PDF: {name}\n"
        f"Source: {url}\n"
        f"Extraction: {method}; limited to the first {PDF_MAX_PAGES} pages.\n\n"
    )
    return _truncate_text(header + text, max_chars, "\n\n... (truncated)")


# --------------------------------------------------------------------------- #
# SSRF guard for web_fetch.
# --------------------------------------------------------------------------- #

_PRIVATE_IP_ATTRS = ("is_private", "is_loopback", "is_link_local",
                     "is_multicast", "is_reserved", "is_unspecified")


class _UnsafeTargetError(ValueError):
    """Raised when a URL cannot be proven public before any remote fetch.

    This is the hard fail-closed boundary: the destination never passed DNS/IP
    validation, so it must never be handed to Decodo or Jina to resolve
    independently. It is a ``ValueError`` subclass for backward compatibility.
    """


# HTTP statuses whose textual/missing-MIME error bodies are eligible for the
# Decodo -> Jina fallback. Deterministic client errors (400/401/404/405/410/422)
# and binary error bodies are intentionally excluded. 500/52x are lower
# confidence but recoverable via the premium proxy + rendered browser tier.
_FALLBACK_HTTP_STATUSES: frozenset[int] = frozenset(
    {403, 408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 527}
)


async def _resolve_host_ips(host: str, port: int, scheme: str) -> list[ipaddress._BaseAddress]:
    try:
        ip = ipaddress.ip_address(host)
        return [ip]
    except ValueError:
        pass
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(
                socket.getaddrinfo, host, port or (443 if scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            ),
            timeout=DNS_RESOLVE_TIMEOUT,
        )
    except asyncio.TimeoutError as exc:
        raise ValueError(f"DNS resolution exceeded {DNS_RESOLVE_TIMEOUT:g} second deadline") from exc
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve URL host: {exc}") from exc
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def _is_private_ip(ip: ipaddress._BaseAddress) -> bool:
    return any(getattr(ip, attr, False) for attr in _PRIVATE_IP_ATTRS)


def _cache_lookup_allowed(url: str) -> bool:
    """Reject URL forms that must never bypass web_fetch's SSRF contract."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        host = parsed.hostname.strip().lower()
        if host in {"localhost", "local"} or host.endswith(".localhost"):
            return False
        parsed.port  # Validate malformed ports before canonical cache lookup.
        try:
            return not _is_private_ip(ipaddress.ip_address(host))
        except ValueError:
            return True
    except (TypeError, ValueError):
        return False


async def _validate_public_http_url(url: str) -> list[ipaddress._BaseAddress]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise _UnsafeTargetError("Only public http(s) URLs can be fetched")
    if parsed.username is not None or parsed.password is not None:
        raise _UnsafeTargetError("Credential-bearing URLs cannot be fetched")
    host = parsed.hostname.strip().lower()
    if host in {"localhost", "local"} or host.endswith(".localhost"):
        raise _UnsafeTargetError("Refusing to fetch local/private URL")
    try:
        addresses = await _resolve_host_ips(host, parsed.port, parsed.scheme)
    except ValueError as exc:
        # DNS resolution failure or timeout: the target was never proven
        # public, so it must fail closed rather than be proxied independently.
        raise _UnsafeTargetError(str(exc)) from exc
    if not addresses:
        raise _UnsafeTargetError("Could not resolve URL host")
    for ip in addresses:
        if _is_private_ip(ip):
            raise _UnsafeTargetError("Refusing to fetch local/private URL")
    # Prefer IPv4 when both families are returned because many local machines
    # lack working public IPv6. The selected address is pinned for the request.
    return sorted(set(addresses), key=lambda ip: (ip.version != 4, str(ip)))


def _pinned_public_request(
    url: str,
    address: ipaddress._BaseAddress,
) -> tuple[str, str, dict[str, Any]]:
    """Build an IP-pinned request while retaining the original Host and TLS SNI."""
    parsed = urllib.parse.urlsplit(url)
    assert parsed.hostname is not None
    original_host = parsed.hostname.encode("idna").decode("ascii")
    host_header = f"[{original_host}]" if ":" in original_host else original_host
    if parsed.port is not None:
        host_header = f"{host_header}:{parsed.port}"
    address_text = str(address)
    pinned_host = f"[{address_text}]" if address.version == 6 else address_text
    if parsed.port is not None:
        pinned_host = f"{pinned_host}:{parsed.port}"
    pinned_url = urllib.parse.urlunsplit(
        (parsed.scheme, pinned_host, parsed.path or "/", parsed.query, "")
    )
    extensions: dict[str, Any] = {}
    if parsed.scheme == "https":
        extensions["sni_hostname"] = original_host
    return pinned_url, host_header, extensions


# --------------------------------------------------------------------------- #
# Brave key resolution (per-call header > env var > mode-0600 secret file).
# --------------------------------------------------------------------------- #

def _read_secret_file(path: Path) -> str:
    """Return the first non-empty stripped line from a mode-0600 secret file.

    The file must live outside the repository and be readable only by the
    owner; otherwise it is ignored. Secrets are never logged or returned to
    clients.
    """
    try:
        if not path.is_file():
            return ""
        stat = path.stat()
        # Require owner-only read/write (0o600 or stricter on the owner bits).
        if stat.st_mode & 0o077:
            logger.warning("Ignoring world/group-readable secret file %s", path)
            return ""
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    return line
    except OSError:
        pass
    return ""


_BRAVE_SECRET_FILE = LOCAL_SEARCH_DATA_DIR / "brave_key"


def _resolve_brave_key() -> str:
    """Return the Brave Search API key from the current HTTP request, env var,
    or a mode-0600 secret file. Empty if none.

    Header precedence (standard `Authorization` is reserved for MCP transport):
      1. X-Brave-Key   (explicit Brave credential)
    Then env var BRAVE_API_KEY, then the secret file at
    $LOCAL_SEARCH_DATA_DIR/brave_key. Generic X-Api-Key and Authorization
    values are never repurposed as Brave keys.
    """
    try:
        headers = get_http_headers()
    except LookupError:
        return BRAVE_API_KEY_ENV or _read_secret_file(_BRAVE_SECRET_FILE)
    value = headers.get("x-brave-key")
    if value and value.strip():
        return value.strip()
    return BRAVE_API_KEY_ENV or _read_secret_file(_BRAVE_SECRET_FILE)


def _resolve_decodo_key() -> str:
    """Return the owner-only Decodo token, or empty when not configured.

    Decodo credentials are intentionally file-only so they never enter launchd
    plists, process arguments, MCP request headers, or operator output.
    """
    token = _read_secret_file(LOCAL_SEARCH_DATA_DIR / "decodo_key")
    if not token or any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in token):
        return ""
    return token


# --------------------------------------------------------------------------- #
# Backend: Brave Search. Independent index; raw ranked results.
# --------------------------------------------------------------------------- #


async def _limited_json_object(response: httpx.Response, *, provider: str) -> dict[str, Any]:
    """Decode one bounded, uncompressed provider response."""
    content_encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if content_encoding not in {"", "identity"}:
        raise ValueError(f"{provider} returned unsupported content encoding")
    content_length = response.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > SEARCH_RESPONSE_MAX_BYTES:
        raise ValueError(f"{provider} response exceeds {SEARCH_RESPONSE_MAX_BYTES} byte limit")
    body = bytearray()
    async for chunk in response.aiter_raw():
        if len(body) + len(chunk) > SEARCH_RESPONSE_MAX_BYTES:
            raise ValueError(f"{provider} response exceeds {SEARCH_RESPONSE_MAX_BYTES} byte limit")
        body.extend(chunk)
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError(f"{provider} returned an invalid JSON payload")
    return data


def _brave_search_url() -> str:
    """Return the configured Brave search endpoint only when credentials can
    be sent safely.
    """
    parsed = urllib.parse.urlsplit(BRAVE_BASE_URL)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("BRAVE_BASE_URL must be a credential-free HTTPS origin") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("BRAVE_BASE_URL must be a credential-free HTTPS origin")
    return urllib.parse.urlunsplit(("https", parsed.netloc, "/res/v1/web/search", "", ""))


async def _brave_search(
    query: str, num_results: int, api_key: str, intent: str = "general"
) -> _BackendOutcome:
    """Search Brave. Requires an API key (no keyless mode).

    Brave's web search endpoint returns a `web.results` array of ranked
    results. We request `text_decorations=false` so snippets are plain text,
    and `safesearch=moderate` to match the broker's default content policy.
    """
    started = time.monotonic()
    credential_mode = "keyed" if api_key else "none"
    circuit_before = _breaker.snapshot("brave")
    if not api_key:
        logger.info("Brave search skipped: no API key configured")
        circuit_after = _breaker.snapshot("brave")
        return _BackendOutcome(
            backend="brave",
            state="error",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            error="missing API key",
            credential_mode="none",
            circuit_before=str(circuit_before["state"]),
            circuit_after=str(circuit_after["state"]),
            circuit_failures=int(circuit_after["consecutive_failures"]),
        )
    if not _breaker.allow("brave"):
        logger.info("Brave circuit open; skipping")
        circuit_after = _breaker.snapshot("brave")
        return _BackendOutcome(
            backend="brave",
            state="circuit_open",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            credential_mode=credential_mode,
            circuit_before=str(circuit_before["state"]),
            circuit_after=str(circuit_after["state"]),
            circuit_failures=int(circuit_after["consecutive_failures"]),
        )

    # Overfetch to allow URL dedupe before truncation to num_results.
    fetch_count = min(num_results * RESULT_OVERFETCH_FACTOR, MAX_NUM_RESULTS)
    params = {
        "q": query,
        "count": str(fetch_count),
        "safesearch": "moderate",
        "text_decorations": "false",
    }
    if freshness := _BRAVE_FRESHNESS.get(intent):
        params["freshness"] = freshness
    headers = {"X-Subscription-Token": api_key, "Accept": "application/json",
               "Accept-Encoding": "identity"}
    try:
        search_url = _brave_search_url()
        client = await _client()
        async with client.stream(
            "GET",
            search_url,
            params=params,
            headers=headers,
            timeout=BRAVE_TIMEOUT,
        ) as response:
            response.raise_for_status()
            data = await _limited_json_object(response, provider="Brave")
    except asyncio.CancelledError:
        _breaker.record_aborted("brave")
        raise
    except httpx.TimeoutException:
        circuit_transition = _breaker.record_failure("brave")
        state = "timeout"
        error = "request timed out"
        http_status = None
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        http_status = status_code
        # Auth/client errors prove reachability, so they do not trip the circuit.
        # Track only known authentication failures separately for readiness.
        if status_code in {400, 401, 403, 404, 422}:
            circuit_transition = _breaker.record_success("brave")
        else:
            circuit_transition = _breaker.record_failure("brave")
        if status_code in {401, 403}:
            _brave_auth_state.record_auth_failure(api_key)
        state = "error"
        error = f"HTTP {status_code}"
    except Exception as exc:
        circuit_transition = _breaker.record_failure("brave")
        state = "error"
        error = type(exc).__name__
        http_status = None
    else:
        circuit_transition = _breaker.record_success("brave")
        _brave_auth_state.record_success(api_key)
        circuit_after = _breaker.snapshot("brave")
        web_block = data.get("web") if isinstance(data, dict) else None
        raw_results = (
            web_block.get("results", []) if isinstance(web_block, dict) else []
        )
        results = [
            normalized
            for item in raw_results[:num_results]
            if isinstance(item, dict)
            and (normalized := _normalize_brave_result(item)) is not None
        ]
        return _BackendOutcome(
            backend="brave",
            results=results,
            ok=True,
            state="ok" if results else "empty",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            attempts=1,
            http_status=response.status_code,
            credential_mode=credential_mode,
            circuit_before=str(circuit_before["state"]),
            circuit_after=str(circuit_after["state"]),
            circuit_transition=circuit_transition,
            circuit_failures=int(circuit_after["consecutive_failures"]),
        )

    circuit_after = _breaker.snapshot("brave")
    logger.warning("Brave search failed (%s): %s", credential_mode, error)
    return _BackendOutcome(
        backend="brave",
        state=state,
        elapsed_ms=round((time.monotonic() - started) * 1000, 1),
        attempts=1,
        error=error,
        http_status=http_status,
        credential_mode=credential_mode,
        circuit_before=str(circuit_before["state"]),
        circuit_after=str(circuit_after["state"]),
        circuit_transition=circuit_transition,
        circuit_failures=int(circuit_after["consecutive_failures"]),
    )


def _brave_image_search_url() -> str:
    """Return the Brave image search endpoint (same origin guard as web search)."""
    parsed = urllib.parse.urlsplit(BRAVE_BASE_URL)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("BRAVE_BASE_URL must be a credential-free HTTPS origin") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("BRAVE_BASE_URL must be a credential-free HTTPS origin")
    return urllib.parse.urlunsplit(("https", parsed.netloc, "/res/v1/images/search", "", ""))


async def _brave_image_search(query: str, num_results: int, api_key: str) -> _BackendOutcome:
    """Search images via Brave. Requires an API key (no keyless mode)."""
    started = time.monotonic()
    credential_mode = "keyed" if api_key else "none"
    circuit_before = _breaker.snapshot("brave")
    if not api_key:
        logger.info("Brave image search skipped: no API key configured")
        circuit_after = _breaker.snapshot("brave")
        return _BackendOutcome(
            backend="brave",
            state="error",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            error="missing API key",
            credential_mode="none",
            circuit_before=str(circuit_before["state"]),
            circuit_after=str(circuit_after["state"]),
            circuit_failures=int(circuit_after["consecutive_failures"]),
        )
    if not _breaker.allow("brave"):
        logger.info("Brave circuit open; skipping image search")
        circuit_after = _breaker.snapshot("brave")
        return _BackendOutcome(
            backend="brave",
            state="circuit_open",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            credential_mode=credential_mode,
            circuit_before=str(circuit_before["state"]),
            circuit_after=str(circuit_after["state"]),
            circuit_failures=int(circuit_after["consecutive_failures"]),
        )

    fetch_count = min(num_results * RESULT_OVERFETCH_FACTOR, MAX_NUM_RESULTS)
    params = {
        "q": query,
        "count": str(fetch_count),
        "safesearch": "strict",
    }
    headers = {"X-Subscription-Token": api_key, "Accept": "application/json",
               "Accept-Encoding": "identity"}
    try:
        search_url = _brave_image_search_url()
        client = await _client()
        async with client.stream(
            "GET",
            search_url,
            params=params,
            headers=headers,
            timeout=BRAVE_TIMEOUT,
        ) as response:
            response.raise_for_status()
            data = await _limited_json_object(response, provider="Brave")
    except asyncio.CancelledError:
        _breaker.record_aborted("brave")
        raise
    except httpx.TimeoutException:
        circuit_transition = _breaker.record_failure("brave")
        state = "timeout"
        error = "request timed out"
        http_status = None
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        http_status = status_code
        if status_code in {400, 401, 403, 404, 422}:
            circuit_transition = _breaker.record_success("brave")
        else:
            circuit_transition = _breaker.record_failure("brave")
        if status_code in {401, 403}:
            _brave_auth_state.record_auth_failure(api_key)
        state = "error"
        error = f"HTTP {status_code}"
    except Exception as exc:
        circuit_transition = _breaker.record_failure("brave")
        state = "error"
        error = type(exc).__name__
        http_status = None
    else:
        circuit_transition = _breaker.record_success("brave")
        _brave_auth_state.record_success(api_key)
        circuit_after = _breaker.snapshot("brave")
        raw_results = data.get("results", []) if isinstance(data, dict) else []
        results = [
            normalized
            for item in raw_results[:num_results]
            if isinstance(item, dict)
            and (normalized := _normalize_brave_image_result(item)) is not None
        ]
        return _BackendOutcome(
            backend="brave",
            results=results,
            ok=True,
            state="ok" if results else "empty",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            attempts=1,
            http_status=response.status_code,
            credential_mode=credential_mode,
            circuit_before=str(circuit_before["state"]),
            circuit_after=str(circuit_after["state"]),
            circuit_transition=circuit_transition,
            circuit_failures=int(circuit_after["consecutive_failures"]),
        )

    circuit_after = _breaker.snapshot("brave")
    logger.warning("Brave image search failed (%s): %s", credential_mode, error)
    return _BackendOutcome(
        backend="brave",
        state=state,
        elapsed_ms=round((time.monotonic() - started) * 1000, 1),
        attempts=1,
        error=error,
        http_status=http_status,
        credential_mode=credential_mode,
        circuit_before=str(circuit_before["state"]),
        circuit_after=str(circuit_after["state"]),
        circuit_transition=circuit_transition,
        circuit_failures=int(circuit_after["consecutive_failures"]),
    )
# --------------------------------------------------------------------------- #

async def _health_payload() -> dict[str, Any]:
    last_search = _last_search or {}
    provider_states = last_search.get("provider_states", {})
    brave_breaker = _breaker.snapshot("brave")
    ready = (
        brave_breaker["state"] != "open"
        and _provider_credential_usable("brave")
    )
    last_status = last_search.get("status")
    if ready and last_status not in {"degraded", "error"}:
        status = "ok"
    elif ready:
        status = "degraded"
    else:
        status = "down"
    return {
        "status": status,
        "ready": ready,
        "service": "mcp-websearch",
        "policy": {
            "total_timeout_s": SEARCH_TOTAL_TIMEOUT,
            "brave_timeout_s": BRAVE_TIMEOUT,
            "fetch_fallbacks": {
                "decodo": {
                    "enabled": DECODO_FALLBACK_ENABLED,
                    "credential_configured": bool(_resolve_decodo_key()),
                    "timeout_s": DECODO_TIMEOUT,
                },
                "jina": {
                    "enabled": JINA_FALLBACK_ENABLED,
                    "timeout_s": JINA_TIMEOUT,
                },
                "operation_timeout_s": FETCH_OPERATION_TIMEOUT,
            },
        },
        "provider_stack": "brave",
        "providers": [
            {
                "name": "brave",
                "output": "raw",
                "timeout_s": BRAVE_TIMEOUT,
                "credential_configured": _provider_credential_configured("brave"),
                "credential_usable": _provider_credential_usable("brave"),
                "circuit": brave_breaker,
                "last_state": provider_states.get("brave"),
            }
        ],
        "telemetry": _get_telemetry().status(),
        "last_search": _last_search,
    }


_UI_ROOT = Path(__file__).resolve().parent / "ui"


@mcp.custom_route("/", methods=["GET"])
async def ui_redirect(request: Request) -> RedirectResponse:
    """Send browser users to the dedicated local operations UI."""
    return RedirectResponse("/ui", status_code=307)


@mcp.custom_route("/ui", methods=["GET"])
async def ui(request: Request) -> FileResponse:
    """Serve the loopback-only Local Search operations UI."""
    return FileResponse(
        _UI_ROOT / "index.html",
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        },
    )


_UI_ASSET_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@mcp.custom_route("/ui/styles.css", methods=["GET"])
async def ui_styles(request: Request) -> FileResponse:
    return FileResponse(
        _UI_ROOT / "styles.css", media_type="text/css", headers=_UI_ASSET_HEADERS
    )


@mcp.custom_route("/ui/app.js", methods=["GET"])
async def ui_script(request: Request) -> FileResponse:
    return FileResponse(
        _UI_ROOT / "app.js", media_type="text/javascript", headers=_UI_ASSET_HEADERS
    )


@mcp.custom_route("/live", methods=["GET"])
async def live(request: Request) -> JSONResponse:
    """Dependency-free process liveness."""
    return JSONResponse({"status": "ok", "service": "mcp-websearch"})


@mcp.custom_route("/ready", methods=["GET"])
async def ready(request: Request) -> JSONResponse:
    """Dependency readiness; returns 503 when active-policy requirements are unmet."""
    payload = await _health_payload()
    return JSONResponse(payload, status_code=200 if payload["ready"] else 503)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Compatibility health endpoint. Always HTTP 200; inspect status and ready."""
    return JSONResponse(await _health_payload())


@mcp.custom_route("/stats", methods=["GET"])
async def stats(request: Request) -> JSONResponse:
    """Return query-free aggregate telemetry for an allowlisted time window."""
    window = request.query_params.get("window", "24h")
    try:
        payload = await asyncio.to_thread(_get_telemetry().stats, window)
    except InvalidWindow as exc:
        return JSONResponse(
            {"status": "error", "error": str(exc)},
            status_code=400,
        )
    except TelemetryUnavailable:
        return JSONResponse(
            {"status": "error", "available": False, "error": "telemetry unavailable"},
            status_code=503,
        )
    except Exception as exc:
        logger.warning("Telemetry stats failed (%s)", type(exc).__name__)
        return JSONResponse(
            {"status": "error", "available": False, "error": "telemetry unavailable"},
            status_code=503,
        )
    return JSONResponse(payload)


# --------------------------------------------------------------------------- #
# MCP tools.
# --------------------------------------------------------------------------- #

def _coerce_backend_outcome(value: Any, backend: str, elapsed_ms: float) -> _BackendOutcome:
    """Normalize a provider result and preserve the backend timing."""
    if isinstance(value, _BackendOutcome):
        return value
    return _BackendOutcome(backend=backend, state="error", elapsed_ms=elapsed_ms)


async def _run_backend(backend: str, awaitable: Any, timeout: float) -> _BackendOutcome:
    started = time.monotonic()
    circuit_before = _breaker.snapshot(backend)
    try:
        value = await asyncio.wait_for(awaitable, timeout=max(0.05, timeout))
    except asyncio.TimeoutError:
        circuit_transition = _breaker.record_failure(backend)
        circuit_after = _breaker.snapshot(backend)
        return _BackendOutcome(
            backend=backend,
            state="timeout",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            attempts=1,
            error="stage deadline exceeded",
            circuit_before=str(circuit_before["state"]),
            circuit_after=str(circuit_after["state"]),
            circuit_transition=circuit_transition,
            circuit_failures=int(circuit_after["consecutive_failures"]),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        circuit_transition = _breaker.record_failure(backend)
        circuit_after = _breaker.snapshot(backend)
        logger.warning("%s search raised %s", backend, type(exc).__name__)
        return _BackendOutcome(
            backend=backend,
            state="error",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            error=type(exc).__name__,
            circuit_before=str(circuit_before["state"]),
            circuit_after=str(circuit_after["state"]),
            circuit_transition=circuit_transition,
            circuit_failures=int(circuit_after["consecutive_failures"]),
        )
    return _coerce_backend_outcome(
        value,
        backend,
        round((time.monotonic() - started) * 1000, 1),
    )


def _fallback_reason(outcome: _BackendOutcome) -> str:
    name = outcome.backend
    if outcome.state == "degraded":
        return f"{name}_degraded"
    return {
        "ok": f"{name}_empty",
        "empty": f"{name}_empty",
        "timeout": f"{name}_timeout",
        "circuit_open": f"{name}_circuit_open",
    }.get(outcome.state, f"{name}_error")


def _provider_search(
    provider: SearchProvider, query: str, num_results: int, intent: str
) -> Awaitable[_BackendOutcome]:
    """Forward explicit intent while preserving the legacy general call shape."""
    if intent == "general":
        return provider.search(query, num_results)
    return provider.search(query, num_results, intent)


def _result_backend(results: list[dict[str, Any]]) -> str:
    # Derive the backend label from the distinct provider names on the
    # results, preserving provider order.
    names: list[str] = []
    for result in results:
        name = result.get("provider") or result.get("engine")
        if isinstance(name, str) and name not in names:
            names.append(name)
    if not names:
        return "none"
    return "+".join(names)


def _timings(
    started: float, outcomes: list[_BackendOutcome | None]
) -> dict[str, float | None]:
    timings: dict[str, float | None] = {"total": round((time.monotonic() - started) * 1000, 1)}
    # Every provider key is always present so callers and tests see a stable shape.
    for provider in _PROVIDERS:
        timings[provider.name] = None
    for outcome in outcomes:
        if outcome is not None:
            timings[outcome.backend] = outcome.elapsed_ms
    return timings


async def _web_search_impl(
    query: str,
    num_results: int = 8,
    *,
    mode: str | None = None,
    intent: str = "general",
) -> str:
    """Execute one web search using the public tool's stable payload contract."""
    started = time.monotonic()
    query = query.strip()
    requested = min(MAX_NUM_RESULTS, max(1, int(num_results)))
    effective_mode = (mode or _SEARCH_MODE).strip().lower()
    if effective_mode not in {"normal", "sensitive", "maximum_recall"}:
        effective_mode = "normal"
    effective_intent = str(intent or "general").strip().lower()
    if effective_intent not in SEARCH_INTENTS:
        timings_ms = _default_timings_ms()
        timings_ms["total"] = round((time.monotonic() - started) * 1000, 1)
        return _finish_search(
            _search_error_payload(
                query,
                "Search error: intent must be one of: general, current, news.",
                timings_ms=timings_ms,
                mode=effective_mode,
                search_mode=effective_mode,
            ),
            status="error",
            backend="none",
            requested_count=requested,
            result_count=0,
            timings_ms=timings_ms,
            mode=effective_mode,
        )
    if not query:
        timings_ms = _default_timings_ms()
        timings_ms["total"] = round((time.monotonic() - started) * 1000, 1)
        return _finish_search(
            _search_error_payload(
                query,
                "Search error: query must not be empty.",
                timings_ms=timings_ms,
                mode=effective_mode,
                search_mode=effective_mode,
            ),
            status="error",
            backend="none",
            requested_count=requested,
            result_count=0,
            timings_ms=timings_ms,
            mode=effective_mode,
        )
    if len(query) > MAX_QUERY_CHARS:
        timings_ms = _default_timings_ms()
        timings_ms["total"] = round((time.monotonic() - started) * 1000, 1)
        return _finish_search(
            _search_error_payload(
                "",
                f"Search error: query must be at most {MAX_QUERY_CHARS} characters.",
                timings_ms=timings_ms,
                mode=effective_mode,
                search_mode=effective_mode,
            ),
            status="error",
            backend="none",
            requested_count=requested,
            result_count=0,
            timings_ms=timings_ms,
            mode=effective_mode,
        )

    # Sensitive / no-egress mode makes no external call. A local KB/corpus
    # index is not yet wired, so refuse with a structured error rather than
    # leak the query to any provider.
    if effective_mode == "sensitive":
        timings_ms = _default_timings_ms()
        timings_ms["total"] = round((time.monotonic() - started) * 1000, 1)
        return _finish_search(
            _search_error_payload(
                query,
                "Search error: sensitive/no-egress mode forbids external search and no local corpus is configured.",
                timings_ms=timings_ms,
                attempted=[],
                provider_states={},
                mode=effective_mode,
                search_mode=effective_mode,
            ),
            status="error",
            backend="none",
            requested_count=requested,
            result_count=0,
            timings_ms=timings_ms,
            mode=effective_mode,
        )

    cache_variant = _search_cache_variant(
        requested, effective_mode, effective_intent
    )
    cache = _get_cache()
    try:
        cache_hit = (
            cache.get_search(query, variant=cache_variant)
            if cache is not None else None
        )
    except Exception:
        cache_hit = None
    if cache_hit is not None:
        cached = cache_hit.payload
        cached_results = cached.get("results")
        cached_suggestions = cached.get("suggestions")
        cached_status = str(cached.get("status") or "ok")
        if (
            isinstance(cached_results, list)
            and all(
                isinstance(result, dict)
                and "rank" in result
                and isinstance(result.get("snippet"), (str, type(None)))
                for result in cached_results
            )
            and isinstance(cached_suggestions, list)
            and all(isinstance(item, str) for item in cached_suggestions)
            and isinstance(cached.get("unresponsive_engines"), (list, type(None)))
            and isinstance(cached.get("provider_states"), (dict, type(None)))
            and isinstance(cached.get("answer"), (str, type(None)))
            and isinstance(cached.get("citations"), (list, type(None)))
            and cached_status in {"ok", "degraded"}
            and (cached_results or bool(cached.get("answer")))
        ):
            timings_ms = _default_timings_ms()
            timings_ms["total"] = round((time.monotonic() - started) * 1000, 1)
            backend = str(cached.get("backend") or _result_backend(cached_results))
            rendered = _format_results(
                query,
                cached_results,
                cached_suggestions,
                status=cached_status,
                backend=backend,
                attempted=[],
                fallback_reason=cached.get("fallback_reason"),
                timings_ms=timings_ms,
                unresponsive_engines=cached.get("unresponsive_engines"),
                provider_states=cached.get("provider_states"),
                answer=cached.get("answer"),
                citations=cached.get("citations"),
                mode=effective_mode,
                search_mode=effective_mode,
                cache_hit=True,
                cache_age_seconds=cache_hit.age_seconds,
            )
            return _finish_search(
                rendered,
                status=cached_status,
                backend=backend,
                requested_count=requested,
                result_count=len(cached_results),
                timings_ms=timings_ms,
                mode=effective_mode,
                cache_hit=True,
            )

    candidate_limit = min(MAX_NUM_RESULTS, requested * RESULT_OVERFETCH_FACTOR)
    provider = _PROVIDERS[0]
    remaining = SEARCH_TOTAL_TIMEOUT - (time.monotonic() - started)
    if remaining <= 0:
        circuit = _breaker.snapshot(provider.name)
        outcome = _BackendOutcome(
            backend=provider.name,
            state="timeout",
            error="total deadline exceeded",
            circuit_before=str(circuit["state"]),
            circuit_after=str(circuit["state"]),
            circuit_failures=int(circuit["consecutive_failures"]),
        )
    else:
        outcome = await _run_backend(
            provider.name,
            _provider_search(provider, query, candidate_limit, effective_intent),
            min(provider.timeout, remaining),
        )

    results = _dedupe_and_rank(outcome.results, requested)
    attempted = _attempted_providers([outcome])
    provider_states = {outcome.backend: outcome.state}
    timings_ms = _timings(started, [outcome])
    backend = outcome.backend if results else "none"
    fallback_reason: str | None = None
    if results:
        status = "degraded" if outcome.state == "degraded" else "ok"
        rendered = _format_results(
            query,
            results,
            outcome.suggestions,
            status=status,
            backend=backend,
            attempted=attempted,
            timings_ms=timings_ms,
            unresponsive_engines=outcome.unresponsive_engines,
            provider_states=provider_states,
            answer=outcome.answer or None,
            citations=outcome.citations or None,
            mode=effective_mode,
            search_mode=effective_mode,
        )
    elif outcome.ok:
        status = "degraded" if outcome.state == "degraded" else "empty"
        fallback_reason = f"{outcome.backend}_degraded" if status == "degraded" else None
        rendered = _format_results(
            query,
            [],
            outcome.suggestions,
            status=status,
            backend="none",
            attempted=attempted,
            fallback_reason=fallback_reason,
            timings_ms=timings_ms,
            unresponsive_engines=outcome.unresponsive_engines,
            provider_states=provider_states,
            mode=effective_mode,
            search_mode=effective_mode,
        )
    else:
        status = "error"
        fallback_reason = _fallback_reason(outcome)
        error_message = (
            "Search error: all configured providers failed."
            if effective_mode == "maximum_recall"
            else "Search error: Brave search failed and no fallback provider is configured."
        )
        rendered = _search_error_payload(
            query,
            error_message,
            outcome.suggestions,
            attempted=attempted,
            fallback_reason=fallback_reason,
            timings_ms=timings_ms,
            unresponsive_engines=outcome.unresponsive_engines,
            provider_states=provider_states,
            mode=effective_mode,
            search_mode=effective_mode,
        )
    return _finish_search(
        rendered,
        status=status,
        backend=backend,
        requested_count=requested,
        result_count=len(results),
        fallback_reason=fallback_reason,
        timings_ms=timings_ms,
        outcomes=[outcome],
        mode=effective_mode,
        cache_query=query,
        cache_variant=cache_variant,
        cache_news=effective_intent in {"current", "news"},
    )


@mcp.tool()
async def web_search(
    query: str,
    num_results: int = 8,
    mode: str | None = None,
    intent: SearchIntent = "general",
) -> str:
    """Search the Brave web index.

    Optional `mode` selects the ADR 0002 routing mode: `normal` (default),
    `sensitive` (no external egress; refuses without a local corpus), or
    `maximum_recall` (opt-in serial escalation across all configured providers).
    Explicit `intent` accepts `general` (default), `current`, or `news`; query
    text is never inspected to infer freshness.
    """
    return await _web_search_impl(
        query, num_results, mode=mode, intent=intent
    )


def _batch_error_payload(message: str, started: float, requested: int) -> str:
    timings_ms = {"total": round((time.monotonic() - started) * 1000, 1)}
    _record_search_telemetry(
        status="error",
        backend="none",
        requested_count=requested,
        result_count=0,
        fallback_reason=None,
        total_latency_ms=float(timings_ms["total"]),
        outcomes=[],
        mode=_SEARCH_MODE,
    )
    return json.dumps(
        {
            "status": "error",
            "error": message,
            "query_count": 0,
            "duplicates_ignored": 0,
            "results": [],
            "timings_ms": timings_ms,
            "mode": _SEARCH_MODE,
        }
    )


def _compact_batch_item(rendered: str) -> dict[str, Any]:
    """Remove the single-search display rendering while preserving structured data."""
    payload = json.loads(rendered)
    payload.pop("text", None)
    return payload


def _batch_timeout_item(query: str, requested: int, elapsed_ms: float) -> dict[str, Any]:
    _record_search_telemetry(
        status="timeout",
        backend="none",
        requested_count=requested,
        result_count=0,
        fallback_reason="batch_deadline",
        total_latency_ms=elapsed_ms,
        outcomes=[],
        mode=_SEARCH_MODE,
    )
    return {
        "query": query,
        "results": [],
        "suggestions": [],
        "status": "timeout",
        "backend": "none",
        "attempted": [],
        "fallback_reason": "batch_deadline",
        "timings_ms": {**_default_timings_ms(), "total": elapsed_ms},
        "mode": _SEARCH_MODE,
        "unresponsive_engines": [],
        "provider_states": {},
        "error": "Search error: batch deadline exceeded.",
    }


@mcp.tool()
async def batch_web_search(
    queries: BatchQueries, num_results: int = 8, intent: SearchIntent = "general"
) -> str:
    """Run up to three searches with one explicit general/current/news intent."""
    started = time.monotonic()
    requested = min(MAX_NUM_RESULTS, max(1, int(num_results)))
    effective_intent = str(intent or "general").strip().lower()
    if effective_intent not in SEARCH_INTENTS:
        return _batch_error_payload(
            "Batch search error: intent must be one of: general, current, news.",
            started,
            requested,
        )
    if not queries:
        return _batch_error_payload("Batch search error: queries must not be empty.", started, requested)
    if len(queries) > BATCH_MAX_QUERIES:
        return _batch_error_payload(
            f"Batch search error: at most {BATCH_MAX_QUERIES} queries are allowed.",
            started,
            requested,
        )

    normalized: list[str] = []
    seen: set[str] = set()
    duplicates_ignored = 0
    for raw_query in queries:
        query = " ".join(raw_query.split())
        if not query:
            return _batch_error_payload(
                "Batch search error: queries must not contain empty values.",
                started,
                requested,
            )
        if len(query) > BATCH_MAX_QUERY_CHARS:
            return _batch_error_payload(
                f"Batch search error: each query must be at most {BATCH_MAX_QUERY_CHARS} characters.",
                started,
                requested,
            )
        key = query.casefold()
        if key in seen:
            duplicates_ignored += 1
            continue
        seen.add(key)
        normalized.append(query)

    semaphore = asyncio.Semaphore(BATCH_MAX_CONCURRENCY)

    async def run_one(query: str) -> dict[str, Any]:
        try:
            async with semaphore:
                if effective_intent == "general":
                    rendered = await _web_search_impl(query, requested)
                else:
                    rendered = await _web_search_impl(
                        query, requested, intent=effective_intent
                    )
                return _compact_batch_item(rendered)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Batch search item failed internally (%s)", type(exc).__name__)
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            _record_search_telemetry(
                status="error",
                backend="none",
                requested_count=requested,
                result_count=0,
                fallback_reason=None,
                total_latency_ms=elapsed_ms,
                outcomes=[],
                mode=_SEARCH_MODE,
            )
            return {
                "query": query,
                "results": [],
                "suggestions": [],
                "status": "error",
                "backend": "none",
                "attempted": [],
                "fallback_reason": None,
                "timings_ms": {**_default_timings_ms(), "total": elapsed_ms},
                "mode": _SEARCH_MODE,
                "unresponsive_engines": [],
                "provider_states": {},
                "error": "Search error: internal batch item failure.",
            }

    tasks = [asyncio.create_task(run_one(query)) for query in normalized]
    try:
        done, pending = await asyncio.wait(tasks, timeout=SEARCH_TOTAL_TIMEOUT)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    items: list[dict[str, Any]] = []
    for query, task in zip(normalized, tasks, strict=True):
        if task in done and not task.cancelled():
            items.append(task.result())
        else:
            items.append(_batch_timeout_item(query, requested, elapsed_ms))

    failed = sum(item.get("status") in {"error", "timeout"} for item in items)
    if failed == 0:
        status = "ok"
    elif failed == len(items):
        status = "error"
    else:
        status = "partial"
    return json.dumps(
        {
            "status": status,
            "query_count": len(items),
            "duplicates_ignored": duplicates_ignored,
            "results": items,
            "timings_ms": {"total": elapsed_ms},
            "mode": _SEARCH_MODE,
        }
    )


@mcp.tool()
async def image_search(query: str, num_results: int = 8) -> str:
    """Search public image metadata through Brave with strict SafeSearch."""
    started = time.monotonic()
    safe_search = "strict"
    query = query.strip()
    requested = min(MAX_NUM_RESULTS, max(1, int(num_results)))
    if not query:
        timings_ms = _default_timings_ms()
        timings_ms["total"] = round((time.monotonic() - started) * 1000, 1)
        return _finish_search(
            _search_error_payload(
                query,
                "Image search error: query must not be empty.",
                timings_ms=timings_ms,
                mode="disabled",
                safe_search=safe_search,
            ),
            status="error",
            backend="none",
            requested_count=requested,
            result_count=0,
            timings_ms=timings_ms,
            mode="disabled",
        )
    if len(query) > MAX_QUERY_CHARS:
        timings_ms = _default_timings_ms()
        timings_ms["total"] = round((time.monotonic() - started) * 1000, 1)
        return _finish_search(
            _search_error_payload(
                "",
                f"Image search error: query must be at most {MAX_QUERY_CHARS} characters.",
                timings_ms=timings_ms,
                mode="disabled",
                safe_search=safe_search,
            ),
            status="error",
            backend="none",
            requested_count=requested,
            result_count=0,
            timings_ms=timings_ms,
            mode="disabled",
        )

    candidate_limit = min(
        MAX_NUM_RESULTS * RESULT_OVERFETCH_FACTOR,
        requested * RESULT_OVERFETCH_FACTOR,
    )
    outcome = await _run_backend(
        "brave",
        _brave_image_search(query, candidate_limit, _resolve_brave_key()),
        min(BRAVE_TIMEOUT, SEARCH_TOTAL_TIMEOUT),
    )
    results = _dedupe_and_rank_images(outcome.results, requested)
    timings_ms = _timings(started, [outcome])
    provider_states = {outcome.backend: outcome.state}
    attempted = _attempted_providers([outcome])
    fallback_reason: str | None = None

    if results:
        status = "degraded" if outcome.state == "degraded" else "ok"
        backend = outcome.backend
        rendered = _format_image_results(
            query,
            results,
            outcome.suggestions,
            status=status,
            backend=backend,
            attempted=attempted,
            safe_search=safe_search,
            fallback_reason=None,
            timings_ms=timings_ms,
            unresponsive_engines=outcome.unresponsive_engines,
            provider_states=provider_states,
        )
    elif outcome.ok:
        status = "degraded" if outcome.state == "degraded" else "empty"
        backend = "none"
        fallback_reason = f"{outcome.backend}_degraded" if status == "degraded" else None
        rendered = _format_image_results(
            query,
            [],
            outcome.suggestions,
            status=status,
            backend=backend,
            attempted=attempted,
            safe_search=safe_search,
            fallback_reason=fallback_reason,
            timings_ms=timings_ms,
            unresponsive_engines=outcome.unresponsive_engines,
            provider_states=provider_states,
        )
    else:
        status = "error"
        backend = "none"
        fallback_reason = _fallback_reason(outcome)
        message = f"Image search error: {outcome.backend} search failed."
        rendered = _search_error_payload(
            query,
            message,
            outcome.suggestions,
            attempted=attempted,
            fallback_reason=fallback_reason,
            timings_ms=timings_ms,
            unresponsive_engines=outcome.unresponsive_engines,
            provider_states=provider_states,
            mode="disabled",
            safe_search=safe_search,
        )

    return _finish_search(
        rendered,
        status=status,
        backend=backend,
        requested_count=requested,
        result_count=len(results),
        fallback_reason=fallback_reason,
        timings_ms=timings_ms,
        outcomes=[outcome],
        mode="disabled",
    )


async def _fetch_public_body(url: str) -> tuple[str, bytes, str, int]:
    current_url = url
    for _ in range(FETCH_MAX_REDIRECTS + 1):
        addresses = await _validate_public_http_url(current_url)
        pinned_url, host_header, extensions = _pinned_public_request(current_url, addresses[0])
        redirect_url: str | None = None
        client = await _public_fetch_client()
        try:
            stream_context = client.stream(
                "GET",
                pinned_url,
                headers={
                    "Host": host_header,
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,image/apng,*/*;q=0.8,"
                        "application/signed-exchange;v=b3;q=0.7"
                    ),
                    "Accept-Encoding": _FETCH_ACCEPT_ENCODING,
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-CH-UA": (
                        '"Google Chrome";v="131", "Chromium";v="131", '
                        '"Not_A Brand";v="24"'
                    ),
                    "Sec-CH-UA-Mobile": "?0",
                    "Sec-CH-UA-Platform": '"macOS"',
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                },
                follow_redirects=False,
                extensions=extensions,
            )
            async with stream_context as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise ValueError("redirect response missing Location header")
                    redirect_url = urllib.parse.urljoin(current_url, location)
                else:
                    # Preserve HTTP status precedence before applying body-size
                    # policy, then reject a valid oversized declaration without
                    # consuming the response stream.
                    resp.raise_for_status()
                    content_length = resp.headers.get("content-length", "").strip()
                    if re.fullmatch(r"[0-9]+", content_length):
                        if int(content_length) > FETCH_MAX_BYTES:
                            raise ValueError(
                                f"response exceeds {FETCH_MAX_BYTES} byte limit"
                            )
                    content_encoding = resp.headers.get(
                        "content-encoding", ""
                    ).strip().lower()
                    if content_encoding and content_encoding != "identity":
                        raise ValueError(
                            f"response uses unsupported content encoding: {content_encoding}"
                        )
                    body = bytearray()
                    async for chunk in resp.aiter_raw():
                        if len(body) + len(chunk) > FETCH_MAX_BYTES:
                            raise ValueError(
                                f"response exceeds {FETCH_MAX_BYTES} byte limit"
                            )
                        body.extend(chunk)
                    return (
                        current_url,
                        bytes(body),
                        resp.headers.get("content-type", "").lower(),
                        int(resp.status_code),
                    )
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()
        current_url = redirect_url or current_url
    raise ValueError("too many redirects")


def _bound_fetch_text(text: str, max_chars: int) -> str:
    """Bound cached extraction text while retaining an HTML attachment section."""
    if len(text) <= max_chars:
        return text
    marker = "\n\nAttachments:"
    if marker in text:
        article, attachment_tail = text.rsplit(marker, 1)
        attachments = f"Attachments:{attachment_tail}"
        if len(attachments) + 2 < max_chars:
            article_budget = max_chars - len(attachments) - 2
            bounded_article = _truncate_text(
                article, article_budget, "\n\n... (truncated)"
            ).rstrip()
            return f"{bounded_article}\n\n{attachments}"
    return _truncate_text(text, max_chars, "\n\n... (truncated)")


def _looks_like_antibot_page(html: str) -> bool:
    """Detect common challenge pages without retaining or scanning full bodies."""
    sample = html[:2000].casefold()
    return any(
        marker.casefold() in sample
        for marker in (
            "Just a moment",
            "cf-browser-verification",
            "Performing security verification",
            "Human Verification",
            "Access Denied",
            "Pardon Our Interruption",
            "Press & Hold to confirm you are",
            "captcha",
        )
    )


async def _read_identity_provider_body(
    response: httpx.Response, *, provider: str, max_bytes: int
) -> bytes:
    """Read one provider response as bounded raw identity bytes."""
    content_encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if content_encoding not in {"", "identity"}:
        raise ValueError(f"{provider} returned unsupported content encoding")
    content_length = response.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
        raise ValueError(f"{provider} response exceeds {max_bytes} byte limit")
    body = bytearray()
    async for chunk in response.aiter_raw():
        if len(body) + len(chunk) > max_bytes:
            raise ValueError(f"{provider} response exceeds {max_bytes} byte limit")
        body.extend(chunk)
    return bytes(body)


async def _decodo_scraper_fetch(
    url: str, max_chars: int, trigger: str = "none", *, timeout: float | None = None
) -> _FetchResult:
    """Fetch one validated public page through Decodo's universal scraper.

    The fallback uses the premium proxy pool, browser rendering, and Markdown
    output because it is reached only after a direct anti-bot failure. The
    destination URL is never accompanied by caller headers, cookies, or auth.
    ``timeout`` clamps this tier to the remaining shared operation budget.
    """
    started = time.monotonic()
    token = _resolve_decodo_key()
    response_status: int | None = None

    def failure(
        message: str,
        *,
        status: int | None = None,
        outcome: str = "proxy_error",
    ) -> _FetchResult:
        text = _fetch_error(message)
        _record_fetch_telemetry(
            url=url,
            outcome=outcome,
            started=started,
            http_status=status,
            tier_used="proxy",
            provider="decodo",
            trigger=trigger,
            provider_http_status=response_status,
        )
        return _FetchResult(text, text, False, 0.0, url, "", "decodo")

    if not token:
        return failure("Decodo credential is not configured")

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }
    request_payload = {
        "url": url,
        "proxy_pool": "premium",
        "headless": "html",
        "markdown": True,
        "geo": "United States",
        "locale": "en-us",
        "device_type": "desktop",
    }
    response_bytes = 0
    tier_timeout = DECODO_TIMEOUT if timeout is None else max(1.0, float(timeout))
    try:
        async with asyncio.timeout(tier_timeout):
            async with httpx.AsyncClient(
                timeout=tier_timeout,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "POST", DECODO_API_URL, headers=headers, json=request_payload
                ) as response:
                    response_status = response.status_code
                    body = await _read_identity_provider_body(
                        response,
                        provider="Decodo",
                        max_bytes=DECODO_RESPONSE_MAX_BYTES,
                    )
                    response_bytes = len(body)
        payload = json.loads(body)
        results = payload.get("results") if isinstance(payload, dict) else None
        if (
            response_status is not None
            and response_status < 500
            and isinstance(results, list)
            and results
            and isinstance(results[0], dict)
        ):
            item = results[0]
        else:
            if response_status is not None and response_status >= 400:
                return failure(f"Decodo HTTP {response_status}", status=response_status)
            return failure("Decodo returned an invalid response", status=response_status)
        target_status = item.get("status_code")
        if not isinstance(target_status, int) or not 200 <= target_status < 300:
            return failure(
                "Decodo could not retrieve the target page",
                status=target_status if isinstance(target_status, int) else response_status,
            )
        content = item.get("content")
        if not isinstance(content, str):
            return failure("Decodo returned invalid content", status=target_status)
        content = content.replace("\x00", "").strip()
        if len(content) < 100 or _looks_like_antibot_page(content):
            return failure("Decodo returned unusable content", status=target_status)
        extracted = _bound_fetch_text(content, max_chars)
        _record_fetch_telemetry(
            url=url,
            outcome="proxy_success",
            started=started,
            http_status=target_status,
            body_bytes=response_bytes,
            tier_used="proxy",
            provider="decodo",
            trigger=trigger,
            provider_http_status=response_status,
        )
        return _FetchResult(
            extracted, None, False, 0.0, url, "text/markdown", "decodo"
        )
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        return failure(f"Decodo request failed: {type(exc).__name__}", outcome="timeout")
    except httpx.RequestError as exc:
        return failure(f"Decodo request failed: {type(exc).__name__}")
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        return failure("Decodo returned an invalid response", status=response_status)
    except Exception as exc:
        logger.warning("Decodo fallback failed (%s)", type(exc).__name__)
        return failure("Decodo request failed")


async def _jina_reader_fetch(
    url: str, max_chars: int, trigger: str = "none", *, timeout: float | None = None
) -> _FetchResult:
    """Fetch a public page through Jina Reader as a server-side fallback.

    Jina receives the public URL being fetched; this is intentionally only used
    after direct public-URL validation, never for authenticated/private traffic.
    ``timeout`` clamps this tier to the remaining shared operation budget.
    """
    started = time.monotonic()
    headers = {"Accept": "text/plain", "Accept-Encoding": "identity"}
    api_key = os.environ.get("JINA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response_status: int | None = None

    def failure(
        message: str,
        *,
        status: int | None = None,
        outcome: str = "proxy_error",
    ) -> _FetchResult:
        text = _fetch_error(message)
        _record_fetch_telemetry(
            url=url,
            outcome=outcome,
            started=started,
            http_status=status,
            tier_used="proxy",
            provider="jina",
            trigger=trigger,
            provider_http_status=response_status,
        )
        return _FetchResult(text, text, False, 0.0, url, "", "jina")

    tier_timeout = JINA_TIMEOUT if timeout is None else max(1.0, float(timeout))
    try:
        async with asyncio.timeout(tier_timeout):
            async with httpx.AsyncClient(
                timeout=tier_timeout, trust_env=False, follow_redirects=False
            ) as client:
                async with client.stream(
                    "GET", f"https://r.jina.ai/{url}", headers=headers
                ) as response:
                    response_status = response.status_code
                    body = await _read_identity_provider_body(
                        response,
                        provider="Jina Reader",
                        max_bytes=JINA_RESPONSE_MAX_BYTES,
                    )
        if response_status >= 400:
            return failure(f"Jina Reader HTTP {response_status}", status=response_status)
        decoded = body.decode("utf-8")
        marker = "Markdown Content:"
        if marker not in decoded:
            return failure("Jina Reader returned an invalid response", status=response_status)
        content = decoded.split(marker, 1)[1].strip()
        if len(content) < 100 or _looks_like_antibot_page(content):
            return failure("Jina Reader returned unusable content", status=response_status)
        extracted = _bound_fetch_text(content, max_chars)
        _record_fetch_telemetry(
            url=url,
            outcome="proxy_success",
            started=started,
            http_status=response_status,
            body_bytes=len(body),
            tier_used="proxy",
            provider="jina",
            trigger=trigger,
            provider_http_status=response_status,
        )
        return _FetchResult(
            extracted, None, False, 0.0, url, "text/markdown", "jina"
        )
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        return failure(f"Jina Reader request failed: {exc}", outcome="timeout")
    except httpx.RequestError as exc:
        return failure(f"Jina Reader request failed: {exc}")
    except (UnicodeError, ValueError) as exc:
        return failure(f"Jina Reader returned an invalid response: {exc}")
    except Exception as exc:
        return failure(f"Jina Reader request failed: {exc}")


def _connection_error_trigger(exc: BaseException) -> str:
    """Classify a transport-layer failure as a telemetry trigger kind."""
    cause = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLError):
            return "tls_error"
        cause = cause.__cause__ or cause.__context__
    return "connection_error"


async def _web_fetch_impl_inner(
    url: str,
    max_chars: int = 20000,
    attempt_state: dict[str, str] | None = None,
    *,
    deadline: float | None = None,
) -> _FetchResult:
    """Fetch and extract one URL, returning text plus internal cache metadata.

    ``deadline`` is the monotonic wall-clock time at which the overall fetch
    operation must complete; fallback tiers are clamped to the remaining budget.
    """
    max_chars = min(50000, max(1, int(max_chars)))
    attempt_state = attempt_state if attempt_state is not None else {
        "provider": "direct",
        "trigger": "none",
    }
    fetch_started = time.monotonic()
    operation_deadline = deadline if deadline is not None else (
        fetch_started + FETCH_OPERATION_TIMEOUT
    )
    cache = _get_cache()

    def finish(
        result: _FetchResult,
        *,
        trigger: str = "none",
        http_status: int | None = None,
        body_bytes: int | None = None,
    ) -> _FetchResult:
        _record_fetch_operation(
            url=url,
            result=result,
            started=fetch_started,
            trigger=trigger,
            http_status=http_status,
            body_bytes=body_bytes,
        )
        return result

    def failure(message: str, *, provider: str = "none") -> _FetchResult:
        text = _fetch_error(message)
        return _FetchResult(text, text, False, 0.0, url, "", provider)

    def cache_fallback(result: _FetchResult) -> None:
        if cache is None:
            return
        try:
            cache.put_content(
                url,
                result.text,
                content_type="text/markdown",
                final_url=url,
            )
        except Exception:
            pass

    def bound_fallback(result: _FetchResult) -> _FetchResult:
        return _FetchResult(
            _bound_fetch_text(result.text, max_chars),
            result.error,
            result.cache_hit,
            result.cache_age_seconds,
            result.url,
            result.content_type,
            result.provider,
        )

    async def fallback_success(trigger: str) -> _FetchResult | None:
        remaining = operation_deadline - time.monotonic()
        if remaining < 1.0:
            return None
        if DECODO_FALLBACK_ENABLED and _resolve_decodo_key():
            attempt_state.update(provider="decodo", trigger=trigger)
            try:
                result = await _decodo_scraper_fetch(
                    url, 50000, trigger, timeout=remaining
                )
            except Exception as exc:
                logger.warning("Decodo fallback failed (%s)", type(exc).__name__)
            else:
                if result.error is None:
                    cache_fallback(result)
                    return bound_fallback(result)
        remaining = operation_deadline - time.monotonic()
        if remaining < 1.0:
            return None
        if JINA_FALLBACK_ENABLED:
            attempt_state.update(provider="jina", trigger=trigger)
            try:
                result = await _jina_reader_fetch(
                    url, 50000, trigger, timeout=remaining
                )
            except Exception as exc:
                logger.warning("Jina Reader fallback failed (%s)", type(exc).__name__)
            else:
                if result.error is None:
                    cache_fallback(result)
                    return bound_fallback(result)
        return None

    try:
        cached = (
            cache.get_content(url)
            if cache is not None and _cache_lookup_allowed(url)
            else None
        )
    except Exception:
        cached = None
    if cached is not None:
        text = _bound_fetch_text(
            cached.content.decode("utf-8", errors="replace"), max_chars
        )
        _record_fetch_telemetry(
            url=url,
            outcome="success",
            started=fetch_started,
            body_bytes=len(cached.content),
            cache_hit=True,
            provider="cache",
        )
        return finish(
            _FetchResult(
                text=text,
                error=None,
                cache_hit=True,
                cache_age_seconds=cached.age_seconds,
                url=cached.final_url or url,
                content_type=cached.content_type,
                provider="cache",
            ),
            body_bytes=len(cached.content),
        )

    try:
        current_url, body, content_type, http_status = await asyncio.wait_for(
            _fetch_public_body(url),
            timeout=FETCH_TIMEOUT,
        )
    except _UnsafeTargetError as exc:
        # Hard fail-closed boundary: the target never passed public validation,
        # so it must never be handed to Decodo or Jina to resolve independently.
        _record_fetch_telemetry(
            url=url,
            outcome="unsafe_url",
            started=fetch_started,
            provider="direct",
            trigger="unsafe_url",
        )
        return finish(failure(str(exc)), trigger="unsafe_url")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        trigger = f"http_{status}" if status in _FALLBACK_HTTP_STATUSES else "none"
        _record_fetch_telemetry(
            url=url,
            outcome="http_error",
            started=fetch_started,
            http_status=status,
            provider="direct",
            trigger=trigger,
        )
        direct_failure = failure(f"HTTP {status} from {url}")
        error_content_type = exc.response.headers.get("content-type", "").lower()
        error_media_type = error_content_type.split(";", 1)[0].strip()
        error_is_textual = (
            "html" in error_media_type
            or error_media_type.startswith("text/")
            or "json" in error_media_type
            or "xml" in error_media_type
        )
        if trigger != "none" and (not error_media_type or error_is_textual):
            fallback = await fallback_success(trigger)
            if fallback is not None:
                return finish(
                    fallback,
                    trigger=trigger,
                    body_bytes=len(fallback.text.encode("utf-8")),
                )
        return finish(direct_failure, trigger=trigger, http_status=status)
    except httpx.TimeoutException as exc:
        trigger = "timeout"
        _record_fetch_telemetry(
            url=url,
            outcome="timeout",
            started=fetch_started,
            provider="direct",
            trigger=trigger,
        )
        direct_failure = failure(f"request failed: {exc}")
        fallback = await fallback_success(trigger)
        if fallback is not None:
            return finish(
                fallback,
                trigger=trigger,
                body_bytes=len(fallback.text.encode("utf-8")),
            )
        return finish(direct_failure, trigger=trigger)
    except httpx.RequestError as exc:
        trigger = _connection_error_trigger(exc)
        _record_fetch_telemetry(
            url=url,
            outcome="connection_error",
            started=fetch_started,
            provider="direct",
            trigger=trigger,
        )
        direct_failure = failure(f"request failed: {exc}")
        fallback = await fallback_success(trigger)
        if fallback is not None:
            return finish(
                fallback,
                trigger=trigger,
                body_bytes=len(fallback.text.encode("utf-8")),
            )
        return finish(direct_failure, trigger=trigger)
    except asyncio.TimeoutError:
        trigger = "timeout"
        _record_fetch_telemetry(
            url=url,
            outcome="timeout",
            started=fetch_started,
            provider="direct",
            trigger=trigger,
        )
        direct_failure = failure(f"request exceeded {FETCH_TIMEOUT:g} second deadline")
        fallback = await fallback_success(trigger)
        if fallback is not None:
            return finish(
                fallback,
                trigger=trigger,
                body_bytes=len(fallback.text.encode("utf-8")),
            )
        return finish(direct_failure, trigger=trigger)
    except ValueError as exc:
        # Post-validation policy failures (oversize, unsupported encoding,
        # redirect missing Location, too many redirects) and any other
        # validation-time ValueError: no remote fallback.
        _record_fetch_telemetry(
            url=url,
            outcome="policy_error",
            started=fetch_started,
            provider="direct",
        )
        return finish(failure(str(exc)))
    except Exception as exc:
        _record_fetch_telemetry(url=url, outcome="connection_error", started=fetch_started)
        return finish(failure(f"unexpected error: {exc}"))

    media_type = content_type.split(";", 1)[0].strip()
    is_pdf = "application/pdf" in content_type or body.startswith(b"%PDF-")
    is_textual = (
        "html" in media_type
        or media_type.startswith("text/")
        or "json" in media_type
        or "xml" in media_type
    )
    if not is_pdf and (not media_type or is_textual):
        decoded_for_fallback = _decode_text_body(body, content_type)
        is_html = "html" in media_type
        trigger = "none"
        if not body:
            trigger = "empty"
        elif is_html and (
            len(body) < 200 or _looks_like_antibot_page(decoded_for_fallback)
        ):
            trigger = "antibot"
        if trigger != "none":
            _record_fetch_telemetry(
                url=url,
                outcome="challenge",
                started=fetch_started,
                http_status=http_status,
                body_bytes=len(body),
                provider="direct",
                trigger=trigger,
            )
            reason = (
                "empty response"
                if trigger == "empty"
                else "response appears to be an anti-bot page"
            )
            direct_failure = failure(f"{reason} from {url}")
            fallback = await fallback_success(trigger)
            if fallback is not None:
                return finish(
                    fallback,
                    trigger=trigger,
                    body_bytes=len(fallback.text.encode("utf-8")),
                )
            return finish(
                direct_failure,
                trigger=trigger,
                http_status=http_status,
                body_bytes=len(body),
            )

    def extraction_failure(message: str) -> _FetchResult:
        _record_fetch_telemetry(
            url=url,
            outcome="extraction_error",
            started=fetch_started,
            http_status=http_status,
            body_bytes=len(body),
            provider="direct",
        )
        return finish(
            failure(message, provider="direct"),
            http_status=http_status,
            body_bytes=len(body),
        )

    async def extraction_fallback_failure(message: str) -> _FetchResult:
        # HTML extraction failures are recoverable via Decodo's rendered browser
        # + Markdown output, so record the direct failure and try the fallback
        # tiers before giving up. PDF/binary failures use ``extraction_failure``.
        trigger = "extraction_error"
        _record_fetch_telemetry(
            url=url,
            outcome="extraction_error",
            started=fetch_started,
            http_status=http_status,
            body_bytes=len(body),
            provider="direct",
            trigger=trigger,
        )
        direct_failure = failure(message, provider="direct")
        fallback = await fallback_success(trigger)
        if fallback is not None:
            return finish(
                fallback,
                trigger=trigger,
                body_bytes=len(fallback.text.encode("utf-8")),
            )
        return finish(
            direct_failure,
            trigger=trigger,
            http_status=http_status,
            body_bytes=len(body),
        )

    full_extracted: str
    effective_content_type = content_type
    if is_pdf:
        effective_content_type = "application/pdf"
        try:
            async with _bounded_extraction_slot(
                _pdf_extract_admission,
                _pdf_extract_semaphore,
                PDF_EXTRACT_TIMEOUT,
                "PDF",
            ) as deadline:
                text, method = await _extract_pdf_text(body, deadline)
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            asyncio.TimeoutError,
            _OutputLimitExceeded,
        ) as exc:
            return extraction_failure(f"PDF extraction failed: {exc}")
        if text is None:
            return extraction_failure(method)
        full_extracted = _pdf_fetch_text(current_url, text, method, 50000)
    else:
        textual = (
            "html" in media_type
            or media_type.startswith("text/")
            or "json" in media_type
            or "xml" in media_type
        )
        if not _looks_like_text(body, content_type):
            return extraction_failure(
                f"unsupported binary content type: {media_type or 'unknown'}"
            )
        if not textual and media_type not in {"", "application/octet-stream"}:
            return extraction_failure(
                f"unsupported binary content type: {media_type or 'unknown'}"
            )

        decoded = _decode_text_body(body, content_type)
        if "html" in media_type:
            try:
                full_extracted = await _extract_html_content_isolated(
                    decoded, current_url, 50000
                )
            except asyncio.TimeoutError:
                return await extraction_fallback_failure(
                    f"HTML extraction exceeded {HTML_EXTRACT_TIMEOUT:g} second deadline"
                )
            except _OutputLimitExceeded as exc:
                return await extraction_fallback_failure(f"HTML extraction failed: {exc}")
            except (OSError, RuntimeError) as exc:
                return await extraction_fallback_failure(f"HTML extraction failed: {exc}")
        else:
            full_extracted = _truncate_text(decoded, 50000, "\n\n... (truncated)")

    extracted = _bound_fetch_text(full_extracted, max_chars)
    if cache is not None:
        try:
            cache.put_content(
                url,
                full_extracted,
                content_type=effective_content_type,
                final_url=current_url,
            )
        except Exception:
            pass
    _record_fetch_telemetry(
        url=url,
        outcome="success",
        started=fetch_started,
        http_status=http_status,
        body_bytes=len(body),
        provider="direct",
    )
    return finish(
        _FetchResult(
            text=extracted,
            error=None,
            cache_hit=False,
            cache_age_seconds=0.0,
            url=current_url,
            content_type=effective_content_type,
            provider="direct",
        ),
        http_status=http_status,
        body_bytes=len(body),
    )


async def _web_fetch_impl(url: str, max_chars: int = 20000) -> _FetchResult:
    """Run one fetch under a hard end-to-end wall-clock deadline."""
    started = time.monotonic()
    attempt_state = {"provider": "direct", "trigger": "none"}
    deadline = started + FETCH_OPERATION_TIMEOUT
    try:
        return await asyncio.wait_for(
            _web_fetch_impl_inner(url, max_chars, attempt_state, deadline=deadline),
            timeout=FETCH_OPERATION_TIMEOUT,
        )
    except asyncio.TimeoutError:
        message = _fetch_error(
            f"operation exceeded {FETCH_OPERATION_TIMEOUT:g} second deadline"
        )
        result = _FetchResult(message, message, False, 0.0, url, "", "none")
        provider = attempt_state["provider"]
        _record_fetch_telemetry(
            url=url,
            outcome="timeout",
            started=started,
            provider=provider,
            tier_used="proxy" if provider in {"decodo", "jina"} else "direct",
            trigger=attempt_state["trigger"],
        )
        _record_fetch_operation(url=url, result=result, started=started)
        return result


@mcp.tool()
async def web_fetch(url: str, max_chars: int = 20000) -> ToolResult:
    """Fetch a public URL while preserving text as the primary MCP content."""
    result = await _web_fetch_impl(url, max_chars)
    return ToolResult(
        content=result.text,
        structured_content={
            "text": result.text,
            "error": result.error,
            "cache_hit": result.cache_hit,
            "cache_age_seconds": max(0, int(result.cache_age_seconds)),
            "url": result.url,
            "content_type": result.content_type,
            "fetch_provider": result.provider,
        },
    )


@mcp.tool()
async def verify_url(url: str, max_chars: int = 20000) -> str:
    """Verify/retrieve content for one URL via the cached direct-fetch path."""
    max_chars = min(50000, max(1, int(max_chars)))
    direct = await _web_fetch_impl(url, max_chars)
    direct_ok = direct.error is None
    return json.dumps({
        "url": direct.url,
        "method": "direct" if direct_ok else "none",
        "text": direct.text[:max_chars] if direct_ok else "",
        "error": direct.error,
        "cache_hit": direct.cache_hit,
        "cache_age_seconds": max(0, int(direct.cache_age_seconds)),
        "content_type": direct.content_type,
        "fetch_provider": direct.provider,
    })


if __name__ == "__main__":
    # Initialize private SQLite state before accepting stdio tool calls.
    _get_telemetry()
    _get_cache()
    mcp.run()
