The calculator completed successfully. Here's the full analysis for **Acme** (prospect sizing).

---

## 🏢 Acme — Worker Sizing Estimate

**Analysis date:** 2026-08-05 | **Deployment:** Airbyte Cloud | **Model:** Estimation (Statistical — prospect, no live billing data yet)

---

### Connection Matrix

| Type | Sub-hourly (15 min) | Hourly | Daily | **Total** |
|---|---|---|---|---|
| API (Salesforce, HubSpot, Stripe) | 6 | 9 | 15 | **30** |
| DB/File (Postgres, MySQL) | 3 | 4 | 8 | **15** |
| **Total** | **9** | **13** | **23** | **45** |

---

### Seven Sizing Views (Source of Truth — Do Not Collapse)

| Sizing View | Workers | What It Means |
|---|---|---|
| **Steady-state requirement** | **4** | Long-run average concurrency across all connections and frequencies |
| **Peak-window drain requirement** | **2** | Workers needed to drain the daily batch within the 60-min freshness window |
| **Worst-case simultaneous burst** | **11** | All daily syncs start at once at 2 AM UTC — the worst credible spike |
| **Production-only requirement** | **4** | Prod steady-state only (no staging) |
| **Combined prod + staging requirement** | **6** | Prod steady-state + one staging environment |
| **Future-growth requirement** | **8** | Combined prod + staging at 80-connection growth target |
| **✅ Recommended contract capacity** | **8** | `max(combined 6 + headroom 2, future growth 8)` |

---

### 🎯 Recommendation: **8 Data Workers**

**Decision logic applied by the calculator:**
- Combined prod + staging steady-state: **6 workers**
- Headroom added (daily syncs + multi-environment): **+2 workers**
- Future growth at 80 connections (prod + staging): **8 workers**
- Recommendation = `max(6 + 2, 8)` = **8 workers**

---

### Key Observations

**✅ Daily-sync freshness is comfortable**
With 23 daily syncs and a 60-minute freshness window, the calculator only needs **2 workers** to drain that batch — the 15-sub-hourly and 13-hourly connections are already handled by steady-state. The 2–6 AM UTC peak window fits entirely within this envelope.

**⚠️ Burst risk is real but manageable at 8 workers**
Worst-case burst (all 23 daily syncs fire simultaneously) hits **11 workers** — 3 above the recommended contract of 8. This is an unlikely scenario for incremental workloads, but worth noting. With Airbyte's default scheduling, the platform naturally staggers starts, so simultaneous bursts rarely materialize in practice. The 8-worker contract provides strong protection against realistic burst shapes.

**📈 Growth path is already baked in**
The 80-connection growth target drives the final recommendation — the contract is already sized to accommodate that growth without renegotiation. At 80 connections with the same mix, combined prod + staging lands at **8 workers**.

**🗄️ DB/API mix is well-balanced**
30 API connections (÷ 5 per worker) and 15 DB connections (÷ 2 per worker) produce a balanced load. DB connections are the binding constraint per-worker (MySQL, Postgres are heavier), but the 33%/67% split keeps this from dominating.

---

### Steady-State Concurrency Breakdown

| Component | Concurrent Jobs | Workers Consumed |
|---|---|---|
| API concurrent | 5.6 jobs | ceil(5.6 / 5) = **2** |
| DB concurrent | 2.7 jobs | ceil(2.7 / 2) = **2** |
| **Steady-state total** | | **4 workers** |

---

### ⚠️ Important Caveats

1. **This is a statistical estimate.** The recommendation uses the heuristic model (`ceil(DB/2) + ceil(API/5)`), which does NOT match the actual Airbyte Cloud billing formula. The platform bills by CPU: `(maxSourceCPU + maxDestCPU + maxOrchestratorCPU) / 8`. Once Acme is live, actual CPU-based hourly data from Metabase is the source of truth.

2. **No initial load assumed.** This sizing is for recurring incremental syncs only, as specified. If Acme later triggers initial loads (full refresh or historical backfills), those jobs carry significantly higher CPU and would temporarily spike worker usage well above these estimates.

3. **Connector defaults assumed.** Salesforce/HubSpot/Stripe use API connector defaults (0.8 src + 0.3 dst + 0.3 orch = **1.4 CPU total** per sync). Postgres/MySQL use DB connector defaults (1.0 src + 1.0 dst + 1.0 orch = **3.0 CPU total** per sync). Any `cpu_request` overrides applied to specific connections would change the billing result.

4. **Staging is included.** The 8-worker contract covers both prod and staging. If staging is de-scoped or runs lighter workloads, the contract could potentially be reduced to 6.

---

**Bottom line for the conversation with Acme:** Start with **8 Data Workers**. This covers current production + staging, absorbs realistic burst behavior, and is already sized for the planned growth to 80 connections — avoiding a renegotiation in 6 months.

Would you like me to generate the two PDF reports (customer-facing executive summary + internal detailed report) for this estimate?
