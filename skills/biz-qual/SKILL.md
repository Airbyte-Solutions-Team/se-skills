---
name: biz-qual
description: Generates a business qualification document for a prospect using the MEDDPICC framework. Given company context or pasted notes, produces a structured qualification scorecard covering metrics, economic buyer, decision criteria, decision process, paper process, identify pain, champion, and competition. Use when the user says "biz qual", "business qualification", "qualify this deal", or wants to assess deal health.
---

# Business Qualification Skill

You are helping a Solutions Engineer at Airbyte assess whether an opportunity is well-qualified and worth advancing.

## Input

The user will provide one or more of:
- Company name and deal context
- Notes from calls, emails, or CRM
- Current deal stage

## Hard Prerequisite: Call Data Required

**This skill requires at least one customer transcript to run.** MEDDPICC scoring is synthesis of what the customer said — without a transcript, the output is hypothesis, not qualification.

Before doing anything else, check:
1. `~/airbyte-work/01-customers/_transcripts/` for files matching the customer
2. If none local, check Gong (14-day window for existing customer, 7-day for new prospect per `_se-playbook.md` Source Freshness Check)

**If zero transcripts exist in either location: REFUSE TO RUN.** Output:
> "Cannot generate biz-qual for [Customer] — zero transcripts available. MEDDPICC scoring requires customer voice, not hypotheses. Recommend: run `prep-call` to plan the first discovery call, then re-run `biz-qual` after the transcript is saved."

Do NOT produce a scorecard with all ⬜ Unknown rows. That output is worse than nothing — it can mislead future decisions.

## Framework

Use **MEDDPICC** as the qualification structure (Metrics, Economic buyer, Decision criteria, Decision process, Paper process, Identify pain, Champion, Competition). For each element, assess: confirmed, partial, or unknown — and flag gaps that need to be addressed.

## Output mode

Default = full MEDDPICC scorecard + all 8 letter sections + risks + next actions.

If user signals brief mode (`--brief`, `quick qual`, `qual summary`): produce just the MEDDPICC scorecard table + overall qualification score + top 3 gaps to close. Skip the per-letter prose sections, risk table, and movement comparison. See `_se-playbook.md` "Output Mode" for the unified brief-mode rule.

## Output Format

Document structure follows `_se-playbook.md` → Output Document Format (At-a-Glance + Jump-to index, H2-per-section, callouts, `==key==` emphasis).

---

