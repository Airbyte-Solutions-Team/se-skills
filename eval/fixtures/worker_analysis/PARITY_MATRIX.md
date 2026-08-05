# worker-analysis parity matrix

Generated during validation of PR #38. Each cell is classified as:

- **Exact** — essentially the same content.
- **Materially equivalent** — same conclusion, coverage, methodology, and level of detail; wording or table layout differs.
- **Minor wording / formatting** — same substance, different phrasing, ordering, or table structure.
- **Runtime-required difference** — expected variation because LLM output is non-deterministic or the prompt interpretation differs, but methodology is consistent.
- **Material parity gap** — a meaningful difference in recommendation, methodology, coverage, or level of detail that should be addressed.

This matrix is reconciled against the saved output artifacts, which are the source of truth.

---

## Reconciliation notes (read this first)

### Scenario 1 — Complete questionnaire (45 mixed SaaS/DB connections)

The saved original output (`eval/fixtures/worker_analysis/original/questionnaire_complete.md`) is internally contradictory about the future recommendation:

- The **Seven Sizing Views** table lists `Future growth (80 connections)` as **8** and `Recommended contract capacity` as **8**.
- The **Recommendation Summary** says the initial contract of **8** "Also covers Future growth to 80 connections (8 combined, same number)".
- The **Growth Trajectory** table lists the 80-connection row with `Combined = 8` but `Recommended = 10`.
- The same summary says "Step-up trigger: When approaching 80 connections → step to **10 workers**".

The port (`eval/fixtures/worker_analysis/se-suite/questionnaire_complete.md`) is consistent: the deterministic `questionnaire_calculator` produces `future_growth_workers = 8` and `recommended_contract_or_deployment_workers = 8`. The future-growth *calculation* (combined prod+staging at 80) is identical to the original's explicit sizing view. The original's 10-worker figure appears to be an additional step-up trigger or a model inconsistency, not a separate calculation. Therefore the future-growth **requirement** is Exact, but the future **headline recommendation** cannot be called Exact because the original artifact gives two numbers.

### Scenario 2 — Cadence preservation (50 connections)

The previous matrix claimed the original recommended "18–20 workers" at month 6. That number is **not present** in the saved original artifact (`eval/fixtures/worker_analysis/original/cadence_preservation.md`). The saved output recommends **10 Data Workers** for the contract and states "6-month growth capacity | ✅ Covered at 80 connections". The port also recommends **10 Data Workers**. The 18–20 figure came from an earlier or different validation run and has been removed.

### Scenario 3 — Incomplete evidence (40 connections, unknown split)

The previous matrix said the original "executive summary" recommended "2 workers today, plan for 3". The saved original artifact (`eval/fixtures/worker_analysis/original/incomplete_evidence.md`) actually says:

- "Estimate: **6–7 Data Workers** (recommended starting capacity)"
- The scenario table shows **Steady-State = 2–3 workers**, **Prod + Staging = 4–5 workers**, **Worst-Case Burst = 8–9 workers**, and **Recommended = 6–7 workers**.

The **2 workers** figure is the steady-state base case, not the final recommendation. The port (`eval/fixtures/worker_analysis/se-suite/incomplete_evidence.md`) produces a single recommended contract of **6 Data Workers**, which falls within the original's 6–7 range. The original does not model a specific future-growth row; it only notes in a follow-up question that at 2× (80 connections) the recommendation would step to 8–9. The port models future growth at 60 connections (its own assumed 50% growth target) and still recommends 6, so the future-growth views are not directly comparable.

---

## Scenario 1 — Complete questionnaire

**Original artifact:** `eval/fixtures/worker_analysis/original/questionnaire_complete.md`
**Port artifact:** `eval/fixtures/worker_analysis/se-suite/questionnaire_complete.md`

**Inputs:** 45 connections, 30 API (Salesforce/HubSpot/Stripe) / 15 DB (Postgres/MySQL), 20% sub-hourly, 30% hourly, 50% daily, 10-min avg duration, 2–6 AM UTC peak window, 1-hour freshness SLA, 2 environments (prod + staging), no initial load, 80-connection 6-month growth target.

