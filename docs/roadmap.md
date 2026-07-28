# Web Search Improvement Roadmap

**Last updated:** 2026-07-24
**Status:** Cache implemented; Brave-only search retained after SearXNG retirement

## Current state

- **Brave Search API** is the sole search provider ($0.005/issued request); image search also uses Brave with strict SafeSearch
- **SearXNG** was evaluated and retired: shadow comparisons showed 0% URL parity with Brave, frequent degradation, and negligible savings against the roughly $2.25/month Brave spend
- **MCP broker** (`local_web_search/mcp-websearch/server.py`) exposes `web_search`, `batch_web_search`, `image_search`, `web_fetch`, `verify_url` over MCP (HTTP + stdio)
- **Cost tracking** live: `estimated_cost_usd` in every search response, displayed as a badge in my-ai UI
- **Planning estimate:** ~87 searches/day and ~$13/month at Brave-only usage; remeasure from telemetry before using this for a budget decision
- **Informal smoke estimate:** `web_fetch` works for ~85% of sampled sites; 403s occur on anti-bot sites (Medium, Quora); browser fallback is handled by Pi's `browser_*` tools (not in broker)
- **Private broker caching is live** for search results and fetched/extracted content; search cache keys retain only query hashes
- Both **Pi** and **my-ai** route through the same MCP broker at `127.0.0.1:8889`

## Goals

1. **Minimize Brave costs** while maintaining search accuracy
2. **Retire evaluated providers** that do not meet reliability or parity targets
3. **Cache web content** to eliminate repeat network calls
4. **Handle 403-blocked sites** without embedding Chromium in the broker
5. **Keep the broker lightweight** and reliable

## Roadmap

### Phase 1: Central web cache (implemented)

**Status:** Implemented in `mcp-websearch/cache.py` with search/fetch integration and cache telemetry.

**What:** A persistent on-disk cache for both search results and fetched web content.

**Storage:**
- `data/cache/cache.sqlite3` — SQLite (WAL mode) for metadata (keys, TTLs, hit counts, eviction ordering)
- `data/cache/content/<hash_prefix>/<hash>` — filesystem blobs for web page content
- 50 GB size limit with LRU eviction

**Two cache layers:**

| Layer | Key | Size/entry | TTL | Saves |
|---|---|---|---|---|
| Search results | SHA-256 of normalized query | ~2-5 KB (JSON inline in SQLite) | 2 hours general; 30 min current/news | $0.005 per Brave hit avoided |
| Web content | SHA-256 of canonical URL | ~10-100 KB (filesystem blob) | 1-30 days (per content type) | Network latency + bandwidth |

**TTL by content type:**
- Search results (general): 2 hours
- Search results (news/current): 30 min
- Documentation sites (docs.python.org, MDN, etc.): 7 days
- General web pages: 24 hours
- GitHub/code: 1 day
- PDFs: 30 days

**Privacy:**
- Query text is never stored — only SHA-256 hashes
- URLs are stored (needed for revalidation)
- Cache directory is mode 0700, gitignored
- Search/parity telemetry records no identifiers; fetch telemetry retains only a normalized destination hostname, never a full URL/path/query

**Integration:**
- Transparent to tool API — no changes to `web_search`/`web_fetch` signatures
- Response payload gets `cache_hit: true/false` + `cache_age_seconds` fields
- Cache hits show `estimated_cost_usd: $0.000` in the UI badge
- Background cleanup task removes expired entries
- LRU eviction triggers when total content exceeds ~45 GB

**Module:** `mcp-websearch/cache.py` — standalone `WebCache` class, no broker dependency

**Estimated savings:** A 20-30% hit rate against the $13/month planning baseline saves about $2.60-$3.90/month, leaving about $9.10-$10.40/month.

**Tests:** `mcp-websearch/tests/test_cache.py` — hit/miss, TTL expiry, eviction, privacy, concurrent access

---

### Phase 2: SearXNG hardening (retired)

**Status:** Retired after evaluation showed frequent degradation and no meaningful cost benefit; no fallback hardening work is planned.

**2a. Redis/Valkey for limiter + caching**
- Add a Valkey sidecar (local, ~20 MB RAM)
- Configure `redis: url: valkey://127.0.0.1:6379/0` in SearXNG `settings.yml`
- Enables SearXNG's built-in bot limiter (protects upstream IP reputation)
- Note: SearXNG's Redis is for the limiter, not result caching — result caching is handled by Phase 1's broker-level cache

**2b. Aggressive engine suspension**
```yaml
search:
  ban_time_on_fail: 5
  max_ban_time_on_fail: 120
  suspended_times:
    SearxEngineAccessDenied: 86400  # 24h
    SearxEngineCaptcha: 86400       # 24h
```
- Prevents the "hammer a blocked engine" death spiral
- Engines that return CAPTCHA/access-denied are suspended for 24h

**2c. User-Agent suffix**
```yaml
outgoing:
  useragent_suffix: "contact: admin@localserver99"
```
- Some engines are more lenient with contact info in the UA

**2d. Update `scripts/local-search` to manage Valkey**
- Add Valkey to the install/start/stop/restart commands
- Add Valkey health check to `local-search status`

**Estimated impact:** SearXNG handles ~60-70% of queries successfully → Brave only called on 30-40% (the fallback cases)

---

### Phase 3: SearXNG-first with Brave fallback (retired)

**Status:** Retired after shadow evaluation found 0% URL parity with Brave and frequent degradation; the broker remains Brave-only.

**What:** Make SearXNG the primary search provider with Brave as quality-gated fallback.

