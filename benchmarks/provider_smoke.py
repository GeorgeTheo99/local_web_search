#!/usr/bin/env python3
"""Synthetic official-domain retrieval smoke for SearXNG and Brave Search.

This calls provider backends directly, bypassing production telemetry. Queries
are public and synthetic. A Brave key must be available through BRAVE_API_KEY
or the owner-only ``data/brave_key`` file. Running the full suite issues up to
12 billable Brave API requests (currently at most $0.06 total).
"""

from __future__ import annotations

import asyncio
import json
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
    ("Brave Search API pricing documentation", "brave.com"),
    ("Brave Search API web search documentation", "api.search.brave.com"),
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
    brave_key = srv._resolve_brave_key()
    if not brave_key:
        raise SystemExit(
            "Brave API key required via BRAVE_API_KEY or owner-only data/brave_key"
        )
    providers = ["searxng", "brave"]

    rows = []
    for query, expected in CASES:
        outcomes = {
            "searxng": await srv._searxng_search(query, 10),
            "brave": await srv._brave_search(query, 10, brave_key),
        }
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
                "maximum_estimated_brave_cost_usd": round(
                    len(CASES) * srv._PROVIDER_COST_USD["brave"], 4
                ),
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
