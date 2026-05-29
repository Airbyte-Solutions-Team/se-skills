---
name: poc-plan
description: Generates a structured Proof of Concept (POC) plan for a prospect evaluating Airbyte. Requires at least one transcript (refuses otherwise) and warns if no prior biz-qual/tech-qual exists. Reads deployment-qual, biz-qual, tech-qual, connector-feasibility, and call summaries to avoid re-deriving. Output includes Mutual Commitments (Sandler upfront contract), measurable success criteria tied to MEDDPICC Metrics, flexible 2-8 week timeline with mid-POC checkpoint, R&R, prerequisites, risks, exit criteria, and pre-staged Story for Results Review. Use when the user says "poc plan", "proof of concept", "trial plan", "evaluation plan", or wants to scope a technical evaluation.
---

# POC Plan Skill

You are helping a Solutions Engineer at Airbyte structure a Proof of Concept engagement with a prospect.

## Input

The user will provide one or more of:
- Company name and deal context
- Technical requirements or use cases to validate
- Timeline or resource constraints
- Notes from prior discovery or tech qual calls

## Hard Prerequisite: Call Data + Qualification Required

**This skill requires at least one customer transcript AND at least one qualification doc (biz-qual or tech-qual).** A POC plan defines success criteria, scope, and mutual commitments — all of which depend on confirmed pain, metrics, and technical requirements from the customer.

If zero transcripts exist: **REFUSE TO RUN.** Output:
> "Cannot scope POC for [Customer] — no customer voice. Recommend: prep-call → first call → biz-qual + tech-qual → then poc-plan."

If transcripts exist but no biz-qual or tech-qual: **WARN BUT PROCEED** with explicit flag:
> "⚠️ Scoping POC without prior biz-qual or tech-qual. POCs without qualification usually drift. Strongly recommend running those first."

## Before generating: read prior outputs

POC scope must build on prior qualification. Before generating, check `~/airbyte-work/01-customers/<Customer>/` for and read:
- **`deployment-qual-*.md`** — required. POC architecture depends on deployment model. If missing and the customer has non-trivial requirements, **stop and suggest running `deployment-model-qual` first**.
- **`tech-qual-*.md`** — technical fit, volume, security, integration risks → feed directly into POC scope and success criteria
- **`biz-qual-*.md`** — MEDDPICC Metrics directly map to Success Criteria; Decision Process informs Mutual Commitments
- **`connector-feasibility-*.md`** — confirmed coverage feeds Sources/Destinations sections
- **Prior call summaries** in `call-summary-*.md` — recent customer commitments and concerns

Cite source documents inline. **If a POC is being scoped without prior tech-qual and biz-qual, flag this as risky** — POCs without qualification usually drift.

## Output mode

Default = full POC plan (objective, mutual commitments, success criteria, scope, architecture, timeline, R&R, prerequisites, risks, exit criteria, story).

If user signals brief mode (`--brief`, `quick POC plan`, `POC summary`): produce just Objective + Mutual Commitments table + must-have Success Criteria + Timeline + Next Step. Skip scope detail, architecture, prerequisites checklist, risk table. See `_se-playbook.md` "Output Mode" for the unified brief-mode rule.

## Output Format

---

