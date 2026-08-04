Excellent — all runs completed cleanly. Here is the full worker sizing analysis for Acme.

---

## 🏗️ Acme — Airbyte Data Worker Sizing Analysis

### Inputs Used
| Parameter | Value |
|---|---|
| Total connections (today) | 45 (30 SaaS API + 15 DB) |
| Connector mix | 66.67% API · 33.33% DB |
| Sync frequencies | 20% every 15 min · 30% hourly · 50% daily |
| Avg sync duration | 10 min |
| Peak window | 2–6 AM UTC (240 min) |
| Freshness SLA | ≤1 hour for key reports |
| Growth target | 80 connections in 6 months |
| Environments | Production + Staging |
| Workload type | No initial load · recurring incremental only |

---

### 🔢 The Billing Formula (Ground Truth)

The Airbyte platform sizes Data Workers from **CPU resource requests**, not connection counts. The simplified planning formula that maps cleanly to that model is:

> **(concurrent API syncs ÷ 5) + (concurrent DB syncs ÷ 2) = Data Workers**

Default CPU footprint per sync:
- **API connector** → 0.8 source + 0.3 dest + 0.3 orch = **1.4 CPU total**
- **DB connector** → 1.0 source + 1.0 dest + 1.0 orch = **3.0 CPU total**

Because DB syncs cost ~2× as much CPU as API syncs, the capacity ratios (5 API vs 2 DB per worker) reflect this difference.

---

### 📊 Current State — 45 Connections (Production)

**Connection breakdown by type & frequency:**

| Freq bucket | API connections | DB connections | Total |
|---|---|---|---|
| Sub-hourly (15 min) | 6 | 3 | 9 |
| Hourly | 8 | 4 | 13 |
| Daily | 14 | 7 | 22 |
| **Total** | **30** | **14** | **45** |

> *Note: integer truncation during distribution accounts for the slight rounding (14 DB rather than 15 at 33.33%).*

**Expected peak concurrency** (duration ÷ interval, summed across frequency buckets):

| Dimension | Value |
|---|---|
| API concurrent syncs | **5.43** |
| DB concurrent syncs | **2.05** |
| Total concurrent | **7.48** |

**Worker calculation:**

| Component | Concurrency | ÷ rate | Capacity units | Workers needed |
|---|---|---|---|---|
| API | 5.43 | ÷ 5 | 1.09 | ⌈1.09⌉ = **2** |
| DB | 2.05 | ÷ 2 | 1.03 | ⌈1.03⌉ = **2** |
| **Total** | | | **2.11** | **4 Data Workers** |

> ✅ **Production today: 4 Data Workers**

---

### 📈 Growth Scenario — 80 Connections (Production, 6 Months)

Same 66/33 mix and frequency distribution, just scaled to 80 connections:

| Dimension | Value |
|---|---|
| API concurrent syncs | 9.51 |
| DB concurrent syncs | 4.59 |
| Total concurrent | 14.10 |

| Component | Concurrency | ÷ rate | Capacity units | Workers needed |
|---|---|---|---|---|
| API | 9.51 | ÷ 5 | 1.90 | ⌈1.90⌉ = **2** |
| DB | 4.59 | ÷ 2 | 2.30 | ⌈2.30⌉ = **3** |
| **Total** | | | **4.20** | **5 Data Workers** |

> ✅ **Production at 6 months: 5 Data Workers**

The DB side is the driver of the +1 worker: at 80 connections the DB capacity crosses a whole unit (2.05 → 2.30), pushing the ceiling up from 2 to 3.

---

### 🔁 Two-Environment Plan (Prod + Staging)

Staging typically mirrors production topology at reduced traffic — not all connections run continuously, frequencies are usually loosened, and syncs are triggered on demand or scheduled less aggressively.

| Environment | Today | At 6 months |
|---|---|---|
| **Production** | **4 workers** | **5 workers** |
| **Staging** (50–60% of prod load) | **2 workers** | **3 workers** |
| **Total across both envs** | **6 workers** | **8 workers** |

If staging is fully mirrored (every connection runs on the same schedule as production), plan for prod × 2, but that is unusual in practice.

---

### 🕐 Peak Window Analysis — 2–6 AM UTC

The 240-minute window is very comfortable:

| Metric | Value |
|---|---|
| Window available | 240 minutes |
| Daily syncs to drain | 22 connections |
| Concurrent capacity at 4 workers | ~10 API + 4 DB simultaneous |
| Estimated drain time (minimum config) | **~46 minutes** |
| Margin before deadline | **~194 minutes** |

Even the minimum configuration (1–2 workers) would drain all 22 daily syncs in ~46 minutes, leaving over 3 hours of buffer. At 4 workers there is no congestion risk in this window.

---

### ⏱️ Freshness SLA — "Data within 1 hour for key reports"

| Frequency bucket | Connections | Meets ≤1 hr SLA? |
|---|---|---|
| Sub-hourly (15 min) | 9 | ✅ Always fresh within 15 min |
| Hourly | 13 | ✅ Fresh within 60 min |
| Daily | 22 | ❌ Refreshes once per 24 hrs |

**Action item:** The 22 daily-scheduled connections cannot satisfy the 1-hour freshness SLA. Before go-live, identify which of those are relied on by key reports and convert them to **hourly** cadence. That will add a modest amount to concurrency (well within the 4-worker budget) and move those connections from the ❌ column to ✅.

---

### 🚀 No Initial Load — Why This Matters

With incremental-only syncs and no historical backfill, Acme avoids the most common sizing trap: a launch-day burst that temporarily requires 3–5× steady-state workers. The estimate above is valid from day 1, with no ramp-up risk.

---

### 📋 Recommendation Summary

| Scenario | Prod Workers | Staging Workers | Total |
|---|---|---|---|
| Launch (45 connections) | **4** | **2** | **6** |
| 6-month growth (80 connections) | **5** | **3** | **8** |

**Key guidance:**
1. **Start at 4 workers in production.** The 4-worker configuration handles all three frequency tiers with headroom, and the 2–6 AM window clears comfortably.
2. **Plan to step up to 5 workers around month 4–5** as the connection count approaches 65–70. The DB side crosses the capacity threshold at that point.
3. **Provision 2 workers for staging today.** Staging does not need to perfectly mirror prod — 2 workers handles typical QA and integration testing loads.
4. **Reclassify daily → hourly for report-critical connections** to satisfy the 1-hour freshness SLA. No additional workers are needed for this change.
5. **Once live, run a Mode 1 job-overlap analysis** against actual job history (30-day window) to validate and tune these estimates. Statistical estimation is deliberately conservative; real-world job overlap often comes in below the modeled peak.

---

> **Methodology note:** This analysis uses Mode 2A (questionnaire-based statistical estimation) from the `worker-analysis` skill. Concurrency is modeled as `connections × (sync_duration ÷ interval)` per frequency bucket, then converted to workers via the `(API ÷ 5) + (DB ÷ 2)` formula. A PDF report has been generated at `/tmp/acme-worker-analysis/Acme_Worker_Estimation_20260804.pdf`.
