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

### Prerequisite check (run this FIRST, before generating anything)

1. **Check for transcripts** (local `_transcripts/` + Gong). If **zero transcripts exist: REFUSE TO RUN.** A POC can't be scoped from hypotheses, and biz-qual/tech-qual would themselves refuse without customer voice — so there's nothing to chain. Output:
   > "Cannot scope a POC for [Customer] — no customer voice in any source. Run `prep-call` → hold the call → then `biz-qual` + `tech-qual` → then re-run `poc-plan`."

2. **If a transcript exists, check for the qualification docs** in `{customers_dir}/<Customer>/outputs/` (per playbook → Workspace Paths) — `biz-qual-*.md` and `tech-qual-*.md`.

   - **Both present** → proceed to generate the POC plan.
   - **One or both missing** → **do NOT silently proceed, and do NOT silently auto-run.** List exactly what's missing and offer to run it first:
     > "poc-plan builds on qualification, and some is missing for [Customer]:
     > &nbsp;&nbsp;• Missing: **biz-qual**, **tech-qual** *(list only the ones actually missing)*
     > &nbsp;&nbsp;• Transcript: ✓ found
     >
     > Want me to run the missing qualification skill(s) now, then continue to the POC plan? (Or reply 'skip' to scope the POC anyway — POCs without qualification tend to drift.)"

   - **On approval:** invoke the missing skill(s) in dependency order — `biz-qual` first, then `tech-qual` — let each save its output, then read those outputs and continue to the POC plan. Cite them as sources.
   - **If the user says 'skip':** proceed, but add a prominent ⚠️ flag in the output noting the POC was scoped without full qualification and is at higher risk of scope drift.

**Never fabricate a qualification doc to satisfy this check.** If a chained biz-qual/tech-qual would refuse (e.g. the transcript has no technical discovery for tech-qual), report that honestly rather than producing a hollow doc.

## Before generating: read prior outputs

POC scope must build on prior qualification. Before generating, check the customer's `outputs/` folder (`{customers_dir}/<Customer>/outputs/<skill>/`) for and read:
- **`outputs/deployment-qual/deployment-qual-*.md`** — required. POC architecture depends on deployment model. If missing and the customer has non-trivial requirements, **stop and suggest running `deployment-model-qual` first**.
- **`outputs/tech-qual/tech-qual-*.md`** — technical fit, volume, security, integration risks → feed directly into POC scope and success criteria
- **`outputs/biz-qual/biz-qual-*.md`** — MEDDPICC Metrics directly map to Success Criteria; Decision Process informs Mutual Commitments
- **`outputs/connector-feasibility/connector-feasibility-*.md`** — confirmed coverage feeds Sources/Destinations sections
- **Prior call summaries** in `outputs/post-call/post-call-*.md` — recent customer commitments and concerns

Cite source documents inline. **If a POC is being scoped without prior tech-qual and biz-qual, flag this as risky** — POCs without qualification usually drift.

**Light connector-availability validation of POC scope.** When the POC names connectors, don't scope on a connector that turns out to be community-tier or non-Cloud by surprise. **Prefer to reuse the already-computed Availability column from `outputs/connector-feasibility/connector-feasibility-*.md` if it exists — don't re-derive.** Only if no connector-feasibility doc exists, run a quick connector availability lookup (per `_se-playbook.md` → "Product & Connector Reference Data") for each scoped connector to resolve `{ exists, supportLevel, cloud_available, self_managed_only, isEnterprise }`. Two things to flag (as a Scope bullet and/or a Risks-table row — keep it a footnote, not a new section):
- **`self_managed_only`** (in the OSS registry but absent from Cloud — the OSS-minus-Cloud set difference) or an **enterprise variant** (DS3 `airbyte-enterprise` `connector_stubs.json` — Oracle/NetSuite/SAP HANA/ServiceNow/SharePoint/Workday/DB2/dest-Salesforce): **flag it — the POC can't run on a Cloud trial. It forces Enterprise Flex (hybrid) or self-hosted OSS,** which changes prerequisites (data-plane provisioning, entitlement) and timeline. Reflect this in POC Architecture (Deployment) and the Access & Prerequisites checklist.
- **`supportLevel: community`** — note that the connector isn't certified; set expectations on reliability in the POC and don't hang a must-have success criterion on it without saying so.
- **Graceful degradation (per playbook):** if the registry cache / `airbyte-enterprise` isn't reachable and no connector-feasibility doc exists, do **not** assert availability — say "connector availability not verified against registry" and don't claim a deployment shape you couldn't confirm.

