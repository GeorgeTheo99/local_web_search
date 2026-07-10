# Local Search Stack

A self-contained, installable local web-search stack for developer/agent
tooling and Home Automation. Runs SearXNG (metasearch) and a FastMCP wrapper
that exposes `web_search` / `web_fetch` tools over MCP (HTTP and stdio), with
**Tavily failover** for reliability when SearXNG's scraped engines block or
rate-limit.

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
| MCP liveness | `GET http://127.0.0.1:8889/live` | Dependency-free process check |
| MCP readiness | `GET http://127.0.0.1:8889/ready` | Provider readiness; HTTP 503 when unavailable |
| MCP diagnostics | `GET http://127.0.0.1:8889/health` | Compatibility endpoint; policy, circuits, and last-search quality |

## Search backends and Tavily policy

`web_search` always tries the loopback SearXNG service first. External Tavily
egress is explicit and controlled by `WEBSEARCH_TAVILY_MODE`:

| Mode | Behavior |
|---|---|
| `disabled` | Never contact Tavily; return SearXNG results, empty, or error state |
| `fallback` (default) | Use Tavily only when SearXNG has no usable results |
| `supplement` | Add Tavily when deduped SearXNG results are below `WEBSEARCH_SUPPLEMENT_MIN_RESULTS` |

The SearXNG layer uses Brave plus a tracked, defensive Mwmbl JSON adapter so a
rate-limited scraper does not leave local search without a second broad index.
The broker overfetches candidates before removing URL fragments and tracking
parameters, dedupes with SearXNG precedence, then truncates to the requested
count. A thread-safe per-backend circuit breaker admits only one half-open
recovery probe after cooldown.

Every payload preserves the legacy `query` / `results` / `suggestions` / `text`
contract and adds `status`, `backend`, `attempted`, `fallback_reason`,
`timings_ms`, `provider_states`, `mode`, and `unresponsive_engines`. `status` distinguishes
`ok`, `empty`, `degraded`, and terminal `error`; only terminal errors carry the
legacy `error` key.

### Tavily: keyless by default, optional key upgrade

Tavily failover works **with zero setup** via Tavily's free keyless tier
(`X-Tavily-Access-Mode: keyless` — no account, no API key, no signup). Every
install gets reliable search out of the box. The keyless tier uses a shared
anonymous rate limit; for higher limits, swap in a free API key
(1,000 credits/month, no credit card) — same code path, no changes needed.

Tavily key resolution (precedence):

1. **Per-call `X-Tavily-Key` header** — explicitly identifies a Tavily credential.
2. **`TAVILY_API_KEY` environment variable** — intended for stdio/server-side consumers.
3. **Neither** → keyless mode (free, no account, shared rate limit).

`Authorization` and generic `X-Api-Key` values are never repurposed as Tavily
credentials. The installer deliberately does not persist secrets in launchd
plists; HTTP clients should forward Tavily keys explicitly, and stdio users
should inject `TAVILY_API_KEY` through their process environment.

When a key is present, it takes precedence over keyless and uses the caller's
account quota. Local-search never persists per-call keys.

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

Flags / non-secret policy overrides:

```bash
./install.sh --no-start                          # bootstrap without starting
LOCAL_SEARCH_LOG_DIR=~/logs ./install.sh         # custom log dir
WEBSEARCH_TAVILY_MODE=disabled ./install.sh      # SearXNG-only privacy mode
WEBSEARCH_TAVILY_MODE=supplement \
  WEBSEARCH_SUPPLEMENT_MIN_RESULTS=5 ./install.sh
```

Installed policy defaults are `18s` total, `7s` SearXNG, and `8s` Tavily.
`WEBSEARCH_TOTAL_TIMEOUT` is hard-capped at 18 seconds so Pi's 20-second MCP
budget retains transport/serialization margin. `WEBSEARCH_SEARCH_TIMEOUT`
remains a deprecated alias for `WEBSEARCH_SEARXNG_TIMEOUT`.

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
- **Bounded search**: one total deadline contains SearXNG retries and Tavily
  fallback; timeout and circuit states remain visible in result metadata.
- **Consistent states**: searches distinguish `ok`, `empty`, `degraded`, and
  terminal `error`; fetch errors retain the stable `Fetch error:` prefix.
- **Health separation**: `/live` is dependency-free; `/ready` returns 503 only
  when no policy-allowed backend is usable; `/health` remains HTTP 200 for
  compatibility and exposes safe breaker/policy/last-search metadata, including
  recent per-provider outcomes, without retaining query text, URLs, headers, or keys.
- **Local binding**: SearXNG and the MCP HTTP broker listen on loopback only.
- **Query-log hygiene**: broker HTTP client logging suppresses full request
  URLs, and the tracked SearXNG runner redacts `q`/`query`/`s` parameters from
  operational log messages before they are written.
- **Dependency-ordered restart**: MCP is stopped before SearXNG and started after
  SearXNG is healthy; ports are checked free to avoid bind races.
- **KeepAlive**: both services auto-restart on crash (`ThrottleInterval` 5s).

## Tests

```bash
cd mcp-websearch && uv run pytest -q
```

Covers the SSRF rejection matrix, `web_fetch` errors, compatibility payloads,
Tavily disabled/fallback/supplement policy, overfetch/dedupe, bounded stage
timeouts, distinct empty/degraded/error states, readiness, safe last-search
metadata, single-probe half-open circuits, key separation, and `tools/list`.

## Layout

```
local-search/
├── install.sh                    # bootstrap entrypoint (→ scripts/local-search install)
├── scripts/
│   └── local-search              # operator CLI (install/uninstall/start/stop/...)
├── mcp-websearch/
│   ├── server.py                 # MCP tools, provider policy, and health routes
│   ├── http_server.py            # HTTP transport entrypoint (uvicorn)
│   ├── pyproject.toml            # uv project (fastmcp, httpx; dev: pytest)
│   └── tests/test_server.py
└── searxng/
    ├── engines/mwmbl_safe.py     # Defensive keyless Mwmbl JSON adapter
    ├── run.py                    # SearXNG entry point with local engines + log redaction
    ├── settings.yml              # SearXNG config (engines, bind, json format)
    ├── SEARXNG_REF               # pinned SearXNG commit for reproducible installs
    └── src/                      # SearXNG checkout + venv (gitignored, bootstrapped)
```

## Uninstall

```bash
local-search uninstall          # stops services + removes plists (keeps src/venvs)
rm -rf ~/local_code/local-search  # full removal
```
