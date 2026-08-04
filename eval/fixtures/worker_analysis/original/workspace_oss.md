Here is the complete worker analysis for **Acme** (workspace `ws-synthetic-001`).

---

## Summary

**Data source:** OSS export with job-level timestamps (enhanced format) — sweep-line job overlap used.  
**Formula:** `ceil(Concurrent API / 5) + ceil(Concurrent DB / 2)` (estimation model; actual Cloud billing uses the CPU formula).

---

### Connection Inventory

| Connection | Type | Jobs |
|---|---|---|
| Salesforce Contacts | API | 4 |
| HubSpot Companies | API | 4 |
| Stripe Charges | API | 3 |
| Google Sheets Reports | API | 2 |
| Postgres Orders | DATABASE | 3 |
| MySQL Users | DATABASE | 3 |

**6 connections, 19 jobs total.**

---

### Peak Concurrency

| Hour (UTC) | API Peak | DB Peak | Workers |
|---|---|---|---|
| 13:00 | 3 | 2 | 1.40 |
| **14:00** | **4** | **3** | **2.30 ◄** |
| 15:00 | 3 | 1 | 1.10 |
| 16:00 | 1 | 1 | 0.70 |

**Peak at 14:00 UTC:** 4 API + 3 DB = 7 concurrent jobs.  
`4/5 + 3/2 = 0.80 + 1.50 = 2.30 raw workers`  
`ceil(0.80) + ceil(1.50) = 1 + 2 = **3 workers**`

The 14:00 UTC spike was driven by three things colliding simultaneously:
- All four API connectors fired within a 6-minute burst (13:50–13:56 UTC), creating 4 concurrent API syncs at 14:00
- Postgres Orders' initial sync was still running (started 13:30, ran 60 min), holding a DB slot
- MySQL Users had two jobs running at the same time (13:58 run overlapped the 14:00 run)

---

### Job Anomalies

**1. Long-running job — Postgres Orders**
- 13:30–14:30 UTC, 60 minutes, `job_type=initial`
- This is a first-ever historical load — expected for Postgres. Regular incremental syncs averaged ~25 min. However, it coincided exactly with the API burst window and pushed DB concurrency from 2 to 3. For future initial syncs of large sources, schedule them during low-traffic hours (15:00–16:00 UTC from this export).

**2. Zero-duration (failed) job — HubSpot Companies**
- 13:50 UTC, 0-second duration, `job_type=sync`
- Job started and ended instantly — a failed or cancelled sync (connection error, credential issue, or manual cancel). A successful retry followed 2 minutes later (13:52 UTC, 5 min). The failure itself didn't contribute to worker billing, but the retry pattern should be monitored. Recommended action: check HubSpot OAuth token expiry and API rate-limit headers.

**3. Self-overlapping run — MySQL Users**
- Run A: 13:58–14:03 UTC (5 min); Run B: 14:00–14:10 UTC (10 min) — **3 minutes of parallel execution**
- A new sync launched before the previous one finished. This indicates the sync interval is shorter than the maximum job duration, or a manual retry was triggered mid-run. On Cloud this creates dual billing for the connection during the overlap. Fix: set the schedule interval to ≥ the expected max duration (~15–30 min), or enable a "wait for previous run" guard.

---

### Worker Recommendations

| Scenario | Workers |
|---|---|
| **Conservative (current peak, as-is)** | **3** |
| Steady-state after Postgres initial completes | 2 |
| Steady-state after both fixes (MySQL overlap resolved + initial done) | 2 |

- **Start with 3 contracted workers** to absorb current peak behavior including the initial sync and the MySQL overlap.
- **Re-evaluate at 2 workers** once the Postgres initial sync finishes and the MySQL scheduling issue is fixed — at that point the DB peak drops from 3 → 2, and the raw peak falls to `4/5 + 2/2 = 1.80` (ceil = 2 workers).

---

### Optimization Suggestions

**(a) Stagger API connector schedules** — All 4 API connectors fire within a 6-minute burst (13:50–13:56 UTC). Spreading them at 15-minute offsets (`:00`, `:15`, `:30`, `:45`) caps API concurrency at 1 at any given minute, dropping API worker demand from `ceil(0.80)=1` to effectively 0.2.

**(b) Fix MySQL Users scheduling overlap** — Set sync interval to ≥ 15 minutes (max observed duration was 10 min). This eliminates the parallel-run risk entirely.

**(c) Schedule future initial syncs off-peak** — The 15:00–16:00 UTC window showed only 1.10–0.70 workers. Any large initial load (Postgres, MySQL full refresh) should be scheduled there to avoid stacking with the API burst.

**(d) Investigate HubSpot connector failure** — Review logs around 13:50 UTC. If zero-duration failures recur across multiple days, this is a connector health issue requiring credential rotation or rate-limit tuning.
