All the data is in. Here is the complete worker analysis for Acme, workspace `ws-synthetic-001`.

---

## Worker Analysis — Acme · `ws-synthetic-001`

**Export date:** 2026-08-04 UTC | **Analysis period:** 2026-08-03 (single day of job history) | **Method:** Job-overlap analysis on actual start/end times (high-confidence estimation)

---

### 1. Connection Inventory

| Connection | Connector | Type | Jobs observed |
|---|---|---|---|
| Salesforce Contacts | `salesforce` | **API** | 4 |
| HubSpot Companies | `hubspot` | **API** | 4 (incl. 1 anomalous) |
| Stripe Charges | `stripe` | **API** | 3 |
| Google Sheets Reports | `google-sheets` | **API** | 2 |
| Postgres Orders | `postgres` | **DATABASE** | 3 (incl. 1 initial load) |
| MySQL Users | `mysql` | **DATABASE** | 3 (incl. 1 overlap anomaly) |

**Totals:** 6 connections — 4 API, 2 DATABASE · 19 job runs in the export

---

### 2. Peak Concurrency

The job-overlap analyzer checks every minute across a 24-hour window and records how many API and DB jobs are simultaneously active.

| Hour (UTC) | API Peak | DB Peak | Total | Workers (raw) | Status |
|---|---|---|---|---|---|
| 13:00 | 3 | 2 | 5 | 1.6 | Active |
| **14:00** | **4** | **3** | **7** | **2.3** | **← PEAK** |
| 15:00 | 3 | 1 | 4 | 1.1 | Active |
| 16:00 | 1 | 1 | 2 | 0.7 | Active |
| All other hours | 0 | 0 | 0 | 0.0 | Idle |

**Peak moment: 14:00 UTC** — all four API connectors were running simultaneously, and three DATABASE job slots were occupied (explained in §4 below).

**Calculation:**
```
(4 API ÷ 5) + (3 DB ÷ 2) = 0.8 + 1.5 = 2.3 estimated workers
```
Applying per-type ceiling (estimation model): `ceil(4/5) + ceil(3/2)` = **1 + 2 = 3 Data Workers**

---

### 3. Long-Running Jobs

| Connection | Type | Duration | Time window | Notes |
|---|---|---|---|---|
| **Postgres Orders** | DATABASE | **60 min** | 13:30–14:30 | Initial full sync — expected but held a DB slot for the entire peak hour |
| **MySQL Users** | DATABASE | **10 min** | 14:00–14:10 | 2× typical duration; contributed to the 3-concurrent-DB peak |
| **Postgres Orders** | DATABASE | **8 min** | 15:00–15:08 | Consistent across two incremental runs (also 8 min at 16:00) |

**Key impact:** The Postgres Orders 60-minute initial load ran from 13:30–14:30, completely spanning the peak window. Had it not been present, peak DB concurrency at 14:00 would have been 2 (not 3), and the worker estimate would drop to `(4/5) + (2/2) = 0.8 + 1.0 = 1.8` → **2 Data Workers** for steady-state incremental syncs. The initial-load overhead is a one-time event; ongoing capacity is lower.

---

### 4. Failed / Retried Jobs

#### 4a. HubSpot Companies — zero-duration job (immediate failure → retry)

```
13:50:00  HubSpot Companies  [API]  start=13:50  end=13:50  duration=0 min  ← FAILED / immediately cancelled
13:52:00  HubSpot Companies  [API]  start=13:52  end=13:57  duration=5 min  ← RETRY (2-min gap) ✓
```

The zero-duration record indicates the job was launched but never ran (connection error, scheduler race condition, or an immediate cancellation). Airbyte automatically retried 2 minutes later and the retry succeeded. **Impact on capacity:** the failed job consumed no measurable worker capacity (zero duration = never active in the overlap window), but it produced a phantom job record in the export count.

#### 4b. MySQL Users — overlapping concurrent jobs (double-trigger / retry-while-running)

