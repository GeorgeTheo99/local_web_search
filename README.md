# Local Web Search Stack

A self-contained, installable local web-search stack for developer/agent
tooling and Home Automation. Runs SearXNG (metasearch) and a FastMCP wrapper
that exposes `web_search` / `batch_web_search` / `image_search` / `web_fetch`
tools over MCP (HTTP and stdio), with **policy-controlled Tavily failover**
when SearXNG has no usable results.

The repository/directory is named `local_web_search`. Stable public interfaces
retain their existing names: the `local-search` operator command, MCP tool names,
launchd labels, ports, and URLs do not change when the repository moves. See the
[current provider comparison](docs/provider-comparison.md) for accuracy, cost,
privacy, and benchmark guidance covering SearXNG, Tavily, and Perplexity.

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
| MCP telemetry | `GET http://127.0.0.1:8889/stats?window=24h` | Query-free aggregates (`24h`, `7d`, or `30d`) |

## Search backends and Tavily policy

`web_search` always tries the loopback SearXNG service first. External Tavily
egress is explicit and controlled by `WEBSEARCH_TAVILY_MODE`:

| Mode | Behavior |
|---|---|
| `disabled` | Never contact Tavily; return SearXNG results, empty, or error state |
| `fallback` (default) | Use Tavily only when SearXNG has no usable results; degraded nonempty results remain local |
| `supplement` | Add Tavily when deduped SearXNG results are below `WEBSEARCH_SUPPLEMENT_MIN_RESULTS`, including degraded nonempty searches |

The SearXNG layer deliberately keeps only a tested engine set: Google CSE,
DuckDuckGo, Bing, Startpage, the tracked defensive Mwmbl JSON adapter, and the
focused Wikipedia/GitHub/arXiv engines. Image search uses only the pinned-upstream
`duckduckgo images` and `google images` engines, whose request implementations
apply moderate SafeSearch. Broker normalization rejects results attributed to
any other image engine. HTML Google web search, Qwant, Mojeek, and the
rate-limited Brave web scraper stay excluded. The `keep_only` policy prevents
new upstream defaults from silently joining every brokered search. Google CSE
is upstream's temporary replacement for its blocked HTML Google adapter and is
expected to require replacement when Google retires the current CSE path in
2027.

The broker overfetches candidates before removing URL fragments and tracking
parameters, dedupes with SearXNG precedence, then truncates to the requested
count. Single and batch queries are capped at 512 characters, and each provider
JSON response is stream-capped at 2 MiB by default. A thread-safe per-backend
circuit breaker admits only one half-open recovery probe after cooldown.

Every payload preserves the legacy `query` / `results` / `suggestions` / `text`
contract and adds `status`, `backend`, `attempted`, `fallback_reason`,
`timings_ms`, `provider_states`, `mode`, and `unresponsive_engines`. `status` distinguishes
`ok`, `empty`, `degraded`, and terminal `error`; only terminal errors carry the
legacy `error` key.

### Batch web search contract

`batch_web_search(queries: list[str], num_results: int = 8)` runs two or three
independent query angles in one MCP round trip. It preserves `web_search`
unchanged for existing consumers and reuses the same SearXNG/Tavily policy,
URL dedupe, circuit breakers, and per-query telemetry.

Inputs are limited to three non-empty queries of at most 512 characters.
Whitespace is normalized and case-insensitive duplicates are ignored rather
than searched twice. At most two provider pipelines run concurrently, and all
items share the broker's single 18-second-or-less total deadline. Caller
cancellation cancels and awaits every child request. The ordered batch payload
reports `ok`, `partial`, or `error`, includes explicit per-query timeout/error
states, and omits each single-search `text` rendering to avoid duplicating the
structured results in model context. It never silently drops excess queries.

### Image search contract

