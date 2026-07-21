#!/usr/bin/env python3
"""Synthetic official-domain retrieval smoke for SearXNG, Tavily, and Perplexity.

This calls provider backends directly, bypassing production telemetry. Queries
are public and synthetic. Set PERPLEXITY_API_KEY to include Perplexity Search;
TAVILY_API_KEY is optional (otherwise Tavily keyless is used).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

MCP_DIR = Path(__file__).resolve().parents[1] / "mcp-websearch"
sys.path.insert(0, str(MCP_DIR))
import server as srv  # noqa: E402

CASES = [
    ("Python packaging installing packages tutorial", "packaging.python.org"),
    ("RFC 9110 HTTP Semantics official", "rfc-editor.org"),
    ("SearXNG why use a private instance documentation", "docs.searxng.org"),
    ("Tavily API credits pricing documentation", "docs.tavily.com"),
    ("Perplexity Search API pricing documentation", "docs.perplexity.ai"),
    ("Caddy reverse_proxy directive documentation", "caddyserver.com"),
    ("uv sync command documentation", "docs.astral.sh"),
    ("SQLite write-ahead logging documentation", "sqlite.org"),
    ("FastMCP tools documentation", "gofastmcp.com"),
    ("GitHub Actions macOS runner documentation", "docs.github.com"),
    ("Python 3.13 what's new documentation", "docs.python.org"),
    ("Apple launchd property list documentation", "developer.apple.com"),
]


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _rank(results: list[dict], expected: str) -> int | None:
    expected = expected.lower().removeprefix("www.")
    for index, item in enumerate(results, 1):
        domain = _host(str(item.get("url", "")))
        if domain == expected or domain.endswith("." + expected):
            return index
    return None


def _record(outcome: srv._BackendOutcome, expected: str) -> dict:
    return {
        "state": outcome.state,
        "latency_ms": outcome.elapsed_ms,
        "result_count": len(outcome.results),
        "rank": _rank(outcome.results, expected),
        "domains": [_host(str(item.get("url", ""))) for item in outcome.results],
        "unresponsive_engines": outcome.unresponsive_engines,
        "error": outcome.error,
        "http_status": outcome.http_status,
        "credential_mode": outcome.credential_mode,
        "circuit_before": outcome.circuit_before,
        "circuit_after": outcome.circuit_after,
        "circuit_transition": outcome.circuit_transition,
        "circuit_failures": outcome.circuit_failures,
    }


async def _perplexity_search(query: str, max_results: int, api_key: str) -> srv._BackendOutcome:
    if not api_key:
        return srv._BackendOutcome(backend="perplexity_search", state="not_configured")
    started = srv.time.monotonic()
    try:
        async with srv.httpx.AsyncClient(timeout=srv.TAVILY_TIMEOUT, trust_env=False) as client:
            async with client.stream(
                "POST",
                "https://api.perplexity.ai/search",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                json={"query": query, "max_results": max_results},
            ) as response:
                response.raise_for_status()
                data = await srv._limited_json_object(response, provider="Perplexity")
        results = []
        for item in data.get("results", []):
            if not isinstance(item, dict) or not (url := srv._public_http_url(item.get("url"))):
                continue
            results.append(
                {
                    "title": str(item.get("title") or "Untitled"),
                    "url": url,
                    "domain": srv._domain(url),
                    "snippet": str(item.get("snippet") or ""),
                    "engine": "perplexity_search",
                    "score": None,
                }
            )
        results = srv._dedupe_and_rank(results, max_results)
        return srv._BackendOutcome(
            backend="perplexity_search",
            results=results,
            ok=True,
            state="ok" if results else "empty",
            elapsed_ms=round((srv.time.monotonic() - started) * 1000, 1),
        )
    except Exception as exc:
        return srv._BackendOutcome(
            backend="perplexity_search",
            state="error",
            error=type(exc).__name__,
            elapsed_ms=round((srv.time.monotonic() - started) * 1000, 1),
        )


def _summary(rows: list[dict], provider: str) -> dict:
    records = [row[provider] for row in rows]
    ranks = [record["rank"] for record in records]
    return {
        "queries": len(records),
        "success_at_5": sum(rank is not None and rank <= 5 for rank in ranks),
        "success_at_10": sum(rank is not None and rank <= 10 for rank in ranks),
        "mrr_at_10": round(sum((1 / rank) if rank else 0 for rank in ranks) / len(ranks), 4),
        "average_latency_ms": round(sum(record["latency_ms"] for record in records) / len(records), 1),
        "degraded_or_error": sum(record["state"] not in {"ok", "empty"} for record in records),
    }


async def main() -> None:
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    perplexity_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    providers = ["searxng", "tavily_basic_keyless" if not tavily_key else "tavily_basic_keyed"]
    if perplexity_key:
        providers.append("perplexity_search")

    rows = []
    for query, expected in CASES:
        outcomes = {
            "searxng": await srv._searxng_search(query, 10),
            providers[1]: await srv._tavily_search(query, 10, tavily_key),
        }
        if perplexity_key:
            outcomes["perplexity_search"] = await _perplexity_search(query, 10, perplexity_key)
        rows.append(
            {
                "query": query,
                "expected_domain": expected,
                **{provider: _record(outcome, expected) for provider, outcome in outcomes.items()},
            }
        )

    print(
        json.dumps(
            {
                "method": "official-domain retrieval, 12 synthetic navigational queries, top 10",
                "providers": providers,
                "summary": {provider: _summary(rows, provider) for provider in providers},
                "rows": rows,
            },
            indent=2,
        )
    )
    if srv._http_client is not None and not srv._http_client.is_closed:
        await srv._http_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
