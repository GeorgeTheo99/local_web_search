#!/usr/bin/env python3
"""Policy-aware MCP web search broker for local SearXNG and Tavily.

Tools:
  - web_search(query, num_results=8)          SearXNG first; Tavily policy-controlled
  - batch_web_search(queries, num_results=8)  up to three searches under one deadline
  - image_search(query, num_results=8)        loopback SearXNG images; no fallback
  - web_fetch(url, max_chars=20000)           direct fetch with SSRF guard

HTTP diagnostics:
  - GET /live    dependency-free process liveness
  - GET /ready   provider readiness (503 when no backend is usable)
  - GET /health  compatibility diagnostics (always HTTP 200)
  - GET /stats   query-free aggregate telemetry (24h, 7d, or 30d)

WEBSEARCH_TAVILY_MODE controls external Tavily egress:
  - disabled:   SearXNG only
  - fallback:   Tavily only when SearXNG has no usable results (default)
  - supplement: Tavily when SearXNG is below the configured result minimum

Searches use one bounded 18-second-or-less budget, overfetch before URL dedupe,
and return additive status/backend/fallback/timing metadata. Tavily uses the
per-call X-Tavily-Key header, then TAVILY_API_KEY, otherwise its keyless mode.
Broker Authorization credentials are intentionally separate and never treated
as Tavily credentials.

Key configuration:
  SEARXNG_URL                         default http://127.0.0.1:8888
  WEBSEARCH_TAVILY_MODE               disabled|fallback|supplement
  WEBSEARCH_FETCH_MAX_BYTES           default 20 MiB
  WEBSEARCH_HTML_EXTRACT_TIMEOUT      default 8 seconds
  WEBSEARCH_PDF_MAX_PAGES             default 20
  WEBSEARCH_TOTAL_TIMEOUT             capped at 18 seconds
  WEBSEARCH_SEARXNG_TIMEOUT           default 7 seconds
  WEBSEARCH_TAVILY_TIMEOUT            default 8 seconds
  WEBSEARCH_SEARCH_MAX_BYTES          default 2 MiB per provider response
  WEBSEARCH_SUPPLEMENT_MIN_RESULTS    default 5
  LOCAL_SEARCH_DATA_DIR               default ../data beside this package
  LOCAL_SEARCH_TELEMETRY_ENABLED      default true
  TAVILY_API_KEY                      optional stdio/server fallback key
  TAVILY_BASE_URL                     default https://api.tavily.com
  MCP_PORT                            default 8889 (HTTP transport only)
"""

from __future__ import annotations

import asyncio
import atexit
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import socket
import sys
import tempfile
import time
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from pydantic import WithJsonSchema
from fastmcp.server.dependencies import get_http_headers
from starlette.requests import Request
from starlette.responses import JSONResponse

import html_extraction as htmlx
from telemetry import (
    InvalidWindow,
    ProviderEvent,
    SearchEvent,
    TelemetryStore,
    TelemetryUnavailable,
    classify_error,
    normalize_engine_failures,
)

logger = logging.getLogger("websearch-mcp")

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
TAVILY_BASE_URL = os.environ.get("TAVILY_BASE_URL", "https://api.tavily.com").rstrip("/")
# Environment fallback for stdio clients. HTTP clients should forward a Tavily
# key only through X-Tavily-Key; broker authentication is a separate concern.
TAVILY_API_KEY_ENV = os.environ.get("TAVILY_API_KEY", "").strip()
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
# client deadline so fallback has time to complete and serialize a response.
TAVILY_MODE = os.environ.get("WEBSEARCH_TAVILY_MODE", "fallback").strip().lower()
if TAVILY_MODE not in {"disabled", "fallback", "supplement"}:
    raise RuntimeError("WEBSEARCH_TAVILY_MODE must be disabled, fallback, or supplement")
SEARCH_TOTAL_TIMEOUT = _bounded_float("WEBSEARCH_TOTAL_TIMEOUT", 18.0, minimum=1.0, maximum=18.0)
SEARCH_TIMEOUT = _bounded_float(
    "WEBSEARCH_SEARXNG_TIMEOUT",
    os.environ.get("WEBSEARCH_SEARCH_TIMEOUT", "7"),
    minimum=0.25,
    maximum=SEARCH_TOTAL_TIMEOUT,
)
TAVILY_TIMEOUT = _bounded_float(
    "WEBSEARCH_TAVILY_TIMEOUT", 8.0, minimum=0.25, maximum=SEARCH_TOTAL_TIMEOUT
)
SUPPLEMENT_MIN_RESULTS = _bounded_int(
    "WEBSEARCH_SUPPLEMENT_MIN_RESULTS", 5, minimum=1, maximum=20
)
MAX_NUM_RESULTS = 20
RESULT_OVERFETCH_FACTOR = 2
MAX_QUERY_CHARS = 512
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
FETCH_TIMEOUT = _bounded_float("WEBSEARCH_FETCH_TIMEOUT", 30.0, minimum=1.0, maximum=60.0)
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
SEARCH_MAX_RETRIES = _bounded_int("WEBSEARCH_SEARCH_MAX_RETRIES", 1, minimum=0, maximum=3)
SEARCH_RETRY_BACKOFF = _bounded_float(
    "WEBSEARCH_SEARCH_RETRY_BACKOFF", 0.5, minimum=0.0, maximum=5.0
)

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
        _http_client = httpx.AsyncClient(timeout=FETCH_TIMEOUT, trust_env=False)
    return _http_client


async def _public_fetch_client() -> httpx.AsyncClient:
    """Return a one-hop client so TLS pools cannot cross original hostnames."""
    return httpx.AsyncClient(timeout=FETCH_TIMEOUT, trust_env=False)


