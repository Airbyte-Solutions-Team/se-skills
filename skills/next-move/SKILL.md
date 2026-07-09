---
name: next-move
description: Determines where a customer sits in the SE workflow and recommends which skill to invoke next. Inspects local artifacts, transcripts, and memory to infer deal stage, surface gaps, and suggest the 1-3 highest-value next moves. Use when the user says "where am I on <customer>", "what's next for <customer>", "next move on <customer>", "route <customer>", "what should I do next on <customer>", or "workflow for <customer>".
---

# SE Workflow Router Skill

You are helping a Solutions Engineer at Airbyte decide what to do next on a specific customer. Your job: inspect what's already been done, figure out where the deal is in the SE workflow, and recommend the most valuable 1-3 next moves.

This skill **does not generate customer-facing content.** It routes Gary to the right downstream skill. Output is a short navigation doc, not a deliverable.

## Input

The user will name a customer (e.g., "where am I on Acme", "next move for Build-Manufacturing"). Required input is just the customer name — everything else is inferred.

If no customer is named, ask before proceeding.

## Output mode

Default = full router output with stage inference, gap analysis, and top 3 recommendations.

If user signals brief mode (`--brief`, `just tell me what's next`, `one recommendation`): produce just the inferred stage + the single top recommended skill + one sentence why. See `_se-playbook.md` "Output Mode" for the unified brief-mode rule.

## Read-Depth Contract (Important — Anti-Hallucination)

The router operates at a **deliberately shallow read depth**:

- ✅ **Reads in full:** prior qualification docs (`biz-qual`, `tech-qual`, `deployment-qual`, `connector-feasibility`, `poc-plan`, prior Deal Assessment), customer-specific memory files, the most recent transcript only
- ✅ **Reads metadata:** filenames, dates, and line counts of older transcripts and other files
- ❌ **Does NOT read in full:** older transcripts (older than the most recent), large raw notes, prior next-move outputs

**Why this design:**
- The router's job is to decide *which skill to invoke next*, not to do the deep synthesis itself
- Deep reads of all transcripts belong in the downstream skill (e.g., `deal-assessment` reads all transcripts in full per its own rules)
- A fast, shallow router that recommends the right deep-read skill beats a slow router that does the deep read itself

**Be explicit about this in the output's Source Coverage section.** State which files were read in full vs. inventoried by metadata only. Don't claim depth you didn't deliver — if the user wants deeper analysis, the recommendation should be "run `deal-assessment` next, which will read all 4 transcripts in full."

## How to Inspect

Read these in order. Don't synthesize until all are read.

### 1. Local artifacts
Check `~/airbyte-work/01-customers/<Customer>/` for:
- `deployment-qual-*.md` — deployment model qualified?
- `biz-qual-*.md` — MEDDPICC scored?
- `tech-qual-*.md` — technical fit assessed?
- `connector-feasibility-*.md` — coverage analyzed?
- `poc-plan-*.md` — POC scoped?
- `Deal-Assessment-*.md` — recent honest read on health?
- `call-summary-*.md` files — post-call digests
- `call-prep-*.md` files — call prep docs
- `emails/` — sent communications
- Any raw notes

For each, note the **date** (from filename). Stale artifacts (>30 days old) are treated as incomplete.

### 2. Transcripts
Check `~/airbyte-work/01-customers/_transcripts/` for files matching the customer. Note:
- Total count
- Date of most recent
- Days since most recent (vs. today)

### 3. Memory
Check `~/.claude/projects/-Users-gary-yang-airbyte-work/memory/` for any project memory files matching the customer. Specifically look for active blockers, pending Airbyte-side actions, or status flags.

### 4. Notion (optional)
Don't auto-query Notion. If the user explicitly asks for "deeper context" or if local artifacts are very thin, use `Notion:search` to find the customer's parent page.

### 5. Gong (optional, conditional)
Apply the Source Freshness Check from `_se-playbook.md`:
- If most recent local transcript is >14 days old, check Gong for newer activity
- A Gong-confirmed silence (no calls in 30+ days) is itself a routing signal — likely route to `deal-assessment` to diagnose decay

