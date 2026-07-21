# Search provider comparison

Snapshot: 2026-07-21

## Recommendation

Keep **self-hosted SearXNG first** and treat external providers as explicit egress:

- Default `fallback` should contact an external provider only when SearXNG has no usable results.
- Use `disabled` for sensitive queries and `supplement` only when broader recall matters more than privacy.
- Tavily remains the better-supported **raw retrieval** fallback today: its published retrieval evaluation is stronger, the broker already normalizes it, and basic search is inexpensive. It is not appropriate for sensitive queries under its standard privacy policy.
- Do **not** adopt Perplexity's raw Search API as a privacy upgrade. Its Search Addendum expressly excludes zero-data-retention obligations and permits Perplexity to retain and use Search Data for lawful business purposes, including product improvement.
- If proprietary Perplexity retrieval plus zero retention is desired, benchmark **Sonar** as a separate fallback and normalize its `search_results` array. Sonar generates an answer and has token costs, so it is not strictly equivalent to a raw ranked-results endpoint.
- Compare Tavily Research with Perplexity Agent/Sonar separately from raw retrieval.

## Apples-to-apples view

| Option | Product being compared | Accuracy evidence | Marginal price | Privacy posture | Operational tradeoff |
|---|---|---|---|---|---|
| Self-hosted SearXNG | Raw metasearch results | No portable vendor-independent score; quality varies with enabled engines, blocks, geography, and ranking | No per-query API fee; machine/network/admin cost remains | Best local control. SearXNG strips client cookies/private data and hides the user's IP from engines, but upstream engines still receive the query and the server IP | Highest maintenance; scraped engines can degrade or block |
| Tavily Search | Raw ranked results and snippets | Tavily's own reproducible eval reports 93.3% SimpleQA and 83.02% document relevance with **advanced** search; Perplexity Search scored 85.92% and 71.2% in that same vendor-run setup | Basic: 1 credit, about $0.005–$0.008/query on paid pricing; advanced: 2 credits, about $0.010–$0.016/query; 1,000 credits/month free | Tavily says it collects queries, may use portions to improve future responses unless contractually restricted, retains data under purpose/account-based criteria, and may share queries with third-party indexes such as Google | Simple integration; current keyless tier is convenient but shared limits are not a reliability SLA |
| Perplexity Search API | Raw ranked results and extracted snippets | Tavily's vendor-run eval places it below Tavily advanced; no independent result set was found that also includes this machine's SearXNG configuration | $5/1,000 requests = $0.005/query, no token charge | **No standard ZDR.** The Search Addendum says Sonar ZDR obligations do not apply and permits retention/use of Input and Output; it also says not to submit personal data without written authorization | Competitive fixed price, proprietary continuously refreshed public-content index, filters, multi-query support, and up to 20 results; requires an API key |

The Tavily evaluation is useful directional evidence, not a neutral verdict: Tavily authored it, Tavily ran in `advanced` mode, and SearXNG was not included. The configuration used Tavily advanced with 10 results and Perplexity Search with 10 results and 512 tokens per page. A local decision should use a blinded query set representative of our traffic.

## Local smoke result

A 2026-07-21 live smoke used 12 navigational queries with known official domains and scored whether that domain appeared in the top 5/10. This is a small reliability/official-source test, **not** a general accuracy benchmark.

| Provider | Success@5 | Success@10 | MRR@10 | Mean latency | Reliability note |
|---|---:|---:|---:|---:|---|
| This SearXNG configuration | 7/12 | 9/12 | 0.4043 | 211 ms | All 12 were marked degraded: DuckDuckGo, Google CSE, and Startpage were suspended or reporting CAPTCHA |
| Tavily basic keyless | 4/12 | 4/12 | 0.3333 overall | 283 ms overall | The first 4 calls succeeded and ranked the expected domain first (806 ms successful-call mean); 3 calls then returned HTTP 429 and opened the circuit, so the final 5 were skipped |
| Perplexity Search | Not run | Not run | Not run | Not run | No API credential is configured on this machine |

The smoke shows why keyless Tavily is a best-effort fallback rather than a sole reliability layer. It also confirms that SearXNG's degradation flag alone says little about result usefulness: despite three blocked engines, it still found 9 of 12 official domains in the top 10. The reproducible synthetic suite is [`benchmarks/provider_smoke.py`](../benchmarks/provider_smoke.py), and the captured result is [`benchmarks/results/2026-07-21.json`](../benchmarks/results/2026-07-21.json). The artifact includes provider HTTP/error/circuit state and was generated verbatim by the linked script. The script calls provider backends directly so production telemetry remains query-free. Set `PERPLEXITY_API_KEY` to add Perplexity Search to a future run.

```bash
cd mcp-websearch
uv run python ../benchmarks/provider_smoke.py > ../benchmarks/results/YYYY-MM-DD.json
```

## Answer and deep-research products

These prices and scores should not be mixed with raw search retrieval:

