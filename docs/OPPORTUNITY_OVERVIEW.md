# Opportunity Overview — Product and Implementation Specification

**Status:** Slice 1 and local Slice 2A implemented; Slice 2B and later slices remain proposed
**Source-of-truth baseline:** Airbyte-Solutions-Team/se-skills main at 6df71210abe9315f175a459c25d2f5e32bbb7fe6, inspected September 17, 2026
**Scope of this document:** Product behavior, information architecture, domain boundaries, API and persistence direction, implementation slices, and acceptance criteria. This document does not authorize deployment, production data migration, new providers, or expansion of the hosted skill allowlist.

## 1. Executive decision

The Opportunity Overview is the canonical front door for one opportunity. It is not another generated skill report.

The product model is:

- **Opportunity Overview:** the current, attributable representation of what is known about the opportunity.
- **Generate:** creates a durable, task-specific artifact such as Prep Call, Post Call, Technical Qualification, or POC Plan.
- **Update overview:** reconciles new authorized evidence into a new immutable version of canonical opportunity state.
- **Outputs:** durable generated artifacts with their own generation, validation, correction, approval, and export history.
- **Evidence and activity:** the source material and events supporting the current state and generated outputs.

This distinction is merge-blocking for the design. The implementation must not create a loop where one generated Markdown report is parsed in the browser and treated as canonical truth, then fed into other reports without provenance.

The page-level design principle is:

> **Overview surface = important conclusions. Expandable cards = supporting reasoning, missing information, evidence, and next steps.**

## 2. Decisions already established in product discussion

The following product behaviors are treated as established for this specification:

1. Opening an opportunity should provide a useful, already-prepared overview rather than landing on a file list.
2. The overview should let an SE understand the opportunity in approximately 60–90 seconds.
3. Tech Eval / POV Readiness is a core operating component and should help the SE both prepare for and execute a successful evaluation.
4. MEDDPICC should retain the existing progressive-disclosure pattern: an overall collapsed summary, followed by individually expandable dimensions.
5. Existing outputs should be first-class objects on the Overview and open in one click.
6. **Update overview** and **Generate** are separate actions with separate outcomes.
7. Missing evidence must be explicit. The application should show Unknown, Not established, or Insufficient evidence rather than completing the page with invented content.
8. Critical blockers must remain visible even when their detailed card is collapsed.
9. Previous generated outputs and human corrections must not be silently overwritten.

The following remain working hypotheses until a bounded implementation slice is approved:

- Exact Postgres table names and JSON shapes.
- Whether the first functional release is local-only, hosted-only, or a shared contract with separate adapters.
- Which non-transcript integrations are included in the first Update overview source set.
- Whether numeric MEDDPICC scoring is added later. This specification does not introduce an opaque 0–100 deal score.

## 3. Current repository truth

### 3.1 Current local opportunity page

The local opportunity route is implemented by **pageOpportunity** in **webapp/static/app.js**. It currently:

- Loads opportunity-scoped outputs from **GET /api/accounts/{account}/outputs?opp={slug}**.
- Displays Live Transcribe, Coverage Handoff, Invoke Skill, and a free-text command bar.
- Displays Generated Outputs grouped by date.
- Reattaches to running jobs and refreshes the output list when a job finishes.
- Receives the opportunity display name from the hash route rather than loading a dedicated opportunity-detail read model.
- Has no opportunity brief, canonical state, technical-evaluation lifecycle, risks/actions summary, stakeholder summary, or evidence/freshness contract.

The local output list is produced by **OutputService.list_outputs** in **webapp/services/output_service.py**. It returns generation time, skill, filename, path, validation fields, and reference-freshness fields, but it does not currently return the human review state in the opportunity output-list response.

Local reruns create separate Markdown or HTML files. They are durable artifacts, but they are not represented as an explicit artifact-series and generation-version model.

### 3.2 Current team landing Overview

**webapp/routes/overview.py** and **webapp/services/overview_service.py** implement the Solutions Team landing-page operational overview. This aggregates members, accounts, jobs, recent outputs, failures, and review attention.

It is not an opportunity overview and must not be repurposed as the new domain service. A new service should use an unambiguous name such as **OpportunityWorkspaceService** for the aggregate read model and **OpportunityStateService** for canonical state.

### 3.3 Current hosted opportunity page

The hosted opportunity route **pageHostedAccountOpportunity** in **webapp/static/app.js** currently:

- Resolves the selected account and opportunity from list APIs.
- Lists and uploads transcripts for that opportunity.
- Does not show opportunity-scoped outputs.
- Does not show an opportunity summary or Update overview action.

The hosted account page lists valid account outputs, but **webapp/hosted/outputs.py** does not currently accept an opportunity filter. Output rows already carry **opportunity_id**, so the relationship exists but the read path is not yet shaped for an opportunity workspace.

### 3.4 Current hosted persistence

The hosted implementation already has sound foundations that must be reused:

- Organization-owned accounts and opportunities with non-null **org_id** and RLS.
- Private, organization-scoped transcript and output storage.
- Durable jobs and attempts with retries, timeouts, cancellation, idempotency, lineage, token usage, and cost.
- Immutable generated outputs.
- Append-only output corrections and review evidence.
- Exact-version approval and export semantics.
- User-action audit events.

The implemented hosted **opportunities** schema contains name, slug, stage, close date, amount, creator, and assignee. Some intended fields described in **docs/DATA_MODEL.md**, such as opportunity type, closed state, and AE, are not present in migration 001 or **OpportunityOut** and therefore cannot be assumed by the UI.

### 3.5 Existing skill contracts

The current skill contracts already produce much of the analysis users expect, but as separate Markdown artifacts:

