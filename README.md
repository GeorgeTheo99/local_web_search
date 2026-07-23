# local_web_search

Local, loopback-only web-search services for macOS:

- **FastMCP broker** on `http://127.0.0.1:8889/mcp`
- **Brave Search API** as the default web and image provider
- **SearXNG** on `http://127.0.0.1:8888` as an optional keyless provider
- **launchd operator CLI** for install, health, logs, telemetry, and upgrades

The broker is shared by Pi and my-ai. It returns structured ranked results,
query-free operational metadata, and an estimated per-search provider cost.

## Architecture

```text
MCP client
  │
  └── 127.0.0.1:8889/mcp
        ├── web_search ───── Brave (default) or SearXNG
        ├── batch_web_search same provider stack, bounded concurrency
        ├── image_search ─── Brave (default) or SearXNG
        ├── web_fetch ────── direct public-URL fetch + extraction
        └── verify_url ───── direct-fetch verification

SearXNG: 127.0.0.1:8888
```

Only two provider stacks are supported:

| `WEBSEARCH_PROVIDER_STACK` | Web search | Image search | External request cost |
|---|---|---|---|
| `brave` (default) | Brave Search API | Brave Images API | Estimated `$0.005` per issued API request |
| `searxng` | Loopback SearXNG | Loopback SearXNG | No provider fee |

SearXNG is diagnostic-only when it is not in the active stack. A healthy
SearXNG process does not make a Brave-only broker ready when the Brave
credential is missing or its circuit is unavailable.

See [`docs/adr/0002-search-architecture-redesign.md`](docs/adr/0002-search-architecture-redesign.md)
and [`docs/provider-comparison.md`](docs/provider-comparison.md) for the provider
decision and evidence.

## Quick start

