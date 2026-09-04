---
name: post-call
description: Summarizes a customer call from a transcript and updates customer artifacts (Notion + local folder). Produces attendees, key takeaways, action items, follow-ups, new objections, and surfaces any Deal Assessment updates needed. Use when the user says "post-call", "summarize call", "call summary", "post call for X", or references a transcript that needs to be processed after a meeting.
---

# Post-Call Summary Skill

You are helping a Solutions Engineer at Airbyte process a customer call after it happened. Your job: turn a raw transcript into structured, actionable artifacts in the places the SE's workflow expects them.

## Input

The user will typically say something like "post-call for Acme" or "summarize the Acme call from yesterday". You should:

1. **Read SE identity config** from `config_file` (per playbook → Workspace Paths; per `_se-playbook.md` SE Identity section). Used for call attribution below.
2. **Find the transcript** in `{transcripts_dir}/` matching the customer name. File naming convention: `<Customer-Name>-MM.DD.YY.<ext>`.
   - Accepted extensions: `.txt`, `.rtf`, `.md`. For `.rtf`, strip RTF markup before reading.
3. **Resolution logic when multiple transcripts match:**
   - If user specified a date, use that specific file
   - If user did not specify, **default to the most recent transcript by date in filename** (not file mtime — filename date is more reliable)
   - State up front which file you're using: "Reading `Acme-04.01.26.txt` — most recent of 4 transcripts found"
4. If no matching transcript exists locally, **fall back to Gong** per `_se-playbook.md` Source Freshness Check (apply session-dedupe rule: check mtime ≤ 30 min before querying). Search narrowly for the most recent completed call on the account unless the user supplied an explicit date/call, which always wins. Persist the selected transcript to `_transcripts/` before using it, report which call was used, and ask for disambiguation when candidates are ambiguous. If Gong is unavailable or misconfigured, stop with a clear actionable failure; never treat partial data as valid.

## Source Coverage (mandatory, anti-hallucination)

**Read the FULL transcript before generating output.** Per `_se-playbook.md` "Source Coverage Transparency":

- Count total lines in the transcript file
- Read every line, not just the first N
- Include a **Source Coverage** section as the final section of the output, reporting line count read / total line count
- If you didn't read the full file, say so explicitly and re-read

Sample line:
> **Source Coverage:** Read `Max-Retail-05.06.26.txt` in full (612 / 612 lines). Cross-referenced SE config to determine attribution.

If the transcript is over ~2000 lines (rare but possible for long workshops), read in batches and confirm full coverage explicitly: "Read in 2 batches: lines 1-2000 + lines 2001-3247."

## Call Attribution

Per `_se-playbook.md` "Call Attribution" section. Determine whether the SE was on this call:

1. Get SE name + aliases from `config_file`
2. Scan the transcript for the SE's name (and aliases) appearing as a speaker or in introductions
3. Set attribution: **SE-attended** (SE name found) vs. **AE-led** (AE name found, SE name absent) vs. **Unknown**

This determines the framing of the coaching layer (see below).

### Output mode

By default, generate the full summary including the coaching layer.

If the user signals brief mode (`--brief`, `quick summary`, `just the takeaways`), produce a tight version: attendees, 3 takeaways, action items, next step. Skip coaching and MEDDPICC. See `_se-playbook.md` "Output Mode" for the unified brief-mode rule.

## What to Produce


> [!info] Canonical output architecture for `post-call`
> This skill follows the shared content-architecture contract. Produce the H2 sections below in this exact order; use H3 (`###`) for the listed sub-topics.
>
> **Canonical H2 order:**
> 1. `Key Takeaways`
> 2. `Deal Impact`
> 3. `Scope & Technical Changes`
> 4. `Objections & Open Questions`
> 5. `Actions & Next Step`
> 6. `Coaching Observations`
> 7. `Source Coverage`
>
> **H3 subtopics (when used):**
> - under `Call Snapshot`: Call Type, Call Date, Attendees, Action Items, Next Step, Deal-Assessment Update Needed, Customer Attendees, Airbyte Attendees, One-Line Deal Impact
> - under `Scope & Technical Changes`: Sources & Destinations, Technical Notes
> - under `Deal Impact`: Movement, Deal Health, MEDDPICC Changes
> - under `Objections & Open Questions`: New Objections / Concerns Surfaced, Open Questions / Follow-Ups
> - under `Actions & Next Step`: Action Items, Next Step
>
> Source Coverage must be the **final H2** when required. The profile-specific summary block is an H3 under the title block (not a navigable section).