- **account-refresher:** people, history, current state, open items, watch-outs.
- **post-call:** key takeaways, deal impact, technical changes, objections, actions, evidence coverage.
- **biz-qual:** MEDDPICC, stakeholder map, gaps, risks, recommended actions.
- **tech-qual:** technical fit, requirements and architecture, readiness, risks, open validation items, next actions.
- **poc-plan:** objective, success criteria, scope, commitments, prerequisites, timeline, risks, exit criteria.
- **deal-assessment:** trajectory, deal thesis, stakeholders, close-path blockers, actions, and movement.

These artifacts are useful source material for an SE, but their Markdown layout is not a canonical opportunity-state schema. **webapp/output_schema.py** extracts headings and validation metadata; it does not establish authoritative field-level opportunity facts.

### 3.6 Hosted beta boundary

The approved hosted beta currently supports **post-call** as the first hosted skill. Additional hosted skills remain post-beta work pending runtime and sandbox review. This Overview design must not silently make all local skills available in hosted mode.

## 4. Product goals

The Opportunity Overview should answer these questions without opening another document:

1. What is the customer trying to accomplish?
2. Why are they evaluating Airbyte?
3. Where does the opportunity stand now?
4. What was validated, and what remains unverified?
5. What could prevent a technical or commercial win?
6. What needs to happen next, by whom, and by when?
7. Is the opportunity ready to start a POV?
8. If a POV is active, is it progressing toward explicit success criteria?
9. Who matters, and which required stakeholders are not engaged?
10. What changed since the previous overview update?
11. Which deeper outputs already exist, and what review state are they in?
12. Which source supports a material claim?

A successful page should let an SE return after several days away and reorient in under 90 seconds, then drill into supporting detail without navigating away.

## 5. Non-goals

The first implementation must not:

- Replace generated outputs with one giant overview.
- Make the Overview a user-visible skill output.
- Parse arbitrary Markdown in the browser to create canonical state.
- Treat an unreviewed model output as higher authority than raw evidence or a human-confirmed fact.
- Introduce a new opaque 0–100 deal-health score.
- Treat completion percentage as proof of readiness when a hard gate is incomplete.
- Write back to Salesforce or any other external system.
- enable local-only capabilities in hosted mode.
- add unrestricted shell, Git, browser automation, local repository access, or arbitrary egress to the hosted runtime.
- migrate existing customer data or deploy a production environment.
- redesign the document reader; outputs remain separate reader experiences.
- force all sections to contain content for an early-stage opportunity.
- infer that an account-level artifact belongs to one opportunity merely because the account has only one currently open opportunity.

## 6. Product and data flow

~~~mermaid
flowchart TD
    E["Authorized evidence<br/>transcripts, CRM snapshots, notes, documents"]
    S["Canonical opportunity state<br/>immutable versions + human overrides"]
    V["Opportunity Overview<br/>effective current read model"]
    G["Generate task-specific output"]
    O["Durable output generations<br/>corrections + approvals"]

    E --> S
    S --> V
    E --> G
    S --> G
    G --> O
    O --> V
~~~

The arrow from Outputs to Overview means that the Overview surfaces output metadata and links. It does not mean that unreviewed generated prose automatically becomes canonical state.

A reviewed output may contribute an explicit human-confirmed correction in a later slice, but that must be a deliberate, attributable promotion into state.

## 7. Page information architecture

The desktop page order is:

1. Opportunity header and freshness.
2. Opportunity Brief.
3. Deal / technical / commercial / timeline indicators.
4. Top Risks and Recommended Actions.
5. Technical Evaluation Lifecycle.
6. Opportunity Outputs.
7. Business Case.
8. MEDDPICC.
9. Stakeholders.
10. Evidence and Recent Activity.

At narrow widths, two-column regions stack in the same order. Critical information must not disappear behind a responsive breakpoint.

### 7.1 Reference wireframe

~~~text
Opportunity name                                  Update overview   Generate
Account · stage · amount · close date
Updated 2h ago · 3 new sources · source confidence

OPPORTUNITY BRIEF
Customer objective · Why Airbyte · Current status
Path to decision · Immediate priority

DEAL QUALIFICATION   TECHNICAL FIT   TIMELINE   CUSTOMER SENTIMENT
Moderate             At risk         Tight      Positive
No opaque composite score

TOP RISKS                              RECOMMENDED ACTIONS
Security approval                      Engage security approver
Connector gap                          Validate named connector
Compressed timeline                    Confirm evaluation calendar

TECHNICAL EVALUATION
Ready with risks · 8/11 required items complete · 2 blockers
Primary blocker remains visible
[expand phases and items]

OPPORTUNITY OUTPUTS
Call Prep        Post Call        Tech Qual        POC Plan
Latest status    Reviewed         Needs review     Not generated
Open             Open             Open             Generate
View all outputs

BUSINESS CASE
[collapsed summary; expand]

MEDDPICC
Moderate · 2 unknown · 3 partial
[expand overall, then expand each dimension]

STAKEHOLDERS
1 blocking approver not engaged · 1 champion · 4 mapped
[expand]

EVIDENCE AND RECENT ACTIVITY
7 authorized sources · last customer interaction · update history
[expand]
~~~

## 8. Detailed page behavior

### 8.1 Opportunity header

The header displays only server-authoritative opportunity and assignment metadata:

- Opportunity name.
- Account name.
- Stage.
- Amount, when known.
- Target close date, when known.
- Assigned SE and AE, when the schema and source provide them.
- Last customer interaction, when supported by evidence.
- Next scheduled meeting, when supported by an authorized source.
- Overview update state and freshness.

Actions:

- **Update overview** or **Create overview**.
- **Generate** dropdown.
- A secondary menu for other actions.

Local-only actions such as Live Transcribe and Coverage Handoff remain available in local mode but should move to the secondary action area rather than compete with Update and Generate. They must remain absent in hosted mode.

The UI must fetch opportunity metadata from an authorized API. It must not trust the account, opportunity name, stage, or title embedded in a hash route.

### 8.2 Opportunity Brief

