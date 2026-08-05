The PDF is generated at `Acme_Worker_Estimation_20260805.pdf`. Here's the full picture:

---

## Acme Prospect Estimation — Results & Gaps

### Estimate: **6–7 Data Workers** (recommended starting capacity)

I ran three scenarios across the realistic range of connector mixes and sync durations. All three converge tightly:

| Scenario | API/DB Split | Avg Duration | Steady-State | Prod + Staging | Worst-Case Burst | **Recommended** |
|---|---|---|---|---|---|---|
| Balanced mix | 50% / 50% | 5.5 min (fleet avg) | 2 workers | 4 workers | 9 workers | **6 workers** |
| API-heavy | 60% / 40% | 15 min (P90 conservative) | 3 workers | 5 workers | 8 workers | **7 workers** |
| DB-heavy | 40% / 60% | 5.5 min (fleet avg) | 2 workers | 4 workers | 9 workers | **6 workers** |

The **6-worker** figure is the right starting point to put in front of the prospect. It covers prod + staging, absorbs the typical burst headroom of 2 workers, and holds across every mix we tested. If the actual mix turns out to be significantly DB-heavy and syncs are long, 7 workers is the right ceiling.

---

### How the formula works (for your reference)

The estimation model uses: `ceil(concurrent API jobs / 5) + ceil(concurrent DB jobs / 2) = Data Workers`

DB connections are capacity-heavy (1 worker handles only 2 DB syncs at once vs. 5 API syncs), so the split matters—but with only 40 connections and short empirical durations, the steady-state concurrency stays low (< 2 concurrent API and < 2 concurrent DB at any given moment). The bigger risk is the **worst-case burst**: if all 20 daily syncs fire at the same timestamp (e.g., every connection set to midnight UTC), that temporarily spikes to 9 workers. Staggered schedules collapse that to steady-state.

---

### What else you need to ask — in priority order

These four questions would materially sharpen or change the estimate:

#### 1. 🔴 How many of the 40 connections are databases vs. SaaS APIs? *(Highest impact)*
DB connections (Postgres, MySQL, MSSQL, Snowflake, etc.) consume 2.5× more worker capacity than API connectors (Salesforce, HubSpot, Stripe, etc.). A 100% DB workload at 40 connections needs ~3× the workers of a 100% API workload. Even a rough answer like "mostly APIs" or "mostly databases" is useful.

#### 2. 🟡 What's the rough hourly vs. daily split? *(Medium impact on steady-state, high impact on burst)*
"Some hourly, some daily" is too wide to pin down. The key question is whether the daily syncs are **all scheduled at the same time** (e.g., midnight batch) or **spread throughout the day**. Clustered daily syncs create a burst spike that can be 3–5× higher than steady-state. If they can confirm schedules are (or will be) staggered, the burst number drops significantly.

#### 3. 🟡 Roughly how long do most syncs take? *(Drives steady-state concurrency directly)*
The fleet median is ~3–6 minutes for API connectors and ~2–4 minutes for databases. If their syncs are unusually long (15+ minutes, e.g., large initial loads or wide tables), steady-state concurrency rises and the recommendation moves toward 7–8 workers. Short syncs (under 5 min) keep steady-state at 2 workers regardless of mix.

#### 4. 🟢 What's the growth target, even roughly? *(Needed to size the contract)*
"Expect to grow" is fine for an initial conversation, but a contract sizing needs a number. Ask: *"Over the next 12 months, do you expect to double connections, add 20%, or grow faster?"* Even a broad bucket (1.5× or 2×) lets us put a future-growth row in the estimate. At 2× (80 connections), the recommendation steps up to 8–9 workers—a meaningful difference for contract sizing.

---

### Lower-priority questions (nice to have)

- **Number of environments** — The estimate assumes prod + staging (2 environments). If they only have production, drop 1 worker from the combined figure.
- **Data freshness SLAs** — If any syncs have hard deadlines (e.g., "the dashboard must be current by 7 AM"), the drain analysis matters more and may push the hourly batch recommendation up.
- **Any sub-hourly syncs?** — The estimate currently assumes none. Even a handful of 15-minute syncs significantly changes the steady-state concurrency.

---

The PDF at `Acme_Worker_Estimation_20260805.pdf` is the customer-facing version of this estimate — it uses the balanced 50/50 scenario as the base case and shows growth headroom. Once you have answers to questions 1–2 above, I can re-run with the actual numbers and the estimate will tighten considerably.
