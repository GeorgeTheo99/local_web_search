# Fetch Fallback Decision Record

**Last updated:** 2026-08-01
**Status:** Decodo pilot implemented; IPRoyal retired before deployment

## Current decision

`web_fetch` uses a direct-first recovery path:

```text
private cache
  → validated, DNS-pinned direct fetch
  → Decodo unified Web Scraping API (when an owner-only key is configured)
  → Jina Reader temporary final fallback
```

Decodo uses the synchronous Universal/Web scraper at
`https://scraper-api.decodo.com/v2/scrape` with:

- `proxy_pool: premium`
- JavaScript rendering (`headless: html`)
- Markdown output
- US geography and locale
- a bounded 25-second provider timeout, 5 MiB identity-response cap, and a
  shared 60-second end-to-end fetch deadline (direct 20s → Decodo 25s → Jina
  up to the remaining ~15s)

The fallback receives only a previously validated public URL. Caller headers,
cookies, credentials, and private-network destinations are never forwarded.
URLs that never pass DNS/IP validation fail closed and are never proxied.
Recoverable direct-fetch failures (recoverable HTTP statuses, empty/anti-bot
HTML, timeouts, connection/TLS errors, and HTML extraction failures) escalate
through Decodo then Jina; deterministic client errors, oversize/encoding/redirect
policy failures, PDFs, and binary responses remain on the direct path.

## Why Decodo Web Scraping API

The unified Web Scraping API matches the broker's page-retrieval contract: one
URL in, bounded readable content out. It manages proxy rotation, anti-bot
handling, rendering, and retries while returning Markdown suitable for the MCP
`web_fetch` response.

The following Decodo products are intentionally not integrated:

- **Site Unblocker:** proxy-style raw transport would duplicate extraction and
  response controls already owned by this broker.
- **Residential/ISP/mobile/datacenter proxies:** would require local rotation,
  retry, fingerprint, and rendering infrastructure.
- **Fast Search API:** Brave remains the search provider.
- **Specialized target templates:** unnecessary for arbitrary public URLs.
- **Decodo MCP:** would bypass this broker's cache, SSRF policy, telemetry, and
  stable local tool contract.
- **Legacy Core/Advanced scraper subscriptions:** superseded by Decodo's unified
  product.

## IPRoyal retirement

IPRoyal never reached runtime implementation. No IPRoyal credential, proxy,
launchd setting, client, or routing path was deployed. The old proposal was
therefore retired as documentation only; there is no operational infrastructure
to migrate or remove.

## Measurement

Telemetry schema v7 separates:

- `fetch_events`: provider attempts (`direct`, `decodo`, `jina`, `cache`) with a
  bounded trigger and outcome.
- `fetch_operations`: exactly one terminal host-only outcome for each fetch.

`GET /stats?window=24h|7d|30d` now reports end-to-end fetch success, provider
resolution, attempt success, latency, and cache rates. It retains only normalized
hostnames and bounded operational fields—never URL paths, queries, page content,
headers, or credentials.

Pilot decisions must use observed Decodo results rather than its advertised
success rate. Jina can be disabled after Decodo demonstrates materially better
recovery and acceptable latency on real fallback traffic.

## Secret handling

The Decodo token lives at `$LOCAL_SEARCH_DATA_DIR/decodo_key` with mode `0600`.
It is read per request and is never placed in environment variables, launchd
plists, process arguments, logs, telemetry, cache metadata, or MCP output.
