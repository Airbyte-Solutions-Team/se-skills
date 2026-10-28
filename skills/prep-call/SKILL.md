---
name: prep-call
description: Prepares an SE call brief — defaults to a tech-discovery call AFTER the AE has already done business discovery (the 90% case). Aggressively checks Gong (7-day lookback, extends to 14) for the AE call — and Granola for calls that weren't recorded in Gong — reads it in full, and produces a prep doc that inherits the AE's discovery, goes deeper on technical implications, and includes a Point of View to Test, Suggested Opener, implication-depth questions, persona-specific questions, and a concrete next-step. Cold-prep mode (no AE call found) is the explicit exception. Use when the user says "prep call", "prep for", "call prep", or provides a company name before a meeting.
---

# Call Prep Skill

You are helping a Solutions Engineer at Airbyte prepare for a discovery or intro call with a prospect.

## Input

The user will provide one or more of:
- Company name or website URL
- Any notes, email threads, or context they have
- The meeting type (first call, technical deep-dive, exec, etc.)

## Default Use Case: Tech Call After AE Discovery

**The most common SE invocation of this skill (~90%) is prepping for a technical/discovery call where the AE already did a business discovery call 1-14 days ago.** That AE call is usually in Gong (or in Granola, if the customer declined recording or hosted the call on their own Zoom/Meet/Teams) and is the single most important input — the SE inherits the AE's discovery rather than re-running it.

Default behavior:
1. **Aggressively check Gong, then Granola** for a prior call on this account (see Gong lookback and Granola step below; source order and duplicate rules per `_se-playbook.md` "Source Freshness Check")
2. **Read the AE's transcript in full** before generating prep
3. **Treat the AE call as the primary source** — research is supplementary
4. **Skip AE-level basics in discovery questions** — go deeper on technical implications

**Cold prep mode is the exception.** Only if no AE call exists in the lookback window do we fall back to pure-research prep.

### Gong lookback for prep-call (tighter than other skills)

- **Primary search: last 7 days** — covers the typical AE→SE handoff
- **If empty: extend to last 14 days** — covers the "scheduling delay" case
- **If still empty in Gong: check Granola over the same window** (see Granola step below) before concluding anything
- **If still empty in both: declare cold-prep mode explicitly** in the output ("Checked Gong and Granola for 14 days, no AE call found — proceeding with cold prep"). Name any source that was disabled or unreachable instead of implying it was checked.

### How to search Gong (do NOT rely on the company name alone)

Gong titles are typed by hand and CRM account names are often the bare domain (e.g. the title "Airbyte x **Artic** Wolf Intro" with CRM account `arcticwolf.com`). A `customerName: "Arctic Wolf"` search returns **zero** calls for that call — which previously caused a false cold-prep declaration. Search in this order, stopping at the first hit:

1. **Resolve the email domain(s)** from Salesforce (Account Website / contact emails), local transcripts/notes, or the user's input. If unknown, derive candidates from the account name (e.g. `arcticwolf.com`).
2. **`search_calls_by_account`** with those `domains` (and `matchCrmAccount: true`) over the lookback window. This matches external participants' email domains, so it is immune to title typos.
3. **`search_calls` with `customerName`** using variants: the name with no spaces/hyphens (`arcticwolf`), the domain root, and the name as typed. Substring match covers CRM account, email domain, and title.
4. **Granola (calls not in Gong).** Run this when `transcript_sources.granola` is on (default) and the Gong search found no call for the meeting — or to catch a newer unrecorded call. Use `search_meetings` with `participant` set to the domain, then the name variants, over the same window with `attended_only: true`; confirm attendees with `get_meetings`; read with `get_meeting_transcript`. Skip internal-only meetings. If the same meeting is in both Gong and Granola, **Gong wins** and the Granola copy is not pulled; a Granola-only meeting counts, and the most recent call across both sources is the AE call. Procedure, speaker-label caveats, and the save/provenance header are in `_se-playbook.md` "Source Freshness Check".
5. **Manual upload.** If the user says they'll upload/paste the transcript, or none of the above finds the call but the user says it happened, ask for it — don't proceed in cold-prep silently.
6. **Only if all of the above return nothing** declare cold-prep mode — and list the exact domains/name variants tried (for Gong and Granola) in Source Coverage so the SE can spot a bad guess.

