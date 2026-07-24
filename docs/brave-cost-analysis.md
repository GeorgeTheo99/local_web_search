# Brave cost analysis

This report uses the repository-local `data/telemetry.sqlite3` snapshot through 2026-07-24 20:35:48 UTC. Dates below are UTC. Telemetry stores operational counts only, not query text.

## Brave provider usage

Cost is estimated at **$0.005 for each Brave provider event that returned one or more results**, as requested. Events with zero results are shown in the event total but are not counted as billable here.

| UTC date | Brave provider events | Result count | Requests with results | Estimated cost |
|---|---:|---:|---:|---:|
| 2026-07-22 | 43 | 471 | 41 | $0.205 |
| 2026-07-23 | 50 | 578 | 49 | $0.245 |
| 2026-07-24 | 28 | 407 | 28 | $0.140 |
| **Total** | **121** | **1,456** | **118** | **$0.590** |

The actual July 2026 Brave spend represented by this telemetry is therefore **$0.59**, not the earlier $13 estimate. All 121 Brave provider events recorded one attempt each; 118 returned results, two were empty, and one errored.

## Search events

| UTC date | Search events |
|---|---:|
| 2026-07-13 | 1 |
| 2026-07-14 | 92 |
| 2026-07-15 | 104 |
| 2026-07-16 | 144 |
| 2026-07-17 | 226 |
| 2026-07-18 | 20 |
| 2026-07-19 | 72 |
| 2026-07-20 | 72 |
| 2026-07-21 | 112 |
| 2026-07-22 | 114 |
| 2026-07-23 | 58 |
| 2026-07-24 | 35 |
| **Total** | **1,050** |

## Enabled-engine failures since cleanup

The engine cleanup was committed as `8b429ce` at 2026-07-22 03:22:51 UTC. Since that timestamp, telemetry contains **zero** `engine_failures` for the currently enabled general-search engines: `bing`, `mwmbl`, `wikipedia`, `github`, and `arxiv`.

## Query method

The report joins `provider_events` to `search_events` for Brave counts and groups on `date(created_at, 'unixepoch')`. Monthly cost uses:

```sql
SUM(CASE WHEN provider_events.result_count > 0 THEN 1 ELSE 0 END) * 0.005
```

Enabled-engine failures use the cleanup commit timestamp as the lower bound and filter to the five engine names listed above.