**New stack:** `searxng+brave`
- `_build_provider_stack()` returns `[_SearXNGProvider(), _BraveProvider()]`
- SearXNG is tried first (free)
- If SearXNG returns no results, errors, or fails the quality gate (< 3 results, < 2 domains, duplicate-dominated), Brave is called ($0.005)
- The quality gate is implemented in `auto|on|off|shadow`; shadow currently records decisions/parity while Brave remains the serving reference when usable

**Promotion target:** `WEBSEARCH_PROVIDER_STACK=searxng+brave`; the code default remains `brave` during evaluation.

**Quality gate implementation:**
- `_quality_gate_enabled()` returns `True` when a fallback provider exists in `auto`/`on`; `shadow` evaluates the same checks without promoting SearXNG serving
- Gate checks: min results (3), min domains (2), duplicate fraction (0.6), and critical Bing availability
- Freshness is not a quality-gate heuristic: explicit `current`/`news` intent maps directly to each provider's freshness filter
- Gate evaluation is transient and query-free (never persists snippets/titles)

**Cost tracking:**
- SearXNG-only success: `estimated_cost_usd: $0.000`
- SearXNG fail + Brave fallback: `estimated_cost_usd: $0.005`
- UI badge shows the difference — users can see the savings

**Measurement period:**
- Before flipping the default, collect would-escalate decisions and schema-v5 parity samples
- Measure SearXNG success/fallback rate plus Brave-reference URL/domain coverage
- Promote only when both operational and parity evidence support it; fallback rate alone is insufficient

**Estimated savings (with Phase 1 + Phase 2):**

| Scenario | Brave calls/day | Monthly cost |
|---|---|---|
| Current (Brave only) | ~87 | ~$13 |
| + Phase 1 (cache) | ~61-70 | ~$9-10 |
| + Phase 2 (hardening only; Brave still serving) | ~61-70 | ~$9.10-10.40 |
| + Phase 3 (30-40% Brave fallback after cache) | ~18-28 | ~$2.75-4.60 |

---

### Phase 4: Model-level optimizations (free, reduces total search volume)

**What:** Guide models to prefer `web_fetch` over `web_search` when URLs are known.

**4a. Update my-ai system prompt:**
- "Prefer `web_fetch` when you already know the URL — it's free and gives full page content"
- "Use `web_search` only to discover URLs you don't already know"
- "Don't search for the same query twice in a conversation"

**4b. Update MCP tool descriptions:**
- `web_search`: "Searches the web with Brave. Prefer `web_fetch` when you know the URL."
- `web_fetch`: "Fetches full page content from a URL. Free and cached. Prefer this over `web_search` when you know the URL."

**4c. Explicit intent (implemented):**
- `web_search` and `batch_web_search` accept explicit `general`, `current`, and `news` intents
- Current/news apply explicit provider freshness filters; freshness is not a quality gate and no intent is inferred from query text
- Engine-specific code/docs routing remains future work

**Estimated impact:** ~30% reduction in total search volume (many searches are for pages whose URLs the model could know)

---

### Phase 5: Browser MCP for 403 fallback (separate service, future)

**What:** A separate MCP service running Playwright/Chromium for sites that block httpx.

**Why separate:**
- Chromium is heavy (~200-500 MB per instance)
- Browser crashes shouldn't affect the search broker
- Different deployment/restart cadence
- Pi already has `browser_*` tools — this is mainly for my-ai

**Architecture:**
```
MCP Broker (lightweight)              Browser MCP (heavy, separate launchd)
├── web_search → Brave                ├── browser_open
├── web_fetch → httpx + cache         ├── browser_navigate
├── image_search                      ├── browser_extract_text
└── verify_url                        └── browser_screenshot
                                      ↓
                                      writes to shared data/cache/
```

**Shared cache integration:**
- Browser MCP writes fetched content to the same `data/cache/` store
- A URL fetched via browser is cached by URL hash
- Next `web_fetch` for the same URL → cache hit (no browser needed)
- 403-blocked sites only need browser **once**, then cache serves forever (within TTL)

**When to build:** Only if my-ai needs browser fallback. Pi already has it. Low priority — 403s affect ~15% of sites and the model can usually find alternative sources.

---

### Phase 6: Brave free tier verification (quick win, potentially free)

**What:** Verify whether Brave offers a free tier that covers most of our usage.

**Kimi K3 noted:** Brave may offer 2,000 free queries/month (1 QPS). At ~2,600 queries/month, that would cover ~75%+ of volume at $0.

**Action items:**
- Check Brave's current pricing page and API dashboard for free tier terms
- If 2,000/month free exists: configure the broker to use the free tier first, pay only for overage
- Combined with Phase 1-3, total cost could drop to **$0-2/month**

---

## Priority order

| Priority | Phase | Effort | Cost savings | Status |
|---|---|---|---|---|
| 1 | Phase 1: Central web cache | Implemented | 20-30% estimate | Complete |
| 2 | Phase 6: Brave free tier check | 30 min research | Potentially 75%+ | Ready to verify |
| 3 | Phase 2: SearXNG hardening | Retired | — | Retired |
| 4 | Phase 3: SearXNG-first fallback | Retired | — | Retired |
| 5 | Phase 4: Model-level optimizations | Prompt changes | ~30% volume reduction | Ready to build |
| 6 | Phase 5: Browser MCP | ~2 days | No direct cost savings | Future, low priority |

## What we're NOT doing

- **Brave inside SearXNG** — SearXNG queries all engines in parallel; can't do conditional fallback. Would pay $0.005 on every search regardless of SearXNG results. (Rejected per Kimi K3 analysis.)
- **Chromium in the MCP broker** — too heavy, reliability risk. Browser stays separate.
- **Separate cache for browser** — one shared cache keyed by URL. A URL is a URL regardless of fetch method.
- **Tavily/Kagi/Sonar** — completely removed. Not coming back.
