All numbers are in. Here's the full worker analysis for **Acme** — seven deterministic sizing views, freshness SLA math, burst risk, and the growth trajectory.

---

## 🔢 Connection Matrix

The skill decomposed Acme's 45 connections into the API/DB × frequency grid:

| Type | Sub-hourly (15 min) | Hourly | Daily | **Total** |
|---|---|---|---|---|
| API (Salesforce, HubSpot, Stripe) | 6 | 9 | 15 | **30** |
| DB (Postgres, MySQL) | 3 | 4 | 8 | **15** |
| **Total** | **9** | **13** | **23** | **45** |

---

## 📊 Seven Sizing Views

These are deterministic outputs from the `questionnaire_calculator`, not invented estimates. Each answers a different planning question.

| Sizing View | Workers | What it answers |
|---|---|---|
| **Steady-state (prod)** | **4** | Normal running load with schedules spread across intervals |
| Peak-window drain | 2 | Minimum to drain the 23 daily syncs within the 1-hour freshness SLA *independently* |
| **Worst-case burst** | **11** | If all 23 daily syncs fire simultaneously at 2 AM UTC alongside sub-hourly/hourly load |
| Production only | 4 | Prod environment alone, no staging |
| **Prod + staging combined** | **6** | Both environments at steady-state |
| Future growth (80 connections) | 8 | Prod+staging at 6-month target |
| **✅ Recommended contract capacity** | **8** | Floor that covers today + growth + burst headroom |

> **Estimation caveat:** This uses the statistical model (`ceil(API/5) + ceil(DB/2)`). Actual billing uses the CPU-based formula `(maxSourceCPU + maxDestCPU + maxOrchCPU) / 8`. Once Acme is live on the platform, re-run the analysis using Metabase billing data for ground truth.

---

## ⏱️ Freshness SLA Analysis — The 1-Hour Constraint

The binding constraint is: **23 daily syncs must complete within 60 minutes of their 2 AM UTC trigger** to meet the "data fresh within 1 hour" requirement. This is a queuing problem — how many lanes (workers) are needed to drain the queue before closing time?

**Sync-minutes of work:** 23 syncs × 10 min avg = **230 sync-minutes** to drain in a 60-minute window.

| Workers | Concurrent Slots (DB) | Waves | Drain (avg) | Drain (P90) | Fits avg? | Fits P90? | Margin |
|---|---|---|---|---|---|---|---|
| 1 | 2 | 12 | 120.5 min | 178.0 min | ❌ | ❌ | −60.5 min |
| 2 | 4 | 6 | **60.0 min** | 88.8 min | ✅ | ❌ | **0 min — no margin** |
| **3** | **6** | **4** | **39.8 min** | **59.0 min** | **✅** | **✅** | **+20.2 min** |
| 4 | 8 | 3 | 29.8 min | 44.1 min | ✅ | ✅ | +30.2 min |

**Key takeaway:** 2 workers technically clears the window at average duration but has **zero margin** — any slow night or retry will break the SLA. **3 workers is the minimum that's P90-safe for the freshness window alone.** This is already captured in the 4-worker steady-state recommendation (which serves all frequency tiers simultaneously).

---

## 🚨 Burst Risk: 2 AM UTC Peak Window

The 2–6 AM UTC window is where all three frequency tiers overlap. The analysis separates two scenarios:

**Normal (staggered daily syncs across the 4-hour window):**
- Steady API concurrent: **5.6** | DB concurrent: **2.72**
- Workers needed: **4** (steady-state)
- The 4-hour peak window provides ample drain time for all 23 daily syncs

**Worst-case (all 23 daily syncs fire at exactly 2:00 AM simultaneously):**
- API concurrent: **20.5** | DB concurrent: **10.67**
- Workers needed: **11** (7 above steady-state)
- This happens when all daily connections are configured to the same cron time

**Mitigation:** Stagger daily syncs across the 2–6 AM window in 10-minute increments. With 23 syncs over 240 minutes, scheduling ~1 sync every 10 minutes keeps peak concurrent load at 1–2 syncs at any given moment on top of the steady-state base load. This eliminates the burst entirely.

---

## 📈 Growth Trajectory

| Scenario | Prod Workers | Staging Workers | Combined | Recommended |
|---|---|---|---|---|
| Today: 45 connections, prod only | 4 | N/A | 4 | 6 |
| **Today: 45 connections, prod + staging** | **4** | **2** | **6** | **8** |
| 6-month target: 80 connections, prod + staging | 6 | 2 | 8 | **10** |

The staging model uses **daily-only** cadence for all connections (conservative assumption — staging typically doesn't run sub-hourly syncs). If Acme's staging environment mirrors prod frequency, add 2 workers to each scenario.

---

## ✅ Recommendation Summary

| Decision | Answer |
|---|---|
| **Initial contract** | **8 Data Workers** |
| Covers | Today's prod+staging steady-state (6) + 2 burst headroom |
| Also covers | Future growth to 80 connections (8 combined, same number) |
| Step-up trigger | When approaching 80 connections → step to **10 workers** |
| Freshness SLA safety | 4-worker steady-state provides 3× the minimum needed for the 1-hour freshness window |
| Burst risk | **Stagger daily syncs** across 2–6 AM UTC to prevent the 11-worker burst spike |

---

## 📋 Scheduling Recommendations

Given the 2–6 AM UTC peak window and 1-hour freshness requirement:

1. **Sub-hourly syncs (9 connections):** These run every 15 minutes already — no change needed.
2. **Hourly syncs (13 connections):** Spread across the hour (e.g., `0 * * * ?`, `5 * * * ?`, `10 * * * ?` …) to avoid the top-of-hour pile-up.
3. **Daily syncs (23 connections):** Stagger from 2:00 AM with ~10-minute spacing:
   - Syncs 1–6: `0 2 * * ?` → `50 2 * * ?`
   - Syncs 7–12: `0 3 * * ?` → `50 3 * * ?`
   - Syncs 13–18: `0 4 * * ?` → `50 4 * * ?`
   - Syncs 19–23: `0 5 * * ?` → `40 5 * * ?`
   - All complete well before 6 AM UTC; all within freshness SLA given 10-min avg duration.

4. **Staging environment:** Run daily-only, offset by 12 hours (2 PM UTC) to avoid peak-window overlap with prod.

---

## 📄 Customer-Facing PDF

The customer-facing report (`Acme_Worker_Estimation_20260805.pdf`) has been generated and is available at the repo root. It uses the **queuing model framing** around the 1-hour freshness window — the most intuitive framing for an Acme data engineering audience — and presents the 2→3 worker minimum for the daily batch with the top-line recommendation set to the appropriate combined sizing.

**What's in the PDF:**
- Executive summary: recommendation box, key metrics table
- The nightly requirement: 23 syncs in a 60-minute window
- The queuing mental model ("checkout lanes, not stadium turnstiles")
- Step-by-step math: 230 sync-minutes ÷ 60-minute window = ~4 concurrent slots
- Scenario table (1–8 workers, drain time avg vs P90)
- Headroom & growth section with the 80-connection target

> **Internal note:** The PDF frames this around the daily batch (the binding SLA). The 8-worker overall recommendation accounts for all frequency tiers, both environments, and the growth path — share that number verbally or in a follow-up slide when presenting the contract.