### 6. Salesforce (active opp only — light snapshot)
Per `_se-playbook.md` "Salesforce Enrichment." Pull the **active opportunity** (matching rule: most recent open, exclude renewals unless only-open) — not the full account arc (router stays fast). Fields:
- `StageName`, `Amount`, `CloseDate`, `Owner.Name`
- `SE_Engaged__c`, `SE_Name__c` — **best call-attribution signal available** (better than scanning transcripts)
- `Days_Since_Last_Activity__c`, `Last_Stage_Change_Date__c` — silence / stuck detection (often more current than local transcript dates)
- `At_risk__c`, `Next_Step_Date__c` — flagged risk + real next step?

**Routing use:** SFDC `StageName` is the AE's answer to "where is this deal?" The **mismatch between SFDC stage and local artifact state is a primary routing signal** — flag it assertively. Examples:
- SFDC = Closed/Lost but no local artifact reflects it → "your local view is stale; the deal is dead"
- SFDC = Negotiation but `SE_Engaged__c` = false and no POV → "AE is ahead of technical reality; SE engagement gap"
- SFDC `Days_Since_Last_Activity__c` contradicts your local silence read → trust the more recent

Add SFDC stage + the mismatch finding to the Inferred Stage section. If SFDC unavailable, skip per the graceful-degradation rule.

---

## How to Infer Deal Stage

Use this decision tree. **Critical rule: never recommend a skill that would refuse to run due to missing call data.** See `_se-playbook.md` "Skill Sequencing Rules" for the hard prerequisites.

```
No customer folder, no transcripts
  → Stage: NEW PROSPECT
  → ONLY valid recommendation: prep-call
  → DO NOT recommend biz-qual, tech-qual, deployment-model-qual, poc-plan, or deal-assessment yet
     (all of those require transcript data — they'll refuse to run)

Customer folder exists, but no deployment-qual
  → Stage: EARLY DISCOVERY
  → Likely recommendations: prep-call (if call upcoming), deployment-model-qual (gate, IF transcripts exist)

deployment-qual exists but no biz-qual or tech-qual
  → Stage: POST-DEPLOYMENT-QUAL
  → Likely recommendations: biz-qual, tech-qual (in parallel)

biz-qual + tech-qual exist, no connector-feasibility
  → Stage: TECHNICAL VALIDATION
  → Likely recommendation: connector-feasibility

biz-qual + tech-qual + feasibility exist, no poc-plan
  → Stage: POC SCOPING
  → Likely recommendation: poc-plan

poc-plan exists, deal is mid-cycle
  → Stage: ACTIVE POC / MID-CYCLE
  → Likely recommendations: post-call (after any new call), deal-assessment (every 2 weeks)

Any stage + recent silence (>21 days since last transcript)
  → Stage: STALLED — OVERRIDE
  → Top recommendation: deal-assessment (diagnose decay)
  → Secondary: follow-up-email (Sandler negative reverse nudge)

Any stage + active blocker in memory
  → Stage: BLOCKED — OVERRIDE
  → Top recommendation: address the blocker (often Slack/email to internal team)
  → Then: follow-up-email to customer once blocker resolved

Any stage + objection raised on most recent call
  → Stage: ANY + ACTIVE OBJECTION
  → Add: objection-handler to the recommendations
```

**Override priority order** (when multiple apply):
1. Stalled (silence override)
2. Blocked (memory blocker override)
3. Stage-based recommendation
4. Active objection (add-on, not override)

**Conflicting signals — name the tension, don't force one tree path.** When signals genuinely collide (e.g. all quals exist but are >30 days stale AND a fresh objection landed; or SFDC stage says POC-scoping but the local artifacts say early-discovery), don't silently pick one branch. In the `Current read`, name the tension in one line and choose the move that **de-risks most**, stating the assumption behind the pick — e.g. "Stage says POC-scoping, but a 60-day-old biz-qual + a fresh pricing objection outrank that — recommend `deal-assessment` first to re-baseline, then `objection-handler`. Assumes the objection is real, not a negotiating feint." The recommendation still resolves to ONE top move in the At-a-Glance (keep those labels exactly — the reader keys on them); the conflict handling lives in the prose, not in new card fields.

---

## Output Format

*Lead with an H1 title (the web app reader uses the H1 as the page title), then a lightweight **At-a-Glance decision card**. next-move is **decision-first**: the recommendation is the lead, the audit trail is the tail. The web-app reader promotes the At-a-Glance block into a prominent hero card above the body, so the labels below are chosen to render as decision tiles — keep the wording.*