## Output mode

Default = full POC plan (objective, mutual commitments, success criteria, scope, architecture, timeline, R&R, prerequisites, risks, exit criteria, story).

If user signals brief mode (`--brief`, `quick POC plan`, `POC summary`): produce just Objective + Mutual Commitments table + must-have Success Criteria + Timeline + Next Step. Skip scope detail, architecture, prerequisites checklist, risk table. See `_se-playbook.md` "Output Mode" for the unified brief-mode rule.

## Output Format

Document structure follows `_se-playbook.md` → Output Document Format (H1 title → At a Glance → Jump-to index → Source Coverage → H2 body sections, callouts, `==key==` emphasis).

---

# POC Plan: [Company Name] × Airbyte
**Date:** [today's date, long form] · **SE owner:** [SE name] · **AE:** [AE name]

### At a Glance
*Decision card — lead with what this POC proves and the call after (see `_se-playbook.md` → Decision-First Layout).*
- **POC proves:** [the one thing — e.g. "Airbyte reliably lands SAP + Coupa data in their Postgres CDR at scale"]
- **Timeline:** ==[N] weeks== ([start] → [end]) · **Mid-POC checkpoint:** ==[date]==
- **Success criteria:** ==[N]== ([M] must-have) · **Mutual commitments:** [N]
- **Scope:** [sources → destination] · **Volume:** ==[e.g., 50M rows]==
- **Primary risk:** [the one thing most likely to derail the POC — one line]
- **Prospect technical lead:** [name / title if known]
- **Source confidence:** [one line — prior qual docs + transcripts; "see Source Coverage"]

**Jump to:** [At a Glance](#at-a-glance) · [Source Coverage](#source-coverage) · [POC Objective](#poc-objective) · [Mutual Commitments](#mutual-commitments-upfront-contract--sandler) · [Success Criteria](#success-criteria) · [Scope](#scope) · [POC Architecture](#poc-architecture) · [Timeline & Milestones](#timeline--milestones) · [Roles & Responsibilities](#roles--responsibilities) · [Access & Prerequisites Checklist](#access--prerequisites-checklist) · [Risks & Mitigations](#risks--mitigations) · [POC Exit Criteria](#poc-exit-criteria) · [Story for Results Review](#story-for-results-review-pre-staged) · [Notes / Open Items](#notes--open-items)

## Source Coverage
[Prior qual docs read, transcripts referenced (line counts), external context pulled — see After Generating. Note whether scoped-connector availability was **reused from connector-feasibility**, checked directly against the **registry/`airbyte-enterprise`** (with cache/checkout date), or **not verified** (source unavailable → availability claims capped).]

## POC Objective
**In one sentence, what does this POC need to prove?**
> [e.g., "Validate that Airbyte can replicate data from [Source A] and [Source B] into Snowflake reliably, at [X] volume, with acceptable latency, and meet [Company]'s security requirements."]

## Mutual Commitments (Upfront Contract — Sandler)
**Required for any POC. A POC without a written upfront contract drifts.**

> [!info] Mutual Commitments — Sandler upfront contract
> This is the foundation of the POC; it gets signed off by champion + EB before kickoff. Without it, a "passed POC" can still stall — customers pass technically and disappear commercially.

*Owner = the named person accountable; render `TBD` when not yet assigned (never invent a name).*

| Party | Commits to | Owner | By when |
|-------|------------|-------|---------|
| **Airbyte** | Deliver all must-have success criteria within [POC duration] | [SE name] | [POC end] |
| **Airbyte** | Provide named SE support with [response SLA] | [SE name] | ongoing |
| **[Customer]** | Provide source/destination credentials and test data | [name or **TBD**] | [date or **TBD**] |
| **[Customer]** | Have technical resource available for [X hours/week] during POC | [name or **TBD**] | [date or **TBD**] |
| **[Customer]** | If all must-have criteria are met, commit to a commercial conversation within 2 weeks of POC end | [EB/champion or **TBD**] | [POC end + 2wk] |
| **Both** | If criteria are not met, mutually agree the deal doesn't move forward (or explicitly extend scope with a renegotiated commitment) | — | [POC end] |

## Success Criteria
These are the specific, measurable outcomes that define a successful POC. **Both parties must agree on these in writing before the POC begins** (Sandler upfront contract — "if we hit these, you move to procurement"). Each criterion must be numeric/verifiable and **tie back to a MEDDPICC Decision Criterion** — the POC should prove the thing that actually drives the buy, not a generic feature checklist. See `_se-playbook.md` → Operating Disciplines for why an open-ended POC without pre-agreed exit criteria becomes a science project.

| # | Criterion | How it will be measured | Ties to Decision Criterion | Must-have or Nice-to-have |
|---|-----------|------------------------|----------------------------|--------------------------|
| 1 | [e.g., Data from Salesforce successfully synced to Snowflake] | Manual data validation / row count check | [which MEDDPICC DC this proves] | Must-have |
| 2 | [e.g., Full refresh completes within X hours] | Sync duration logged | [DC] | Must-have |
| 3 | [e.g., Incremental sync detects and captures all CDC events] | Delta validation | [DC] | Must-have |
| 4 | [e.g., SSO login works with their IdP] | Login test | [DC] | Must-have |
| 5 | [e.g., SE can configure connector without engineering support] | Usability assessment | [DC] | Nice-to-have |

**POC passes if:** All must-have criteria are met. **Pre-agreed with:** [name + role who signed off on these criteria, and date] — if this is blank, the criteria aren't really agreed yet; flag it.

**Preserve the customer's stated success criteria.** Do not remove a difficult success criterion merely to make the POC easier to complete. If a criterion is genuinely out of scope for the POC, move it to **Optional stretch scope** or **Production requirements** below and explain the rationale; do not silently drop it. The customer's baseline requirement stays the baseline.

## Scope

**In scope:**
- [Use case 1 — e.g., Salesforce → Snowflake full refresh + incremental]
- [Use case 2 — e.g., Postgres CDC → Snowflake]
- [Specific streams or tables to validate]
- [Security validation — SSO, RBAC, audit logs]

*Connector-availability note (from connector-feasibility, or a quick registry lookup — see "Before generating"): [confirm each scoped connector is Cloud-available + certified, OR flag — e.g. "`source-db2` is Self-Managed-only (not on Cloud) → this POC runs on Enterprise Flex or self-hosted OSS, not a Cloud trial"; "`source-workday` is an enterprise variant (Flex, entitlement-gated)". If unverified: "connector availability not verified against registry."]*

**Out of scope (explicitly):**
- [e.g., Custom connector development]
- [e.g., dbt transformation layer]
- [e.g., Production-scale volume testing]
- [e.g., BI tool integration]

### Scope tiers
Separate the POC into four tiers so the customer sees what is being proven now versus what is deferred:
- **Minimum viable POC scope:** the smallest set of must-have success criteria that proves the core value. This is what the POC will actually run.
- **Optional stretch scope:** additional success criteria or connectors that will be validated only if time permits and only with explicit customer agreement.
- **Production requirements:** items the customer needs in production but that are intentionally excluded from the POC (e.g., full volume, full HA, production SSO, dbt transforms). List them so they are not forgotten.
- **POC-specific simplifications:** deliberate departures from the final production architecture (e.g., test data subset, one region, manual credential rotation). Label each one and explain what must be revisited before go-live.

Do not let a production requirement disappear because it is "hard to test in a POC." If it cannot be tested, say so, document the proxy validation, and keep it in the production requirements list.

## POC Architecture
- **Deployment:** [Airbyte Cloud / Self-Managed on [cloud provider]]
- **Environment:** [Dedicated POC workspace / sandbox / their existing infra]
- **Data sources:** [list]
- **Destinations:** [list]
- **Approximate data volume for POC:** [estimate]

## Timeline & Milestones

**Size the POC to scope, not habit:**
- **1–2 weeks** — single-connector validation (one source → one destination, no transformation).
- **3–5 weeks** — multi-source and/or a transformation/modeling requirement.
- **6–8 weeks** — only with a stated enterprise reason (security review, procurement gate, multi-team coordination). If you're reaching for 6–8 weeks without one of those, right-size down.

*Default below is a 4-week template — for 1–2-week POCs, compress; for 6–8-week enterprise POCs, expand the validation phase.*

**Anchor the end date to the customer's compelling event (D2).** If biz-qual surfaced a dated forcing function (contract renewal, migration deadline, audit), back-plan the POC so results-review + commercial conversation land with enough runway to hit it (POC success → security review → procurement/legal → signature, before the event). If there's no compelling event, say so — the POC timeline is then SE-driven, not customer-driven, which is a weaker position worth noting.

| Week | Milestone | Owner |
|------|-----------|-------|
| Pre-POC | Kickoff call, access provisioned, environment set up | Both |
| Week 1 | [Use case 1 configured and validated] | Prospect + Airbyte SE |
| Week 1-2 | [Use case 2 configured] | Prospect |
| Week 2-3 | [Security requirements validated] | Prospect + Airbyte SE |
| Week 2-3 | [Performance / volume test run] | Both |
| Mid-POC | **Go/no-go checkpoint** — see below | Both |
| End of POC | Results review call, success criteria scored | Both |
| Post-POC | Commercial conversation / next steps | AE + SE |

**Mid-POC checkpoint is an explicit go/no-go gate, not a status call.** State it as: "By [mid-date] we should have [specific must-have working]; if not, we pause and diagnose rather than pushing to the end date." Name who declares go/no-go (the SE + the customer's POC owner). A POC that's clearly off-track at the midpoint is cheaper to reset than to let drift to the end.

## Roles & Responsibilities

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

## Access & Prerequisites Checklist
Before the POC can begin, the following must be in place. *Access delays are the #1 POC killer — name an owner and a date for each. Render `TBD` when unassigned; never invent.*

| Prerequisite | Owner | By when | Status |
|--------------|-------|---------|--------|
| Airbyte Cloud workspace provisioned (or self-managed env set up) | [name or **TBD**] | [date or **TBD**] | ☐ |
| Source credentials provided: [list sources] | [name or **TBD**] | [date or **TBD**] | ☐ |
| Destination credentials provided: [list destinations] | [name or **TBD**] | [date or **TBD**] | ☐ |
| Network access confirmed (firewall, IP allowlist if needed) | [name or **TBD**] | [date or **TBD**] | ☐ |
| SSO/IdP details shared (if testing SSO) | [name or **TBD**] | [date or **TBD**] | ☐ |
| Internal stakeholders aligned on POC scope and timeline | [name or **TBD**] | [date or **TBD**] | ☐ |

## Risks & Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| [e.g., Access provisioning delays] | Medium | Start checklist 1 week before kickoff |
| [e.g., Connector gap for custom source] | Low | Pre-validate catalog before POC starts |
| [Scoped connector is Self-Managed-only or an enterprise variant → POC can't run on a Cloud trial] | [only if flagged above] | Provision Enterprise Flex (or self-hosted OSS) up front; add data-plane/entitlement to prerequisites — don't discover it at kickoff |
| [e.g., Stakeholder availability] | Medium | Lock milestone review dates upfront |
| [e.g., Scope creep] | Medium | Enforce written scope; log any additions as post-POC |

> [!risk] Top POC risk
> [Name the single highest-likelihood/highest-impact risk from the table and the concrete mitigation. Access-provisioning delays and scope creep are the most common POC killers — call out whichever applies here.]

## POC Exit Criteria
At the end of the POC, one of three outcomes:

| Outcome | Definition | Next step |
|---------|------------|-----------|
| **Pass** | All must-have criteria met | Advance to commercial discussion |
| **Conditional pass** | Must-haves met, minor gaps remain | Agree remediation plan, proceed to commercial |
| **No-go** | One or more must-haves not met | Document gap, escalate to Product/Eng or disqualify |

## Story for Results Review (Pre-staged)
**The narrative you'll tell at the end of POC — designed during planning, not afterward.**

Not "we synced data." Something like:
> "We synced [customer's hardest source] in [actual hours of config time] vs. the [customer-estimated weeks] it would have taken your team to build. Schema changes during the POC were handled automatically — no engineering intervention needed."

Pre-stage 2-3 narrative beats you'll be able to tell if the POC succeeds:
1. [Narrative beat tied to a Must-Have criterion]
2. [Narrative beat tied to a Must-Have criterion]
3. [Narrative beat tied to a Need-Payoff moment]

## Notes / Open Items
- [ ] [Any open question or dependency before POC can be confirmed]

---

## Style (poc-plan skill guidance — not part of output template)

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
- Removing a difficult success criterion just to make the POC easier to complete
- POC scope that grows without renegotiating timeline or commercial commitment
- No named decision-maker for "did the POC pass?" — scoring becomes political
- 6-week POCs without milestone check-ins — they always drift to 10 weeks
- Treating the POC as a feature demo instead of a value validation

---

## After Generating

### Auto-save (default)

Per `_se-playbook.md` "Output Persistence (Auto-Save)" rule, save to:
```
{customers_dir}/<Customer>/outputs/poc-plan/poc-plan-<YYYY-MM-DD>-<Descriptor>.md
```

Create folders if missing. Append `-v2` etc. for same-day duplicates. User can suppress with `--no-save`.

Per `_se-playbook.md` "Date format inside documents", write dates in the document body — the H1/title line especially, plus headers and prose — in long form (`June 11, 2026`), not the numeric `2026-06-11`. The numeric `YYYY-MM-DD` stays only in the filename.

### Source Coverage

Include a Source Coverage section at the top reporting prior qual docs read, transcripts referenced (line counts), and any external context pulled.

### SE Identity

Read `config_file` (per playbook → Workspace Paths) for the `[SE name]` field in the SE owner row.

---

## Changelog

- **2026-07-14** — **Phase 3 guardrails: preserve success criteria and separate POC scope tiers.** Added an explicit rule that the customer's stated success criteria must be preserved — a difficult criterion cannot be silently removed to make the POC easier. Added a "Scope tiers" subsection separating minimum viable POC scope, optional stretch scope, production requirements, and POC-specific simplifications so the customer sees what is proven now vs. deferred.
- **2026-07-10** — **Light DS1/DS3 connector-availability validation of POC scope (secondary consumer of "Product & Connector Reference Data").** When the POC names connectors, validate each against the registry — existence, `supportLevel` tier, and Cloud-vs-Self-Managed availability (the OSS-minus-Cloud set difference) + enterprise-variant detection (private `airbyte-enterprise` `connector_stubs.json`) — so a POC isn't scoped on a community-tier or non-Cloud connector by surprise. A `self_managed_only` or enterprise-variant connector now flags a POC-shape risk: it forces Enterprise Flex (hybrid) or self-hosted OSS rather than a Cloud trial, changing prerequisites/timeline. Prefers to reuse connector-feasibility's already-computed Availability column (no re-derive); hits the registry directly only if no connector-feasibility doc exists; degrades loud ("availability not verified against registry") when the cache/enterprise repo is unreachable. Surfaced as a Scope bullet + a Risks-table row + a Source Coverage note — no new section, refusal rules unchanged.
- **2026-07-10** — Success criteria now must tie each criterion to a MEDDPICC Decision Criterion + record who pre-agreed them in writing (per playbook → Operating Disciplines); POC timeline anchored to the customer's compelling event (D2) with backward-planning to signature.
- **2026-07-10** — Repointed hardcoded `~/airbyte-work/` paths to the workspace-path resolver (`{customers_dir}`/`{transcripts_dir}`/`{notes_dir}`/`config_file`/`memory_dir`) per playbook → Workspace Paths. Portable across SE machines.
- **2026-07-09** — Fixed the "Before generating" prior-doc read block: reads `deployment-qual`/`tech-qual`/`biz-qual`/`connector-feasibility` from `outputs/<skill>/` (was the customer root — inconsistent with the already-correct check earlier in the skill); prior call summaries now `outputs/post-call/post-call-*.md` (was the never-existing `call-summary-*.md`).
- **2026-07-09** — Added duration-sizing heuristics (1–2 wk single-connector / 3–5 wk multi-source or transform / 6–8 wk only with a stated enterprise reason) so the timeline is right-sized to scope, not habit; reframed the mid-POC checkpoint as an explicit go/no-go gate with a named decider ("by [mid-date] X works or we pause & diagnose").
- **2026-07-07** — Prerequisite handling changed from warn-but-proceed to **detect & offer to run first**: if biz-qual/tech-qual are missing (but a transcript exists), poc-plan now lists what's missing and offers to run the missing qualification skill(s) in order, then continue. Still refuses on zero transcripts (nothing to chain); still allows 'skip' with a drift-risk flag; never fabricates a qual doc.
- **2026-06-18** — Output adopts the shared Output Document Format (_se-playbook.md): At-a-Glance + Jump-to index, H2-per-section, callouts, ==key== emphasis.

- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Hard prerequisite: refuses to run without transcripts; warns if no biz-qual/tech-qual. Reads prior outputs. Mutual Commitments (Sandler upfront contract) section added — required for any POC. Story for Results Review pre-staged. Flexible 2-8 week duration. Mid-POC checkpoint milestone. [SE name] placeholder. Style normalized.
- **2026-05-27** — Initial scaffold.
