# Local Search Stack

Shared local search infrastructure for developer/agent tooling and Home Automation.

## Services

- SearXNG: `http://127.0.0.1:8888`
- MCP websearch HTTP: `http://127.0.0.1:8889/mcp`
- MCP websearch stdio: `mcp-websearch/server.py`

`mcp-websearch` wraps SearXNG and exposes `web_search` and `web_fetch` tools.
Consumers should use endpoint/config wiring (`SEARCH_MCP_URL`, `SEARXNG_URL`) rather
than importing this code from `server/`.

## Local setup

```bash
cd ~/local_code/local-search/mcp-websearch
uv sync
```

SearXNG is installed as a vendored checkout under `searxng/src` on this machine.
LaunchAgents are managed by `server-ci` for now, but paths point at this directory.
