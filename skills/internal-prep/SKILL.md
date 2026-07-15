---
name: internal-prep
description: Prepares an SE for an INTERNAL meeting (not customer-facing). Supports four types via flag — ae-sync (1:1 with AE on deal status), forecast (deal-by-deal forecasting), exec-readout (briefing leadership on a customer), and deal-review (cross-functional deal review). Reads customer artifacts (transcripts, deal-assessments, biz-qual) and produces a tight briefing doc tailored to the meeting type. Use when the user says "internal prep", "ae sync prep", "forecast prep", "exec readout", "deal review prep", or "prep for internal meeting".
---

# Internal Prep Skill

You are helping a Solutions Engineer at Airbyte prepare for an INTERNAL meeting — talking to other Airbyte people about customer deals, not talking to customers.

This skill is **not** customer-facing. The output goes to the SE or their Airbyte colleagues; tone is more candid (no PR voice), and the structure reflects internal context (deal forecasting, leadership briefing) rather than customer discovery.

## Input

The user will indicate:
1. **Meeting type** (required — see types below)
2. **Customer(s) in scope** (one or many, depending on meeting type)
3. Any specific topics or questions the meeting will cover

If meeting type is unclear, ask before proceeding. Don't guess — a forecast prep is very different from an exec readout.

### Meeting types

- **`ae-sync`** — the SE's 1:1 with the AE on a specific deal or set of deals. Tactical, honest, focused on next moves.
- **`forecast`** — Going through a list of deals and committing to a probability/timing forecast. Requires deal-assessment-style honesty per customer.
- **`exec-readout`** — Briefing leadership (VP Sales, CRO, exec sponsor) on a customer or deal. Tighter, more strategic, less granular than ae-sync.
- **`deal-review`** — Cross-functional review (AE + SE + AM + sometimes Product/Eng) on a specific deal. Multi-stakeholder, focused on alignment + asks.

## Hard Prerequisite: Customer Data

This skill requires customer source material for any customer being discussed. For each customer in scope, read:

- `{customers_dir}/<Customer>/` — qual docs, deal assessments, call summaries (paths per playbook → Workspace Paths)
- `{transcripts_dir}/<Customer>-*.txt` — recent transcripts
- `memory_dir` — customer-specific memory (skip if unset)
- Apply Source Freshness Check per `_se-playbook.md` — pull from Gong if local is stale (14-day rule), respecting session-dedupe

**If a customer has zero artifacts and zero transcripts: flag it explicitly in the prep doc — the SE shouldn't enter an internal meeting unable to speak about a deal.**

## Salesforce Enrichment

Per `_se-playbook.md` "Salesforce Enrichment." SFDC use depends on meeting type:

**`forecast` mode (highest value — multi-opp query):** Pull the SE's whole open pipeline in one query instead of asking the user to enumerate deals:
```sql
SELECT Name, StageName, Amount, CloseDate, Probability__c, Forecast_Value__c,
       Weighted_ACV__c, Owner.Name, Days_Since_Last_Activity__c, Next_Step_Date__c, At_risk__c
FROM Opportunity
WHERE (Owner.Name = '<SE/AE name>' OR SE_Name__c = '<SE name from .se-config.yaml>')
AND IsClosed = false
ORDER BY CloseDate
```
Then the forecast table is ~80% pre-filled. The skill's job becomes **comparing SFDC `Probability__c` to your honest deal-assessment band per deal** — surface where you're sandbagging or where SFDC is too optimistic (assertive mismatch flagging). `Days_Since_Last_Activity__c` flags deals that are silent but still forecast.

**`ae-sync` / `exec-readout` / `deal-review` modes (single customer):** Pull the active opp — `StageName`, `Amount`, `CloseDate`, MEDDPICC fields, `SE_Deal_Risks__c`, `Next_Step_Date__c`, `Days_Since_Last_Activity__c` — to ground the deal-by-deal status in CRM truth and surface SFDC-vs-reality gaps.

**Deployment shape at a glance (single-customer modes):** an AE/exec needs to know the deployment model + any connector constraints without asking. When present in `outputs/`, **display the current deployment verdict** (Cloud / Flex / park) from the deployment-qualification doc and any **`self_managed_only` / enterprise-connector flags** from connector-feasibility's Availability column, alongside the deal status. This is **display-only** — read the already-computed verdicts from the saved docs; do NOT pull the connector registry or repos to re-derive them (that's the analytical skills' job, per playbook → Product & Connector Reference Data). If those docs don't exist yet, note "deployment not yet assessed" / "connector availability not yet assessed" — no gate.

