#!/usr/bin/env python3
"""Lightweight MCP server wrapping SearXNG with structured search output."""

import asyncio
import ipaddress
import urllib.parse
import json
import logging
import socket
from html.parser import HTMLParser

import httpx
from fastmcp import FastMCP

import os

logger = logging.getLogger("websearch-mcp")

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888")

mcp = FastMCP(
    "websearch",
    instructions=(
        "Web search and page fetching via local SearXNG. "
        "Use web_search for any query about current events, facts, or information. "
        "Use web_fetch to retrieve the full text content of a specific URL."
    ),
)


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


async def _validate_public_http_url(url: str) -> None:
    """Reject non-public fetch targets, including DNS names resolving private."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only public http(s) URLs can be fetched")
    host = parsed.hostname.strip().lower()
    if host in {"localhost", "local"} or host.endswith(".localhost"):
        raise ValueError("Refusing to fetch local/private URL")

    try:
        ip = ipaddress.ip_address(host)
        addresses = [ip]
    except ValueError:
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError(f"Could not resolve URL host: {exc}") from exc
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]

    for ip in addresses:
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise ValueError("Refusing to fetch local/private URL")


async def _searxng_request(path: str, params: dict, timeout: float = 15.0) -> dict | None:
    """Make a request to SearXNG and return parsed JSON, or None on failure."""
    qs = urllib.parse.urlencode(params)
    url = f"{SEARXNG_URL}{path}?{qs}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        logger.error(f"Cannot connect to SearXNG at {SEARXNG_URL}")
        return None
    except httpx.HTTPStatusError as e:
        logger.error(f"SearXNG returned HTTP {e.response.status_code}")
        return None
    except Exception as e:
        logger.error(f"SearXNG request error: {e}")
        return None


@mcp.tool()
async def web_search(query: str, num_results: int = 8) -> str:
    """Search the web via local SearXNG. Use this for ANY question about current events, news, facts, people, places, or any topic that requires up-to-date information. Returns ranked results with titles, URLs, and snippets."""
    data = await _searxng_request(
        "/search",
        {"q": query, "format": "json", "categories": "general"},
    )
    if data is None:
        return json.dumps({
            "query": query,
            "results": [],
            "suggestions": [],
            "text": (
                f"Search error: Could not reach SearXNG at {SEARXNG_URL}. "
                "The SearXNG service may be down. Ask the user to check if it's running."
            ),
        })

    results = data.get("results", [])[:num_results]
    suggestions = data.get("suggestions", [])

    # Check if engines returned errors (SearXNG can return results even when
    # all engines fail — the results list will just be empty)
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
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            for _ in range(6):
                resp = await client.get(current_url, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*",
                })
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        return "Fetch error: redirect response missing Location header"
                    current_url = str(resp.url.join(location))
                    await _validate_public_http_url(current_url)
                    continue
                resp.raise_for_status()
                body = resp.text
                content_type = resp.headers.get("content-type", "")
                break
            else:
                return "Fetch error: too many redirects"
    except Exception as e:
        return f"Fetch error: {e}"

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
