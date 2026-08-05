I have everything. Let me now synthesize the full analysis.

---

## Acme — Worker Analysis: OSS Workspace `ws-synthetic-001`

**Data source:** OSS export at `eval/fixtures/worker_analysis_workspace.json`
**Analysis period:** 2026-08-03 (single observed day)
**Analysis mode:** `2B_oss_workspace_job_overlap` (job overlap / estimation model — not billing formula)
**19 jobs** across **6 connections** (4 API, 2 DATABASE)

---

### 1. Connection Inventory

| Connection ID | Name | Type | Jobs Observed | Avg Duration |
|---|---|---|---|---|
| `conn-api-1` | Salesforce Contacts | API | 4 | 5.0 min |
| `conn-api-2` | HubSpot Companies | API | 4 (incl. 1 failed) | 3.8 min |
| `conn-api-3` | Stripe Charges | API | 3 | 5.0 min |
| `conn-api-4` | Google Sheets Reports | API | 2 | 4.5 min |
| `conn-db-1` | Postgres Orders | DATABASE | 3 (incl. 1 initial) | 25.3 min |
| `conn-db-2` | MySQL Users | DATABASE | 3 (incl. 1 double-trigger) | 6.7 min |

---

### 2. Peak Concurrency

**Peak window: 14:00 UTC**

| Metric | Value |
|---|---|
| Peak concurrent API jobs | **4** (Salesforce, HubSpot, Stripe at :00, Google Sheets trailing from :56) |
| Peak concurrent DB jobs | **3** (Postgres initial load still in flight + MySQL × 2 concurrent) |
| Peak total concurrent jobs | **7** |
| Estimated workers at peak | **(4/5) + (3/2) = 0.80 + 1.50 = 2.30 workers** |
| P99 workers (hourly) | **2.3** ← toolkit output |

The peak exists for **5 consecutive minutes (14:00–14:04 UTC)** before the trailing Google Sheets and MySQL jobs drain. Workers drop to 1.6 by 14:05 and to 1.0 by 14:10.

> **⚠️ Important caveat:** This uses the heuristic estimation model (`ceil(DB/2) + ceil(API/5)`), not the actual CPU-based billing formula. Applying the SKILL ceiling form gives `ceil(4/5) + ceil(3/2)` = **1 + 2 = 3 workers**, which is the conservative contract-sizing number. Once on Airbyte Cloud, actual Metabase billing data is the authoritative source.

---

### 3. Anomalies Detected

#### 3a. ⚠️ Failed / Zero-Duration Job — HubSpot Companies

- **Job:** `conn-api-2`, started `2026-08-03T13:50:00Z`, ended `2026-08-03T13:50:00Z` — **0-minute duration**
- **Pattern:** Immediately followed by a successful 5-minute retry at `13:52:00Z` (2-minute gap)
- **Assessment:** Classic fail-and-retry signature. The job either hit an immediate connection error (auth failure, rate-limit response, transient timeout) or was aborted before any records were processed. The retry succeeded.
- **Impact on workers:** Zero-duration jobs are filtered from concurrency calculation (they contribute no real CPU load). No worker distortion.
- **Action:** Investigate HubSpot connector logs around `13:50` for error code. If recurrent, consider increasing the retry delay or reviewing the OAuth token refresh cycle.

#### 3b. ⚠️ Double-Trigger — MySQL Users (conn-db-2)

- **Jobs:** `conn-db-2` at `13:58→14:03` AND `14:00→14:10` — **3-minute overlap** between the two
- **Root cause:** The first sync started at `:58` (likely a slightly-delayed hourly trigger) and was still running when the next hourly trigger fired at exactly `14:00`. Airbyte did not detect the in-progress job and launched a second one.
- **Impact on workers:** At 14:00 UTC, MySQL contributes **2 DB slots** instead of the expected 1, which is the single largest contributor to the peak DB=3. Without this overlap, peak DB would be 2 → workers drop to `(4/5) + (2/2) = 1.8`.
- **Action:** Check if the connection has `allowSimultaneous` enabled or if the job scheduler has a race window. On OSS, consider enabling "cancel and restart" behavior or shifting the schedule to `:00` exactly so the delay doesn't accumulate. On Cloud, this would be handled automatically by the admission scheduler.

#### 3c. ⚠️ Long-Running Initial Load — Postgres Orders (conn-db-1)

