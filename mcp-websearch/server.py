#!/usr/bin/env python3
"""Lightweight MCP server wrapping SearXNG + Tavily failover.

Exposes two MCP tools:
  - web_search(query, num_results=8)   SearXNG first, Tavily failover
  - web_fetch(url, max_chars=20000)    direct fetch with SSRF guard

And one auxiliary HTTP route (HTTP transport only):
  - GET /health  -> {"status": "ok|degraded|down", ...}

Search strategy (SearXNG-first, credit-conserving):
  1. Query SearXNG (free, always-on).
  2. If SearXNG returns results → return them.
  3. If SearXNG returns empty OR errors AND a Tavily key is available →
     query Tavily and return its results.
  4. If no Tavily key → return the SearXNG empty/error payload (free default).
  5. Merge + dedupe by URL across backends; per-backend circuit breaker trips
     after N consecutive failures (skips that backend for a cooldown).

Tavily key sources (precedence: per-call header > env var):
  - MCP Authorization: Bearer <key>  (Home Server McpSearchProvider forwards it)
  - TAVILY_API_KEY env var           (pi-shared/agent stdio consumers)

Configuration via environment:
  SEARXNG_URL   default http://localhost:8888
  TAVILY_API_KEY  optional, fallback when no per-call key is sent
  TAVILY_BASE_URL  default https://api.tavily.com
  MCP_PORT      default 8889 (HTTP transport only)
  LOG_LEVEL     default INFO
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
from collections.abc import Iterable
from html.parser import HTMLParser
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("websearch-mcp")

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888").rstrip("/")
TAVILY_BASE_URL = os.environ.get("TAVILY_BASE_URL", "https://api.tavily.com").rstrip("/")
# Env fallback key; per-call Authorization: Bearer header takes precedence.
TAVILY_API_KEY_ENV = os.environ.get("TAVILY_API_KEY", "").strip()

# Tunables.
SEARCH_TIMEOUT = float(os.environ.get("WEBSEARCH_SEARCH_TIMEOUT", "15"))
FETCH_TIMEOUT = float(os.environ.get("WEBSEARCH_FETCH_TIMEOUT", "30"))
FETCH_MAX_REDIRECTS = int(os.environ.get("WEBSEARCH_FETCH_MAX_REDIRECTS", "6"))
SEARCH_MAX_RETRIES = int(os.environ.get("WEBSEARCH_SEARCH_MAX_RETRIES", "1"))
SEARCH_RETRY_BACKOFF = float(os.environ.get("WEBSEARCH_SEARCH_RETRY_BACKOFF", "1.0"))
TAVILY_TIMEOUT = float(os.environ.get("WEBSEARCH_TAVILY_TIMEOUT", "15"))

# Circuit breaker: trip after this many consecutive failures, skip for cooldown.
BREAKER_FAIL_THRESHOLD = int(os.environ.get("WEBSEARCH_BREAKER_FAIL_THRESHOLD", "3"))
BREAKER_COOLDOWN = float(os.environ.get("WEBSEARCH_BREAKER_COOLDOWN", "60"))

_http_client: httpx.AsyncClient | None = None


async def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=FETCH_TIMEOUT)
    return _http_client


mcp = FastMCP(
    "websearch",
    instructions=(
        "Web search and page fetching via local SearXNG with Tavily failover. "
        "Use web_search for any question about current events, news, facts, people, places, or any topic that requires up-to-date information. "
        "Use web_fetch to retrieve the full text content of a specific URL."
    ),
)


# --------------------------------------------------------------------------- #
# Circuit breaker (per backend).
# --------------------------------------------------------------------------- #

class _CircuitBreaker:
    """Tracks consecutive failures for a named backend. Trips after threshold."""

    def __init__(self) -> None:
        self._fails: dict[str, int] = {}
        self._tripped: dict[str, float] = {}  # backend -> tripped-at monotonic

    def allow(self, backend: str) -> bool:
        tripped_at = self._tripped.get(backend)
        if tripped_at is None:
            return True
        if time.monotonic() - tripped_at < BREAKER_COOLDOWN:
            return False
        # Cooldown elapsed — half-open: allow one attempt.
        self._tripped.pop(backend, None)
        self._fails[backend] = 0
        return True

    def record_success(self, backend: str) -> None:
        self._fails.pop(backend, None)
        self._tripped.pop(backend, None)

    def record_failure(self, backend: str) -> None:
        fails = self._fails.get(backend, 0) + 1
        self._fails[backend] = fails
        if fails >= BREAKER_FAIL_THRESHOLD:
            self._tripped[backend] = time.monotonic()
            logger.warning("Circuit breaker tripped for %s after %d failures (cooldown %ds)",
                           backend, fails, int(BREAKER_COOLDOWN))


_breaker = _CircuitBreaker()


# --------------------------------------------------------------------------- #
# Error / payload helpers.
# --------------------------------------------------------------------------- #

def _search_error_payload(query: str, message: str, suggestions: list[str] | None = None) -> str:
    return json.dumps({
        "query": query,
        "results": [],
        "suggestions": suggestions or [],
        "error": message,
        "text": message,
    })


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


def _dedupe_and_rank(results: list[dict[str, Any]], num_results: int) -> list[dict[str, Any]]:
    """Dedupe by normalized URL (first wins, preserving SearXNG precedence),
    then assign rank."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for r in results:
        key = (r.get("url") or "").split("#", 1)[0].rstrip("/").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(r)
        if len(unique) >= num_results:
            break
    for i, r in enumerate(unique, 1):
        r["rank"] = i
    return unique