If a call is found in either source, read the full transcript before generating, and treat it as the primary source (per Default behavior above). Never state "zero calls found" based on a single name-only query.

### Before generating: clarify

If any of these are unclear from the user's input, ask before generating (one batch of questions, not one-at-a-time):
- **Meeting type** — usually tech discovery (the default), but confirm if it's an exec readout, POC kickoff, etc.
- **Known attendees** — names/roles, even partial. Determines per-persona tailoring.
- **Meeting duration** — affects agenda timing
- **Specific topics the customer wants to cover** — if they sent an agenda

If the user just says "prep for Acme" with no context, ask. Don't guess at exec-readout meeting type when the default is tech discovery.

### Connector check (registry-grounded — only when connectors are named)

If the AE call, notes, or Salesforce name specific systems the customer wants to sync (e.g. "GitHub", "Postgres"), resolve each one against the connector registry **before** writing the prep — never answer "do we have it?" from memory. Skip this step entirely if no specific systems are named; don't invent a connector list.

1. **Cache first.** Read `{airbyte_repos_dir}/{reference_data.registry.cache_dir}/oss_registry.json` and `cloud_registry.json` (default `~/.se-skills/02-repos/registry/`; paths per playbook → Workspace Paths and "Product & Connector Reference Data"). If a file is missing or unreadable, make **one** live fetch from `reference_data.registry.oss_url` / `cloud_url` and save it to the cache dir. Only if the cache is missing **and** the fetch fails, mark the connector rows **unverified** (and say why in Source Coverage) — never fall back to memory silently. A cache older than ~7 days is usable but must be flagged with its date.
2. **Match** each system by `dockerRepository` suffix (`source-github`, `source-postgres`; `destination-…` for destinations), falling back to a case-insensitive `name` match. Check the `sources` and `destinations` lists. Registry entries expose everything needed: `dockerImageTag`, `supportLevel`, `releaseStage`, `sourceType`, and `documentationUrl`.
3. **Cloud vs OSS-only:** present in `oss_registry` but absent from `cloud_registry` = self-managed only; flag it, since it bears on the deployment conversation.
4. **Not in the registry ≠ doesn't exist.** Enterprise connectors (Oracle, NetSuite, SAP HANA, ServiceNow, SharePoint, Workday, DB2) are not in the public registry — say "not in the public registry; may be an enterprise connector — confirm via `connector-feasibility`." If it's genuinely absent (e.g. Backstage has no Airbyte connector), say so plainly and name the nearest path as a suggestion (e.g. Backstage's backing Postgres database, or the Connector Builder) — never imply a connector exists.
5. **Registry proves availability, not fit.** It doesn't confirm the objects, sync modes, volume, or auth the customer needs. Hand fit questions to `connector-feasibility`.

Emit the result as a `### Connector Check` H3 under `## Account Context` (format in the template below), and add the registry cache date to Source Coverage.

### Research scope

Web search is **conditional**, not automatic:
- If an AE call exists (Gong or Granola), web research is supplementary at best — the AE already learned the relevant context
- If cold-prep mode (no AE call found), do focused web research (industry, recent news, tech stack signals)
- Don't burn cycles re-researching a company we already know well

### Output mode

Default = full prep doc (all sections below).

If user signals brief mode (`--brief`, `quick prep`, `1-pager`, `short version`): produce a tight version with Meeting Snapshot, Point of View to Test, Suggested Opener, the top 3 Must-Ask Questions, and Suggested Next Step. Skip persona-specific questions, full implication-depth questions, and the full agenda table. See `_se-playbook.md` "Output Mode" for the unified brief-mode rule.

## Output Format


> [!info] Canonical output architecture for `prep-call`
> This skill follows the shared content-architecture contract. Produce the H2 sections below in this exact order; use H3 (`###`) for the listed sub-topics.
>
> **Canonical H2 order:**
> 1. `Account Context`
> 2. `Call Strategy`
> 3. `Discovery Plan`
> 4. `Agenda`
> 5. `Watch Outs`
> 6. `Desired Next Step`
> 7. `Source Coverage`
>
> **H3 subtopics (when used):**
> - under `Meeting Snapshot`: Date, Time, Duration, Primary Contact, Attendees, Call Objective, Key Unknown
> - under `Account Context`: Company Snapshot, Why Airbyte, Prior Call Context, Open Threads From Prior Calls, Connector Check
> - under `Call Strategy`: Point of View to Test, Suggested Opener
> - under `Discovery Plan`: Must-Ask Questions, Implication-Depth Questions, Persona-Specific Questions
> - under `Agenda`: Suggested Agenda (30 min)
> - under `Watch Outs`: Watch-Outs / Landmines
> - under `Desired Next Step`: Suggested Next Step
>
> Source Coverage must be the **final H2** when required. The profile-specific summary block is an H3 under the title block (not a navigable section).

Document structure follows `~/.claude/skills/_se-playbook.md` → Shared Skill Boilerplate → Output format reference. Produce a structured call prep brief with the following sections:

---

<!-- output-template:start -->
# Call Prep: [Company Name]
**Date:** [prep date, long form, e.g. June 11, 2026] · **Call:** [day + date + time w/ tz, e.g. Mon, June 29, 2026 · 11:00am ET / 8:00am PT] · **SE:** [SE name]
### Meeting Snapshot
- **Date:** [prep date, long form]
- **Time:** [call time and time zone]
- **Duration:** [e.g., 30 min]
- **Primary contact:** [name / title]
- **Attendees:** [names + roles if known, else TBC]
- **Call objective:** [the one thing this call must accomplish]
- **Key unknown:** [the most important unanswered question to resolve]

**Jump to:** [Account Context](#account-context) · [Call Strategy](#call-strategy) · [Discovery Plan](#discovery-plan) · [Agenda](#agenda) · [Watch Outs](#watch-outs) · [Desired Next Step](#desired-next-step) · [Source Coverage](#source-coverage)

*(Use the canonical sections below; retain all facts, tables, and reasoning while nesting sub-topics under the relevant H2.)*

## Account Context

### Company Snapshot
*Context first — the SE needs to know who they are before the AE's notes mean anything. **Tag each fact with its origin** — `[per AE call]` / `[SFDC]` / `[public — G2/news/BuiltWith]` / `[assumption — confirm live]`. Never present an unsourced fact as known; a cold-prep snapshot built on invented company facts is a live-call credibility risk.*
- **What they do:** [1-2 sentence business description — with source tag]
- **Industry:** [with source tag]
- **Size:** [employees / ARR if known — with source tag]
- **Tech signals:** [any known tech stack, tools, or integrations — job postings, G2, BuiltWith — tag `[public]` or `[assumption — confirm live]`]
- **Recent news:** [funding, launches, acquisitions, leadership changes — with source tag]

### Why Airbyte
*Positioning anchor — comes before talk tracks/agenda because it frames how the SE leads the call.* Based on their profile, the most likely reasons they're evaluating Airbyte — **hypotheses to validate on the call, not the customer's stated position:**
- [Hypothesis 1 — e.g., scaling data pipelines beyond a manual solution]
- [Hypothesis 2 — e.g., replacing a brittle custom ETL or legacy tool]
- [Hypothesis 3 — e.g., need for connector breadth or self-hosted deployment]

### Prior Call Context
*Only present if a prior AE call exists. The SE inherits the AE's discovery and goes deeper from there. In cold-prep mode, skip the bullets below and emit the cold-prep risk callout instead.*

> [!risk] Cold-prep mode — no AE call found
> *(Include this callout ONLY in cold-prep mode.)* Checked Gong and Granola for 14 days, no AE business-discovery call found — proceeding with cold prep on pure research. The AE→SE handoff hasn't happened; treat all customer context below as hypothesis, not confirmed.

- **AE call date + duration:** [date, length]
- **Attendees on AE call:** [names + roles surfaced]
- **Stated business pain:** [direct quote or close paraphrase from transcript]
- **Stated forcing function / why now:** [what's pushing them to evaluate]
- **Stated current stack (if mentioned):** [sources, destinations, tools]
- **Stated budget / timeline signals:** [if any]
- **Stated competitors / alternatives being considered:** [if any]
- **Open questions the AE flagged for the SE:** [things the AE said "we'll have our SE answer that"]
- **AE's read on the deal:** [if AE shared a temperature check on the call or in notes]

### Open Threads From Prior Calls
*Skip this section if first call. Otherwise: ground the call in the most recent transcript — continuity belongs right after the AE inheritance.*
- Most recent call: [date]
- Last stated next-step: [what was committed]
- Topics that went quiet since: [anything from earlier calls that stopped being discussed]
- Walking-it-back signals to address: [if any stakeholder has been softening commitment]

### Connector Check
*Only when the AE call/notes/SFDC name specific systems — omit the whole H3 otherwise. Resolved from the cached connector registry per "Connector check" above, never from memory.*

| Customer system | Airbyte connector | Cloud / OSS-only | Support · stage | Version | Docs |
|---|---|---|---|---|---|
| [as named, with source tag e.g. `[per AE call]`] | `source-…` / *no connector — [nearest path]* / *unverified — [reason]* | [Cloud + OSS / OSS-only / not in public registry] | [certified · GA] | [x.y.z] | [documentationUrl link] |

*Availability only — fit (objects, sync modes, volume, auth) is not confirmed; run `connector-feasibility` for that.*

## Call Strategy

### Point of View to Test
**ONE counterintuitive, data-backed reframe you'll lead with.** Not generic discovery — a point of view that reframes what they thought they were buying.

> [Example: "Most data teams think their cost problem is warehouse spend. The data shows 60-70% of actual cost is engineering time maintaining custom connectors — invisible because it's salary, not SaaS."]

State the reframe as **"They likely believe X (basis: …); we reframe to Y."** Name the belief you're reframing and its basis — if there's no basis in the sources, label it a **hypothesis to test on the call**, not a finding.

Why this reframe for this customer: [brief rationale based on their stack/industry/news — cite the signal, or mark it a hypothesis]

### Suggested Opener
**Your opener — sets agenda, outcomes, and mutual permission to disqualify.**

> [!info] Suggested opener
> "We've got [duration]. I want to understand your current data integration pain and your evaluation criteria. You'll probably want to see how we handle [their likely use case]. By the end we should know whether a POC makes sense — or whether this isn't a fit. Sound good?"

## Discovery Plan

### Must-Ask Questions
*Two modes — depends on whether an AE call exists. In both modes, prioritize the highest-value questions per subsection (3-5, not exhaustive) over stacking near-duplicate questions, and phrase each as a neutral request for the missing fact — not a conclusion wrapped in a question (see Style section below for the pattern to avoid).*

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

### Implication-Depth Questions
*For each top-2 likely pain point, pre-stage 2-3 Implication questions that force the customer to quantify the cost themselves on the call. Wrap the resulting cost framing in `==…==` where it's a headline figure (e.g., ==$80K/yr== of engineering capacity, ==13h → 15min== latency) — but only when a source (AE call, prior notes) already supplied the underlying number (frequency, hours, headcount). Don't invent a cost/sizing figure to fill this line — if the inputs aren't sourced yet, say the number will be established live on the call.*

**Pain Hypothesis 1: [name the pain]**
- "How often does X happen?" → [expected answer, if a source suggests one — else leave as the open question to ask]
- "When that happens, who gets paged and how long is it down?" → [expected answer, if a source suggests one]
- "What's the downstream impact — what reports go stale, what decisions get delayed?" → [expected answer, if a source suggests one]
- Cost framing: [a number, only if the inputs above are already sourced — otherwise "not yet quantified; establish on the call"]

**Pain Hypothesis 2: [name the pain]**
- [Same structure]

### Persona-Specific Questions
*If multiple personas will attend, tailor questions per persona. Same deck for everyone = #1 expansion killer.*

**For [CDO / Data Eng VP / etc.]:**
- [Persona-specific question]
- [Persona-specific question]

**For [CFO / FinOps / etc.]:**
- [Persona-specific question]

**For [Security / Compliance lead, if attending]:**
- [Persona-specific question]

## Agenda

### Suggested Agenda (30 min)
| Time | Topic |
|------|-------|
| 0–5 min | Intros, confirm agenda |
| 5–10 min | Their context — what prompted the evaluation |
| 10–20 min | Discovery — current state, pain, requirements |
| 20–25 min | Airbyte overview / positioning to their situation |
| 25–30 min | Next steps |

## Watch Outs

### Watch-Outs / Landmines
- **Competitors:** [name one only if a source — AE call, SFDC, public research — identified it, and cite that source; if none was named, write "No competitor named — not identified" rather than guessing one]
- [Any known sensitivities — e.g., data residency, compliance, open-source skepticism — with source]

## Desired Next Step

Name the specific calendar-able mutual commitment to ask for at the end of the call: meeting date, attendees, purpose, and owner. Add the fallback ask to use if the primary commitment stalls, and state the trigger for switching to it.









### Suggested Next Step
*"Follow up next week" is not a next step. Write the exact next-step you'll push for.*

- **Meeting name:** [e.g., Technical deep-dive — security & deployment]
- **Attendees needed:** [specific names/roles]
- **Proposed date:** [specific date or "within 1 week of this call"]
- **Agenda:** [3-4 bullets]
- **Pre-work:** [anything the SE or customer needs to do beforehand]

## Source Coverage


*Audit trail — last content section (progressive disclosure per `_se-playbook.md`).* [Transcript source used + AE call transcript path + line count, local notes, memory files, web queries — see After Generating. In cold-prep mode, state that explicitly here.]

---
<!-- output-template:end -->

## Style (prep-call skill guidance — not part of output template)

- Concise and scannable — this is a working doc, not a report
- Flag when information is inferred vs. confirmed
- If a section can't be filled, use `[research needed]` rather than guessing
- Tailor discovery questions to the company's likely maturity and use case
- Cite Gong transcripts inline when pulling AE-learned content (date + speaker)
- **Base every section on confirmed context first** — the AE transcript, existing local files, or a cited source — before layering in hypotheses. A hypothesis stays labeled as a hypothesis to validate; never restate it as the customer's position (see `_se-playbook.md` → "Evidence thresholds").
- **Discovery questions are a neutral ask for the missing fact, not a conclusion wrapped in a question.** Don't write "The customer believes the pricing will be cost-prohibitive, so confirm their concurrency." Write "The customer asked how the workload maps to pricing. Concurrency was not established, so confirm the peak concurrency and completion requirements before estimating capacity."
- **Don't name a competitor, stakeholder, decision process, or objection that no source named** — say so ("no competitor named — not identified") instead of guessing one to fill a section.
- **Don't state a cost, sizing, or capacity number the sources don't support.** If a pain point's cost depends on an answer the customer hasn't given yet, say the number will be established on the call — don't invent one to fill the template.

---

## SE Best Practices Applied to Call Prep

Read `~/.claude/skills/_se-playbook.md` for full framework details. Apply specifically to call prep:

### Memory Check
Read `memory_dir` (per playbook → Workspace Paths) `MEMORY.md` and any customer-specific memory files before generating prep (skip gracefully if `memory_dir` is unset). Active blockers, pending Airbyte-side actions, and stakeholder dynamics often live here and don't appear in transcripts. Per `_se-playbook.md` ("Memory Check").

### Source Freshness Check — Always Check Gong First, Then Granola
**Always check Gong — then Granola for anything Gong doesn't have — for the most recent call on this account before generating prep.** Gong wins when both have the same meeting. Per `_se-playbook.md` ("Source Freshness Check"):

**Lookback window — match the prospect state:**
- **NEW prospect** (no local customer folder, no transcripts, no notes): lookback ≤ **7 days**. If an AE intro call happened, it'll be very recent. Don't search the whole history for a deal that doesn't have one.
- **EXISTING customer** (local folder exists with prior artifacts): apply the standard **14-day rule** — pull from Gong if most-recent local transcript is older than that.

**Workflow:**
- Check `{transcripts_dir}` first. If a transcript matching this customer was saved in the last 30 min (`mtime`-recent), use it and skip Gong — per `_se-playbook.md` session-dedupe rule.
- If no local match for a recent call, search Gong with the appropriate lookback, then Granola for meetings Gong lacks
- Pull the **most recent call only** (across both sources) — do not bulk-pull
- Save the pulled transcript to `{transcripts_dir}/<Customer-Name>-MM.DD.YY.txt` BEFORE using it (per CLAUDE.md); Granola/uploaded files carry the provenance header from the playbook
- Note in the output which sources were freshly pulled
- If no call exists in Gong or Granola in the window, say so — don't silently fall back to web research only

### Apply Cross-Transcript Analysis (when prior call history exists)
For follow-up calls with an existing customer, read the "Cross-Transcript Analysis" section in `_se-playbook.md`. Read prior transcripts (recency-weighted but not recency-only). Specifically build:
- An "Open Threads From Prior Calls" section grounded in the most recent transcript
- A "Topics to revisit" section flagging anything that went quiet or unresolved
- A "Watch for contradictions" callout if a stakeholder has been softening commitment over time (walking it back signal — bring it up directly)
- Cite prior-call dates and speakers when staging questions

### Lead with a Point of View to Test, not generic discovery
Before the call, draft ONE provocative reframe based on what you know about their industry/stack. Example for a data-engineering-heavy prospect: "Most teams think their data cost problem is warehouse spend — for companies your size, 60-70% is actually engineering time maintaining custom connectors." Put it under `### Point of View to Test`.

### Pre-stage implication-depth questions
For each likely pain point, write 2-3 Implication questions that force the customer to quantify the cost themselves. Don't just ask "is X a problem?" — ask "when X happens, who gets paged, how long are you down, what does that cost?" Add these under each pain hypothesis.

### Draft the Suggested Opener (Sandler)
Add a `### Suggested Opener` section to the prep doc with a 2-3 sentence opener the SE can use to set agenda + outcomes + mutual permission to disqualify. Example: "We've got 30 minutes. I want to understand your current data integration pain and evaluation criteria. You'll want to see how we handle [their likely use case]. By the end we should know whether a POC makes sense — or whether this isn't a fit. Sound good?"

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

### Auto-save path
Per `~/.claude/skills/_se-playbook.md` → Shared Skill Boilerplate → After Generating (saving skills), save the prep doc automatically to:
```
{customers_dir}/<Customer>/opportunities/<opp-slug>/outputs/prep-call/call-prep-<YYYY-MM-DD>-<Descriptor>.md
```

(Use `{customers_dir}/<Customer>/outputs/prep-call/` when there is no opportunity folder.) The folder is `prep-call/` — the skill id. The legacy `call-prep/` folder is still read by the webapp and `next-move`, but new files go to `prep-call/`. The filename keeps the `call-prep-` prefix.

Filename example: `call-prep-2026-05-28-Tech-Discovery.md`.

### Source Coverage Reporting
Include a final Source Coverage section of the output stating:
- **Transcript source:** which source supplied the AE call and why — `Gong (Granola also had this meeting — not used)` / `Granola (no Gong recording found; checked <domains/names/window>)` / `Manual upload` / `Local`; or cold-prep with every source tried
- AE call transcript (Gong/Granola/upload): file path + line count read (if applicable)
- Local notes consulted: file names
- Memory: file names
- Web research: queries run
- Connector registry (when a Connector Check was produced): cache file + date, or "unverified — <reason>"

Per `_se-playbook.md` "Source Coverage Transparency" rule.

### SE Identity
Read `config_file` (per playbook → Workspace Paths) to populate the `[SE name]` field in headers and the Suggested Opener. If the config doesn't exist, ask the user once and recommend they create it.

---

## Changelog

- **2026-10-28** — Granola + manual upload as transcript sources behind Gong: the AE call may come from Gong, then Granola (customer declined recording / customer-hosted Zoom, Meet, Teams), then a user upload. Gong wins when both have the same meeting; a Granola-only meeting still counts as the most recent call. Cold-prep is declared only after Gong **and** Granola are checked, and Source Coverage now states the transcript source used. Rules live in `_se-playbook.md` "Source Freshness Check".
- **2026-10-28** — Added registry-grounded Connector Check: when the AE call names specific systems, resolve them against the cached connector registry (cache first, one live fetch if missing, "unverified" only if both fail) and emit a `### Connector Check` table under Account Context — connector, Cloud/OSS-only, support level · stage, version, and the connector's `documentationUrl`. Availability only; fit stays with `connector-feasibility`.
- **2026-10-28** — Gong search method specified: resolve email domain → `search_calls_by_account` → `customerName` variants; cold-prep only after all fail, with the tried domains/names listed in Source Coverage (a name-only search missed a call titled "Artic Wolf" with CRM account `arcticwolf.com`). Auto-save folder is now `outputs/prep-call/` (skill id); legacy `outputs/call-prep/` still recognized by the webapp.
- **2026-09-04** — Fact-vs-assumption hardening pass (no structural change — canonical sections, order, and save behavior are unchanged): Watch-Outs' competitor line now requires a cited source or "No competitor named — not identified" instead of inviting a guess; "Why Airbyte" is explicitly labeled hypotheses to validate, not the customer's stated position; Implication-Depth Questions' cost/sizing figure is now gated on a sourced input (else "not yet quantified; establish on the call") instead of always producing a number; Must-Ask Questions now asks for 3-5 prioritized questions over exhaustive lists, phrased as neutral asks rather than conclusions wrapped in questions. Added 4 new Style-section rules pointing at `_se-playbook.md` → "Evidence thresholds."
- **2026-07-10** — Repointed hardcoded `~/airbyte-work/` paths to the workspace-path resolver (`{customers_dir}`/`{transcripts_dir}`/`{notes_dir}`/`config_file`/`memory_dir`) per playbook → Workspace Paths. Portable across SE machines.
- **2026-07-09** — Genericized hardcoded "Gary" SE-identity prose → "the SE" (reads identity from `.se-config.yaml`).
- **2026-07-09** — Sourcing discipline for cold-prep facts: every Company Snapshot fact carries an origin tag (`[per AE call]` / `[SFDC]` / `[public]` / `[assumption — confirm live]`); the reframe is now stated as "they likely believe X (basis…); reframe to Y," labeled a hypothesis to test if unsourced. No refuse-gate added — stays cold-runnable (light-touch).
- **2026-06-18** — Output adopts the shared Output Document Format (_se-playbook.md): At-a-Glance + Jump-to index, H2-per-section, callouts, ==key== emphasis.

- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Default mode reframed to "tech call after AE discovery" (90% case). Gong lookback 7d→14d. Added "What the AE Already Learned" section. Discovery questions now have two modes (default = deeper / cold-prep = full). Reframe Hypothesis, Upfront Contract, SPIN ladders, per-persona questions, concrete next-step. Memory + freshness gates. Style section normalized.
- **2026-05-27** — Initial scaffold.