The brief has a predictable field contract:

- **Customer objective**
- **Why Airbyte**
- **Current status**
- **Path to decision**
- **Immediate priority**

Each field supports:

- A value.
- Knowledge state.
- Confidence.
- Evidence references.
- Last confirmed time.
- Optional human override indicator.

Allowed knowledge states:

- **known**
- **partial**
- **unknown**
- **not_applicable**
- **conflicting**

The UI copy for missing values must be direct:

- Unknown
- Not established
- Insufficient evidence
- Conflicting evidence — review needed

A blank field must never be rendered as a completed statement.

### 8.3 Health indicators

The first release should use a small number of explainable indicators:

- **Deal qualification:** Strong, Moderate, Weak, or Insufficient evidence.
- **Technical fit:** Strong, Moderate, Weak, Blocked, or Insufficient evidence.
- **Timeline:** Comfortable, Tight, At risk, Unknown.
- **Customer sentiment:** Positive, Mixed, Negative, Unknown.

Commercial readiness may be added when its definition is separated cleanly from MEDDPICC.

Do not add one composite deal score. If numeric MEDDPICC scoring is later desired, it requires a separately approved deterministic rubric, visible calculation, and tests. Existing red/yellow/green or confirmed/partial/unknown states remain the source for v1.

Each indicator must expose:

- Current status.
- One-sentence reason.
- Evidence count.
- Last confirmed date.
- Whether it is generated, inferred, or human-confirmed.

### 8.4 Top Risks and Recommended Actions

Top Risks and Recommended Actions remain expanded because they are the operational core of the page.

Each risk includes:

- Stable item key.
- Title.
- Description.
- Severity.
- Classification: critical blocker, implementation risk, commercial risk, timeline risk, or open validation item.
- Owner, if known.
- Mitigation or clearing condition.
- Evidence.
- Last updated.

Each recommended action includes:

- Stable item key.
- Action.
- Goal.
- Definition of done.
- Owner or TBD.
- Due date or TBD.
- Status.
- Related risk, gate, MEDDPICC gap, or success criterion.
- Evidence or human-confirmation source.

The application must not invent an owner or date. It uses TBD when evidence does not support one.

### 8.5 Technical Evaluation Lifecycle

This component is stage-aware and operational. Its stable lifecycle is:

1. **Plan** — technical discovery, deployment shape, requirements, success criteria, scope, mutual commitments.
2. **Prepare** — environment, credentials, connectivity, security, sizing, responsibilities, dates, cadence.
3. **Execute** — configure data paths, run core use cases, address connector gaps, track progress.
4. **Validate** — data correctness, incremental behavior, scale, reliability/recovery, security, RBAC/SSO, monitoring, IaC, and other agreed criteria.
5. **Close** — customer scoring, unresolved-gap disposition, technical win, production architecture, results readout, handoff.

The card label adapts to state:

- Before an evaluation: **Technical Discovery**
- Preparing to start: **POV Readiness**
- During evaluation: **POV Execution**
- Near completion: **Technical Win Readiness**
- After success: **Production Handoff**

The lifecycle state is not derived from CRM stage alone. CRM stage is an input; checklist evidence and human confirmation determine the component state.

#### Collapsed state

The collapsed row shows:

- Lifecycle label.
- Readiness status.
- Completed required items / applicable required items.
- Count of blockers.
- One primary blocker or next gate.
- Freshness or last confirmed time.

Example:

> **POV Readiness — Ready with risks** · 8/11 required · 2 blockers  
> Success criteria approval and security access remain unresolved.

If a hard gate is incomplete, the summary must say **Blocked** or **Not ready**, regardless of completion percentage.

#### Expanded state

Expanding the card reveals phase rows. Each phase remains individually expandable.

Example phase summaries:

- Plan — 6/8 complete
- Prepare — 4/7 complete · 1 blocker
- Execute — 2/6 complete
- Validate — 0/8 complete
- Close — 0/5 complete

Each item has:

- Key and label.
- Phase.
- Type: gate, required, recommended, optional.
- Status: unknown, not_started, in_progress, blocked, done, not_applicable.
- Owner.
- Due date.
- Evidence references.
- Confirmation mode: generated, inferred, or human_confirmed.
- Note.
- Blocking reason.
- Clearing condition.
- Last updated.

Initial checklist content should be based on the existing **tech-qual** and **poc-plan** contracts rather than invented independently. The first template should cover at least:

**Plan**
- Technical discovery completed.
- Sources, destinations, volume, frequency, and latency captured.
- Deployment model selected or explicitly blocked.
- Connector feasibility for must-have systems validated.
- POC objective defined.
- Measurable success criteria documented.
- Must-have criteria agreed with a named customer evaluator.
- POC scope and out-of-scope boundaries documented.
- Mutual commitments established.
- Decision-maker for POC outcome identified.

**Prepare**
- Airbyte environment or data plane provisioned.
- Source credentials available.
- Destination credentials available.
- Network path validated.
- Security review initiated or completed as required.
- Data-worker or infrastructure sizing understood where applicable.
- Responsibilities assigned.
- Start, checkpoint, and exit dates agreed.
- Check-in cadence scheduled.
- Test data and validation method agreed.

**Execute**
- Source connectivity validated.
- Destination connectivity validated.
- Core data path configured.
- Initial sync completed.
- Incremental or CDC behavior tested when in scope.
- Required custom or enterprise connector path tested.
- Issues have owners and next steps.
- Scope changes are explicitly accepted rather than silently added.

**Validate**
- Every must-have criterion has an evidence-backed result.
- Data correctness validated against the source of truth.
- Required volume, throughput, or concurrency tested or covered by an agreed proxy.
- Failure and recovery behavior tested when in scope.
- Schema-change behavior tested when in scope.
- Networking and security requirements validated.
- Operational monitoring reviewed.
- RBAC, SSO, audit, Terraform, or API requirements validated when in scope.
- Production-only requirements excluded from the POV remain tracked.

