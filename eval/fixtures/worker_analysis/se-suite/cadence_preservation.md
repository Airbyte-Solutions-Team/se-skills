All three analyses completed cleanly. Here is the full worker sizing for **Acme**.

---

## Acme — Data Worker Estimation

**PDF generated:** `Acme_Worker_Estimation_20260804.pdf`
*(Safe to share externally — queuing narrative, no internal formulas)*

---

### Input Summary

| Parameter | Value |
|---|---|
| Total connections | 50 |
| Connector mix | 35 API (Salesforce, HubSpot, Stripe, Shopify, Zendesk) · 15 DB (Postgres, MySQL, Snowflake) |
| Schedule mix | 40% hourly (20 conns) · 25% every 15 min (12 conns) · 35% daily (17 conns) |
| Avg sync duration | 12 min |
| Peak window | 1–5 AM UTC |
| Environments | 2 (prod + staging) |
| Workload type | Recurring incremental, no initial load |
| Growth | → 80 connections in 6 months |

---

### Estimation Model Used

> **Estimation Formula:** `ceil(Concurrent API / 5) + ceil(Concurrent DB / 2)` — statistical heuristic, not the CPU-based billing formula. Once Acme is live, actual Metabase billing data becomes the source of truth.

---

### 1 · Steady-State Worker Estimate (50 connections)

The calculator distributes the 12-minute average duration across frequency intervals and connector types:

| Bucket | API concurrent | DB concurrent |
|---|---|---|
| Sub-hourly (12 conns @ 15 min) | 8 × (12/15) = **6.4** | 3 × (12/15) = **2.4** |
| Hourly (20 conns @ 60 min) | 14 × (12/60) = **2.8** | 6 × (12/60) = **1.2** |
| Daily (17 conns @ 1440 min) | 11 × (12/1440) ≈ **0.09** | 5 × (12/1440) ≈ **0.04** |
| **Total concurrent** | **9.3** | **3.6** |

```
Workers = ceil(9.3 / 5) + ceil(3.6 / 2) = 2 + 2 = 4 Data Workers
```

**→ Steady-state: 4 workers (prod only)**

---

### 2 · Critical Reports — Hourly Batch (Queuing Analysis)

The 20 hourly connections (40% of 50) are the critical report syncs that must complete within the 60-minute window. The queuing calculator treats this as a drain-the-queue problem:

```
20 syncs × 12 min avg = 240 sync-minutes of work in 60 min
Minimum concurrent slots needed ≈ 240 / 60 = 4 slots → 1 worker (5 API slots)
```

| Option | Workers | API Slots | Drain Time | Margin | P90 fits? |
|---|---|---|---|---|---|
| **Minimum** | **1** | **5** | **49.5 min** | **10.5 min** | No |
| **With headroom** | **2** | **10** | **24.5 min** | **35.5 min** | **Yes** |

The 20 critical hourly syncs clear the 60-minute window with just **1 worker**, but P90 durations (15 min) only fit safely with **2 workers**. Because Acme requires reliable hourly delivery, **2 workers ensures the critical batch lands on time even on slow nights.**

---

### 3 · Peak Window Burst Check (1–5 AM UTC) ⚠️

**Worst case: all 17 daily connections fire at 1:00 AM simultaneously** (common with `0 1 * * *` cron expressions that pile up at the top of the hour):

```
API peak = 9.2 (sub-hourly + hourly SS) + 11 (daily burst) = 20.2 concurrent
DB  peak = 3.6 (sub-hourly + hourly SS) + 5  (daily burst) = 8.6  concurrent

Peak workers = ceil(20.2 / 5) + ceil(8.6 / 2) = 5 + 5 = 10 Data Workers
```

**→ Worst-case peak: 10 workers for prod alone**

**Why this matters:** With only 4 workers contracted, any daily syncs that pile up at 1 AM will queue behind the running sub-hourly and hourly load. This can delay critical report syncs.

**Mitigation — stagger the daily syncs across the 4-hour window:**

```
# Instead of all daily syncs at 0 1 * * *:
Salesforce (daily) → 0 1 * * *      # 1:00 AM
HubSpot    (daily) → 0 2 * * *      # 2:00 AM
Postgres   (daily) → 30 1 * * *     # 1:30 AM
Snowflake  (daily) → 30 2 * * *     # 2:30 AM
MySQL      (daily) → 0 3 * * *      # 3:00 AM
```

Spreading 17 daily syncs evenly across the 1–5 AM window (240 minutes) at 12 min/sync means at most 1–2 daily syncs running at any given moment, dropping the burst ceiling back to **~5–6 workers**.

---

### 4 · Two-Environment Sizing (Prod + Staging)

Staging typically mirrors production connections but runs off-peak or at reduced frequency (~50% of prod concurrent load):

| State | Prod | Staging | **Total** |
|---|---|---|---|
| Steady-state | 4 w | 2 w | **6 workers** |
| Peak burst (daily pile-up) | 10 w | 5 w | **≤ 15 workers** |
| Peak burst (with staggering) | 5–6 w | 3 w | **≤ 9 workers** |

---

### 5 · Growth Scenario (50 → 80 connections, 6 months)

| Scenario | API conns | DB conns | API concurrent | DB concurrent | Workers (steady) | Workers (peak burst) |
|---|---|---|---|---|---|---|
| **Current: 50 conns** | 35 | 15 | 9.3 | 3.6 | **4** | **10** |
| **Growth: 80 conns** | 56 | 24 | 15.8 | 6.7 | **8** | **15** |

At 80 connections with two environments: **12 workers steady-state, up to 15 workers peak burst in prod.**

---

### Sizing Recommendation Summary

| Use Case | Recommended Workers |
|---|---|
| Prod only — steady-state (launch) | **4** |
| Prod — with headroom for critical hourly reliability | **5** |
| Prod — peak burst covered (worst-case daily pile-up) | **10** (avoidable with staggering → **5–6**) |
| **Prod + Staging — launch recommendation** | **6–7** |
| Prod + Staging — 6-month growth (80 conns) | **12–14** |

**Recommended contract at launch: 5 workers for prod + 2 for staging = 7 workers total.** This covers steady-state load with headroom, ensures the 20 critical hourly syncs always land on time (2 workers are enough for the batch), and handles moderate daily bursts. Staggering the 17 daily syncs across the 1–5 AM window keeps the burst ceiling well below 7 workers.

**At the 6-month mark (80 connections):** plan to step up to **12–14 workers** to maintain the same headroom ratio.

---

### Key Caveats

1. **Statistical estimate, not CPU billing.** Airbyte Cloud bills using `(maxSourceCPU + maxDestCPU + maxOrchestratorCPU) / 8`. This estimate uses the `ceil(API/5) + ceil(DB/2)` heuristic. Once live, actual Metabase billing data is the source of truth and numbers should be re-validated.
2. **No initial load means no burst risk at go-live.** Incremental-only mode keeps sync durations predictable.
3. **Schedule spreading is the highest-leverage lever.** The difference between 10 workers (uncoordinated daily pile-up) and 5 workers (staggered) is entirely in cron expression design — no additional cost.
4. **Staging sizing is approximate.** If staging runs the full schedule at full frequency, treat it as another prod environment and double the numbers.
