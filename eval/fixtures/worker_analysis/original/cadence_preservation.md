Excellent — the skill ran successfully. Here is the full analysis for **Acme**:

---

## 🧮 Worker Estimation: Acme (Prospect)

**Mode:** Mode 2A — Questionnaire-based Statistical Estimation
**Analysis date:** 2026-08-05
⚠️ *This is an estimate. The actual billing formula uses CPU values per sync; once Acme is on the platform, actual hourly Metabase data becomes the source of truth.*

---

### Connection Matrix (50 connections)

| Type | Sub-hourly (every 15 min) | Hourly | Daily | **Total** |
|---|---|---|---|---|
| **API** (Salesforce, HubSpot, Stripe, Shopify, Zendesk) | 8 | 14 | 13 | **35** |
| **DB/File** (Postgres, MySQL, Snowflake) | 4 | 6 | 5 | **15** |
| **Total** | 12 | 20 | 18 | **50** |

*Split from inputs: 70% API / 30% DB × {25% sub-hourly / 40% hourly / 35% daily}*

---

### Sizing Views

| Sizing view | Workers | What it means |
|---|---|---|
| **Steady-state requirement** | **5** | Expected load when schedules are spread across intervals |
| Peak-window drain requirement | 2 | Minimum to flush 18 daily syncs within a 60-min freshness SLA |
| **Worst-case burst** | **10** | All 18 daily syncs fire simultaneously + ongoing sub-hourly/hourly steady state |
| Production-only requirement | 5 | Prod environment alone at steady state |
| Combined prod + staging | 7 | Prod (5) + staging modeled as daily-only (2) |
| **Future growth (80 connections)** | **10** | 80-connection target at same mix, prod + staging |
| ✅ **Recommended contract capacity** | **10** | Governs — driven by growth target |

---

### Steady-State Concurrency Breakdown (12-min avg duration)

The key driver of concurrency is how long each sync runs relative to its schedule interval (duty cycle = duration ÷ interval):

| Frequency | Interval | Duty cycle | Concurrent API syncs | Concurrent DB syncs |
|---|---|---|---|---|
| Sub-hourly (every 15 min) | 15 min | **80%** | 8 × 0.80 = **6.40** | 4 × 0.80 = **3.20** |
| Hourly | 60 min | 20% | 14 × 0.20 = **2.80** | 6 × 0.20 = **1.20** |
| Daily | 1,440 min | 0.83% | 13 × 0.0083 = **0.11** | 5 × 0.0083 = **0.04** |
| **Total** | — | — | **9.31** | **4.44** |

**Steady-state worker formula:**
`ceil(9.31 / 5) + ceil(4.44 / 2)` = `ceil(1.86) + ceil(2.22)` = **2 + 3 = 5 workers**

---

### Worst-Case Burst (1–5 AM UTC Peak Window)

At the nightly batch window, all 18 daily syncs fire near simultaneously while sub-hourly and hourly steady-state continues:

| Component | API concurrent | DB concurrent |
|---|---|---|
| Sub-hourly steady-state | 6.40 | 3.20 |
| Hourly steady-state | 2.80 | 1.20 |
| Daily burst (all fire at once) | **+13.00** | **+5.00** |
| **Burst total** | **22.20** | **9.40** |

**Burst worker formula:**
`ceil(22.20 / 5) + ceil(9.40 / 2)` = `ceil(4.44) + ceil(4.70)` = **5 + 5 = 10 workers**

> **Key insight on the 1–5 AM window:** Sub-hourly connections (12 total, 80% duty cycle) are the heaviest contributor at any hour — they are essentially always running. Scheduling all 18 daily syncs in the same window means peak capacity must absorb the full burst on top of constant sub-hourly load. Staggering daily syncs across the 4-hour window (e.g., 6 syncs at 01:00, 6 at 02:30, 6 at 04:00) would reduce burst from **10 → ~6 workers**.

---

### Growth Scenario: Launch → 80 connections (6 months)

| Scenario | Connections | Prod workers | Staging workers | **Combined** |
|---|---|---|---|---|
| Launch (today) | 50 | 5 | 2 | **7** |
| Growth target | 80 | 8 | 2 | **10** |

The 80-connection target at the same connector mix (70/30 API/DB) and the same schedule distribution reaches **10 workers combined** across prod and staging — which is why 10 is the recommended contract anchor.

---

### Recommendation

| Dimension | Recommended |
|---|---|
| **Contract capacity** | **10 Data Workers** |
| Launch-day steady-state | 5 workers (50% utilization at contract) |
| Burst (unoptimized daily window) | 10 workers — hits contracted ceiling |
| Post-staggering burst (recommended) | ~6 workers — comfortable headroom |
| 6-month growth capacity | ✅ Covered at 80 connections |

**Three actions to stay well within 10 workers as the workload grows:**

1. **Stagger the 1–5 AM daily batch** — spread 18 nightly syncs evenly across the 4-hour window (every ~13 min) rather than a simultaneous fire. This alone cuts burst from 10 to ~6 workers, giving 4 workers of headroom for growth.
2. **Review sub-hourly necessity** — 12 connections running every 15 min carry an 80% duty cycle; confirming these truly need 15-min cadence (vs. hourly) would free ~1–2 steady-state workers.
3. **Keep staging on daily-only schedules** — the staging model (2 workers) assumes daily cadence. If staging inherits sub-hourly schedules, it adds incremental load beyond this estimate.

---

### Caveats

- This estimate uses the **heuristic model** (`ceil(API/5) + ceil(DB/2)`), not the actual CPU-based billing formula `(maxSourceCPU + maxDestCPU + maxOrchestratorCPU) / 8`. Actual billing on Cloud may differ.
- No initial full-load is modeled (as requested). Full-load syncs would temporarily spike workers well above these estimates during the onboarding window.
- Snowflake is classified as DB for this estimate. Since it will be used as a *destination* warehouse rather than a source, its CPU weight in billing may vary — verify via Datadog once live.
- Once Acme is on the platform, run a **Mode 1 job overlap analysis** after 7–14 days of production history for precise contract sizing.