Document structure follows `~/.claude/skills/_se-playbook.md` → Shared Skill Boilerplate → Output format reference.

---

<!-- output-template:start -->
# Call Summary: [Customer Name] — [Call Date in long form, e.g. June 11, 2026]
**Date:** [today's date, long form]
### Call Snapshot
- **Call type:** [Discovery / Technical / Exec / POC review / etc.]
- **Call date:** [long-form date]
- **Attendees:** [number and short roster summary]
- **Action items:** [count]
- **Next step:** [one line]
- **Deal-assessment update needed:** [yes/no — if yes, one line on what changed]
- **Customer attendees:** [names + roles]
- **Airbyte attendees:** [names + roles]
- **One-line deal impact:** [what this call changed in the deal]

**Jump to:** [Key Takeaways](#key-takeaways) · [Deal Impact](#deal-impact) · [Scope & Technical Changes](#scope-technical-changes) · [Objections & Open Questions](#objections-open-questions) · [Actions & Next Step](#actions-next-step) · [Coaching Observations](#coaching-observations) · [Source Coverage](#source-coverage)

*(Use the canonical sections below; retain all facts, tables, and reasoning while nesting sub-topics under the relevant H2.)*

## Key Takeaways


3–6 bullets capturing the most important things learned. Lead with what changed in your understanding of the deal, not a chronological recap. Mark each takeaway `[stated]` (cite the specific speaker by name/role — customer or Airbyte, either can be `[stated]`) or `[inferred]` (your read of the evidence) — never blend the two in one bullet, and never blend what two different speakers said into one claim (see `_se-playbook.md` → "Citation precision"). If the `[stated]` claim is about Airbyte's own product, architecture, or deployment model, check it against `_se-playbook.md` → "Product/architecture facts must be verified" before it becomes the takeaway's framing. A downstream skill (deal-assessment, tech-qual) will treat a `[stated]` fact differently from an `[inferred]` read.

## Deal Impact

### Deal Health
Quick read on what this call moved (or didn't):
- **Positive signals:** [what they said/did that's good]
- **Negative signals:** [hesitation, delays, scope shrinkage, etc.]
- **Recommended Deal Assessment update?** [yes/no — if yes, briefly say what changed]

> [!verdict] [Title the strongest positive signal — only if the call produced a genuinely strong positive]
> [The signal and why it moves the deal forward — e.g., EB confirmed budget, champion pushed timeline up. Omit if the call was neutral or negative.]

### Movement
Describe the deal-stage, momentum, and customer-commitment movement since the prior call; state what changed and why it matters.

### MEDDPICC Changes
*(AE-led discovery calls only — summarize each MEDDPICC letter with 🟢/🟡/🔴 status, the change this call produced, and whether that status is confirmed / partial / inferred / not discussed. A letter the call didn't address is "not discussed" — not 🔴 — an unaddressed topic is not a negative finding. Never mark Economic Buyer or Champion "confirmed" from a title or reporting line alone; that's `[inferred]` at best until the person's own behavior or statement confirms it (see `_se-playbook.md` → "Evidence thresholds"). Omit this subsection for SE-attended or unknown attribution.)*

## Scope & Technical Changes
### Sources & Destinations
*Include this section whenever the call named ANY system the customer wants to move data from or to. This is the single most reused fact downstream — `connector-feasibility` and `tech-qual` both build directly on it — so capture it as its own section, not buried in prose. Omit only for a purely business/exec call with zero systems mentioned.*

Capture each system verbatim as named, tagged as a **Source** (data comes FROM it) or **Destination** (data goes TO it). Note if a system's role is ambiguous or if it's a new mention vs. a prior call. Don't assess connector coverage here — that's `connector-feasibility`'s job; this is just the record of what was said.

| System | Role | Notes (version, hosting, new this call?) |
|---|---|---|
| [e.g. Salesforce] | Source | [as stated] |
| [e.g. Snowflake] | Destination | [as stated] |

- **Ambiguous / unconfirmed:** *(name each system mentioned without a clear source/destination role, and say in prose that it needs SE confirmation — don't wrap the note itself in brackets; see Self-check)*
- **Changed since a prior call:** *(list systems added, dropped, or re-scoped since the last call)*

> [!info] Feeds connector-feasibility & tech-qual
> Run or update `connector-feasibility` to check Airbyte coverage for these systems, and `tech-qual` to fold them into the canonical requirements. (Routed in "After Generating" below.)

### Technical Notes
*Include this section ONLY if the call surfaced technical scope beyond source/destination systems (volume, latency, deployment, auth, sizing/pricing). Omit entirely for a purely business/exec call.*

Capture the technical FACTS as stated on this call — raw, attributed, not synthesized. This is a record, not an analysis: don't build the full requirements matrix here (that's `tech-qual`'s job). Quote numbers and system names verbatim where load-bearing.
- **Volume / scale / frequency:** [figures stated — flag if they revise an earlier estimate]
- **Deployment / infra / security:** [constraints raised — on-prem, residency, VPC, SSO, KMS]
- **Sizing / pricing signals:** [data-worker count, enterprise connectors, capacity-vs-volume comments]
- **New technical risks or open questions:** [anything unresolved]

> [!info] Feeds tech-qual
> This call added technical scope. Run or update `tech-qual` to consolidate these facts into the canonical **Technical Requirements & Scope** section — don't let scope live only in scattered call summaries. (Routed in "After Generating" below.)

## Objections & Open Questions
### New Objections / Concerns Surfaced
Anything the customer raised that wasn't on your radar before the call — pricing, security, deployment model, competitor mentions, internal politics. **A question is not an objection** — if the customer asked something without stating a concern, pushback, or reservation, it belongs in Open Questions / Follow-Ups below, not here (see `_se-playbook.md` → "Evidence thresholds"). *(Placed high: a newly-surfaced objection is often the most important thing that changed, and it usually drives an action item below.)*

> [!risk] [Title the new objection — only if a genuinely new concern surfaced]
> [What they raised, who raised it, and the severity. Omit this callout if no new objection surfaced; if multiple, use one callout each for the material ones.]

### Open Questions / Follow-Ups
Questions the customer asked that weren't fully answered, or that you committed to follow up on. These should feed the customer's Notion `Q&A` page.

## Actions & Next Step
### Action Items
Markdown checklist. Each item: who owns it, what they're doing, by when (if stated). **Don't assign an owner or due date the transcript doesn't support — write `TBD` rather than guessing.**
- [ ] **[Owner, or TBD if not stated]** — [action] *(by [date if mentioned, else omit])*

### Next Step
The single most important next action. Be specific — "send POC proposal by Friday" not "follow up." **State whether this was actually agreed on the call (both sides committed) or is a recommended follow-up the SE hasn't proposed/gotten agreement on yet** — don't write a recommendation as though the customer already committed to it.

## Coaching Observations

Write 2–4 candid, personally actionable observations about how the SE/AE ran the call: what worked and what to change next time in the talk track, discovery technique, or demo pacing. Label each observation **[stated]** or **[inferred]**, and tie it to a concrete moment or behavior rather than grading the deal. Keep the language proportional to the evidence — a single ambiguous moment supports a mild, specific note, not a dramatic verdict.

## Source Coverage


Audit trail — final content section, after all analytical and coaching content (progressive disclosure per `_se-playbook.md`). List each transcript read (lines read / total), attribution determination, prior transcript or summary cross-referenced, memory file, Salesforce field or record, and any source requested but unavailable. Distinguish a full read from metadata-only inventory.
<!-- output-template:end -->

## After Generating the Summary

### Auto-save path
Per `~/.claude/skills/_se-playbook.md` → Shared Skill Boilerplate → After Generating (saving skills), save the output automatically to:
```
{customers_dir}/<Customer>/outputs/post-call/post-call-<YYYY-MM-DD>-<Descriptor>.md
```

Filename example: `post-call-2026-05-28-Tech-Discovery.md`.

### Self-check before save
(1) every action item has an owner or `TBD`; (2) every deal-health signal cites speaker + timestamp; (3) no attendee/company fact appears that isn't in the transcript; (4) the Sources & Destinations table matches what was actually said; (5) no bracketed `[flag-for-confirmation]`-style aside anywhere outside `[stated]`/`[inferred]` tags — if something needs the SE's attention (an unnamed tool, an unconfirmed detail, a date to fill in), write it as plain prose or move it to Open Questions / Follow-Ups, don't leave a `[bracketed note]` in the sentence. The validator treats any other bracketed text as an unresolved template placeholder and will reject the output. (6) **Every `[stated]` bullet, re-verified**: re-read its literal cited line(s) and confirm the sentence is what that one speaker said about that one subject — not a synthesis of two speakers or two subjects (see `_se-playbook.md` → "Citation precision"). This skill is unusually exposed to this failure because every call has multiple speakers and Key Takeaways / Technical Notes / Coaching Observations routinely cite several lines close together. (7) **Every claim about Airbyte's product/architecture/deployment model** (not the customer's environment) is checked against what's actually true before it's asserted — a participant's belief about the product is `[stated — belief]`, not verified fact; if it's wrong, add a `[correction]` per `_se-playbook.md` → "Product/architecture facts must be verified." (8) **Every material number, stakeholder conclusion, risk, action, and next step is traceable** to a speaker/line or explicitly labeled `[inferred]` — check each one against `_se-playbook.md` → "Evidence thresholds": no question read as an objection, no title read as a confirmed EB/champion, no invented competitor/budget/timeline/urgency/compelling event, no sizing/cost number without the inputs it needs, and nothing the call didn't address written as a negative finding instead of "not discussed." Mark each takeaway `[stated]` (customer said it) or `[inferred]` (SE read) — never blend. If a check fails, fix the summary before saving — don't persist a summary a downstream skill will treat as ground truth when it isn't.

### Then ask which other artifacts to update
1. **Update Notion** — create a new subpage under the customer's parent page named `<YYYY-MM-DD> — <Call Name>` with Attendees / Key Takeaways / Action Items / Follow-up Date sections. Also append new Q&A items to the customer's `Q&A` subpage.
2. **Propose memory update** — if call surfaced a material change (new blocker, stakeholder change, decision). Per conditional rule in earlier section.
3. **Suggest deal-assessment** — if call materially shifted deal health (not every call warrants this).
4. **Suggest connector-feasibility** — if the call named any new sources/destinations (the Sources & Destinations section is non-empty with systems not already checked). Recommend running or updating `connector-feasibility` to confirm Airbyte coverage for those systems.
5. **Suggest tech-qual** — if the call surfaced technical scope (the Technical Notes or Sources & Destinations section is non-empty). Recommend running or updating `tech-qual` so the facts land in its canonical **Technical Requirements & Scope** section. If a `tech-qual-*.md` already exists, frame it as an update (revised volume, new source, new constraint), not a fresh run.

Wait for explicit yes/no on Notion / memory / deal-assessment / tech-qual before doing those.

---

## Style (post-call skill guidance — not part of output template)

- Concise and scannable. This is a working doc, not a report.
- Pull direct quotes from the transcript when they're load-bearing (especially for objections and commitments).
- Flag inferred vs. stated. If you're guessing at intent, say so.
- Don't pad. If the call was short or low-content, the summary should be short too.
- Never invent action items or attendees not present in the transcript.

## Conventions to Respect

- Customer folder names use Title Case (e.g., `Build-Manufacturing`)
- Notion parent: under `AE Calls > Charlie` unless the user says Graham
- No emoji in Notion page titles
- Customer parent page in Notion has no content directly — always create a subpage
- Local files in `{customers_dir}/<Customer>/`, transcripts in `{transcripts_dir}/`

---

## SE Best Practices Applied to Post-Call

Read `~/.claude/skills/_se-playbook.md` for full framework details. Apply to post-call analysis:

### Memory Check
Read `memory_dir` `MEMORY.md` and any customer-specific memory files before summarizing (skip gracefully if `memory_dir` is unset). Active blockers and prior context shape how to interpret what was said — but keep that external account context visibly separate from this call's confirmed findings; cite memory/prior-call content by its own source rather than folding it into a `[stated]` claim about this call. Per `_se-playbook.md` ("Memory Check").

**After the summary, propose memory updates only if warranted:**
- ✅ Propose update if: new active blocker, stakeholder change, deal-status change (e.g., POC paused, deal at-risk), material commitment from either side
- ❌ Skip if: routine progress check-in, no new information beyond what's already in memory, just incremental status

Don't ask the SE to update memory after every call — only when the call moved something meaningful.

### Source Freshness Check (Gong Fallback)
Per `_se-playbook.md` ("Source Freshness Check"): if no matching transcript is in `_transcripts/`, or the user references a specific call that isn't local, fall back to Gong before asking the user to pull it manually.
- Search Gong via `search_calls` with account + date filters (most recent completed call when no explicit date/call was supplied)
- Pull the specific call only — do not bulk-pull; ambiguous candidates require disambiguation
- Save to `{transcripts_dir}/<Customer-Name>-MM.DD.YY.txt` BEFORE using it (per CLAUDE.md)
- Report which call was used. If Gong is unavailable or misconfigured, stop with a clear actionable failure; never use partial data as valid. If the requested call isn't in Gong either, say so and ask the user for clarification

### Apply Cross-Transcript Analysis
If prior transcripts or call summaries exist for this customer, read the "Cross-Transcript Analysis" section in `_se-playbook.md` and apply it. Specifically:
- Read at least the most recent prior transcript (and earlier ones if topics from this call have history)
- Cross-reference topics: did anything in this call contradict or evolve from a prior call? Classify as Evolution / Stakeholder split / Walking it back
- Flag topics that went quiet — themes from prior calls that didn't come up this time
- Cite prior-call sources when surfacing contradictions

### MEDDPICC scoring — CONDITIONAL on call type

**Only include MEDDPICC Movement for AE-led discovery calls** (which feed into the SE's prep for follow-up tech calls). Full MEDDPICC scoring belongs in `biz-qual`, not post-call.

Decision logic:
- **AE-led discovery call (SE not on the call):** Under `## Deal Impact`, add a `### MEDDPICC Changes` subsection with brief 🟢/🟡/🔴 status per letter — enough signal to feed `prep-call` for the SE's follow-up. Don't run the full pain funnel or champion test here; that's biz-qual's job.
- **SE-attended call (tech-discovery, deep-dive, exec readout, POC review, etc.):** Skip MEDDPICC scoring entirely. Focus on call-specific data (attendees, takeaways, action items, objections, next step). Anyone who needs MEDDPICC scoring should run `biz-qual` directly.
- **Unknown attribution:** Skip MEDDPICC by default; note "MEDDPICC skipped — call attribution unclear."

### Coaching layer — framed by call attribution

Add a `## Coaching Observations` section (H2, so it lands in the Jump-to index) immediately **before the final `## Source Coverage` section**. **Framing depends on call attribution** (per `_se-playbook.md` Call Attribution):

**If SE was on the call (SE-attended):**
Frame as "what to do differently next time." Direct critique of the SE's moves:
- Happy ears moments where the SE heard "this looks great" without anchoring a next step
- Feature dumping past stated pain into a tour
- Skipped Implication — pain points the customer raised, SE didn't quantify ("X is a problem" without "what does X cost you?")
- Weak next-step — "we'll follow up" instead of date+attendees+agenda
- Solution-pitching too early

**If AE-led (SE not on the call):**
Frame as "context to share with the AE" or "things to address on your next call." Examples:
- AE skipped Implication on stated pain X — bring it up on your tech call ("when [pain] happens, what does it cost?")
- AE didn't pin EB — first question on your tech call should test who signs
- AE accepted "this looks great" without testing — your tech call should validate with quantified pain

**If attribution unknown:**
Output a minimal observations block: "Attribution unclear — review with care; coaching frame omitted."

This section is for SE growth or AE collaboration, not for the customer-facing Notion page. Keep it candid.

### Identify the "no" status
Per Sandler/Voss: a real "no" is more valuable than a fake "yes." If the call ended with vague positivity, flag it as a happy-ears risk. If the customer pushed back on something specific, flag that as healthy signal.

### Strengthen the Next Step
The "Next Step" section in the summary must be concrete: who, what, when, agenda. If the call didn't produce one, propose one the SE should drive via follow-up email.

### Surface Reframe opportunities (Challenger)
If the customer revealed a belief about their own business that Airbyte data could refute (e.g., "we just need more connectors", "we'll build it ourselves cheaper"), flag it as a Reframe opportunity for the next call.

### Anti-patterns to avoid in this skill
- Generic "customer seems interested" assessments without evidence
- Action items invented to fill space rather than pulled from the transcript
- Deal health signals that don't translate to specific next-call moves

---

## Changelog

- **2026-09-04** — Fact-vs-assumption hardening pass (no structural change — canonical sections, order, and save behavior are unchanged): broadened `[stated]` in Key Takeaways to any named speaker (not just the customer) with a pointer to verify Airbyte's own product claims; MEDDPICC Changes now requires a confirmed/partial/inferred/not-discussed label per letter and forbids inferring Economic Buyer/Champion from a title or reporting line alone; New Objections now explicitly excludes bare questions (route to Open Questions instead); Action Items/Next Step now require `TBD` over a guessed owner/date and require stating whether a next step was actually agreed vs. is a recommended-but-unagreed follow-up; Coaching Observations now asks for language proportional to the evidence. Added self-check (8) tying all of this to the new `_se-playbook.md` → "Evidence thresholds" section.
- **2026-09-03** — Fixed a `[stated]` misattribution found in an Ista call summary: a bullet merged a customer's comment about *Airbyte's* managed-region availability with an AE's separate comment about Flex architecture into one `[stated]` "customer requires AWS" claim — reversing the actual finding (the customer's own infra is Azure, and the AE later confirmed Flex's value was specifically EU-on-Azure) and citing lines that didn't actually say what the sentence claimed. This is a cross-cutting fix, not post-call-only: added "Citation precision" and "Product/architecture facts must be verified" to `_se-playbook.md` → "Confidence & Assumptions (all skills)" — the canonical, skill-agnostic definition of `[stated]`/`[inferred]` every skill inherits. Added post-call-specific self-check items (6)/(7) here since this skill is the most exposed (every call has multiple speakers, and Key Takeaways/Technical Notes/Coaching Observations cite adjacent lines constantly).
- **2026-09-03** — Fixed a recurring false "Output incomplete" flag: (1) the Source Coverage validator required a literal `N / N lines` count, which a Gong-pulled transcript never has (no native line numbering) — every Gong-sourced call (increasingly the common path when no local transcript exists) failed validation even when fully read. Validator now also accepts a Gong call ID + explicit full/complete-read claim. (2) The Sources & Destinations template modeled "flag this for the SE" as a bracketed clause, which outputs then echoed as a general-purpose bracket-note style elsewhere in the doc (e.g. `[other tool]`, `[specific day]`) — indistinguishable from an unfilled template placeholder to the validator. Reworded the template to avoid bracket-wrapped instructional asides and added an explicit self-check item against bracket-style flags outside `[stated]`/`[inferred]`.
- **2026-07-10** — Repointed hardcoded `~/airbyte-work/` paths to the workspace-path resolver (`{customers_dir}`/`{transcripts_dir}`/`{notes_dir}`/`config_file`/`memory_dir`) per playbook → Workspace Paths. Portable across SE machines.
- **2026-07-09** — Genericized hardcoded "Gary" SE-identity prose → "the SE" (intro + memory-ask + next-step notes).
- **2026-07-09** — Resolved save-behavior contradiction (local auto-save ON; Notion/memory ask-first — was a head-on conflict with "do NOT auto-write local files"). Added a pre-save self-check (owners, cited signals, no ungrounded facts, S&D matches transcript) + `[stated]`/`[inferred]` labeling on takeaways so downstream skills don't treat an SE read as ground truth.
- **2026-07-07** — Promoted sources/destinations from a single bullet in Technical Notes to a dedicated **Sources & Destinations** section (table: system · role · notes) — the single most reused fact downstream. Added to Jump-to index; routes to `connector-feasibility` + `tech-qual` in After Generating.
- **2026-06-18** — Output adopts the shared Output Document Format (_se-playbook.md): At-a-Glance + Jump-to index, H2-per-section, callouts, ==key== emphasis.

- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Explicit transcript resolution (most recent by filename date), RTF support, Gong fallback for missing transcripts. Brief mode. Conditional memory updates (only on material changes). MEDDPICC movement scoring + coaching layer ("What Could Have Gone Better"). Cross-Transcript Analysis applied.
- **2026-05-27** — Initial scaffold.