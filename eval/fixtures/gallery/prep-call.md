# Call Prep: Northwind Analytics

**Date:** August 19, 2026 · **Call:** Thu, August 21, 2026 · 11:00am ET / 8:00am PT · **SE:** Sam Rivera

### Meeting Snapshot
- **Date:** August 19, 2026
- **Time:** 11:00am ET / 8:00am PT
- **Duration:** 30 min
- **Primary contact:** Dana Okafor — Director, Data Platform
- **Attendees:** Dana Okafor (Director, Data Platform), Marcus Feld (Staff Data Engineer), Devin Ortiz (AE)
- **Call objective:** Confirm whether the missed 06:00 finance close window is a connector problem or a warehouse problem, and get the security review booked
- **Key unknown:** who signs off on the deployment model

**Jump to:** [Account Context](#account-context) · [Call Strategy](#call-strategy) · [Discovery Plan](#discovery-plan) · [Agenda](#agenda) · [Watch Outs](#watch-outs) · [Desired Next Step](#desired-next-step) · [Source Coverage](#source-coverage)

## Account Context

### Company Snapshot
- **What they do:** Subscription analytics for mid-market retailers; the reporting layer is the product, not an internal tool [public — company site]
- **Industry:** B2B SaaS / retail analytics [SFDC]
- **Size:** ~420 employees, ~$60M ARR [SFDC]
- **Tech signals:** Snowflake, dbt, Fivetran on two Postgres sources, Airflow for orchestration [per AE call]
- **Recent news:** Series C in March 2026; hiring three data engineers [public — job postings]

### Why Airbyte
Based on their profile, the most likely reasons they are evaluating Airbyte:

- Pipeline maintenance is consuming engineering capacity that was hired for product work
- Two legacy Postgres sources no longer support CDC on their current version, forcing nightly full refreshes
- Deployment control matters more than price because customer PII transits the pipeline

### Prior Call Context
- **AE call date + duration:** August 14, 2026 · 38 min
- **Attendees on AE call:** Dana Okafor, Marcus Feld
- **Stated business pain:** the 06:00 finance close window is missed roughly twice a week [stated — Dana Okafor]
- **Stated forcing function / why now:** the FY close in October is the first one the CFO will run off this warehouse [stated — Dana Okafor]
- **Stated current stack (if mentioned):** 38 sources into Snowflake `ANALYTICS_WH`; Salesforce, Postgres, NetSuite, Zuora in scope
- **Open questions the AE flagged for the SE:** whether CDC is possible on the two legacy Postgres versions, and what the self-hosted footprint costs to run

### Open Threads From Prior Calls
- Most recent call: August 14, 2026
- Last stated next-step: Marcus to send the failing DAG logs — not yet received
- Topics that went quiet since: pricing, which Dana raised once and has not mentioned again
- Walking-it-back signals to address: none observed

## Call Strategy

### Point of View to Test

**They likely believe the close window is a warehouse performance problem (basis: Marcus profiled the warehouse first); we reframe to a full-refresh problem in the ingestion layer.**

Why this reframe for this customer: their own trace showed two Postgres sources doing nightly full refreshes inside the close window [stated — Marcus Feld]. If that holds, warehouse tuning cannot fix it and the fix is a connector change.

*Treat the CDC-version limit as a hypothesis to confirm live; the AE call did not establish the exact Postgres version.*

### Suggested Opener

> [!info] Suggested opener
> "We have 30 minutes. I want to confirm what is actually blowing the 06:00 window, and I would like to understand who has to bless the deployment model. By the end we should know whether a technical deep-dive is worth booking — or whether this is not a fit yet. Sound good?"

## Discovery Plan

### Must-Ask Questions

*✓ AE already covered: business pain, basic stack, forcing function. Don't re-ask these.*

- **"Marcus, when the 06:00 window is missed, walk me through what happens — who gets paged, how long is it down, and which reports go stale?"** — *why this matters:* separates the ingestion cost from the reporting cost. *Listen for:* whether the CFO's four dashboards are named.
- **"You traced it to a full refresh on two Postgres sources. What version are those on, and is CDC off because of the version or because of a permissions decision?"** — *why this matters:* decides whether this is a connector configuration change or a customer-side upgrade. *Listen for:* "we can't upgrade until Q1."
- **"Who has to approve the deployment model before a POC can start, and has that person seen a security questionnaire from a vendor like us before?"** — *why this matters:* this is the unknown that blocks every date we would propose. *Listen for:* a name, not a team.

### Implication-Depth Questions

**Pain Hypothesis 1: engineering time lost to pipeline maintenance**

- "How many hours a week does the team spend on failed syncs?" → expect 8–12
- "Who covers that, and what else stops while they do?" → expect Marcus, and the product roadmap
- Estimated cost framing: ==$95K/yr== of senior engineering capacity

**Pain Hypothesis 2: finance close credibility**

- "What happens on the days the CFO opens a stale dashboard?" → expect a manual reconciliation
- "How much of the close is manual today because of it?" → expect half a day

### Persona-Specific Questions

**For Dana Okafor (Director, Data Platform):**

- "If the close window were solved next month, what is the next thing your team would be judged on?"

**For Wei Chen (Security):**

- "What specifically has to be true about where the data plane runs for this to clear your review?"

## Agenda

### Suggested Agenda (30 min)

| Time | Topic |
|------|-------|
| 0–5 min | Intros, confirm agenda |
| 5–12 min | The close window — what actually breaks |
| 12–20 min | Postgres CDC and the connector path |
| 20–26 min | Deployment model and who approves it |
| 26–30 min | Next steps |

## Watch Outs

### Watch-Outs / Landmines
- **Fivetran is incumbent on the two Postgres sources.** A cutover story that ignores the fiscal-quarter freeze will not survive Marcus's review.
- **No customer PII may transit a shared control plane** [stated — Wei Chen]. Lead with deployment, not price.
- **Budget owner is unnamed.** Do not propose a paid POC until that is resolved.

## Desired Next Step

### Suggested Next Step
- **Meeting name:** Technical deep-dive — security & deployment model
- **Attendees needed:** Dana Okafor, Marcus Feld, Wei Chen (Security)
- **Proposed date:** within one week of this call
- **Agenda:** Postgres CDC path · self-hosted footprint · security questionnaire walkthrough · POC entry criteria
- **Pre-work:** SE sends the security questionnaire response; Marcus sends the failing DAG logs

## Source Coverage

Every source below was read in full before this document was written.

- `_transcripts/Northwind-08.14.26.txt` — 412 / 412 lines read
- Salesforce opportunity record — stage, amount, close date
- Product reference snapshot — 2026-08-18
- Public sources — company site, job postings (August 2026)

_No source was partially read; nothing was inferred from a summary._
