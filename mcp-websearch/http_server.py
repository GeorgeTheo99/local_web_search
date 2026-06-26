#!/usr/bin/env python3
"""Run the websearch MCP server as an HTTP service."""

import os
import sys

# Add the parent directory so we can import from server.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server import mcp

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888")
MCP_PORT = int(os.environ.get("MCP_PORT", "8889"))

if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=MCP_PORT,
        show_banner=False,
        stateless_http=True,
        json_response=True,
    )
