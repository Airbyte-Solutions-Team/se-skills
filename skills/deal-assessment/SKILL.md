---
name: deal-assessment
description: Generates an honest, structured Deal Assessment for a prospect. Requires at least one transcript OR one real qualification doc (refuses on empty source). Reads all transcripts, prior Deal Assessments, biz-qual, tech-qual, deployment-qual, connector-feasibility, call summaries, and memory. Produces MEDDPICC pre-scorecard, Activity Trajectory (silence-as-signal), and the seven required narrative sections (Driver, Need, Urgency, What Would Close It, Deal Blocker, What Would Lose It, Bottom Line) — Bottom Line uses constrained probability bands (<20% / 20-40% / 40-60% / 60-80% / >80%) with required evidence. Includes Coaching Observations section for SE-craft growth. Use when the user says "deal assessment", "assess the deal", "deal health", "is this deal real", or wants a candid read on whether a customer will close.
---

# Deal Assessment Skill

You are helping a Solutions Engineer at Airbyte produce a candid, structured assessment of deal health for a specific customer. The output must follow the exact format defined in the **Output Format** section of this file (self-contained — do not depend on any file outside this repo).

## Input

The user will name a customer (e.g., "deal assessment for Acme"). You should:

1. **Read all transcripts** in `{transcripts_dir}/` (per playbook → Workspace Paths) matching the customer
2. **Read prior outputs** in `{customers_dir}/<Customer-Name>/outputs/<skill>/` — especially:
   - Prior `outputs/deal-assessment/deal-assessment-*.md` files (compare against — fuels the "What Changed Since Last Assessment" section)
   - `outputs/biz-qual/biz-qual-*.md` (use MEDDPICC scoring as input, don't re-derive)
   - `outputs/tech-qual/tech-qual-*.md` (technical risk feeds into Deal Blocker analysis)
   - `outputs/deployment-qual/deployment-qual-*.md` (deployment-model verdict — if 🔴, that's often the deal)
   - `outputs/connector-feasibility/connector-feasibility-*.md` (gap analysis)
   - `outputs/post-call/post-call-*.md` files (pre-digested call content)
3. **Read memory** — `memory_dir/MEMORY.md` (per playbook → Workspace Paths; skip gracefully if unset) and any customer-specific memory files (active blockers, pending Airbyte-side actions)
4. **Optionally pull Notion context** if the user references it (use Notion:search to find the customer's parent page, then read Overview and Q&A subpages)
5. Read full source material before synthesizing — do not skim
6. Cite source documents inline (filename + date) when pulling in prior conclusions

## Hard Prerequisite: Call Data Required

**This skill requires at least one customer transcript OR at least one real qualification doc to run.** A deal assessment is a synthesis of customer voice and qualification — without either, the output is wishful forecasting.

## Source Sufficiency Gate

Before generating, check whether you have enough to write an honest assessment:

| Available sources | Action |
|-------------------|--------|
| ≥2 transcripts + ≥1 qualification doc (biz/tech/deployment) | ✅ Proceed — full assessment |
| 1 transcript + ≥1 qual doc | ✅ Proceed — full assessment with thin-base caveat |
| 1 transcript only, no qual docs | ⚠️ Proceed but flag: "thin source base — recommend running biz-qual + tech-qual before relying on this" |
| 0 transcripts but ≥1 real qual doc | ⚠️ Proceed but flag: "no recent call data — assessment based on prior qualification only; recommend a fresh call" |
| **0 transcripts AND 0 qual docs** | **🛑 REFUSE TO RUN.** Output the refusal message below. |
| Memory exists but is >30 days old | Note staleness; verify key claims against current sources before asserting |

### Refusal message (when 0 transcripts AND 0 qual docs)

> "Cannot generate deal-assessment for [Customer] — no customer voice in any source. Deal health requires actual conversation data, not hypotheses about what the deal *might* look like.
>
> Recommended sequence:
> 1. Run `prep-call` to plan the first call
> 2. Save the transcript after the call
> 3. Run `post-call` to digest it
> 4. Run `biz-qual` for MEDDPICC scoring
> 5. THEN deal-assessment will have data to work with"

Don't generate a confident-looking assessment from thin data — say what's missing.

## Output mode

Default = full assessment (MEDDPICC pre-scorecard, Activity Trajectory, all 7 narrative sections, probability bands, coaching observations).

If user signals brief mode (`--brief`, `quick assessment`, `deal health summary`): produce just the punchy verdict + Activity Trajectory + Bottom Line with probability band + #1 deal blocker. Skip MEDDPICC scorecard, all narrative sections, coaching layer. See `_se-playbook.md` "Output Mode" for the unified brief-mode rule.

## Output Format

Document structure follows `~/.claude/skills/_se-playbook.md` → Shared Skill Boilerplate → Output format reference.

Title format: `<Customer> — Deal Assessment: <short punchy verdict>`

The punchy verdict should be honest, e.g.:
- "Likely to close Q2, blocked on legal review"
- "Stalled — no economic buyer identified"
- "Strong technical fit, soft on commercial urgency"
- "At risk — competitor has executive sponsorship"

---

# <Customer> — Deal Assessment: <punchy verdict>
**Date:** [today's date — long form per `_se-playbook.md`, e.g. June 11, 2026, NOT 2026-06-11 or MM.DD.YY] · **Stage:** [Discovery / POC / Negotiation / Closed Won / Closed Lost / Stalled]

### At a Glance
*Decision card — lead with the call (see `_se-playbook.md` → Decision-First Layout).*
- **Probability:** ==[band, e.g. 40–60%]== ([dead/dying / at risk / likely / very likely / committed])
- **Stage:** [stage] · **Trajectory:** [🟢 Accelerating / 🟡 Steady / 🔴 Decelerating / 🔴 Silent]
- **#1 Blocker:** [one line — name the person/process/alternative]
- **Recommended motion:** [the single highest-leverage next move]
- **Driver:** [one line — what's pushing them now]
- **Source confidence:** [one line — N transcripts + notes, dates; "see Source Coverage"]

**Jump to:** [At a Glance](#at-a-glance) · [Source Coverage](#source-coverage) · [Activity Trajectory](#activity-trajectory) · [What Changed Since Last Assessment](#what-changed-since-last-assessment) · [Driver](#driver) · [Need](#need) · [Urgency](#urgency) · [What Would Close It](#what-would-close-it) · [Deal Blocker](#deal-blocker) · [What Would Lose It](#what-would-lose-it) · [Bottom Line](#bottom-line) · [Coaching Observations](#coaching-observations)
*(Include the "What Changed Since Last Assessment" anchor only when a prior assessment exists — see that section; drop it from this line on a first assessment.)*

---

## Activity Trajectory
*Silence is signal. Quantify the deal's cadence and whether it's healthy.*

| Metric | Value |
|--------|-------|
| Total calls captured | [count of transcripts] |
| Date range of activity | [first call → most recent call] |
| Calls in last 30 days | [count] |
| Days since most recent call | [number] |
| Current trajectory | 🟢 Accelerating / 🟡 Steady / 🔴 Decelerating / 🔴 Silent (>30d) |

**What this signals:**
[1-2 sentences. Acceleration = healthy. Silence on a deal with a forcing function = serious deal-decay signal. Long gaps between meetings + verbal commitments not yet executed = walking it back. Be honest about what the cadence indicates. Wrap a headline figure such as ==31 days silent== if it's decision-relevant.]

---

## What Changed Since Last Assessment
*Conditional — include this section ONLY when a prior `outputs/deal-assessment/deal-assessment-*.md` exists. On a first assessment, omit it entirely (and drop it from the Jump-to line). This is what turns the skill from a one-time snapshot into health monitoring.*

Compared to the prior assessment ([its date]):
- **Probability band:** [prior band → current band — and the one reason it moved, or "unchanged"]
- **MEDDPICC movement:** [which letters moved up/down since last time — e.g. "Economic Buyer 🔴→🟡 (met the VP Data on 06.02)"]
- **Timeline / forcing function:** [shifted? slipped? newly introduced?]
- **New since last time:** [new objections, new stakeholders, new blockers, or new positive signals]
- **Net read:** [one line — is this deal healthier or weaker than at the last assessment, and why]

---

## Driver
What's pushing them to evaluate Airbyte *right now*? (Not what their general problem is — what made them pick up the phone this quarter.) If you can't identify one, say so plainly — that's a red flag.

## Need
What do they actually require from the product? Be specific — connectors, deployment model, volume, latency, compliance. Distinguish must-have from nice-to-have.

## Urgency
What's the **compelling event** (D2 — a dated, external forcing function: contract renewal, migration deadline, compliance/audit date, funding milestone, system sunset)? "They're keen" or "sometime this year" is not a compelling event — if that's all there is, say so plainly: absence of a compelling event is why deals slip a quarter every quarter, and it caps the probability band. If a real forcing function exists, does the backward-planned timeline (signature → procurement → security → POC) actually fit before it? See `_se-playbook.md` → Operating Disciplines D2.

## Stakeholder read (multi-threading)
Pull the stakeholder map from the customer's biz-qual (per `_se-playbook.md` → Operating Disciplines) if it exists; otherwise reconstruct a quick who's-who. Flag two things: (1) are we **single-threaded** (deal lives on one contact — a top failure mode)? (2) is there a **coach masquerading as a champion** (friendly and informative but no power/access)? Both belong in the health read.

## What Would Close It
Specific levers that could move this to signature. Be concrete — "POC success on Workday source", "exec demo with their CDO", "pricing concession on Pro tier", "introducing them to reference customer in financial services".

## Deal Blocker
The primary obstacle to closing. There's usually one big thing. Name it. If there are multiple, rank them.

## What Would Lose It
What kills this deal entirely? Competitor selection? Budget cut? Internal build decision? Champion leaving? Be honest about the failure modes.

## Bottom Line
One paragraph. Honest assessment of deal health. Not optimistic, not pessimistic — accurate.

**Probability estimate — use bands, not point estimates.** Render the chosen band as a verdict callout, picking the type by band: `[!verdict]` if ≥60%, `[!risk]` if 20–60%, `[!blocker]` if <20%. Wrap the band figure in `==…==`.

```markdown
> [!risk] Probability ==40–60%== (likely if execution stays clean)
> In this band because: (1) confirmed EB engagement on 04.15, (2) champion passed all three tests, (3) but paper process is unknown and creates execution risk.
```

| Band | When to use |
|------|-------------|
| <20% (dead/dying) | Any of: 30+ days of silence, walking-back signals confirmed, competitor entrenched, EB never found, customer publicly disqualified |
| 20–40% (at risk) | Mid-cycle deals with major MEDDPICC gaps (no EB, weak Champion, vague Pain). **This is where most honestly-assessed mid-cycle deals sit.** |
| 40–60% (likely if execution stays clean) | Strong MEDDPICC on most letters, healthy cadence, named EB and Champion (Champion tested against all 3 criteria), clear forcing function. Still has execution risk. |
| 60–80% (very likely) | All MEDDPICC letters 🟢, verbal commit, paper process in flight, exec sponsorship, no significant outstanding objections |
| >80% (committed) | Order form sent, redlines minor, executive verbal commit, no surprises on the horizon |

**No default band — pick based on evidence each time.** Most mid-cycle deals honestly sit in 20–40% (at risk); happy ears push them higher than they should be.

Defend the band with specific evidence. Don't say "40-60%" — say "in the 40-60% band because: (1) confirmed EB engagement on 04.15, (2) champion passed all three tests, (3) but paper process is unknown and creates execution risk."

**Bias toward the lower band when uncertain.** Happy-ears assessments are worse than no assessment.

If you don't have enough signal to band, say "Unable to estimate — source base too thin."

---

## Coaching Observations (For the SE's Growth)
*This section is for the SE, not for the deal. Flag SE-craft issues surfaced by the source material:*

- **Happy-ears moments:** Times when verbal positivity wasn't backed by a next step
- **Skipped Implication:** Pains stated but not quantified — the customer said it but the SE didn't follow up with "what does that cost?"
- **Weak next-steps:** Calls that ended with "we'll follow up" instead of date + attendees + agenda
- **Solution-pitching too early:** Airbyte features pitched before the customer articulated the underlying problem
- **Walking-it-back signals missed:** Stakeholders softening commitment that wasn't addressed directly

Keep candid. This is the part of the assessment the SE can act on personally.

---

## Style

- **Brutally honest** — internal use, not customer-facing. Sugarcoating wastes the SE's time.
- **Specific over generic** — "Jordan mentioned competitor X by name on 04.01" beats "they're considering alternatives"
- **Cite sources** — every material claim references a transcript date + speaker, a memory file, or a prior qual doc
- **Flag what you don't know** — "Urgency: unknown — not asked in available transcripts" beats a guess
- **Separate fact from read** — when you state something the customer did NOT say, label it *"(inferred — not stated)"* (per `_se-playbook.md` → Decision-First, facts vs. judgment). The narrative sections are judgment; keep the evidence behind them visible.
- **Compare to prior assessments** when they exist; flag movement in the "What Changed Since Last Assessment" section
- **Bias toward pessimism over optimism** — happy-ears assessments are worse than no assessment

## After Generating

### Auto-save path
Per `~/.claude/skills/_se-playbook.md` → Shared Skill Boilerplate → After Generating (saving skills), save to:
```
{customers_dir}/<Customer>/outputs/deal-assessment/deal-assessment-<YYYY-MM-DD>.md
```

### Source Coverage
Include a Source Coverage section at the top reporting:
- Every transcript read (filename + line count: e.g., "Acme-04.01.26.txt — 566 / 566 lines")
- Every prior qual doc read (filename + date)
- Memory records consulted (filename + last update date)
- Notion pages accessed (if any)
- Any source inventoried but not read in full — explicitly say so

### Then ask which other artifacts to update
1. **Mirror to Notion** as a subpage under the customer's parent page
2. **Update memory** — if this assessment surfaced a significant status change (e.g., deal moved from "active POC" to "stalled", or a new deal-killer emerged, or trajectory flipped from 🟢 to 🔴), propose adding/updating a project memory. Don't update for routine cadence — only material status changes.

Wait for explicit yes/no on Notion / memory before doing those.

---

## SE Best Practices Applied to Deal Assessment

Read `~/.claude/skills/_se-playbook.md` for full framework details.

### Source Freshness Check (Gong Fallback)
Before synthesizing, check whether local transcripts are current. Per `_se-playbook.md` ("Source Freshness Check"):
- If the most-recent local transcript is more than **14 days old**, OR the user signals "check for new activity," search Gong for newer calls
- Pull the **most recent call only** — do not bulk-pull
- Save any pulled transcript to `_transcripts/<Customer-Name>-MM.DD.YY.txt` BEFORE using it (workspace transcript convention)
- If no newer Gong call exists, say so explicitly — silence on Gong is itself a signal (often a deal-decay flag worth noting in the assessment)

### Salesforce Enrichment (full account arc — deepest SFDC read)
Per `_se-playbook.md` "Salesforce Enrichment." This skill reads the **full account arc**, not just the active opp. Pull:
- **Active opp** (matching rule): all MEDDPICC fields (`Economic_Buyer__c`, `Champion__c`, `Identify_Pain__c`, `Decision_Process__c`, `Primary_Competitor__c`, `Gong__MainCompetitors__c`, `Required_features_functionality__c`), the three whys (`Why_buy_anything__c`, `Why_buy_now__c`, `Why_buy_from_Airbyte__c`), `SE_Deal_Risks__c`, `Probability__c`, `StageName`, `Amount`, `CloseDate`, `Next_Step_Date__c`, `Days_Since_Last_Activity__c`
- **Trajectory:** stage-date series (`Stage_X_Date__c`, `Stage_Time_X_Y__c`, `Last_Stage_Change_Date__c`) → feed the Activity Trajectory section
- **Account arc:** all opps on the account — prior wins, prior losses (`Closed_Reason__c`, `Closed_Lost_Detail__c`, `Stage_Before_Closed_Lost__c`), existing ARR

**How to use it (assertive mismatch flagging):**
- Cross-reference every SFDC MEDDPICC field against the transcripts. AE filled "Champion: Michel" but Michel hasn't been on a call in 6 weeks → flag it.
- SFDC `Probability__c` vs. your honest probability band → surface the delta explicitly (this is the forecasting-honesty check).
- Map the three whys to your Driver / Urgency / What-Would-Close sections — and flag where SFDC claims a driver/urgency the transcripts don't support.
- Use prior-loss reasons to inform "What Would Lose It" (we've lost here before on X — is X still a risk?).
- Use existing ARR to frame the deal: expansion vs. net-new changes the whole assessment.

Whenever there's a material gap, add a `## SFDC vs. Reality` section to the output and render the mismatch as a `[!risk]` callout:

```markdown
## SFDC vs. Reality
> [!risk] SFDC says Negotiation; reality is mid-discovery
> AE set `StageName` = Negotiation and `Probability__c` = 80%, but `Days_Since_Last_Activity__c` = ==45== and no POV exists. Champion field names someone absent from every transcript. Treat the forecast as happy-ears.
```

If SFDC unavailable, skip per graceful-degradation and note it.

### Apply Cross-Transcript Analysis
This skill almost always operates over multiple transcripts and notes. Read the "Cross-Transcript Analysis" section in `_se-playbook.md` before synthesizing. Specifically:
- Read ALL available transcripts, not just the most recent (recency-weight, don't recency-skip)
- Cross-reference topics across calls — surface contradictions in a dedicated section, classified as Evolution / Stakeholder split / Walking it back
- Flag topics that went quiet (resolved vs. abandoned — both are diagnostic)
- Cite sources inline (transcript date + speaker) when making claims

### Score every MEDDPICC letter — don't just narrate
Before writing the prose sections, build an internal MEDDPICC scorecard. The Deal Assessment sections (Driver, Need, Urgency, etc.) should be informed by which MEDDPICC letters are 🟢/🟡/🔴.

- "Strong technical fit but no EB access" = a specific MEDDPICC gap, not vague concern
- "Champion can't name decision-makers" = Champion is 🔴, not "developing"

Surface the worst-scoring letters in the Bottom Line.

### Apply the "happy ears" test
Before writing the verdict, ask: what specific evidence supports my optimism? If you're forecasting Commit because customer said "this looks great," that's happy ears, not signal. Push back on optimism with: "Have they given any reason to think they *won't* close?" If they haven't, you don't have enough signal — and that should be in the assessment.

### "What Would Lose It" must include the build-vs-buy story
For data infra deals, the most common loser isn't a competitor — it's "we'll build it ourselves." Always evaluate: does this customer have the engineering capacity and motivation to build? Have we surfaced the TCO of doing so? If "build it" is alive in the customer's head and we haven't addressed it, that's a Deal Blocker.

### Deal Blocker should name a person or a process, not a feeling
Weak: "Concerns about pricing."
Strong: "CFO Sarah Kim hasn't seen the ROI model. Champion is reluctant to push without exec sponsorship from CDO."

If the blocker is abstract, the assessment is incomplete.

### Use Sandler negative-reverse framing in Bottom Line when warranted
If the deal is drifting, the honest verdict might be: "Based on three reschedules and no exec engagement after 6 weeks, this is more likely to die quietly than to close. Recommend forcing a real 'no' via [specific action] rather than continuing to invest cycles."

This is the kind of honest read that justifies the skill existing.

### Compare to prior Deal Assessment
If a prior `outputs/deal-assessment/deal-assessment-*.md` already exists for this customer, the new one MUST include the "What Changed Since Last Assessment" section (see the template, after Activity Trajectory). Did MEDDPICC letters move up or down? Did the timeline shift? New objections? Without this comparison, the assessment is a snapshot — not health monitoring.

### Anti-patterns to avoid in this skill
- Optimistic "Bottom Line" without specific evidence
- Deal Blocker that's a feature gap rather than a stakeholder, process, or alternative
- Probability estimates with no defensible basis
- Burying the "should we keep working this?" question instead of answering it

---

## Changelog

- **2026-07-10** — Sharpened Urgency into an explicit compelling-event + backward-timeline read (D2), and added a Stakeholder read (multi-threading / coach-vs-champion, D3/D4) that pulls the biz-qual stakeholder map. Per playbook → Operating Disciplines.
- **2026-07-10** — Repointed hardcoded `~/airbyte-work/` paths to the workspace-path resolver (`{customers_dir}`/`{transcripts_dir}`/`{notes_dir}`/`config_file`/`memory_dir`) per playbook → Workspace Paths. Portable across SE machines.
- **2026-07-09** — Genericized hardcoded "Gary" SE-identity prose → "the SE" (Coaching Observations section + description) so the skill isn't tied to one operator.
- **2026-07-09** — Added the **"What Changed Since Last Assessment"** section to the Output Format template + Jump-to index (was referenced in prose but had no slot — the "MUST include" instruction produced inconsistent output). Reconciled the two names ("Movement" / "What Changed") to one. Fixed the prior-doc read path + glob: reads from `outputs/deal-assessment/deal-assessment-*.md` (was capitalized `Deal-Assessment-*.md` at the customer root — missed the file it saves), and the other prior docs from `outputs/<skill>/`; `call-summary-*.md` → `post-call-*.md`. Together this makes the health-monitoring comparison actually work (D1 + D2).
- **2026-07-09** — Verified self-contained (P7): the required 7-section format (Driver / Need / Urgency / What Would Close It / Deal Blocker / What Would Lose It / Bottom Line, each with its one-line intent + in the Jump-to index) is defined in this file's Output Format section and matches the canonical spec; no external dependency remains, band logic unchanged. (Known follow-on, out of scope: reconcile the prose "Movement / What Changed Since Last Assessment" references with the template — currently referenced but not templated.)
- **2026-07-09** — Removed the external `~/airbyte-work/CLAUDE.md` dependency for the output format. The skill's own "Output Format" section already defined the full template (all 7 sections + At-a-Glance + probability bands, richer than the CLAUDE.md spec), so the reference was vestigial — repointed to it. Skill is now self-contained.

- **2026-06-18** — Output adopts the shared Output Document Format (_se-playbook.md): At-a-Glance + Jump-to index, H2-per-section, callouts, ==key== emphasis.

- **2026-05-28** — Salesforce enrichment added (reads from sf-mcp via mcp__salesforce__run_soql_query). Pulls AE-view MEDDPICC / technical / forecast fields per the playbook field map; assertive SFDC-vs-reality mismatch flagging; graceful degradation if SFDC disabled. Org alias + query dir from .se-config.yaml.

- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Hard Source Sufficiency Gate refuses on 0 transcripts AND 0 qual docs. Reads all prior qualification outputs + memory. Activity Trajectory section (silence-as-signal becomes structural). MEDDPICC pre-scorecard. Probability bands constrained (<20%/20-40%/40-60%/60-80%/>80%) — no default band, bias toward lower band when uncertain. Coaching Observations section integrated into output. After Generating with memory-write. Style normalized.
- **2026-05-27** — Initial scaffold.
