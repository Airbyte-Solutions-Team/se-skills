Clean run. Now let me walk through everything end-to-end.

---

## Acme — Prospect Worker Sizing Estimate

> ⚠️ **Estimation caveat:** This uses the statistical estimation model (API/5 + DB/2), **not** the CPU-based billing formula. The CPU formula requires actual platform data. Once Acme is live on Airbyte Cloud, use Metabase billing data for the authoritative number.

---

### Inputs Confirmed

| Parameter | Value |
|---|---|
| Total connections | 50 |
| API connectors (Salesforce, HubSpot, Stripe, Shopify, Zendesk) | 35 — **70%** |
| Database connectors (Postgres, MySQL, Snowflake) | 15 — **30%** |
| Sub-hourly (every 15 min) | 25% → **12 connections** |
| Hourly | 40% → **20 connections** |
| Daily | 35% → **18 connections** |
| Avg sync duration | 12 minutes |
| Freshness window (1–5 AM UTC peak drain target) | **240 minutes** (4-hour window) |
| Environments | 2 (prod + staging) |
| Growth target | 80 connections |

---

### Connection Matrix

| Type | Sub-hourly (15 min) | Hourly | Daily | Total |
|---|---|---|---|---|
| **API** | 8 | 14 | 13 | **35** |
| **DB/File** | 4 | 6 | 5 | **15** |
| **Total** | **12** | **20** | **18** | **50** |

---

### Seven Sizing Views (Deterministic)

| # | Sizing View | Workers |
|---|---|---|
| 1 | Steady-state requirement | **5** |
| 2 | Peak-window drain requirement | **2** |
| 3 | Worst-case simultaneous burst | **10** |
| 4 | Production-only requirement | **5** |
| 5 | Combined prod + staging requirement | **7** |
| 6 | Future-growth requirement (80 connections) | **10** |
| 7 | 🎯 **Recommended contract / deployment capacity** | **10** |

---

### How Each Number Was Derived

#### View 1 — Steady-State (5 workers)
Assumes schedules are spread evenly across their intervals — the best-case normal operation.

```
API concurrent  = 8×(12/15) + 14×(12/60) + 13×(12/1440)
               = 6.40 + 2.80 + 0.11 = 9.31

DB concurrent   = 4×(12/15) + 6×(12/60) + 5×(12/1440)
               = 3.20 + 1.20 + 0.04 = 4.44

Workers = ceil(9.31 / 5) + ceil(4.44 / 2)
        = ceil(1.86) + ceil(2.22)
        = 2 + 3 = 5
```

#### View 2 — Peak-Window Drain (2 workers)
Minimum workers needed to drain all 18 daily syncs inside the 4-hour (240-minute) 1–5 AM window:

```
API daily drain = ceil((13 × 12) / 240) = ceil(0.65) = 1 slot → ceil(1/5) = 1 worker
DB daily drain  = ceil((5  × 12) / 240) = ceil(0.25) = 1 slot → ceil(1/2) = 1 worker
Total drain = 1 + 1 = 2
```
With the generous 4-hour freshness window, peak-window drain is **not the binding constraint**. All 18 daily syncs complete well within the window at 2 workers.

#### View 3 — Worst-Case Burst (10 workers)
All 18 daily syncs fire simultaneously (e.g., misconfigured schedules or a backfill) while sub-hourly and hourly syncs continue at steady state:

```
API concurrent  = 8×(12/15) + 14×(12/60) + 13 (all daily at once)
               = 6.40 + 2.80 + 13.00 = 22.20

DB concurrent   = 4×(12/15) + 6×(12/60) + 5
               = 3.20 + 1.20 + 5.00 = 9.40

Workers = ceil(22.20 / 5) + ceil(9.40 / 2)
        = 5 + 5 = 10
```
This is the ceiling Acme must be prepared for if schedules cluster.

#### View 4 — Production-Only (5 workers)
Identical to steady-state — single environment, no staging overhead.

#### View 5 — Combined Prod + Staging (7 workers)
Staging is modelled as daily-only (full refresh pattern) with the same connector mix:

```
Staging API  = 35 × (12/1440) = 0.292 → ceil(0.292/5) = 1 worker
Staging DB   = 15 × (12/1440) = 0.125 → ceil(0.125/2) = 1 worker
Staging total = 2

Combined = 5 (prod steady) + 2 (staging) = 7
```

#### View 6 — Future Growth at 80 Connections (10 workers)
Same 70/30 API/DB and 25/40/35 frequency ratios at 80 connections:
- Prod steady-state scales to **8 workers**
- Staging scales to **2 workers**
- Combined at growth: **8 + 2 = 10**

#### View 7 — Recommended Contract Capacity (10 workers)
Decision logic applied by the calculator:

```
Burst delta    = max(0, burst_workers − combined_steady) = max(0, 10 − 7) = 3
Burst headroom = min(2, 3) = 2        ← capped at 2
Base headroom  = 1 (daily syncs exist, multiple environments)
Effective headroom = max(1, 2) = 2

With headroom:   combined_steady + headroom = 7 + 2 = 9
Growth floor:    future_combined = 10

Recommended = max(9, 10) = 10
```

The **future growth path drives the recommendation** — at 80 connections, combined prod+staging naturally lands at 10 workers, which also satisfies the headroom buffer above the current baseline.

---

### Key Observations for the Acme Conversation

| Observation | Detail |
|---|---|
| **Schedule clustering is the #1 risk** | Steady-state is only 5 workers, but an uncoordinated daily burst hits 10. Staggering the 18 daily syncs across the 1–5 AM window prevents worst-case spikes. |
| **The 4-hour drain window is generous** | At 2 drain workers, all 18 daily syncs complete in the window with capacity to spare. No need to over-provision for freshness. |
| **Staging adds 2 workers** | A daily-pattern staging environment is lean. If staging runs full-frequency (same as prod), re-run with `--environments` and staging scheduled identically — expect staging to mirror prod. |
| **70% API mix is favorable** | API connectors (Salesforce, Zendesk, HubSpot, Stripe, Shopify) require 5 concurrent per worker vs. 2 for DB. The high API fraction keeps per-worker utilization efficient. |
| **Growth to 80 connections lands exactly at 10 workers** | No additional contract expansion needed in the 6-month horizon — the 10-worker contract covers both today's burst ceiling and the full growth target. |
| **MySQL CPU quirk** | If Acme has CPU overrides on MySQL connections (a common performance request), the orchestrator retains its 1.0 CPU default rather than matching the override. Verify actual per-container values via Datadog `avg:kubernetes.cpu.requests{ab_connection_id:<ID>} by {kube_container_name}` after onboarding. |

---

### Final Recommendation

> **Contract Acme at 10 Data Workers.**

This single number:
- Covers **normal operations** (5 workers steady-state, prod only)
- Covers **both environments** comfortably (7 workers combined)
- Absorbs a **worst-case schedule cluster** (10 workers burst)
- **Future-proofs growth to 80 connections** in 6 months without a contract renegotiation

Once Acme is live, pull actual hourly CPU billing data from Metabase (`organization_data_worker_usage_daily` + `workspace_data_worker_usage_hourly`) to validate this estimate against the ground-truth `(maxSourceCPU + maxDestCPU + maxOrchestratorCPU) / 8` formula. The estimation model tends to be conservative for well-staggered API-heavy workloads, so actual usage may land slightly below 10 under normal operations.
