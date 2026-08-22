# Prep Call — Northwind Analytics

**Date:** August 19, 2026 · **Account:** Northwind Analytics · **Skill:** internal-prep

### Meeting / Decision Summary
- **Verdict:** 🟡 viable with a dated security dependency
- **Stage:** technical validation
- **#1 Blocker:** security review is not booked
- **Recommended Motion:** get the architecture review on the calendar this week
- **Confidence:** 🟡 medium — budget line unconfirmed

**Jump to:** [Relevant Deal Context](#relevant-deal-context) · [Alignment & Asks](#alignment-&-asks) · [Decisions Required](#decisions-required) · [Source Coverage](#source-coverage)

## Relevant Deal Context

Read the sub-sections in order; each one carries its own evidence.

### Deal-by-Deal Status

Dana's team publishes 140 dashboards off the warehouse; the ones that matter to the CFO are the four that depend on the orders pipeline [stated — Dana Okafor].

- **Sources in scope:** Salesforce, Postgres, NetSuite, Zuora
- **Destination:** Snowflake (prod warehouse `ANALYTICS_WH`)
- **Sync cadence:** hourly, with a nightly full-refresh fallback
- **Open item:** marketing sources have no named owner

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

## Alignment & Asks

The three items below are what actually moves this forward this week.

### Open Items Between Us

Wei's constraint is narrow but firm: no customer PII may transit a shared control plane. That is the reason the deployment-model question comes before pricing.

- **Owner:** Priya Raman
- **Decision date:** target 2026-10-15
- **Evidence needed:** signed security review
- **Unknown:** who approves the final spend

The team runs 38 sources into Snowflake and misses the 06:00 finance window roughly twice a week. Marcus traced it to a full refresh on two Postgres sources that no longer support CDC on their current version [stated — Marcus Feld].

## Decisions Required

What follows is the current read, with the unknowns called out explicitly.

### Decisions Needed This Sync

- Confirm the Snowflake landing schema with the platform team · Owner: Marcus Feld · Due: 2026-09-04 · Status: in progress
- Send the security questionnaire response back to Wei · Owner: Devin Ortiz (AE) · Due: 2026-09-02 · Status: not started

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
