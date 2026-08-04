The numbers are stable and clear. Here is the full analysis and interpretation.

---

## Acme — Prospect Worker Estimate

**Bottom line: 2 Data Workers at current scale, plan for 3 at ~2× growth.**

This estimate is remarkably stable — the result lands at **2 workers** across every connector-mix and frequency-split scenario tested for the baseline 40 connections. Here is why, and what the sensitivity looks like.

---

### How the Model Works

The estimation heuristic is:
```
Workers = ceil(Peak Concurrent API / 5) + ceil(Peak Concurrent DB / 2)
```

For 40 connections running a mix of hourly and daily syncs, each connection is concurrently active for only a few minutes per interval. The expected concurrent count at any moment is around **1.1–2.2 concurrent jobs** — well below the thresholds where the formula rounds up to 3 workers.

Durations used are fleet-observed averages from Airbyte Cloud:

| Type | Hourly avg | Daily avg |
|---|---|---|
| API connector | 5.5 min | 15.8 min |
| DB connector | 3.3 min | 4.5 min |

---

### Baseline Scenario Grid (40 connections)

| Scenario | API concurrent | DB concurrent | **Est. Workers** |
|---|---|---|---|
| Mixed, 60% hourly | 1.38 | 1.38 | **2** |
| Mixed, 50% hourly | 1.26 | 1.26 | **2** |
| Mixed, 40% hourly | 1.11 | 1.11 | **2** |
| API-heavy, 60% hourly | 2.16 | 0.94 | **2** |
| DB-heavy, 60% hourly | 0.66 | 1.52 | **2** |
| DB-heavy, 40% hourly | 0.45 | 1.23 | **2** |

**The estimate is 2 workers regardless of how the split lands.**

---

### Growth Scenarios

#### Most Likely (50/50 split, 50% hourly / 50% daily)

| Scale | Connections | Est. Workers |
|---|---|---|
| Baseline | 40 | **2** |
| +25% | 50 | **2** |
| +50% | 60 | **2** |
| +100% | 80 | **3** |

#### Conservative Bound (DB-heavy: 70% DB, 60% hourly)

| Scale | Connections | Est. Workers |
|---|---|---|
| Baseline | 40 | **2** |
| +25% | 50 | **2** |
| +50% | 60 | **3** |
| +100% | 80 | **3** |

**Recommended starting tier: 2 workers, with a clear upgrade path to 3.**

---

### What I Still Need — and Why It Matters

The "2 workers" answer is robust to the unknowns you have *today*, but three things could shift it meaningfully:

#### 1. **Are any connections sub-hourly?** *(Highest impact)*
Not mentioned, so the model assumes zero. Sub-hourly syncs dramatically raise concurrent counts because jobs overlap within the same interval. Even 10 sub-hourly connections could add 0.5–1.0 workers to the peak. **If any syncs run every 15–30 minutes, ask.**

#### 2. **Do all daily syncs fire at the same clock time?** *(Medium impact)*
The model distributes daily syncs evenly across the day. If all 20 daily connections are scheduled at midnight (a common default), peak concurrency spikes and the real-world number could be higher. **Knowing whether they plan to stagger schedules is worth asking.**

#### 3. **Are there high-volume tables that require full refreshes?** *(Medium impact at onboarding)*
The duration defaults assume typical incremental syncs (Avg 3–16 min). First-run or reconfigured connections doing full refreshes on large tables can run for hours. This temporarily spikes worker usage during onboarding but normalizes after the initial load. **Not a long-term capacity driver, but worth noting if they have very large tables.**

#### 4. **Which specific connectors?** *(Lower impact for this size, matters for CPU overrides)*
A handful of connector combinations carry a `cpu_request` override (e.g., SAP HANA, ServiceNow, some Salesforce configurations) that allocates 2–3× more CPU per sync, directly inflating the billing formula. At 40 connections this is unlikely to be a factor, but if the prospect mentions SAP, Oracle, or high-volume MSSQL, it's worth flagging.

---

### Caveats

> ⚠️ This uses the **heuristic estimation model** (`ceil(API/5) + ceil(DB/2)`), not the actual billing formula. Airbyte Cloud billing is CPU-based: `(maxSrcCPU + maxDstCPU + maxOrchCPU) / 8 = Data Workers`. The heuristic is a good planning tool but results will differ from actual billed usage once the customer is live. **Recommend re-assessing at 30 and 90 days using Metabase billing data.**
