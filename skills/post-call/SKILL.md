---
name: post-call
description: Summarizes a customer call from a transcript and updates customer artifacts (Notion + local folder). Produces attendees, key takeaways, action items, follow-ups, new objections, and surfaces any Deal Assessment updates needed. Use when the user says "post-call", "summarize call", "call summary", "post call for X", or references a transcript that needs to be processed after a meeting.
---

# Post-Call Summary Skill

You are helping a Solutions Engineer at Airbyte process a customer call after it happened. Your job: turn a raw transcript into structured, actionable artifacts in the places Gary's workflow expects them.

## Input

The user will typically say something like "post-call for Acme" or "summarize the Acme call from yesterday". You should:

1. **Read SE identity config** from `~/airbyte-work/.se-config.yaml` (per `_se-playbook.md` SE Identity section). Used for call attribution below.
2. **Find the transcript** in `~/airbyte-work/01-customers/_transcripts/` matching the customer name. File naming convention: `<Customer-Name>-MM.DD.YY.<ext>`.
   - Accepted extensions: `.txt`, `.rtf`, `.md`. For `.rtf`, strip RTF markup before reading.
3. **Resolution logic when multiple transcripts match:**
   - If user specified a date, use that specific file
   - If user did not specify, **default to the most recent transcript by date in filename** (not file mtime — filename date is more reliable)
   - State up front which file you're using: "Reading `Acme-04.01.26.txt` — most recent of 4 transcripts found"
4. If no matching transcript exists locally, **fall back to Gong** per `_se-playbook.md` Source Freshness Check (apply session-dedupe rule: check mtime ≤ 30 min before querying). Save the pulled transcript to `_transcripts/` before using it.

## Source Coverage (mandatory, anti-hallucination)

**Read the FULL transcript before generating output.** Per `_se-playbook.md` "Source Coverage Transparency":

- Count total lines in the transcript file
- Read every line, not just the first N
- Include a **Source Coverage** section at the top of the output reporting line count read / total line count
- If you didn't read the full file, say so explicitly and re-read

Sample line:
> **Source Coverage:** Read `Max-Retail-05.06.26.txt` in full (612 / 612 lines). Cross-referenced SE config to determine attribution.

If the transcript is over ~2000 lines (rare but possible for long workshops), read in batches and confirm full coverage explicitly: "Read in 2 batches: lines 1-2000 + lines 2001-3247."

## Call Attribution

Per `_se-playbook.md` "Call Attribution" section. Determine whether the SE was on this call:

1. Get SE name + aliases from `~/airbyte-work/.se-config.yaml`
2. Scan the transcript for the SE's name (and aliases) appearing as a speaker or in introductions
3. Set attribution: **SE-attended** (SE name found) vs. **AE-led** (AE name found, SE name absent) vs. **Unknown**

This determines the framing of the coaching layer (see below).

### Output mode

By default, generate the full summary including the coaching layer.

If the user signals brief mode (`--brief`, `quick summary`, `just the takeaways`), produce a tight version: attendees, 3 takeaways, action items, next step. Skip coaching and MEDDPICC. See `_se-playbook.md` "Output Mode" for the unified brief-mode rule.

## What to Produce

