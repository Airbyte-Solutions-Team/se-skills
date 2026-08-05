The calculator has run cleanly. Let me now present the full deterministic sizing analysis.

---

## ⚡ Acme — Prospect Worker Estimation

> **This is a statistical estimate** using the API/DB concurrency heuristic model (`ceil(DB concurrent / 2) + ceil(API concurrent / 5)`). It does **not** use the CPU-based billing formula, which requires live Cloud data. Once Acme is on-platform, Metabase billing data becomes the authoritative source.

---

### Connection Matrix (45 Connections Total)

| Type | Sub-hourly (15 min) | Hourly | Daily | Total |
|---|---|---|---|---|
| **API** (Salesforce, HubSpot, Stripe) | 6 | 9 | 15 | **30** |
| **DB/File** (Postgres, MySQL) | 3 | 4 | 8 | **15** |
| **Total** | 9 | 13 | 23 | **45** |

---

### Seven Sizing Views (Deterministic)

| # | Sizing View | Workers |
|---|---|:---:|
| 1 | **Steady-state** — schedules spread across intervals | **4** |
| 2 | **Peak-window drain** — flush daily batch within 60 min freshness SLA | **2** |
| 3 | **Worst-case burst** — all 23 daily syncs fire simultaneously at 2 AM UTC | **11** |
| 4 | **Production-only** | **4** |
| 5 | **Combined prod + staging** (staging modelled as daily-only) | **6** |
| 6 | **Future-growth** (80 connections, both environments) | **8** |
| 7 | ✅ **Recommended contract capacity** | **8** |

---

### How the Recommendation Was Derived

**Step 1 — Steady-state concurrency at 45 connections (prod):**
> At any given moment: 5.60 API jobs + 2.72 DB jobs running concurrently
> → `ceil(5.60 / 5) + ceil(2.72 / 2)` = **2 + 2 = 4 workers**

**Step 2 — Staging environment (+1 env, daily-only model):**
> Staging adds **2 workers** → combined prod + staging = **6 workers**

**Step 3 — Headroom:**
> Burst delta = 11 (worst-case) − 6 (combined steady) = 5; headroom capped at 2.
> Has daily syncs + 2 environments → headroom = **2 workers**
> `combined + headroom = 6 + 2 = 8`

**Step 4 — Growth check:**
> At 80 connections: prod=6 + staging=2 = **8 combined** → ties with headroom path
> `recommended = max(8, 8) = `**8 Data Workers**

---

### Peak Window Analysis (2–6 AM UTC)

The 2–6 AM maintenance window is where the **daily batch risk concentrates**. All 23 daily connections are likely scheduled in this 4-hour window:

| Scenario | API Concurrent | DB Concurrent | Workers |
|---|---|---|---|
| Perfectly staggered (ideal) | 5.6 | 2.7 | **4** |
| Drain within 60-min freshness SLA | 2.5 slots → 1 worker | 1.3 slots → 1 worker | **2** |
| All daily syncs at 2:00 AM simultaneously | 20.5 | 10.7 | **11** |

> **Key insight:** The 8-worker recommendation comfortably covers steady-state and staggered-schedule scenarios. The burst risk of 11 only materialises if Acme schedules *all* 23 daily syncs at the same cron time (e.g., `0 2 * * *` on every connection). Staggering them across 2–6 AM keeps peak below 8.

---

### Freshness SLA Check (1-hour window)

To drain the full daily batch (23 connections × 10 min = 230 sync-minutes) within the 60-minute freshness window:
- **API drain:** 15 connections × 10 min ÷ 60 min = 2.5 slot-minutes → `ceil(2.5 / 5)` = **1 API worker**
- **DB drain:** 8 connections × 10 min ÷ 60 min = 1.33 slot-minutes → `ceil(1.33 / 2)` = **1 DB worker**
- **Total drain need: 2 workers** ✅ Well within the 8-worker contract

---

### Growth Trajectory

| Milestone | Connections | Prod Workers | + Staging | Total |
|---|---|---|---|---|
| **Today** | 45 | 4 | +2 | 6 |
| **+6 months (growth target)** | 80 | 6 | +2 | **8** |
| **+50% beyond target** | 120 | ~8–9 | +3 | ~11–12 |

> At 80 connections, the 8-worker contract exactly covers combined prod+staging with no headroom surplus — growth beyond 80 would warrant revisiting contract sizing.

---

### ✅ Recommendation Summary

| Item | Value |
|---|---|
| **Recommended starting contract** | **8 Data Workers** |
| **Basis** | `max(combined 6 + headroom 2, future-growth 8)` |
| **Burst ceiling (unstaggered)** | 11 workers — mitigated by schedule staggering |
| **Growth headroom** | Covers 80-connection growth target exactly; plan for upgrade at ~85–90 connections |
| **Scheduling advice** | Stagger daily syncs across the 2–6 AM window in 10–15 min offsets; 4 hours × 60 min ÷ 23 daily connections = ~10 min apart keeps concurrent usage well below the 8-worker ceiling |

---

> **Caveat:** This estimate uses the `ceil(DB/2) + ceil(API/5)` heuristic, not the actual CPU-based billing formula (`(maxSourceCPU + maxDestCPU + maxOrchestratorCPU) / 8`). Actual billing may differ once Acme is on the platform. Would you like me to generate the **two PDF reports** (customer-facing executive report + internal detailed report) for this analysis?
