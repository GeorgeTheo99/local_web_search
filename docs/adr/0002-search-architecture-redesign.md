# ADR 0002: Search architecture — Brave + SearXNG

- **Status:** Accepted (implemented 2026-07-22)
- **Date:** 2026-07-22

## Context

The original broker was SearXNG-first with hard-coded Tavily fallback. Two
problems motivated redesign:

1. **Privacy.** Tavily's terms permit broad use of submitted content and are
   account-linked; SearXNG sends queries to upstream engines under the
   household public IP and is not a true no-egress mode.

2. **Reliability.** SearXNG scrape-based engines (DuckDuckGo, Google CSE,
   Startpage, Mojeek) chronically suspend from this home IP, contributing a
   permanent degraded flag. Tavily keyless mode is rate-limited (429 after a
   handful of calls) and not a reliability SLA.

## Decision

Adopt **Brave Search API** as the sole external search provider, with
**SearXNG** retained as an optional loopback alternative for web and image
search.

- **Brave Search API** — default raw ranked-search provider. Independent
  40B+ page index (not metasearch over Google/Bing), $5/1k requests
  ($0.005/query), privacy-first posture, self-service key bootstrap.
- **SearXNG** — optional keyless loopback stack
  (`WEBSEARCH_PROVIDER_STACK=searxng`) for web and image search. Not a privacy
  mode; upstream engines see the household IP.
- **Tavily, Kagi, Perplexity Sonar** — removed entirely. The codebase no
  longer contains active provider code, configuration, or tests for them.
  Historical decision and benchmark evidence is retained.

## Why Brave

| Dimension | Brave Search API |
|---|---|
| Index | Own independent web index (40B+ pages) — no upstream Google/Bing dependency |
| Privacy | Privacy-first company; no cross-service tracking; minimal logs |
| Price | $5/1k requests ($0.005/query) |
| Key bootstrap | Self-service API keys via Brave dashboard |
| Reliability | Independent index avoids the scrape-engine suspension problems that plague SearXNG |

Brave was chosen over Kagi (more expensive at $0.012/query, metasearch rather
than independent index) and Tavily (weaker privacy policies, account-linked
queries, keyless rate limits).

## Provider stacks

| Stack | Behavior | Egress |
|---|---|---|
| `brave` (default) | Brave raw search → local fetch/browser verification | Brave (key); fetch to destination sites |
| `searxng` (optional) | Best-effort/keyless local SearXNG only | Upstream engines see household IP |

## Routing modes

| Mode | Behavior |
|---|---|
| `normal` (default) | Brave raw search → optional fetch/browser verify |
| `sensitive` / `no-egress` | No external call; refuse without a local corpus |
| `maximum_recall` | Serial escalation across all configured providers (opt-in) |

## Cost tracking

Every search response includes an `estimated_cost_usd` field computed from a
per-provider rate table. The my-ai UI displays it in the web-search tool call
summary (for example, "3 results · Brave · estimated total $0.005").

## Credential lifecycle

- Brave key stored in a mode-0600 file at
  `$LOCAL_SEARCH_DATA_DIR/brave_key`, or supplied through the explicit
  request header or process environment.
- Key resolution: `X-Brave-Key` header (HTTP) → `BRAVE_API_KEY` env var →
  secret file.
- Secrets are never returned to the frontend, never written to telemetry,
  logs, or repo files.

## Implementation

All code is implemented and tested (181 tests). The broker exposes `web_search`,
`batch_web_search`, `image_search`, `web_fetch`, and `verify_url` tools over
MCP (HTTP and stdio). Provider-neutral health, telemetry, and circuit-breaker
infrastructure is in place for future provider additions.
