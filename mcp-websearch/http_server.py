#!/usr/bin/env python3
"""Run websearch on loopback, optionally as an MCP-only Tailscale Serve backend.

Serves:
  POST /mcp    — MCP streamable-http transport (tools/list, tools/call)
  GET  /live   — dependency-free process liveness
  GET  /ready  — backend readiness (503 when unavailable)
  GET  /health — compatibility diagnostics (always HTTP 200)
  GET  /stats  — query-free telemetry aggregates (`window=24h|7d|30d`)

Configuration via environment:
  MCP_PORT                default 8889
  LOCAL_SEARCH_DATA_DIR   default repository data directory
  LOG_LEVEL               default INFO
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse

from server import _get_telemetry, mcp

MCP_BIND_HOST = "127.0.0.1"
MCP_PORT = int(os.environ.get("MCP_PORT", "8889"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs full GET URLs at INFO, including search query parameters.
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


def validate_tailnet_host(value: str) -> str:
    """Require one canonical MagicDNS hostname, never a URL or wildcard."""
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if len(value) > 253 or not re.fullmatch(rf"{label}\.{label}\.ts\.net", value):
        raise ValueError("MCP_TAILNET_HOST must be a lowercase device.tailnet.ts.net hostname")
    return value


class TailnetRequestGuard:
    """MCP-only ingress. Authorization is Tailscale Serve's network boundary.

    This must use a separate loopback listener, never the local diagnostic app.
    Local processes are trusted; Host/Origin checks are not authentication.
    """

    def __init__(self, app: Callable[..., Awaitable[Any]], hostname: str) -> None:
        self.app = app
        self.hostname = validate_tailnet_host(hostname)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers") or []
        hosts = [v.decode("latin-1") for k, v in headers if k.lower() == b"host"]
        origins = [v.decode("latin-1") for k, v in headers if k.lower() == b"origin"]
        peer = scope.get("client")
        error = None
        if not peer or peer[0] != MCP_BIND_HOST:
            error = (403, "loopback proxy required")
        elif len(hosts) != 1 or hosts[0] not in {self.hostname, f"{self.hostname}:443"}:
            error = (421, "configured tailnet Host required")
        elif len(origins) > 1 or (origins and origins[0] not in {
            f"https://{self.hostname}", f"https://{self.hostname}:443",
        }):
            error = (403, "same-site HTTPS Origin required")
        elif scope.get("path") != "/mcp" or scope.get("query_string"):
            error = (404, "not found")
        elif scope.get("method") != "POST":
            error = (405, "POST required")
        if error:
            await PlainTextResponse(error[1], status_code=error[0])(scope, receive, send)
            return
        await self.app(scope, receive, send)


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


def build_app(*, tailnet_host: str | None = None):
    guard = (
        Middleware(TailnetRequestGuard, hostname=validate_tailnet_host(tailnet_host))
        if tailnet_host is not None else Middleware(LoopbackRequestGuard)
    )
    return mcp.http_app(
        transport="streamable-http",
        json_response=True,
        stateless_http=True,
        middleware=[guard],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tailnet", action="store_true", help="MCP-only Serve backend")
    args = parser.parse_args()
    tailnet_host = None
    if args.tailnet:
        tailnet_host = validate_tailnet_host(os.environ.get("MCP_TAILNET_HOST", ""))
    app = build_app(tailnet_host=tailnet_host)
    # Initialize private SQLite state before accepting search requests.
    _get_telemetry()
    # uvicorn is a fastmcp dependency; import lazily so stdio mode (server.py)
    # never requires it.
    import uvicorn

    uvicorn.run(
        app,
        host=MCP_BIND_HOST,
        port=MCP_PORT,
        log_level=LOG_LEVEL.lower(),
        access_log=False,
        # Preserve the actual peer; forwarded client IPs are not local peers.
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
