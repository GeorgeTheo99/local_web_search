#!/usr/bin/env python3
"""Run the websearch MCP server as an HTTP service.

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

import logging
import os

from server import _get_telemetry, mcp

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
MCP_PORT = int(os.environ.get("MCP_PORT", "8889"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs full GET URLs at INFO, including SearXNG's `q` query parameter.
# Keep operational logs from retaining search text.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

if __name__ == "__main__":
    # Initialize private SQLite state before accepting search requests.
    _get_telemetry()
    app = mcp.http_app(
        transport="streamable-http",
        json_response=True,
        stateless_http=True,
    )
    # uvicorn is a fastmcp dependency; import lazily so stdio mode (server.py)
    # never requires it.
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=MCP_PORT,
        log_level=LOG_LEVEL.lower(),
        access_log=False,
    )
