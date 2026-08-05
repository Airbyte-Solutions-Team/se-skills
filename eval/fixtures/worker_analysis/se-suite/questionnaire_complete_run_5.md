Both files are ready. Page counts check out (5 pages for customer — the Recommendations section with growth chart overflowed one page; 8 pages internal as designed). Let me now present the full analysis:

---

## 📋 Acme Worker Estimation — August 5, 2026

**Estimation mode:** Mode 2A — Questionnaire / Statistical (prospect, no platform data)
**Model:** Universal heuristic: `ceil(API concurrent / 5) + ceil(DB concurrent / 2)`

> ⚠️ **Important caveat:** This uses the estimation model, not the CPU-based billing formula. Once Acme is on the platform, re-run using actual job overlap data (workspace analysis) for billing-accurate P99 results. Actual billing: `(maxSourceCPU + maxDestCPU + maxOrchestratorCPU) / 8`.

---

### Connection Matrix

| Type | Sub-hourly (15 min) | Hourly | Daily | **Total** |
|------|---------------------|--------|-------|-----------|
| API (Salesforce, HubSpot, Stripe…) | 6 | 9 | 15 | **30** |
| DB/File (Postgres, MySQL) | 3 | 4 | 8 | **15** |
| **Total** | **9** | **13** | **23** | **45** |

---

### The Seven Sizing Views

| Sizing View | Workers | Basis |
|---|---|---|
| **1. Steady-state requirement** | **4** | `ceil(5.60/5) + ceil(2.72/2)` — long-run average concurrency |
| **2. Peak-window drain requirement** | **2** | Drain 23 daily syncs within the 60-min freshness SLA |
| **3. Worst-case simultaneous burst** | **11** | All 23 daily syncs fire at once + sub-hourly/hourly baseline in parallel: `ceil(20.5/5) + ceil(10.67/2)` |
| **4. Production-only requirement** | **4** | Same as steady-state (prod only) |
| **5. Combined prod + staging** | **6** | Prod (4 DW) + 1 staging env (2 DW daily-only model) |
| **6. Future-growth requirement** | **8** | Prod + staging at 80-connection target |
| **7. Recommended capacity** | **8** | `max(combined(6) + headroom(2), future_combined(8))` |

---

### Headroom Decision Logic

```
has_daily = True, environments = 2 → base_headroom = 1
burst_delta = max(0, 11 − 6) = 5 → burst_headroom = min(2, 5) = 2
final_headroom = max(1, 2) = 2

recommended = max(6 + 2, 8) = 8 Data Workers ✅
```

---

### Growth Scenario Projection

| Scenario | Connections | Est. Steady-State Workers | Combined (+ staging) | vs. 8 DW |
|---|---|---|---|---|
| Current | 45 | 4 | 6 | **−2 DW headroom** |
| +25% | 56 | 5 | 7 | −1 DW headroom |
| +50% | 68 | 6 | 8 | At capacity — reassess |
| Growth target | 80 | 6 | 8 | At capacity — upgrade likely needed |

**Signal:** Acme will be comfortable through the 6-month growth target at 8 DW, but should plan a capacity review when approaching 68+ connections, or sooner if connector mix shifts toward more DB sources (which have a higher per-worker cost at `DB/2` vs. `API/5`).

---

### Scheduling Note (2–6 AM UTC Peak Window)

The worst-case burst of **11 DW** — well above the recommended contract of 8 — arises only if all 23 daily syncs start simultaneously. Staggering them:

| Action | Effect |
|---|---|
| Spread 15 API daily syncs across 2:00–4:00 AM (every ~8 min) | Cuts API burst from 15 simultaneous → ~2–3 concurrent |
| Spread 8 DB daily syncs across 3:00–5:00 AM (every ~15 min) | Cuts DB burst from 8 → ~1–2 concurrent |
| Schedule staging to 10:00 AM–4:00 PM UTC | Eliminates prod/staging overlap entirely |

**Projected peak after staggering: ~6–7 DW** (within contract, no queueing risk).

---

### Deliverables

| File | Purpose | OK to share? |
|---|---|---|
| `Acme_Worker_Estimation_20260805.pdf` | Customer-facing executive report (5 pages, 108 KB) | ✅ Yes — safe to forward to Acme |
| `Acme_Worker_Report_Internal_20260805.pdf` | Internal detailed report with formulas, concurrency math, cron schedules (8 pages, 183 KB) | 🔒 No — Airbyte internal only (watermarked on every page) |

The customer report covers: Executive Summary → Modeled Load Profile chart → Growth Scenarios chart → Scheduling suggestions (natural language only, no formulas or cron syntax).

The internal report covers: Full methodology → Steady-state walkthrough → All 7 sizing views with math → Worst-case burst breakdown → Growth scenarios → Quartz cron schedule recommendations → Confidence & methodology table.