mcp = FastMCP(
    "websearch",
    instructions=(
        "Web and image search plus page fetching via local SearXNG, with policy-controlled Tavily fallback or supplementation for web search only. "
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


@dataclass
class _RequestOutcome:
    data: dict[str, Any] | None
    state: str
    attempts: int
    error: str | None = None
    http_status: int | None = None


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


_last_search: dict[str, Any] | None = None
_telemetry: TelemetryStore | None = None
_telemetry_lock = Lock()


# --------------------------------------------------------------------------- #
# Error / payload helpers.
# --------------------------------------------------------------------------- #

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
) -> dict[str, Any]:
    return {
        "status": status,
        "backend": backend,
        "attempted": attempted or [],
        "fallback_reason": fallback_reason,
        "timings_ms": timings_ms or {"total": 0.0, "searxng": None, "tavily": None},
        "mode": mode or TAVILY_MODE,
        "unresponsive_engines": (unresponsive_engines or [])[:10],
        "provider_states": provider_states or {},
    }


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


def _record_search_telemetry(
    *,
    status: str,
    backend: str,
    requested_count: int,
    result_count: int,
    fallback_reason: str | None,
    total_latency_ms: float,
    searxng: _BackendOutcome | None,
    tavily: _BackendOutcome | None,
    mode: str | None = None,
) -> None:
    """Queue only explicitly allowlisted operational fields, never a search payload."""
    try:
        providers = tuple(
            _provider_event(outcome)
            for outcome in (searxng, tavily)
            if outcome is not None
        )
        engine_failures = normalize_engine_failures(
            searxng.unresponsive_engines if searxng is not None else []
        )
        _get_telemetry().record(
            SearchEvent(
                status=status,
                backend=backend,
                mode=mode or TAVILY_MODE,
                requested_count=requested_count,
                result_count=result_count,
                fallback_reason=fallback_reason,
                total_latency_ms=total_latency_ms,
                providers=providers,
                engine_failures=engine_failures,
            )
        )
    except Exception as exc:
        # Monitoring must never break or delay search behavior.
        logger.warning("Telemetry event dropped (%s)", type(exc).__name__)


def _finish_search(
    rendered: str,
    *,
    status: str,
    backend: str,
    requested_count: int,
    result_count: int,
    fallback_reason: str | None = None,
    timings_ms: dict[str, float | None] | None = None,
    searxng: _BackendOutcome | None = None,
    tavily: _BackendOutcome | None = None,
    mode: str | None = None,
) -> str:
    total_latency_ms = float((timings_ms or {}).get("total") or 0.0)
    _record_search_telemetry(
        status=status,
        backend=backend,
        requested_count=requested_count,
        result_count=result_count,
        fallback_reason=fallback_reason,
        total_latency_ms=total_latency_ms,
        searxng=searxng,
        tavily=tavily,
        mode=mode,
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
        ),
    }
    _record_last_search(payload)
    return json.dumps(payload)


def _fetch_error(message: str) -> str:
    return f"Fetch error: {message}"


# --------------------------------------------------------------------------- #
# Result normalization + dedupe.
# --------------------------------------------------------------------------- #

def _normalize_searxng_result(r: dict) -> dict[str, Any] | None:
    url = _public_http_url(r.get("url"))
    if url is None:
        return None
    return {
        "title": r.get("title", "Untitled"),
        "url": url,
        "domain": _domain(url),
        "snippet": (r.get("content") or "").strip(),
        "engine": r.get("engine") if isinstance(r.get("engine"), str) else "searxng",
        "score": None,
    }


