# Mutual Close Plan — Halcyon Health

**Date:** August 19, 2026 · **Account:** Halcyon Health · **Skill:** mutual-close-plan

### Close Summary
- **Verdict:** 🟡 viable with a dated security dependency
- **Stage:** technical validation
- **#1 Blocker:** security review is not booked
- **Recommended Motion:** get the architecture review on the calendar this week
- **Confidence:** 🟡 medium — budget line unconfirmed

**Jump to:** [Path to Signature](#path-to-signature) · [Two-Sided Responsibilities](#two-sided-responsibilities) · [Critical Path & Risks](#critical-path-&-risks) · [Mutual Agreement Ask](#mutual-agreement-ask)

## Path to Signature

- Send the security questionnaire response back to Wei · Owner: Devin Ortiz (AE) · Due: 2026-09-02 · Status: not started
- Book the 60-minute architecture review with Priya's staff · Owner: Dana Okafor · Due: 2026-09-09
- Confirm the Snowflake landing schema with the platform team · Owner: Marcus Feld · Due: 2026-09-04 · Status: in progress

## Two-Sided Responsibilities

- **Priya Raman** — VP Data Platform — economic buyer, owns the 2027 budget line
- **Marcus Feld** — Staff Data Engineer — technical champion, ran the pilot
- **Dana Okafor** — Director of Analytics — day-to-day sponsor, reports to Priya
- **Wei Zhang** — Security Architect — must sign off on the deployment model

## Critical Path & Risks

- **Budget line is unconfirmed.** Priya described the funding as "probable" — no approved amount is on record yet.
- **Security review not scheduled.** Wei has not seen an architecture diagram, and his team books three weeks out. This is the single dated blocker to a Q4 close.

## Mutual Agreement Ask

No formal evaluation criteria exist yet. The team is comparing on reliability of the nightly window first and on cost second [inferred — how the team described the goal].

- **Current stack:** Fivetran + dbt Cloud + Snowflake
- **Volume:** ==1.4 TB/day== peak, 38 sources
- **Deployment model:** Cloud with VPC peering
- **Contract renewal:** 2026-12-31
- **Unknown:** SLA for the marketing sources

## Source Coverage

Every source below was read in full before this document was written.

- `_transcripts/Northwind-08.14.26.txt` — 412 / 412 lines read
- `outputs/biz-qual/2026-08-02-biz-qual.md` — full document
- `outputs/tech-qual/2026-08-11-tech-qual.md` — full document
- Salesforce opportunity record — stage, amount, close date
- Gong call `northwind-2026-08-14` — full transcript
- Product reference snapshot — 2026-08-18

_No source was partially read; nothing was inferred from a summary._