**Close**
- Customer scored the success criteria.
- Failures or conditional passes have remediation owners and dates.
- Technical win or no-go is explicitly recorded.
- Production architecture is agreed.
- Results-review narrative and evidence are prepared.
- Results review completed with required stakeholders.
- Production handoff and remaining risks are documented.

#### Deterministic readiness rules

Readiness is computed, not model-scored:

1. Exclude not_applicable items from the denominator.
2. Completion ratio uses only gate and required items by default.
3. Any blocked or incomplete gate makes the phase and overall start-readiness **Blocked**.
4. No incomplete gate plus all required items done makes it **Ready**.
5. No incomplete gate plus some required items incomplete makes it **Ready with risks** only when the remaining items are explicitly non-blocking for the next phase; otherwise **Not ready**.
6. Unknown evidence on a gate is treated as incomplete.
7. Recommended and optional items inform risk but do not block.
8. A human-confirmed status takes precedence over an unreviewed inference until cleared or superseded through an attributable action.
9. Completion ratios never replace the textual readiness label.

### 8.6 Opportunity Outputs

Outputs appear near the top of the page, after the Technical Evaluation card.

The default view groups artifacts by output type and shows the latest generation for each:

- Display name.
- Skill id.
- Generated time.
- Validation status.
- Review status.
- Current generation number.
- Short description or title.
- Running/failed generation state when applicable.
- Open action.
- Generate action when absent.
- History count.

Output cards must keep validation and human review separate. Examples:

- Valid · Needs review
- Valid · Approved
- Source changed · Approved version remains historical
- Generation running
- Generation failed
- Not generated

Do not collapse these into one ambiguous badge.

The section ends with **View all outputs**. That view groups by skill, then generation. Each generation opens its own correction/approval version history.

#### Generation versus correction version

These are different concepts:

- A rerun creates a new **output generation**.
- A human correction creates a new immutable **version within one generation**.
- An approval applies to the exact current correction version of one generation.
- A later generation does not erase the previous approved generation.
- The latest generation is not automatically the recommended generation if it is invalid, failed, or unreviewed.
- The UI may label one generation **Current** only by a defined selection rule, never by latest timestamp alone.

For local files, separate timestamped files are generations. Existing feedback sidecars belong to the exact file. For hosted data, each **outputs** row is a generation and **output_versions** is its correction chain.

Opportunity-scoped output rules:

- Hosted output list APIs must filter by **org_id + account_id + opportunity_id**.
- Local output listing must stay under the resolved opportunity directory.
- Outputs with no opportunity association must not be silently attached to a specific opportunity.
- Account-level artifacts may appear in a distinct **Account-level outputs** group only when the product explicitly chooses to surface them.

### 8.7 Business Case

The collapsed state shows:

- Established dimensions / applicable dimensions.
- Strongest known business driver.
- Largest missing business-evidence gap.

Expanded content uses predictable sections:

- Current state.
- Required future state.
- Negative consequences of staying put.
- Positive business outcomes.
- Required capabilities.
- Quantified metrics or missing measurement.
- Why now / compelling event.
- Why Airbyte.

Every inferred value remains labeled as inferred. Product claims require the existing product-truth verification discipline.

### 8.8 MEDDPICC

The overall card is collapsed by default and shows:

- Overall qualification label.
- Count confirmed, partial, and unknown.
- Weakest or blocking dimension.
- Last updated and evidence count.

When expanded, each dimension is independently expandable:

- Metrics
- Economic Buyer
- Decision Criteria
- Decision Process
- Paper Process
- Identify Pain
- Champion
- Competition

Each dimension displays:

- Status.
- Known.
- Missing.
- Why it matters.
- Evidence.
- Suggested discovery.
- Human confirmation state.
- Last confirmed.

The page should not derive MEDDPICC from a rendered biz-qual table in browser JavaScript. The state updater must emit a validated typed structure or the card must show Not available.

### 8.9 Stakeholders

The collapsed state shows:

- Number mapped.
- Champion state.
- Economic-buyer state.
- Count of required but unengaged stakeholders.
- Blocking stakeholder, when present.

Expanded rows include:

- Name.
- Role and title.
- Authority.
- Disposition: champion, coach, supporter, neutral, skeptic, blocker, unknown.
- Engagement status.
- Last interaction.
- Required next engagement.
- Evidence.
- Human-confirmation state.

A title alone must not establish Economic Buyer or Champion.

### 8.10 Evidence and Recent Activity

This section is collapsed by default. It contains two related but distinct views:

**Evidence**
- Authorized source label and type.
- Source date.
- Whether read in full or metadata-only.
- Captured revision or content hash.
- Availability.
- Claims supported.
- Source freshness.
- Open action to inspect the source when an authorized route exists.

**Activity**
- Transcript uploaded or deleted.
- Overview update requested, succeeded, or failed.
- Output generation requested, succeeded, or failed.
- Comment, correction, approval, or export.
- Manual opportunity-state confirmation or clearing action.
- CRM snapshot refresh, when later implemented.

The existing audit policy remains: no transcript body, output content, prompt, model response, raw sidecar, filename, or storage path in general audit metadata.

## 9. Update overview contract

### 9.1 User-visible states

The action label and supporting text are:

| State | Label | Supporting text |
|---|---|---|
| No current state | Create overview | No overview has been created for this opportunity. |
| Current, no changes detected | Up to date | Updated {relative time}; no new authorized evidence. |
| New or changed evidence | Update overview | {N} new or changed sources since the current overview. |
| Job queued or running | Updating overview | The current overview remains available while the update runs. |
| Last update failed | Update overview | Last attempt failed; the previous overview is still current. |
| Conflicting human override | Review conflict | New evidence conflicts with a human-confirmed value. |