`image_search(query: str, num_results: int = 8)` uses only the configured
loopback SearXNG endpoint. It sends category `images` with numeric
`SafeSearch=1` (moderate), accepts results only from the verified engine allowlist,
never resolves a Tavily key, and never falls back to or supplements from Tavily.
Its SearXNG stage and total runtime use the same
bounded timeout policy as `web_search`; requested results are clamped to 1–20.
Image payloads use the normal metadata fields above with `mode: "disabled"`,
`attempted: ["searxng"]`, and `timings_ms.tavily: null`.

Each image result always has exactly these normalized fields:

```text
rank, title, image_url, thumbnail_url, page_url, source, engine,
width, height, mime_type, creator, license, license_url
```

`image_url` is required. Invalid, local/private-looking, credential-bearing, or
non-HTTP(S) image candidates are dropped; invalid optional URLs become `null`.
Candidates are deduplicated by canonical image URL before truncation while
preserving SearXNG order. Missing dimensions, MIME type, and optional URLs are
`null`; unavailable text metadata is an empty string. The broker returns URLs
only: it does not fetch or store image bytes, and query-free telemetry stores no
queries, URLs, titles, or result metadata.

### Tavily: keyless by default, optional key upgrade

Tavily failover works **with zero setup** via Tavily's free keyless tier
(`X-Tavily-Access-Mode: keyless` — no account, no API key, no signup). It is a
best-effort fallback, not guaranteed capacity. The keyless tier uses a shared
rate limit; it avoids an account/key but is not anonymous to Tavily, which still
receives the query and network metadata. For higher limits, swap in a free API key
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

## Query-free telemetry

Operational telemetry is enabled by default and stored in
`data/telemetry.sqlite3`, or under `LOCAL_SEARCH_DATA_DIR` when configured.
SQLite writes use WAL, short transactions, and a background queue so telemetry
cannot hold up search responses. The directory is mode `0700`; database,
WAL, SHM, and non-secret install-state files are restricted to the current
user and ignored by Git.

Telemetry persists only:

- timestamps, requested/result counts, provider outcomes, and latency;
- SearXNG degradation plus categorized engine failures;
- Tavily selection/attempts, errors, HTTP 429s, and keyed/keyless mode (never
  the credential itself);
- fallback reasons/rates and circuit trips, skips, and recoveries.

Batch search records one ordinary query-free operational event per query,
including an allowlisted `timeout` status and `batch_deadline` reason; it adds
no query-bearing batch state.
It never stores search queries or hashes, URLs, result titles/snippets/content,
suggestions, response bodies, request headers, credentials, or API keys. The
loopback-only `/stats` endpoint returns aggregates rather than raw events. It
marks a window incomplete when durable drop/write-failure markers exist.
Tavily credit usage is reported as unavailable because no dedicated usage API
credential is configured.

Supported windows:

```bash
local-search stats 24h
local-search stats 7d
local-search stats 30d
```

Telemetry is preserved by install, update, and uninstall. A custom installed
data path is reused by later CLI, update, and reinstall commands. Telemetry is erased only by
the explicit command `local-search telemetry-reset --yes`, which briefly stops
the managed HTTP MCP process to drain its writer queue before clearing SQLite
and then restarts it. A database reset epoch also rejects pre-reset events from
other writers. Set `LOCAL_SEARCH_TELEMETRY_ENABLED=false` during install only
when telemetry must be disabled completely.

## Requirements