def _public_http_url(value: Any) -> str | None:
    """Return a syntactically public-looking HTTP(S) URL without resolving or fetching it."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or any(char.isspace() for char in url):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").lower()
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


def _first_public_http_url(r: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        if url := _public_http_url(r.get(key)):
            return url
    return None


def _image_dimension(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 and str(value).strip() in {str(parsed), f"{parsed}.0"} else None


def _image_dimensions(r: dict[str, Any]) -> tuple[int | None, int | None]:
    width = _image_dimension(r.get("width"))
    height = _image_dimension(r.get("height"))
    resolution = r.get("resolution")
    if (width is None or height is None) and isinstance(resolution, str):
        match = re.search(r"(\d+)\s*[x×]\s*(\d+)", resolution, flags=re.IGNORECASE)
        if match:
            width = width or _image_dimension(match.group(1))
            height = height or _image_dimension(match.group(2))
    return width, height


def _image_mime_type(r: dict[str, Any]) -> str | None:
    value = r.get("mime_type") or r.get("img_format")
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if not value:
        return None
    if re.fullmatch(r"image/[a-z0-9.+-]+", value):
        return value
    subtype = {"jpg": "jpeg", "svg": "svg+xml", "tif": "tiff"}.get(value, value)
    if re.fullmatch(r"[a-z0-9.+-]+", subtype):
        return f"image/{subtype}"
    return None


def _image_text(r: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = r.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if key in {"license", "creator"} and isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return ""


_MODERATE_SAFESEARCH_IMAGE_ENGINES = frozenset({"duckduckgo images", "google images"})


def _normalize_searxng_image_result(r: dict[str, Any]) -> dict[str, Any] | None:
    image_url = _first_public_http_url(r, "img_src", "image_url", "image")
    if image_url is None:
        return None
    page_url = _first_public_http_url(r, "url", "page_url")
    width, height = _image_dimensions(r)
    engine = _image_text(r, "engine")
    if not engine and isinstance(r.get("engines"), (list, tuple)):
        engine = next(
            (item.strip() for item in r["engines"] if isinstance(item, str) and item.strip()),
            "",
        )
    engine = engine.lower()
    if engine not in _MODERATE_SAFESEARCH_IMAGE_ENGINES:
        return None
    source = _image_text(r, "source") or (_domain(page_url) if page_url else "")
    return {
        "title": _image_text(r, "title") or "Untitled",
        "image_url": image_url,
        "thumbnail_url": _first_public_http_url(
            r, "thumbnail_src", "thumbnail_url", "thumbnail"
        ),
        "page_url": page_url,
        "source": source,
        "engine": engine or "searxng",
        "width": width,
        "height": height,
        "mime_type": _image_mime_type(r),
        "creator": _image_text(r, "creator", "author"),
        "license": _image_text(r, "license", "license_name"),
        "license_url": _public_http_url(r.get("license_url")),
    }


def _normalize_tavily_result(r: dict) -> dict[str, Any] | None:
    url = _public_http_url(r.get("url"))
    if url is None:
        return None
    return {
        "title": r.get("title") or "Untitled",
        "url": url,
        "domain": _domain(url),
        "snippet": str(r.get("content") or r.get("raw_content") or ""),
        "engine": "tavily",
        "score": r.get("score"),
    }


def _domain(url: str) -> str:
    if not url:
        return ""
    try:
        return urllib.parse.urlparse(url).netloc
    except Exception:
        return ""


def _canonical_result_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return url.split("#", 1)[0].rstrip("/")
        query = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}
        ]
        path = parsed.path.rstrip("/") or "/"
        return urllib.parse.urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), path, urllib.parse.urlencode(sorted(query)), "")
        )
    except Exception:
        return url.split("#", 1)[0].rstrip("/")


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
    """Dedupe canonical image URLs before truncation while preserving SearXNG order."""
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


def _merge_with_secondary_reserve(
    primary: list[dict[str, Any]],
    secondary: list[dict[str, Any]],
    requested: int,
) -> list[dict[str, Any]]:
    """Keep primary ordering while reserving result slots for a fallback provider."""
    primary_unique = _dedupe_and_rank(primary, MAX_NUM_RESULTS)
    primary_keys = {_canonical_result_url(str(item.get("url") or "")) for item in primary_unique}
    secondary_unique = [
        item for item in _dedupe_and_rank(secondary, MAX_NUM_RESULTS)
        if _canonical_result_url(str(item.get("url") or "")) not in primary_keys
    ]
    if not secondary_unique:
        return _dedupe_and_rank(primary_unique, requested)
    reserve = min(len(secondary_unique), max(1, requested // 3))
    primary_limit = max(0, requested - reserve)
    candidates = [
        *primary_unique[:primary_limit],
        *secondary_unique[:reserve],
        *primary_unique[primary_limit:],
        *secondary_unique[reserve:],
    ]
    return _dedupe_and_rank(candidates, requested)


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
        ),
    }
    _record_last_search(payload)
    return json.dumps(payload)


def _format_image_results(
    query: str,
    results: list[dict[str, Any]],
    suggestions: list[str],
    *,
    status: str,
    backend: str,
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
        "safe_search": "moderate",
        **_search_metadata(
            status=status,
            backend=backend,
            attempted=["searxng"],
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


async def _resolve_host_ips(host: str, port: int, scheme: str) -> list[ipaddress._BaseAddress]:
    try:
        ip = ipaddress.ip_address(host)
        return [ip]
    except ValueError:
        pass
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, port or (443 if scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve URL host: {exc}") from exc
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def _is_private_ip(ip: ipaddress._BaseAddress) -> bool:
    return any(getattr(ip, attr, False) for attr in _PRIVATE_IP_ATTRS)


async def _validate_public_http_url(url: str) -> list[ipaddress._BaseAddress]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only public http(s) URLs can be fetched")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credential-bearing URLs cannot be fetched")
    host = parsed.hostname.strip().lower()
    if host in {"localhost", "local"} or host.endswith(".localhost"):
        raise ValueError("Refusing to fetch local/private URL")
    addresses = await _resolve_host_ips(host, parsed.port, parsed.scheme)
    if not addresses:
        raise ValueError("Could not resolve URL host")
    for ip in addresses:
        if _is_private_ip(ip):
            raise ValueError("Refusing to fetch local/private URL")
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
# Tavily key resolution (per-call header > env var).
# --------------------------------------------------------------------------- #

def _resolve_tavily_key() -> str:
    """Return the Tavily API key from the current HTTP request, falling back to
    the TAVILY_API_KEY env var. Empty if neither.

    Header precedence (custom headers pass through to tools; standard
    `Authorization` is reserved exclusively for MCP transport authentication):
      1. X-Tavily-Key   (explicit Tavily credential)
    Then env var TAVILY_API_KEY for stdio/server-side consumers. Generic
    X-Api-Key and Authorization values are never repurposed as Tavily keys.
    """
    try:
        headers = get_http_headers()
    except LookupError:
        # stdio transport / no HTTP context — env var only.
        return TAVILY_API_KEY_ENV
    value = headers.get("x-tavily-key")
    if value and value.strip():
        return value.strip()
    return TAVILY_API_KEY_ENV


# --------------------------------------------------------------------------- #
# Backend: SearXNG.
# --------------------------------------------------------------------------- #

def _coerce_request_outcome(value: Any) -> _RequestOutcome:
    """Accept legacy test doubles that return dict/None as well as new outcomes."""
    if isinstance(value, _RequestOutcome):
        return value
    if isinstance(value, dict):
        return _RequestOutcome(value, "ok", 1)
    return _RequestOutcome(None, "error", 1, "request failed")


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


def _tavily_search_url() -> str:
    """Return the configured Tavily endpoint only when credentials can be sent safely."""
    parsed = urllib.parse.urlsplit(TAVILY_BASE_URL)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("TAVILY_BASE_URL must be a credential-free HTTPS origin") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("TAVILY_BASE_URL must be a credential-free HTTPS origin")
    return urllib.parse.urlunsplit(("https", parsed.netloc, "/search", "", ""))


def _searxng_url_is_loopback() -> bool:
    try:
        parsed = urllib.parse.urlsplit(SEARXNG_URL)
        host = parsed.hostname or ""
        if parsed.scheme.lower() not in {"http", "https"} or not host:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        _ = parsed.port
        return host.lower().rstrip(".") == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def _searxng_request(
    path: str, params: dict, timeout: float | None = None
) -> _RequestOutcome:
    """GET SearXNG within one stage deadline, retrying only transient failures."""
    budget = SEARCH_TIMEOUT if timeout is None else max(0.05, timeout)
    deadline = time.monotonic() + budget
    qs = urllib.parse.urlencode(params)
    url = f"{SEARXNG_URL}{path}?{qs}"
    client = await _client()
    last_state = "error"
    last_error = "request failed"
    last_http_status: int | None = None
    attempts = 0

    for attempt in range(SEARCH_MAX_RETRIES + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _RequestOutcome(None, "timeout", attempts, "stage deadline exceeded")
        attempts = attempt + 1
        retryable = True
        try:
            async with client.stream(
                "GET",
                url,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                timeout=remaining,
            ) as response:
                response.raise_for_status()
                data = await _limited_json_object(response, provider="SearXNG")
                return _RequestOutcome(data, "ok", attempts, http_status=response.status_code)
        except httpx.TimeoutException:
            last_state = "timeout"
            last_error = "request timed out"
            logger.warning("SearXNG timed out (attempt %d)", attempts)
        except httpx.ConnectError:
            last_state = "error"
            last_error = "connection failed"
            logger.warning("SearXNG connection failed (attempt %d)", attempts)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            last_http_status = status_code
            retryable = status_code in {408, 425, 429} or status_code >= 500
            last_state = "error"
            last_error = f"HTTP {status_code}"
            logger.warning("SearXNG HTTP %d (attempt %d)", status_code, attempts)
        except Exception as exc:
            last_state = "error"
            last_error = type(exc).__name__
            logger.warning("SearXNG request failed (attempt %d): %s", attempts, type(exc).__name__)

        if attempt >= SEARCH_MAX_RETRIES or not retryable:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            last_state = "timeout"
            last_error = "stage deadline exceeded"
            break
        await asyncio.sleep(min(SEARCH_RETRY_BACKOFF, remaining))

    return _RequestOutcome(None, last_state, attempts, last_error, last_http_status)


async def _searxng_search(query: str, num_results: int) -> _BackendOutcome:
    started = time.monotonic()
    circuit_before = _breaker.snapshot("searxng")
    if not _breaker.allow("searxng"):
        logger.info("SearXNG circuit open; skipping")
        circuit_after = _breaker.snapshot("searxng")
        return _BackendOutcome(
            backend="searxng",
            state="circuit_open",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            circuit_before=str(circuit_before["state"]),
            circuit_after=str(circuit_after["state"]),
            circuit_failures=int(circuit_after["consecutive_failures"]),
        )

    try:
        raw_outcome = await _searxng_request(
            "/search", {"q": query, "format": "json", "categories": "general"}
        )
    except asyncio.CancelledError:
        _breaker.record_aborted("searxng")
        raise

    request = _coerce_request_outcome(raw_outcome)
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    if request.data is None:
        circuit_transition = _breaker.record_failure("searxng")
        circuit_after = _breaker.snapshot("searxng")
        return _BackendOutcome(
            backend="searxng",
            state=request.state,
            elapsed_ms=elapsed_ms,
            attempts=request.attempts,
            error=request.error,
            http_status=request.http_status,
            circuit_before=str(circuit_before["state"]),
            circuit_after=str(circuit_after["state"]),
            circuit_transition=circuit_transition,
            circuit_failures=int(circuit_after["consecutive_failures"]),
        )

    circuit_transition = _breaker.record_success("searxng")
    circuit_after = _breaker.snapshot("searxng")
    raw_results = request.data.get("results", [])
    if not isinstance(raw_results, list):
        raw_results = []
    results = [
        normalized
        for item in raw_results[:num_results]
        if isinstance(item, dict)
        and (normalized := _normalize_searxng_result(item)) is not None
    ]
    suggestions = request.data.get("suggestions", [])
    if not isinstance(suggestions, list):
        suggestions = []
    unresponsive = request.data.get("unresponsive_engines", [])
    if not isinstance(unresponsive, list):
        unresponsive = []
    state = "degraded" if unresponsive else ("ok" if results else "empty")
    return _BackendOutcome(
        backend="searxng",
        results=results,
        suggestions=[str(item) for item in suggestions],
        ok=True,
        state=state,
        elapsed_ms=elapsed_ms,
        attempts=request.attempts,
        unresponsive_engines=unresponsive,
        http_status=request.http_status,
        circuit_before=str(circuit_before["state"]),
        circuit_after=str(circuit_after["state"]),
        circuit_transition=circuit_transition,
        circuit_failures=int(circuit_after["consecutive_failures"]),
    )


async def _searxng_image_search(query: str, num_results: int) -> _BackendOutcome:
    """Search images through loopback SearXNG only and normalize URL metadata."""
    started = time.monotonic()
    circuit_before = _breaker.snapshot("searxng")
    if not _searxng_url_is_loopback():
        return _BackendOutcome(
            backend="searxng",
            state="error",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            error="SearXNG URL is not loopback",
            circuit_before=str(circuit_before["state"]),
            circuit_after=str(circuit_before["state"]),
            circuit_failures=int(circuit_before["consecutive_failures"]),
        )
    if not _breaker.allow("searxng"):
        logger.info("SearXNG circuit open; skipping image search")
        circuit_after = _breaker.snapshot("searxng")
        return _BackendOutcome(
            backend="searxng",
            state="circuit_open",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            circuit_before=str(circuit_before["state"]),
            circuit_after=str(circuit_after["state"]),
            circuit_failures=int(circuit_after["consecutive_failures"]),
        )

    try:
        raw_outcome = await _searxng_request(
            "/search",
            {"q": query, "format": "json", "categories": "images", "safesearch": 1},
        )
    except asyncio.CancelledError:
        _breaker.record_aborted("searxng")
        raise

    request = _coerce_request_outcome(raw_outcome)
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    if request.data is None:
        circuit_transition = _breaker.record_failure("searxng")
        circuit_after = _breaker.snapshot("searxng")
        return _BackendOutcome(
            backend="searxng",
            state=request.state,
            elapsed_ms=elapsed_ms,
            attempts=request.attempts,
            error=request.error,
            http_status=request.http_status,
            circuit_before=str(circuit_before["state"]),
            circuit_after=str(circuit_after["state"]),
            circuit_transition=circuit_transition,
            circuit_failures=int(circuit_after["consecutive_failures"]),
        )

    circuit_transition = _breaker.record_success("searxng")
    circuit_after = _breaker.snapshot("searxng")
    raw_results = request.data.get("results", [])
    if not isinstance(raw_results, list):
        raw_results = []
    results = [
        normalized
        for item in raw_results[:num_results]
        if isinstance(item, dict)
        and (normalized := _normalize_searxng_image_result(item)) is not None
    ]
    suggestions = request.data.get("suggestions", [])
    if not isinstance(suggestions, list):
        suggestions = []
    unresponsive = request.data.get("unresponsive_engines", [])
    if not isinstance(unresponsive, list):
        unresponsive = []
    state = "degraded" if unresponsive else ("ok" if results else "empty")
    return _BackendOutcome(
        backend="searxng",
        results=results,
        suggestions=[str(item) for item in suggestions],
        ok=True,
        state=state,
        elapsed_ms=elapsed_ms,
        attempts=request.attempts,
        unresponsive_engines=unresponsive,
        http_status=request.http_status,
        circuit_before=str(circuit_before["state"]),
        circuit_after=str(circuit_after["state"]),
        circuit_transition=circuit_transition,
        circuit_failures=int(circuit_after["consecutive_failures"]),
    )


# --------------------------------------------------------------------------- #
# Backend: Tavily.
# --------------------------------------------------------------------------- #

async def _tavily_search(query: str, num_results: int, api_key: str) -> _BackendOutcome:
    """Search Tavily in keyed or keyless mode without mixing credential roles."""
    started = time.monotonic()
    credential_mode = "keyed" if api_key else "keyless"
    circuit_before = _breaker.snapshot("tavily")
    if not _breaker.allow("tavily"):
        logger.info("Tavily circuit open; skipping")
        circuit_after = _breaker.snapshot("tavily")
        return _BackendOutcome(
            backend="tavily",
            state="circuit_open",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            credential_mode=credential_mode,
            circuit_before=str(circuit_before["state"]),
            circuit_after=str(circuit_after["state"]),
            circuit_failures=int(circuit_after["consecutive_failures"]),
        )

    payload = {
        "query": query,
        "max_results": min(MAX_NUM_RESULTS, max(1, num_results)),
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
        "topic": "general",
    }
    headers = (
        {"Authorization": f"Bearer {api_key}", "Accept-Encoding": "identity"}
        if api_key
        else {"X-Tavily-Access-Mode": "keyless", "Accept-Encoding": "identity"}
    )
    try:
        search_url = _tavily_search_url()
        client = await _client()
        async with client.stream(
            "POST",
            search_url,
            json=payload,
            headers=headers,
            timeout=TAVILY_TIMEOUT,
        ) as response:
            response.raise_for_status()
            data = await _limited_json_object(response, provider="Tavily")
    except asyncio.CancelledError:
        _breaker.record_aborted("tavily")
        raise
    except httpx.TimeoutException:
        circuit_transition = _breaker.record_failure("tavily")
        state = "timeout"
        error = "request timed out"
        http_status = None
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        http_status = status_code
        # Authentication/client errors prove the provider is reachable and
        # must not globally circuit-break valid keys or keyless traffic.
        if status_code in {400, 401, 403, 404, 422}:
            circuit_transition = _breaker.record_success("tavily")
        else:
            circuit_transition = _breaker.record_failure("tavily")
        state = "error"
        error = f"HTTP {status_code}"
    except Exception as exc:
        circuit_transition = _breaker.record_failure("tavily")
        state = "error"
        error = type(exc).__name__
        http_status = None
    else:
        circuit_transition = _breaker.record_success("tavily")
        circuit_after = _breaker.snapshot("tavily")
        raw_results = data.get("results", [])
        if not isinstance(raw_results, list):
            raw_results = []
        results = [
            normalized
            for item in raw_results[:num_results]
            if isinstance(item, dict)
            and (normalized := _normalize_tavily_result(item)) is not None
        ]
        return _BackendOutcome(
            backend="tavily",
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

    circuit_after = _breaker.snapshot("tavily")
    logger.warning(
        "Tavily search failed (%s): %s",
        "keyed" if api_key else "keyless",
        error,
    )
    return _BackendOutcome(
        backend="tavily",
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
# Health probe.
# --------------------------------------------------------------------------- #

async def _probe_searxng() -> dict[str, Any]:
    started = time.monotonic()
    try:
        client = await _client()
        resp = await client.get(f"{SEARXNG_URL}/healthz", timeout=3.0)
        return {
            "reachable": resp.status_code == 200,
            "status_code": resp.status_code,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }
    except Exception as e:
        return {
            "reachable": False,
            "error": str(e),
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }


async def _health_payload() -> dict[str, Any]:
    searxng = await _probe_searxng()
    searxng_breaker = _breaker.snapshot("searxng")
    tavily_breaker = _breaker.snapshot("tavily")
    last_search = _last_search or {}
    provider_states = last_search.get("provider_states", {})
    failed_states = {"error", "timeout", "circuit_open"}
    searxng_available = (
        bool(searxng.get("reachable"))
        and searxng_breaker["state"] != "open"
        and provider_states.get("searxng") not in failed_states
    )
    tavily_available = (
        TAVILY_MODE != "disabled"
        and tavily_breaker["state"] != "open"
        and provider_states.get("tavily") not in failed_states
    )
    ready = searxng_available or tavily_available
    last_status = last_search.get("status")
    if searxng_available and last_status not in {"degraded", "error"}:
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
            "tavily_mode": TAVILY_MODE,
            "total_timeout_s": SEARCH_TOTAL_TIMEOUT,
            "searxng_timeout_s": SEARCH_TIMEOUT,
            "tavily_timeout_s": TAVILY_TIMEOUT,
            "supplement_min_results": SUPPLEMENT_MIN_RESULTS,
        },
        "searxng_url": SEARXNG_URL,
        "searxng": {
            **searxng,
            "available": searxng_available,
            "last_state": provider_states.get("searxng"),
            "circuit": searxng_breaker,
        },
        "tavily": {
            "available": tavily_available,
            "enabled": TAVILY_MODE != "disabled",
            "env_key_configured": bool(TAVILY_API_KEY_ENV),
            "keyless_supported": True,
            "keyless_available": tavily_available,
            "base_url": TAVILY_BASE_URL,
            "mode": "keyed" if TAVILY_API_KEY_ENV else "keyless",
            "credential_mode": "keyed" if TAVILY_API_KEY_ENV else "keyless",
            "last_state": provider_states.get("tavily"),
            "circuit": tavily_breaker,
        },
        "telemetry": _get_telemetry().status(),
        "last_search": _last_search,
    }


@mcp.custom_route("/live", methods=["GET"])
async def live(request: Request) -> JSONResponse:
    """Dependency-free process liveness."""
    return JSONResponse({"status": "ok", "service": "mcp-websearch"})


@mcp.custom_route("/ready", methods=["GET"])
async def ready(request: Request) -> JSONResponse:
    """Dependency readiness; returns 503 only when no policy-allowed backend is usable."""
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
    """Accept legacy tuple-shaped test doubles during the contract transition."""
    if isinstance(value, _BackendOutcome):
        return value
    if backend == "searxng" and isinstance(value, tuple) and len(value) == 4:
        results, suggestions, ok, unresponsive = value
        return _BackendOutcome(
            backend=backend,
            results=results,
            suggestions=suggestions,
            ok=bool(ok),
            state="ok" if results else ("empty" if ok else "error"),
            elapsed_ms=elapsed_ms,
            unresponsive_engines=unresponsive,
        )
    if backend == "tavily" and isinstance(value, tuple) and len(value) == 2:
        results, ok = value
        return _BackendOutcome(
            backend=backend,
            results=results,
            ok=bool(ok),
            state="ok" if results else ("empty" if ok else "error"),
            elapsed_ms=elapsed_ms,
        )
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


def _fallback_reason(searxng: _BackendOutcome, result_count: int, threshold: int) -> str:
    if searxng.state == "degraded":
        return "searxng_degraded"
    if result_count and result_count < threshold:
        return "below_minimum"
    return {
        "empty": "searxng_empty",
        "timeout": "searxng_timeout",
        "circuit_open": "searxng_circuit_open",
    }.get(searxng.state, "searxng_error")


def _result_backend(results: list[dict[str, Any]]) -> str:
    has_tavily = any(result.get("engine") == "tavily" for result in results)
    has_searxng = any(result.get("engine") != "tavily" for result in results)
    if has_tavily and has_searxng:
        return "searxng+tavily"
    if has_tavily:
        return "tavily"
    if has_searxng:
        return "searxng"
    return "none"


def _timings(started: float, searxng: _BackendOutcome, tavily: _BackendOutcome | None) -> dict[str, float | None]:
    return {
        "total": round((time.monotonic() - started) * 1000, 1),
        "searxng": searxng.elapsed_ms,
        "tavily": tavily.elapsed_ms if tavily else None,
    }


async def _web_search_impl(query: str, num_results: int = 8) -> str:
    """Execute one web search using the public tool's stable payload contract."""
    started = time.monotonic()
    query = query.strip()
    requested = min(MAX_NUM_RESULTS, max(1, int(num_results)))
    if not query:
        timings_ms = {
            "total": round((time.monotonic() - started) * 1000, 1),
            "searxng": None,
            "tavily": None,
        }
        return _finish_search(
            _search_error_payload(
                query,
                "Search error: query must not be empty.",
                timings_ms=timings_ms,
            ),
            status="error",
            backend="none",
            requested_count=requested,
            result_count=0,
            timings_ms=timings_ms,
        )
    if len(query) > MAX_QUERY_CHARS:
        timings_ms = {
            "total": round((time.monotonic() - started) * 1000, 1),
            "searxng": None,
            "tavily": None,
        }
        return _finish_search(
            _search_error_payload(
                "",
                f"Search error: query must be at most {MAX_QUERY_CHARS} characters.",
                timings_ms=timings_ms,
            ),
            status="error",
            backend="none",
            requested_count=requested,
            result_count=0,
            timings_ms=timings_ms,
        )

    candidate_limit = min(MAX_NUM_RESULTS, requested * RESULT_OVERFETCH_FACTOR)
    attempted = ["searxng"]
    searxng = await _run_backend(
        "searxng",
        _searxng_search(query, candidate_limit),
        min(SEARCH_TIMEOUT, SEARCH_TOTAL_TIMEOUT),
    )
    searxng_candidates = _dedupe_and_rank(searxng.results, candidate_limit)
    threshold = min(SUPPLEMENT_MIN_RESULTS, requested)
    provider_states = {"searxng": searxng.state}
    should_use_tavily = (
        TAVILY_MODE == "fallback" and not searxng_candidates
    ) or (
        TAVILY_MODE == "supplement" and len(searxng_candidates) < threshold
    )

    if not should_use_tavily:
        results = _dedupe_and_rank(searxng_candidates, requested)
        timings_ms = _timings(started, searxng, None)
        fallback_reason: str | None = None
        backend = "none"
        if results:
            status = "degraded" if searxng.state == "degraded" else "ok"
            backend = "searxng"
            rendered = _format_results(
                query,
                results,
                searxng.suggestions,
                status=status,
                backend=backend,
                attempted=attempted,
                timings_ms=timings_ms,
                unresponsive_engines=searxng.unresponsive_engines,
                provider_states=provider_states,
            )
        elif searxng.ok:
            degraded = searxng.state == "degraded"
            status = "degraded" if degraded else "empty"
            fallback_reason = "searxng_degraded" if degraded else None
            rendered = _format_results(
                query,
                [],
                searxng.suggestions,
                status=status,
                backend=backend,
                attempted=attempted,
                fallback_reason=fallback_reason,
                timings_ms=timings_ms,
                unresponsive_engines=searxng.unresponsive_engines,
                provider_states=provider_states,
            )
        else:
            status = "error"
            fallback_reason = _fallback_reason(searxng, 0, threshold)
            rendered = _search_error_payload(
                query,
                "Search error: SearXNG failed and Tavily is disabled by policy.",
                searxng.suggestions,
                attempted=attempted,
                fallback_reason=fallback_reason,
                timings_ms=timings_ms,
                unresponsive_engines=searxng.unresponsive_engines,
                provider_states=provider_states,
            )
        return _finish_search(
            rendered,
            status=status,
            backend=backend,
            requested_count=requested,
            result_count=len(results),
            fallback_reason=fallback_reason,
            timings_ms=timings_ms,
            searxng=searxng,
        )

    attempted.append("tavily")
    reason = _fallback_reason(searxng, len(searxng_candidates), threshold)
    remaining = SEARCH_TOTAL_TIMEOUT - (time.monotonic() - started)
    if remaining <= 0:
        circuit = _breaker.snapshot("tavily")
        tavily = _BackendOutcome(
            backend="tavily",
            state="timeout",
            error="total deadline exceeded",
            circuit_before=str(circuit["state"]),
            circuit_after=str(circuit["state"]),
            circuit_failures=int(circuit["consecutive_failures"]),
        )
    else:
        # Tavily credentials are resolved only when policy permits external egress.
        tavily_key = _resolve_tavily_key()
        tavily = await _run_backend(
            "tavily",
            _tavily_search(query, candidate_limit, tavily_key),
            min(TAVILY_TIMEOUT, remaining),
        )
        if tavily.credential_mode == "none":
            tavily.credential_mode = "keyed" if tavily_key else "keyless"
    provider_states["tavily"] = tavily.state

    if TAVILY_MODE == "supplement" or (
        TAVILY_MODE == "fallback" and searxng.state == "degraded"
    ):
        results = _merge_with_secondary_reserve(searxng_candidates, tavily.results, requested)
    else:
        results = _dedupe_and_rank(tavily.results, requested)
    backend = _result_backend(results)
    timings_ms = _timings(started, searxng, tavily)
    if results:
        status = "degraded"
        rendered = _format_results(
            query,
            results,
            searxng.suggestions,
            status=status,
            backend=backend,
            attempted=attempted,
            fallback_reason=reason,
            timings_ms=timings_ms,
            unresponsive_engines=searxng.unresponsive_engines,
            provider_states=provider_states,
        )
    elif not searxng.ok and not tavily.ok:
        status = "error"
        rendered = _search_error_payload(
            query,
            "Search error: SearXNG unreachable and Tavily failed.",
            searxng.suggestions,
            attempted=attempted,
            fallback_reason=reason,
            timings_ms=timings_ms,
            unresponsive_engines=searxng.unresponsive_engines,
            provider_states=provider_states,
        )
    else:
        status = "empty" if searxng.ok and tavily.ok else "degraded"
        backend = "none"
        rendered = _format_results(
            query,
            [],
            searxng.suggestions,
            status=status,
            backend=backend,
            attempted=attempted,
            fallback_reason=reason,
            timings_ms=timings_ms,
            unresponsive_engines=searxng.unresponsive_engines,
            provider_states=provider_states,
        )
    return _finish_search(
        rendered,
        status=status,
        backend=backend,
        requested_count=requested,
        result_count=len(results),
        fallback_reason=reason,
        timings_ms=timings_ms,
        searxng=searxng,
        tavily=tavily,
    )