**Re-run analysis** and **Invoke Skill** are not used for this action.

### 9.2 Source freshness

Freshness is based on stable source identity and revision, not display timestamps alone.

Each source reference includes:

- Source type.
- Server-authoritative source id.
- Organization, account, and opportunity scope.
- Source revision or content hash.
- Source date.
- Availability state.
- Read mode.
- Optional locator.

The current state version stores the exact source manifest used. The API compares it with the current authorized source manifest.

Freshness states:

- up_to_date
- new_sources
- changed_sources
- removed_or_unavailable_sources
- unknown
- conflict

### 9.3 Async behavior

Update overview is a durable asynchronous job:

1. Browser requests an update for an authorized account and opportunity.
2. API resolves the organization and current authorized source manifest.
3. API enqueues an idempotent opportunity-state job.
4. Worker materializes only allowlisted evidence in an isolated workspace.
5. Runtime produces a typed state candidate and candidate provenance.
6. Trusted worker validates the schema and evidence references outside the runtime.
7. Worker writes a new immutable state version.
8. An atomic promotion step makes it current only if doing so cannot replace a newer version.
9. UI refreshes the effective state and deterministic change summary.

The current overview remains readable throughout. A failed update never blanks or partially mutates it.

### 9.4 Idempotency and concurrency

The idempotency fingerprint includes:

- Organization id.
- Account id.
- Opportunity id.
- Current source-manifest hash.
- State schema version.
- State-updater version.
- Model and runtime configuration.
- Active base state version.

Equivalent requests reuse the existing queued, running, or completed result.

Two concurrent updates must not allow an older source set or an older base version to replace a newer current version. The commit path must lock or compare the opportunity current-version pointer before promotion. A completed stale result may remain as non-current evidence, but it must not become current silently.

### 9.5 What changed

The application computes changes from typed state versions. It must not display an unvalidated model-written change narrative as the only explanation.

At minimum, v1 compares:

- Brief field knowledge state and value.
- Health indicator status.
- Risks by stable key.
- Actions by stable key.
- Technical-evaluation item status.
- MEDDPICC dimension status.
- Stakeholder role/disposition/engagement state.
- Source manifest additions, changes, and removals.

Each change contains the old value, new value, evidence reference, and confirmation mode. The UI can then render a short narrative such as:

- Security approver identified.
- Private networking added as a POV gate.
- Decision Process changed from Partial to Unknown because the prior statement is no longer supported.
- Source credentials marked Done by Gary.
- Three new transcripts included.

## 10. Canonical opportunity-state contract

### 10.1 Version model

Generated state is immutable and append-only.

A proposed **opportunity_state_versions** record contains:

| Field | Purpose |
|---|---|
| id | UUID |
| org_id | Non-null tenancy owner |
| account_id | Same-organization account |
| opportunity_id | Same-account, same-organization opportunity |
| base_version_id | State version current when update began |
| job_id | Durable update job |
| revision | Monotonic per opportunity |
| schema_version | Typed state schema |
| updater_version | Prompt/runtime contract or content hash |
| model | Authoritative model used |
| runtime_version | Authoritative runtime used |
| source_manifest | Stable identifiers and revision hashes only |
| source_manifest_hash | Idempotency and freshness input |
| state | Validated structured JSON |
| validation_status | valid or invalid; invalid state is never current |
| generated_by | Requesting member |
| generated_at | Timestamp |
| promoted_at | Timestamp if selected current |
| superseded_reason | Optional non-sensitive reason |

All same-organization relationships require composite foreign keys. The table has RLS. Normal requests use the existing app_user tenant context and never a browser-provided organization id.

A proposed **opportunities.current_state_version_id** pointer may select the current version. Promotion must be atomic and reject stale-base replacement.

### 10.2 Human overrides

Generated state and human corrections remain separate.

A proposed append-only **opportunity_state_actions** record contains:

| Field | Purpose |
|---|---|
| id | UUID |
| org_id | Non-null tenancy owner |
| account_id | Same-organization account |
| opportunity_id | Same-account opportunity |
| action | confirm, set, clear, mark_not_applicable, or resolve_conflict |
| field_path | Allowlisted typed path, not arbitrary query syntax |
| value | Typed JSON value for set or confirm |
| base_state_version_id | State version the user was viewing |
| supersedes_action_id | Prior action replaced by this action |
| reason | Optional plain-text explanation |
| source_ref | Optional authorized evidence reference |
| actor_id | Derived from authenticated membership |
| created_at | Timestamp |

There is no destructive overwrite. The effective read model overlays the latest active human action on the current generated version.

Precedence:

1. Active human-confirmed value.
2. Evidence-backed extracted value.
3. Unreviewed inference.
4. Unknown.

New evidence that conflicts with an active human-confirmed value does not overwrite it. The Overview shows a conflict that a user can resolve explicitly.

Field paths must be allowlisted and schema-validated. The browser cannot write arbitrary JSON paths or cross-opportunity references.

### 10.3 Claim shape

Every material field that is not raw CRM metadata uses a consistent claim envelope:

~~~json
{
  "knowledge_state": "known",
  "value": "Customer wants to centralize HR data in Databricks.",
  "confidence": "high",
  "confirmation": "evidence_backed",
  "last_confirmed_at": "2026-09-12T14:00:00Z",
  "evidence_refs": [
    {
      "source_type": "transcript",
      "source_id": "uuid",
      "locator": "speaker/time or line range"
    }
  ]
}
~~~

Allowed confidence values are low, medium, medium_high, and high. Confidence is not a substitute for knowledge state.

### 10.4 Top-level state shape

The typed state contains:

- schema metadata.
- opportunity brief.
- health indicators.
- risks.
- actions.
- technical evaluation.
- business case.
- MEDDPICC.
- stakeholders.
- evidence summary.
- source manifest.
- generated provenance.

Raw transcript text and output Markdown do not live in this JSON.

