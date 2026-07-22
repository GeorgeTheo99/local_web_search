# ADR 0002: Search architecture redesign — Kagi + Sonar, quality-gated routing

- **Status:** Proposed (awaiting approval)
- **Date:** 2026-07-21

## Context

The broker (`mcp-websearch/server.py`) is SearXNG-first with hard-coded Tavily
fallback/supplement. Two problems motivate this redesign:

1. **Privacy.** Tavily's standard terms permit broad use of submitted content
   and are account-linked; SearXNG sends queries to upstream engines under the
   household public IP and is not a true no-egress mode. The user wants at most
   **two external API providers/keys**, no identifiable search histories, and no
   query-content training, while retaining reliable agentic web search.

2. **Reliability / quality gate.** Fallback currently fires only when SearXNG
   returns **zero** deduped results (`fallback` mode) or is below a count
   threshold (`supplement` mode). `state == "degraded"` is set solely from
   `unresponsive_engines` being non-empty — it does **not** reflect result
   quality. Generic/Bing-only output with all engines "responsive" is treated as
   `ok` and returned with no fallback. This is the single most important
   correctness gap: "HTTP success + nonempty results" blocks escalation on
   stale, thin, or generic output.

The previous session surveyed vendors and produced a tentative two-key stack.
This ADR finalizes the design. No code has been changed yet.

## Decision

Adopt a **Kagi + Perplexity Sonar** two-key architecture:

- **Kagi Search API** — default raw ranked-search provider.
- **Perplexity Sonar** — quality-gated fallback and explicit answer/research
  route; returns a generated answer plus a `search_results` array.
- **Local fetch + browser extraction** — retrieval/verification layer after
  search discovery; not a routine consumer-search fallback.
- **SearXNG** — optional best-effort/keyless lane, off the reliability-critical
  path; retained only as an experimental or no-budget mode, not a privacy mode.
- **Tavily** — removed from the reliability path and migrated out (see
  Migration). It may remain temporarily behind a flag during transition.

### Why Kagi + Sonar (and not Kagi + Valyu)

| Dimension | Kagi + Sonar (chosen) | Kagi + Valyu (rejected) |
|---|---|---|
| Second provider category | General web search + generated answer/research | **Not general web search.** Valyu is a RAG/structured-data provider: 55+ specialized sources (academic, finance, compliance, healthcare, env/geo) plus one "Web" source, accessed via an OpenAI-MCP integration. It is not a Kagi/Tavily-class ranked-web-results endpoint. |
| Raw-result parity for fallback | Sonar returns `search_results` alongside its answer; normalized, it provides ranked URLs/domains/snippets usable as fallback. | Valyu "Web" is a single minor source on a data-platform product; not a parity replacement for Tavily-style raw web results. |
| Privacy | Sonar standard content is **zero-data-retention, not used for training** (official privacy page). Kagi links only usage volume, not queries, to the account. | Valyu query content is short-retained (24–72h) and not used for training, but **query unlinking is not promised**; API-key/IP metadata may persist up to 90 days. |
| Answer/research mode | Sonar provides it natively. | Valyu does not provide a generated web answer. |
| Key budget | Two keys: Kagi, Perplexity. | Would consume the second key on a non-parity product and still leave no answer/research route. |

**Conclusion:** Valyu was considered because the prior session framed it as
"structurally closer to Tavily." Direct inspection corrects that: Valyu is a
different category of product. No viable second **raw general-web-search**
provider meets the privacy threshold under the two-key constraint, so Sonar —
which adds a genuine answer/research capability and a ZDR fallback — is the
correct second provider.

### Tradeoffs accepted

- **Sonar is generated output, not pure raw search.** Its `search_results` are
  usable as fallback, but a Sonar fallback response carries a generated answer
  and token cost. The broker must mark `backend`/`provider` clearly so callers
  can distinguish raw-ranked from generated responses.
- **Kagi is more expensive per query** ($0.012/query at $12/1k) than Tavily
  basic (~$0.005) or Perplexity raw Search ($0.005). Privacy and quality justify
  the premium for this workload; cost is bounded by prepaid credit + usage
  limits and monitored via telemetry.
