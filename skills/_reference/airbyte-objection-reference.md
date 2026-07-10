# Airbyte Objection Reference

Quick reference for common customer objections, the reality behind each, and the recommended path forward. Used by the `objection-handler` skill. Update as Airbyte's product/positioning evolves.

**Last updated:** 2026-07-10
**Owner / refresh cadence:** Solutions team — review product-fact rows (deployment GA status, pricing model, supported regions, compliance certs by plan) at least quarterly and after any GTM/product announcement. A single stale row here propagates into `objection-handler` and `deployment-model-qual`, so treat freshness as a first-class maintenance task, not a nicety.

---

## Deployment Model Objections

> **Product reality (as of 2026-07-10):** Airbyte sells **two** live deployment shapes: **Cloud Pro** and **Enterprise Flex (Hybrid)**. **Enterprise Flex** — Airbyte-hosted control plane + a **customer-hosted self-managed data plane** running in the customer's own VPC (Helm on K8s, or Airbox for single-node), with full 600+ connector parity — is **sellable to new customers, with caveats** (region availability, deal-desk/commercial approval, and possible limited-availability gating — confirm current terms before positioning). This means "data can't leave our environment" and "we need VPC isolation" are **no longer an automatic park** — Flex is often the answer. **Self-Managed Enterprise (SME) — the historical fully self-hosted shape (entire platform incl. control plane, plus BYOK/KMS) — has been retired and is not currently offered (may return).** It is a product-capability fact, not a sellable motion: for a genuine Cloud-and-Flex boundary (customer must control the whole platform, or needs BYOK/KMS), there is **no currently-offered shape that meets it** — be upfront that it's a park / no-fit today. Do not route or recommend SME as a live option.

| Objection | Reality | Path Forward |
|-----------|---------|--------------|
| "Data can't leave our environment" | The **data** never has to. On Cloud Pro, Airbyte runs the data plane; on **Enterprise Flex**, the customer runs a self-managed data plane in their own VPC — data stays in their environment while Airbyte hosts only the control plane. | **Position Enterprise Flex** (data-plane isolation without full self-host). Confirm current Flex availability/terms for their region. If they must control the *control plane* too, that's a genuine Cloud-and-Flex boundary — no currently-offered fit; park/disqualify honestly (SME historically covered full self-host; retired, may return). |
| "We need our own KMS / BYOK" | Not supported on Cloud Pro **or** Flex (both use Airbyte-managed secrets). BYOK/customer-managed KMS was a **Self-Managed Enterprise capability** — and SME is **retired / not currently offered** (may return). | **No currently-offered fit — park/disqualify honestly.** Be upfront: no live shape does customer-managed KMS today. (SME historically covered this; retired, may return.) Genuine Cloud-and-Flex boundary — don't oversell Flex, and don't route to a dead motion. |
| "Multi-tenancy is a problem for our regulated data" | Cloud is shared infra (with org-level isolation); **Flex gives a dedicated, customer-run data plane** so regulated data is processed in isolation. | Probe the specific concern. If it's "our data can't share compute," **Flex resolves it**. If it's a broader control-plane/full-platform tenancy mandate, that's a genuine Cloud-and-Flex boundary — **no currently-offered fit, so park/disqualify honestly** (SME historically covered full-platform isolation; retired, may return). |
| "We need BYOC / hybrid (Flex)" | **Flex is sellable to new customers as of July 2026, with caveats** (region/commercial/limited-availability gating — confirm current terms). | **Position Flex.** Confirm current availability + terms with the AE/deal-desk before committing to a timeline. Do NOT park or say "not GA." |
| "We need VPC isolation for the data plane" | **Flex runs the data plane in the customer's VPC.** Cloud Pro runs Airbyte's. (Running the *control plane* in-VPC too was an SME capability — SME is retired / not currently offered.) | **Position Flex** for data-plane VPC isolation. If they require the **control plane in-VPC** as well, that's a genuine Cloud-and-Flex boundary — **no currently-offered fit, park/disqualify honestly** (SME retired, may return). |
| "Data residency in [specific region]" | Cloud managed regions = US + EU. **Flex supports per-region data planes** (EU/US/APAC possible) since the customer hosts them. | If the region isn't a Cloud managed region, **Flex** (customer-hosted data plane in-region) is usually the answer. Verify current supported regions/terms. |
| "Where does our data actually flow on Cloud?" | **Compliance nuance:** the Cloud **control plane is US-hosted**, and cursor / primary-key values pass through it even on EU data planes. For regulated data this can surface in security review. | Surface this **proactively** for regulated-data deals. **Flex** (customer data plane) keeps the actual records in-environment. If even control-plane metadata transit is a hard boundary — i.e. the control plane itself must be customer-hosted — that's a genuine Cloud-and-Flex boundary with **no currently-offered fit; park/disqualify honestly** (SME historically covered a fully customer-hosted control plane; retired, may return). |

