# Local runtime data

`local-search` stores private operational telemetry here by default in
`telemetry.sqlite3`. The database and its SQLite WAL/SHM files are ignored by
Git; the installer and runtime restrict this directory to the current user.
A private, non-secret `install.env` remembers the selected telemetry path,
enabled state, and non-secret fallback settings across update/uninstall/reinstall
commands. Optional provider credentials such as `decodo_key` are separate
owner-only files and are never copied into `install.env` or launchd plists.

Search telemetry contains only timestamps, latency, provider outcomes,
aggregate result counts, circuit activity, and HTTP status categories. It
stores no queries, URLs, domains, URL/domain hashes, titles, snippets/content,
request headers, credentials, or API keys. Fetch telemetry separately stores a
normalized destination hostname and bounded outcome/status/size/latency,
provider, and fallback-trigger fields; `fetch_operations` stores one terminal
outcome per fetch. It never stores a full URL, path, query, content, header, or
credential. Search cache hits have no provider-attempt row.

- View aggregates: `scripts/local-search stats 24h` (also `7d` or `30d`)
- Explicitly erase telemetry: `scripts/local-search telemetry-reset --yes`

The private cache is stored under `cache/`: `cache.sqlite3` holds metadata,
search result payloads, and canonical content URLs; extracted content is stored
under `cache/content/`. Search lookup keys are SHA-256 digests of normalized
queries, not query text. Cache data is operationally private but is not subject
to telemetry's identifier-free contract.

`install.env` is read by the operator CLI, but `restart` does not regenerate an
installed launchd plist. Run `scripts/local-search install` after changing
persisted configuration.

Install, update, and uninstall preserve this directory. Removing the checkout
manually also removes its local telemetry and cache data.