- **Kagi has no identified public SLA.** The failover path (Sonar) must be
  tested deliberately and the Kagi circuit breaker tuned conservatively.
- **Privacy claims are contractual, not cryptographic.** Kagi's account-unlinked
  query claim and Sonar's ZDR are provider terms that can drift. Dated source
  references are stored in this ADR and scheduled for periodic review.

## Routing modes and semantics

All modes are serial by default; parallel fan-out multiplies disclosure and is
opt-in only.

| Mode | Behavior | Egress |
|---|---|---|
| `normal` (default) | Kagi raw search → local fetch/browser verification of selected results. Sonar only on quality-gate failure. | Kagi (key 1); fetch egress to destination sites |
| `answer` / `research` | Sonar directly; return generated answer + `search_results`. | Perplexity (key 2) |
| `sensitive` / `no-egress` | Local KB/corpus only, or refuse. **SearXNG is not no-egress** and is not used here. | None |
| `searxng` (optional) | Best-effort/keyless local SearXNG only; experimental/budget mode. Not a privacy mode. | Upstream engines see household IP |
| `maximum-recall` (opt-in) | Serial escalation Kagi → Sonar, plus optional SearXNG; never parallel unless explicitly requested. | Kagi + Perplexity (+ SearXNG if enabled) |

### Quality-gated fallback (`normal` → Sonar)

Fallback fires when **any** of the following hold after Kagi returns (a
deterministic, conservative gate — no LLM grader in v1):

1. Kagi error, timeout, or circuit-open state.
2. Fewer than `WEBSEARCH_QUALITY_MIN_RESULTS` usable deduped results
   (default 3, clamped to `[1, 10]`).
3. Unique-domain diversity below `WEBSEARCH_QUALITY_MIN_DOMAINS` (default 2).
4. Duplicate/generic-result detection: the deduped set is dominated by a single
   domain or by near-identical snippets (Jaccard/simhash on snippets, threshold
   configurable, query-free).
5. Freshness failure for current/news intent: when the request signals
   time-sensitive intent and all results are older than a configurable staleness
   window. Intent is inferred only from explicit request metadata, not from
   stored query content.
6. Authoritative-source absence is **not** a hard gate in v1 (it risks
   over-triggering on legitimate niche queries); it is a telemetry signal only
   until evidence shows it is needed.

The gate must be computable **without persisting queries, snippets, or
titles** — only transient in-memory evaluation against the normalized result set.
Telemetry records gate outcomes and counts, never the evaluated content.

## Provider-neutral adapter contract

A small interface replaces the hard-coded SearXNG/Tavily orchestration so Kagi
and Sonar (and an optional SearXNG lane) share one path:

```python
class SearchProvider(Protocol):
    name: str                 # "kagi" | "sonar" | "searxng"
    output: str               # "raw" | "generated"
    async def search(self, query: str, num_results: int, *,
                     deadline: float) -> BackendOutcome: ...
```

- **Normalized result schema** (unchanged from today, extended):
  `rank`, `title`, `url`, `domain`, `snippet`, `engine`, `score`, and a new
  `provider` field (`kagi` | `sonar` | `searxng`). Sonar results are normalized
  from its `search_results` array; `engine` stays the per-source engine when
  known, `provider` names the API.
- **`BackendOutcome`** keeps the existing `state` (`ok` | `degraded` | `empty` |
  `timeout` | `error` | `circuit_open`), `credential_mode`, circuit fields, and
  `unresponsive_engines`. A new `generated: bool` and `answer: str | None`
  carry Sonar's generated text without polluting the raw result list.
- **Deadlines/limits:** per-provider deadline bounded by
  `WEBSEARCH_TOTAL_TIMEOUT` (today 18s); per-provider byte limit
  (`WEBSEARCH_SEARCH_MAX_BYTES`); circuit breaker per provider with the existing
  failure-threshold/cooldown knobs.
- **Retry rules:** `SEARCH_MAX_RETRIES` with backoff, unchanged, per provider.
- **Raw-vs-generated handling:** raw providers return `output="raw"`; Sonar
  returns `output="generated"` and the broker must never silently merge a
  generated answer into a raw result list. The response payload's `backend`
  field distinguishes `kagi`, `sonar`, `kagi+sonar`, etc.
