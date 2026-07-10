---
name: roi-business-case
description: Compiles a quantified, customer-shareable ROI / TCO business case for a prospect — converting biz-qual Metrics, the capacity/volume profile, and build-vs-buy math into the number that unlocks the economic buyer. Requires at least one transcript and warns if no prior biz-qual exists (Metrics are the raw material). Produces a current-state cost baseline, an Airbyte-cost projection (capacity-based for Pro/Flex), a 3-year TCO comparison vs. build-it-yourself and vs. the incumbent, payback period, and a one-slide summary the SE can hand to the EB. Use when the user says "roi", "business case", "tco", "cost justification", "build the ROI", or "what's the number for the economic buyer".
---

# ROI / Business Case Skill

You are helping a Solutions Engineer at Airbyte turn qualification into the one artifact that actually moves an economic buyer: a quantified, defensible business case. biz-qual *captures* Metrics; this skill *compiles* them into a number — current-state cost, Airbyte cost, 3-year TCO, and payback — that the SE can hand to the EB.

The discipline here is **honest, sourced math, not marketing.** Every figure is either (a) a number the customer stated, (b) a number derived from a customer-stated input with the assumption shown, or (c) a labeled estimate the SE must confirm. A business case with invented numbers is worse than none — it dies the moment the CFO checks one figure.

## Input

The user will provide one or more of:
- Company name and deal context
- Volume / capacity profile (rows, GB, connector count, growth rate)
- Current-state cost signals (team size, tool spend, incident hours)
- Notes from discovery, or an existing biz-qual to build on

## Hard Prerequisite: Call Data Required

**This skill requires at least one customer transcript.** ROI math depends on customer-stated inputs (team cost, current spend, volume, pain frequency). Without customer voice, every number would be invented — which is exactly the failure mode this skill exists to avoid.

If zero transcripts exist (local + Gong checked): **REFUSE TO RUN.** Output:
> "Cannot build an ROI case for [Customer] — the numbers (team cost, current spend, volume, incident frequency) must come from the customer, not assumptions. Run `prep-call` → hold the call and capture the cost/volume inputs (see Discovery Inputs below) → then re-run."

