# Local telemetry data

`local-search` stores private operational telemetry here by default in
`telemetry.sqlite3`. The database and its SQLite WAL/SHM files are ignored by
Git; the installer and runtime restrict this directory to the current user.
A private, non-secret `install.env` remembers the selected telemetry path and
enabled state across update/uninstall/reinstall commands.

Telemetry contains only timestamps, latency, provider outcomes, aggregate
result counts, fallback/circuit activity, HTTP status categories, and
categorized SearXNG engine failures. It does **not** store search queries,
URLs, result titles/snippets/content, request headers, credentials, or API
keys.

- View aggregates: `scripts/local-search stats 24h` (also `7d` or `30d`)
- Explicitly erase telemetry: `scripts/local-search telemetry-reset --yes`

Install, update, and uninstall preserve this directory. Removing the checkout
manually also removes its local telemetry database.