- **No generic credential forwarding:** each provider resolves its own key from
  the secret store; keys are never passed between providers or returned to the
  frontend.
- **Query-free telemetry preserved:** the existing allowlist in
  `telemetry.py` stays; provider events record state/counts/timings, never
  queries, URLs, snippets, headers, or keys.

## Browser/fetch placement

- **Direct fetch first** for selected URLs via the existing `_public_fetch_client`
  / `html_extraction.py` path; bounded bytes, redirects, and timeout as today.
- **Browser only** for JS-heavy pages, fetch-blocked pages, interactive
  verification, or high-importance single-URL verification where direct fetch
  fails. Reuse the shared `browser_*` tools; never automate consumer SERPs.
- **Never** a routine consumer-search fallback, and never used to bypass
  CAPTCHAs, logins, rate limits, or robots restrictions.
- Browser/fetch does not count as a search-provider API key, but it does expose
  the machine IP/fingerprint to destination sites; this is acceptable for
  post-discovery verification, not for query delivery.

## Credential lifecycle and security

Both providers require **manual first-key bootstrap** (no programmatic
onboarding endpoint exists for initial keys):

- **Kagi:** key generated in the API Portal via "Generate Key"; optionally
  IP-restricted and product-scoped. No documented OAuth/device flow or
  programmatic key-management endpoint. Rotation is manual (generate new,
  repoint config, revoke old) unless Kagi adds management support.
- **Perplexity:** the API Group and first key are created manually at
  `console.perplexity.ai`. After bootstrap, an existing key can programmatically
  create/revoke keys via `POST /generate_auth_token` and
  `POST /revoke_auth_token`.

Onboarding/design:

- A local-only provider setup screen (or CLI command) opens vendor portals,
  accepts a one-time pasted key, validates it server-side, and stores it in
  **macOS Keychain** (preferred) or a mode-`0600` file outside the repository.
- Secrets are never returned to the frontend, never written to telemetry, logs,
  launchd plist contents, handoff files, or repo files.
- The broker exposes only validation status (`configured`/`unconfigured`/`invalid`)
  and per-provider usage counts, never the secret itself.
- Perplexity programmatic rotation is automated only after manual bootstrap;
  Kagi rotation stays manual.

This mirrors the existing `model-gateway` onboarding pattern (secret-free YAML
profiles + external mode-`0600` secret files / Keychain) already used on this
machine.

## Privacy / data-flow

```text
query (transient, not persisted)
   │
   ├─ normal: Kagi Search API ──► ranked results (transient) ──► normalize/dedupe/gate
   │            key 1 (Keychain)                                         │
   │                                                                      ├─ gate OK ──► return raw results; optional fetch/browser verify
   │                                                                      └─ gate fail ─► Sonar (key 2) ──► answer + search_results ──► return
   │
   ├─ answer/research: Sonar directly ──► return generated answer + results
   │
   ├─ sensitive/no-egress: local KB only, or refuse (no external call)
   │
   └─ searxng (optional): SearXNG loopback ──► upstream engines see household IP
```

- Telemetry records provider, state, counts, timings, circuit transitions, and
  gate outcomes. It does **not** record queries, URLs, snippets, titles,
  response bodies, headers, or keys.
- Kagi sees the query and the household IP; it states queries are not linked to
  the account and only usage volume is linked.
- Sonar sees the prompt/response; standard content is ZDR and not used for
  training; billing metadata (model, tokens, timestamp, API-key id) retained.
- Fetch/browser destinations see the machine IP/fingerprint for visited URLs
  only, not the original query (no referrer query leakage).

## Failure / quality-gate policy (summary)

| Condition | Action |
|---|---|
| Kagi error/timeout/circuit-open | Fallback to Sonar |
| Kagi nonempty but below min results or min domains | Fallback to Sonar |
| Kagi nonempty but duplicate/generic-dominated | Fallback to Sonar |
| Kagi nonempty but stale for time-sensitive intent | Fallback to Sonar |
| Kagi OK, gate passes | Return Kagi results; optional fetch/browser verify |
| Sonar also fails | Return error/empty with `fallback_reason`; do not cascade to SearXNG automatically in `normal` |
| `sensitive` mode | No external call; local only or refuse |

