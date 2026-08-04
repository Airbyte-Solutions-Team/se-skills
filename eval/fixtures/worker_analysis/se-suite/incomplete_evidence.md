Here is the full estimate and what's driving the uncertainty.

---

## Acme — Worker Estimate (40 Connections, API + DB Mix, Hourly + Daily)

### What the model tells us

I ran the estimation model across every plausible API/DB and hourly/daily split. One pattern holds perfectly across **all** of them:

| Metric | Value |
|---|---|
| **Steady-state workers** | **2** (consistent across every split tried) |
| **Burst workers (worst-case)** | **5 – 9** (depends heavily on the split and schedule alignment) |

**Why steady-state is always 2:** At 40 connections with typical Airbyte sync durations (API: avg 5.5 min/hour; DB: avg 3.3 min/hour), only ~1–2 syncs are actually running at any given moment. The estimation formula — `ceil(concurrent API / 5) + ceil(concurrent DB / 2)` — rounds up to 1 worker for each type, giving 2 total. This holds even under P90 duration assumptions.

**Why burst is the real question:** The formula changes dramatically if daily syncs pile up at the same moment. This is very common — many customers set crons like `0 0 * * *` (midnight) and all their daily connections fire simultaneously. Here's the burst range across splits:

| Scenario | Steady-state | Burst (all daily fire at once) |
|---|---|---|
| 60% API / 40% DB, 50% hourly / 50% daily | 2 | **8** |
| 50% API / 50% DB, 50% hourly / 50% daily | 2 | **9** |
| 40% API / 60% DB, 50% hourly / 50% daily | 2 | **9** |
| 60% API / 40% DB, 70% hourly / 30% daily | 2 | **5** |
| 50% API / 50% DB, 70% hourly / 30% daily | 2 | **6** |

Growth doesn't move the steady-state needle much at this scale — even at 2x connections (80), steady-state stays at 2 workers. **Burst, on the other hand, scales linearly** — 2x connections → roughly 2x burst workers.

---

### Sizing recommendation (given what we know now)

| Scenario | Recommended Contract |
|---|---|
| Daily syncs are spread throughout the day (staggered) | **2–3 workers** |
| Daily syncs may pile up at the same time | **4–6 workers** (headroom for burst) |
| DB-heavy AND daily syncs all fire at once | **8–10 workers** (worst case) |
| +50% growth planned (60 connections) | Add ~1 worker to whichever row above applies |

My middle-ground starting recommendation without knowing the answers below: **3–4 workers** — this comfortably covers steady-state plus a reasonable burst buffer, and gives room for modest growth.

---

### What I still need to refine this

Here are the five questions in priority order, with why each one matters:

1. **Exact API/DB split** *(highest impact on burst sizing)*
   The DB-heavy cases hit burst workers of 9 vs. 8 for API-heavy because DB connections consume 1 worker per 2 concurrent vs. 1 per 5 for API. If they're, say, 70% DB, the burst sizing goes up.

2. **How are daily syncs scheduled — staggered or same-time?**
   This is the single biggest risk factor. If every daily connection uses the same cron hour (e.g., `0 0 * * *`), burst can hit 6–9 workers for just a few minutes. If they're spread across hours, burst is minimal. Ask: *"Do the daily syncs run at the same time, or are they distributed throughout the day?"*

3. **More precise hourly vs. daily count**
   The burst size is driven almost entirely by the daily count. 10 daily connections → much smaller burst than 20 daily connections. Even a rough answer (e.g., "about a third daily") cuts the uncertainty significantly.

4. **Expected growth factor and timeline**
   "Expect to grow" could mean +25% or +300%. At 80 connections the steady-state stays at 2–4 workers, but worst-case burst could reach 16–17 — those are very different contracts. Even a ballpark (e.g., "double in 12 months") lets me give a firm growth headroom number.

5. **Average sync duration for their specific connectors**
   The empirical defaults I'm using are Airbyte fleet medians (3–16 min). If Acme has connectors doing large full-refresh syncs or running SAP HANA / Oracle with `cpu_request` overrides, actual durations (and worker costs) could be 3–5× higher. Ask: *"Are any syncs doing full table refreshes, or are they all incremental? Any enterprise DB sources like SAP, Oracle, or large Postgres?"*

---

> ⚠️ **Caveat:** This is a prospect estimate using the statistical concurrency model (`ceil(concurrent DB / 2) + ceil(concurrent API / 5)`), not the actual CPU-based billing formula the platform uses once they're live on Airbyte Cloud. Once Acme is running, Metabase billing data replaces this estimate as the source of truth. Real usage may differ from this range.