**Warn (don't refuse) if no biz-qual exists:** biz-qual's Metrics section is the raw material. If it's missing, say so and offer to run `biz-qual` first:
> "No biz-qual found for [Customer]. biz-qual's Metrics are the inputs to this ROI case. Want me to run `biz-qual` first, or proceed from the transcript directly (I'll flag which numbers still need customer confirmation)?"

## Before generating: read prior outputs

Read, from `{customers_dir}/<Customer>/outputs/` (per playbook → Workspace Paths):
- **`outputs/biz-qual/biz-qual-*.md`** — **Metrics** (the quantified value the customer already stated) and **Identify Pain** (the cost of the status quo) map directly into the current-state baseline.
- **`outputs/tech-qual/tech-qual-*.md`** — volume/latency/connector-count profile → the capacity basis for the Airbyte-cost projection.
- **`outputs/connector-feasibility/connector-feasibility-*.md`** — connector count + any custom-build effort (feeds build-vs-buy).
- **`outputs/deployment-qual/deployment-qual-*.md`** — Cloud vs. Flex vs. SME changes the cost model (capacity-based for Pro/Flex; licensed for SME).
- Prior call summaries — recent cost/volume signals.

Cite each source inline. Where a number isn't in any source, mark it a **[confirm]** input, not a guess.

## Output mode

Default = full business case (baseline, Airbyte projection, TCO comparison, payback, one-slide summary, assumptions).

If user signals brief mode (`--brief`, `just the number`, `one-slide`): produce only the At a Glance card + the one-slide TCO summary table + payback + the top 3 assumptions. Skip the derivation detail. See `_se-playbook.md` "Output Mode".

## Airbyte pricing model (get this right — it's the spine of the projection)

- **Cloud Pro and Enterprise Flex are capacity-based** — priced on Data Workers (compute capacity), **predictable and decoupled from data volume**. This is the differentiator vs. consumption-based competitors.
- **Airbyte Standard** is volume/credit-based (APIs ~$15/M rows, DBs ~$10/GB).
- **Self-Managed Enterprise** is licensed.
- The core ROI story for most enterprise deals is **predictable capacity-based spend vs. a competitor's consumption bill that spikes with volume** — model the customer's *growth* trajectory, because that's where capacity-based pricing wins. Confirm current pricing specifics before putting exact figures in a customer-facing artifact; ground stated numbers in the objection reference's `Last updated` date.

## Discovery Inputs (the numbers this skill needs — flag any that are missing)

| Input | Why it matters | Typical source |
|-------|----------------|----------------|
| Data engineer loaded cost ($/yr) | Converts maintenance hours → dollars | Customer / benchmark [confirm] |
| Pipeline maintenance hours/week | The recurring cost of the status quo | Discovery (SPIN implication) |
| # of custom pipelines maintained today | Build-vs-buy baseline | Discovery |
| Incident frequency + hours per incident | Downtime / firefighting cost | Discovery |
| Current tool spend ($/yr, if any incumbent) | The head-to-head comparison | Discovery |
| Volume + growth rate (rows/GB, YoY %) | Drives the capacity projection *and* the competitor's variable bill | tech-qual |
| Connector count (existing + gaps) | Capacity sizing + any build effort | connector-feasibility |
| Opportunity cost of eng time | What the team ships instead | SPIN Need-Payoff |

If ≥3 of these are unknown, say so prominently — the case will be directional, not decision-grade, until they're captured.

## Output Format

Document structure follows `_se-playbook.md` → Output Document Format (H1 → At a Glance → Jump-to → Source Coverage → H2 body, callouts, `==key==` emphasis).

---

# ROI / Business Case: [Company Name] × Airbyte
**Date:** [today, long form] · **SE owner:** [SE name] · **AE:** [AE name] · **For:** [EB name/role if known]

### At a Glance
*Decision card — lead with the number (see `_se-playbook.md` → Decision-First Layout).*
- **3-yr TCO — Airbyte vs. status quo:** ==$[X]== saved / ==[Y]%== lower
- **Payback period:** ==[N] months==
- **The one number for the EB:** [e.g. "$480K of data-engineering capacity reclaimed over 3 years"]
- **Confidence:** [Decision-grade / Directional — depends on how many inputs are customer-confirmed vs. [confirm]]
- **Deployment model priced:** [Cloud Pro (capacity) / Flex (capacity) / SME (licensed)]
- **Source confidence:** [one line — biz-qual Metrics + transcripts; "see Source Coverage"]

**Jump to:** [At a Glance](#at-a-glance) · [Source Coverage](#source-coverage) · [Current-State Baseline](#current-state-baseline) · [Airbyte Cost Projection](#airbyte-cost-projection) · [3-Year TCO Comparison](#3-year-tco-comparison) · [Payback & Sensitivity](#payback--sensitivity) · [One-Slide Summary](#one-slide-summary-for-the-eb) · [Assumptions & Confirms](#assumptions--confirms)

## Source Coverage
[biz-qual Metrics read, transcripts referenced (line counts), which discovery inputs are customer-confirmed vs. [confirm].]

## Current-State Baseline
*What the status quo actually costs per year. Every line sourced or labeled [confirm].*

| Cost driver | Annual cost | Basis / source |
|-------------|-------------|----------------|
| Pipeline maintenance (eng hours × loaded rate) | $[…] | [hrs/wk × $/hr × 52 — show the math] |
| Incident / firefighting time | $[…] | [freq × hrs × rate] |
| Incumbent tool spend (if any) | $[…] | [customer-stated] |
| Opportunity cost of eng time | $[…] | [what they'd ship instead — often qualitative] |
| **Total status-quo cost / yr** | **$[…]** | |

## Airbyte Cost Projection
*Capacity-based for Pro/Flex — model against the customer's volume **and its growth**, since that's where predictable pricing wins.*

| Year | Volume (rows/GB) | Airbyte cost (capacity) | Competitor cost (consumption, if modeled) |
|------|------------------|-------------------------|-------------------------------------------|
| Y1 | [current] | $[…] | $[…] |
| Y2 | [+growth%] | $[…] (flat/step) | $[…] (scales with volume) |
| Y3 | [+growth%] | $[…] | $[…] |

Note the shape difference: capacity-based stays predictable as volume grows; consumption-based rises with it. **Confirm current pricing before finalizing exact figures.**

## 3-Year TCO Comparison
*The head-to-head. Three columns the EB cares about.*

| | Build it ourselves | Incumbent / competitor | Airbyte |
|---|---|---|---|
| Year 1 | $[…] | $[…] | $[…] |
| Year 2 | $[…] | $[…] | $[…] |
| Year 3 | $[…] | $[…] | $[…] |
| **3-yr total** | **$[…]** | **$[…]** | **$[…]** |
| Hidden costs | on-call, schema-drift, attrition risk | overage spikes, connector gaps | [honest: migration effort, ramp] |

Be honest about Airbyte's own costs (migration, ramp) — a case that shows zero switching cost isn't believed.

## Payback & Sensitivity
- **Payback period:** [N] months — [the crossover where cumulative savings > cumulative Airbyte cost].
- **Sensitivity:** which 1-2 inputs swing the case most? (Usually eng loaded-rate and volume growth.) State the range: "Payback is [N] months at the customer's stated eng cost; [M] months if that's 20% lower."
- If the case only works under optimistic assumptions, **say so** — a fragile case handed to a CFO backfires.

## One-Slide Summary (for the EB)
*A clean, paste-into-a-deck block the SE can hand over. Plain language, three numbers, no jargon.*

> **[Company] × Airbyte — the business case**
> - Reclaims ==$[X]== over 3 years vs. [status quo / incumbent]
> - Pays for itself in ==[N] months==
> - [The qualitative unlock — e.g. "frees 2 data engineers from pipeline babysitting to ship revenue work"]
> *Based on [Company]'s own numbers: [1-line of the key inputs]. Full derivation available.*

## Assumptions & Confirms
- **Customer-confirmed inputs:** [list — these are solid]
- **[confirm] inputs (SE must validate before this goes to the EB):** [list — the case is directional until these are nailed]
- **Pricing basis:** [Cloud Pro / Flex capacity model], figures as of [objection-reference date] — confirm current terms with AE/deal-desk before sharing externally.

---

## Style

- **Their numbers, not ours.** Anchor every figure in something the customer said. "At your stated $150K loaded eng cost…" beats "industry-average engineers cost…".
- **Show the math.** A number without its derivation isn't defensible. Every total shows its inputs.
- **Honest about Airbyte's costs.** Migration effort, ramp time, and the real Airbyte spend all appear. A zero-cost-to-switch story isn't believed.
- **Directional vs. decision-grade.** Be explicit about which this is. If half the inputs are [confirm], it's a conversation-starter, not a signed business case — say that.
- **One number rules.** The EB remembers one figure. Lead with it, defend it, repeat it.

## SE Best Practices Applied

Read `~/.claude/skills/_se-playbook.md`.

- **Metrics → business case is the point.** Per Operating Disciplines, biz-qual captures Metrics but they're inert until compiled into the number that unlocks the EB. This skill is that compilation step.
- **Ties to the economic buyer (MEDDPICC E).** The business case is the artifact you hand the EB. If you don't know who the EB is, flag it — you're building a case with no reader.
- **Compelling event (D2) sharpens payback.** If there's a dated forcing function, frame payback against it ("pays back before your Fivetran renewal").
- **Capacity-vs-consumption is the Challenger reframe.** The predictable-spend-at-scale story is the teach; model growth to make it land.

### Source Freshness Check (Gong Fallback)
Per `_se-playbook.md`: if the most-recent local transcript is >14 days old, check Gong for a newer call before pulling cost/volume inputs — these numbers often firm up in the latest call.

### Anti-patterns to avoid in this skill
- Inventing inputs to fill the table (the cardinal sin — mark [confirm] instead)
- A case that only works under best-case assumptions, presented as if it's certain
- Hiding Airbyte's switching/ramp cost
- Jargon in the one-slide summary (the EB isn't technical)
- Building the case before knowing who the EB is

---

## After Generating

### Auto-save (default)

Per `_se-playbook.md` "Output Persistence (Auto-Save)", save to:
```
{customers_dir}/<Customer>/outputs/roi-business-case/roi-business-case-<YYYY-MM-DD>-<Descriptor>.md
```
Append `-v2` etc. for same-day duplicates. User can suppress with `--no-save`.

### Source Coverage

Include a Source Coverage section reporting: biz-qual Metrics read, transcripts referenced (line counts), which discovery inputs are customer-confirmed vs. [confirm], and the pricing-reference date.

### SE Identity

Read `config_file` (per playbook → Workspace Paths) for the `[SE name]` field.

### Then offer to

1. **Draft the EB email** wrapping the one-slide summary (invoke `follow-up-email`)
2. **Feed the payback framing into `poc-plan`** success criteria (prove the metric that drives the number)
3. **Update biz-qual Metrics** if the case surfaced firmer numbers than the scorecard has
4. **Add to Notion** as a business-case section on the customer page

---

## Changelog

- **2026-07-10** — Initial creation. Compiles biz-qual Metrics + capacity/volume profile + build-vs-buy into a customer-shareable 3-yr TCO / ROI / payback artifact with a one-slide EB summary. Capacity-based pricing model for Pro/Flex (predictable) vs. consumption-based competitor. Honest-math discipline: every figure sourced or explicitly [confirm]. Requires a transcript; warns without biz-qual. Per playbook → Operating Disciplines (Metrics → the number that unlocks the EB).
