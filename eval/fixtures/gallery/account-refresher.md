# Account Refresher — Cascade Retail Group

**Date:** August 19, 2026 · **Account:** Cascade Retail Group · **Skill:** account-refresher

### Account Snapshot
- **Verdict:** 🟡 viable with a dated security dependency
- **Stage:** technical validation
- **#1 Blocker:** security review is not booked
- **Recommended Motion:** get the architecture review on the calendar this week
- **Confidence:** 🟡 medium — budget line unconfirmed

**Jump to:** [Who's Who](#who's-who) · [Story So Far](#story-so-far) · [Current State](#current-state) · [Open Items](#open-items)

## Who's Who

- **Priya Raman** — VP Data Platform — economic buyer, owns the 2027 budget line
- **Marcus Feld** — Staff Data Engineer — technical champion, ran the pilot
- **Dana Okafor** — Director of Analytics — day-to-day sponsor, reports to Priya
- **Wei Zhang** — Security Architect — must sign off on the deployment model

## Story So Far

Analytics is the loudest consumer, but the platform team owns the decision. Nothing in the current stack is contractually locked past December, which is why the evaluation is happening now rather than next year [inferred — timing of the renewal].

- **Owner:** Priya Raman
- **Decision date:** target 2026-10-15
- **Evidence needed:** signed security review
- **Unknown:** who approves the final spend

## Current State

Dana's team publishes 140 dashboards off the warehouse; the ones that matter to the CFO are the four that depend on the orders pipeline [stated — Dana Okafor].

- **Sources in scope:** Salesforce, Postgres, NetSuite, Zuora
- **Destination:** Snowflake (prod warehouse `ANALYTICS_WH`)
- **Sync cadence:** hourly, with a nightly full-refresh fallback
- **Open item:** marketing sources have no named owner

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

## Open Items

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

- **Current stack:** Fivetran + dbt Cloud + Snowflake
- **Volume:** ==1.4 TB/day== peak, 38 sources
- **Deployment model:** Cloud with VPC peering
- **Contract renewal:** 2026-12-31
- **Unknown:** SLA for the marketing sources

## Watch Outs

- **Champion is the only advocate.** Marcus is doing the internal selling alone; if he is reassigned the deal stalls with no second sponsor.
- **Budget line is unconfirmed.** Priya described the funding as "probable" — no approved amount is on record yet.

## Source Coverage

Every source below was read in full before this document was written.

- `_transcripts/Northwind-08.14.26.txt` — 412 / 412 lines read
- `outputs/biz-qual/2026-08-02-biz-qual.md` — full document
- `outputs/tech-qual/2026-08-11-tech-qual.md` — full document
- Salesforce opportunity record — stage, amount, close date
- Gong call `northwind-2026-08-14` — full transcript
- Product reference snapshot — 2026-08-18

_No source was partially read; nothing was inferred from a summary._
