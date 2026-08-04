The numbers are in. Here's the full analysis.

---

## Acme Prospect — Worker Estimation Analysis
**Date:** 2026-08-04 · **Method:** Statistical estimation model (prospect — no Metabase data)

> ⚠️ **Estimation caveat:** This uses `ceil(API/5) + ceil(DB/2)`. Once Acme is on Cloud, Metabase billing data (CPU-based formula) becomes the source of truth. Treat all numbers here as pre-sales sizing, not billing guarantees.

---

### Input Assumptions

| Parameter | Value | Derived From |
|---|---|---|
| Total connections | 50 | Provided |
| API connectors | 35 (70%) | Salesforce, HubSpot, Stripe, Shopify, Zendesk |
| Database connectors | 15 (30%) | Postgres, MySQL, Snowflake |
| Sub-hourly (every 15 min) | 12 connections | 25% × 50 |
| Hourly | 20 connections | 40% × 50 |
| Daily | 17 connections | 35% × 50 (land in 1–5 AM window) |
| Avg sync duration | 12 min | Provided |
| P90 sync duration | ~20 min | Conservative (12 min × 1.67); above empirical fleet P90s |
| Peak window | 1–5 AM UTC | Provided |
| Growth target | 80 connections | 6-month projection |
| Environments | 2 (prod + staging) | Provided |
| Workload type | Incremental only | No initial load |

---

### 1 · Connection Breakdown (Proportional Distribution)

Frequency × type distributed proportionally (70% API / 30% DB):

| Schedule | Total | API | DB |
|---|---|---|---|
| Every 15 min | 12 | 8 | 3 |
| Hourly | 20 | 14 | 6 |
| Daily (peak window) | 17 | 11 | 5 |
| **Total** | **50** | **35** | **15** |

---

### 2 · Steady-State Concurrency (Billing Baseline)

Using `concurrency = connections × (avg_duration / interval)` per schedule group:

| Group | Interval | API Concurrent | DB Concurrent | Formula |
|---|---|---|---|---|
| Every 15 min | 15 min | 8 × (12/15) = **6.40** | 3 × (12/15) = **2.40** | 0.80 utilization ratio |
| Hourly | 60 min | 14 × (12/60) = **2.80** | 6 × (12/60) = **1.20** | 0.20 utilization ratio |
| Daily (in 240-min peak window) | 240 min | 11 × (12/240) = **0.55** | 5 × (12/240) = **0.25** | 0.05 utilization ratio |
| **Totals** | | **9.75 API** | **3.85 DB** | |

**Steady-state workers = ceil(9.75 / 5) + ceil(3.85 / 2) = 2 + 2 = `4 workers`**

This is the floor — what billing sees during normal operation between hourly bursts. It matches the toolkit's output of 4 workers.

---

### 3 · Peak Burst Analysis (Top-of-Hour, 1:00–4:00 AM UTC)

The most important scenario: at every `:00` mark during the peak window, **both** hourly and sub-hourly connections fire simultaneously. That's a synchronized burst of **32 connections** starting at the same instant (with a 12-minute avg run duration).

| Component | Count | API | DB |
|---|---|---|---|
| Hourly connections starting | 20 | 14.0 | 6.0 |
| Sub-hourly connections starting | 12 | 8.4 | 3.6 |
| Daily (steady-state, in window) | — | 0.55 | 0.25 |
| **Burst totals** | | **22.95** | **9.85** |

**Burst workers = ceil(22.95 / 5) + ceil(9.85 / 2) = 5 + 5 = `10 workers`**

This spike lasts approximately 12 minutes (avg sync duration), repeating 4× per night (1:00, 2:00, 3:00, 4:00 AM UTC). Between bursts (at `:15`, `:30`, `:45`), only sub-hourly fires and load drops back to **4 workers**.

```
Worker usage pattern during 1–5 AM UTC:
                                                              
  10 ┤ ████                    ████                    ████
   8 ┤ ████                    ████                    ████
   6 ┤ ████                    ████                    ████
   4 ┤ ████ ████ ████ ████ ████ ████ ████ ████ ████ ████
   2 ┤ ████ ████ ████ ████ ████ ████ ████ ████ ████ ████
   0 └────────────────────────────────────────────────────
     :00  :15  :30  :45  :00  :15  :30  :45  :00  :15
     │←── 1 AM ─────────────────────────────── 3 AM ───→│
     
     ▓ = 10 workers (burst at :00)    █ = 4 workers (steady-state)
```

---

### 4 · Queuing Model — Daily Batch (1–5 AM Window)

The 17 daily connections can be modeled as a queued batch over 240 minutes. The queuing calculator shows they are extremely lightweight even without dedicated workers — they'll drain comfortably using the capacity already needed for the sub-hourly/hourly workload.