```
13:58:00  MySQL Users  [DATABASE]  start=13:58  end=14:03  duration=5 min
14:00:00  MySQL Users  [DATABASE]  start=14:00  end=14:10  duration=10 min
                                   ↑ overlap: 14:00–14:03 (3 min)
```

At exactly 14:00 UTC, a second MySQL Users job started while the first was still running. This is the **direct cause of the 3-concurrent-DATABASE spike** and the peak worker reading of 2.3. Possible causes:
- A scheduled sync fired on the top-of-hour boundary before the previous run completed
- A manual sync was triggered in the UI while the scheduled job was in flight
- A retry was issued before the original job's status resolved

The second job also ran 2× longer (10 min vs. 5 min), suggesting it picked up more data or encountered a slower execution path. This pattern is worth monitoring: if MySQL Users syncs consistently run close to the schedule interval, the scheduler will overlap them regularly.

---

### 5. Worker Recommendations

#### Immediate recommendation: **3 Data Workers** (safe ceiling)

| Scenario | API peak | DB peak | Workers (raw) | Workers (ceil) |
|---|---|---|---|---|
| Observed peak (incl. initial load + overlap) | 4 | 3 | 2.3 | **3** |
| Steady-state incremental (no initial load, no overlap) | 4 | 2 | 1.8 | **2** |

**Start with 3 workers.** The current 3-DB spike is driven by two atypical conditions (one-time initial load + a double-triggered MySQL job). Once the Postgres initial load completes and the MySQL overlap is resolved, steady-state usage is closer to 1.8, which rounds to **2 workers**. If Acme adds more connections, 3 workers provides adequate headroom.

#### Specific action items

1. **Stagger MySQL Users away from the top of the hour.** Currently it fires at `:00`, the same moment as Salesforce, HubSpot, and Stripe. Move MySQL Users to `:05` or `:10` past the hour. This eliminates the most likely cause of the overlap and drops peak DB concurrency back to 2 during steady-state.

2. **Monitor the Postgres Orders sync duration.** The initial load is done (60-min run is a one-time event), but incremental syncs are already running 8 minutes. If row counts grow significantly, the sync time will increase. Watch that it stays well under the schedule interval (currently hourly) to avoid the same overlap risk seen with MySQL.

3. **Investigate the HubSpot Companies zero-duration failure.** A single failure is not alarming, but if it recurs consistently on this connection, it may indicate a credential refresh issue or a rate-limit being hit before the sync starts. Check the Airbyte logs for the 13:50 UTC job ID to confirm the failure reason.

4. **Note on data depth.** This analysis covers a single day of job history (2026-08-03). The P99 metric over 24 hourly buckets is effectively just the maximum. For a statistically meaningful P99, re-run this analysis with at least 10–14 days of export data so the busiest real-world scheduling patterns (month-end batch loads, Monday morning pipelines, etc.) are captured.

5. **Path to accurate billing data.** This analysis uses the API/DB estimation model (`ceil(API/5) + ceil(DB/2)`), which is a heuristic. Once Acme migrates to Airbyte Cloud, the authoritative metric is the CPU-based billing formula `(maxSourceCPU + maxDestCPU + maxOrchestratorCPU) / 8`. The estimate here may differ from actual billed workers by 10–30% depending on connector CPU profiles.

---

### Summary

| Metric | Value |
|---|---|
| Workspace | `ws-synthetic-001` |
| Connections | 6 (4 API, 2 DATABASE) |
| Jobs analyzed | 19 |
| Peak hour (UTC) | **14:00** |
| Peak concurrent API | **4** |
| Peak concurrent DB | **3** (anomaly-driven) |
| P99 workers (raw) | **2.3** |
| **Recommended Data Workers** | **3** (current) / **2** (steady-state) |
| Failed jobs | 1 (HubSpot 0-duration, retried ✓) |
| Overlapping jobs | 1 pair (MySQL Users, 3-min overlap at 14:00) |
| Long-running jobs | 3 (60-min initial load + two 8-min Postgres incrementals) |
| Analysis confidence | Medium — estimation model, single day of data |
