# Proxy & Hardening Roadmap

**Last updated:** 2026-07-24
**Status:** Free hardening, cache, and dual-stack shadow are implemented; paid proxy deferred pending measurement

> **Implementation-status amendment (2026-07-24):** The panel findings below
> describe the pre-hardening baseline. Fetch telemetry, private caching,
> raw-byte fetch limits with encoded-response rejection, SearXNG secret
> injection, and `searxng+brave` shadow routing now exist. Shadow remains
> evaluation-only; promotion requires numeric
> result/domain parity evidence as well as fallback-rate data.

## How we got here

SearXNG degraded every few days from home-IP reputation decay on scrape engines.
A multi-model panel review (Codex 5.6, Opus 5, Kimi K3) independently concluded
the paid proxy (IPRoyal Web Unblocker) is **step 6, not step 1** — the free fixes
and measurement must come first, and the $13/month Brave baseline may be inflated.

### Key panel findings that changed the plan

| Finding | Source | Impact |
|---|---|---|
| Bing has **0 failures** in telemetry (1,046 events, 12 days) — all degradation was from already-removed engines | Opus 5 | Proxy may solve a problem that doesn't exist post-cleanup |
| Pre-hardening `web_fetch` fingerprint likely contributed to 403s (bot-like UA, no `sec-fetch-*`, HTTP/1.1); the panel also criticized `Accept-Encoding: identity` | Opus 5 | Browser-like headers/HTTP2 were adopted, but identity encoding is now intentional so the raw-byte cap cannot be bypassed by decompression |
| Actual Brave spend is ~$3–10/month, not $13 (telemetry shows 70+20+24 calls on 7/22–24) | Opus 5 | Savings ceiling is ~$3–5/month, not ~$8 |
| `searxng/settings.yml` is git-tracked with a committed `secret_key` | Kimi K3 | Real security issue, fix before adding any proxy creds |
| Per-engine SearXNG `network` is all-or-nothing (every request proxied, not just failures) | Codex 5.6 | Can't do direct→proxy fallback per-engine without duplicate engine defs |
| No fetch telemetry table exists | Opus 5 | Can't measure 403 rate or pilot results without instrumentation |
| SearXNG isn't the primary provider yet (Brave is default) | Kimi K3 | Paying to stabilize a provider nothing routes through is premature |
| IPRoyal Web Unblocker is more invasive than a dumb proxy (does fingerprinting/MITM to solve CAPTCHAs) | Codex 5.6 | Privacy claim needs verification against IPRoyal's logging/retention policy |

## Phase 0: Free and immediate fixes (implemented)

**Status:** Implemented without a new vendor or paid service.

| Fix | Cost | Why |
|---|---|---|
| web_fetch: modern Chrome headers + HTTP/2; request identity encoding and reject encoded responses before raw streaming | $0 | Improve the request fingerprint without allowing pre-cap decompression allocation |
| settings.yml: move `secret_key` to gitignored secret file (env injection) | $0 | Security: committed secret in git-tracked file |
| Telemetry: add `fetch_events` table (host, status, outcome, tier, bytes, latency) | $0 | No fetch measurement plane exists — prerequisite for any pilot |
| Verify actual Brave spend from telemetry | $0 | Baseline may be ~$3–10/mo not $13 |

**Expected outcome:** web_fetch success rate jumps from ~85% to ~93–95% for $0.
If this holds, the browser fallback (currently ~13% of fetches) shrinks to ~5%,
and the paid proxy case for fetch may evaporate entirely.

## Phase 1: Central web cache (implemented)

**Status:** Implemented in `mcp-websearch/cache.py`.

- SQLite (WAL) for metadata + filesystem blobs for page content
- `data/cache/cache.sqlite3` + `data/cache/content/<hash_prefix>/<hash>`
- Search results: 2h general TTL; 30m current/news TTL. Web content: 1–30d by content type.
- Privacy: cache search keys are SHA-256 digests; cache entries may store result/content URLs. Search/parity telemetry stores no identifiers, while fetch telemetry stores only normalized destination hostnames.
- **25% cache hit rate → about $3.25/month saved from the $13 planning baseline, leaving about $9.75/month** (or about $0.75–$2.50 saved if the baseline is $3–$10)

**Why before proxy:** Shrinks total request volume, making every downstream
provider (SearXNG, Brave, proxy) cheaper. The cache is free and benefits all
providers regardless of which is primary.

## Phase 2: SearXNG-first + Brave quality-gate fallback (shadow evaluation)

**Status:** Dual-stack routing and `auto|on|off|shadow` modes are implemented;
shadow serves Brave when usable and is not a production promotion.

- `WEBSEARCH_PROVIDER_STACK=searxng+brave` — SearXNG primary, Brave policy-controlled reference/fallback
- The quality gate is implemented: <3 results, <2 domains, duplicate-dominated, or critical Bing unavailable → escalate in auto/on; shadow records the same decision while Brave serves when usable
- Freshness is separate and explicit: `current`/`news` intents map to provider freshness filters rather than a gate heuristic
- Schema-v5 telemetry compares deduped top-k URL/domain counts without storing identifiers

**Why before proxy:** Kimi K3 is right — paying $2.60/month to stabilize a provider
nothing routes through is premature. Flip SearXNG to primary first, measure for a
week. If Bing holds (telemetry says it has 0 failures post-cleanup), the proxy
may never be needed.

## Phase 3: Measure (1 week, free)

With Phase 0–2 implemented, observe:

