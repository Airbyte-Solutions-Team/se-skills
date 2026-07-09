---
name: prep-call
description: Prepares an SE call brief — defaults to a tech-discovery call AFTER the AE has already done business discovery (the 90% case). Aggressively checks Gong (7-day lookback, extends to 14) for the AE call, reads it in full, and produces a prep doc that inherits the AE's discovery (What the AE Already Learned), goes deeper on technical implications, and includes Reframe Hypothesis, Upfront Contract, SPIN Implication ladders, per-persona questions, and a concrete next-step. Cold-prep mode (no AE call found) is the explicit exception. Use when the user says "prep call", "prep for", "call prep", or provides a company name before a meeting.
---

# Call Prep Skill

You are helping a Solutions Engineer at Airbyte prepare for a discovery or intro call with a prospect.

## Input

The user will provide one or more of:
- Company name or website URL
- Any notes, email threads, or context they have
- The meeting type (first call, technical deep-dive, exec, etc.)

## Default Use Case: Tech Call After AE Discovery

**The most common SE invocation of this skill (~90%) is prepping for a technical/discovery call where the AE already did a business discovery call 1-14 days ago.** That AE call is in Gong and is the single most important input — Gary inherits the AE's discovery rather than re-running it.

Default behavior:
1. **Aggressively check Gong** for a prior call on this account (see Gong lookback below)
2. **Read the AE's transcript in full** before generating prep
3. **Treat the AE call as the primary source** — research is supplementary
4. **Skip AE-level basics in discovery questions** — go deeper on technical implications

**Cold prep mode is the exception.** Only if no AE call exists in the lookback window do we fall back to pure-research prep.

### Gong lookback for prep-call (tighter than other skills)

- **Primary search: last 7 days** — covers the typical AE→SE handoff
- **If empty: extend to last 14 days** — covers the "scheduling delay" case
- **If still empty: declare cold-prep mode explicitly** in the output ("Checked Gong for 14 days, no AE call found — proceeding with cold prep")

### Before generating: clarify

If any of these are unclear from the user's input, ask before generating (one batch of questions, not one-at-a-time):
- **Meeting type** — usually tech discovery (the default), but confirm if it's an exec readout, POC kickoff, etc.
- **Known attendees** — names/roles, even partial. Determines per-persona tailoring.
- **Meeting duration** — affects agenda timing
- **Specific topics the customer wants to cover** — if they sent an agenda

If the user just says "prep for Acme" with no context, ask. Don't guess at exec-readout meeting type when the default is tech discovery.

### Research scope

Web search is **conditional**, not automatic:
- If a Gong AE call exists, web research is supplementary at best — the AE already learned the relevant context
- If cold-prep mode (no AE call found), do focused web research (industry, recent news, tech stack signals)
- Don't burn cycles re-researching a company we already know well

### Output mode

Default = full prep doc (all sections below).

If user signals brief mode (`--brief`, `quick prep`, `1-pager`, `short version`): produce a tight version with only Snapshot, Reframe Hypothesis, Upfront Contract, top 3 discovery questions, and concrete Next Step. Skip per-persona, full SPIN ladders, full agenda table. See `_se-playbook.md` "Output Mode" for the unified brief-mode rule.

## Output Format

Document structure follows `_se-playbook.md` → Output Document Format (H1 title → At a Glance → Jump-to index → Source Coverage → H2 body sections, callouts, `==key==` emphasis). Produce a structured call prep brief with the following sections:

---

# Call Prep: [Company Name]
**Date:** [prep date, long form, e.g. June 11, 2026] · **Call:** [day + date + time w/ tz, e.g. Mon, June 29, 2026 · 11:00am ET / 8:00am PT] · **SE:** [SE name]

