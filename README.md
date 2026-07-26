# local_web_search

Local, loopback-only web-search services for macOS:

- **FastMCP broker** on `http://127.0.0.1:8889/mcp`
- **Brave Search API** as the default web and image provider
- **SearXNG** on `http://127.0.0.1:8888` as a keyless provider or dual-stack primary
- **launchd operator CLI** for install, health, logs, telemetry, and upgrades

The broker is shared by Pi and my-ai. It returns structured ranked results,
query-free operational metadata, and an estimated per-search provider cost.

## Architecture

```text
MCP client
  │
  └── 127.0.0.1:8889/mcp
        ├── web_search ───── Brave, SearXNG, or SearXNG → Brave
        ├── batch_web_search same provider stack, bounded concurrency
        ├── image_search ─── Brave, except SearXNG-only stack
        ├── web_fetch ────── direct public-URL fetch + extraction
        └── verify_url ───── direct-fetch verification

SearXNG: 127.0.0.1:8888
```

Three provider stacks are supported:

| `WEBSEARCH_PROVIDER_STACK` | Web search | Image search | External request cost |
|---|---|---|---|
| `brave` (default) | Brave Search API | Brave Images API | Estimated `$0.005` per issued Brave request |
| `searxng` | Loopback SearXNG | Loopback SearXNG | No provider fee |
| `searxng+brave` | SearXNG primary plus policy-controlled Brave reference/fallback | Brave Images API only | `$0` until a Brave request is issued; then estimated `$0.005` |

SearXNG is diagnostic-only when it is not in the active stack. A healthy
SearXNG process does not make a Brave-only broker ready when the Brave
credential is missing or its circuit is unavailable. The dual stack requires
both loopback SearXNG and a usable Brave credential when shadow mode is active.

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

For dual-stack evaluation (SearXNG evaluated, Brave still served):

```bash
WEBSEARCH_PROVIDER_STACK=searxng+brave WEBSEARCH_QUALITY_GATE=shadow ./install.sh
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

### `web_search(query, num_results=8, mode=None, intent="general")`

Searches the active provider stack. Queries are limited to 512 characters and
results to 20. Provider responses, retries, and total latency are bounded.
`intent` must be explicit: `general` uses no freshness filter, `current` uses a
recent window, and `news` uses the narrowest freshness window. Intent is never
inferred from query text. `batch_web_search` accepts the same three intents.

Routing modes:

| Mode | Behavior |
|---|---|
| `normal` | Use the configured provider stack |
| `sensitive` | Make no external request; currently returns a structured refusal because no local corpus is configured |
| `maximum_recall` | Contact every provider in the configured stack serially and merge results |

For `searxng+brave`, `WEBSEARCH_QUALITY_GATE` controls normal routing:

| Gate | Dual-stack behavior |
|---|---|
| `shadow` | On every uncached search (cache miss), evaluate SearXNG and Brave, return Brave when usable, and record query-free decision/parity metrics; evaluation only |
| `auto` or `on` | Return SearXNG when it passes; call Brave on empty/error/timeout/circuit skip or a quality-gate failure |
| `off` | Disable quality checks; still use Brave for primary empty/error/timeout/circuit skip |

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

### `batch_web_search(queries, num_results=8, intent="general")`

Runs up to three unique queries with at most two active search pipelines and one
shared deadline. One explicit `general`, `current`, or `news` intent applies to
the batch. Output ordering follows input ordering. Partial completions and
per-item timeouts remain explicit.

### `image_search(query, num_results=8)`

Uses Brave Images for the `brave` and `searxng+brave` stacks; the dual stack
does not route images through SearXNG. The `searxng` stack uses SearXNG images.
Brave Images uses `safesearch=moderate`; SearXNG uses its moderate policy. The
response includes the actual backend, request attempts, safety policy, and
estimated cost.

### `web_fetch(url, max_chars=20000)`

Fetches a public HTTP(S) URL with DNS/IP validation, connection pinning,
redirect revalidation, response-size limits, and isolated HTML/PDF extraction.
Loopback, private, link-local, and other non-public destinations are rejected.
The default 20 MiB body cap is enforced before reading a valid oversized
`Content-Length` and again while streaming raw bytes, so chunked or misdeclared
responses cannot bypass it. Fetches request `Accept-Encoding: identity` and
reject encoded responses before reading their bodies.

### `verify_url(url)`

Runs the direct-fetch verifier and returns bounded observations. It does not
fall back to a search-provider extraction API.

## Health and telemetry

| Endpoint | Purpose |
|---|---|
| `GET /live` | Dependency-free process liveness |
| `GET /ready` | Active-policy readiness; HTTP 503 when requirements are unmet (shadow evaluation requires usable SearXNG and Brave) |
| `GET /health` | Compatibility diagnostics; always HTTP 200 |
| `GET /stats?window=24h` | Query-free aggregates; windows: `24h`, `7d`, `30d` |

Telemetry is enabled by default and stored in
`$LOCAL_SEARCH_DATA_DIR/telemetry.sqlite3`. Search and parity telemetry stores
bounded operational fields only: status, provider, routing mode, counts,
latency, normalized failure reason, credential mode, circuit state, and numeric
shadow parity. It stores no search/result identifiers: never queries, URLs,
domains, hashes of URLs/domains, titles, snippets, headers, result content, or
credentials. Fetch telemetry separately stores the normalized destination
hostname plus bounded outcome/status/size/latency fields; it never stores a full
URL, path, or query. The primary general-web count uses the normalized
Bing/Mwmbl engine label supplied by SearXNG.

`/stats` exposes numeric comparison aggregates under `shadow.parity`. Rate
denominators are explicit in `reference_result_total`,
`reference_domain_total`, and `primary_result_total`, with corresponding
nonempty sample counts and `comparable_sample_count`. A rate is `null` when its
denominator is zero, rather than reporting a misleading `0%`. Cache hits and
incomplete comparisons are excluded.

The private cache lives under `$LOCAL_SEARCH_DATA_DIR/cache/`. Search keys are
query hashes and cached search payloads contain returned result data; fetched
content entries retain canonical URLs and extracted bodies. Cache hits emit
provider-free telemetry and report zero estimated provider cost.

```bash
scripts/local-search stats 7d
scripts/local-search telemetry-reset --yes
```

Disable telemetry with `LOCAL_SEARCH_TELEMETRY_ENABLED=false` when running
`scripts/local-search install`; configuration changes require install to
regenerate the LaunchAgent environment.

## Configuration

Important environment variables:

| Variable | Default |
|---|---|
| `WEBSEARCH_PROVIDER_STACK` | `brave` |
| `WEBSEARCH_SEARCH_MODE` | `normal` |
| `WEBSEARCH_QUALITY_GATE` | `auto`; also `on`, `off`, or `shadow` |
| `WEBSEARCH_QUALITY_MIN_RESULTS` | `3` |
| `WEBSEARCH_QUALITY_MIN_DOMAINS` | `2` |
| `WEBSEARCH_QUALITY_DUPLICATE_FRACTION` | `0.6` |
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

Use `scripts/local-search env` to inspect resolved non-secret values. The
`install` command persists settings, rewrites the launchd plist, and starts it
unless `--no-start` is used. `restart` only restarts the already-installed
plist; it does **not** re-render changed shell variables or `data/install.env`.
Run `scripts/local-search install` after changing persistent configuration.

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
- Identifier-free search/parity telemetry, host-only fetch telemetry, and URL-query log redaction
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