# Business Qualification: [Company Name]
**Date:** [today's date] · **Deal stage:** [Discovery / Technical Eval / POC / Negotiation] · **SE owner:** [SE name]

### At a Glance
*Decision card — lead with the judgment (see `_se-playbook.md` → Decision-First Layout).*
- **Overall:** 🟢 Strong / 🟡 Moderate / 🔴 Weak — [3–6 word headline]
- **MEDDPICC:** [one-line scorecard, e.g. `M🟢 E🔴 D🟡 D🟡 P🔴 I🟢 C🟡 C🟢`]
- **Economic Buyer:** [name/title or "not identified"] · **Champion:** [name/title or "untested"]
- **Biggest gap:** [the weakest/blocking letter — one line — what it blocks]
- **Recommended motion:** [the one next move to close the biggest gap]
- **Source confidence:** [one line — N transcripts + SFDC; "see Source Coverage"]

**Jump to:** [At a Glance](#at-a-glance) · [Source Coverage](#source-coverage) · [MEDDPICC Scorecard](#meddpicc-scorecard) · [Metrics](#metrics) · [Economic Buyer](#economic-buyer) · [Decision Criteria](#decision-criteria) · [Decision Process](#decision-process) · [Paper Process](#paper-process) · [Identify Pain](#identify-pain) · [Champion](#champion) · [Competition](#competition) · [Movement Since Last Qualification](#movement-since-last-qualification) · [Deal Risks](#deal-risks) · [Recommended Next Actions](#recommended-next-actions)

---

## MEDDPICC Scorecard

*The Source column is your facts column (cite transcript date + speaker, or mark Unknown); Why it matters states the deal consequence.*

| Element | Status | Source (transcript date + speaker) | Why it matters |
|---------|--------|-------------------------------------|----------------|
| Metrics | 🟢 Confirmed / 🟡 Partial / 🔴 Unknown | | [consequence for the deal] |
| Economic Buyer | 🟢 / 🟡 / 🔴 | | |
| Decision Criteria | 🟢 / 🟡 / 🔴 | | |
| Decision Process | 🟢 / 🟡 / 🔴 | | |
| Paper Process | 🟢 / 🟡 / 🔴 | | |
| Identify Pain | 🟢 / 🟡 / 🔴 | | |
| Champion | 🟢 / 🟡 / 🔴 | | |
| Competition | 🟢 / 🟡 / 🔴 | | |

**Overall qualification score:** [Strong / Moderate / Weak]

Surface the weakest letters as callouts directly under the scorecard — `[!blocker]` for any 🔴 letter that blocks the deal, `[!risk]` for at-risk 🟡 letters that need confirmation:

```markdown
> [!blocker] Economic Buyer not identified
> No one with budget authority has engaged. Champion can't name who signs a deal this size. This gates everything downstream.

> [!risk] Paper Process unknown — end-of-quarter risk
> No InfoSec/legal timeline confirmed. New-vendor onboarding can run ==60–90 days==; if not started now, the close date slips.
```

### No gap without a close-path

Every 🔴/🟡 MEDDPICC element must produce a Next Actions row: `Gap → the specific ask that closes it → owner (or TBD) → by when`. A logged gap with no owned next step is incomplete. State score confidence: e.g. "Scored from 3 transcripts through 05.20; Economic Buyer unconfirmed — treat the EB line as [inferred], not fact."

---

## Metrics
**What business outcomes are they trying to achieve?**
- Quantified value: [e.g., "reduce pipeline build time from 3 weeks to 1 day", "consolidate 5 tools into 1"]
- KPIs they've mentioned: 
- ROI hypothesis: 

**Gaps / questions to resolve:**
- [ ] [e.g., Have they quantified the cost of their current approach?]

---

## Economic Buyer
**Who controls the budget and can say yes?**
- Name / title: 
- Engaged: [Yes / No / Indirectly]
- Their priorities: 
- Access plan: [How will we get in front of them?]

**Gaps / questions to resolve:**
- [ ] [e.g., Is the champion able to get EB time before end of quarter?]

---

## Decision Criteria
**What does "winning" look like to them?**
- Stated criteria: 
- Unstated / inferred criteria: 
- Technical must-haves: 
- Business must-haves: 
- Showstoppers / dealbreakers: 

**Gaps / questions to resolve:**
- [ ] [e.g., Have they formalized a scoring rubric or RFP?]

---

## Decision Process
**How will they make the decision?**
- Evaluation steps: 
- Key stakeholders involved: 
- Timeline to decision: 
- Decision-maker on POC outcome:

**Gaps / questions to resolve:**
- [ ] [e.g., Is there a formal RFP or vendor review committee?]

---

## Paper Process
**The legal/procurement/security steps between handshake and signed contract. Surface this early — surprises here kill end-of-quarter deals.**
- InfoSec / security review: [required? owner? typical duration?]
- Legal redline cycle: [DPA, MSA, order form — owner and timeline]
- Procurement / vendor onboarding: [required? owner? steps?]
- Architecture review board: [required? owner?]
- Known blockers: [e.g., new vendor onboarding can take 60-90 days]

**Gaps / questions to resolve:**
- [ ] [e.g., Has security questionnaire been requested? When can it start?]

---

## Identify Pain
**Apply the Sandler pain funnel — go three layers deep. If you can't fill layer 3, pain isn't fully identified.**

**Layer 1 — Surface complaint (what they said):**
[Direct quote or paraphrase from transcript]

**Layer 2 — Operational impact (what breaks because of it):**
[What downstream consequence does the surface problem cause? Quantify if possible.]

**Layer 3 — Personal stake (whose career or quarterly goal depends on it):**
[Who personally feels the pain? Whose number or initiative is on the line? This is where the budget lives.]

**Urgency drivers:** [What's forcing them to act now? Contract expiry, project deadline, exec mandate?]

**Gaps / questions to resolve:**
- [ ] [e.g., Have we identified WHO personally wins/loses if pain isn't solved?]
- [ ] [e.g., Is there a hard deadline tied to the pain?]

---

## Champion
**Who is selling Airbyte internally on our behalf?**
- Name / title: 
- Motivation: [Why do they personally win if Airbyte wins?]
- Access / influence: [Can they get us to EB, influence criteria, mobilize team?]
- Champion strength: [Strong champion / Coach only / Weak]

**Champion vs. Coach test:**
A real champion does all three:
- [ ] Takes meetings on short notice
- [ ] Shares internal context unprompted
- [ ] Gives you bad news, not just good news

If they only relay your messages, they're a coach — not a champion. Downgrade accordingly.

**Gaps / questions to resolve:**
- [ ] [e.g., Has the champion explicitly agreed to advocate internally?]

---

## Competition
**Who else are they considering — including the "do nothing" and "build it ourselves" options?**

| Alternative | Status | Notes |
|-------------|--------|-------|
| [e.g., Fivetran] | Active / On shortlist / Removed | |
| [e.g., Stitch / Matillion] | | |
| [e.g., Build internally / custom ETL] | | |
| [e.g., dbt + custom EL] | | |
| [e.g., Do nothing / run existing tool unsupported] | | |

**Off-the-list test:** "Who did you remove from the shortlist and why?" — the answer reveals their real decision criteria.

**Gaps / questions to resolve:**
- [ ] [e.g., Have we surfaced the "build internally" alternative with TCO numbers?]

---

## Movement Since Last Qualification
*If a prior biz-qual exists for this customer (check `~/airbyte-work/01-customers/<Customer>/outputs/biz-qual/biz-qual-*.md`), compare letter-by-letter:*

| Letter | Prior status | Current status | Trend |
|--------|--------------|----------------|-------|
| | | | ⬆️ Improved / ⬇️ Regressed / ➡️ No change |

Flag any letter that regressed (especially Champion, EB, Pain) — that's a *walking it back* signal per `_se-playbook.md` Cross-Transcript Analysis.

---

## Deal Risks
| Risk | Severity | Mitigation |
|------|----------|------------|
| [e.g., No EB access] | High | |
| [e.g., Competitor entrenched] | Medium | |
| [e.g., Budget not confirmed] | High | |

---

## Recommended Next Actions
*Action table — render `TBD` for Owner when the source doesn't state one; never invent a name. **Every 🔴/🟡 letter in the scorecard gets a row here** (no gap without an owned close-path); order by what unblocks the deal soonest.*

| # | Gap (MEDDPICC letter) | The ask that closes it | By when | Owner |
|---|-----------------------|------------------------|---------|-------|
| 1 | [weakest/blocking letter] | [the specific question or action that resolves it] | [date or **TBD**] | [name or **TBD**] |
| 2 | [next 🔴/🟡] | | | |
| 3 | [next 🔴/🟡] | | | |

---

## Style

- Direct about weak qualification — flag red flags clearly
- Use 🟢 🟡 🔴 status indicators consistently across all letters
- Extract qualification signals from raw notes; map them to MEDDPICC elements with source citations (transcript date + speaker)
- Ask the user to confirm or correct any inferred claims
- Concise — the scorecard table is the centerpiece; per-letter prose supports it, not the other way around

---

## SE Best Practices Applied to Business Qualification

Read `~/.claude/skills/_se-playbook.md` for full MEDDPICC + Sandler details.

### Salesforce Enrichment (pre-fills MEDDPICC scaffold — AE view vs SE view)
Per `_se-playbook.md` "Salesforce Enrichment." Pull the **active opp** MEDDPICC fields + **account arc**. Fields:
- `Economic_Buyer__c`, `Decision_Maker__c` (→ E), `Champion__c` (→ Champion), `Identify_Pain__c` (→ I), `Decision_Process__c` (→ Decision Process), `Primary_Competitor__c` + `Gong__MainCompetitors__c` + `Fivetran_competitive__c` (→ Competition), `Required_features_functionality__c` (→ Decision Criteria), `Amount`/`ARR__c` (→ Metrics anchor), `CloseDate` (→ forcing function), `Why_buy_*__c` (→ Driver/Urgency context)
- Account arc: existing ARR (expansion vs net-new), prior losses

**How to use it:** Pre-populate the MEDDPICC scorecard with the AE's SFDC entries, **marked "(AE-entered, unverified)"**. Then score against the transcripts as a separate column. The output should show **two views side by side: AE (from SFDC) | SE (from transcripts)**. The disagreements are the qualification gaps — flag them assertively. Example: SFDC `Champion__c` = "Michel" but transcripts show Michel hasn't engaged in weeks → Champion is 🟡 Coach in the SE column even though AE marked him Champion.

If SFDC unavailable, skip per graceful-degradation and score from transcripts only.

### Source Freshness Check (Gong Fallback)
Per `_se-playbook.md` ("Source Freshness Check"): if the most-recent local transcript is more than **14 days old**, search Gong for newer calls before scoring MEDDPICC.
- Pull the **most recent call only** — do not bulk-pull
- Save to `_transcripts/<Customer-Name>-MM.DD.YY.txt` BEFORE using it (per CLAUDE.md)
- A MEDDPICC scorecard based on stale data is worse than no scorecard

### Apply Cross-Transcript Analysis
MEDDPICC scoring is only as good as the source coverage behind it. If multiple transcripts/notes exist, read the "Cross-Transcript Analysis" section in `_se-playbook.md`. Specifically:
- Score each MEDDPICC letter using evidence from ALL available calls, not just the latest
- When a letter's status changed over time (e.g., Champion was 🟢 in March, 🟡 now), call that out as a *walking it back* signal in the notes column
- Topics that went quiet often indicate a letter regressing — flag explicitly
- Cite source (transcript date + speaker) for each material claim in the scorecard

### Why MEDDPICC (not just MEDDIC)
The two extra letters do real work:
- **P — Paper Process**: legal, security review, procurement steps. Surprises here kill end-of-quarter deals.
- **C — Competition**: who else is on the shortlist, who got removed, and why. Always include "build it ourselves" and "do nothing" as competitors.

Both are required rows in the scorecard.

### Test the Champion (don't assume)
A real champion (a) takes meetings on short notice, (b) shares internal context unprompted, (c) gives you bad news. In the Champion section, score against these three. If they only relay messages, downgrade to "coach" — not champion.

### Confirm Economic Buyer by behavior, not title
For the EB section, don't accept "VP of Data Engineering" at face value. The qualifier is: "Has the champion confirmed this person can sign a deal of our expected size without escalation?" If not, EB status is Unknown.

### Anchor Metrics in their numbers, not yours
The Metrics section is the most-skipped MEDDPICC letter. Don't write generic "reduce pipeline maintenance time." Write specific quantified claims tied to *their* business: "Reduce 10 hrs/wk of pipeline maintenance × loaded DE cost of $80/hr = ~$40K/year reclaimed capacity." If we don't have the customer's numbers, mark Metrics as Unknown.

### Apply Sandler pain funnel to Identify Pain section
"Primary pain" should not be one sentence. Three layers:
1. Surface complaint (what they said)
2. Operational impact (what breaks because of it)
3. Personal stake (whose career or quarterly goal depends on it)

If you can't fill layer 3, pain isn't fully identified.

### Anti-patterns to avoid in this skill
- All-green scorecard on a deal with no confirmed Metrics or EB (happy-ears qualification)
- Pain section that's a feature wish-list, not actual pain
- Decision Process that's vague ("they'll evaluate vendors") instead of named steps with owners and dates
- Listing Airbyte features as "decision criteria" — those are *our* criteria, not theirs

---

## After Generating

### Auto-save (default)

Per `_se-playbook.md` "Output Persistence (Auto-Save)" rule, save to:
```
~/airbyte-work/01-customers/<Customer>/outputs/biz-qual/biz-qual-<YYYY-MM-DD>-<Descriptor>.md
```

Filename example: `biz-qual-2026-05-28-Post-Tech-Call.md`. Create folders if missing. Append `-v2` etc. for same-day duplicates. User can suppress with `--no-save`.

Per `_se-playbook.md` "Date format inside documents", write dates in the document body — the H1/title line especially, plus headers and prose — in long form (`June 11, 2026`), not the numeric `2026-06-11`. The numeric `YYYY-MM-DD` stays only in the filename.

### Source Coverage

Include a Source Coverage section at the top reporting transcripts read (with line counts), prior biz-qual docs (for movement comparison), memory records, and any other inputs.

### SE Identity

Read `~/airbyte-work/.se-config.yaml` for the `[SE name]` field.

### Then ask which other artifacts to update

1. **Mirror to Notion** under the customer's parent page
2. **Update memory** — if MEDDPICC scoring surfaced a material change (Champion confirmed, EB identified, deal-killer competitor discovered, etc.), propose adding/updating a project memory. Don't update for incremental progress — only material status shifts.

Wait for explicit yes/no on Notion / memory before doing those.

---

## Changelog

- **2026-07-09** — Fixed the "Movement Since Last Qualification" read path: prior biz-qual is read from `outputs/biz-qual/biz-qual-*.md` (was the customer root, where it's never saved) so the letter-by-letter comparison actually finds the prior scorecard.
- **2026-07-09** — Every 🔴/🟡 element now requires a paired, owned close-path in Next Actions (`Gap → ask that closes it → by when → owner/TBD`); added explicit score-confidence + `[inferred]` labeling for unconfirmed elements. Next Actions table restructured to make the gap→close-path pairing explicit.
- **2026-06-18** — Output adopts the shared Output Document Format (_se-playbook.md): At-a-Glance + Jump-to index, H2-per-section, callouts, ==key== emphasis.

- **2026-05-28** — Salesforce enrichment added (reads from sf-mcp via mcp__salesforce__run_soql_query). Pulls AE-view MEDDPICC / technical / forecast fields per the playbook field map; assertive SFDC-vs-reality mismatch flagging; graceful degradation if SFDC disabled. Org alias + query dir from .se-config.yaml.

- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Hard prerequisite: refuses to run without transcripts. Scorecard table now MEDDPICC (8 letters) with Source column. Paper Process + Competition sections added. Identify Pain restructured to 3-layer Sandler pain funnel. Champion section with 3-criterion test. Movement Since Last Qualification section. After Generating with memory-write proposal. Style section normalized. [SE name] placeholder.
- **2026-05-27** — Initial scaffold (was MEDDIC).
