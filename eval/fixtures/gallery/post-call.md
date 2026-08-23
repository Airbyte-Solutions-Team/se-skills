# Post Call — Northwind Analytics

**Date:** August 19, 2026 · **Account:** Northwind Analytics · **Skill:** post-call

### Call Snapshot
- **Verdict:** 🟡 viable with a dated security dependency
- **Stage:** technical validation
- **#1 Blocker:** security review is not booked
- **Recommended Motion:** get the architecture review on the calendar this week
- **Confidence:** 🟡 medium — budget line unconfirmed

**Jump to:** [Key Takeaways](#key-takeaways) · [Deal Impact](#deal-impact) · [Scope & Technical Changes](#scope-&-technical-changes) · [Objections & Open Questions](#objections-&-open-questions)

## Key Takeaways

Analytics is the loudest consumer, but the platform team owns the decision. Nothing in the current stack is contractually locked past December, which is why the evaluation is happening now rather than next year [inferred — timing of the renewal].

- **Owner:** Priya Raman
- **Decision date:** target 2026-10-15
- **Evidence needed:** signed security review
- **Unknown:** who approves the final spend

## Deal Impact

What follows is the current read, with the unknowns called out explicitly.

### Movement

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

- **Current stack:** Fivetran + dbt Cloud + Snowflake
- **Volume:** ==1.4 TB/day== peak, 38 sources
- **Deployment model:** Cloud with VPC peering
- **Contract renewal:** 2026-12-31
- **Unknown:** SLA for the marketing sources

### Deal Health

Wei's constraint is narrow but firm: no customer PII may transit a shared control plane. That is the reason the deployment-model question comes before pricing.

- **Owner:** Priya Raman
- **Decision date:** target 2026-10-15
- **Evidence needed:** signed security review
- **Unknown:** who approves the final spend

The team runs 38 sources into Snowflake and misses the 06:00 finance window roughly twice a week. Marcus traced it to a full refresh on two Postgres sources that no longer support CDC on their current version [stated — Marcus Feld].

### MEDDPICC Changes

The team runs 38 sources into Snowflake and misses the 06:00 finance window roughly twice a week. Marcus traced it to a full refresh on two Postgres sources that no longer support CDC on their current version [stated — Marcus Feld].

- **Sources in scope:** Salesforce, Postgres, NetSuite, Zuora
- **Destination:** Snowflake (prod warehouse `ANALYTICS_WH`)
- **Sync cadence:** hourly, with a nightly full-refresh fallback
- **Open item:** marketing sources have no named owner

## Scope & Technical Changes

The three items below are what actually moves this forward this week.

### Sources & Destinations

Dana's team publishes 140 dashboards off the warehouse; the ones that matter to the CFO are the four that depend on the orders pipeline [stated — Dana Okafor].

- **Owner:** Priya Raman
- **Decision date:** target 2026-10-15
- **Evidence needed:** signed security review
- **Unknown:** who approves the final spend

### Technical Notes

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

- **Sources in scope:** Salesforce, Postgres, NetSuite, Zuora
- **Destination:** Snowflake (prod warehouse `ANALYTICS_WH`)
- **Sync cadence:** hourly, with a nightly full-refresh fallback
- **Open item:** marketing sources have no named owner

Wei's constraint is narrow but firm: no customer PII may transit a shared control plane. That is the reason the deployment-model question comes before pricing.

## Objections & Open Questions

The three items below are what actually moves this forward this week.

### New Objections / Concerns Surfaced

The team runs 38 sources into Snowflake and misses the 06:00 finance window roughly twice a week. Marcus traced it to a full refresh on two Postgres sources that no longer support CDC on their current version [stated — Marcus Feld].

- **Owner:** Priya Raman
- **Decision date:** target 2026-10-15
- **Evidence needed:** signed security review
- **Unknown:** who approves the final spend

Analytics is the loudest consumer, but the platform team owns the decision. Nothing in the current stack is contractually locked past December, which is why the evaluation is happening now rather than next year [inferred — timing of the renewal].

### Open Questions / Follow-Ups

- **What breaks today when the nightly load misses the 06:00 finance window?**
  *Listen for: named revenue or reporting impact, not general frustration.*
- **Who signs off if data leaves the VPC?**
  *Why this matters: names the real security approver early.*
- **What made you look at replacing the current stack this quarter?**
  *Listen for: a dated trigger event rather than a standing wish.*

## Actions & Next Step

What follows is the current read, with the unknowns called out explicitly.

### Action Items

Dana's team publishes 140 dashboards off the warehouse; the ones that matter to the CFO are the four that depend on the orders pipeline [stated — Dana Okafor].

- **Current stack:** Fivetran + dbt Cloud + Snowflake
- **Volume:** ==1.4 TB/day== peak, 38 sources
- **Deployment model:** Cloud with VPC peering
- **Contract renewal:** 2026-12-31
- **Unknown:** SLA for the marketing sources

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

### Next Step

- Send the security questionnaire response back to Wei · Owner: Devin Ortiz (AE) · Due: 2026-09-02 · Status: not started
- Book the 60-minute architecture review with Priya's staff · Owner: Dana Okafor · Due: 2026-09-09
- Confirm the Snowflake landing schema with the platform team · Owner: Marcus Feld · Due: 2026-09-04 · Status: in progress

## Coaching Observations

Wei's constraint is narrow but firm: no customer PII may transit a shared control plane. That is the reason the deployment-model question comes before pricing.

- **Sources in scope:** Salesforce, Postgres, NetSuite, Zuora
- **Destination:** Snowflake (prod warehouse `ANALYTICS_WH`)
- **Sync cadence:** hourly, with a nightly full-refresh fallback
- **Open item:** marketing sources have no named owner

The team runs 38 sources into Snowflake and misses the 06:00 finance window roughly twice a week. Marcus traced it to a full refresh on two Postgres sources that no longer support CDC on their current version [stated — Marcus Feld].

## Source Coverage

Every source below was read in full before this document was written.

- `_transcripts/Northwind-08.14.26.txt` — 412 / 412 lines read
- `outputs/biz-qual/2026-08-02-biz-qual.md` — full document
- `outputs/tech-qual/2026-08-11-tech-qual.md` — full document
- Salesforce opportunity record — stage, amount, close date
- Gong call `northwind-2026-08-14` — full transcript
- Product reference snapshot — 2026-08-18

_No source was partially read; nothing was inferred from a summary._
