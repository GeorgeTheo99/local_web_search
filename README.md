# local_web_search

Local-first web search and page retrieval for macOS (loopback-only by default):

- **FastMCP broker** on `http://127.0.0.1:8889/mcp`
- **Brave Search API** for ranked web and image search
- **Direct-first page retrieval** with optional Decodo and Jina recovery
- **launchd operator CLI** for install, health, logs, telemetry, and upgrades

The broker is shared by Pi and my-ai. It returns structured ranked results,
query-free operational metadata, and estimated search-provider cost.

## Architecture

```text
MCP client
  │
  └── 127.0.0.1:8889/mcp
        ├── web_search ───── Brave Search API
        ├── batch_web_search Brave, bounded concurrency
        ├── image_search ─── Brave Images API
        ├── web_fetch ────── cache → direct → Decodo → Jina
        └── verify_url ───── same bounded fetch path with provider attribution
```

Brave is the only search provider. SearXNG was retired; old
`WEBSEARCH_PROVIDER_STACK` values are ignored by the broker and normalized to
`brave` by the operator CLI. Historical comparison documents remain evidence,
not active configuration.

## Quick start

Prerequisites: macOS, `bash`, `curl`, `git`, `launchctl`, Python 3, and
[`uv`](https://docs.astral.sh/uv/).

The default Brave stack requires its owner-only key **before** the first
started install. The installer fails fast with an actionable message when the
key is absent or has unsafe permissions.

```bash
cd ~/local_code/local_web_search
mkdir -p data && chmod 700 data
umask 077
read -r -s -p 'Brave API key: ' BRAVE_KEY; printf '\n'
printf '%s\n' "$BRAVE_KEY" > data/brave_key
unset BRAVE_KEY
chmod 600 data/brave_key
./install.sh
```

`install.sh` delegates to `scripts/local-search install`. It validates the
Brave credential and optional Decodo credential, synchronizes the Python
environment, installs the launchd plist, removes any retired SearXNG service
artifact, starts the MCP broker, and verifies health plus the exact tool list.

When upgrading an installation created before the Brave-only operator rewrite,
run the pull and install as separate commands so the newly pulled script—not the
already parsed legacy shell process—performs the migration:

```bash
git pull --ff-only
scripts/local-search install
```

Subsequent `scripts/local-search update` calls re-exec the newly pulled script
after each pull.

### Maintainer Git and runtime topology

On the maintainer server, `~/repos/local_web_search.git` is the authoritative
local bare repository. The development checkout keeps `origin` pointed at that
bare repository and a separate `github` remote for the public mirror:

```bash
git fetch github && git merge github/main   # only if the push was rejected
git push origin main    # update the bare repository and publish to GitHub
```

The bare repository's github-sync hooks (`infra/local-ci` `github-sync/`) reject
a `main` push that is missing commits already on GitHub, then publish the
accepted tip. GitHub outages only warn; if publishing failed, run
`git push github main` once GitHub is reachable. Other branches and tags are
published manually. Verify synchronization with
`git rev-parse HEAD origin/main github/main`.

The maintainer's Homebrew-managed LaunchAgent runs from the installed module at
`~/.local/share/pi-shared/modules/local_web_search`, not the development checkout.
Publish approved source changes, then use `pi-shared update --modules-only` to
install them; do not edit installed copies. Pushing a remote alone does not
restart the service. For standalone clones, use `scripts/local-search update`.
Use the installed operator's `install` when persistent configuration changes.

Secret scanning is enforced in three layers: tracked pre-commit/pre-push hooks,
the authoritative bare repository's pre-receive hook, and the pinned GitHub
Gitleaks workflow. Install Gitleaks and activate the worktree hooks once per
clone:

```bash
brew install gitleaks
git config core.hooksPath .githooks
```

Do not bypass failed scans. `.gitleaksignore` contains only an exact fingerprint
for a synthetic credential fixture, not a file-wide exception.

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

## Decodo credential

Decodo is the primary remote fallback for public HTML pages that fail the
direct fetch with a recoverable error: `403`/`408`/`429`, recoverable edge
statuses (`500`/`502`/`503`/`504` and CDN `520`–`524`/`527`), an empty or
anti-bot/challenge response, a direct timeout, a connection/TLS failure, or an
HTML extraction failure. It uses the unified Web Scraping API in synchronous
Universal/Web mode with the premium proxy pool, JavaScript rendering, and
Markdown output. The fallback is enabled only when an owner-only Web Scraping
API authorization token exists at `$LOCAL_SEARCH_DATA_DIR/decodo_key`. Copy
only the token value after `Basic` from Decodo's Web Scraping API
Playground-generated `Authorization` header—not the `Basic ` prefix, a generic
Decodo API key, or another product's credential:

```bash
mkdir -p data && chmod 700 data
umask 077
read -r -s -p 'Decodo Web Scraping token (value after Basic): ' DECODO_KEY; printf '\n'
printf '%s\n' "$DECODO_KEY" > data/decodo_key
unset DECODO_KEY
chmod 600 data/decodo_key
scripts/local-search restart
curl -fsS http://127.0.0.1:8889/health | python3 -m json.tool
```

The token is file-only: it is never written to launchd plists, process
arguments, MCP request headers, logs, telemetry, or operator output. Decodo
receives the requested public URL and returned page content. Caller headers,
cookies, and credentials are never forwarded.

## MCP tools

### `web_search(query, num_results=8, mode=None, intent="general")`

Searches Brave. Queries are limited to 512 characters and
results to 20. Provider responses, retries, and total latency are bounded.
`intent` must be explicit: `general` uses no freshness filter, `current` uses a
recent window, and `news` uses the narrowest freshness window. Intent is never
inferred from query text. `batch_web_search` accepts the same three intents.

Routing modes:

| Mode | Behavior |
|---|---|
| `normal` | Query Brave |
| `sensitive` | Make no external request; return a structured refusal because no local corpus is configured |
| `maximum_recall` | Query the complete configured provider inventory—which is currently Brave only |

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

Uses Brave Images with `safesearch=strict`. The response includes the actual
backend, request attempts, safety policy, and estimated cost.

### `web_fetch(url, max_chars=20000)`

Fetches a public HTTP(S) URL with DNS/IP validation, connection pinning,
redirect revalidation, response-size limits, and isolated HTML/PDF extraction.
Loopback, private, link-local, and other non-public destinations are rejected.
The default 20 MiB body cap is enforced before reading a valid oversized
`Content-Length` and again while streaming raw bytes, so chunked or misdeclared
responses cannot bypass it. Fetches request `Accept-Encoding: identity` and
reject encoded responses before reading their bodies.

After public-URL validation, recoverable direct-fetch failures escalate to
Decodo when configured and then to Jina Reader as a final fallback, in that
order for every eligible trigger. Eligible triggers are `403`/`408`/`429`,
recoverable edge statuses (`500`/`502`/`503`/`504` and CDN `520`–`524`/`527`),
empty or anti-bot/challenge HTML, direct timeouts, connection/TLS failures,
and HTML extraction failures. URLs that never pass DNS/IP validation
(private/loopback/credential-bearing/unresolvable) fail closed and are never
proxied. Deterministic client errors (`400`/`401`/`404`/`405`/`410`/`422`),
oversize/encoding/redirect policy failures, PDFs, and binary responses never
leave the direct path. The direct fetch, Decodo, and Jina tiers share one
60-second operation deadline (direct 20s → Decodo 25s → Jina up to the
remaining ~15s by default; env-tunable). Successful fallback Markdown is
cached. Structured output includes `fetch_provider` (`cache`, `direct`,
`decodo`, or `jina`).

HTML extraction excludes title, script, style, and noscript text. Empty extracted bodies and
recognized whole-output loading/JavaScript placeholders are extraction failures,
not successful fetches, and use the same bounded fallback path. Short HTML body
text remains valid; there is no minimum body or extracted-text length. Validation
runs before caller truncation and cache writes. This detects common empty shells,
not every low-quality page or whether content answers a particular question.

### `verify_url(url)`

Runs the same cached and bounded fetch path as `web_fetch`, including configured
Decodo/Jina fallbacks, and reports the actual `method`/`fetch_provider`.

## Health and telemetry

| Endpoint | Purpose |
|---|---|
| `GET /ui` | Loopback browser dashboard: health, attention items, fetch-tier and search observability, recent failed fetch hosts, and resolved config |
| `GET /live` | Dependency-free process liveness |
| `GET /ready` | Brave readiness; HTTP 503 when its credential or circuit is unavailable |
| `GET /health` | Compatibility diagnostics; always HTTP 200 |
| `GET /stats?window=24h` | Query-free aggregates; windows: `24h`, `7d`, `30d` |
| `GET /config` | Resolved non-secret configuration; credentials report presence only |
| `GET /activity?window=24h` | Bucketed search/fetch timeline plus the 25 most recent failed fetch attempts (hostnames only) |

Telemetry is enabled by default and stored in
`$LOCAL_SEARCH_DATA_DIR/telemetry.sqlite3`. Search telemetry stores bounded
operational fields only: status, provider, routing mode, counts, latency,
normalized failure reason, credential mode, and circuit state. It stores no
search/result identifiers: never queries, URLs, domains, hashes of URLs/domains,
titles, snippets, headers, result content, or credentials. Fetch telemetry
stores the normalized destination hostname plus bounded
outcome/status/size/latency/provider/trigger fields and one terminal operation
outcome; it never stores a full URL, path, or query. This makes direct, Decodo,
and Jina success rates measurable without correlating or retaining destination
paths.

`/stats` exposes numeric search/provider, cache, end-to-end fetch, and
fetch-attempt aggregates. Cache hits have no search-provider attempt or cost.
Open `http://127.0.0.1:8889/ui` for a read-only operational view of these metrics.
The UI uses relative URLs, so a reverse proxy can serve it under a path prefix
(the maintainer's directory exposes it at `/local-search/` through a GET-only
allowlist that rewrites `Host` to loopback and never forwards `/mcp`).
`/activity` is the only endpoint that returns destination hostnames.

The private cache lives under `$LOCAL_SEARCH_DATA_DIR/cache/`. Search keys are
query hashes and cached search payloads contain returned result data; fetched
content entries retain canonical URLs and extracted bodies. Cache hits emit
provider-free telemetry and report zero estimated provider cost.

Cache schema v2 adds an HTML extraction-version marker. Older HTML entries lack
body-validation evidence and are discarded individually when requested, then
re-fetched through the normal public-URL checks; no bulk cache reset is needed.
Newly validated short HTML is cacheable. Search entries and non-HTML content
(including fallback Markdown) are unaffected. Back up the cache with SQLite's
backup API before a runtime upgrade if a schema-v1 rollback is needed: an older
broker cannot open a v2 cache and will operate without caching until its v1 backup
is restored while the service is stopped.

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
| `WEBSEARCH_TOTAL_TIMEOUT` | `18` seconds, hard-capped at 18 |
| `WEBSEARCH_BRAVE_TIMEOUT` | `8` seconds |
| `WEBSEARCH_SEARCH_MAX_BYTES` | `2 MiB` per provider response |
| `WEBSEARCH_FETCH_MAX_BYTES` | `20 MiB` |
| `WEBSEARCH_FETCH_TIMEOUT` | `30` seconds |
| `DECODO_FALLBACK_ENABLED` | `true`; inert without `decodo_key` |
| `DECODO_TIMEOUT` | `30` seconds |
| `DECODO_RESPONSE_MAX_BYTES` | `5 MiB` |
| `WEBSEARCH_FETCH_OPERATION_TIMEOUT` | `60` seconds end-to-end |
| `JINA_FALLBACK_ENABLED` | `true` |
| `JINA_TIMEOUT` | `30` seconds |
| `JINA_RESPONSE_MAX_BYTES` | `5 MiB` |
| `LOCAL_SEARCH_DATA_DIR` | repository `data/` directory |
| `LOCAL_SEARCH_TELEMETRY_ENABLED` | `true` |
| `BRAVE_BASE_URL` | `https://api.search.brave.com` |
| `MCP_PORT` | `8889` |

Use `scripts/local-search env` to inspect resolved non-secret values. The
`install` command persists settings—including `MCP_PORT`—in the owner-only
`data/install.env`, rewrites the launchd plist, and starts it unless
`--no-start` is used. Fresh shell sessions and updates recover that persisted
port (or migrate it from the installed plist) before falling back to `8889`.
An explicit `MCP_PORT` environment value takes precedence when intentionally
re-running `install`. `restart` only restarts the already-installed plist; it
does **not** re-render changed shell variables or `data/install.env`. Run
`scripts/local-search install` after changing persistent configuration.

## Client configuration

Clients should use the endpoint, not repository paths:

```text
http://127.0.0.1:8889/mcp
```

For stdio clients:

```bash
scripts/local-search mcp-stdio
```

Services always bind to loopback. Remote access supports either:

- A persistent [SSH local forward](docs/remote-mcp-over-ssh.md) (no broker changes).
- Explicit [Tailscale HTTPS mode](docs/remote-mcp-over-tailnet.md): a separate
  MCP-only loopback ingress behind Tailscale Serve, restricted to one configured
  hostname. All devices permitted by your tailnet access rules are trusted;
  no API key is needed. Local diagnostics remain private.

Remote ingress is off by default (`MCP_TAILNET_HOST` empty). Do not publish either
listener through a public proxy, a LAN listener, or Tailscale Funnel.

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

Historical benchmark result files may contain retired provider names; they are
retained as immutable evidence, not active configuration.

## Security and reliability

- Loopback Host/Origin enforcement by default; opt-in MCP-only Tailscale ingress
  with an exact hostname and HTTPS Origin policy (never public Funnel)
- Public-address validation and DNS/IP pinning for fetches
- Redirect-chain revalidation
- Bounded provider JSON, fetch bodies, Decodo responses, extraction output, and subprocess stderr
- Direct-first Decodo/Jina escalation only after public-URL validation
- Circuit breakers and total-deadline enforcement
- Identifier-free search telemetry, host-only fetch telemetry, and URL-query log redaction
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
scripts/
  local-search           operator CLI
  local-search-tunnel    remote SSH forwarding helper
benchmarks/
  provider_smoke.py      historical provider comparison harness
  results/               historical evidence
docs/
  adr/                    architecture decisions
  provider-comparison.md current provider rationale
  roadmap.md             cost/reliability roadmap
```
