The calculator ran cleanly. Here is the full analysis for Acme.

---

## Acme — Worker Sizing Estimate (Prospect)

**Analysis Date:** 2026-08-05 | **Method:** Deterministic Questionnaire Calculator | **Estimation Model (heuristic)**

> ⚠️ **Caveat:** This uses the estimation model (`ceil(API concurrent / 5) + ceil(DB concurrent / 2)`), not the CPU-based billing formula that runs on the actual platform. Once Acme is live, Metabase billing data is the source of truth.

---

### Connection Matrix

The inputs resolve to this concurrency matrix after proportional splitting:

| Type | Sub-hourly (every 15 min) | Hourly | Daily | **Total** |
|------|--------------------------|--------|-------|-----------|
| API (Salesforce, HubSpot, Stripe) | 6 | 9 | 15 | **30** |
| DB (Postgres, MySQL) | 3 | 4 | 8 | **15** |
| **Total** | **9** | **13** | **23** | **45** |

---

### Seven Sizing Views (All Returned by Calculator)

| Sizing View | Workers | What It Represents |
|---|---|---|
| **1. Steady-state requirement** | **4** | Long-run average: schedules spread evenly, syncs overlap naturally. Concurrent API ≈ 5.6, DB ≈ 2.72 → `ceil(5.6/5) + ceil(2.72/2) = 2 + 2` |
| **2. Peak-window drain requirement** | **2** | Workers needed to drain all 23 daily syncs within Acme's 1-hour freshness SLA. API: 1 worker, DB: 1 worker |
| **3. Worst-case burst requirement** | **11** | All 23 daily syncs fire simultaneously at 2 AM. Concurrent API peaks at 20.5, DB at 10.67 → `ceil(20.5/5) + ceil(10.67/2) = 5 + 6` |
| **4. Production-only requirement** | **4** | Same as steady-state — prod environment alone |
| **5. Combined prod + staging** | **6** | Prod steady-state (4) + staging environment modeled as daily-only (2) |
| **6. Future-growth requirement** | **8** | Combined prod + staging at 80 connections target |
| **7. ✅ Recommended contract capacity** | **8** | `max(combined_steady(6) + headroom(2), future_combined(8))` = 8 |

---

### How the Recommendation Was Built

```
Combined prod + staging (current)   = 6 workers
Headroom (burst delta = 11-6 = 5 → cap at 2, base = 1, max = 2)
                                    + 2 workers
                                    ─────────────
Combined + headroom                 = 8 workers

Future growth (80 connections, prod + staging)
                                    = 8 workers

Recommended = max(8, 8)            = 8 Data Workers ✅
```

The headroom of **2 workers** is driven by the large burst delta (11 vs. 6) — the 23 daily syncs all converging at the 2–6 AM window is the single biggest risk. The recommended contract of **8 workers** also happens to exactly cover the 6-month growth target, so Acme won't need a contract revision if they hit 80 connections on schedule.

---

### 2–6 AM UTC Peak Window Analysis

Acme's 4-hour window fits comfortably: the calculator confirmed the peak window can handle **~168 infrequent syncs** against the 23 daily syncs in scope. ✅ No window overflow risk even with current load.

However, the **worst-case burst of 11 workers** occurs when all 23 daily syncs launch simultaneously at 2 AM. At 8 contracted workers, 3 would momentarily queue. **Staggering** the 23 daily syncs across the 4-hour window (e.g., batches at 2:00, 3:00, 4:00, 5:00 AM) eliminates this entirely and keeps peak well under 8 workers.

---

### Growth Trajectory

| Scenario | Connections | Estimated Workers (combined prod + staging) |
|---|---|---|
| Today (current) | 45 | **6** (steady-state) |
| Recommended contract | 45 | **8** (with headroom) |
| 6-month target | 80 | **8** (exactly fits recommended contract) |
| Beyond 80 connections | >80 | Reassess — contract upgrade likely needed |

Acme has a clean growth runway to 80 connections without needing a contract change. Beyond that, a conversation about upgrading from 8 → 10–12 workers would be appropriate.

---

### Key Scheduling Recommendation

The 1-hour freshness SLA for key reports is achievable with only **2 workers** during the drain period — but only if the 23 daily syncs are **spread across the window rather than all starting at 2:00 AM**. Suggest staggering in 4 batches:

| Batch | Start Time | Connections | Mix |
|---|---|---|---|
| 1 | 02:00 UTC | 6 | 4 API, 2 DB |
| 2 | 03:00 UTC | 6 | 4 API, 2 DB |
| 3 | 04:00 UTC | 6 | 4 API, 2 DB |
| 4 | 05:00 UTC | 5 | 3 API, 2 DB |

Each batch runs ≤6 connections, peaks well below 4 workers, and all 23 complete by 6:00 AM — fully within the freshness window.

---

### Final Recommendation

**Contract Acme at 8 Data Workers.**

This covers:
- Current steady-state with comfortable headroom (6 sustained + 2 burst buffer)
- The 6-month growth target to 80 connections without renegotiation
- The 1-hour freshness SLA for daily reports assuming reasonable schedule staggering
- Both prod and staging environments

Once Acme is live, run a Mode 1 job overlap analysis against actual Metabase hourly data after 2–4 weeks to confirm sizing against the CPU-based billing formula.