## 11. Aggregate read API

The page should receive one coherent, authorized read model rather than assembling security-sensitive joins in the browser.

Proposed hosted route:

**GET /api/hosted/accounts/{account_id}/opportunities/{opportunity_id}/workspace**

Response:

- account.
- opportunity.
- effective overview state.
- current generated state version metadata.
- freshness.
- change summary.
- technical-evaluation deterministic summary.
- latest output generation per skill.
- output history counts.
- validation and review states.
- running/recent jobs for this opportunity.
- evidence counts and recent activity.
- available actions based on mode and allowlist.

The route must:

- Resolve organization from the authenticated membership.
- Filter every query by organization, account, and opportunity.
- Return the same not-found response for missing, wrong-account, and cross-organization identifiers.
- Avoid returning raw transcript/output content.
- Avoid using app_admin, a service-role key, or user-provided org id.
- Keep list payloads bounded.
- escape or sanitize all user-visible text at the existing rendering boundary.

Supporting routes:

- **POST /api/hosted/accounts/{account_id}/opportunities/{opportunity_id}/overview-updates**
- Existing job-status routes for polling.
- **GET /api/hosted/accounts/{account_id}/outputs?opportunity_id={opportunity_id}**
- **POST /api/hosted/accounts/{account_id}/opportunities/{opportunity_id}/state-actions**
- **GET /api/hosted/accounts/{account_id}/opportunities/{opportunity_id}/state-history**

Exact paths may change during implementation, but the authorization and response boundaries must not.

For local mode, a parallel adapter may expose:

**GET /api/accounts/{account}/opportunities/{slug}/workspace**

It resolves account and opportunity safely through existing services and returns the same presentation contract where possible. Local-only fields and actions remain explicitly marked.

## 12. Generate contract

Generate creates specialized artifacts. It does not mutate the Overview synchronously.

The dropdown groups available skills by task:

- Prepare: Prep Call, Internal Prep.
- Process: Post Call.
- Qualify: Business Qualification, Technical Qualification, Deployment Model.
- Evaluate: Connector Feasibility, POC Plan, Worker Analysis.
- Advance: Deal Assessment, Next Move, Mutual Close Plan, ROI Business Case.
- Other approved local skills.

Availability is mode-specific:

- Local mode can expose the existing locally approved skill set and permission disclosures.
- Hosted mode exposes only the server allowlist. At the inspected baseline, post-call is the only approved hosted skill.
- A disabled hosted item must not imply it can run. Prefer hiding unavailable items or labeling them Local only in a non-actionable reference area.

Generate uses the existing durable job model. New outputs appear in Opportunity Outputs when complete.

## 13. Early-stage behavior

The page structure remains stable while knowledge grows.

Examples:

- Customer objective: Unknown — no discovery source has been added.
- Economic Buyer: Not identified.
- Technical architecture: Not established.
- Decision Process: Insufficient evidence.
- POV Readiness: Not ready — success criteria and deployment shape are not defined.
- Opportunity Outputs: No outputs yet.
- Recommended discovery: identify the business problem, decision owner, production timeline, and technical data path.

An early-stage opportunity may have a short Opportunity Brief and mostly collapsed empty frameworks. Empty sections should show one useful next action rather than large blank cards.

Do not calculate poor scores from missing evidence. Missing is not the same as negative.

## 14. Visual and interaction rules

- Reuse the Airbyte brand direction already present in the application.
- The latest reader redesign is scoped to **.doc-layout**. The Opportunity Overview must not depend on those scoped variables accidentally or change the reader while implementing this page.
- Prefer flat, editorial sections and hairline dividers over a wall of independent rounded cards.
- Preserve semantic status labels in addition to color.
- Use native button and disclosure semantics with keyboard support and visible focus.
- The top-level blocker summary remains visible while details are collapsed.
- Nested disclosures remember state only within the current page session in v1.
- Avoid horizontal overflow at 420 px, 780 px, and standard desktop widths.
- Loading uses section skeletons or a coherent page-level state; do not flash Unknown for data still loading.
- An aggregate-read failure shows a recoverable error and does not render fabricated empty state.
- Update and Generate jobs remain visible after navigation through the existing global job pattern.

## 15. Source authority and anti-recursion rules

Source authority order:

1. Human-confirmed state action.
2. Raw customer evidence: transcript, customer-authored note, approved CRM snapshot, uploaded document.
3. Verified product/reference evidence for Airbyte capability claims.
4. Human-reviewed and approved output content, when explicitly promoted.
5. Unreviewed generated inference.
6. Unknown.

Rules:

- An output being newer does not make it more authoritative.
- A generated output is surfaced as an artifact, not silently ingested as fact.
- If the state updater consumes a prior reviewed artifact, its source manifest must label it derived and retain the original raw evidence references when available.
- A correction inside an output does not automatically rewrite canonical state. The user must explicitly apply or confirm the fact in a later slice.
- Conflicting sources remain visible and reduce certainty.
- Product capability claims follow the existing repo/source verification rules and are not accepted solely because a transcript participant stated them.

## 16. Implementation sequence

### Slice 1 — Opportunity workspace shell and output launchpad

**Outcome:** Replace the local opportunity file-list landing experience with the agreed information hierarchy using only data that exists today.

Scope:

- Add an Opportunity Workspace aggregate service and route for local mode.
- Load server-authoritative opportunity metadata rather than trusting route display text.
- Restructure **pageOpportunity** into the new shell.
- Preserve Live Transcribe, Coverage Handoff, free-text invocation, and named-skill invocation as local-only actions.
- Rename the primary named-skill action to **Generate**.
- Add Opportunity Outputs grouped by skill with latest generation plus history count.
- Expose validation and review status separately in the list response.
- Show honest empty states for canonical sections that do not yet have state.
- Add collapsed placeholder shells only where they offer a next action; do not fake summaries.
- Do not add Update overview until there is a real state/update contract behind it. The header may show **Create overview — not yet available** only in a design/dev feature flag, not as a dead production control.
- Do not change hosted scope.