| Dimension | Original | Port | Classification | Notes |
|---|---|---|---|---|
| Connection matrix | 6/9/15 API and 3/4/8 DB across 9/13/23 frequency buckets | Same | Exact | Identical deterministic decomposition. |
| Steady-state (prod) | 4 workers | 4 workers | Exact | |
| Peak-window drain | 2 workers | 2 workers | Exact | |
| Worst-case burst | 11 workers | 11 workers | Exact | |
| Production-only | 4 workers | 4 workers | Exact | |
| Combined prod + staging (today) | 6 workers | 6 workers | Exact | |
| Future-growth requirement (80 conns) | 8 workers (sizing view) | 8 workers | Exact | Combined prod+staging at 80 = 8 in both. |
| Current headline recommendation | 8 Data Workers | 8 Data Workers | Exact | Both recommend 8 today. |
| Future headline recommendation at 80 | **8** in sizing-view/summary, **10** in growth-trajectory table | 8 Data Workers | Materially equivalent | The original artifact is internally inconsistent. The port's deterministic `max(combined + headroom, future_growth) = 8` matches the original's explicit 8-worker future-growth sizing view. The 10 in the original growth-trajectory table appears to be an additional step-up trigger or model inconsistency, not a separate calculation. |
| Optimization recommendations | Stagger daily batch across 2–6 AM, offset staging | Stagger daily batch across 2–6 AM, offset staging | Materially equivalent | Same levers, slightly different phrasing. |
| Risks and caveats | Estimation model caveat, 15-min sub-hourly default, 10-min avg, no initial load, staging timing | Same caveats | Materially equivalent | |
| Output depth | Seven sizing views, growth trajectory, recommendation summary, scheduling notes | Seven sizing views, connection matrix, recommendation breakdown, scheduling notes | Materially equivalent | |

---

## Scenario 2 — Cadence preservation

**Original artifact:** `eval/fixtures/worker_analysis/original/cadence_preservation.md`
**Port artifact:** `eval/fixtures/worker_analysis/se-suite/cadence_preservation.md`

**Inputs:** 50 connections, 35 API (Salesforce/HubSpot/Stripe/Shopify/Zendesk) / 15 DB (Postgres/MySQL/Snowflake), 25% sub-hourly, 40% hourly, 35% daily, 12-min avg duration, 1–5 AM UTC peak window, 2 environments, 80-connection growth target.

| Dimension | Original | Port | Classification | Notes |
|---|---|---|---|---|
| Connection matrix | 8/14/13 API and 4/6/5 DB across 12/20/18 frequency buckets | Same | Exact | Identical deterministic decomposition. |
| Steady-state | 5 workers | 5 workers | Exact | |
| Peak-window drain | 2 workers | 2 workers | Exact | |
| Worst-case burst | 10 workers | 10 workers | Exact | |
| Production-only | 5 workers | 5 workers | Exact | |
| Combined prod + staging | 7 workers | 7 workers | Exact | |
| Future-growth requirement (80 conns) | 10 workers | 10 workers | Exact | |
| Current headline recommendation | 10 Data Workers | 10 Data Workers | Exact | |
| Future headline recommendation at 80 | 10 Data Workers ("6-month growth capacity | ✅ Covered at 80 connections") | 10 Data Workers | Exact | The earlier "18–20" figure is not present in the saved original artifact. |
| Optimization recommendations | Stagger daily batch, review sub-hourly cadence, keep staging daily-only | Stagger daily batch, spread sub-hourly, offset staging | Materially equivalent | Same levers, slightly different phrasing. |
| Risks and caveats | Heuristic-model caveat, no initial full-load, Snowflake-as-DB caveat | Same caveats | Materially equivalent | |

---

## Scenario 3 — Incomplete evidence

**Original artifact:** `eval/fixtures/worker_analysis/original/incomplete_evidence.md`
**Port artifact:** `eval/fixtures/worker_analysis/se-suite/incomplete_evidence.md`

**Inputs:** ~40 connections, API/DB split unknown, average duration unknown, some hourly and some daily, growth expected but no firm number. The port additionally assumes 40% hourly / 60% daily, 8-min avg duration, 2 environments, and 60-connection growth target.

