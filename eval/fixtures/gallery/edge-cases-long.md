# Deal Assessment — Consolidated Global Data Platform Modernization and Ingestion Replacement Programme for Meridian Transcontinental Logistics Holdings (EMEA + APAC), Fiscal Year 2027

**Date:** August 19, 2026 · **Account:** Meridian Transcontinental Logistics Holdings · **Opportunity:** FY27 Global Data Platform Modernization — Phase 2 (Ingestion Replacement, EMEA + APAC, including the Singapore and Rotterdam regional warehouses) · **Skill:** deal-assessment

### Decision Summary
- **Close Probability:** 🟡 40–60% — two named blockers, both dated
- **Stage:** technical validation
- **#1 Blocker:** the regional security review for APAC has no booked date and the reviewer is on leave until the end of the month
- **Recommended Motion:** trade the APAC scope out of Phase 2 and close EMEA on the current timeline
- **Key Players:** Priya Raman (EB), Marcus Feld (champion), Wei Zhang (security)
- **Last Touch:** 2026-08-14 — architecture working session
- **Confidence:** 🔴 low — no written budget confirmation
- **Unknown:** whether the APAC entity has its own signing authority

## Deal Thesis

**If the EMEA ingestion cutover lands before the December renewal, the incumbent contract lapses and the remaining decision becomes an internal one.** Everything else in this deal is downstream of that date. The APAC scope adds revenue but also adds a second security reviewer, a second legal entity, and a third procurement queue.

*Supporting evidence: renewal date confirmed in the MSA; APAC entity structure described verbally only.*

### Driver
The incumbent contract lapses on 2026-12-31 and the platform team does not want to auto-renew [stated — Priya Raman].

### Need
Reliable CDC on the two Postgres order systems, plus a NetSuite path that survives custom-record schema drift.

### Urgency
The finance close window is missed roughly twice every ten business days, and the CFO has started asking about it directly.

## Stakeholders & Qualification

- **Alexandra Constantinescu-Whitfield** — Interim Global Head of Data Platform Engineering and Analytics Enablement (EMEA + APAC), joined four weeks ago and is still forming a point of view on whether ingestion should be bought or built in-house
- **Priya Raman** — VP Data Platform, economic buyer, owns the 2027 budget line
- **Marcus Feld** — Staff Data Engineer, technical champion, ran the pilot
- **Wei Zhang** — Security Architect, must sign off on the deployment model, on leave until 2026-08-31
- **Bo Lin** — Procurement Lead, controls the MSA redlines

### Stakeholder Read
Coverage is thin above Priya. Nobody in the account has met the CFO, and the only written artifact from the EB is a calendar invite.

## Close Path Blockers & Loss Risks

- **APAC security review is unbooked.** The reviewer is on leave until 2026-08-31 and his team books three weeks out, which puts a signed review past the Phase 2 date.
- **Champion is the only advocate.** Marcus is doing the internal selling alone; if he is reassigned the deal stalls with no second sponsor.
- **Budget line is unconfirmed.** Priya described the funding as "probable" — no approved amount is on record.

> [!blocker]
> No signed security review means no Phase 2 close, regardless of commercial progress.

> [!risk]
> The interim platform lead has an open build-vs-buy question and has not been engaged directly.

### What Would Close It
Trade APAC out of Phase 2, get the EMEA review booked this week, and put the interim platform lead in front of the reference architecture.

## Timeline & Milestones

| Workstream | Owner | Start | End | Dependency | Exit evidence | Escalation path | Regional variance | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EMEA landing schema sign-off | Marcus Feld | 2026-09-01 | 2026-09-05 | Snowflake role grants | Written approval in the shared doc | Dana → Priya | None | Blocked on the finance close calendar |
| EMEA security architecture review | Wei Zhang | 2026-09-08 | 2026-09-26 | Architecture diagram | Completed questionnaire and review notes | Wei → CISO delegate | EMEA only | Three-week booking lead time |
| APAC security architecture review | Unassigned | Unknown | Unknown | Regional reviewer availability | Not yet defined | Unknown | APAC only | Reviewer on leave until 2026-08-31 |
| Commercial redlines | Bo Lin | 2026-09-15 | 2026-10-10 | MSA draft | Countersigned redline set | Bo → General Counsel | Both | Procurement freeze in the final week of the quarter |

## Recommended Actions & Coaching

- Rewrite the ingestion cutover runbook so the finance close window is never blocked by a full refresh, including the fallback path if the legacy connectors have to stay online through the end of the fiscal quarter · Owner: Marcus Feld · Due: 2026-10-01 · Status: blocked
- Book the EMEA architecture review · Owner: Devin Ortiz (AE) · Due: 2026-08-22 · Status: not started
- Get a written budget confirmation from Priya · Owner: Devin Ortiz (AE) · Due: 2026-09-05
- Introduce the interim platform lead to the reference architecture · Owner: Dana Okafor · Due: 2026-09-12 · Status: in progress

### Coaching Observations
Two of the last three calls ended without a next step in the calendar. The deal is running on the champion's goodwill rather than on a mutual plan.

## Source Coverage

Every source below was read in full before this document was written.

- `_transcripts/Meridian-08.14.26.txt` — 918 / 918 lines read
- `outputs/biz-qual/2026-07-28-biz-qual.md` — full document
- `outputs/tech-qual/2026-08-04-tech-qual.md` — full document
- `outputs/deployment-model-qual/2026-08-11-deployment-model-qual.md` — full document
- `outputs/connector-feasibility/2026-08-12-connector-feasibility.md` — full document
- Salesforce opportunity record — stage, amount, close date, last activity
- Gong call `meridian-2026-08-14` — full transcript
- Gong call `meridian-2026-07-30` — full transcript
- Product reference snapshot — 2026-08-18
- MSA draft v4 — renewal and termination clauses only

_No source was partially read; nothing was inferred from a summary._