Why first: it provides immediate navigation and output value while keeping the persistence and agent boundary explicit.

Primary files likely affected:

- webapp/static/app.js
- webapp/static/style.css
- webapp/static/index.html cache-bust
- webapp/routes/accounts.py or a new opportunity-workspace router
- webapp/services/account_service.py or a new OpportunityWorkspaceService
- webapp/services/output_service.py
- focused local API/service/frontend tests
- webapp/README.md and webapp/SESSION-LOG.md

### Slice 2A — Typed canonical state and local Create Overview (implemented)

**Outcome:** Add the first real, versioned opportunity-state contract and asynchronous local-mode Create workflow without treating it as a Markdown output.

Scope:

- Add typed OpportunityState models and validators.
- Add immutable local state-version persistence under the resolved workspace as a local adapter only.
- Add a durable `opportunity_state_create` job kind with idempotency, interruption recovery, failure preservation, and order-independent evidence-manifest hashing.
- Generate typed state only from read-only trusted opportunity metadata and explicitly selected saved local transcripts resolved by opaque server identifiers.
- Run Claude Code 2.1.272 through a dedicated restricted/safe, no-tools, no-MCP, no-plugin, no-session executor in a new isolated temporary directory; evidence travels only through bounded stdin and returned JSON is independently Pydantic-validated.
- Add Create Overview empty/loading/running/failed/retry/completed states and evidence inspection.
- Keep output generation separate.
- Add adversarial state-parser, storage, path, evidence-change, restart, idempotency, concurrency, executor-boundary, route, aggregate, and frontend tests using synthetic fixtures.
- Document that local filesystem persistence is not production durability.

Explicit exclusions: no Update Overview, What Changed, human corrections/overrides, Tech Eval lifecycle state, hosted route, external evidence provider, Salesforce write, deployment, or customer-data fixture.

### Slice 2B — Local Update Overview and typed change history (deferred)

**Outcome:** Reconcile newly authorized evidence into later immutable versions and derive What Changed from typed versions.

Scope remains subject to separate approval. It includes freshness comparison, stale-base promotion rules, typed diffs, and any human correction/conflict behavior. Slice 2A Create refuses to run after a current version exists.

### Slice 3 — Technical Evaluation Lifecycle

**Outcome:** Turn the collapsed Tech Eval card into an evidence-backed, human-correctable operating plan.

Scope:

- Add the five-phase template and deterministic readiness evaluator.
- Add gate/required/recommended/optional semantics.
- Add manual confirm/set/not-applicable actions.
- Show evidence and confirmation source per item.
- Add conflict handling when a later update disagrees with a human state.
- Preserve historical actions.

This should be separate from Slice 2 because checklist workflow and human controls create a new write boundary.

### Slice 4 — Remaining overview frameworks

**Outcome:** Add Business Case, MEDDPICC, Stakeholders, and richer evidence drawers on the typed state contract.

Scope:

- Typed dimensions and validation.
- Nested disclosures.
- Explicit missing-data and suggested-discovery behavior.
- No opaque numeric scoring.
- Field-level evidence inspection.
- Human corrections with precedence and history.

### Slice 5 — Hosted opportunity workspace read model

**Outcome:** Bring the non-generative workspace shell and opportunity-scoped output launchpad to hosted mode.

Scope:

- Add the organization-scoped aggregate API.
- Add opportunity-filtered hosted outputs with review summaries.
- Reuse the existing hosted review reader.
- Preserve current post-call-only Generate allowlist.
- Add cross-organization, wrong-account, and wrong-opportunity tests.
- No hosted overview generation yet.

This can be moved before Slices 2–4 if the Product Owner prioritizes the hosted shell over the local state prototype.

### Slice 6 — Hosted canonical state and Update overview

**Outcome:** Persist canonical state in Postgres and run updates through the durable isolated worker.

Prerequisites:

- Product Owner approves this feature as an expansion beyond the current post-call hosted beta.
- Source set is approved.
- Runtime contract for the updater is reviewed.
- Postgres migration, RLS, storage, worker, audit, retention, and cost behavior are defined.
- Any additional external integrations use approved least-privilege per-user or organization credentials encrypted at rest.

Scope:

- Add state versions, state actions, current-version promotion, and RLS.
- Extend durable jobs with a discriminated opportunity-state result.
- Add isolated state runtime and trusted validation.
- Add audit coverage for update requests and manual state actions.
- Add concurrency, replay, mixed-version, cancellation, and recovery tests.
- Add hosted UI behavior and What changed.

## 17. Slice 1 acceptance criteria

### Behavior

- Opening a local opportunity lands on the Opportunity workspace, not a bare Generated Outputs list.
- Header shows account, opportunity, stage, amount, close date, and assignment only when the server returns them.
- Generate opens the existing invoke flow with the opportunity context.
- Existing local-only actions remain functional and visually secondary.
- The top of the page does not fabricate an Opportunity Brief before canonical state exists.
- Opportunity Outputs shows one latest card per skill plus the number of older generations.
- View all outputs exposes every generation newest-first within its skill.
- Opening an output preserves the current router/back behavior.
- A completed job refreshes the correct skill card and history count.
- A running job displays on the correct opportunity.
- An output with **opp_slug** for another opportunity is excluded.
- An account-level output is not silently attached to this opportunity.
- Validation status and review status display independently.
- No-output, malformed-sidecar, malformed-feedback, missing-file, and failed-job cases degrade safely.
- Empty sections explain what is missing and give one appropriate next action.
- Critical status is never represented by color alone.

### Security and robustness