---

# SE Workflow: [Customer] — [Inferred Stage]
**Date:** [today, long form] · **Stage:** [inferred stage] · **Days since activity:** [N] · **Sources:** [N] transcripts, [N] qual docs, memory [yes/no]

### At a Glance
- **Recommended Next Move:** [the ONE skill/action — e.g. "Run `deal-assessment`"]
- **Confidence:** [High / Medium / Low] — [what it's pending on, one clause]
- **Stage:** [🟢/🟡/🔴 + inferred stage]
- **Top Blocker:** [🔴 the single biggest thing in the way, or "None — clear runway"]
- **Effort:** [quick / moderate / depends on source coverage]
- **Expected output:** [what artifact the top move produces]

### Current read
[The TL;DR as 1–2 sentences of PROSE (not bullets): "[Customer] is in [stage]; the highest-value move is [X] because [why]. [Do-not caveat if any]." This is the one-liner a user copies into Slack — keep it tight and self-contained.]

---

> [!blocker] 🔴 Stalled / Blocked     ← use `[!blocker]` for a hard stall/block
> 🔴 Stalled — [X days since last activity]
> 🔴 Blocked — [memory cite + specific blocker]

> [!risk] 🟡 Active objection     ← use `[!risk]` for an open objection
> 🟡 Active objection — [from transcript]

*(Only render the override callouts that actually apply; omit this block if none. These feed the reader's Top-Risks strip automatically.)*

---

## Why This Move
- **Rationale:** [1–2 sentences tying the top move to the specific gap or override it resolves]
- **What it unblocks:** [the downstream work this enables]

---

## Ranked Next Moves

**1 · `[skill]` — [headline reason]**
- **Priority:** High
- **Why now:** [1–2 sentence rationale tied to the gap or override]
- **Inputs needed from Gary:** [anything not already in the workspace, or "None"]
- **Expected output:** [what artifact this produces]
- **Effort:** [quick / moderate / depends on source coverage]

**2 · `[skill]` — [headline reason]**
- **Priority:** Medium
- [same structure]

**3 · `[skill]` — [headline reason]**
- **Priority:** Medium
- [same structure]

*(Each move is a lead-bolded bullet so the reader renders it as a scannable card. Keep 1–3 moves — don't pad to three if only one or two are real.)*

---

## Don't Do Yet
*Skills tempting but premature given current state (missing ≠ needed):*
- **`[skill]`.** [Why not yet — e.g. "No deployment qualification yet; scoping `tech-qual` now risks an air-gap customer who can't use Cloud."]

*(Lead-bold the skill. Do NOT put the word "risk" in this section's heading or bullets' lead — that would wrongly feed the Top-Risks strip.)*

---

## Workflow State
- **Inferred stage:** [from decision tree]
- **Reasoning:** [1–2 sentences citing the specific artifacts/transcripts that put them here]
- **SFDC vs. reality:** [the mismatch finding, if any — flag it assertively; omit the line if SFDC and local state agree or SFDC is unavailable]

---

## Context Inventory
*What context exists to ground the recommendation. `Needed Now?` separates a true gap (**Yes**) from a not-yet artifact (**Later**) or an irrelevant one (**No**) — so missing ≠ todo.*

| Artifact | Status | Date | Needed Now? |
|----------|--------|------|-------------|
| deployment-qual | ✅ Present / ❌ Missing / ⚠️ Stale (>30d) | [date or n/a] | Yes / Later / No |
| biz-qual | ✅ / ❌ / ⚠️ | | |
| tech-qual | ✅ / ❌ / ⚠️ | | |
| connector-feasibility | ✅ / ❌ / ⚠️ | | |
| poc-plan | ✅ / ❌ / ⚠️ | | |
| Deal Assessment | ✅ / ❌ / ⚠️ | | |
| Most recent call summary | ✅ / ❌ | | |
| Most recent transcript | | [date] | |

---

## Gaps
*What's missing that should exist at this stage, sorted by whether it blocks the next move:*

**Critical (blocks the next move):**
- [ ] [Missing artifact that gates progress — e.g., "No Deal Assessment; a stalled ~$110K deal has no honest health read"]

**Non-critical (nice to have soon):**
- [ ] [Missing artifact that doesn't block — e.g., "No tech-qual; technical scope scattered but not gating the close"]

**Data hygiene:**
- [ ] [Stale / duplicate / misfiled artifact or CRM field to clean up]

*(Omit any subgroup that's empty.)*

---

## External Actions
*Things to do that aren't a skill — but matter for moving the deal. Owner is a real person's name or **TBD** — never fabricate a name.*

| Action | Owner | Why it matters | Definition of done |
|--------|-------|----------------|--------------------|
| [e.g. Slack Airbyte eng re: pending blocker] | [name or **TBD**] | [deal impact] | [what "resolved" looks like] |
| [e.g. AE/RevOps — fix SFDC fields] | [name or **TBD**] | [forecasting accuracy] | [fields updated] |

*(Only include this section if external actions actually apply — don't fabricate them. For a healthy mid-cycle deal, replace the table with: "No external actions needed — workflow is internally driven.")*

---

## Source Coverage
*Per the Read-Depth Contract: report what was read in full vs. inventoried by metadata only.*
- **Read in full:** [qual docs, memory files, most recent transcript — filenames + dates]
- **Metadata only:** [older transcripts, large raw notes — filenames + line counts]
- **Memory:** [records read, or "none matched"]

---

## Style

- **Be specific.** "Run biz-qual" is weak. "Run biz-qual — we have 4 transcripts and a deployment-qual but no MEDDPICC scoring; without that the POC scope is built on guesses" is strong.
- **Bias toward the smallest next move that unblocks the most downstream work.** Don't recommend 3 parallel skills when 1 sequential one will produce the prerequisites for the others.
- **Surface staleness honestly.** A 60-day-old biz-qual is functionally missing. Flag it as ⚠️ Stale and recommend refresh.
- **Don't recommend deliverable skills (follow-up-email, objection-handler) without a clear trigger.** Those are tactical — only suggest them if the source material gives a reason (a question to answer, an objection to handle, a silence to break).
- **Surface "do nothing yet" as a legitimate recommendation** when warranted (e.g., waiting on a customer commitment, internal blocker that hasn't been resolved).

---

## After Generating

### Auto-save (default)

Save the routing recommendation as an output file so it shows up in the web app's Generated Outputs (and the completion toast can deep-link to it). Per `_se-playbook.md` "Output Persistence (Auto-Save)", write to:
```
~/airbyte-work/01-customers/<Customer>/outputs/next-move/next-move-<YYYY-MM-DD>.md
```
When invoked for a specific opportunity, save under that opp's folder instead:
```
~/airbyte-work/01-customers/<Customer>/opportunities/<opp-slug>/outputs/next-move/next-move-<YYYY-MM-DD>.md
```
Append `-v2` etc. for same-day re-runs. User can suppress with `--no-save`.

> **Why this changed (2026-07-01):** next-move was previously chat-only/ephemeral. In the web app that meant an invoke finished but produced **no file** — the recommendation "disappeared" with nothing in Generated Outputs to open. Saving a dated file fixes that; the doc is still a point-in-time read (it's a snapshot of the workflow state when generated, not a living artifact — the dated filename makes its staleness obvious).

### Source Coverage

Fill in the **Source Coverage** section at the bottom of the template: artifact inventory (filenames + dates), transcript line counts (with explicit "read in full" vs. "metadata only" per the Read-Depth Contract above), memory records read. It's the last section by design — the reader collapses it (audit trail, not the lead).

### Then ask the user

1. **Invoke the #1 recommended skill** now (chain into it)
2. **Nothing more** — user just wanted the read

Don't auto-invoke downstream skills without confirmation — the user may have context that overrides the recommendation.

---

## SE Best Practices Applied

Read `~/.claude/skills/_se-playbook.md` for full framework details.

### Honest stage inference, not optimistic
A deal with 4 transcripts and no MEDDPICC is *not* in "advanced discovery" — it's in "POC-scoping with happy ears." Diagnose what's actually there, not what Gary wishes were there.

### Silence is signal (Sandler / Cross-Transcript Analysis)
Long gaps between transcripts are not "the customer is thinking it over" by default. Per the SE playbook, silence on a deal where the forcing function is on the seller is decay. Route to `deal-assessment` to diagnose.

### Surface walking-it-back signals as override
If prior transcripts show a stakeholder softening commitment over time (per Cross-Transcript Analysis), even if all artifacts exist, that's an override → route to `deal-assessment` and `follow-up-email` (negative reverse).

### Don't push deliverable skills prematurely
The router can recommend `follow-up-email` or `objection-handler`, but only when:
- A specific question/objection exists in the source material
- A silence needs breaking
- An action item is pending

Avoid "run follow-up-email because it's been a while" without a substantive trigger.

### Anti-patterns to avoid in this skill
- Recommending all 5 qualification skills simultaneously when 1 is the prerequisite
- Treating an old artifact as "done" without checking freshness
- Inferring stage from gut feel instead of artifact inventory
- Skipping the Stalled and Blocked overrides — those are the most important routing decisions
- Auto-invoking downstream skills without Gary's confirmation

---

## Changelog

- **2026-07-09** — Added a **conflicting-signals branch** to the override logic: when signals collide (stale quals + fresh objection, or SFDC-stage vs local-artifact mismatch), name the tension in `Current read` and pick the de-risking move with its assumption stated, rather than forcing one tree path. Handled in prose only — the four At-a-Glance labels (`Recommended Next Move`/`Confidence`/`Stage`/`Top Blocker`) the web-app reader keys on for hero-card routing are unchanged.
- **2026-07-02** — **Decision-first output rewrite.** For a "what do I do next?" skill, the recommendation was buried below an Artifacts Inventory + diagnostics — it read like an audit report. Reordered the template to lead with the answer: **At-a-Glance decision card → Current read → override callouts → Why This Move → Ranked Next Moves → Don't Do Yet → Workflow State → Context Inventory → Gaps → External Actions → Source Coverage.** The At-a-Glance labels (`Recommended Next Move`/`Confidence`/`Stage`/`Top Blocker`) are chosen to render as the web-app reader's hero decision tiles; `### Current read` becomes the hero's narrative one-liner (relocated from the old bottom TL;DR — no duplicate). Renamed "Artifacts Inventory" → **Context Inventory** with a `Needed Now?` column (missing ≠ todo); split "Gaps" into Critical / Non-critical / Data-hygiene; "Ranked Next Moves" render as cards; External Actions is now an Owner/Why/Definition-of-done table. next-move is no longer "light-touch" — it now leads with a Decision Card (reader change: `EXEC_SECTION` card routing + Low-confidence tile color in `webapp/static/app.js`). Playbook reclassification updated to match.

- **2026-07-01** — **Now auto-saves** (reversing the 2026-05-28 exemption). Chat-only output meant a web-app invoke finished with no file, so the recommendation vanished with nothing in Generated Outputs to open. Saves a dated snapshot to `outputs/next-move/next-move-<YYYY-MM-DD>.md` (opp-scoped when invoked for an opportunity); `--no-save` suppresses. Still a point-in-time read — the dated filename signals staleness. Playbook exemption + CLAUDE.md folder structure updated to match.

- **2026-06-18** — Callouts per `_se-playbook.md` → Output Document Format (next-move is light-touch: no At-a-Glance/Jump-to). Wrapped the top Recommended Next Move in an `[!info]` callout; the Inferred-Stage overrides (🔴 Stalled/Blocked → `[!blocker]`, 🟡 Active objection → `[!risk]`) now render as callouts. Artifacts Inventory table and Recommended-Next-Moves structure unchanged.

- **2026-05-28** — Salesforce enrichment added (reads from sf-mcp via mcp__salesforce__run_soql_query). Pulls AE-view MEDDPICC / technical / forecast fields per the playbook field map; assertive SFDC-vs-reality mismatch flagging; graceful degradation if SFDC disabled. Org alias + query dir from .se-config.yaml.

- **2026-05-28** — **Exempted from auto-save.** Router output is ephemeral (stale within hours of being read); persisting it adds noise without future value. Default = chat output only; save only on explicit "save this" request.
- **2026-05-28** — Added Read-Depth Contract section: router reads qual docs + memory + most recent transcript in full; older transcripts read by metadata only. Surfaced explicitly in output's Source Coverage section. Deeper reads belong in downstream skills like deal-assessment.
- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Initial creation. Inspects local artifacts, transcripts, memory, optionally Gong. Stage inference decision tree with Stalled / Blocked / Active Objection overrides. Refuses to recommend skills that would refuse to run due to missing prerequisites. Top 3 recommendations with "what NOT to do yet" section. Brief mode (1 recommendation only).