| Metric | How | Decision |
|---|---|---|
| SearXNG success rate and parity | `search_events`, `provider_events`, `shadow.parity` stats | Promotion requires acceptable fallback plus URL/domain coverage; proxy decisions still depend on reliability |
| Bing engine failures | `engine_failures` table | 0 failures → no proxy needed; recurring → proxy candidate |
| web_fetch 403 rate by domain | new `fetch_events` table | <5% → done; >10% → consider header improvements or proxy |
| Actual Brave spend | `provider_events` × $0.005 | Confirms monthly cost baseline |
| web_fetch latency p95 | `fetch_events.latency_ms` | Must stay <5s for good UX |

**Only if Bing starts failing OR web_fetch 403 rate stays >10% after header fix → proceed to Phase 4.**

## Phase 4: Free Mullvad experiment (if measurement shows degradation)

**Status:** Proposed by Kimi K3. Not built.

You already pay for Mullvad (via Tailscale integration). Test before buying IPRoyal:

- Run a `gluetun` (Mullvad) container sharing its network namespace with SearXNG
- Per-process egress rotation at $0 — no system-wide Tailscale exit node needed
- Datacenter IPs are CAPTCHA-prone on Google, but **Bing is more tolerant**
- 1-week test: if Mullvad holds Bing for 7+ days, the IPRoyal line item disappears

**Why before IPRoyal:** Free, uses something you already pay for. If it works,
no new vendor, no new cost, no new privacy exposure.

## Phase 5: IPRoyal Web Unblocker (paid, last resort)

**Status:** Designed, deferred. Only if Phase 3 measurement shows Bing degradation
AND Phase 4 Mullvad experiment fails.

### If needed: Bing-only pilot

```
SearXNG outgoing.networks.unblocker:
  proxies: all://: http://user:pass@unblocker.iproyal.com:12323
  extra_proxy_timeout: 8.0

engines:
  - name: bing
    network: unblocker        # only scrape engine proxied
  - name: mwmbl               # keyless API — direct
  - name: wikipedia           # keyless API — direct
  - name: github              # keyless API — direct
  - name: arxiv               # keyless API — direct
```

**Cost:** Bing-only = ~2,610 req/mo = ~$2.61/month. DO NOT re-enable all 6
scrape engines (6 engines × 2,610 × $0.001 = $15.66/month, exceeding Brave).

### web_fetch proxy parameter (if header fix insufficient)

Add `proxy: bool = false` parameter to `web_fetch`. When true, routes through
Web Unblocker. Agent-driven escalation:

```
1. web_fetch(url)              → direct httpx (free)         85-95%
2. browser_*(url)              → real Chromium (free, Pi)    +5-13%
3. web_fetch(url, proxy=true)  → Web Unblocker (~$0.001)     +1-2% last resort
```

**Why browser before proxy:** Real Chromium is more capable than Web Unblocker
at anti-bot (real TLS, real JS, real cookies) except for IP rotation. Web Unblocker
only wins when the home IP itself is blocklisted.

### What NOT to do

- ❌ Re-enable Google/DuckDuckGo through proxy — feeds queries to ad-tech, cost exceeds Brave
- ❌ Embed Chromium in the broker — contradicts roadmap Phase 5, 200-500MB/instance
- ❌ Use a single Mullvad SOCKS5 as a "fix" — relocates decay, doesn't solve it
- ❌ Put proxy credentials in git-tracked settings.yml — use the secret file pattern

### Vendor details (if Phase 5 is reached)

| Vendor | Product | Pricing | Personal-friendly |
|---|---|---|---|
| **IPRoyal** | Web Unblocker | $1.00/1k requests | ✅ No KYC |
| DataImpulse | Residential proxy | $1/GB | ✅ No KYC, but less anti-bot capability |
| IPRoyal | Raw residential | $1.75/GB | ✅ No KYC, but doesn't handle CAPTCHAs |
| Bright Data | Residential | $4-8/GB | ⚠️ KYC required |

**Privacy caveat (Codex 5.6):** Web Unblocker does fingerprinting/MITM to solve
CAPTCHAs — it may see full URLs, queries, and page content, not just destination
hosts. Verify IPRoyal's logging/retention policy before relying on the split-trust
privacy model. A dumb CONNECT proxy only sees SNI; Web Unblocker sees more.

## Decision tree

```
Phase 0 (free fixes) ──→ Phase 1 (cache) ──→ Phase 2 (SearXNG-first) ──→ Phase 3 (measure 1 week)
                                                                              │
                                                    ┌─────────────────────────┴─────────────────────────┐
                                                    │                                                   │
                                            Bing holds (0 failures)                           Bing degrades
                                            web_fetch <5% 403s                                or 403s >10%
                                                    │                                                   │
                                                    ▼                                                   ▼
                                            DONE. ~$3-8/mo total.                          Phase 4: free Mullvad test (1 week)
                                            No proxy needed.                                      │
                                                                                          ┌─────────────┴─────────────┐
                                                                                          │                           │
                                                                                    Mullvad holds               Mullvad fails
                                                                                          │                           │
                                                                                          ▼                           ▼
                                                                                    DONE. $0 proxy.            Phase 5: IPRoyal pilot
                                                                                    Bing via Mullvad.          Bing-only, ~$2.60/mo
                                                                                                               + web_fetch proxy param
```

## Files to touch (when each phase is reached)

| Phase | Files | New? |
|---|---|---|
| 0 | mcp-websearch/server.py, mcp-websearch/telemetry.py, searxng/settings.yml, scripts/local-search, .gitignore | edits |
| 1 | mcp-websearch/cache.py, data/cache/ | new |
| 2 | mcp-websearch/server.py (`_build_provider_stack`), data/install.env | edits |
| 3 | docs/measurement-report.md (from telemetry queries) | new |
| 4 | docker-compose or gluetun config, scripts/local-search | new |
| 5 | searxng/settings.yml (networks), mcp-websearch/server.py (proxy param), data/iproyal_unblocker_url | edits + new secret |