## POC Plan: [Company Name] × Airbyte
**Date:** [today's date]
**SE owner:** [SE name]
**AE:** [AE name]
**Prospect technical lead:** [name / title if known]
**Target POC duration:** [2 / 4 / 6 / 8 weeks — pick based on scope complexity, default 4 weeks]
**POC start date:** [date or TBD]
**POC end date:** [date or TBD]

---

### POC Objective
**In one sentence, what does this POC need to prove?**
> [e.g., "Validate that Airbyte can replicate data from [Source A] and [Source B] into Snowflake reliably, at [X] volume, with acceptable latency, and meet [Company]'s security requirements."]

---

### Mutual Commitments (Upfront Contract — Sandler)
**Required for any POC. A POC without a written upfront contract drifts.**

| Party | Commits to |
|-------|------------|
| **Airbyte** | Deliver all must-have success criteria within [POC duration] |
| **Airbyte** | Provide named SE support with [response SLA] |
| **[Customer]** | Provide source/destination credentials and test data by [date] |
| **[Customer]** | Have technical resource available for [X hours/week] during POC |
| **[Customer]** | If all must-have criteria are met, commit to a commercial conversation within 2 weeks of POC end |
| **Both** | If criteria are not met, mutually agree the deal doesn't move forward (or explicitly extend scope with a renegotiated commitment) |

**Why this matters:** Without these, a "passed POC" can still stall — customers pass technically and disappear commercially. This section gets signed off by champion + EB before kickoff.

---

### Success Criteria
These are the specific, measurable outcomes that define a successful POC. Both parties should agree on these before the POC begins.

| # | Criterion | How it will be measured | Must-have or Nice-to-have |
|---|-----------|------------------------|--------------------------|
| 1 | [e.g., Data from Salesforce successfully synced to Snowflake] | Manual data validation / row count check | Must-have |
| 2 | [e.g., Full refresh completes within X hours] | Sync duration logged | Must-have |
| 3 | [e.g., Incremental sync detects and captures all CDC events] | Delta validation | Must-have |
| 4 | [e.g., SSO login works with their IdP] | Login test | Must-have |
| 5 | [e.g., SE can configure connector without engineering support] | Usability assessment | Nice-to-have |

**POC passes if:** All must-have criteria are met.

---

### Scope

**In scope:**
- [Use case 1 — e.g., Salesforce → Snowflake full refresh + incremental]
- [Use case 2 — e.g., Postgres CDC → Snowflake]
- [Specific streams or tables to validate]
- [Security validation — SSO, RBAC, audit logs]

**Out of scope (explicitly):**
- [e.g., Custom connector development]
- [e.g., dbt transformation layer]
- [e.g., Production-scale volume testing]
- [e.g., BI tool integration]

---

### POC Architecture
**Deployment:** [Airbyte Cloud / Self-Managed on [cloud provider]]
**Environment:** [Dedicated POC workspace / sandbox / their existing infra]
**Data sources:** 
**Destinations:** 
**Approximate data volume for POC:** 

---

### Timeline & Milestones
*Adjust based on POC duration. Default below is a 4-week template — for 2-week POCs, compress; for 6-8 week enterprise POCs, expand the validation phase.*

| Week | Milestone | Owner |
|------|-----------|-------|
| Pre-POC | Kickoff call, access provisioned, environment set up | Both |
| Week 1 | [Use case 1 configured and validated] | Prospect + Airbyte SE |
| Week 1-2 | [Use case 2 configured] | Prospect |
| Week 2-3 | [Security requirements validated] | Prospect + Airbyte SE |
| Week 2-3 | [Performance / volume test run] | Both |
| Mid-POC | Checkpoint call — score progress, flag risks | Both |
| End of POC | Results review call, success criteria scored | Both |
| Post-POC | Commercial conversation / next steps | AE + SE |

---

### Roles & Responsibilities

**Airbyte ([SE owner]):**
- [ ] Configure initial workspace and connections
- [ ] Provide technical guidance throughout POC
- [ ] Be available for async questions (response SLA: [e.g., same business day])
- [ ] Lead results review call

**Prospect:**
- [ ] Provide access to source systems (read-only credentials)
- [ ] Provide access to destination (Snowflake/BigQuery/etc.)
- [ ] Assign internal technical resource to be primary POC contact
- [ ] Validate data accuracy against source of truth
- [ ] Complete success criteria scoring at end of POC

---

### Access & Prerequisites Checklist
Before the POC can begin, the following must be in place:

- [ ] Airbyte Cloud workspace provisioned (or self-managed environment set up)
- [ ] Source credentials provided: [list sources]
- [ ] Destination credentials provided: [list destinations]
- [ ] Network access confirmed (firewall rules, IP allowlisting if needed)
- [ ] SSO/IdP details shared (if testing SSO)
- [ ] Internal stakeholders aligned on POC scope and timeline

---

### Risks & Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| [e.g., Access provisioning delays] | Medium | Start checklist 1 week before kickoff |
| [e.g., Connector gap for custom source] | Low | Pre-validate catalog before POC starts |
| [e.g., Stakeholder availability] | Medium | Lock milestone review dates upfront |
| [e.g., Scope creep] | Medium | Enforce written scope; log any additions as post-POC |

---

### POC Exit Criteria
At the end of the POC, one of three outcomes:

| Outcome | Definition | Next step |
|---------|------------|-----------|
| **Pass** | All must-have criteria met | Advance to commercial discussion |
| **Conditional pass** | Must-haves met, minor gaps remain | Agree remediation plan, proceed to commercial |
| **No-go** | One or more must-haves not met | Document gap, escalate to Product/Eng or disqualify |

---

### Story for Results Review (Pre-staged)
**The narrative you'll tell at the end of POC — designed during planning, not afterward.**

Not "we synced data." Something like:
> "We synced [customer's hardest source] in [actual hours of config time] vs. the [customer-estimated weeks] it would have taken your team to build. Schema changes during the POC were handled automatically — no engineering intervention needed."

Pre-stage 2-3 narrative beats you'll be able to tell if the POC succeeds:
1. [Narrative beat tied to a Must-Have criterion]
2. [Narrative beat tied to a Must-Have criterion]
3. [Narrative beat tied to a Need-Payoff moment]

---

### Notes / Open Items
- [ ] [Any open question or dependency before POC can be confirmed]

---

## Style

- Success criteria specific and measurable — reject vague criteria like "data looks correct"
- Scope tight — a 2-week POC that passes beats a 6-week POC that drifts
- Surface prerequisites early — access delays are the most common POC killer
- Extract use cases from transcripts and prior qual docs; cite source inline
- Mutual Commitments section is the foundation; everything else supports it

---

## SE Best Practices Applied to POC Planning

Read `~/.claude/skills/_se-playbook.md` for full framework details.

### Source Freshness Check (Gong Fallback)
Per `_se-playbook.md` ("Source Freshness Check"): if the most-recent local transcript is more than **14 days old**, search Gong for newer calls before scoping the POC. The most recent call usually contains the freshest version of success criteria, scope, and stakeholder commitments — using stale data risks scoping a POC the customer has already moved past.
- Pull the **most recent call only** — do not bulk-pull
- Save to `_transcripts/<Customer-Name>-MM.DD.YY.txt` BEFORE using it (per CLAUDE.md)

### Upfront Contract (Sandler) is mandatory for POCs
A POC without a written upfront contract = a POC that drifts. Add a `### Mutual Commitments` section before Success Criteria:
- "If we hit all must-have criteria by [end date], you commit to a commercial conversation within 2 weeks."
- "If we miss criteria, we mutually agree the deal doesn't move forward (or we explicitly extend scope)."

Without this, a "passed POC" can still stall — customers can pass technically and disappear commercially.

### Success criteria must tie to MEDDPICC Metrics
Every must-have criterion should map back to a Metric in biz-qual. If the customer cares about "reducing pipeline maintenance time," a success criterion should measure that. If a criterion can't be traced to a stated Metric, it's a feature test, not a value test — flag it.

### Define the Decision Process (MEDDPICC D) inside the POC
Add a section: "At end of POC, who scores it? Who decides to move forward?" Get names. If the EB isn't in the loop on the POC outcome, the POC isn't actually de-risking the deal — it's busywork.

### Voss "no" calibration at kickoff
At the kickoff call, ask: "Is there anything about how this POC is scoped that wouldn't be acceptable to your security/legal/procurement team?" Getting to a "no" early surfaces blockers before they kill momentum at end of POC.

### Pre-stage the Reframe for end-of-POC
When designing the POC, identify the *story* the data will tell at the results review. Not "we synced data" — "we synced your hardest source in 4 hours of config time vs. the 6 weeks your team estimated to build it." Add a `### Story for Results Review` section noting the planned narrative.

### Anti-patterns to avoid in this skill
- Vague success criteria ("data looks correct", "performance is acceptable")
- POC scope that grows without renegotiating timeline or commercial commitment
- No named decision-maker for "did the POC pass?" — scoring becomes political
- 6-week POCs without milestone check-ins — they always drift to 10 weeks
- Treating the POC as a feature demo instead of a value validation

---

## After Generating

### Auto-save (default)

Per `_se-playbook.md` "Output Persistence (Auto-Save)" rule, save to:
```
~/airbyte-work/01-customers/<Customer>/outputs/poc-plan/poc-plan-<YYYY-MM-DD>-<descriptor>.md
```

Create folders if missing. Append `-v2` etc. for same-day duplicates. User can suppress with `--no-save`.

### Source Coverage

Include a Source Coverage section at the top reporting prior qual docs read, transcripts referenced (line counts), and any external context pulled.

### SE Identity

Read `~/airbyte-work/.se-config.yaml` for the `[SE name]` field in the SE owner row.

---

## Changelog

- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Hard prerequisite: refuses to run without transcripts; warns if no biz-qual/tech-qual. Reads prior outputs. Mutual Commitments (Sandler upfront contract) section added — required for any POC. Story for Results Review pre-staged. Flexible 2-8 week duration. Mid-POC checkpoint milestone. [SE name] placeholder. Style normalized.
- **2026-05-27** — Initial scaffold.