## Migration plan (Tavily and SearXNG)

1. **Introduce the adapter and Kagi provider** behind a new
   `WEBSEARCH_PROVIDER_STACK=kagi+sonar` flag; keep the legacy
   `WEBSEARCH_TAVILY_MODE` path functional during transition.
2. **Add the quality gate** and Sonar fallback; gate the new path on it.
3. **Move Tavily off the reliability path:** default the stack to `kagi+sonar`;
   Tavily becomes an opt-in legacy lane only if any caller still needs it, then
   is removed once parity is verified. Tavily credential handling is deleted.
4. **SearXNG:** keep the loopback instance and its image-search tool (images
   have no external API fallback and remain SearXNG-only). For general web
   search, SearXNG becomes the optional `searxng` mode only. Its maintenance
   burden is re-evaluated after one month of Kagi+Sonar production use; if
   unjustified, general-web SearXNG is removed (image search keeps the
   instance).
5. **Update `docs/provider-comparison.md`** with Kagi and Sonar sections,
   corrected Valyu categorization, and dated source references.

## Implementation phases

| Phase | Scope | Verification |
|---|---|---|
| 1 | `SearchProvider` protocol; refactor SearXNG + Tavily behind it (no behavior change) | Existing test suite passes green |
| 2 | Kagi adapter + credential bootstrap (Keychain/file); `normal` mode routes to Kagi | New Kagi unit tests + live smoke against `/api/v1/search` |
| 3 | Quality gate (min results, min domains, duplicate/generic, freshness) with query-free telemetry | New gate unit tests; telemetry privacy tests stay green |
| 4 | Sonar adapter + `answer`/`research` modes + quality-gated fallback | Sonar unit tests + live smoke; raw-vs-generated payload assertions |
| 5 | `sensitive`/`no-egress` and optional `searxng`/`maximum-recall` modes | Mode routing tests |
| 6 | Migrate Tavily off the path; update docs and `provider-comparison.md` | Full suite; provider-comparison review; no Tavily in default path |
| 7 | Local fetch/browser verification wiring (Kagi Extract + browser for JS/blocked pages) | Fetch/browser smoke; no SERP automation |

No code is changed until this ADR is approved.

## Sources (verified 2026-07-21)

- Kagi API portal / key management: <https://help.kagi.com/kagi/api/overview.html>
- Kagi Search API: <https://help.kagi.com/kagi/api/search.html>
- Kagi API pricing: <https://kagi.com/api/pricing> ($12/1k search requests)
- Kagi privacy: <https://kagi.com/privacy>
- Perplexity API groups / key bootstrap: <https://docs.perplexity.ai/docs/getting-started/api-groups>
- Perplexity programmatic key management: <https://docs.perplexity.ai/docs/admin/api-key-management.md>
- Perplexity Sonar privacy / ZDR: <https://docs.perplexity.ai/docs/resources/privacy-security>
- Perplexity raw Search Addendum (rejected): <https://www.perplexity.ai/hub/legal/perplexity-api-terms-of-service-search>
- Valyu docs (RAG/structured-data platform, not general web search): <https://docs.valyu.network/>
- SearXNG private-instance privacy model: <https://docs.searxng.org/own-instance.html>
- Existing local comparison: `docs/provider-comparison.md`
- Existing smoke artifact: `benchmarks/results/2026-07-21.json`

## Open questions for approval

1. Confirm the **Kagi + Sonar** two-key stack and the rejection of Valyu as a
   raw-search fallback (Valyu is a RAG/data platform, not general web search).
2. Confirm the **quality-gate thresholds** (defaults: min 3 results, min 2
   unique domains) are acceptable starting points, to be tuned against the
   local 12-query smoke set.
3. Confirm **SearXNG retention**: keep the loopback instance for image search
   and an optional `searxng` web mode, re-evaluate after one month.
4. Confirm **credential storage preference**: macOS Keychain vs mode-`0600`
   external file (Keychain recommended).
5. Confirm **Tavily migration**: remove from the default path, optionally keep
   behind a legacy flag during Phase 6, then delete.
