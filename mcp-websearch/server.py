#!/usr/bin/env python3
"""Policy-aware MCP web search broker for local SearXNG and Tavily.

Tools:
  - web_search(query, num_results=8)  SearXNG first; Tavily policy-controlled
  - web_fetch(url, max_chars=20000)   direct fetch with SSRF guard

HTTP diagnostics:
  - GET /live    dependency-free process liveness
  - GET /ready   provider readiness (503 when no backend is usable)
  - GET /health  compatibility diagnostics (always HTTP 200)

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
  WEBSEARCH_TOTAL_TIMEOUT             capped at 18 seconds
  WEBSEARCH_SEARXNG_TIMEOUT           default 7 seconds
  WEBSEARCH_TAVILY_TIMEOUT            default 8 seconds
  WEBSEARCH_SUPPLEMENT_MIN_RESULTS    default 5
  TAVILY_API_KEY                      optional stdio/server fallback key
  TAVILY_BASE_URL                     default https://api.tavily.com
  MCP_PORT                            default 8889 (HTTP transport only)
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import time
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from threading import Lock
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("websearch-mcp")

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
TAVILY_BASE_URL = os.environ.get("TAVILY_BASE_URL", "https://api.tavily.com").rstrip("/")
# Environment fallback for stdio clients. HTTP clients should forward a Tavily
# key only through X-Tavily-Key; broker authentication is a separate concern.
TAVILY_API_KEY_ENV = os.environ.get("TAVILY_API_KEY", "").strip()


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

# Other tunables.
FETCH_TIMEOUT = _bounded_float("WEBSEARCH_FETCH_TIMEOUT", 30.0, minimum=1.0, maximum=60.0)
FETCH_MAX_REDIRECTS = _bounded_int("WEBSEARCH_FETCH_MAX_REDIRECTS", 6, minimum=0, maximum=12)
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

_http_client: httpx.AsyncClient | None = None


async def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=FETCH_TIMEOUT)
    return _http_client


mcp = FastMCP(
    "websearch",
    instructions=(
        "Web search and page fetching via local SearXNG with policy-controlled Tavily fallback or supplementation. "
        "Use web_search for any question about current events, news, facts, people, places, or any topic that requires up-to-date information. "
        "Use web_fetch to retrieve the full text content of a specific URL."
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

    def record_success(self, backend: str) -> None:
        with self._lock:
            self._fails.pop(backend, None)
            self._tripped.pop(backend, None)
            self._half_open.discard(backend)

    def record_failure(self, backend: str) -> None:
        with self._lock:
            was_half_open = backend in self._half_open
            self._half_open.discard(backend)
            fails = self._fails.get(backend, 0) + 1
            self._fails[backend] = fails
            if was_half_open or fails >= BREAKER_FAIL_THRESHOLD:
                self._tripped[backend] = time.monotonic()
                logger.warning(
                    "Circuit breaker tripped for %s after %d failures (cooldown %ds)",
                    backend,
                    fails,
                    int(BREAKER_COOLDOWN),
                )

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


_last_search: dict[str, Any] | None = None


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
) -> dict[str, Any]:
    return {
        "status": status,
        "backend": backend,
        "attempted": attempted or [],
        "fallback_reason": fallback_reason,
        "timings_ms": timings_ms or {"total": 0.0, "searxng": None, "tavily": None},
        "mode": TAVILY_MODE,
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
        ),
    }
    _record_last_search(payload)
    return json.dumps(payload)


def _fetch_error(message: str) -> str:
    return f"Fetch error: {message}"


# --------------------------------------------------------------------------- #
# Result normalization + dedupe.
# --------------------------------------------------------------------------- #

def _normalize_searxng_result(r: dict) -> dict[str, Any]:
    url = r.get("url", "")
    return {
        "title": r.get("title", "Untitled"),
        "url": url,
        "domain": _domain(url),
        "snippet": (r.get("content") or "").strip(),
        "engine": r.get("engine") if isinstance(r.get("engine"), str) else "searxng",
        "score": None,
    }


def _normalize_tavily_result(r: dict) -> dict[str, Any]:
    url = str(r.get("url") or "")
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
        key = _canonical_result_url(str(result.get("url") or ""))
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


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._chunks: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        self._skip = tag in ("script", "style", "noscript")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self._chunks.append(text)

    def get_text(self) -> str:
        return "\n".join(self._chunks)


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


async def _validate_public_http_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only public http(s) URLs can be fetched")
    host = parsed.hostname.strip().lower()
    if host in {"localhost", "local"} or host.endswith(".localhost"):
        raise ValueError("Refusing to fetch local/private URL")
    addresses = await _resolve_host_ips(host, parsed.port, parsed.scheme)
    for ip in addresses:
        if _is_private_ip(ip):
            raise ValueError("Refusing to fetch local/private URL")


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
    attempts = 0

    for attempt in range(SEARCH_MAX_RETRIES + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _RequestOutcome(None, "timeout", attempts, "stage deadline exceeded")
        attempts = attempt + 1
        retryable = True
        try:
            response = await client.get(
                url,
                headers={"Accept": "application/json"},
                timeout=remaining,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                return _RequestOutcome(None, "error", attempts, "invalid JSON payload")
            return _RequestOutcome(data, "ok", attempts)
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

    return _RequestOutcome(None, last_state, attempts, last_error)


async def _searxng_search(query: str, num_results: int) -> _BackendOutcome:
    started = time.monotonic()
    if not _breaker.allow("searxng"):
        logger.info("SearXNG circuit open; skipping")
        return _BackendOutcome(
            backend="searxng",
            state="circuit_open",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
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
        _breaker.record_failure("searxng")
        return _BackendOutcome(
            backend="searxng",
            state=request.state,
            elapsed_ms=elapsed_ms,
            attempts=request.attempts,
            error=request.error,
        )

    _breaker.record_success("searxng")
    raw_results = request.data.get("results", [])
    if not isinstance(raw_results, list):
        raw_results = []
    results = [_normalize_searxng_result(item) for item in raw_results[:num_results] if isinstance(item, dict)]
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
    )


# --------------------------------------------------------------------------- #
# Backend: Tavily.
# --------------------------------------------------------------------------- #

async def _tavily_search(query: str, num_results: int, api_key: str) -> _BackendOutcome:
    """Search Tavily in keyed or keyless mode without mixing credential roles."""
    started = time.monotonic()
    if not _breaker.allow("tavily"):
        logger.info("Tavily circuit open; skipping")
        return _BackendOutcome(
            backend="tavily",
            state="circuit_open",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
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
        {"Authorization": f"Bearer {api_key}"}
        if api_key
        else {"X-Tavily-Access-Mode": "keyless"}
    )
    try:
        client = await _client()
        response = await client.post(
            f"{TAVILY_BASE_URL}/search",
            json=payload,
            headers=headers,
            timeout=TAVILY_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("invalid JSON payload")
    except asyncio.CancelledError:
        _breaker.record_aborted("tavily")
        raise
    except httpx.TimeoutException:
        _breaker.record_failure("tavily")
        state = "timeout"
        error = "request timed out"
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        # Authentication/client errors prove the provider is reachable and
        # must not globally circuit-break valid keys or keyless traffic.
        if status_code in {400, 401, 403, 404, 422}:
            _breaker.record_success("tavily")
        else:
            _breaker.record_failure("tavily")
        state = "error"
        error = f"HTTP {status_code}"
    except Exception as exc:
        _breaker.record_failure("tavily")
        state = "error"
        error = type(exc).__name__
    else:
        _breaker.record_success("tavily")
        raw_results = data.get("results", [])
        if not isinstance(raw_results, list):
            raw_results = []
        results = [
            _normalize_tavily_result(item)
            for item in raw_results[:num_results]
            if isinstance(item, dict)
        ]
        return _BackendOutcome(
            backend="tavily",
            results=results,
            ok=True,
            state="ok" if results else "empty",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            attempts=1,
        )

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
    try:
        value = await asyncio.wait_for(awaitable, timeout=max(0.05, timeout))
    except asyncio.TimeoutError:
        _breaker.record_failure(backend)
        return _BackendOutcome(
            backend=backend,
            state="timeout",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            error="stage deadline exceeded",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _breaker.record_failure(backend)
        logger.warning("%s search raised %s", backend, type(exc).__name__)
        return _BackendOutcome(
            backend=backend,
            state="error",
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            error=type(exc).__name__,
        )
    return _coerce_backend_outcome(
        value,
        backend,
        round((time.monotonic() - started) * 1000, 1),
    )


def _fallback_reason(searxng: _BackendOutcome, result_count: int, threshold: int) -> str:
    if result_count and result_count < threshold:
        return "below_minimum"
    return {
        "empty": "searxng_empty",
        "degraded": "searxng_degraded",
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


@mcp.tool()
async def web_search(query: str, num_results: int = 8) -> str:
    """Search SearXNG first, then apply the configured Tavily egress policy."""
    started = time.monotonic()
    query = query.strip()
    requested = min(MAX_NUM_RESULTS, max(1, int(num_results)))
    if not query:
        return _search_error_payload(query, "Search error: query must not be empty.")

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
        if results:
            status = "degraded" if searxng.state == "degraded" else "ok"
            return _format_results(
                query,
                results,
                searxng.suggestions,
                status=status,
                backend="searxng",
                attempted=attempted,
                timings_ms=timings_ms,
                unresponsive_engines=searxng.unresponsive_engines,
                provider_states=provider_states,
            )
        if searxng.ok:
            degraded = searxng.state == "degraded"
            return _format_results(
                query,
                [],
                searxng.suggestions,
                status="degraded" if degraded else "empty",
                backend="none",
                attempted=attempted,
                fallback_reason="searxng_degraded" if degraded else None,
                timings_ms=timings_ms,
                unresponsive_engines=searxng.unresponsive_engines,
                provider_states=provider_states,
            )
        return _search_error_payload(
            query,
            "Search error: SearXNG failed and Tavily is disabled by policy.",
            searxng.suggestions,
            attempted=attempted,
            fallback_reason=_fallback_reason(searxng, 0, threshold),
            timings_ms=timings_ms,
            unresponsive_engines=searxng.unresponsive_engines,
            provider_states=provider_states,
        )

    attempted.append("tavily")
    reason = _fallback_reason(searxng, len(searxng_candidates), threshold)
    remaining = SEARCH_TOTAL_TIMEOUT - (time.monotonic() - started)
    if remaining <= 0:
        tavily = _BackendOutcome(backend="tavily", state="timeout", error="total deadline exceeded")
    else:
        # Tavily credentials are resolved only when policy permits external egress.
        tavily = await _run_backend(
            "tavily",
            _tavily_search(query, candidate_limit, _resolve_tavily_key()),
            min(TAVILY_TIMEOUT, remaining),
        )
    provider_states["tavily"] = tavily.state

    combined = (
        searxng_candidates + tavily.results
        if TAVILY_MODE == "supplement"
        else tavily.results
    )
    results = _dedupe_and_rank(combined, requested)
    backend = _result_backend(results)
    timings_ms = _timings(started, searxng, tavily)
    if results:
        return _format_results(
            query,
            results,
            searxng.suggestions,
            status="degraded",
            backend=backend,
            attempted=attempted,
            fallback_reason=reason,
            timings_ms=timings_ms,
            unresponsive_engines=searxng.unresponsive_engines,
            provider_states=provider_states,
        )

    if not searxng.ok and not tavily.ok:
        return _search_error_payload(
            query,
            "Search error: SearXNG unreachable and Tavily failed.",
            searxng.suggestions,
            attempted=attempted,
            fallback_reason=reason,
            timings_ms=timings_ms,
            unresponsive_engines=searxng.unresponsive_engines,
            provider_states=provider_states,
        )

    status = "empty" if searxng.ok and tavily.ok else "degraded"
    return _format_results(
        query,
        [],
        searxng.suggestions,
        status=status,
        backend="none",
        attempted=attempted,
        fallback_reason=reason,
        timings_ms=timings_ms,
        unresponsive_engines=searxng.unresponsive_engines,
        provider_states=provider_states,
    )


@mcp.tool()
async def web_fetch(url: str, max_chars: int = 20000) -> str:
    """Fetch a URL and return its text content. Use this to read the full content of a web page found via web_search, or any URL the user provides."""
    try:
        current_url = url
        await _validate_public_http_url(current_url)
        client = await _client()
        for _ in range(FETCH_MAX_REDIRECTS):
            resp = await client.get(
                current_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*",
                },
                follow_redirects=False,
            )
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    return _fetch_error("redirect response missing Location header")
                current_url = str(resp.url.join(location))
                await _validate_public_http_url(current_url)
                continue
            resp.raise_for_status()
            body = resp.text
            content_type = resp.headers.get("content-type", "")
            break
        else:
            return _fetch_error("too many redirects")
    except httpx.HTTPStatusError as e:
        return _fetch_error(f"HTTP {e.response.status_code} from {current_url}")
    except httpx.RequestError as e:
        return _fetch_error(f"request failed: {e}")
    except ValueError as e:
        return _fetch_error(str(e))
    except Exception as e:
        return _fetch_error(f"unexpected error: {e}")

    if "html" in content_type:
        parser = _TextExtractor()
        parser.feed(body)
        text = parser.get_text()
    else:
        text = body

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... (truncated at {max_chars} chars)"
    return text


if __name__ == "__main__":
    mcp.run()