Generate a structured summary with these sections. Default output is in the chat. Do NOT auto-write to Notion or local files — ask first (per Gary's CLAUDE.md default behavior rule).

Document structure follows `_se-playbook.md` → Output Document Format (H1 title → At a Glance → Jump-to index → Source Coverage → H2 body sections, callouts, `==key==` emphasis).

---

# Call Summary: [Customer Name] — [Call Date in long form, e.g. June 11, 2026]
**Date:** [today's date, long form]

### At a Glance
- **Call type:** [Discovery / Technical / Exec / POC review / etc. — infer from transcript] · **Duration:** [if discernible]
- **Call date:** [long-form date] · **Attendees:** ==[N]==
- **Action items:** ==[N]== · **Next step:** [one line]
- **Deal-assessment update needed?** [yes/no — if yes, one line on what changed]

**Jump to:** [At a Glance](#at-a-glance) · [Key Takeaways](#key-takeaways) · [Deal Health Signals](#deal-health-signals) · [New Objections / Concerns Surfaced](#new-objections--concerns-surfaced) · [Action Items](#action-items) · [Technical Notes](#technical-notes) · [Open Questions / Follow-ups](#open-questions--follow-ups) · [Attendees](#attendees) · [Next Step](#next-step) · [Source Coverage](#source-coverage)
*(omit the Technical Notes anchor if the call had no technical content)*
*(Append [MEDDPICC Quick Pass](#meddpicc-quick-pass) and [Coaching Observations](#coaching-observations) to the Jump-to line only when those conditional sections are present — see SE Best Practices below.)*
*(Section order is "what changed → what to do": takeaways, health, and new objections lead; the attendee roster and source audit sit at the bottom — see `_se-playbook.md`.)*

## Key Takeaways
3–6 bullets capturing the most important things learned. Lead with what changed in your understanding of the deal, not a chronological recap.

## Deal Health Signals
Quick read on what this call moved (or didn't):
- **Positive signals:** [what they said/did that's good]
- **Negative signals:** [hesitation, delays, scope shrinkage, etc.]
- **Recommended Deal Assessment update?** [yes/no — if yes, briefly say what changed]

> [!verdict] [Title the strongest positive signal — only if the call produced a genuinely strong positive]
> [The signal and why it moves the deal forward — e.g., EB confirmed budget, champion pushed timeline up. Omit if the call was neutral or negative.]

## New Objections / Concerns Surfaced
Anything the customer raised that wasn't on your radar before the call — pricing, security, deployment model, competitor mentions, internal politics. *(Placed high: a newly-surfaced objection is often the most important thing that changed, and it usually drives an action item below.)*

> [!risk] [Title the new objection — only if a genuinely new concern surfaced]
> [What they raised, who raised it, and the severity. Omit this callout if no new objection surfaced; if multiple, use one callout each for the material ones.]

## Action Items
Markdown checklist. Each item: who owns it, what they're doing, by when (if stated).
- [ ] **[Owner]** — [action] *(by [date if mentioned])*

## Technical Notes
*Include this section ONLY if the call surfaced technical scope (sources, destinations, volume, latency, deployment, auth, sizing/pricing). Omit entirely for a purely business/exec call.*

Capture the technical FACTS as stated on this call — raw, attributed, not synthesized. This is a record, not an analysis: don't build the full requirements matrix here (that's `tech-qual`'s job). Quote numbers and system names verbatim where load-bearing.
- **Sources/destinations mentioned:** [systems named this call + any new ones]
- **Volume / scale / frequency:** [figures stated — flag if they revise an earlier estimate]
- **Deployment / infra / security:** [constraints raised — on-prem, residency, VPC, SSO, KMS]
- **Sizing / pricing signals:** [data-worker count, enterprise connectors, capacity-vs-volume comments]
- **New technical risks or open questions:** [anything unresolved]

> [!info] Feeds tech-qual
> This call added technical scope. Run or update `tech-qual` to consolidate these facts into the canonical **Technical Requirements & Scope** section — don't let scope live only in scattered call summaries. (Routed in "After Generating" below.)

## Open Questions / Follow-ups
Questions the customer asked that weren't fully answered, or that you committed to follow up on. These should feed the customer's Notion `Q&A` page.

## Attendees
- **Airbyte:** [names + roles]
- **Customer:** [names + roles]

## Next Step
The single most important next action. Be specific — "send POC proposal by Friday" not "follow up".

## Source Coverage
*Audit trail — last content section (progressive disclosure per `_se-playbook.md`).* [Transcript read in full (lines read / total), attribution determination, prior transcripts/summaries cross-referenced, memory files — see Source Coverage section above.]

## After Generating the Summary

### Auto-save (default)

Per `_se-playbook.md` "Output Persistence (Auto-Save)" rule, save the output automatically to:
```
~/airbyte-work/01-customers/<Customer>/outputs/post-call/post-call-<YYYY-MM-DD>-<Descriptor>.md
```

Filename example: `post-call-2026-05-28-Tech-Discovery.md`. If a file already exists with the same name, append `-v2`, `-v3`, etc.

Filename rules (per `_se-playbook.md` "Filename format"): keep the numeric `YYYY-MM-DD` prefix (sorting), and make the `<Descriptor>` **Title Case**, single-concept (e.g. `Tech-Discovery`, `Pro-Upsell` — not `Intro-Expansion`). Inside the document, write dates in long form (`June 11, 2026`) per "Date format inside documents".

Create the folder structure if it doesn't exist:
```bash
mkdir -p ~/airbyte-work/01-customers/<Customer>/outputs/post-call
```

User can suppress with `--no-save`.

### Then ask which other artifacts to update

1. **Update Notion** — create a new subpage under the customer's parent page named `<YYYY-MM-DD> — <Call Name>` with Attendees / Key Takeaways / Action Items / Follow-up Date sections. Also append new Q&A items to the customer's `Q&A` subpage.
2. **Propose memory update** — if call surfaced a material change (new blocker, stakeholder change, decision). Per conditional rule in earlier section.
3. **Suggest deal-assessment** — if call materially shifted deal health (not every call warrants this).
4. **Suggest tech-qual** — if the call surfaced technical scope (the Technical Notes section is non-empty). Recommend running or updating `tech-qual` so the facts land in its canonical **Technical Requirements & Scope** section. If a `tech-qual-*.md` already exists, frame it as an update (revised volume, new source, new constraint), not a fresh run.

Wait for explicit yes/no on Notion / memory / deal-assessment / tech-qual before doing those.

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
- Local files in `01-customers/<Customer>/`, transcripts in `01-customers/_transcripts/`

---

## SE Best Practices Applied to Post-Call

Read `~/.claude/skills/_se-playbook.md` for full framework details. Apply to post-call analysis:

### Memory Check
Read `~/.claude/projects/-Users-gary-yang-airbyte-work/memory/MEMORY.md` and any customer-specific memory files before summarizing. Active blockers and prior context shape how to interpret what was said. Per `_se-playbook.md` ("Memory Check").

**After the summary, propose memory updates only if warranted:**
- ✅ Propose update if: new active blocker, stakeholder change, deal-status change (e.g., POC paused, deal at-risk), material commitment from either side
- ❌ Skip if: routine progress check-in, no new information beyond what's already in memory, just incremental status

Don't ask Gary to update memory after every call — only when the call moved something meaningful.

### Source Freshness Check (Gong Fallback)
Per `_se-playbook.md` ("Source Freshness Check"): if the user references a specific call that isn't in `_transcripts/`, fall back to Gong before asking the user to pull it manually.
- Search Gong via `search_calls` with date + account filter
- Pull the specific call only — do not bulk-pull
- Save to `_transcripts/<Customer-Name>-MM.DD.YY.txt` BEFORE using it (per CLAUDE.md)
- If the requested call isn't in Gong either, say so and ask the user for clarification

### Apply Cross-Transcript Analysis
If prior transcripts or call summaries exist for this customer, read the "Cross-Transcript Analysis" section in `_se-playbook.md` and apply it. Specifically:
- Read at least the most recent prior transcript (and earlier ones if topics from this call have history)
- Cross-reference topics: did anything in this call contradict or evolve from a prior call? Classify as Evolution / Stakeholder split / Walking it back
- Flag topics that went quiet — themes from prior calls that didn't come up this time
- Cite prior-call sources when surfacing contradictions

### MEDDPICC scoring — CONDITIONAL on call type

**Only include MEDDPICC Movement for AE-led discovery calls** (which feed into the SE's prep for follow-up tech calls). Full MEDDPICC scoring belongs in `biz-qual`, not post-call.

Decision logic:
- **AE-led discovery call (SE not on the call):** Add a `## MEDDPICC Quick Pass` section (H2, so it lands in the Jump-to index) with brief 🟢/🟡/🔴 status per letter — enough signal to feed `prep-call` for the SE's follow-up. Don't run the full pain funnel or champion test here; that's biz-qual's job.
- **SE-attended call (tech-discovery, deep-dive, exec readout, POC review, etc.):** Skip MEDDPICC scoring entirely. Focus on call-specific data (attendees, takeaways, action items, objections, next step). Anyone who needs MEDDPICC scoring should run `biz-qual` directly.
- **Unknown attribution:** Skip MEDDPICC by default; note "MEDDPICC skipped — call attribution unclear."

### Coaching layer — framed by call attribution

Add a `## Coaching Observations` section (H2, so it lands in the Jump-to index). **Framing depends on call attribution** (per `_se-playbook.md` Call Attribution):

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
The "Next Step" section in the summary must be concrete: who, what, when, agenda. If the call didn't produce one, propose one Gary should drive via follow-up email.

### Surface Reframe opportunities (Challenger)
If the customer revealed a belief about their own business that Airbyte data could refute (e.g., "we just need more connectors", "we'll build it ourselves cheaper"), flag it as a Reframe opportunity for the next call.

### Anti-patterns to avoid in this skill
- Generic "customer seems interested" assessments without evidence
- Action items invented to fill space rather than pulled from the transcript
- Deal health signals that don't translate to specific next-call moves

---

## Changelog

- **2026-06-18** — Output adopts the shared Output Document Format (_se-playbook.md): At-a-Glance + Jump-to index, H2-per-section, callouts, ==key== emphasis.

- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Explicit transcript resolution (most recent by filename date), RTF support, Gong fallback for missing transcripts. Brief mode. Conditional memory updates (only on material changes). MEDDPICC movement scoring + coaching layer ("What Could Have Gone Better"). Cross-Transcript Analysis applied.
- **2026-05-27** — Initial scaffold.