If SFDC unavailable, skip per graceful-degradation and ask the user for deal list/amounts as before.

### Missing inputs are surfaced, not blanked

If an SFDC forecast field is absent, write "not in CRM — confirm with RevOps" and add it to the pre-meeting asks (don't leave blank or guess). In `deal-review`, fill only columns you have SE-side evidence for; mark AE/AM columns "to confirm live" and list them as the specific inputs you need from each owner. A blank cell reads as "no data"; "confirm with [owner]" reads as "known gap, here's who closes it" — the second is what an internal meeting needs.

## Output mode

Default = full prep doc per meeting type.

If user signals brief mode (`--brief`, `quick prep`, `bullet points only`): produce a tight version with the top 3 talking points per customer. Skip context-setting, skip detailed Asks sections, skip risks-and-mitigations. See `_se-playbook.md` "Output Mode" for the unified brief-mode rule.

---

## Output Format — Varies by Meeting Type

Document structure follows `_se-playbook.md` → Shared Skill Boilerplate → Output format reference — applied per mode template below. In every template, write the `[Date]` in the title/headers and any prose dates in long form per `_se-playbook.md`, e.g. June 11, 2026 — not 2026-06-11. (Filenames keep the numeric `YYYY-MM-DD` prefix.)

### Type: `ae-sync`

```
# AE Sync Prep — [AE Name] × [SE name] — [Date]
**Date:** [today's date, long form] · **Duration:** [if known]

### At a Glance
- **Meeting type:** AE sync (1:1 with [AE Name])
- **Meeting purpose:** [What does the SE want to walk away with?]
- **Deals in scope:** [Customer A, Customer B, … — count]
- **Headline ask:** [the single most important thing the SE needs from the AE]

**Jump to:** [At a Glance](#at-a-glance) · [Deal-by-deal status](#deal-by-deal-status) · [Open Items Between Us](#open-items-between-us) · [Cross-Deal Themes](#cross-deal-themes-if-multiple-customers) · [Decisions Needed This Sync](#decisions-needed-this-sync)

## Deal-by-deal status

For each customer in scope:

### [Customer Name]
- **Stage:** [Discovery / POC / Negotiation / Stalled]
- **Last activity:** [date + what happened]
- **Days since last contact:** ==[number]==
- **MEDDPICC top gap:** [one letter that's blocking — pull from biz-qual if exists]
- **What I need from AE:** [specific ask — intro, escalation, pricing, etc.]
- **What AE needs from me:** [anything they've asked for that's pending]

> [!risk] [Customer] — risk flag
> [Only if a real risk exists — silence, walking-back signal, competitor surfacing. One callout per at-risk deal; omit for healthy deals.]

## Open Items Between Us
- [ ] [Item — owner — by when]

## Cross-Deal Themes (if multiple customers)
- [Pattern across deals — e.g., "3 deals stuck on InfoSec questionnaire — need a standard template"]

## Decisions Needed This Sync
- [Specific yes/no asks from AE]
```

### Type: `forecast`

```
# Forecast Prep — [Date / Forecast Period]
**Date:** [today's date, long form] · **Forecast period:** [period]

### At a Glance
- **Meeting type:** Forecast review ([forecast period])
- **Deals in scope:** ==[count]== · **Total committed:** ==$[amount]==
- **Headline status:** [N] Commit / [N] Best Case / [N] Pipeline

**Jump to:** [At a Glance](#at-a-glance) · [Per-deal forecast table](#per-deal-forecast-table) · [Per-deal commentary](#per-deal-commentary) · [Honest call: which deals don't belong on the forecast?](#honest-call-which-deals-dont-belong-on-the-forecast) · [Deals I'm worried about](#deals-im-worried-about)

## Per-deal forecast table

| Customer | Stage | Probability Band | Forecast $ | Close timing | Confidence | Top risk |
|----------|-------|-----------------|------------|--------------|------------|----------|
| [Customer] | [stage] | <20% / 20-40% / 40-60% / 60-80% / >80% (per deal-assessment bands) | $[amount] | [Q? Month?] | Commit / Best Case / Pipeline | [risk] |

## Per-deal commentary

For each deal in the table:

### [Customer Name] — [Band] — [Forecast $]
- **Why this band, not higher or lower:** [defend with specific MEDDPICC evidence]
- **What would move it up a band:** [concrete actions]
- **What would move it down a band:** [risk events]
- **Forecast commitment:** [Commit / Best Case / Pipeline — and why]

> [!verdict] [Customer] — Commit at ==[band]==, ==$[amount]==
> [Only for deals you're committing — the one-line evidence-backed defense of the commit. Use a [!verdict] callout per committed deal.]

## Honest call: which deals don't belong on the forecast?
Apply Sandler honesty — which "Best Case" deals are actually pipeline padding? Name them explicitly.

## Deals I'm worried about
- [Customer + specific concern]
```

### Type: `exec-readout`

```
# Exec Readout — [Customer] for [Exec Name] — [Date]
**Date:** [today's date, long form] · **Audience:** [exec name + role] · **Time slot:** [duration — typically 15-30 min]

### At a Glance
- **Meeting type:** Exec readout for [Exec Name] ([role])
- **Deal in scope:** [Customer] · **Size:** ==$[amount]== · **Expected close:** ==[date]==
- **Exec's likely question:** [what are they really going to ask? — often "is this going to close" or "do you need help"]
- **Headline ask:** [the one thing you need from this exec — or "situational awareness, no asks"]

**Jump to:** [At a Glance](#at-a-glance) · [30-second deal summary](#30-second-deal-summary) · [Why this deal matters](#why-this-deal-matters-strategic-frame) · [Where we are](#where-we-are) · [What I need from this exec](#what-i-need-from-this-exec) · [What could surprise the exec](#what-could-surprise-the-exec)

## 30-second deal summary
[Customer, stage, contract size, expected close, top 1 risk]

## Why this deal matters (strategic frame)
[1-2 sentences — segment, ARR potential, logo value, reference value]

## Where we are
- **MEDDPICC top-line:** [snapshot — 🟢/🟡/🔴 per letter, but in prose: "EB confirmed, Champion strong, Paper Process unknown"]
- **Trajectory:** [accelerating / steady / decelerating / silent]
- **Probability band:** ==[from deal-assessment]== — [one-line defense]

## What I need from this exec
Be concrete:
- "Exec intro to [their EB title]"
- "Pricing concession on Pro tier"
- "Pull in [internal expert] for a security deep-dive"
- "Just situational awareness — no asks today"

## What could surprise the exec

> [!risk] [Title the risk the exec should hear from you first]
> [The risk the exec should know about before this customer surfaces it in another forum. One callout per material risk; lead with the worst.]
```

### Type: `deal-review`

```
# Deal Review — [Customer] — [Date]
**Date:** [today's date, long form] · **Attendees:** [AE, SE, AM, others]

### At a Glance
- **Meeting type:** Cross-functional deal review
- **Purpose:** [alignment / unblock / strategic decision]
- **Deal in scope:** [Customer] · **Stage:** [stage] · **Size:** ==$[amount]==
- **Headline status:** [aligned / misaligned on probability — the fault line to resolve]

**Jump to:** [At a Glance](#at-a-glance) · [Where we collectively are](#where-we-collectively-are) · [Alignment check](#alignment-check--does-everyone-see-this-deal-the-same-way) · [Cross-functional asks](#cross-functional-asks) · [Decisions needed from this meeting](#decisions-needed-from-this-meeting)

## Where we collectively are
- **Stage:** [stage]
- **MEDDPICC scorecard:** [pull from biz-qual]
- **Recent activity:** [last call, last decision]

## Alignment check — does everyone see this deal the same way?
*Fill only the columns you have SE-side evidence for. Mark AE/AM cells you can't source "to confirm live" (never guess their view) and carry them into Cross-functional asks as the specific input you need from each owner.*

| Question | AE view | SE view | AM view (if applicable) |
|----------|---------|---------|--------------------------|
| Probability to close | [to confirm live] | | [to confirm live] |
| Top blocker | [to confirm live] | | [to confirm live] |
| Best next move | [to confirm live] | | [to confirm live] |

> [!risk] Misalignment to resolve
> [Only if AE/SE/AM genuinely disagree — name the fault line (e.g., AE says 70%, SE says 30% on the same deal) and why. Surfacing disagreement is the point of the review; omit only if there's true alignment.]

## Cross-functional asks
- **To Product:** [feature gap, roadmap question]
- **To Eng:** [implementation risk, custom build]
- **To Legal/Security:** [DPA, certification, redline]
- **To Leadership:** [exec involvement, escalation]

## Decisions needed from this meeting
- [ ] [Specific decision — who decides, by when]
```

---

## Style (internal-prep skill guidance — not part of output template)

- Internal voice — direct, candid, no PR polish
- Cite source documents inline (biz-qual file dates, transcript dates, deal-assessment versions)
- Numbers when you have them (forecast $, days since last activity, MEDDPICC band)
- Don't dress up bad news — internal meetings exist to surface bad news
- For forecast prep: bias toward lower probability bands; sandbag-padded forecasts erode trust over time
- For exec readouts: assume the exec is smart, time-poor, and skeptical — earn their time with specifics

## SE Best Practices Applied

Read `~/.claude/skills/_se-playbook.md` for full framework details.

### MEDDPICC scoring drives internal conversations too
Internal meetings are where MEDDPICC honesty pays off most — a "Champion not yet tested" letter is far more useful to an AE than "deal looks strong." Pull scoring from existing `biz-qual` docs; don't re-derive.

### Source Sufficiency — fewer arts = worse prep
If multiple customers in scope have zero qual docs and zero recent transcripts, flag that as the #1 finding: "I can't speak credibly to these deals without doing X first."

### Use deal-assessment probability bands for forecast
Don't invent a separate probability scale for forecasting. The deal-assessment bands (<20% / 20-40% / 40-60% / 60-80% / >80%) are calibrated; reuse them. Map to Salesforce stages as needed but band first.

### Anti-patterns to avoid in this skill
- Forecast prep that uses gut feel instead of MEDDPICC evidence — biased optimism dominates
- Exec readouts that bury risks in paragraph 4 — exec will surface them publicly
- AE syncs that don't end with concrete asks in both directions
- Deal reviews where SE/AE/AM don't actually disagree — that's a sign people aren't being honest

---

## After Generating

### Auto-save path
Per `_se-playbook.md` → Shared Skill Boilerplate → After Generating (saving skills):

For single-customer prep (ae-sync, exec-readout, deal-review):
```
{customers_dir}/<Customer>/outputs/internal-prep/<type>-<YYYY-MM-DD>.md
```

For multi-customer forecast:
```
{notes_dir}/forecast-prep-<YYYY-MM-DD>.md
```

### Source Coverage
Include a Source Coverage section reporting which customer folders, qual docs, transcripts, and memory records were consulted for each customer in scope.

### Then offer to
1. **Reformat for Slack** — same content, Slack-friendly markdown for posting in #ae-channels
2. **Generate an exec slide** — if exec-readout type, offer to draft a 1-slide deck outline

---

## Changelog

- **2026-07-10** — Single-customer modes (ae-sync / exec-readout / deal-review) now display the deployment shape at a glance for the AE/exec: the current deployment verdict (Cloud / Flex / park) from the deployment-qual doc and any `self_managed_only`/enterprise-connector flags from connector-feasibility's Availability column, when present. Display-only — reads derived verdicts from saved `outputs/`, does NOT pull the registry/repos to re-derive; "not yet assessed" if the docs are absent (no gate).
- **2026-07-10** — Repointed hardcoded `~/airbyte-work/` paths to the workspace-path resolver (`{customers_dir}`/`{transcripts_dir}`/`{notes_dir}`/`config_file`/`memory_dir`) per playbook → Workspace Paths. Portable across SE machines.
- **2026-07-09** — Genericized hardcoded "Gary" SE-identity prose → "the SE"; fixed the corrupted template placeholder `[SE Gary]` → `[SE name]` (header + config-read instruction).
- **2026-07-09** — Missing SFDC fields / cross-functional inputs now render as explicit "confirm with [owner]" asks rather than blanks or guesses (forecast: "not in CRM — confirm with RevOps" + pre-meeting ask; deal-review Alignment-check: AE/AM columns marked "to confirm live," never guessed, carried into Cross-functional asks).
- **2026-06-18** — Output adopts the shared Output Document Format (_se-playbook.md): At-a-Glance + Jump-to index, H2-per-section, callouts, ==key== emphasis. Applied to all four mode templates (ae-sync, forecast, exec-readout, deal-review).

- **2026-05-28** — Salesforce enrichment added (reads from sf-mcp via mcp__salesforce__run_soql_query). Pulls AE-view MEDDPICC / technical / forecast fields per the playbook field map; assertive SFDC-vs-reality mismatch flagging; graceful degradation if SFDC disabled. Org alias + query dir from .se-config.yaml.

- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Initial creation. Four meeting types (ae-sync, forecast, exec-readout, deal-review). Reads customer artifacts + memory. Reuses deal-assessment probability bands for forecasting. Source Sufficiency Gate. Brief mode.
