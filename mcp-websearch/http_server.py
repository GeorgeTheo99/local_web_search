#!/usr/bin/env python3
"""Run the websearch MCP server as an HTTP service.

Serves:
  POST /mcp    — MCP streamable-http transport (tools/list, tools/call)
  GET  /health — liveness + SearXNG reachability probe

Configuration via environment:
  SEARXNG_URL   default http://localhost:8888
  MCP_PORT      default 8889
  LOG_LEVEL     default INFO
"""

import logging
import os

from server import mcp

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888").rstrip("/")
MCP_PORT = int(os.environ.get("MCP_PORT", "8889"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

if __name__ == "__main__":
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
