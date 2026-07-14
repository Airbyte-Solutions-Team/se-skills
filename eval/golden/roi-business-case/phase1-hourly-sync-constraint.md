# Acme — Roi Business Case: 🟢 viable with standard caveats
**Date:** 2026-07-14 · **Skill:** roi-business-case

## At a Glance
- **Verdict:** 🟢 viable with standard caveats
- **Confidence:** Medium
- **Source confidence:** Synthetic transcript only

## Airbyte Cost Projection
- **Primary scenario:** Customer's requested operating model with **hourly sync**, full stated scope, and concurrency assumptions.
- Data-worker estimate is sized for hourly cadence; no silent reduction in frequency or scope.
- If an optimization is modeled, it is labeled as an **alternative scenario** and the trade-off is shown.

## Assumptions & Confirms
- **Customer-confirmed inputs:** [list]
- **[confirm] inputs (SE must validate):** [list]
- **Missing inputs that materially affect the result:** true concurrency, data-worker pricing, exact volume growth. Hourly sync and 2M rows/day are the baseline; lowering frequency would materially change the worker estimate.

## Current-State Baseline
- Estimated current-state engineering cost: $[X] per year (derived from customer-stated FTE burden).
- Maintenance / on-call burden: labeled as `[confirm]` if not quantified.

## 3-Year TCO Comparison
| Year | Current-state cost | Airbyte cost | Switching/ramp cost | Net savings |
|------|-------------------:|-------------:|--------------------:|------------:|
| 1 | $[X] | $[Y] | $[Z] | $[X-Y-Z] |
| 2 | $[X] | $[Y] | — | $[X-Y] |
| 3 | $[X] | $[Y] | — | $[X-Y] |

## Payback & Sensitivity
- **Payback period:** [N] months (range: [N-M]–[N+M] months) — directional until data-worker pricing is confirmed.
- **Inputs that swing the case most:** true concurrency target, exact data-worker pricing, volume growth rate.

## One-Slide Summary
> **One-slide summary:** [X]-month payback, $[A] 3-yr savings vs current state, based on hourly sync and customer-stated FTE burden. Assumptions are labeled; missing inputs are listed.

## Source Coverage
- Synthetic transcript used for evaluation.
- Workspace: `TMP_DIR` (replaced at runtime)
- Environment flags: {"registry_available": false, "airbyte_platform_available": false, "airbyte_enterprise_available": false, "salesforce_enabled": false, "connector_models_enabled": false}
