# Search provider comparison

Snapshot: 2026-07-24

## Recommendation

**Brave Search API** remains the code default and paid reference provider.
SearXNG is available alone or as the primary in the `searxng+brave` web stack.
The dual stack is currently for shadow evaluation: both providers run, Brave is
served when usable, and only identifier-free numeric parity is retained. Do not
promote it based on fallback rate alone. Dual-stack image search uses Brave;
SearXNG images are limited to the SearXNG-only stack. See
`docs/adr/0002-search-architecture-redesign.md`.

## Brave Search API

| Dimension | Value |
|---|---|
| Product | Raw ranked results from an independent 40B+ page index |
| Price | $5/1k requests ($0.005/query) |
| Privacy | Privacy-first company; no cross-service tracking; minimal logs; independent index (queries not forwarded to Google/Bing) |
| Key bootstrap | Self-service via Brave dashboard; `X-Subscription-Token` header |
| SLA | No public SLA identified |

Brave was chosen over alternatives:

| Provider | Why rejected |
|---|---|
| **Tavily** | Weaker privacy: collects queries, may use for product improvement, account-linked, may share with third-party indexes. Keyless tier rate-limited (429 after handful of calls). |
| **Kagi** | More expensive ($0.012/query vs $0.005). Metasearch over upstream indexes rather than own index. |
| **Perplexity Sonar** | Generated output, not raw search. Token costs on top of request fee. Would add a second external provider/key. |
| **Perplexity Search API** | No standard ZDR; Search Addendum permits retention/use of Input and Output for product improvement. |
| **Valyu** | Not a general web search product — RAG/structured-data platform. |

## SearXNG (loopback or dual-stack primary)

| Dimension | Value |
|---|---|
| Product | Raw metasearch results |
| Price | No per-query fee; machine/network/admin cost |
| Privacy | Best local control, but upstream engines see the query and server IP. Not a no-egress mode. |
| Reliability | Previously problematic scrape engines were removed; Bing is broker-critical for general-web coverage and its unavailability triggers dual-stack fallback. |

## Local smoke benchmark

[`benchmarks/provider_smoke.py`](../benchmarks/provider_smoke.py) now compares
SearXNG and Brave directly over 12 synthetic navigational queries. Running it
issues up to 12 billable Brave requests (currently at most `$0.06`), so it is
not part of the automatic local verification suite.

The checked-in result files predate the Brave migration and remain historical
evidence only. No post-migration Brave score is claimed until the repaired
benchmark is intentionally run and its result is saved.

## Sources

- ADR 0002: [`adr/0002-search-architecture-redesign.md`](adr/0002-search-architecture-redesign.md)
- Brave Search API docs: <https://api.search.brave.com/app/documentation/web-search/get-started>
- Brave Search API pricing: <https://brave.com/search/api/> ($5/1k search requests)
- Brave privacy: <https://brave.com/privacy/browser/>
- SearXNG private-instance privacy model: <https://docs.searxng.org/own-instance.html>
