All three scenarios converge on the same number. Here's the full picture:

---

## Acme Prospect: Worker Estimate

> ⚠️ **This is a statistical estimate.** It uses the heuristic model (`ceil(DB concurrent / 2) + ceil(API concurrent / 5)`), not the CPU-based billing formula used for actual Cloud customers. Actual billing may differ — once Acme is on the platform, real job history analysis replaces this.

---

### The Answer: **6 Data Workers** — across all scenarios

All three API/DB mixes converge to the same recommendation. Here's why:

| Sizing View | API-heavy (70/30) | Mixed (50/50) | DB-heavy (30/70) |
|---|---|---|---|
| **Steady-state** | 2 | 2 | 2 |
| **Peak-window drain** | 2 | 2 | 3 |
| **Worst-case burst** | 8 | 10 | **12** |
| **Prod-only** | 2 | 2 | 2 |
| **Prod + Staging** | 4 | 4 | 4 |
| **Future growth (60 conns)** | 4 | 4 | 5 |
| 🟢 **Recommended contract** | **6** | **6** | **6** |

**The recommendation of 6 holds** because: prod+staging combined steady-state = 4 in all scenarios, plus 2 workers of headroom to absorb burst, which exceeds the future-growth estimate of 4–5 across every mix. The connector split shifts the *burst ceiling* significantly (8→12) but doesn't change the contract recommendation at this connection count.

---

### What I assumed (and where those assumptions matter)

| Assumption | Value Used | Impact if Wrong |
|---|---|---|
| Hourly / Daily split | 40% hourly / 60% daily | Low-medium on steady-state; medium on burst |
| Sub-hourly syncs | **None assumed** | **High** — even a handful of sub-hourly syncs spike workers |
| Avg sync duration | 8 min (empirical fleet median) | Medium on burst; low on steady-state |
| Environments | 2 (prod + staging) | Medium — prod-only drops recommendation to 3–4 |
| Growth target | 60 connections (~50% growth) | Low — growth estimate already fits in 6 |

---

### What would sharpen or change this estimate

Here are the five questions to ask, ordered by how much they affect the answer:

1. **Any sub-hourly syncs?** *(biggest risk item)* — Even 5–10 connections running every 5 or 15 minutes create constant concurrency that the daily-burst model doesn't capture. If yes, the estimate could jump to 8–10.

2. **Exact hourly vs. daily split?** — If it's heavily hourly (e.g., 70%+ hourly), steady-state workers climb. If mostly daily, 6 is comfortably conservative.

3. **Firm growth target?** — "Expect to grow" at 60 connections stays within 6. If growth is 100+ connections, we'd re-run and likely land at 8–10.

4. **Production-only, or prod + staging?** — Assumed 2 environments. If prod-only, 3–4 workers may suffice today, with 6 as a growth buffer.

5. **Any large-table database syncs?** *(long duration risk)* — Database connectors syncing many millions of rows often run 30–90+ minutes. If the DB connections are full-table large syncs, the average duration assumption of 8 min is too low and burst workers could be materially higher.

---

**Bottom line:** Quote **6 Data Workers** as the initial contract. It covers prod+staging today, absorbs the growth to ~60 connections without a contract change, and provides 2 workers of burst headroom regardless of the connector mix. If any sub-hourly syncs exist, revisit — that's the one input that could push the number materially higher.
