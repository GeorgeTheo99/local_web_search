#!/usr/bin/env python3
"""Lightweight MCP server wrapping SearXNG with structured search output.

Exposes two MCP tools:
  - web_search(query, num_results=8)
  - web_fetch(url, max_chars=20000)

And one auxiliary HTTP route (only served by the HTTP transport):
  - GET /health  -> {"status": "ok|degraded|down", ...}

Configuration via environment:
  SEARXNG_URL   default http://localhost:8888
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
from html.parser import HTMLParser
from typing import Any

import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("websearch-mcp")

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888").rstrip("/")

# Tunables (env-overridable for ops).
SEARCH_TIMEOUT = float(os.environ.get("WEBSEARCH_SEARCH_TIMEOUT", "15"))
FETCH_TIMEOUT = float(os.environ.get("WEBSEARCH_FETCH_TIMEOUT", "30"))
FETCH_MAX_REDIRECTS = int(os.environ.get("WEBSEARCH_FETCH_MAX_REDIRECTS", "6"))
SEARCH_MAX_RETRIES = int(os.environ.get("WEBSEARCH_SEARCH_MAX_RETRIES", "1"))
SEARCH_RETRY_BACKOFF = float(os.environ.get("WEBSEARCH_SEARCH_RETRY_BACKOFF", "1.0"))

# A single shared client keeps connection pooling across tool calls.
_http_client: httpx.AsyncClient | None = None


async def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=FETCH_TIMEOUT)
    return _http_client


mcp = FastMCP(
    "websearch",
    instructions=(
        "Web search and page fetching via local SearXNG. "
        "Use web_search for any question about current events, news, facts, people, places, or any topic that requires up-to-date information. "
        "Use web_fetch to retrieve the full text content of a specific URL."
    ),
)


# --------------------------------------------------------------------------- #
# Error payload helpers — consistent shape across all tool failures.
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
    # web_fetch returns plain text by contract; keep the historical prefix so
    # callers and tests can pattern-match reliably.
    return f"Fetch error: {message}"


# --------------------------------------------------------------------------- #
# Result formatting.
# --------------------------------------------------------------------------- #

def _format_results(query: str, results: list[dict], suggestions: list[str]) -> str:
    """Format SearXNG results as structured JSON plus model-friendly text."""
    if not results:
        return json.dumps({
            "query": query,
            "results": [],
            "suggestions": suggestions[:5],
            "text": f"No results found for: {query}",
        })
    lines = [f"## Search: {query}\n"]
    structured_results: list[dict[str, object]] = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        snippet = r.get("content", "").strip()
        engine = r.get("engine")
        domain = ""
        if url:
            try:
                domain = urllib.parse.urlparse(url).netloc
            except Exception:
                pass
        structured_results.append({
            "rank": i,
            "title": title,
            "url": url,
            "domain": domain,
            "snippet": snippet,
            "engine": engine if isinstance(engine, str) else None,
        })
        lines.append(f"{i}. **{title}** — {domain}")
        if snippet:
            lines.append(f"   {snippet}")
        lines.append(f"   {url}")
        lines.append("")
    if suggestions:
        lines.append(f"Related: {', '.join(suggestions[:5])}")
    return json.dumps({
        "query": query,
        "results": structured_results,
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
    """Resolve a hostname to IP addresses. Raises on resolution failure."""
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
    """Reject non-public fetch targets, including DNS names resolving private."""
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
# SearXNG transport with retry + structured logging.
# --------------------------------------------------------------------------- #

async def _searxng_request(path: str, params: dict, timeout: float | None = None) -> dict | None:
    """GET a JSON path from SearXNG with one retry. Returns parsed JSON or None.

    None means the backend could not be reached or returned a non-2xx status;
    callers should treat that as a backend outage, not an empty result set.
    """
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
        except Exception as e:  # JSON decode, timeout, etc.
            last_err = e
            logger.error("SearXNG request error (attempt %d): %s", attempt + 1, e)
        if attempt < SEARCH_MAX_RETRIES:
            await asyncio.sleep(SEARCH_RETRY_BACKOFF)
    if last_err:
        logger.error("SearXNG request failed after %d attempts: %s",
                     SEARCH_MAX_RETRIES + 1, last_err)
    return None


# --------------------------------------------------------------------------- #
# Health probe (used by /health route and by verify tooling).
# --------------------------------------------------------------------------- #

async def _probe_searxng() -> dict[str, Any]:
    """Lightweight SearXNG liveness probe. Never raises."""
    started = time.monotonic()
    try:
        client = await _client()
        resp = await client.get(f"{SEARXNG_URL}/healthz", timeout=3.0)
        ok = resp.status_code == 200
        return {
            "reachable": ok,
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
    """Health endpoint: reports MCP liveness + SearXNG backend reachability.

    Status:
      ok        — MCP up and SearXNG reachable
      degraded  — MCP up but SearXNG unreachable (search will fail)
    Always returns 200 so naive uptime probes don't false-alarm; callers
    inspecting status should branch on the `status` field.
    """
    searxng = await _probe_searxng()
    status = "ok" if searxng.get("reachable") else "degraded"
    return JSONResponse({
        "status": status,
        "service": "mcp-websearch",
        "searxng_url": SEARXNG_URL,
        "searxng": searxng,
    })


# --------------------------------------------------------------------------- #
# MCP tools.
# --------------------------------------------------------------------------- #

@mcp.tool()
async def web_search(query: str, num_results: int = 8) -> str:
    """Search the web via local SearXNG. Use this for ANY question about current events, news, facts, people, places, or any topic that requires up-to-date information. Returns ranked results with titles, URLs, and snippets."""
    data = await _searxng_request(
        "/search",
        {"q": query, "format": "json", "categories": "general"},
    )
    if data is None:
        return _search_error_payload(
            query,
            f"Search error: Could not reach SearXNG at {SEARXNG_URL}. "
            "The SearXNG service may be down. Ask the user to check if it's running.",
        )

    results = data.get("results", [])[:num_results]
    suggestions = data.get("suggestions", [])

    # SearXNG can return an empty results list when all engines fail; surface
    # the unresponsive engines so the caller can diagnose.
    if not results:
        unresponsive_engines = data.get("unresponsive_engines", [])
        engine_msg = ""
        if unresponsive_engines:
            names = [e.get("name", str(e)) if isinstance(e, dict) else str(e)
                     for e in unresponsive_engines]
            engine_msg = f" (unresponsive engines: {', '.join(names)})"
        return json.dumps({
            "query": query,
            "results": [],
            "suggestions": suggestions[:5],
            "text": f"No results found for: {query}{engine_msg}",
        })

    return _format_results(query, results, suggestions)


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