| Daily batch | Workers needed | Concurrent slots | Drain time | Margin |
|---|---|---|---|---|
| 11 daily API syncs | 1 worker | 5 concurrent | ~27 min | ~213 min |
| 5 daily DB syncs | 1 worker | 2 concurrent | ~31 min | ~209 min |
| **Combined** | **2 workers** | — | ~31 min max | **~209 min spare** |

**Key insight:** The daily batch is trivially small relative to the 240-minute window. Even 1 worker clears all 17 daily connections in under 35 minutes. The burst from hourly + sub-hourly connections dominates sizing — **not the daily batch**.

---

### 5 · Growth Projections (50 → 80 Connections Over 6 Months)

Same ratio assumptions maintained (70% API, 30% DB, same schedule distribution, same 12-min avg duration):

| Scenario | Connections | Steady-State | Peak Burst (prod) | P99 Est. |
|---|---|---|---|---|
| **Today** | 50 | 4 workers | 10 workers | ~8 workers |
| +25% | 62 | 6 workers | 13 workers | ~10 workers |
| +50% | 75 | 6 workers | 15 workers | ~12 workers |
| **6-mo target** | 80 | 8 workers | 17 workers | ~14 workers |

At 80 connections the burst reaches 17 workers — a 70% increase from today. This is the key growth inflection that should trigger a contract review.

---

### 6 · Dual-Environment Impact (Prod + Staging)

Three staging configurations to consider:

| Configuration | Prod Workers | Staging Workers | **Total** |
|---|---|---|---|
| Staging mirrors prod exactly (same schedule) | 10 burst | 10 burst | **20 workers** |
| Staging at 50% load (offset peak) | 10 burst | 5 burst | **15 workers worst-case** |
| Staging offset by 4–6h (staggered) | 10 burst | max at once | **10 workers** |

**Recommendation on staging:** Stagger the staging peak by at least 4 hours from prod. This eliminates the dual-burst problem and keeps the combined need at 10 workers rather than 15–20. The 1–5 AM prod window pairs well with a 7–11 AM staging window, well inside business hours for monitoring.

---

### 7 · Contract Recommendations

```
┌──────────────────────────────────────────────────────────────────┐
│                  RECOMMENDED CONTRACT TIERS                      │
├─────────────────────────────────┬──────────┬─────────────────────┤
│ Scenario                        │ Workers  │ Notes               │
├─────────────────────────────────┼──────────┼─────────────────────┤
│ Prod only — minimum viable      │  4       │ Floor; no burst room│
│ Prod only — burst headroom (P99)│  10      │ Covers peak cleanly │
│ Both envs — staggered schedule  │  10–12   │ Recommended start   │
│ Both envs — simultaneous peaks  │  14–16   │ Conservative/safe   │
│ 6-mo growth target (both envs)  │  20–24   │ Plan for upgrade    │
└─────────────────────────────────┴──────────┴─────────────────────┘

RECOMMENDED STARTING CONTRACT: 10–12 workers
GROWTH TRIGGER: Upgrade to 18–20 workers when connections exceed 65
```

**Why 10–12 and not 4?** The steady-state of 4 workers covers average operation but offers zero headroom for the 10-worker burst that occurs every hour during the 1–5 AM window. Under capacity enforcement, syncs arriving during a burst would queue. Starting at 10–12 covers prod cleanly and gives staging capacity via slack.

---

### 8 · Optimization Levers (Reduce Burst Without Adding Workers)

The 10-worker burst is entirely caused by **synchronized start times** — all 32 hourly + sub-hourly connections fire at the same `:00` moment. Three practical levers:

**A) Stagger hourly connections across the hour**
Spread the 20 hourly connections in 4-minute offsets instead of all firing at `:00`. Example:
- `0 0 1/1 * * ?` → `0 5 1/1 * * ?`, `0 10 1/1 * * ?`, etc.
- This flattens the burst from a 10-worker spike to ~4–5 workers sustained — potentially halving the required contract.

**B) Shift daily connections outside the hourly burst**
Schedule all 17 daily connections at `:30` past the hour (e.g., 1:30, 2:30 AM). They complete well within the 4-hour window and don't compete with the hourly burst.

**C) Stagger staging peak 4–6 hours from prod**
1–5 AM prod / 7–11 AM staging eliminates dual-env burst overlap entirely.

**Projected impact of staggering (A alone):**
Burst drops from 32 simultaneous → ~8 concurrent at any given minute → **5 workers covers everything**, potentially bringing the contract from 10–12 down to **6–8 workers**.

---

### Summary Card

| Metric | Value |
|---|---|
| **Steady-state workers (billing baseline)** | **4** |
| **Peak burst workers (top of hour, prod)** | **10** |
| **Recommended contract — start** | **10–12** |
| **Recommended contract — 6-month growth** | **18–20** |
| **Burst duration** | ~12 min, 4× per night |
| **Daily batch workers needed** | 2 (trivial — rides on existing capacity) |
| **Critical schedule risk** | All hourly + sub-hourly start simultaneously |
| **Single biggest optimization** | Stagger hourly start times → reduce burst 40–50% |