- macOS (launchd-managed)
- `uv` ([astral-sh/uv](https://github.com/astral-sh/uv)) — `brew install uv`
- `git`, `python3`, `curl`
- Optional but recommended for PDF reading: Poppler (`brew install poppler`). On macOS, scanned PDFs then fall back to bounded Apple Vision OCR through `/usr/bin/swift`; without these tools, `web_fetch` returns a clear extraction error instead of binary PDF bytes.
- SearXNG source is cloned from `https://github.com/searxng/searxng.git` at the
  pinned ref in [`searxng/SEARXNG_REF`](searxng/SEARXNG_REF).

## Install

Clone this repo (or your fork) anywhere, then run the installer. It is
idempotent and safe to re-run:

```bash
git clone <this-repo> ~/local_code/local_web_search
cd ~/local_code/local_web_search
./install.sh
```

What `install.sh` does (delegates to `scripts/local-search install`):

1. Clones/updates SearXNG source into `searxng/src` at the pinned ref.
2. Builds the SearXNG venv (`uv venv` + `requirements.txt` + editable install).
3. Builds the MCP websearch venv (`uv sync`).
4. Creates the private telemetry/log directories and writes mode-`0600`
   launchd plists (`com.local.searxng`, `com.local.mcp-websearch`) into
   `~/Library/LaunchAgents/`, with mode-`0600` logs under
   `~/Library/Logs/local-search/` and launchd `Umask 077`.
5. Starts services (SearXNG first, then MCP) and runs health verification.

Flags / non-secret policy overrides:

```bash
./install.sh --no-start                          # bootstrap without starting
LOCAL_SEARCH_LOG_DIR=~/logs ./install.sh         # custom log dir
LOCAL_SEARCH_DATA_DIR=/private/path ./install.sh # custom telemetry directory
WEBSEARCH_TAVILY_MODE=disabled ./install.sh      # SearXNG-only privacy mode
WEBSEARCH_TAVILY_MODE=supplement \
  WEBSEARCH_SUPPLEMENT_MIN_RESULTS=5 ./install.sh
```

Installed policy defaults are `18s` total, `7s` SearXNG, and `8s` Tavily.
Provider search responses default to a 2 MiB cap (`WEBSEARCH_SEARCH_MAX_BYTES`).
`WEBSEARCH_TOTAL_TIMEOUT` is hard-capped at 18 seconds so Pi's 20-second MCP
budget retains transport/serialization margin. `WEBSEARCH_SEARCH_TIMEOUT`
remains a deprecated alias for `WEBSEARCH_SEARXNG_TIMEOUT`.

### Provider stack selection (ADR 0002)

`WEBSEARCH_PROVIDER_STACK` selects the active search stack (default
`searxng+tavily`, preserving legacy behavior):

| Stack | Primary | Fallback | Notes |
|---|---|---|---|
| `searxng+tavily` | SearXNG | Tavily | Default; unchanged from pre-ADR behavior. |
| `searxng` | SearXNG | none | SearXNG-only. |
| `kagi` | Kagi | none | Kagi raw search; requires a Kagi key. |
| `kagi+sonar` | Kagi | Perplexity Sonar | ADR target stack; quality-gated fallback to Sonar. |

Credentials (manual bootstrap per ADR 0002; never stored in the repo or
launchd plists):

- **Kagi**: `X-Kagi-Key` header (per-call) → `KAGI_API_KEY` env → mode-`0600`
  file at `$LOCAL_SEARCH_DATA_DIR/kagi_key`. Generate the key in the Kagi API
  Portal (`help.kagi.com/kagi/api/overview.html`).
- **Perplexity Sonar**: `X-Perplexity-Key` header → `PERPLEXITY_API_KEY` env →
  mode-`0600` file at `$LOCAL_SEARCH_DATA_DIR/perplexity_key`. Create the API
  group and first key at `console.perplexity.ai`.

Secret files must be owner-only (`chmod 0600`); group/world-readable files are
ignored. After onboarding both keys, switch the stack:

```bash
WEBSEARCH_PROVIDER_STACK=kagi+sonar ./install.sh
```

The quality gate (`WEBSEARCH_QUALITY_GATE=auto`) is enabled automatically for
non-legacy stacks; it falls back to Sonar when Kagi returns thin, single-domain,
or duplicate-dominated results. `WEBSEARCH_QUALITY_MIN_RESULTS` (default 3) and
`WEBSEARCH_QUALITY_MIN_DOMAINS` (default 2) tune the gate.

Routing modes (`web_search` `mode` argument or `WEBSEARCH_SEARCH_MODE`):
`normal` (default), `sensitive` (no external egress; refuses without a local
corpus — SearXNG is not no-egress), `maximum_recall` (opt-in serial escalation
across all configured providers). The `answer_search` tool calls Sonar directly
for a grounded answer with citations.

## Operator CLI

All day-to-day operations go through `scripts/local-search`:

```bash
local-search install [--no-start]    bootstrap + start + verify
local-search uninstall               stop services, remove plists (keeps src/venvs)
local-search start | stop | restart  service control (ordered: searxng before mcp)
local-search status                  launchd state + /health probes
local-search stats [24h|7d|30d]      query-free telemetry aggregates
local-search telemetry-reset --yes   explicitly erase telemetry events
local-search verify                  health + tools/list smoke
local-search logs [searxng|mcp] [-f|N]
local-search rotate-logs --yes       privately archive logs without deleting them
local-search update                  git pull + rebuild venvs + restart + verify
local-search update-searxng-ref <sha>   pin a new SearXNG commit
local-search mcp-stdio               run MCP in stdio mode (for client config)
local-search env                     print resolved config
```

Install a `local-search` shim on PATH (optional):

```bash
ln -s ~/local_code/local_web_search/scripts/local-search ~/.local/bin/local-search
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
      "command": "/Users/<you>/local_code/local_web_search/mcp-websearch/.venv/bin/python",
      "args": ["/Users/<you>/local_code/local_web_search/mcp-websearch/server.py"]
    }
  }
}
```

HTTP transport (any MCP client): `http://127.0.0.1:8889/mcp`.

### Remote clients

The supported remote architecture is a persistent SSH local forward. The broker
stays bound to server loopback, and the remote client still uses exactly:

```text
http://127.0.0.1:8889/mcp
```

Use [`scripts/local-search-tunnel`](scripts/local-search-tunnel) on a client Mac.
It creates a secret-free, private LaunchAgent and does not edit Pi or another
client's configuration. The complete key restrictions, threat model, install,
status, verify, log rotation, and uninstall procedure is in
[`docs/remote-mcp-over-ssh.md`](docs/remote-mcp-over-ssh.md). The architecture
comparison and direct-exposure prerequisites are recorded in
[ADR 0001](docs/adr/0001-loopback-mcp-over-ssh.md).

Do not proxy MCP through Caddy, bind it to a LAN/tailnet interface, or add a
public endpoint. A Tailscale address may transport SSH, but SSH remains the
client authentication boundary.

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
  Each redirect hop connects to the exact validated IP while preserving the
  original HTTP Host and TLS SNI. A fresh one-hop HTTP client prevents TLS
  connection reuse across different hostnames that share an IP, closing both
  DNS-rebinding and cross-host certificate-validation gaps.
- **Readable bounded fetches**: responses are byte-capped; already-downloaded
  HTML is reduced to its main content with `readability-lxml`, with the original
  bounded parser as a fallback and omitted document/archive attachments appended
  inside the output limit. Untrusted parsing runs in a concurrency-limited child
  process with CPU, memory, file, output, admission, queue-wait, and wall-clock
  bounds; excess extraction work fails fast instead of building an unbounded
  queue. The extractor performs no network access. Text PDFs use Poppler; scanned
  PDFs use bounded macOS Vision OCR when available. Unsupported binary data is
  never presented as successfully read text.
- **Bounded search**: one total deadline contains SearXNG retries and Tavily
  fallback; each query is capped at 512 characters and each provider JSON body
  is stream-capped; timeout and circuit states remain visible in result metadata.
  Batch search shares that same deadline across at most three queries with two
  active provider pipelines. Image search uses the SearXNG portion of the same
  deadline and has no Tavily path.
- **Consistent states**: searches distinguish `ok`, `empty`, `degraded`, and
  terminal `error`; fetch errors retain the stable `Fetch error:` prefix.
- **Health separation**: `/live` is dependency-free; `/ready` returns 503 only
  when no policy-allowed backend is usable; `/health` remains HTTP 200 for
  compatibility and exposes safe breaker/policy/last-search metadata, including
  recent per-provider outcomes, without retaining query text, URLs, headers, or keys.
- **Private telemetry**: `/stats` is loopback-only and aggregate-only. SQLite
  stores no query/result/credential data, uses a private data directory and
  file permissions, and fails open so monitoring cannot break search.
- **Local binding**: SearXNG and the MCP HTTP broker listen on loopback only.
  HTTP requests also require an exact loopback `Host`; a supplied browser
  `Origin` must be loopback.
- **Query-log hygiene**: broker HTTP client logging suppresses full request
  URLs, and the tracked SearXNG runner redacts every URL query value plus known
  query-bearing summary paths before operational messages are written. Use
  `local-search rotate-logs --yes` to preserve old logs in a private archive and
  start clean files; archives are never deleted automatically.
- **Dependency-ordered restart**: MCP is stopped before SearXNG and started after
  SearXNG is healthy; ports are checked free to avoid bind races.
- **KeepAlive**: both services auto-restart on crash (`ThrottleInterval` 5s).

## Tests

```bash
cd mcp-websearch && uv run pytest -q
```

Covers the SSRF rejection matrix, DNS-pinned fetches, `web_fetch` errors,
isolated main-content extraction and fallback, truncation-safe attachment
retention, compatibility payloads, Tavily
disabled/fallback/supplement policy, overfetch/dedupe, bounded stage timeouts,
distinct empty/degraded/error states, readiness, safe last-search metadata,
single-probe half-open circuits, key separation, and `tools/list`. Focused batch
tests cover validation, normalization/dedupe, ordered compact results,
concurrency, shared deadlines, partial timeouts, cancellation, provider-policy
reuse, and query-free telemetry.
Focused image tests cover category/SafeSearch request parameters, URL and field
normalization, canonical-image dedupe/limits, empty/degraded/error states,
loopback enforcement, no-Tavily behavior, and query/result-free telemetry.
Telemetry tests cover persistence across reopen, aggregation windows, schema and
row privacy, concurrent writes, invalid windows, unavailable/disabled storage,
file permissions, HTTP 429/circuit/fallback counts, and explicit reset. Tunnel
helper tests validate exact SSH arguments, plist structure, idempotence, and
private permissions; HTTP tests cover Host/Origin rejection and the loopback
bind regression.

The optional synthetic provider smoke is reproducible and bypasses production
telemetry. It uses Tavily keyless unless `TAVILY_API_KEY` is set and adds
Perplexity Search only when `PERPLEXITY_API_KEY` is set:

```bash
cd mcp-websearch
uv run python ../benchmarks/provider_smoke.py
```

## Layout

```
local_web_search/
├── install.sh                    # bootstrap entrypoint (→ scripts/local-search install)
├── data/
│   └── README.md                 # private telemetry location (SQLite files ignored)
├── docs/
│   ├── remote-mcp-over-ssh.md     # remote client setup and operations
│   └── adr/0001-loopback-mcp-over-ssh.md
├── scripts/
│   ├── local-search              # server operator CLI
│   └── local-search-tunnel       # macOS remote-client SSH LaunchAgent helper
├── mcp-websearch/
│   ├── server.py                 # MCP single/batch/image/fetch tools, policy, and HTTP routes
│   ├── html_extraction.py        # offline resource-limited HTML extraction child
│   ├── telemetry.py              # query-free SQLite events, aggregates, and reset
│   ├── macos_vision_ocr.swift    # optional scanned-PDF OCR helper
│   ├── http_server.py            # HTTP transport entrypoint (uvicorn)
│   ├── pyproject.toml            # uv project (fastmcp, httpx, readability; dev: pytest)
│   └── tests/                    # broker and telemetry tests
└── searxng/
    ├── engines/mwmbl_safe.py     # Defensive keyless Mwmbl JSON adapter
    ├── run.py                    # SearXNG entry point with local engines + log redaction
    ├── settings.yml              # SearXNG config (engines, bind, json format)
    ├── SEARXNG_REF               # pinned SearXNG commit for reproducible installs
    └── src/                      # SearXNG checkout + venv (gitignored, bootstrapped)
```

## Uninstall

```bash
local-search uninstall          # stops services + removes plists; keeps telemetry/src/venvs
rm -rf ~/local_code/local_web_search  # full removal, including repository-local telemetry
```
