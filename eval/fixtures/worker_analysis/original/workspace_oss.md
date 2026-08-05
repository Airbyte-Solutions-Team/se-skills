All three sanitizer assertions pass. Here is the complete analysis for Acme:

---

## Acme — Worker Analysis: workspace `ws-synthetic-001`

### Analysis Summary

The OSS export contains **19 job records across 6 connections** spanning a single day (August 3, 2026). Analysis used the **job overlap sweep method** — minute-level concurrency counts, aggregated to hourly peaks, then the estimation formula applied.

---

### 1. Peak Concurrency

| Hour (UTC) | Concurrent API | Concurrent DB | Estimated Workers | Status |
|---|---|---|---|---|
| **14:00** | **4** | **3** | **2.30** | ⚠ OVER (if contracted at 2) |
| 13:00 | 3 | 2 | 1.60 | OK |
| 15:00 | 3 | 1 | 1.10 | OK |
| 16:00 | 1 | 1 | 0.70 | OK |
| All other hours | 0 | 0 | 0.00 | Idle |

**Peak formula at 14:00 UTC:** `ceil(4 API / 5) + ceil(3 DB / 2) = 1 + 2 = 3 workers` (ceiled) | `4/5 + 3/2 = 2.30` (raw float, used as P99)

The spike at 14:00 UTC is caused by **five connections firing at exactly the same minute**: Salesforce Contacts, HubSpot Companies, Stripe Charges (all newly-scheduled API syncs) plus the still-running Postgres Orders initial sync and an overlapping MySQL Users run. Activity is concentrated in a narrow 4-hour window (13:00–16:00 UTC); the remaining 19 hours are completely idle.

---

### 2. Long-Running Jobs

One job significantly exceeds the norm:

| # | Connection | Type | Start | End | Duration | Kind |
|---|---|---|---|---|---|---|
| 1 | **Postgres Orders** (conn-db-1) | DATABASE | 13:30 UTC | 14:30 UTC | **60 min** | `initial` |

This is a full-table initial sync — expected behavior. All subsequent incremental syncs for this connection complete in 8 minutes or less. However, because this 60-minute job occupies a DB slot during the busiest window (13:30–14:30 UTC), it **directly contributes to the 14:00 peak**. For future migrations, scheduling initial syncs during off-peak hours (e.g., 02:00 UTC) would avoid this effect.

---

### 3. Failed & Retried Jobs

**Zero-duration job — likely failure + auto-retry:**

| # | Connection | Start | End | Duration | Pattern |
|---|---|---|---|---|---|
| 19 | **HubSpot Companies** (conn-api-2) | 13:50:00 UTC | 13:50:00 UTC | **0 min** | Instant abort |
| 3 | HubSpot Companies (conn-api-2) | 13:52:00 UTC | 13:57:00 UTC | 5 min | ← Likely retry |

Job #19 has an identical start and end timestamp — it ran for zero seconds and produced no output. Job #3 (the same connection) started 2 minutes later and completed successfully in 5 minutes. This is the classic failed-job → immediate-retry pattern. **Recommendation:** Check HubSpot connector logs at 13:50 UTC on Aug 3 for the error message (typically a rate-limit hit or auth token expiry on startup).

**Overlapping concurrent runs on the same connection:**

| Jobs | Connection | Overlap Window | Duration |
|---|---|---|---|
| #6 (13:58–14:03) and #10 (14:00–14:10) | **MySQL Users** (conn-db-2) | 14:00–14:03 UTC | **3 min** |

Two separate job instances for the same connection were running simultaneously for 3 minutes. This is anomalous — Airbyte normally prevents concurrent runs per connection. The most likely cause is a **manual "Run now" trigger at 14:00 UTC while a scheduled sync was already in progress**. This concurrent overlap contributes a spurious extra DB slot to the peak count, artificially inflating the measured peak by ~0.5 workers. On Airbyte Cloud, this situation would queue the second run rather than run it concurrently.

---

### 4. Worker Recommendations

| Scenario | Est. Peak Workers | Action |
|---|---|---|
| **Current load** (6 connections) | **2.30** | Contract **2 workers** and stagger (or **3 workers** with headroom) |
| +25% growth (8 connections) | ~2.9 | 3 workers recommended |
| +50% growth (9 connections) | ~3.5 | 3 workers recommended |
| +100% growth (12 connections) | ~4.6 | 5 workers recommended |

**Immediate recommendation:** Contract **3 data workers**. At current load, 3 workers gives 23% headroom even at the busiest observed peak, and supports up to ~9 connections before needing to revisit.

**Scheduling optimization (to stay within 2 workers):** The 14:00 UTC spike is entirely driven by simultaneous scheduling. Staggering just three API connections by 5–10 minutes reduces peak to ~1.4 workers with no infrastructure changes:

| Connection | Current | Proposed | Quartz Cron |
|---|---|---|---|
| HubSpot Companies | 14:00 UTC | 14:05 UTC | `0 5 14 * * ?` |
| Stripe Charges | 14:00 UTC | 14:10 UTC | `0 10 14 * * ?` |
| MySQL Users | 13:58 UTC | 14:15 UTC | `0 15 14 * * ?` |

**⚠ Estimation caveat:** This analysis uses the heuristic model (`ceil(DB/2) + ceil(API/5)`) because no CPU data is available from OSS exports. Actual Airbyte Cloud billing uses `(maxSourceCPU + maxDestCPU + maxOrchestratorCPU) / 8`. Results may differ by ±15% from billing actuals once the customer migrates to Cloud.

---

### PDF Reports

Two PDF reports have been generated:

- **`Acme_Worker_Report_20260805.pdf`** (52 KB) — ✅ **Safe to share with Acme.** Customer-facing executive report (4 pages): cover, key metrics, 24-hour usage chart, and recommendations in natural language. All internal formulas, workspace IDs, and estimation methodology have been stripped by `DataSanitizer`. DataSanitizer validation passed all three tests (field allowlisting, blocked-content detection, clean passthrough).
- **`Acme_Worker_Report_Internal_20260805.pdf`** (192 KB) — 🔒 **Internal use only.** Full 8-page report including: cover with INTERNAL USE ONLY watermark, full job log table, worker calculation model with formulas, minute-level concurrency analysis, anomaly table (zero-duration job, concurrent overlap, long-running initial sync), growth scenarios chart, and specific Quartz cron rescheduling recommendations.