| Product | Current pricing signal | Appropriate use |
|---|---|---|
| Perplexity Sonar | Request fee $0.005/$0.008/$0.012 for low/medium/high context, plus $1/M input and $1/M output tokens | Fast grounded answer with citations |
| Perplexity Sonar Pro | Request fee $0.006/$0.010/$0.014 plus $3/M input and $15/M output; Pro Search request fee $0.014/$0.018/$0.022 plus tokens | More difficult, multi-step questions |
| Perplexity Sonar Deep Research | Official examples range from about $0.41 to $1.32 per request depending on context/reasoning/searches | Exhaustive research, not routine retrieval |
| Tavily Research | `mini`: 4–110 credits; `pro`: 15–250 credits | Multi-source generated reports; benchmark separately from Search |

Perplexity's own 2026 agentic benchmark reports strong full-system results (for example 0.805 BrowseComp and 0.871 DeepSearchQA), but that evaluates the Perplexity Agent API—not the raw Perplexity Search API or Sonar alone.

## Privacy detail

### SearXNG

A private instance gives us control over application logs, configuration, retention, and access. It removes client cookies/private data, randomizes browser profiles, and prevents visited result pages from seeing the original search query as a referrer. It does **not** make upstream search private from the upstream engine: each selected engine can see the query and the SearXNG server's IP. Proxy/Tor configuration can reduce that linkage at a reliability cost.

### Tavily

Tavily's policy, updated 2025-11-24, states that it collects query data and uploaded documents. Unless a customer contract says otherwise, it may use portions of query data to improve future responses. Retention is not a fixed short interval; it is tied to account/service/legal/operational need or a valid deletion request. Tavily can send queries to third-party search-index providers when its own index cannot retrieve content. Do not send personal, secret, customer, or internal terms through this fallback without an appropriate enterprise contract.

### Perplexity

Perplexity's API privacy page says **Sonar API** prompt/response content is not retained or used for training; only billing metadata such as model, token count, timestamp, duration, and API-key identity is retained. The separate Perplexity Search Addendum, last updated 2025-09-22, is decisive for the raw `/search` endpoint: it says Search Services are separate from Sonar, that zero-data-retention obligations for other services do not apply, and that Perplexity may retain, copy, distribute, and otherwise use Search Data (Input and Output) for lawful business purposes including product improvement. It also prohibits submitting personal data without Perplexity's written authorization. General API FAQ or privacy wording should not be read to override these service-specific terms.

Sonar uses Perplexity's search index/public internet and returns a structured `search_results` array alongside generated content. It is therefore the Perplexity product to benchmark when ZDR is required, accepting the extra generation step and token cost.

## Local benchmark design

Run at least 100 blinded queries sampled from real, non-sensitive use cases:

1. **Strata:** navigational/official-source, current events, software documentation, long-tail facts, ambiguous/local queries, and multi-source research.
2. **Retrieval parity:** compare SearXNG, Tavily basic, Tavily advanced, and Perplexity Search at the same result count. Do not compare generated Sonar answers with raw links.
3. **Human labels:** two reviewers independently mark relevance at ranks 1/3/5/10 and whether the authoritative source is present. Resolve disagreements without seeing provider identity.
4. **Metrics:** success@5, nDCG@10, precision@5, authoritative-source recall, duplicate rate, dead-link rate, p50/p95 latency, error/degradation rate, external-egress rate, and actual billed cost/query.
5. **Answer track:** feed each provider's normalized top results to the same frozen local or API model and grade exact answer correctness and citation entailment. Keep model, prompt, token budget, and fetched-content limits identical.
6. **Repeatability:** rerun current-event and long-tail strata on three days because indexes and anti-bot behavior change.
7. **Privacy:** use synthetic queries only; record provider/status/latency/cost but never query text, URLs, snippets, headers, or credentials in telemetry.

A 100-query raw-retrieval run costs roughly $0.50 for Perplexity Search, $0.50–$0.80 for Tavily basic, or $1.00–$1.60 for Tavily advanced at published paid rates, before any grading-model cost.

## Sources

- Tavily credits and pricing: <https://docs.tavily.com/documentation/api-credits>
- Tavily privacy policy: <https://docs.tavily.com/documentation/privacy>
- Tavily search evaluation and configuration: <https://github.com/tavily-ai/tavily-search-evals>
- Perplexity pricing: <https://docs.perplexity.ai/docs/getting-started/pricing>
- Perplexity Search API: <https://docs.perplexity.ai/docs/search/quickstart>
- Perplexity Search Addendum: <https://www.perplexity.ai/hub/legal/perplexity-api-terms-of-service-search>
- Perplexity API privacy/security (Sonar ZDR): <https://docs.perplexity.ai/docs/resources/privacy-security>
- Perplexity agentic evaluation: <https://github.com/perplexityai/search_evals>
- SearXNG private-instance privacy model: <https://docs.searxng.org/own-instance.html>
