All seven sizing views are in. Here's the complete deterministic worker analysis for **Acme**.

---

## 🔍 Acme — Prospect Worker Sizing Analysis
**Date:** 2026-08-05 | **Model:** Estimation (questionnaire — no job history yet)

---

### Input Parameters Confirmed

| Parameter | Value |
|---|---|
| Total connections | 45 |
| Connector mix | 30 API (Salesforce, HubSpot, Stripe) / 15 DB (Postgres, MySQL) |
| API % | 66.67% |
| DB % | 33.33% |
| Sub-hourly (every 15 min) | 20% |
| Hourly | 30% |
| Daily | 50% |
| Avg sync duration | 10 min |
| Peak window | 2–6 AM UTC |
| Freshness SLA | 60 min |
| Environments | 2 (prod + staging) |
| Growth target | 80 connections in 6 months |

---

### Connection Matrix

| Type | Sub-hourly (15-min) | Hourly | Daily | Total |
|---|---|---|---|---|
| **API** | 6 | 9 | 15 | **30** |
| **DB/File** | 3 | 4 | 8 | **15** |
| **Total** | **9** | **13** | **23** | **45** |

---

### Seven Sizing Views

| # | Sizing View | Workers | Notes |
|---|---|---|---|
| 1 | **Steady-state requirement** | **4** | Schedules spread across their intervals; 5.6 API + 2.72 DB concurrent |
| 2 | **Peak-window drain requirement** | **2** | 23 daily syncs drained in 60 min freshness window (1 API worker + 1 DB worker) |
| 3 | **Worst-case simultaneous burst** | **11** | All 23 daily syncs fire at once while sub-hourly/hourly continue; 20.5 API + 10.67 DB concurrent |
| 4 | **Production-only requirement** | **4** | Same as steady-state (prod environment alone) |
| 5 | **Combined prod + staging requirement** | **6** | Prod 4 + staging modeled as daily-only at same mix (+2) |
| 6 | **Future-growth requirement (80 conns)** | **8** | Prod 6 + staging 2 at 80-connection scale |
| 7 | 🎯 **Recommended contract capacity** | **8** | `max(combined + headroom, future_growth)` = `max(6+2, 8)` = **8** |

---

### Recommendation Breakdown

```
Combined prod+staging (steady)   =  6 workers
+ Headroom                       = +2 workers   (burst delta = 11−6 = 5, capped at 2)
                                 = ──────────
Combined + headroom              =  8 workers
Future-growth target (80 conns)  =  8 workers
                                 = ──────────
Recommended contract capacity    =  8 Data Workers  ← max(8, 8)
```

---

### Key Findings & Interpretation

#### 1. Steady-State is Lean, Burst is the Governing Risk
Under normal distributed scheduling, Acme needs only **4 workers** to sustain the ongoing load. The critical concern is the **2–6 AM UTC peak window** where 23 daily syncs tend to cluster at the top of the hour — the worst-case burst scenario reaches **11 workers**, nearly 3× the steady-state floor. The recommended **8-worker contract** absorbs the structural steady load plus 2 workers of burst headroom, without sizing to the extreme tail.

#### 2. Daily Batch Freshness is Well-Covered at 8 Workers
The peak-window drain analysis shows only **2 workers** are the *minimum* needed to flush all 23 daily syncs within the 1-hour freshness SLA — meaning at 8 workers, the entire daily batch for key reports can complete **significantly faster** than the 60-minute window. No freshness risk at the recommended contract size.

#### 3. Sub-hourly Connections Have Minimal Impact
The 9 sub-hourly connections (20% of 45) contribute only ~1.7 workers to steady-state concurrency. Their 10-minute sync duration at a 15-minute interval means they consume just 67% of their available cycle time — plenty of slack to absorb occasional retries.

#### 4. Growth Is Already Baked In
The recommended 8-worker contract is sized **at the growth target** (80 connections in 6 months). Acme can on-board all planned connections without a contract change — the 8-worker capacity covers both the current 45-connection launch and the 80-connection endpoint.

#### 5. Staging Sizing Assumption
Staging (+2 workers) is modeled conservatively as **daily-only** with the same API/DB mix. If Acme runs sub-hourly or hourly syncs in staging too, actual staging load could be 3–4 workers; in that case, consider a 9–10 worker contract or designating staging as a separate cost center.

---

### Risk Assessment

| Risk | Level | Mitigation |
|---|---|---|
| Burst at 2–6 AM UTC if daily syncs cluster | 🟡 Moderate | Stagger daily syncs across the 4-hour peak window in 10-min offsets (6 syncs/10 min × 4h = 24 slots — fits all 23) |
| Staging consuming prod headroom | 🟡 Moderate | Deploy prod and staging as separate workspaces with separate worker budgets |
| Growth beyond 80 connections | 🟢 Low | At 80 connections the model projects 8 workers — contract is at ceiling; monitor when >80 connections are added |
| MySQL orchestrator CPU exception | 🟢 Low | No overridden `cpu_request` connections assumed; validate after on-boarding if any MySQL connections get CPU bumps |

---

### Scheduling Recommendation for the 2–6 AM Window

To avoid burst clustering within the 4-hour window, distribute the 23 daily syncs at **~10-minute staggered offsets**:

```
Slot 0 (02:00 UTC) → 3–4 connections
Slot 1 (02:10 UTC) → 3–4 connections
Slot 2 (02:20 UTC) → 3–4 connections
...continuing to 05:50 UTC
```

This keeps peak concurrency at **≤3–4 concurrent syncs** at any given time during the window — well under the 8-worker contract — rather than the 11-worker spike from a simultaneous fire.

---

### ⚠️ Estimation Caveat

> This analysis uses the **statistical estimation model** (`ceil(DB/2) + ceil(API/5)`), not the actual Airbyte Cloud CPU-based billing formula (`(maxSourceCPU + maxDestCPU + maxOrchestratorCPU) / 8`). Results are appropriate for pre-sales sizing and contract discussions. Once Acme is live on the platform, run a **Metabase billing analysis** against `organization_data_worker_usage_daily` to validate against actual CPU-metered usage and right-size the contract if needed.

---

**Bottom line: Start Acme on an 8 Data Worker contract.** This covers day-one operations with burst headroom, the 60-minute freshness SLA for key reports, both prod and staging environments, and the full 80-connection growth target — without over-provisioning.