- Account and opportunity inputs pass through existing safe-name and resolved-path checks.
- The aggregate route does not accept or trust a filesystem path.
- HTML and output titles are escaped through existing safe render boundaries.
- Malformed output metadata cannot break the page.
- List sizes are bounded or paginatable if the opportunity has many generations.
- No customer data is added to repository fixtures.
- Local-only actions remain unreachable when HOSTED_MODE is active.

### Tests

- Focused service tests for opportunity matching and output grouping.
- Route tests for normal, missing, malformed, and wrong-opportunity requests.
- Behavioral output review-state tests rather than source-string-only assertions.
- Browser test for Generate, Open output, Back, job refresh, disclosures, and empty states.
- Browser test at desktop, 780 px, and 420 px.
- Keyboard test for disclosures and Generate.
- Regression test that the document reader remains unchanged.
- Existing deterministic suite passes.
- Manual run uses the same launch command/environment the user actually uses, not the alternate PEP 723 environment noted in SESSION-LOG.
- **webapp/static/index.html** cache-bust is updated for JavaScript changes.
- **webapp/README.md** and **webapp/SESSION-LOG.md** are updated in the same change.

## 18. Canonical-state acceptance criteria for later slices

- A state version cannot reference a cross-organization account, opportunity, job, user, transcript, output, or source.
- Every new tenant table has non-null **org_id**, RLS, least-privilege grants, and composite same-organization foreign keys.
- Normal request paths use **app_user** and signed tenant context, not app_admin or service role.
- The browser cannot choose organization id, actor id, storage path, source path, or current version directly.
- Only validated state becomes current.
- An older or stale-base update cannot replace a newer current version.
- Duplicate update requests are idempotent.
- Cancellation, timeout, worker crash, model error, invalid state, and partial persistence preserve the previous current overview.
- State candidates with unknown fields, extra fields, duplicate stable keys, invalid evidence references, oversized content, malformed JSON, or unsupported schema versions fail validation.
- Human state actions are append-only, attributable, allowlisted, and schema-validated.
- A new model update cannot silently overwrite an active human-confirmed value.
- Clearing or superseding a human value remains reconstructable.
- What changed is derived from typed versions and includes provenance.
- Source-manifest changes are deterministic and do not rely only on filesystem mtime.
- State JSON contains no raw transcript body, output Markdown, credentials, signed URL, storage path, or secret.
- Logs and errors contain no customer content.
- Overview update usage and cost are recorded.
- Hosted sandbox receives only allowlisted files and tools and has no arbitrary egress.
- Tests cover traversal, cross-org access, spoofed ids, replay, concurrent updates, stale completion, malformed state, oversized input/output, source deletion, mixed schema versions, cancellation, restart, partial failure, and cleanup.
- Hosted/local parity is assessed on representative synthetic fixtures for required fields, missing-evidence behavior, source coverage, constraint preservation, and correction precedence.

## 19. Risks and pitfalls

### 19.1 Frontend-only synthesis

Risk: parsing the latest biz-qual, tech-qual, or POC Markdown in JavaScript is fast to prototype but creates a brittle implicit data model and makes layout changes change product truth.

Decision: do not use browser Markdown parsing for canonical state.

### 19.2 AI summarizing AI

Risk: Overview reads output A, skill B reads Overview, and the next Overview reads skill B. Inference becomes detached from raw evidence.

Decision: raw evidence and explicit human corrections remain primary. Outputs are surfaced, not automatically promoted.

### 19.3 Misleading readiness percentage

Risk: 85 percent ready looks positive while the missing 15 percent contains credentials and security approval.

Decision: hard gates override ratios. Always show blocker count and primary blocker.

### 19.4 Output history ambiguity

Risk: reruns and corrections are both labeled versions, making approval unclear.

Decision: call reruns generations and corrections versions. Approval always targets one exact correction version within one generation.

### 19.5 Account/opportunity leakage

Risk: an aggregate endpoint joins account-wide outputs or transcripts into the wrong opportunity.

Decision: use explicit account and opportunity scope in every query and relationship. Unscoped account artifacts remain separately labeled.

### 19.6 Hosted scope creep

Risk: a Generate menu visually promises local skills in hosted mode even though only post-call is approved.

Decision: availability is server-authoritative and mode-specific.

### 19.7 Existing docs/schema drift

Risk: planned fields in DATA_MODEL are assumed to exist even though migration 001 and OpportunityOut do not expose all of them.

Decision: migrations and exact code are implementation truth. New header fields require explicit schema/API work.

## 20. Product decisions required before Slice 2

The Product Owner should explicitly choose:

1. **First state implementation target**
   - Recommended: local Slice 1 shell, then shared typed contract with local state adapter.
   - Alternative: hosted shell first, with Update overview deferred.

2. **Initial update source set**
   - Recommended local set: opportunity-scoped transcripts, opportunity metadata, approved local notes, and verified reference data; surface outputs but do not ingest unreviewed output prose.
   - Hosted set must be separately approved and is initially limited by available uploaded evidence.

3. **Human control depth**
   - Recommended: start with Tech Eval item confirmation and high-value brief fields, then expand to arbitrary supported fields.

4. **CRM relationship**
   - Recommended: read-only snapshot in the first release. No Salesforce write-back.

5. **Account-level outputs**
   - Recommended: show them in a separately labeled group, never merge them into an opportunity automatically.

6. **Numeric scoring**
   - Recommended: no new composite score. Use categorical status and deterministic counts until a transparent rubric is approved.

## 21. Recommended next implementation brief

After this specification is accepted, the first Codex implementation task should be **Slice 1 — Opportunity workspace shell and output launchpad** only.

Codex must begin from the then-current main, inspect uncommitted changes, and restate the verified current behavior before editing. It should implement the existing-data shell, preserve local workflows, avoid fake overview content, add behavior-focused tests, update documentation, and report exact validation evidence. It should not add canonical state persistence, Update overview, a new skill, a hosted runtime, a database migration, a provider, or a deployment in that slice.