## Trust / OSS Objections

| Objection | Reality | Path Forward |
|-----------|---------|--------------|
| "Open source is a support risk" | Cloud Pro is fully managed by Airbyte with SLAs | Reframe — OSS is the distribution model, not the support model. Cloud Pro is enterprise-grade. |
| "What happens to my data if Airbyte goes away?" | Sources are yours; destinations are yours; Airbyte is a pipeline | Reframe — Airbyte doesn't hold your data. Worst case you swap the ETL layer. |
| "Why should we trust Airbyte vs. a 20-year incumbent?" | Track record + connector count + community velocity | Customer references; benchmark data on reliability |

## Pricing / Commercial Objections

| Objection | Reality | Path Forward |
|-----------|---------|--------------|
| "Fivetran is cheaper at our volume" | Different pricing *shapes*: Airbyte **Pro and Flex are capacity-based (priced on Data Workers / compute capacity — predictable, decoupled from data volume)**; only **Airbyte Standard** is volume/credit-based (APIs ~$15/M rows, DBs ~$10/GB). Fivetran is consumption-based (MAR) — the bill spikes with volume. | Reframe as **predictable capacity-based spend vs. variable consumption-based spend**. Run their real volume/growth profile through both models; show 3-yr TCO, not list price. The differentiator is *predictability at scale*, not a lower sticker. |
| "We can't justify the cost vs. what we have today" | Often "what we have today" doesn't include engineering maintenance cost | Stack the implications — engineer hours/week × loaded cost × 3 years |
| "Annual contracts only? We want monthly." | Standard is annual for Cloud Pro | Explain why (predictable infra commitments) — push back if their evaluation timeline genuinely doesn't fit |

## Build-vs-Buy Objections

| Objection | Reality | Path Forward |
|-----------|---------|--------------|
| "Why not just build it ourselves?" | Engineering team underestimates the long tail | TCO conversation: 1-3 weeks per connector + 5-15% maintenance/year + on-call burden + schema-drift handling + opportunity cost. Multiply by connector count. |
| "Our team built one in a week" | Sure — and now they own it forever | Reframe — building one is the easy part. The 6-month break-fix cycle is the cost. |
| "We're already using dbt + custom EL" | dbt is transformation; doesn't handle extraction reliably | Position as complementary — Airbyte handles E + L, dbt handles T. Stop maintaining custom EL. |

## Reliability / Scale Objections

| Objection | Reality | Path Forward |
|-----------|---------|--------------|
| "We've heard about sync failures with Airbyte" | Some community connectors are flaky; certified connectors meet enterprise SLAs | Be specific about which connectors they need; flag certified vs. community |
| "We need sub-minute latency" | Standard syncs run on schedules; CDC supports near-real-time for DB sources | Match their latency requirement to specific connectors and modes |
| "Will this scale to billions of rows/day?" | Yes with proper sizing — Cloud Pro handles many enterprise workloads at scale | Walk through architecture; offer reference customer at similar scale |

## Connector Gap Objections

| Objection | Reality | Path Forward |
|-----------|---------|--------------|
| "You don't have a connector for [X]" | Either we do (verify with `connector-feasibility`), or we have a manifest-only builder / custom CDK | If gap: outline build path (manifest-only YAML or custom CDK) + effort estimate. Reframe to coverage of *their* stack, not raw count. |
| "Your connector for [X] is community/alpha-tier" | Be honest about tier; flag known reliability issues | Don't oversell. Offer custom validation in POC. |
| "Fivetran has 3x the connector count" | Count includes connectors most customers never use | Reframe to coverage of *your* stack, not total count |

---

## How to use this reference

- The `objection-handler` skill reads this doc to ground talk tracks in accurate Airbyte positioning
- Update this doc when:
  - A product capability changes (e.g., BYOK becomes available on Cloud)
  - A new objection pattern emerges from customer calls
  - A "Reality" column becomes outdated
- Keep "Reality" honest — this is internal reference, not marketing copy. If we genuinely can't do something, say so.