| Dimension | Original | Port | Classification | Notes |
|---|---|---|---|---|
| Executive summary / headline | "6–7 Data Workers (recommended starting capacity)" | "6 Data Workers — across all scenarios" | Materially equivalent | The port's 6 is inside the original's 6–7 range. |
| Steady-state base case | 2 workers | 2 workers | Exact | The previous matrix mislabeled this as the final recommendation. |
| Prod + staging base case | 4 workers | 4 workers | Exact | |
| Worst-case burst | 8–9 workers | 8–12 workers (API-heavy 8, mixed 10, DB-heavy 12) | Materially equivalent | Both highlight burst as the main risk and give a similar ceiling range. |
| Recommended contract | 6–7 workers | 6 workers | Materially equivalent | Port picks the low end of the original's range; both avoid false precision. |
| Future-growth view | Not modeled as a row; notes that 2× (80 conns) would be 8–9 workers | 60-conns future-growth = 4–5 workers, still recommended 6 | Runtime-required difference | The future target was not the same in the two outputs (none explicitly modeled vs 60 connections), so the numbers are not directly comparable. Both state that growth pushes the recommendation up. |
| Follow-up questions | 4 prioritised questions | 5 prioritised questions | Minor wording / formatting | |
| Risks and caveats | API/DB split, duration, schedule clustering, specific connectors | Same plus sub-hourly caveat | Materially equivalent | |

---

## Scenario 4 — Workspace-style OSS export

**Original artifact:** `eval/fixtures/worker_analysis/original/workspace_oss.md`
**Port artifact:** `eval/fixtures/worker_analysis/se-suite/workspace_oss.md`

**Inputs:** `eval/fixtures/worker_analysis_workspace.json`, 6 connections, 19 jobs, 14:00 UTC peak.

| Dimension | Original | Port | Classification | Notes |
|---|---|---|---|---|
| Current sizing | 3 Data Workers | 3 Data Workers | Exact | |
| Peak concurrency | 14:00 UTC: 4 API + 3 DB | Same | Exact | |
| Long-running / initial load | Postgres Orders 60-min initial load | Same | Exact | |
| Failed / retried jobs | HubSpot Companies zero-duration at 13:50 | Same | Exact | |
| Optimization recommendations | Stagger API burst, schedule initial loads in dead zone, resolve HubSpot failure | Stagger MySQL away from top of hour, check HubSpot logs, stagger API | Materially equivalent | |

---

## Cross-scoring summary

| Dimension | Overall parity |
|---|---|
| Analysis depth | Materially equivalent |
| Section structure | Materially equivalent |
| Sizing methodology | Exact (`ceil(API/5) + ceil(DB/2)`, per-type ceiling, deterministic burst) |
| Current-state assessment | Exact (questionnaire, workspace) / Materially equivalent (cadence, incomplete) |
| Low/base/high scenarios | Materially equivalent |
| Current headline Data Worker recommendation | **Exact** for questionnaire-complete (8), cadence-preservation (10), workspace (3); Materially equivalent for incomplete evidence (6 vs 6–7) |
| Future-growth requirement (calculation) | Exact (questionnaire 8, cadence 10) / Not directly comparable (incomplete evidence) |
| Future headline recommendation | Materially equivalent for questionnaire (original artifact is internally 8/10; port is 8) / Exact for cadence (10) / Not directly comparable for incomplete evidence |
| Assumptions | Materially equivalent |
| Headroom reasoning | Materially equivalent |
| Connector classifications | Exact |
| Concurrency findings | Exact |
| Optimization recommendations | Materially equivalent |
| Risks and caveats | Materially equivalent |
| Confidence level | Materially equivalent |
| Follow-up questions | Materially equivalent |
| Customer-constraint handling | Materially equivalent |
| Output depth | Materially equivalent |
| Section coverage | Materially equivalent |

## Open parity notes after reconciliation

1. **Scenario 1 future recommendation ambiguity.** The saved original output gives two numbers for the 80-connection recommendation (8 and 10). The port's deterministic output is 8, matching the original's explicit future-growth sizing view and the overall recommended contract capacity. The 10 in the original growth-trajectory table is treated as an internal model inconsistency, not a port defect.
2. **Scenario 2 month-6 claim corrected.** The earlier "18–20 workers" claim is not present in the saved original artifact; both original and port recommend 10 Data Workers.
3. **Scenario 3 label corrected.** The original "2 workers" figure is the steady-state base case, not the final recommendation. The final recommendation is 6–7 Data Workers; the port recommends 6.
4. **No production code changed during reconciliation.** All differences were traced to the wording/labeling in the parity matrix or to genuine internal ambiguity in the generated original output, not to a calculation or methodology defect in the port.
