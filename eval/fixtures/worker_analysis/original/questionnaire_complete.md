All analysis is complete. Here is the full Acme prospect estimation.

---

## Acme — Data Worker Estimation
**Date:** 2026-08-04 · **Source:** Statistical estimation (prospect, no Metabase data) · **Model:** `ceil(API/5) + ceil(DB/2)`

---

## Connection Matrix

| Type | Sub-15 min | Hourly | Daily | **Total** |
|---|---|---|---|---|
| **API** (Salesforce, HubSpot, Stripe) | 6 | 9 | 15 | **30** |
| **DB** (Postgres, MySQL) | 3 | 4 | 8 | **15** |
| **All connections** | **9** | **13** | **23** | **45** |

> Frequencies distributed proportionally across both types (66.7% API / 33.3% DB). Average sync duration: **10 min** (user-provided). Sub-hourly interval: 15 min.

---

## Section 1: Steady-State Concurrency

Using the expected-concurrency formula `connections × (duration / interval)` summed across all frequency buckets:

| Group | Connections | Duration | Interval | Concurrent |
|---|---|---|---|---|
| API sub-15min | 6 | 10 min | 15 min | **4.00** |
| API hourly | 9 | 10 min | 60 min | **1.50** |
| API daily | 15 | 10 min | 1,440 min | **0.10** |
| DB sub-15min | 3 | 10 min | 15 min | **2.00** |
| DB hourly | 4 | 10 min | 60 min | **0.67** |
| DB daily | 8 | 10 min | 1,440 min | **0.06** |

```
API total concurrent: 4.00 + 1.50 + 0.10 = 5.60
DB  total concurrent: 2.00 + 0.67 + 0.06 = 2.73

Workers = ceil(5.60 / 5) + ceil(2.73 / 2)
        = ceil(1.12)     + ceil(1.37)
        = 2              + 2
        = 4 workers  (steady-state, production)
```

The **sub-15-min connections dominate** — they're always running and together they consume 4.0 concurrent API + 2.0 concurrent DB slots continuously, almost filling a worker on each dimension by themselves.

---

## Section 2: Peak Window Analysis — 2–6 AM UTC

Three models of the 2–6 AM peak, ranging from worst-case to optimal:

### 2A: Worst-case burst (all 23 daily syncs start simultaneously at 2:00 AM)

```
API concurrent: 4.0 (sub-15) + 1.5 (hourly) + 15.0 (all daily) = 20.5
DB  concurrent: 2.0 (sub-15) + 0.7 (hourly) +  8.0 (all daily) = 10.7

Workers = ceil(20.5 / 5) + ceil(10.7 / 2) = 5 + 6 = 11 workers
```

This is the planning ceiling. It assumes every daily connection fires at the same millisecond with zero stagger — almost never happens in practice, but it tells you what a botched schedule costs.

### 2B: Queuing model — daily batch vs. 1-hour freshness window

Since key reports need data within **1 hour**, the binding constraint is: all 23 daily syncs must complete within 60 minutes of their start. The queuing calculator drains the batch serially through a fixed pool of concurrent slots:

**DB daily batch (8 syncs, 10 min avg, 60-min window):**

| Workers | Concurrent Slots | Drain Time (avg) | Fits in 60 min? |
|---|---|---|---|
| 1 | 2 | 41.5 min | ✅ Yes — 18.5 min buffer |
| 2 | 4 | 20.5 min | ✅ Yes — 39.5 min buffer |

**API daily batch (15 syncs, 10 min avg, 60-min window):**

| Workers | Concurrent Slots | Drain Time (avg) | Fits in 60 min? |
|---|---|---|---|
| 1 | 5 | 31.0 min | ✅ Yes — 29 min buffer |
| 2 | 10 | 15.5 min | ✅ Yes — 44.5 min buffer |

**Key insight:** Even a single worker handles the daily batch well within the 1-hour freshness requirement. The daily batch is **not the capacity driver** — it's the always-on sub-15-min connections that set the floor.

### 2C: Staggered schedule — realistic peak (recommended)

By splitting the daily batch into two sub-batches with 20-min offset and starting DB connections 5 minutes after API:

```
At peak moment (2:00 AM, first sub-batch fires):
  API concurrent: 4.0 (sub-15) + 1.5 (hourly) + 8.0 (daily sub-batch A) = 13.5
  DB  concurrent: 2.0 (sub-15) + 0.7 (hourly) + 4.0 (daily, staggered)  =  6.7

  Workers = ceil(13.5 / 5) + ceil(6.7 / 2) = 3 + 4 = 7 workers
```

With good scheduling design, peak drops from **11 → 7 workers**.

---

## Section 3: Two-Environment Analysis (Prod + Staging)

Staging is modeled as daily-only (no sub-15min or hourly syncs; validates data before production schedules run):

| Environment | Connections | Schedule | Concurrent API | Concurrent DB | **Workers** |
|---|---|---|---|---|---|
| Production | 45 | 20/30/50 mix | 5.60 | 2.73 | **4** |
| Staging | 45 | Daily only | 0.21 | 0.10 | **2** |
| **Combined** | — | — | — | — | **6** |

Staging adds a persistent **+2 workers** regardless of connection count, because all 45 connections run daily at once and `ceil(0.21/5) + ceil(0.10/2) = 1 + 1 = 2`. It's the smallest floor the model can produce.

---

## Section 4: Growth Projection to 80 Connections