Prerequisites: macOS, `bash`, `curl`, `git`, `launchctl`, Python 3, and
[`uv`](https://docs.astral.sh/uv/).

The default Brave stack requires its owner-only key **before** the first
started install. The installer fails fast with an actionable message when the
key is absent or has unsafe permissions.

```bash
cd /Users/localserver99/local_code/local_web_search
mkdir -p data && chmod 700 data
umask 077
read -r -s -p 'Brave API key: ' BRAVE_KEY; printf '\n'
printf '%s\n' "$BRAVE_KEY" > data/brave_key
unset BRAVE_KEY
chmod 600 data/brave_key
./install.sh
```

For a keyless SearXNG-only install:

```bash
WEBSEARCH_PROVIDER_STACK=searxng ./install.sh
```

`install.sh` delegates to `scripts/local-search install`. It validates provider
readiness prerequisites, creates/syncs the Python environments, installs
launchd plists, starts SearXNG and the MCP broker, and verifies health plus the
exact MCP tool list.

Common commands:

```bash
scripts/local-search status
scripts/local-search verify
scripts/local-search stats 24h
scripts/local-search logs mcp -f
scripts/local-search restart
scripts/local-search env
```

Install without starting services:

```bash
scripts/local-search install --no-start
```

## Brave credential

Credential precedence is:

1. Per-request `X-Brave-Key`
2. `BRAVE_API_KEY` in the broker process environment
3. Owner-only file at `$LOCAL_SEARCH_DATA_DIR/brave_key`

The repository-local default is `data/brave_key`. Create it without putting the
secret in shell history:

```bash
mkdir -p data
chmod 700 data
umask 077
read -r -s -p 'Brave API key: ' BRAVE_KEY; printf '\n'
printf '%s\n' "$BRAVE_KEY" > data/brave_key
unset BRAVE_KEY
chmod 600 data/brave_key
scripts/local-search restart
scripts/local-search verify
```

The key file is ignored by Git. Group/world-readable key files are rejected.
`Authorization` and generic `X-Api-Key` headers are never repurposed as Brave
credentials.

## MCP tools

### `web_search(query, num_results=8, mode=None)`

Searches the active provider stack. Queries are limited to 512 characters and
results to 20. Provider responses, retries, and total latency are bounded.

Routing modes:

| Mode | Behavior |
|---|---|
| `normal` | Use the configured provider stack |
| `sensitive` | Make no external request; currently returns a structured refusal because no local corpus is configured |
| `maximum_recall` | Contact every provider in the configured stack serially and merge results |

The response includes:

- `status`: `ok`, `empty`, `degraded`, or `error`
- `backend`: provider that supplied returned results
- `attempted`: providers that actually issued at least one request
- `estimated_cost_usd`: sum of configured rates for issued requests
- `provider_states`, `timings_ms`, `fallback_reason`, and circuit diagnostics
- `mode` and `search_mode`

Missing credentials, open-circuit skips, and providers excluded by the total
deadline are not counted as billable attempts. Failed HTTP requests are counted
because the upstream may still bill them.

### `batch_web_search(queries, num_results=8)`

Runs up to three unique queries with at most two active search pipelines and one
shared deadline. Output ordering follows input ordering. Partial completions and
per-item timeouts remain explicit.

### `image_search(query, num_results=8)`

Uses Brave Images for the `brave` stack and SearXNG images for the `searxng`
stack. Brave Images uses `safesearch=strict`; SearXNG uses its moderate policy.
The response includes the actual backend, request attempts, safety policy, and
estimated cost.

### `web_fetch(url, max_chars=20000)`

Fetches a public HTTP(S) URL with DNS/IP validation, connection pinning,
redirect revalidation, response-size limits, and isolated HTML/PDF extraction.
Loopback, private, link-local, and other non-public destinations are rejected.

### `verify_url(url)`

Runs the direct-fetch verifier and returns bounded observations. It does not
fall back to a search-provider extraction API.

## Health and telemetry

| Endpoint | Purpose |
|---|---|
| `GET /live` | Dependency-free process liveness |
| `GET /ready` | Active-provider readiness; HTTP 503 when unusable |
| `GET /health` | Compatibility diagnostics; always HTTP 200 |
| `GET /stats?window=24h` | Query-free aggregates; windows: `24h`, `7d`, `30d` |

Telemetry is enabled by default and stored in
`$LOCAL_SEARCH_DATA_DIR/telemetry.sqlite3`. It stores bounded operational fields
only: status, provider, routing mode, counts, latency, normalized failure reason,
credential mode, and circuit state. It never accepts query text, URLs, snippets,
headers, result content, or credentials.

```bash
scripts/local-search stats 7d
scripts/local-search telemetry-reset --yes
```

Disable telemetry with `LOCAL_SEARCH_TELEMETRY_ENABLED=false` before install or
restart.

## Configuration

Important environment variables:

| Variable | Default |
|---|---|
| `WEBSEARCH_PROVIDER_STACK` | `brave` |
| `WEBSEARCH_SEARCH_MODE` | `normal` |
| `WEBSEARCH_TOTAL_TIMEOUT` | `18` seconds, hard-capped at 18 |
| `WEBSEARCH_BRAVE_TIMEOUT` | `8` seconds |
| `WEBSEARCH_SEARXNG_TIMEOUT` | `7` seconds |
| `WEBSEARCH_SEARCH_MAX_BYTES` | `2 MiB` per provider response |
| `WEBSEARCH_FETCH_MAX_BYTES` | `20 MiB` |
| `WEBSEARCH_FETCH_TIMEOUT` | `30` seconds |
| `LOCAL_SEARCH_DATA_DIR` | repository `data/` directory |
| `LOCAL_SEARCH_TELEMETRY_ENABLED` | `true` |
| `SEARXNG_URL` | `http://127.0.0.1:8888` |
| `BRAVE_BASE_URL` | `https://api.search.brave.com` |
| `MCP_PORT` | `8889` |

Use `scripts/local-search env` to inspect the installed non-secret values.

## Client configuration

Clients should use the endpoint, not repository paths:

```text
http://127.0.0.1:8889/mcp
```

For stdio clients:

```bash
scripts/local-search mcp-stdio
```

Services intentionally bind to loopback. Remote use is supported only through a
persistent SSH local forward; see
[`docs/remote-mcp-over-ssh.md`](docs/remote-mcp-over-ssh.md). Do not publish the
broker through Caddy, a LAN listener, a tailnet listener, or a public endpoint.

## Verification

Run the broker suite:

```bash
cd mcp-websearch
uv run pytest -q
uv run python -m py_compile server.py telemetry.py http_server.py html_extraction.py
cd ..
bash -n scripts/local-search
scripts/local-search verify
```

The optional provider benchmark issues up to 12 billable Brave requests
(currently at most `$0.06`) and therefore should be run intentionally:

```bash
cd mcp-websearch
uv run python ../benchmarks/provider_smoke.py > ../benchmarks/results/$(date +%F).json
```

Historical benchmark result files may contain provider names from earlier
architecture evaluations; they are retained as immutable evidence, not active
configuration.

## Security and reliability

- Loopback Host/Origin enforcement on MCP HTTP transport
- Public-address validation and DNS/IP pinning for fetches
- Redirect-chain revalidation
- Bounded provider JSON, fetch bodies, extraction output, and subprocess stderr
- Circuit breakers and total-deadline enforcement
- Query-free telemetry and URL-query log redaction
- Owner-only data directory, telemetry database, install config, and secret file
- launchd `KeepAlive` with dependency-ordered restart and verification

## Repository layout

```text
mcp-websearch/
  server.py              MCP tools, routing, providers, fetch security
  http_server.py         HTTP transport and loopback policy
  html_extraction.py     isolated readable-text extraction
  telemetry.py           private SQLite operational telemetry
  tests/                 provider, routing, fetch, telemetry, and contract tests
searxng/
  settings.yml           local engine configuration
  run.py                 SearXNG runner
  SEARXNG_REF             pinned upstream revision
scripts/
  local-search           operator CLI
  local-search-tunnel    remote SSH forwarding helper
benchmarks/
  provider_smoke.py      intentional paid Brave/SearXNG comparison
  results/               historical evidence
docs/
  adr/                    architecture decisions
  provider-comparison.md current provider rationale
  roadmap.md             cost/reliability roadmap
```