def _format_results(query: str, results: list[dict[str, Any]], suggestions: list[str]) -> str:
    """Format aggregated results as structured JSON plus model-friendly text."""
    if not results:
        return json.dumps({
            "query": query,
            "results": [],
            "suggestions": suggestions[:5],
            "text": f"No results found for: {query}",
        })
    lines = [f"## Search: {query}\n"]
    for r in results:
        title = r.get("title", "Untitled")
        domain = r.get("domain", "")
        snippet = (r.get("snippet") or "").strip()
        lines.append(f"{r['rank']}. **{title}** — {domain}")
        if snippet:
            lines.append(f"   {snippet}")
        lines.append(f"   {r.get('url', '')}")
        lines.append("")
    if suggestions:
        lines.append(f"Related: {', '.join(suggestions[:5])}")
    return json.dumps({
        "query": query,
        "results": results,
        "suggestions": suggestions[:5],
        "text": "\n".join(lines),
    })


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

    Header precedence (custom headers pass through to tools; the standard
    `Authorization` header is consumed by the MCP transport and does NOT reach
    tool code, so we use a custom header that Home Server's McpSearchProvider
    sends alongside its Authorization header):
      1. X-Tavily-Key   (preferred, custom)
      2. X-Api-Key      (generic fallback)
    Then env var TAVILY_API_KEY (pi-shared/agent stdio consumers).
    """
    try:
        headers = get_http_headers()
    except LookupError:
        # stdio transport / no HTTP context — env var only.
        return TAVILY_API_KEY_ENV
    for name in ("x-tavily-key", "x-api-key"):
        value = headers.get(name)
        if value and value.strip():
            return value.strip()
    return TAVILY_API_KEY_ENV


# --------------------------------------------------------------------------- #
# Backend: SearXNG.
# --------------------------------------------------------------------------- #

async def _searxng_request(path: str, params: dict, timeout: float | None = None) -> dict | None:
    """GET a JSON path from SearXNG with retry. Returns parsed JSON or None."""
    timeout = SEARCH_TIMEOUT if timeout is None else timeout
    qs = urllib.parse.urlencode(params)
    url = f"{SEARXNG_URL}{path}?{qs}"
    client = await _client()
    last_err: Exception | None = None
    for attempt in range(SEARCH_MAX_RETRIES + 1):
        try:
            resp = await client.get(url, headers={"Accept": "application/json"}, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except httpx.ConnectError as e:
            last_err = e
            logger.error("SearXNG connect error (attempt %d): %s", attempt + 1, e)
        except httpx.HTTPStatusError as e:
            last_err = e
            logger.error("SearXNG HTTP %d (attempt %d)", e.response.status_code, attempt + 1)
        except Exception as e:
            last_err = e
            logger.error("SearXNG request error (attempt %d): %s", attempt + 1, e)
        if attempt < SEARCH_MAX_RETRIES:
            await asyncio.sleep(SEARCH_RETRY_BACKOFF)
    if last_err:
        logger.error("SearXNG request failed after %d attempts: %s",
                     SEARCH_MAX_RETRIES + 1, last_err)
    return None


async def _searxng_search(query: str, num_results: int) -> tuple[list[dict], list[str], bool, list[str]]:
    """Return (normalized_results, suggestions, ok, unresponsive_engines).
    ok=False means backend failure (not just empty results)."""
    if not _breaker.allow("searxng"):
        logger.info("SearXNG circuit open; skipping")
        return [], [], False, []
    data = await _searxng_request("/search", {"q": query, "format": "json", "categories": "general"})
    if data is None:
        _breaker.record_failure("searxng")
        return [], [], False, []
    _breaker.record_success("searxng")
    raw = data.get("results", [])[:num_results]
    results = [_normalize_searxng_result(r) for r in raw]
    suggestions = data.get("suggestions", [])
    unresponsive = data.get("unresponsive_engines", [])
    return results, suggestions, True, unresponsive


# --------------------------------------------------------------------------- #
# Backend: Tavily.
# --------------------------------------------------------------------------- #

async def _tavily_search(query: str, num_results: int, api_key: str) -> tuple[list[dict], bool]:
    """Return (normalized_results, ok). ok=False means backend failure."""
    if not _breaker.allow("tavily"):
        logger.info("Tavily circuit open; skipping")
        return [], False
    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": num_results,
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
        "topic": "general",
    }
    try:
        client = await _client()
        resp = await client.post(f"{TAVILY_BASE_URL}/search", json=payload, timeout=TAVILY_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("Tavily search failed: %s: %s", type(e).__name__, e)
        _breaker.record_failure("tavily")
        return [], False
    _breaker.record_success("tavily")
    raw = data.get("results", [])[:num_results]
    results = [_normalize_tavily_result(r) for r in raw if isinstance(r, dict)]
    return results, True


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


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Liveness + SearXNG reachability. Always 200; inspect `status`."""
    searxng = await _probe_searxng()
    status = "ok" if searxng.get("reachable") else "degraded"
    tavily_configured = bool(TAVILY_API_KEY_ENV)
    return JSONResponse({
        "status": status,
        "service": "mcp-websearch",
        "searxng_url": SEARXNG_URL,
        "searxng": searxng,
        "tavily": {
            "env_key_configured": tavily_configured,
            "base_url": TAVILY_BASE_URL,
        },
    })