| Scenario | Connections | API concurrent | DB concurrent | Steady DW (prod) | Two-env total |
|---|---|---|---|---|---|
| Today | 45 | 5.60 | 2.73 | 4 | 6 |
| +25% | 56 | 6.90 | 3.41 | 4 | 6 |
| +50% | 68 | 8.40 | 4.15 | 4 | 6 |
| **80-conn target** | **80** | **9.51** | **4.59** | **5** | **7** |
| +100% | 90 | 11.21 | 4.76 | 6 | 8 |

The steady-state worker count stays at **4** until approximately **65–68 connections**, because API capacity has room to absorb connections without a new worker (`ceil(API_cc/5)` doesn't tick over until API concurrent exceeds 10.0, requiring worker 3). The upgrade trigger is when production alone crosses **5 steady-state workers** at ~75–80 connections.

---

## Section 5: Schedule Recommendations

Staggering drops peak from 11 → 7 workers without any additional capacity purchase.

### Quartz Cron Expressions (Internal Use)

**Sub-15-min connections (9 total — keep near-current schedule, offset by connector):**
```
API connections (6): stagger across minutes 0, 2, 5, 7, 10, 12
  Salesforce A:  0 0/15 * * * ?   (00:00, 00:15, 00:30 …)
  Salesforce B:  0 2/15 * * * ?   (00:02, 00:17, 00:32 …)
  HubSpot:       0 5/15 * * * ?
  Stripe:        0 7/15 * * * ?
  (+ 2 more at :10 and :12)

DB connections (3): offset after API burst
  Postgres A:    0 1/15 * * * ?   (00:01, 00:16, 00:31 …)
  Postgres B:    0 3/15 * * * ?
  MySQL A:       0 8/15 * * * ?
```

**Hourly connections (13 total — avoid :00 to not collide with sub-15min):**
```
API connections (9): start at :05 through :25, 2-min spacing
  0 5  * * * ?   0 7  * * * ?   0 10 * * * ?
  0 12 * * * ?   0 15 * * * ?   0 17 * * * ?
  0 20 * * * ?   0 22 * * * ?   0 25 * * * ?

DB connections (4): start at :30–:45, 5-min spacing
  0 30 * * * ?   0 35 * * * ?   0 40 * * * ?   0 45 * * * ?
```

**Daily connections (23 total — split into two sub-batches, 02:00–03:00 UTC):**
```
API daily — batch A (8 syncs): 0 0  2 * * ?  (02:00 UTC)
API daily — batch B (7 syncs): 0 20 2 * * ?  (02:20 UTC)
DB  daily  (8 syncs):           0 5  2 * * ?  (02:05 UTC — between API batches)
```

**Before/after worker comparison:**

| Window | Without stagger | With stagger | Δ |
|---|---|---|---|
| 2:00 AM burst | 11 workers | 7 workers | **−4** |
| Steady-state | 4 workers | 4 workers | 0 |
| 2-env combined | 6 workers | 6 workers | 0 |

---

## Section 6: Contract Recommendation

| Option | Workers | What It Covers | Risk |
|---|---|---|---|
| **A — Minimum** | **4** | Prod only, current load | No room for staging; any burst trips the limit |
| **B — Floor** | **6** | Prod (4) + Staging (2) today | Correct floor, zero headroom; retry burst + stagger failure hits the ceiling |
| **C — Recommended** ⭐ | **8** | Prod+Staging today, grows to ~65 prod connections | Buffer for retries and daily-batch stagger failures; avoids upgrade before 6-month mark |
| **D — Growth-Ready** | **10** | Prod+Staging at 80-connection target | Covers entire 6-month roadmap in one purchase; peak-burst comfortable |

### Recommended: **8 workers to start (Option C)**

- **Steady-state** (production): 4 workers — always comfortable
- **Staging overlay**: +2 workers — always on
- **Buffer**: +2 workers above combined floor, absorbs:
  - Retry bursts (failed syncs re-queue and temporarily double-run)
  - No-initial-load caveat: incremental is assumed, but first runs after new connections are added may behave like an initial sync
  - Daily stagger not-yet-configured: if all 23 daily syncs start together on day 1, 8 workers gives runway

- **Growth runway**: Stays within 8 workers until ~65 production connections
- **Upgrade trigger**: When production connections reach **75–80**, upgrade to 10 workers to cover the 6-month target

### Growth path:

```
Launch (45 conns):  8 workers  — comfortable headroom
Month 3 (~63 conns): 8 workers  — still inside contract
Month 6 (~80 conns): upgrade to 10 workers
```

---

## Key Caveats

> **⚠️ Estimation model, not billing truth.** This uses `ceil(API_concurrent/5) + ceil(DB_concurrent/2)`. The actual Airbyte Cloud billing formula is `(maxSourceCPU + maxDestCPU + maxOrchestratorCPU) / 8`, which maps CPU resource requests to workers — not connection type counts. The two models agree at moderate loads but diverge at extremes (especially for high-CPU DB connectors like Postgres CDC or Oracle). **Re-validate against Metabase billing data within the first 30 days after go-live.**

> **No-initial-load assumption held.** If any connection needs a full historical load after go-live (even for a "small" Postgres table), treat that sync as a DB job running for hours, not 10 minutes. This can temporarily spike to 2–3× normal DB concurrency.

> **Staging timing matters.** If staging runs its daily batch during the same 2–6 AM UTC window as production, the 6-worker combined total may peak simultaneously. Consider scheduling staging daily syncs at 06:00–08:00 UTC to keep the two environments isolated.

> **Sub-15-min connections are the capacity anchor.** The 9 sub-15-min connections consume ~6 concurrent slots at any moment and do not benefit from the queuing model. They are the minimum floor that can't be optimized away without changing the schedule to hourly.
