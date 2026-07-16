#!/usr/bin/env python3
"""Run the loopback-only websearch MCP server as an HTTP service.

Serves:
  POST /mcp    — MCP streamable-http transport (tools/list, tools/call)
  GET  /live   — dependency-free process liveness
  GET  /ready  — backend readiness (503 when unavailable)
  GET  /health — compatibility diagnostics (always HTTP 200)
  GET  /stats  — query-free telemetry aggregates (`window=24h|7d|30d`)

Configuration via environment:
  SEARXNG_URL             default http://127.0.0.1:8888
  MCP_PORT                default 8889
  LOCAL_SEARCH_DATA_DIR   default repository data directory
  LOG_LEVEL               default INFO
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse

from server import _get_telemetry, mcp

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
MCP_BIND_HOST = "127.0.0.1"
MCP_PORT = int(os.environ.get("MCP_PORT", "8889"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs full GET URLs at INFO, including SearXNG's `q` query parameter.
# Keep operational logs from retaining search text.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _loopback_authority(value: str) -> bool:
    """Return whether an HTTP authority is an exact loopback host."""
    try:
        parsed = urllib.parse.urlsplit(f"//{value}")
        _ = parsed.port  # validate malformed and out-of-range ports
    except ValueError:
        return False
    return (
        parsed.hostname is not None
        and parsed.hostname.rstrip(".").lower() in _ALLOWED_LOOPBACK_HOSTS
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


def _loopback_origin(value: str) -> bool:
    """Return whether a browser Origin is a syntactically valid loopback origin."""
    try:
        parsed = urllib.parse.urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and parsed.hostname is not None
        and parsed.hostname.rstrip(".").lower() in _ALLOWED_LOOPBACK_HOSTS
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


class LoopbackRequestGuard:
    """Reject non-loopback Host/Origin values before MCP request handling."""

    def __init__(self, app: Callable[..., Awaitable[Any]]) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        hosts = [value.decode("latin-1") for name, value in headers if name.lower() == b"host"]
        origins = [value.decode("latin-1") for name, value in headers if name.lower() == b"origin"]
        if len(hosts) != 1 or not _loopback_authority(hosts[0]):
            await PlainTextResponse("loopback Host required", status_code=421)(scope, receive, send)
            return
        if len(origins) > 1 or (origins and not _loopback_origin(origins[0])):
            await PlainTextResponse("loopback Origin required", status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_app():
    return mcp.http_app(
        transport="streamable-http",
        json_response=True,
        stateless_http=True,
        middleware=[Middleware(LoopbackRequestGuard)],
    )


def main() -> None:
    # Initialize private SQLite state before accepting search requests.
    _get_telemetry()
    # uvicorn is a fastmcp dependency; import lazily so stdio mode (server.py)
    # never requires it.
    import uvicorn

    uvicorn.run(
        build_app(),
        host=MCP_BIND_HOST,
        port=MCP_PORT,
        log_level=LOG_LEVEL.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