# --------------------------------------------------------------------------- #
# MCP tools.
# --------------------------------------------------------------------------- #

@mcp.tool()
async def web_search(query: str, num_results: int = 8) -> str:
    """Search the web via local SearXNG with Tavily failover. Use this for ANY question about current events, news, facts, people, places, or any topic that requires up-to-date information. Returns ranked results with titles, URLs, and snippets.

    Strategy: SearXNG first (free); if SearXNG returns no results or errors and
    a Tavily key is available (per-call Authorization header or TAVILY_API_KEY
    env var), Tavily is queried as failover. Results are deduped by URL."""
    tavily_key = _resolve_tavily_key()

    # Tier 1: SearXNG.
    searxng_results, suggestions, searxng_ok, unresponsive = await _searxng_search(query, num_results)

    if searxng_results:
        # SearXNG returned results — return them (no Tavily spend).
        ranked = _dedupe_and_rank(searxng_results, num_results)
        return _format_results(query, ranked, suggestions)

    # SearXNG empty or failed — attempt Tavily failover if a key is available.
    if not tavily_key:
        # No key: return the SearXNG outcome (free default). Distinguish
        # backend-failure from genuine empty results for the caller.
        if not searxng_ok:
            return _search_error_payload(
                query,
                f"Search error: SearXNG unreachable at {SEARXNG_URL} and no Tavily key configured. "
                "Check that SearXNG is running or set a Tavily API key.",
            )
        # SearXNG reachable but empty — surface unresponsive engines if any.
        engine_msg = ""
        if unresponsive:
            names = [e.get("name", str(e)) if isinstance(e, dict) else str(e)
                     for e in unresponsive]
            engine_msg = f" (unresponsive engines: {', '.join(names)})"
        return json.dumps({
            "query": query,
            "results": [],
            "suggestions": suggestions[:5],
            "text": f"No results found for: {query}{engine_msg}",
        })

    if not _breaker.allow("tavily"):
        # Tavily breaker open — return SearXNG's empty/error result.
        return _format_results(query, [], suggestions) if searxng_ok else _search_error_payload(
            query, f"Search error: SearXNG unreachable and Tavily circuit open. Try again shortly.")

    tavily_results, tavily_ok = await _tavily_search(query, num_results, tavily_key)

    if tavily_results:
        ranked = _dedupe_and_rank(tavily_results, num_results)
        return _format_results(query, ranked, [])

    # Both backends returned nothing usable.
    if not searxng_ok and not tavily_ok:
        return _search_error_payload(
            query,
            f"Search error: SearXNG unreachable and Tavily failed. Check services and Tavily key.",
        )
    # Both reachable but empty.
    return _format_results(query, [], suggestions)


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
