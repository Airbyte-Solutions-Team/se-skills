The calculator completed cleanly. Here is the full analysis for Acme.

---

## 🏢 Acme — Worker Sizing Estimate

> ⚠️ **Estimation caveat:** This uses the API/DB concurrency model (`ceil(DB/2) + ceil(API/5)`), not the actual CPU-based billing formula. Once Acme is on the platform, analyze real job history for ground-truth numbers.

---

### Connection Matrix (Input Decomposition)

| Type | Sub-hourly (15 min) | Hourly | Daily | **Total** |
|---|---|---|---|---|
| API (Salesforce, HubSpot, Stripe…) | 6 | 9 | 15 | **30** |
| DB/File (Postgres, MySQL…) | 3 | 4 | 8 | **15** |
| **Total** | **9** | **13** | **23** | **45** |

---

### 📊 Seven Sizing Views (All Seven, Exactly as Returned)

| Sizing View | Workers | Notes |
|---|:---:|---|
| **Steady-state requirement** | **4** | Long-run average concurrency: 5.6 API + 2.72 DB concurrent |
| **Peak-window drain requirement** | **2** | Workers to drain all daily syncs within the 60-min freshness window |
| **Worst-case burst** | **11** | All 23 daily syncs fire simultaneously (20.5 API + 10.67 DB concurrent) |
| **Production-only requirement** | **4** | Prod environment steady state only |
| **Combined prod + staging** | **6** | Prod (4) + staging environment (2) |
| **Future-growth requirement** | **8** | Combined prod + staging at 80 connections |
| **✅ Recommended contract capacity** | **8** | `max(combined_steady 6 + headroom 2, future_growth 8)` |

---

### 🎯 Final Recommendation: **8 Data Workers**

**Decision logic applied by the calculator:**

```
Combined prod + staging   →  6 workers
+ Headroom                → +2 workers  (absorbs burst delta, covers daily + multi-env variability)
─────────────────────────────────────────
Headroom-adjusted total   →  8 workers

Future growth (80 conns)  →  8 workers

Recommended = max(8, 8)   =  8 Data Workers  ✅
```

---

### 📈 Key Insights by Scenario

#### 1. Steady-State is Comfortable — But Bursts Are the Risk

With 10-minute average sync duration, Acme's **steady-state concurrency is light** (5.6 API + 2.72 DB = ~8.3 jobs in flight at any given moment → **4 workers**). This reflects the efficient cadence of short, incremental syncs.

However, the **worst-case burst reaches 11 workers** — that's what happens when all 23 daily syncs pile up at the 2 AM UTC window simultaneously. The recommendation of 8 workers intentionally sits below the theoretical 11-worker burst ceiling because:
- 10-minute syncs drain the queue quickly (within the 60-min freshness window)
- Staggering daily syncs even slightly across the 2–6 AM window would prevent simultaneous firing
- The freshness window check shows the peak-window drain needs only **2 workers** to clear all daily jobs within 60 minutes

> **Maintenance window is healthy:** 23 daily syncs fit comfortably within a 4-hour window (~168 capacity). Acme has plenty of scheduling flexibility at the 2–6 AM UTC target window.

#### 2. Staging Environment Adds ~2 Workers

The 6-worker figure for combined prod + staging accounts for a staging instance running at roughly half the prod load. This is the realistic operating baseline.

#### 3. Growth Path to 80 Connections

Scaling from 45 → 80 connections (78% growth) pushes the combined prod+staging estimate from 6 → 8 workers. The recommendation already prices this in, so Acme can grow to 80 connections **without a contract upgrade** — the 8-worker contract covers them end-to-end.

| Scenario | Connections | Est. Workers |
|---|:---:|:---:|
| Current (prod only) | 45 | 4 |
| Current (prod + staging) | 45 | 6 |
| Growth target (prod + staging) | 80 | 8 |
| Worst-case burst (current) | 45 | 11 |

---

### 💡 Scheduling Recommendations

Given the 2–6 AM UTC peak window and 60-min freshness requirement:

- **Sub-hourly syncs (9 connections):** Keep as-is — 15-minute cadence with 10-min average duration leaves 5 minutes of buffer per cycle. No changes needed.
- **Hourly syncs (13 connections):** Spread across `:00`, `:15`, `:30`, `:45` start offsets to avoid simultaneous firing at the top of the hour.
- **Daily syncs (23 connections):** Rather than all firing at 02:00 UTC, stagger across the 2–6 AM window in ~10-minute intervals. This alone would bring the realistic burst well below 11 workers (likely to 5–6), keeping Acme safely within the 8-worker recommendation.

---

### ✅ Summary for Acme

| | |
|---|---|
| **Recommended contract** | **8 Data Workers** |
| **Covers today** | 45 connections, prod + staging |
| **Covers growth** | Up to 80 connections, no upgrade needed |
| **Burst risk** | Low — 10-min syncs self-clear within freshness window |
| **Scheduling action** | Stagger daily syncs across 2–6 AM UTC window |
| **Model used** | Estimation (API/DB concurrency heuristic) — refine after onboarding |
