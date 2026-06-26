# Local Search Stack

A self-contained, installable local web-search stack for developer/agent
tooling and Home Automation. Runs SearXNG (metasearch) and a FastMCP wrapper
that exposes `web_search` / `web_fetch` tools over MCP (HTTP and stdio).

This is its own component. It is a **dependency** of:

- **`pi-shared`** — agent/cloud tooling wires MCP search via Claude/Codex settings.
- **`server` (Home Automation)** — uses it through `search.provider: mcp`.

Consumers depend on the **endpoints** (`SEARCH_MCP_URL`, `SEARXNG_URL`), never
on local filesystem paths inside this repo. Home Automation customer installs
stay BYO (Tavily / external SearXNG / external MCP) by default; this stack is
opt-in for machines that want a local search backend.

## Services

| Service | Address | Purpose |
|---|---|---|
| SearXNG | `http://127.0.0.1:8888` | Metasearch engine (`/healthz`, `/search?format=json`) |
| MCP websearch (HTTP) | `http://127.0.0.1:8889/mcp` | MCP streamable-http transport |
| MCP websearch (stdio) | `mcp-websearch/server.py` | MCP stdio transport |
| MCP health | `GET http://127.0.0.1:8889/health` | Liveness + SearXNG reachability probe |

## Requirements

- macOS (launchd-managed)
- `uv` ([astral-sh/uv](https://github.com/astral-sh/uv)) — `brew install uv`
- `git`, `python3`, `curl`
- SearXNG source is cloned from `https://github.com/searxng/searxng.git` at the
  pinned ref in [`searxng/SEARXNG_REF`](searxng/SEARXNG_REF).

## Install

Clone this repo (or your fork) anywhere, then run the installer. It is
idempotent and safe to re-run:

```bash
git clone <this-repo> ~/local_code/local-search
cd ~/local_code/local-search
./install.sh
```

What `install.sh` does (delegates to `scripts/local-search install`):

1. Clones/updates SearXNG source into `searxng/src` at the pinned ref.
2. Builds the SearXNG venv (`uv venv` + `requirements.txt` + editable install).
3. Builds the MCP websearch venv (`uv sync`).
4. Writes launchd plists (`com.local.searxng`, `com.local.mcp-websearch`) into
   `~/Library/LaunchAgents/` with logs under `~/Library/Logs/local-search/`.
5. Starts services (SearXNG first, then MCP) and runs health verification.

Flags / overrides:

```bash
./install.sh --no-start                       # bootstrap without starting
LOCAL_SEARCH_LOG_DIR=~/logs ./install.sh      # custom log dir
SEARXNG_URL=http://localhost:8888 ./install.sh
```

## Operator CLI

All day-to-day operations go through `scripts/local-search`:

```bash
local-search install [--no-start]    bootstrap + start + verify
local-search uninstall               stop services, remove plists (keeps src/venvs)
local-search start | stop | restart  service control (ordered: searxng before mcp)
local-search status                  launchd state + /health probes
local-search verify                  health + tools/list smoke
local-search logs [searxng|mcp] [-f|N]
local-search update                  git pull + rebuild venvs + restart + verify
local-search update-searxng-ref <sha>   pin a new SearXNG commit
local-search mcp-stdio               run MCP in stdio mode (for client config)
local-search env                     print resolved config
```

Install a `local-search` shim on PATH (optional):

```bash
ln -s ~/local_code/local-search/scripts/local-search ~/.local/bin/local-search
```

### Upgrade

```bash
local-search update
```

Pulls this repo, re-syncs SearXNG to the pinned ref, rebuilds both venvs,
rewrites plists, restarts in dependency order, and verifies. To move to a newer
SearXNG:

```bash
local-search update-searxng-ref <commit-sha>
local-search update
```

## Consumer wiring

### Home Automation (`server`)

`runtime.yaml`:

```yaml
search:
  provider: mcp
  mcp_url: "http://127.0.0.1:8889/mcp"
```

See `server/home-automation/src/search/service.py` (`McpSearchProvider`). The
provider calls `tools/call` and extracts `result.content[].text`, which this
server returns as a JSON string with `results` / `suggestions` / `text` (and
`error` on failure).

### Claude Code / Codex / agent MCP settings

stdio transport (Claude `settings.json`):

```json
{
  "mcpServers": {
    "websearch": {
      "command": "/Users/<you>/local_code/local-search/mcp-websearch/.venv/bin/python",
      "args": ["/Users/<you>/local_code/local-search/mcp-websearch/server.py"]
    }
  }
}
```

HTTP transport (any MCP client): `http://127.0.0.1:8889/mcp`.

## Distribution matrix

Home Automation supports several search backends. This stack is one option:

| Backend | When to use | Config |
|---|---|---|
| Tavily (default) | Customer installs, no local infra | `search.provider: tavily` + API key |
| External SearXNG | Self-hosted SearXNG elsewhere | `search.provider: searxng`, `search.searxng_base_url` |
| External MCP | Any MCP search endpoint | `search.provider: mcp`, `search.mcp_url` |
| **This stack (local)** | Developer/power-user Mac with local SearXNG | install this repo + `search.provider: mcp` + `mcp_url: http://127.0.0.1:8889/mcp` |

This stack is **not** bundled into customer Home Automation installs. It is
installed separately on machines that want a local search backend.

## Reliability & security

- **SSRF guard**: `web_fetch` rejects loopback, RFC1918, link-local, multicast,
  reserved, unspecified IPs and `localhost`/`.localhost` domains, including
  hostnames that resolve to private IPs and redirect chains to private IPs.
- **Retries**: `web_search` retries SearXNG once with backoff; `_searxng_request`
  never raises — backend outages surface as structured error payloads.
- **Consistent error payloads**: search errors return JSON with `error` + `text`;
  fetch errors return `Fetch error: <message>` (stable prefix for callers/tests).
- **Health probe**: `GET /health` reports `ok` / `degraded` (MCP up but SearXNG
  unreachable) plus SearXNG latency — always HTTP 200 so naive probes don't alarm.
- **Dependency-ordered restart**: MCP is stopped before SearXNG and started after
  SearXNG is healthy; ports are checked free to avoid bind races.
- **KeepAlive**: both services auto-restart on crash (`ThrottleInterval` 5s).

## Tests

```bash
cd mcp-websearch && uv run pytest -q
```

Covers the SSRF rejection matrix (loopback, RFC1918, link-local, IPv6 local,
localhost domains, DNS-resolves-to-private, non-http schemes, public allow
cases), `web_fetch` error contract, `web_search` error/empty/success payload
shapes vs the `McpSearchProvider` contract, and `tools/list`.

## Layout

```
local-search/
├── install.sh                    # bootstrap entrypoint (→ scripts/local-search install)
├── scripts/
│   └── local-search              # operator CLI (install/uninstall/start/stop/...)
├── mcp-websearch/
│   ├── server.py                 # FastMCP tools web_search/web_fetch + /health route
│   ├── http_server.py            # HTTP transport entrypoint (uvicorn)
│   ├── pyproject.toml            # uv project (fastmcp, httpx; dev: pytest)
│   └── tests/test_server.py
└── searxng/
    ├── settings.yml              # SearXNG config (engines, bind, json format)
    ├── SEARXNG_REF               # pinned SearXNG commit for reproducible installs
    └── src/                      # SearXNG checkout + venv (gitignored, bootstrapped)
```

## Uninstall

```bash
local-search uninstall          # stops services + removes plists (keeps src/venvs)
rm -rf ~/local_code/local-search  # full removal
```
