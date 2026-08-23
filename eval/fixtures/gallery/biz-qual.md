# Business Qualification — Cascade Retail Group

**Date:** August 19, 2026 · **Account:** Cascade Retail Group · **Skill:** biz-qual

### Decision Summary
- **Verdict:** 🟡 viable with a dated security dependency
- **Stage:** technical validation
- **#1 Blocker:** security review is not booked
- **Recommended Motion:** get the architecture review on the calendar this week
- **Confidence:** 🟡 medium — budget line unconfirmed

**Jump to:** [MEDDPICC Scorecard](#meddpicc-scorecard) · [Qualification Narrative](#qualification-narrative) · [Movement & Deal Risks](#movement-&-deal-risks) · [Recommended Next Actions](#recommended-next-actions)

## MEDDPICC Scorecard

Analytics is the loudest consumer, but the platform team owns the decision. Nothing in the current stack is contractually locked past December, which is why the evaluation is happening now rather than next year [inferred — timing of the renewal].

- **Owner:** Priya Raman
- **Decision date:** target 2026-10-15
- **Evidence needed:** signed security review
- **Unknown:** who approves the final spend

## Qualification Narrative

What follows is the current read, with the unknowns called out explicitly.

### No Gap Without a Close Path

- **Security review not scheduled.** Wei has not seen an architecture diagram, and his team books three weeks out. This is the single dated blocker to a Q4 close.
- **Champion is the only advocate.** Marcus is doing the internal selling alone; if he is reassigned the deal stalls with no second sponsor.
- **Budget line is unconfirmed.** Priya described the funding as "probable" — no approved amount is on record yet.

> [!risk]
> The security review is the only dated dependency; everything else can run in parallel.

### Metrics

Wei's constraint is narrow but firm: no customer PII may transit a shared control plane. That is the reason the deployment-model question comes before pricing.

- **Owner:** Priya Raman
- **Decision date:** target 2026-10-15
- **Evidence needed:** signed security review
- **Unknown:** who approves the final spend

The team runs 38 sources into Snowflake and misses the 06:00 finance window roughly twice a week. Marcus traced it to a full refresh on two Postgres sources that no longer support CDC on their current version [stated — Marcus Feld].

### Economic Buyer

The team runs 38 sources into Snowflake and misses the 06:00 finance window roughly twice a week. Marcus traced it to a full refresh on two Postgres sources that no longer support CDC on their current version [stated — Marcus Feld].

- **Sources in scope:** Salesforce, Postgres, NetSuite, Zuora
- **Destination:** Snowflake (prod warehouse `ANALYTICS_WH`)
- **Sync cadence:** hourly, with a nightly full-refresh fallback
- **Open item:** marketing sources have no named owner

### Decision Criteria

- Confirm the Snowflake landing schema with the platform team · Owner: Marcus Feld · Due: 2026-09-04 · Status: in progress
- Send the security questionnaire response back to Wei · Owner: Devin Ortiz (AE) · Due: 2026-09-02 · Status: not started

- Rewrite the ingestion cutover runbook so the finance close window is never blocked by a full refresh, including the fallback path if the legacy Fivetran connectors have to stay online through the end of the fiscal quarter · Owner: Marcus Feld · Due: 2026-10-01 · Status: blocked

### Decision Process

- Send the security questionnaire response back to Wei · Owner: Devin Ortiz (AE) · Due: 2026-09-02 · Status: not started
- Book the 60-minute architecture review with Priya's staff · Owner: Dana Okafor · Due: 2026-09-09
- Confirm the Snowflake landing schema with the platform team · Owner: Marcus Feld · Due: 2026-09-04 · Status: in progress

### Paper Process

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

- **Sources in scope:** Salesforce, Postgres, NetSuite, Zuora
- **Destination:** Snowflake (prod warehouse `ANALYTICS_WH`)
- **Sync cadence:** hourly, with a nightly full-refresh fallback
- **Open item:** marketing sources have no named owner

Wei's constraint is narrow but firm: no customer PII may transit a shared control plane. That is the reason the deployment-model question comes before pricing.

### Identify Pain

Wei's constraint is narrow but firm: no customer PII may transit a shared control plane. That is the reason the deployment-model question comes before pricing.

- **Current stack:** Fivetran + dbt Cloud + Snowflake
- **Volume:** ==1.4 TB/day== peak, 38 sources
- **Deployment model:** Cloud with VPC peering
- **Contract renewal:** 2026-12-31
- **Unknown:** SLA for the marketing sources

### Champion

The team runs 38 sources into Snowflake and misses the 06:00 finance window roughly twice a week. Marcus traced it to a full refresh on two Postgres sources that no longer support CDC on their current version [stated — Marcus Feld].

- **Owner:** Priya Raman
- **Decision date:** target 2026-10-15
- **Evidence needed:** signed security review
- **Unknown:** who approves the final spend

Analytics is the loudest consumer, but the platform team owns the decision. Nothing in the current stack is contractually locked past December, which is why the evaluation is happening now rather than next year [inferred — timing of the renewal].

### Stakeholder Map

- **Priya Raman** — VP Data Platform — economic buyer, owns the 2027 budget line
- **Marcus Feld** — Staff Data Engineer — technical champion, ran the pilot
- **Dana Okafor** — Director of Analytics — day-to-day sponsor, reports to Priya
- **Wei Zhang** — Security Architect — must sign off on the deployment model

### Competition

Analytics is the loudest consumer, but the platform team owns the decision. Nothing in the current stack is contractually locked past December, which is why the evaluation is happening now rather than next year [inferred — timing of the renewal].

- **Sources in scope:** Salesforce, Postgres, NetSuite, Zuora
- **Destination:** Snowflake (prod warehouse `ANALYTICS_WH`)
- **Sync cadence:** hourly, with a nightly full-refresh fallback
- **Open item:** marketing sources have no named owner

## Movement & Deal Risks

The three items below are what actually moves this forward this week.

### Movement Since Last Qualification

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

- **Owner:** Priya Raman
- **Decision date:** target 2026-10-15
- **Evidence needed:** signed security review
- **Unknown:** who approves the final spend

### Deal Risks

- **Budget line is unconfirmed.** Priya described the funding as "probable" — no approved amount is on record yet.
- **Security review not scheduled.** Wei has not seen an architecture diagram, and his team books three weeks out. This is the single dated blocker to a Q4 close.

### Reasons to Walk or Deprioritize

The team runs 38 sources into Snowflake and misses the 06:00 finance window roughly twice a week. Marcus traced it to a full refresh on two Postgres sources that no longer support CDC on their current version [stated — Marcus Feld].

- **Current stack:** Fivetran + dbt Cloud + Snowflake
- **Volume:** ==1.4 TB/day== peak, 38 sources
- **Deployment model:** Cloud with VPC peering
- **Contract renewal:** 2026-12-31
- **Unknown:** SLA for the marketing sources

## Recommended Next Actions

- Send the security questionnaire response back to Wei · Owner: Devin Ortiz (AE) · Due: 2026-09-02 · Status: not started
- Book the 60-minute architecture review with Priya's staff · Owner: Dana Okafor · Due: 2026-09-09

- Rewrite the ingestion cutover runbook so the finance close window is never blocked by a full refresh, including the fallback path if the legacy Fivetran connectors have to stay online through the end of the fiscal quarter · Owner: Marcus Feld · Due: 2026-10-01 · Status: blocked

## Source Coverage

Every source below was read in full before this document was written.

- `_transcripts/Northwind-08.14.26.txt` — 412 / 412 lines read
- `outputs/biz-qual/2026-08-02-biz-qual.md` — full document
- `outputs/tech-qual/2026-08-11-tech-qual.md` — full document
- Salesforce opportunity record — stage, amount, close date
- Gong call `northwind-2026-08-14` — full transcript
- Product reference snapshot — 2026-08-18

_No source was partially read; nothing was inferred from a summary._
