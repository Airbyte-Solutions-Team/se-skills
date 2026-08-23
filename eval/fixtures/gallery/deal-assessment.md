# Deal Assessment — Northwind Analytics

**Date:** August 19, 2026 · **Account:** Northwind Analytics · **Skill:** deal-assessment

### Decision Summary / Bottom Line
- **Verdict:** 🟡 viable with a dated security dependency
- **Stage:** technical validation
- **#1 Blocker:** security review is not booked
- **Recommended Motion:** get the architecture review on the calendar this week
- **Confidence:** 🟡 medium — budget line unconfirmed

**Jump to:** [Trajectory & What Changed](#trajectory-&-what-changed) · [Deal Thesis](#deal-thesis) · [Stakeholders & Qualification](#stakeholders-&-qualification) · [Close Path Blockers & Loss Risks](#close-path-blockers-&-loss-risks)

## Trajectory & What Changed

Read the sub-sections in order; each one carries its own evidence.

### Activity Trajectory

Dana's team publishes 140 dashboards off the warehouse; the ones that matter to the CFO are the four that depend on the orders pipeline [stated — Dana Okafor].

- **Sources in scope:** Salesforce, Postgres, NetSuite, Zuora
- **Destination:** Snowflake (prod warehouse `ANALYTICS_WH`)
- **Sync cadence:** hourly, with a nightly full-refresh fallback
- **Open item:** marketing sources have no named owner

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

### What Changed Since Last Assessment

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

- **Current stack:** Fivetran + dbt Cloud + Snowflake
- **Volume:** ==1.4 TB/day== peak, 38 sources
- **Deployment model:** Cloud with VPC peering
- **Contract renewal:** 2026-12-31
- **Unknown:** SLA for the marketing sources

## Deal Thesis

Read the sub-sections in order; each one carries its own evidence.

### Driver

The team runs 38 sources into Snowflake and misses the 06:00 finance window roughly twice a week. Marcus traced it to a full refresh on two Postgres sources that no longer support CDC on their current version [stated — Marcus Feld].

- **Sources in scope:** Salesforce, Postgres, NetSuite, Zuora
- **Destination:** Snowflake (prod warehouse `ANALYTICS_WH`)
- **Sync cadence:** hourly, with a nightly full-refresh fallback
- **Open item:** marketing sources have no named owner

### Need

Analytics is the loudest consumer, but the platform team owns the decision. Nothing in the current stack is contractually locked past December, which is why the evaluation is happening now rather than next year [inferred — timing of the renewal].

- **Current stack:** Fivetran + dbt Cloud + Snowflake
- **Volume:** ==1.4 TB/day== peak, 38 sources
- **Deployment model:** Cloud with VPC peering
- **Contract renewal:** 2026-12-31
- **Unknown:** SLA for the marketing sources

Dana's team publishes 140 dashboards off the warehouse; the ones that matter to the CFO are the four that depend on the orders pipeline [stated — Dana Okafor].

### Urgency

Dana's team publishes 140 dashboards off the warehouse; the ones that matter to the CFO are the four that depend on the orders pipeline [stated — Dana Okafor].

- **Owner:** Priya Raman
- **Decision date:** target 2026-10-15
- **Evidence needed:** signed security review
- **Unknown:** who approves the final spend

## Stakeholders & Qualification

What follows is the current read, with the unknowns called out explicitly.

### Stakeholder Read

- **Priya Raman** — VP Data Platform — economic buyer, owns the 2027 budget line
- **Marcus Feld** — Staff Data Engineer — technical champion, ran the pilot
- **Dana Okafor** — Director of Analytics — day-to-day sponsor, reports to Priya
- **Wei Zhang** — Security Architect — must sign off on the deployment model

## Close Path Blockers & Loss Risks

The three items below are what actually moves this forward this week.

### What Would Close It

- **Champion is the only advocate.** Marcus is doing the internal selling alone; if he is reassigned the deal stalls with no second sponsor.
- **Budget line is unconfirmed.** Priya described the funding as "probable" — no approved amount is on record yet.

### Deal Blocker

- **Budget line is unconfirmed.** Priya described the funding as "probable" — no approved amount is on record yet.
- **Security review not scheduled.** Wei has not seen an architecture diagram, and his team books three weeks out. This is the single dated blocker to a Q4 close.
- **Champion is the only advocate.** Marcus is doing the internal selling alone; if he is reassigned the deal stalls with no second sponsor.

### Loss Risks

- **Security review not scheduled.** Wei has not seen an architecture diagram, and his team books three weeks out. This is the single dated blocker to a Q4 close.
- **Champion is the only advocate.** Marcus is doing the internal selling alone; if he is reassigned the deal stalls with no second sponsor.

> [!risk]
> The security review is the only dated dependency; everything else can run in parallel.

## Recommended Actions & Coaching

Read the sub-sections in order; each one carries its own evidence.

### Coaching Observations

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
