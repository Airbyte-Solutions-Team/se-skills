# Deployment Model Qualification — Halcyon Health

**Date:** August 19, 2026 · **Account:** Halcyon Health · **Skill:** deployment-model-qual

### Decision Summary
- **Verdict:** 🟡 viable with a dated security dependency
- **Stage:** technical validation
- **#1 Blocker:** security review is not booked
- **Recommended Motion:** get the architecture review on the calendar this week
- **Confidence:** 🟡 medium — budget line unconfirmed

**Jump to:** [Deployment Verdict](#deployment-verdict) · [Customer Constraints](#customer-constraints) · [Remaining Validation](#remaining-validation) · [Recommended Motion](#recommended-motion)

## Deployment Verdict

Read the sub-sections in order; each one carries its own evidence.

### Verdict

Dana's team publishes 140 dashboards off the warehouse; the ones that matter to the CFO are the four that depend on the orders pipeline [stated — Dana Okafor].

- **Sources in scope:** Salesforce, Postgres, NetSuite, Zuora
- **Destination:** Snowflake (prod warehouse `ANALYTICS_WH`)
- **Sync cadence:** hourly, with a nightly full-refresh fallback
- **Open item:** marketing sources have no named owner

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

### Product Reality Stamp

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

- **Current stack:** Fivetran + dbt Cloud + Snowflake
- **Volume:** ==1.4 TB/day== peak, 38 sources
- **Deployment model:** Cloud with VPC peering
- **Contract renewal:** 2026-12-31
- **Unknown:** SLA for the marketing sources

### Verdict Breakdown

Wei's constraint is narrow but firm: no customer PII may transit a shared control plane. That is the reason the deployment-model question comes before pricing.

- **Owner:** Priya Raman
- **Decision date:** target 2026-10-15
- **Evidence needed:** signed security review
- **Unknown:** who approves the final spend

The team runs 38 sources into Snowflake and misses the 06:00 finance window roughly twice a week. Marcus traced it to a full refresh on two Postgres sources that no longer support CDC on their current version [stated — Marcus Feld].

## Customer Constraints

What follows is the current read, with the unknowns called out explicitly.

### The Five Questions

- **What breaks today when the nightly load misses the 06:00 finance window?**
  *Listen for: named revenue or reporting impact, not general frustration.*
- **Who signs off if data leaves the VPC?**
  *Why this matters: names the real security approver early.*
- **What made you look at replacing the current stack this quarter?**
  *Listen for: a dated trigger event rather than a standing wish.*

### Implications by Answer

Analytics is the loudest consumer, but the platform team owns the decision. Nothing in the current stack is contractually locked past December, which is why the evaluation is happening now rather than next year [inferred — timing of the renewal].

- **Current stack:** Fivetran + dbt Cloud + Snowflake
- **Volume:** ==1.4 TB/day== peak, 38 sources
- **Deployment model:** Cloud with VPC peering
- **Contract renewal:** 2026-12-31
- **Unknown:** SLA for the marketing sources

Dana's team publishes 140 dashboards off the warehouse; the ones that matter to the CFO are the four that depend on the orders pipeline [stated — Dana Okafor].

## Remaining Validation

Dana's team publishes 140 dashboards off the warehouse; the ones that matter to the CFO are the four that depend on the orders pipeline [stated — Dana Okafor].

- **Owner:** Priya Raman
- **Decision date:** target 2026-10-15
- **Evidence needed:** signed security review
- **Unknown:** who approves the final spend

## Recommended Motion

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

- **Sources in scope:** Salesforce, Postgres, NetSuite, Zuora
- **Destination:** Snowflake (prod warehouse `ANALYTICS_WH`)
- **Sync cadence:** hourly, with a nightly full-refresh fallback
- **Open item:** marketing sources have no named owner

Wei's constraint is narrow but firm: no customer PII may transit a shared control plane. That is the reason the deployment-model question comes before pricing.

## Source Coverage

Every source below was read in full before this document was written.

- `_transcripts/Northwind-08.14.26.txt` — 412 / 412 lines read
- `outputs/biz-qual/2026-08-02-biz-qual.md` — full document
- `outputs/tech-qual/2026-08-11-tech-qual.md` — full document
- Salesforce opportunity record — stage, amount, close date
- Gong call `northwind-2026-08-14` — full transcript
- Product reference snapshot — 2026-08-18

_No source was partially read; nothing was inferred from a summary._