@mcp.tool()
async def web_search(query: str, num_results: int = 8) -> str:
    """Search SearXNG first, then apply the configured Tavily egress policy."""
    return await _web_search_impl(query, num_results)


def _batch_error_payload(message: str, started: float, requested: int) -> str:
    timings_ms = {"total": round((time.monotonic() - started) * 1000, 1)}
    _record_search_telemetry(
        status="error",
        backend="none",
        requested_count=requested,
        result_count=0,
        fallback_reason=None,
        total_latency_ms=float(timings_ms["total"]),
        searxng=None,
        tavily=None,
    )
    return json.dumps(
        {
            "status": "error",
            "error": message,
            "query_count": 0,
            "duplicates_ignored": 0,
            "results": [],
            "timings_ms": timings_ms,
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
        searxng=None,
        tavily=None,
    )
    return {
        "query": query,
        "results": [],
        "suggestions": [],
        "status": "timeout",
        "backend": "none",
        "attempted": [],
        "fallback_reason": "batch_deadline",
        "timings_ms": {"total": elapsed_ms, "searxng": None, "tavily": None},
        "mode": TAVILY_MODE,
        "unresponsive_engines": [],
        "provider_states": {},
        "error": "Search error: batch deadline exceeded.",
    }


@mcp.tool()
async def batch_web_search(queries: BatchQueries, num_results: int = 8) -> str:
    """Run up to three deduplicated searches concurrently under one total deadline."""
    started = time.monotonic()
    requested = min(MAX_NUM_RESULTS, max(1, int(num_results)))
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
                return _compact_batch_item(await _web_search_impl(query, requested))
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
                searxng=None,
                tavily=None,
            )
            return {
                "query": query,
                "results": [],
                "suggestions": [],
                "status": "error",
                "backend": "none",
                "attempted": [],
                "fallback_reason": None,
                "timings_ms": {"total": elapsed_ms, "searxng": None, "tavily": None},
                "mode": TAVILY_MODE,
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
        }
    )


