# Airbyte Objection Reference

Quick reference for common customer objections, the reality behind each, and the recommended path forward. Used by the `objection-handler` skill. Update as Airbyte's product/positioning evolves.

**Last updated:** 2026-05-27

---

## Deployment Model Objections

| Objection | Reality | Path Forward |
|-----------|---------|--------------|
| "Data can't leave our environment" | Cloud not viable — Airbyte runs the data plane on Cloud Pro | Requalify to Self-Managed Enterprise |
| "We need our own KMS / BYOK" | Not supported on Cloud Pro | Self-Managed Enterprise |
| "Multi-tenancy is a problem for our regulated data" | Cloud is shared infra (with org-level isolation) | Probe what specifically is the concern — often resolvable, sometimes not. If true regulatory blocker, requalify to SME. |
| "We need BYOC / hybrid (Flex)" | Flex not GA for new customers as of May 2026 | Park the deal or escalate to leadership for exception |
| "We need VPC isolation for the data plane" | Cloud runs Airbyte's data plane, not customer's | Self-Managed Enterprise |
| "Data residency in [specific region]" | Cloud supports US + EU; other regions are SME-only | Verify current regions available; requalify if needed |

## Trust / OSS Objections

| Objection | Reality | Path Forward |
|-----------|---------|--------------|
| "Open source is a support risk" | Cloud Pro is fully managed by Airbyte with SLAs | Reframe — OSS is the distribution model, not the support model. Cloud Pro is enterprise-grade. |
| "What happens to my data if Airbyte goes away?" | Sources are yours; destinations are yours; Airbyte is a pipeline | Reframe — Airbyte doesn't hold your data. Worst case you swap the ETL layer. |
| "Why should we trust Airbyte vs. a 20-year incumbent?" | Track record + connector count + community velocity | Customer references; benchmark data on reliability |

## Pricing / Commercial Objections

| Objection | Reality | Path Forward |
|-----------|---------|--------------|
| "Fivetran is cheaper at our volume" | Pricing models differ (MAR vs. rows) — true cost depends on volume profile | Run actual numbers on their specific volume; show TCO over 3 years not list price |
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