### At a Glance
- **Meeting type:** [tech discovery / exec / POC kickoff] · **Duration:** ==[e.g., 30 min]==
- **Primary contact:** [name / title]
- **Attendees:** [names + roles if known, else "TBC"]
- **Reframe hypothesis (1 line):** [the counterintuitive point of view you'll lead with]
- **Top goal for this call:** [the one thing this call must accomplish]

**Jump to:** [At a Glance](#at-a-glance) · [Company Snapshot](#company-snapshot) · [Why Airbyte (Hypothesis)](#why-airbyte-hypothesis) · [What the AE Already Learned](#what-the-ae-already-learned-from-prior-gong-call) · [Where We Left Off](#where-we-left-off-if-follow-up-call) · [Reframe Hypothesis](#reframe-hypothesis-challenger) · [Upfront Contract](#upfront-contract-sandler) · [Discovery Questions](#discovery-questions) · [SPIN Implication Ladders](#spin-implication-ladders) · [Per-Persona Questions](#per-persona-questions) · [Suggested Agenda](#suggested-agenda-30-min) · [Watch-outs / Landmines](#watch-outs--landmines) · [Suggested Next Step](#suggested-next-step-concrete--date--attendees--agenda) · [Source Coverage](#source-coverage)

*(Section order is context-first: who they are and why we matter, then the AE's inheritance, then the call plan. Source Coverage is the last content section — see `_se-playbook.md`.)*

## Company Snapshot
*Context first — the SE needs to know who they are before the AE's notes mean anything. **Tag each fact with its origin** — `[per AE call]` / `[SFDC]` / `[public — G2/news/BuiltWith]` / `[assumption — confirm live]`. Never present an unsourced fact as known; a cold-prep snapshot built on invented company facts is a live-call credibility risk.*
- **What they do:** [1-2 sentence business description — with source tag]
- **Industry:** [with source tag]
- **Size:** [employees / ARR if known — with source tag]
- **Tech signals:** [any known tech stack, tools, or integrations — job postings, G2, BuiltWith — tag `[public]` or `[assumption — confirm live]`]
- **Recent news:** [funding, launches, acquisitions, leadership changes — with source tag]

## Why Airbyte (Hypothesis)
*Positioning anchor — comes before talk tracks/agenda because it frames how the SE leads the call.* Based on their profile, the most likely reasons they're evaluating Airbyte:
- [Hypothesis 1 — e.g., scaling data pipelines beyond a manual solution]
- [Hypothesis 2 — e.g., replacing a brittle custom ETL or legacy tool]
- [Hypothesis 3 — e.g., need for connector breadth or self-hosted deployment]

## What the AE Already Learned (from prior Gong call)
*Only present if a prior AE call exists. The SE inherits the AE's discovery and goes deeper from there. In cold-prep mode, skip the bullets below and emit the cold-prep risk callout instead.*

> [!risk] Cold-prep mode — no AE call found
> *(Include this callout ONLY in cold-prep mode.)* Checked Gong for 14 days, no AE business-discovery call found — proceeding with cold prep on pure research. The AE→SE handoff hasn't happened; treat all customer context below as hypothesis, not confirmed.

- **AE call date + duration:** [date, length]
- **Attendees on AE call:** [names + roles surfaced]
- **Stated business pain:** [direct quote or close paraphrase from transcript]
- **Stated forcing function / why now:** [what's pushing them to evaluate]
- **Stated current stack (if mentioned):** [sources, destinations, tools]
- **Stated budget / timeline signals:** [if any]
- **Stated competitors / alternatives being considered:** [if any]
- **Open questions the AE flagged for the SE:** [things the AE said "we'll have our SE answer that"]
- **AE's read on the deal:** [if AE shared a temperature check on the call or in notes]

## Where We Left Off (if follow-up call)
*Skip this section if first call. Otherwise: ground the call in the most recent transcript — continuity belongs right after the AE inheritance.*
- Most recent call: [date]
- Last stated next-step: [what was committed]
- Topics that went quiet since: [anything from earlier calls that stopped being discussed]
- Walking-it-back signals to address: [if any stakeholder has been softening commitment]

## Reframe Hypothesis (Challenger)
**ONE counterintuitive, data-backed reframe you'll lead with.** Not generic discovery — a point of view that reframes what they thought they were buying.

> [Example: "Most data teams think their cost problem is warehouse spend. The data shows 60-70% of actual cost is engineering time maintaining custom connectors — invisible because it's salary, not SaaS."]

State the reframe as **"They likely believe X (basis: …); we reframe to Y."** Name the belief you're reframing and its basis — if there's no basis in the sources, label it a **hypothesis to test on the call**, not a finding.

Why this reframe for this customer: [brief rationale based on their stack/industry/news — cite the signal, or mark it a hypothesis]

## Upfront Contract (Sandler)
**Your opener — sets agenda, outcomes, and mutual permission to disqualify.**

> [!info] Upfront Contract opener
> "We've got [duration]. I want to understand your current data integration pain and your evaluation criteria. You'll probably want to see how we handle [their likely use case]. By the end we should know whether a POC makes sense — or whether this isn't a fit. Sound good?"

## Discovery Questions

*Two modes — depends on whether an AE call exists.*

**If AE call exists (default mode):**
- **Skip AE-level basics.** Do NOT re-ask "tell me about your data stack" or "what business problem are you solving" — the AE covered these. Re-asking signals you didn't read the AE's transcript.
- **Go deeper.** Build on what the AE learned. Each question should explicitly reference what's already known.
- **Mark "AE already covered"** above the section so it's clear what's intentionally skipped.

**If cold-prep mode (no AE call found):**
- Use the full hypothesis-based discovery question set — this is the first conversation.

---

**[Default — Tech Call After AE Discovery]**

*✓ AE already covered: [list of topics — e.g., business pain, basic stack, forcing function]. Don't re-ask these.*

**Technical Depth — Building on AE Discovery**
1. [Question that goes deeper on a specific pain the AE surfaced — e.g., "[AE] mentioned X is your biggest pipeline bottleneck. Walk me through what happens when X breaks — who gets paged, how long is it down, what's downstream impact?"]
2. [Quantification question — "How many hours per week does your team spend on Y?" then implication: "× loaded cost × 52 weeks = ?"]
3. [Architecture question grounded in their known stack — "You mentioned [specific source]. Are you doing CDC, batch, or both? What's the latency requirement?"]

**Deployment & Security (the 5 questions from `deployment-model-qual`)**
1. Cloud / self-hosted preference and why
2. Data residency or air-gap requirements
3. Multi-tenancy concerns for regulated data
4. KMS / secrets management requirements
5. VPC isolation for data plane

**Decision Process — Sharpening What the AE Heard**
1. [Validate AE's read on EB — "[AE] said [name] is involved. Walk me through how decisions like this typically get made — who else?"]
2. [Paper process — "Typically we see InfoSec → legal redline → procurement. Who runs each on your side, and what's the timeline?"]
3. [Competition follow-up — "When you said you were also looking at [competitor mentioned to AE], what specifically are you trying to compare?"]

---

**[Cold Prep Mode — No AE Call Found]**

*Full hypothesis-based discovery set. Prefer hypothesis-based questions over generic open-ended. "From your job posts you're hiring 3 DEs — I'm guessing pipeline maintenance is eating your team. Fair?" beats "Tell me about your data stack."*

**Business / Pain**
1. [Hypothesis-based question]
2. [Hypothesis-based question]
3. [Hypothesis-based question]

**Current State**
1. [Hypothesis-based question about current data stack]
2. [Diagnostic question about what's broken — "When X breaks, who gets paged?"]
3. [Hypothesis about champion/EB structure]

**Technical**
1. [Source/destination question]
2. [Volume/latency diagnostic]
3. [Deployment model question — see `deployment-model-qual` for the 5 questions]

**Process / Timeline**
1. [Decision process hypothesis]
2. [Timeline question — anchored to forcing function]
3. [Paper-process question]

---

## SPIN Implication Ladders
*For each top-2 likely pain point, pre-stage 2-3 Implication questions that force the customer to quantify the cost themselves. Wrap the resulting cost framing in `==…==` where it's a headline figure (e.g., ==$80K/yr== of engineering capacity, ==13h → 15min== latency).*

**Pain Hypothesis 1: [name the pain]**
- "How often does X happen?" → [expected answer]
- "When that happens, who gets paged and how long is it down?" → [expected answer]
- "What's the downstream impact — what reports go stale, what decisions get delayed?" → [expected answer]
- Estimated cost framing: [annual dollar or hours number]

**Pain Hypothesis 2: [name the pain]**
- [Same structure]

## Per-Persona Questions
*If multiple personas will attend, tailor questions per persona. Same deck for everyone = #1 expansion killer.*

**For [CDO / Data Eng VP / etc.]:**
- [Persona-specific question]
- [Persona-specific question]

**For [CFO / FinOps / etc.]:**
- [Persona-specific question]

**For [Security / Compliance lead, if attending]:**
- [Persona-specific question]

## Suggested Agenda (30 min)
| Time | Topic |
|------|-------|
| 0–5 min | Intros, confirm agenda |
| 5–10 min | Their context — what prompted the evaluation |
| 10–20 min | Discovery — current state, pain, requirements |
| 20–25 min | Airbyte overview / positioning to their situation |
| 25–30 min | Next steps |

## Watch-outs / Landmines
- [Any competitors they likely use or have evaluated]
- [Any known sensitivities — e.g., data residency, compliance, open-source skepticism]

## Suggested Next Step (Concrete — date + attendees + agenda)
*"Follow up next week" is not a next step. Write the exact next-step you'll push for.*

- **Meeting name:** [e.g., Technical deep-dive — security & deployment]
- **Attendees needed:** [specific names/roles]
- **Proposed date:** [specific date or "within 1 week of this call"]
- **Agenda:** [3-4 bullets]
- **Pre-work:** [anything Gary or customer needs to do beforehand]

## Source Coverage
*Audit trail — last content section (progressive disclosure per `_se-playbook.md`).* [AE Gong transcript path + line count, local notes, memory files, web queries — see After Generating. In cold-prep mode, state that explicitly here.]

---

## Style (prep-call skill guidance — not part of output template)

- Concise and scannable — this is a working doc, not a report
- Flag when information is inferred vs. confirmed
- If a section can't be filled, use `[research needed]` rather than guessing
- Tailor discovery questions to the company's likely maturity and use case
- Cite Gong transcripts inline when pulling AE-learned content (date + speaker)

---

## SE Best Practices Applied to Call Prep

Read `~/.claude/skills/_se-playbook.md` for full framework details. Apply specifically to call prep:

### Memory Check
Read `~/.claude/projects/-Users-gary-yang-airbyte-work/memory/MEMORY.md` and any customer-specific memory files before generating prep. Active blockers, pending Airbyte-side actions, and stakeholder dynamics often live here and don't appear in transcripts. Per `_se-playbook.md` ("Memory Check").

### Source Freshness Check — Always Check Gong First
**Always check Gong for the most recent call on this account before generating prep.** Per `_se-playbook.md` ("Source Freshness Check"):

**Lookback window — match the prospect state:**
- **NEW prospect** (no local customer folder, no transcripts, no notes): lookback ≤ **7 days**. If an AE intro call happened, it'll be very recent. Don't search the whole history for a deal that doesn't have one.
- **EXISTING customer** (local folder exists with prior artifacts): apply the standard **14-day rule** — pull from Gong if most-recent local transcript is older than that.

**Workflow:**
- Check `_transcripts/` first. If a transcript matching this customer was saved in the last 30 min (`mtime`-recent), use it and skip Gong — per `_se-playbook.md` session-dedupe rule.
- If no local match for a recent call, search Gong with the appropriate lookback
- Pull the **most recent call only** — do not bulk-pull
- Save the pulled transcript to `_transcripts/<Customer-Name>-MM.DD.YY.txt` BEFORE using it (per CLAUDE.md)
- Note in the output which sources were freshly pulled
- If no Gong call exists in the window, say so — don't silently fall back to web research only

### Apply Cross-Transcript Analysis (when prior call history exists)
For follow-up calls with an existing customer, read the "Cross-Transcript Analysis" section in `_se-playbook.md`. Read prior transcripts (recency-weighted but not recency-only). Specifically build:
- A "Where we left off" section grounded in the most recent transcript
- A "Topics to revisit" section flagging anything that went quiet or unresolved
- A "Watch for contradictions" callout if a stakeholder has been softening commitment over time (walking it back signal — bring it up directly)
- Cite prior-call dates and speakers when staging questions

### Lead with a Challenger Reframe, not generic discovery
Before the call, draft ONE provocative reframe based on what you know about their industry/stack. Example for a data-engineering-heavy prospect: "Most teams think their data cost problem is warehouse spend — for companies your size, 60-70% is actually engineering time maintaining custom connectors." Add a `### Reframe Hypothesis` section to the prep doc.

### Pre-stage SPIN Implication questions
For each likely pain point, write 2-3 Implication questions that force the customer to quantify the cost themselves. Don't just ask "is X a problem?" — ask "when X happens, who gets paged, how long are you down, what does that cost?" Add these under each pain hypothesis.

### Draft an Upfront Contract (Sandler)
Add a `### Upfront Contract` section to the prep doc with a 2-3 sentence opener Gary can use to set agenda + outcomes + mutual permission to disqualify. Example: "We've got 30 minutes. I want to understand your current data integration pain and evaluation criteria. You'll want to see how we handle [their likely use case]. By the end we should know whether a POC makes sense — or whether this isn't a fit. Sound good?"

### Tailor by persona
If multiple attendees are known, list discovery questions *per persona*. CDO ≠ CFO ≠ security lead. Same deck for everyone is the #1 expansion killer.

### Set a concrete next-step hypothesis
Don't end prep with "follow up next week." Write the specific next-step you'll push for: meeting name, attendees needed, agenda, by-when. Customer should leave the call knowing exactly what's next.

### Anti-patterns to avoid in this skill
- Generic "tell me about your business" questions in the Discovery list — replace with hypothesis-based questions
- Discovery questions only; no diagnostic or confirmation questions in the mix
- Suggested agenda that's a tour of Airbyte features instead of structured discovery

---

## After Generating

### Auto-save (default)

Per `_se-playbook.md` "Output Persistence (Auto-Save)" rule, save the prep doc automatically to:
```
~/airbyte-work/01-customers/<Customer>/outputs/call-prep/call-prep-<YYYY-MM-DD>-<Descriptor>.md
```

Filename example: `call-prep-2026-05-28-Tech-Discovery.md`. Create folders if missing. Append `-v2` etc. if same-day duplicate.

Filename rules (per `_se-playbook.md` "Filename format"): keep the numeric `YYYY-MM-DD` prefix (sorting), and make the `<Descriptor>` **Title Case**, single-concept. Inside the document, write dates in long form (`June 11, 2026`) per "Date format inside documents".

User can suppress with `--no-save`.

### Source Coverage Reporting

Include a Source Coverage section at the top of the output stating:
- AE Gong transcript: file path + line count read (if applicable)
- Local notes consulted: file names
- Memory: file names
- Web research: queries run

Per `_se-playbook.md` "Source Coverage Transparency" rule.

### SE Identity

Read `~/airbyte-work/.se-config.yaml` to populate the `[SE name]` field in headers and the Upfront Contract opener. If the config doesn't exist, ask the user once and recommend they create it.

---

## Changelog

- **2026-07-09** — Sourcing discipline for cold-prep facts: every Company Snapshot fact carries an origin tag (`[per AE call]` / `[SFDC]` / `[public]` / `[assumption — confirm live]`); the reframe is now stated as "they likely believe X (basis…); reframe to Y," labeled a hypothesis to test if unsourced. No refuse-gate added — stays cold-runnable (light-touch).
- **2026-06-18** — Output adopts the shared Output Document Format (_se-playbook.md): At-a-Glance + Jump-to index, H2-per-section, callouts, ==key== emphasis.

- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Default mode reframed to "tech call after AE discovery" (90% case). Gong lookback 7d→14d. Added "What the AE Already Learned" section. Discovery questions now have two modes (default = deeper / cold-prep = full). Reframe Hypothesis, Upfront Contract, SPIN ladders, per-persona questions, concrete next-step. Memory + freshness gates. Style section normalized.
- **2026-05-27** — Initial scaffold.