@mcp.tool()
async def image_search(query: str, num_results: int = 8) -> str:
    """Search public image metadata via loopback SearXNG with moderate SafeSearch."""
    started = time.monotonic()
    query = query.strip()
    requested = min(MAX_NUM_RESULTS, max(1, int(num_results)))
    if not query:
        timings_ms = {
            "total": round((time.monotonic() - started) * 1000, 1),
            "searxng": None,
            "tavily": None,
        }
        return _finish_search(
            _search_error_payload(
                query,
                "Image search error: query must not be empty.",
                timings_ms=timings_ms,
                mode="disabled",
            ),
            status="error",
            backend="none",
            requested_count=requested,
            result_count=0,
            timings_ms=timings_ms,
            mode="disabled",
        )
    if len(query) > MAX_QUERY_CHARS:
        timings_ms = {
            "total": round((time.monotonic() - started) * 1000, 1),
            "searxng": None,
            "tavily": None,
        }
        return _finish_search(
            _search_error_payload(
                "",
                f"Image search error: query must be at most {MAX_QUERY_CHARS} characters.",
                timings_ms=timings_ms,
                mode="disabled",
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
    searxng = await _run_backend(
        "searxng",
        _searxng_image_search(query, candidate_limit),
        min(SEARCH_TIMEOUT, SEARCH_TOTAL_TIMEOUT),
    )
    results = _dedupe_and_rank_images(searxng.results, requested)
    timings_ms = _timings(started, searxng, None)
    provider_states = {"searxng": searxng.state}
    fallback_reason: str | None = None

    if results:
        status = "degraded" if searxng.state == "degraded" else "ok"
        backend = "searxng"
        rendered = _format_image_results(
            query,
            results,
            searxng.suggestions,
            status=status,
            backend=backend,
            fallback_reason=None,
            timings_ms=timings_ms,
            unresponsive_engines=searxng.unresponsive_engines,
            provider_states=provider_states,
        )
    elif searxng.ok:
        status = "degraded" if searxng.state == "degraded" else "empty"
        backend = "none"
        fallback_reason = "searxng_degraded" if status == "degraded" else None
        rendered = _format_image_results(
            query,
            [],
            searxng.suggestions,
            status=status,
            backend=backend,
            fallback_reason=fallback_reason,
            timings_ms=timings_ms,
            unresponsive_engines=searxng.unresponsive_engines,
            provider_states=provider_states,
        )
    else:
        status = "error"
        backend = "none"
        fallback_reason = _fallback_reason(searxng, 0, requested)
        message = (
            "Image search error: loopback SearXNG is required."
            if searxng.error == "SearXNG URL is not loopback"
            else "Image search error: loopback SearXNG failed."
        )
        rendered = _search_error_payload(
            query,
            message,
            searxng.suggestions,
            attempted=["searxng"],
            fallback_reason=fallback_reason,
            timings_ms=timings_ms,
            unresponsive_engines=searxng.unresponsive_engines,
            provider_states=provider_states,
            mode="disabled",
        )

    return _finish_search(
        rendered,
        status=status,
        backend=backend,
        requested_count=requested,
        result_count=len(results),
        fallback_reason=fallback_reason,
        timings_ms=timings_ms,
        searxng=searxng,
        mode="disabled",
    )


async def _fetch_public_body(url: str) -> tuple[str, bytes, str]:
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
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    "Accept": "text/html,application/xhtml+xml,application/pdf,application/json,text/plain,*/*",
                    "Accept-Encoding": "identity",
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
                    resp.raise_for_status()
                    content_encoding = resp.headers.get("content-encoding", "").lower().strip()
                    if content_encoding not in {"", "identity"}:
                        raise ValueError(f"unsupported encoded response: {content_encoding}")
                    content_length = resp.headers.get("content-length")
                    if content_length and content_length.isdigit() and int(content_length) > FETCH_MAX_BYTES:
                        raise ValueError(f"response exceeds {FETCH_MAX_BYTES} byte limit")
                    chunks = bytearray()
                    async for chunk in resp.aiter_raw():
                        if len(chunks) + len(chunk) > FETCH_MAX_BYTES:
                            raise ValueError(f"response exceeds {FETCH_MAX_BYTES} byte limit")
                        chunks.extend(chunk)
                    return current_url, bytes(chunks), resp.headers.get("content-type", "").lower()
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()
        current_url = redirect_url or current_url
    raise ValueError("too many redirects")


@mcp.tool()
async def web_fetch(url: str, max_chars: int = 20000) -> str:
    """Fetch a public URL and return readable text, including bounded PDF OCR on macOS."""
    max_chars = min(50000, max(1, int(max_chars)))
    try:
        current_url, body, content_type = await asyncio.wait_for(
            _fetch_public_body(url),
            timeout=FETCH_TIMEOUT,
        )
    except httpx.HTTPStatusError as e:
        return _fetch_error(f"HTTP {e.response.status_code} from {url}")
    except httpx.RequestError as e:
        return _fetch_error(f"request failed: {e}")
    except asyncio.TimeoutError:
        return _fetch_error(f"request exceeded {FETCH_TIMEOUT:g} second deadline")
    except ValueError as e:
        return _fetch_error(str(e))
    except Exception as e:
        return _fetch_error(f"unexpected error: {e}")

    if "application/pdf" in content_type or body.startswith(b"%PDF-"):
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
            return _fetch_error(f"PDF extraction failed: {exc}")
        if text is None:
            return _fetch_error(method)
        return _pdf_fetch_text(current_url, text, method, max_chars)

    media_type = content_type.split(";", 1)[0].strip()
    textual = (
        "html" in media_type
        or media_type.startswith("text/")
        or "json" in media_type
        or "xml" in media_type
    )
    if not _looks_like_text(body, content_type):
        return _fetch_error(f"unsupported binary content type: {media_type or 'unknown'}")
    if not textual and media_type not in {"", "application/octet-stream"}:
        return _fetch_error(f"unsupported binary content type: {media_type or 'unknown'}")

    decoded = _decode_text_body(body, content_type)
    if "html" in media_type:
        try:
            text = await _extract_html_content_isolated(decoded, current_url, max_chars)
        except asyncio.TimeoutError:
            return _fetch_error(
                f"HTML extraction exceeded {HTML_EXTRACT_TIMEOUT:g} second deadline"
            )
        except _OutputLimitExceeded as exc:
            return _fetch_error(f"HTML extraction failed: {exc}")
        except (OSError, RuntimeError) as exc:
            return _fetch_error(f"HTML extraction failed: {exc}")
        return text

    return _truncate_text(decoded, max_chars, "\n\n... (truncated)")


if __name__ == "__main__":
    # Initialize private SQLite state before accepting stdio tool calls.
    _get_telemetry()
    mcp.run()