- **Job:** `conn-db-1`, `initial` type, `13:30→14:30` — **60 minutes**
- **Context:** Normal behavior for an initial full-table load. Subsequent incremental syncs are 8 minutes — the initial was **7.5× longer**.
- **Impact on workers:** The initial load was still running at 14:00 when all scheduled hourly syncs fired. This made the peak DB count 3 instead of 2. Had the initial load been scheduled for off-peak (e.g., 02:00 UTC), the 14:00 peak workers would have been `(4 API + 2 DB) → 1.8` instead of 2.3.
- **Action:** For future resets or initial loads on large tables, schedule them in the overnight window (00:00–06:00 UTC based on observed no-activity periods) to avoid colliding with regular hourly sync windows.

---

### 4. Concurrency Timeline (Key Minutes)

```
UTC    API  DB   Workers   Notes
─────────────────────────────────────────────────────────────
13:50   1    1    0.70     Salesforce starts; Postgres initial in flight
13:52   2    1    0.90     HubSpot retry starts
13:54   3    1    1.10     Stripe starts
13:56   3    1    1.10     Google Sheets starts
13:58   2    2    1.40     MySQL first job starts
14:00   4    3    2.30 ◀ PEAK  All 3 :00 API syncs + MySQL double-trigger
14:01   3    3    2.10     Google Sheets ends at :01
14:05   0    2    1.00     API syncs drain; Postgres + MySQL still running
14:10   0    1    0.50     MySQL second job ends; only Postgres remains
14:30   0    0    0.00     Postgres initial completes
```

---

### 5. Hourly Worker Summary (Full Day)

The export covers a single day. Based on the jobs observed:

| Hour (UTC) | API Peak | DB Peak | Workers |
|---|---|---|---|
| 13 | 3 | 2 | 1.60 |
| **14** | **4** | **3** | **2.30 ← peak** |
| 15 | 4 | 1 | 1.30 |
| 16 | 1 | 1 | 0.70 |
| All other hours | 0 | 0 | 0.00 |

P99 workers (hourly): **2.3**
Average workers (across 24 hours): **0.24** (heavily concentrated in the 13–16 UTC window)

---

### 6. Worker Recommendations

#### Recommended contract: **3 Data Workers**

| Sizing View | Value | Basis |
|---|---|---|
| Heuristic peak (direct division) | 2.3 workers | (4/5) + (3/2) |
| Heuristic peak (ceiling form) | **3 workers** | ceil(4/5) + ceil(3/2) = 1 + 2 |
| Headroom at 3 workers | **0.7 workers** (~23%) | Against direct-division peak |
| If double-trigger is fixed | 1.8 workers | (4 API + 2 DB) at peak |
| If initial load moved off-peak | 1.8 workers | (4 API + 2 DB) at peak |
| If both resolved | 1.3 workers | (4 API + 1 DB) at peak |

**Start with 3 workers.** This provides a safe buffer above the observed 2.3-worker peak and absorbs moderate growth (1–2 new connections). The peak only occurs in a narrow 5-minute window at 14:00 UTC and is driven by three fixable issues below.

#### If growth exceeds ~8–10 total connections or syncs become more frequent, re-evaluate at that time.

---

### 7. Optimization Actions (Priority Order)

| Priority | Action | Expected Reduction |
|---|---|---|
| 🔴 High | **Fix MySQL double-trigger (conn-db-2)** — investigate why sync at 13:58 overlaps with 14:00 fire; enable "skip if running" or fix schedule alignment | Peak DB: 3 → 2; workers: 2.3 → **1.8** |
| 🟡 Medium | **Stagger the 4 API connections** away from the exact :00 boundary — e.g., Salesforce at :00, HubSpot at :03, Stripe at :06, Google Sheets at :09 | Reduces peak API from 4 to 2-3; workers at peak: ~1.4 |
| 🟡 Medium | **Reschedule future initial loads / resets for Postgres off-peak** — use 02:00–06:00 UTC window (no activity observed) | Avoids adding +1 DB to peak window |
| 🟢 Low | **Investigate HubSpot zero-duration failure** — review logs for root cause; reduce recurrence of fail/retry cycles | Reliability improvement; minor worker impact |

With all optimizations applied, the sustainable peak for this workspace drops to approximately **1.3 workers**, meaning a 2-worker contract would comfortably cover normal operations with room for additional connections.

---

> **Methodology note:** This analysis uses the heuristic estimation model (`ceil(DB/2) + ceil(API/5)`) on job overlap data — it is an approximation. The actual Airbyte Cloud billing formula uses `(maxSourceCPU + maxDestCPU + maxOrchestratorCPU) / 8`. Once Acme is on Cloud, Metabase billing data will be the authoritative source and may produce a different worker number.
