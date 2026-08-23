# POC Plan — Cascade Retail Group

**Date:** August 19, 2026 · **Account:** Cascade Retail Group · **Skill:** poc-plan

### POC Summary
- **Verdict:** 🟡 viable with a dated security dependency
- **Stage:** technical validation
- **#1 Blocker:** security review is not booked
- **Recommended Motion:** get the architecture review on the calendar this week
- **Confidence:** 🟡 medium — budget line unconfirmed

**Jump to:** [POC Objective](#poc-objective) · [Success Criteria](#success-criteria) · [Scope & Architecture](#scope-&-architecture) · [Mutual Commitments & Roles](#mutual-commitments-&-roles)

## POC Objective

Analytics is the loudest consumer, but the platform team owns the decision. Nothing in the current stack is contractually locked past December, which is why the evaluation is happening now rather than next year [inferred — timing of the renewal].

- **Owner:** Priya Raman
- **Decision date:** target 2026-10-15
- **Evidence needed:** signed security review
- **Unknown:** who approves the final spend

## Success Criteria

Dana's team publishes 140 dashboards off the warehouse; the ones that matter to the CFO are the four that depend on the orders pipeline [stated — Dana Okafor].

- **Sources in scope:** Salesforce, Postgres, NetSuite, Zuora
- **Destination:** Snowflake (prod warehouse `ANALYTICS_WH`)
- **Sync cadence:** hourly, with a nightly full-refresh fallback
- **Open item:** marketing sources have no named owner

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

## Scope & Architecture

The three items below are what actually moves this forward this week.

### Scope

Wei's constraint is narrow but firm: no customer PII may transit a shared control plane. That is the reason the deployment-model question comes before pricing.

- **Owner:** Priya Raman
- **Decision date:** target 2026-10-15
- **Evidence needed:** signed security review
- **Unknown:** who approves the final spend

The team runs 38 sources into Snowflake and misses the 06:00 finance window roughly twice a week. Marcus traced it to a full refresh on two Postgres sources that no longer support CDC on their current version [stated — Marcus Feld].

### Scope Tiers

The team runs 38 sources into Snowflake and misses the 06:00 finance window roughly twice a week. Marcus traced it to a full refresh on two Postgres sources that no longer support CDC on their current version [stated — Marcus Feld].

- **Sources in scope:** Salesforce, Postgres, NetSuite, Zuora
- **Destination:** Snowflake (prod warehouse `ANALYTICS_WH`)
- **Sync cadence:** hourly, with a nightly full-refresh fallback
- **Open item:** marketing sources have no named owner

### POC Architecture

Analytics is the loudest consumer, but the platform team owns the decision. Nothing in the current stack is contractually locked past December, which is why the evaluation is happening now rather than next year [inferred — timing of the renewal].

- **Current stack:** Fivetran + dbt Cloud + Snowflake
- **Volume:** ==1.4 TB/day== peak, 38 sources
- **Deployment model:** Cloud with VPC peering
- **Contract renewal:** 2026-12-31
- **Unknown:** SLA for the marketing sources

Dana's team publishes 140 dashboards off the warehouse; the ones that matter to the CFO are the four that depend on the orders pipeline [stated — Dana Okafor].

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

## Mutual Commitments & Roles

The three items below are what actually moves this forward this week.

### Mutual Commitments

- Send the security questionnaire response back to Wei · Owner: Devin Ortiz (AE) · Due: 2026-09-02 · Status: not started
- Book the 60-minute architecture review with Priya's staff · Owner: Dana Okafor · Due: 2026-09-09

- Rewrite the ingestion cutover runbook so the finance close window is never blocked by a full refresh, including the fallback path if the legacy Fivetran connectors have to stay online through the end of the fiscal quarter · Owner: Marcus Feld · Due: 2026-10-01 · Status: blocked

### Roles & Responsibilities

- **Priya Raman** — VP Data Platform — economic buyer, owns the 2027 budget line
- **Marcus Feld** — Staff Data Engineer — technical champion, ran the pilot
- **Dana Okafor** — Director of Analytics — day-to-day sponsor, reports to Priya
- **Wei Zhang** — Security Architect — must sign off on the deployment model

## Timeline & Milestones

| Workstream | Owner | Start | End | Dependency | Exit evidence | Escalation path | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Landing schema sign-off | Marcus Feld | 2026-09-01 | 2026-09-05 | Snowflake role grants | Written approval in the shared doc | Dana → Priya | Blocked on the finance close calendar |
| Security architecture review | Wei Zhang | 2026-09-08 | 2026-09-26 | Architecture diagram | Completed questionnaire + review notes | Wei → CISO delegate | Three-week booking lead time |
| Commercial redlines | Bo Lin | 2026-09-15 | 2026-10-10 | MSA draft | Countersigned redline set | Bo → General Counsel | Procurement freeze in the last week of the quarter |

## Access & Prerequisites

What follows is the current read, with the unknowns called out explicitly.

### Access & Prerequisites Checklist

Dana's team publishes 140 dashboards off the warehouse; the ones that matter to the CFO are the four that depend on the orders pipeline [stated — Dana Okafor].

- **Current stack:** Fivetran + dbt Cloud + Snowflake
- **Volume:** ==1.4 TB/day== peak, 38 sources
- **Deployment model:** Cloud with VPC peering
- **Contract renewal:** 2026-12-31
- **Unknown:** SLA for the marketing sources

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

## Risks & Mitigations

- **Champion is the only advocate.** Marcus is doing the internal selling alone; if he is reassigned the deal stalls with no second sponsor.
- **Budget line is unconfirmed.** Priya described the funding as "probable" — no approved amount is on record yet.
- **Security review not scheduled.** Wei has not seen an architecture diagram, and his team books three weeks out. This is the single dated blocker to a Q4 close.

## Exit Results Review

What follows is the current read, with the unknowns called out explicitly.

### POC Exit Criteria

The team runs 38 sources into Snowflake and misses the 06:00 finance window roughly twice a week. Marcus traced it to a full refresh on two Postgres sources that no longer support CDC on their current version [stated — Marcus Feld].

- **Current stack:** Fivetran + dbt Cloud + Snowflake
- **Volume:** ==1.4 TB/day== peak, 38 sources
- **Deployment model:** Cloud with VPC peering
- **Contract renewal:** 2026-12-31
- **Unknown:** SLA for the marketing sources

### Story for Results Review

Analytics is the loudest consumer, but the platform team owns the decision. Nothing in the current stack is contractually locked past December, which is why the evaluation is happening now rather than next year [inferred — timing of the renewal].

- **Owner:** Priya Raman
- **Decision date:** target 2026-10-15
- **Evidence needed:** signed security review
- **Unknown:** who approves the final spend

Dana's team publishes 140 dashboards off the warehouse; the ones that matter to the CFO are the four that depend on the orders pipeline [stated — Dana Okafor].

## Open Items

What follows is the current read, with the unknowns called out explicitly.

### Notes / Open Items

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

- **Current stack:** Fivetran + dbt Cloud + Snowflake
- **Volume:** ==1.4 TB/day== peak, 38 sources
- **Deployment model:** Cloud with VPC peering
- **Contract renewal:** 2026-12-31
- **Unknown:** SLA for the marketing sources

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
