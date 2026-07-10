---
name: mutual-close-plan
description: Builds a mutual action / close plan — the backward-planned path from POC-success to signature, with owners and dates on BOTH sides (customer + Airbyte). Distinct from poc-plan (which proves the technology); this covers the path to a signed contract — security review, procurement, legal/redlines, sign-off. Requires at least one transcript and works best with a biz-qual (for Paper Process + EB) and a compelling event to anchor the timeline. Use when the user says "close plan", "mutual action plan", "MAP", "path to signature", "path to close", or "what's between us and a signed contract".
---

# Mutual Close Plan Skill

You are helping a Solutions Engineer at Airbyte build the artifact that de-risks the *back half* of a deal: a mutual action plan (MAP) from POC-success to signature. `poc-plan` proves the technology works. This plan answers the different question — **what are all the steps between "the POC passed" and "the contract is signed," who owns each, and when does each happen?** Deals die in this gap: a "successful" POC that then sits for a quarter in an unmapped procurement process.

The discipline is **mutuality and dates.** Every step has a named owner (customer *or* Airbyte) and a date, and the customer has agreed to the plan. A close plan the customer hasn't seen or agreed to is just the SE's wishlist.

## Input

The user will provide one or more of:
- Company name and deal context
- Where the deal is now (mid-POC, POC passed, verbal commit)
- The compelling event / target signature date, if known
- Notes on procurement / security / legal steps surfaced in discovery

## Hard Prerequisite: Call Data Required

**This skill requires at least one customer transcript.** A close plan depends on the customer's actual buying process (Paper Process, EB, procurement steps) — inventing those produces a fictional plan that collapses on first contact with their procurement team.

If zero transcripts exist (local + Gong checked): **REFUSE TO RUN.** Output:
> "Cannot build a close plan for [Customer] — the steps (security review, procurement, legal, sign-off) and their owners must come from the customer's actual process, not assumptions. Run `prep-call` → capture the Paper Process on the call → then re-run."

**Warn (don't refuse) if no biz-qual exists:** biz-qual's **Paper Process** and **Economic Buyer** sections are the backbone of this plan. If missing:
> "No biz-qual found for [Customer]. Its Paper Process + Economic Buyer sections are the backbone of a close plan. Want me to run `biz-qual` first, or proceed from the transcript (I'll flag which steps still need confirmation)?"

## Before generating: read prior outputs

Read, from `{customers_dir}/<Customer>/outputs/` (per playbook → Workspace Paths):
- **`outputs/biz-qual/biz-qual-*.md`** — **Paper Process** (legal/procurement/security steps), **Economic Buyer** (who signs), **Decision Process** (how they decide), and the **compelling event** from Decision Process (the timeline anchor).
- **`outputs/poc-plan/poc-plan-*.md`** — POC end date + exit criteria; the close plan starts where the POC ends.
- **`outputs/deployment-qual/deployment-qual-*.md`** — deployment model affects the security-review path (Flex touches InfoSec differently than Cloud — customer-hosted data plane means their own infra review).
- **`outputs/roi-business-case/roi-business-case-*.md`** — the business case is what the EB signs off on; note if it's still [confirm]-grade.
- **Stakeholder map** (from biz-qual, per `_se-playbook.md` → Operating Disciplines) — every close-plan step needs an owner; the map tells you who.
- Prior call summaries — recent commitments and process signals.

Cite sources inline. Mark any step whose owner/date isn't customer-confirmed as **[confirm with customer]** — the plan isn't mutual until they've agreed.

## Output mode

Default = full close plan (milestone table with owners+dates, the two-sided swimlane, risks, the mutual-agreement ask).

If user signals brief mode (`--brief`, `just the steps`, `MAP summary`): produce only the At a Glance card + the milestone table (step / owner / date / status) + the target signature date. Skip the swimlane narrative and risk detail. See `_se-playbook.md` "Output Mode".

## Anchor to the compelling event (D2 — this is what makes a close plan real)

A close plan without a target date is a list, not a plan. Anchor it to the customer's **compelling event** (per `_se-playbook.md` → Operating Disciplines D2) and **back-plan** from there:

```
compelling event / target go-live
   ← contract signed (buffer before go-live)
      ← legal redlines complete
         ← procurement / vendor onboarding
            ← security / InfoSec review complete
               ← EB sign-off on the business case
                  ← POC success / results review
                     ← (you are here)
```

If the backward math doesn't fit before the event, **that's the headline finding** — surface it now, while there's time to compress a step or move the date, not at quarter-end.

If there's **no** compelling event, say so plainly: the plan is then Airbyte-paced, the customer has no urgency, and slippage is likely. That's a D1 deprioritization signal worth flagging, not papering over.

## Output Format

Document structure follows `_se-playbook.md` → Output Document Format (H1 → At a Glance → Jump-to → Source Coverage → H2 body, callouts, `==key==` emphasis).

---

# Mutual Close Plan: [Company Name] × Airbyte
**Date:** [today, long form] · **SE owner:** [SE name] · **AE:** [AE name] · **EB:** [name/role]

### At a Glance
*Decision card — lead with the target and the gating risk (see `_se-playbook.md` → Decision-First Layout).*
- **Target signature date:** ==[date]== (anchored to [compelling event] on [date])
- **Steps to signature:** ==[N]== · **Critical path:** [the step most likely to gate — e.g. "InfoSec review, 3-wk queue"]
- **Timeline verdict:** [✅ fits before the event / ⚠️ tight — needs compression / 🔴 doesn't fit — event or scope must move]
- **Mutually agreed?** [✅ customer has seen + agreed / ⬜ draft — not yet shared with customer]
- **Single biggest risk to close:** [one line]
- **Source confidence:** [one line — biz-qual Paper Process + transcripts; "see Source Coverage"]

**Jump to:** [At a Glance](#at-a-glance) · [Source Coverage](#source-coverage) · [Path to Signature](#path-to-signature) · [Two-Sided Responsibilities](#two-sided-responsibilities) · [Critical Path & Risks](#critical-path--risks) · [The Mutual Agreement Ask](#the-mutual-agreement-ask)

## Source Coverage
[biz-qual Paper Process / EB read, poc-plan end date, transcripts referenced (line counts), which steps are customer-confirmed vs. [confirm].]

## Path to Signature
*Backward-planned from the target date. Every row has an owner and a date. Status tracks live.*

| # | Step | Owner (side) | Target date | Status | Notes / dependency |
|---|------|--------------|-------------|--------|--------------------|
| 1 | POC results review + success criteria signed off | Both | [date] | ⬜ | Gates everything downstream |
| 2 | EB sign-off on business case | [EB name] (customer) | [date] | ⬜ | Needs roi-business-case final |
| 3 | Security / InfoSec review | [name] (customer) + [SE] (Airbyte) | [date] | ⬜ | Often the longest queue — start early |
| 4 | Procurement / vendor onboarding | [name] (customer) | [date] | ⬜ | Vendor forms, MSA |
| 5 | Legal redlines | [name] (customer) + [AE/legal] (Airbyte) | [date] | ⬜ | Redline cycle can be 2-4 wks |
| 6 | Signature | [EB] (customer) | ==[target]== | ⬜ | |

*Adapt rows to the customer's actual Paper Process — don't invent steps they didn't mention, and don't omit ones they did.*

## Two-Sided Responsibilities
*The "mutual" in mutual action plan — make both sides' commitments explicit. This is a Sandler upfront contract applied to the close, not just the POC.*

- **Airbyte commits to:** [e.g. deliver final business case by [date], provide SOC 2 report + security questionnaire responses within [N] days of request, AE to send order form by [date]]
- **[Customer] commits to:** [e.g. schedule InfoSec review by [date], name the procurement owner, return legal redlines within [N] days]
- **Shared checkpoints:** [e.g. weekly 15-min close-plan sync until signature]

## Critical Path & Risks
- **Critical path:** the sequence of steps that determines the earliest possible signature. Name the single step most likely to gate (usually security review or procurement queue) and what compresses it.
- **Risks to close:**

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| [e.g. InfoSec review has a 3-wk queue we haven't entered] | High | Start it during the POC, not after |
| [e.g. EB hasn't seen the business case] | Med | roi-business-case → EB email this week |
| [e.g. no compelling event — timeline is Airbyte-paced] | — | Flag as deprioritization risk (D1) |

- **If the timeline doesn't fit before the compelling event:** state it as the headline. Options: compress a step (e.g. parallelize security + procurement), move the go-live, or accept a slip — but decide it now, with the customer.

## The Mutual Agreement Ask
*A close plan is only real once the customer has agreed to it. End with the concrete ask.*
> "Here's how I see the path from POC to go-live by [date]. Can we walk through it together, confirm the owners and dates on your side, and agree to a weekly 15-minute check-in until signature? If any of these dates don't work, I'd rather adjust the plan now than discover it in [target month]."

If the plan is still a draft the customer hasn't seen, mark it ⬜ **not yet mutual** in At a Glance and make "review + agree with [EB/champion]" the next step.

---

## Style

- **Owners and dates, always.** A step without a named owner and a date is not in the plan — it's a wish. Use [confirm] where unknown, never a blank.
- **Mutual, not unilateral.** Both sides commit. A plan that only lists what Airbyte will do (or only what the customer must do) isn't a MAP.
- **Back-plan from the event.** Start at the target date and work backwards; if it doesn't fit, that's the finding.
- **Security review starts during the POC, not after.** The single most common close-plan mistake is treating InfoSec as a post-POC step — its queue is often the critical path.
- **Honest about the "not mutual yet" state.** Don't present a draft as agreed. The value is in the customer saying yes to it.

## SE Best Practices Applied

Read `~/.claude/skills/_se-playbook.md`.

- **Distinct from the POC plan.** poc-plan proves the tech; this proves the *path to signature*. Per Operating Disciplines, the close plan is the missing surface between technical proof and a signed contract.
- **Paper Process (MEDDPICC P) is the backbone.** Every step traces to the customer's real legal/procurement/security process. If Paper Process is unknown, the plan is a hypothesis — flag it and offer the hypothesis-not-cold approach ("typically at your size we see InfoSec → legal → procurement; who runs each?").
- **Compelling event + mutual timeline (D2).** The anchor. No event → Airbyte-paced → slippage risk (D1).
- **Sandler upfront contract, applied to close.** The two-sided responsibilities + the agreement ask are an upfront contract for the back half of the deal.
- **Stakeholder map (D3/D4).** Owners come from the map. If a step's owner is a coach not a champion, or you're single-threaded on the whole close, that's a risk.

### Source Freshness Check (Gong Fallback)
Per `_se-playbook.md`: if the most-recent local transcript is >14 days old, check Gong for a newer call before mapping the close — procurement/security steps often surface late, once the deal gets real.

### Anti-patterns to avoid in this skill
- A plan the customer has never seen, presented as "the close plan" (it's a draft until they agree)
- Steps without owners or dates
- Treating security review as a post-POC step (start it early — it's usually the critical path)
- A one-sided plan (only customer to-dos, or only Airbyte to-dos)
- Inventing a procurement process the customer never described

---

## After Generating

### Auto-save (default)

Per `_se-playbook.md` "Output Persistence (Auto-Save)", save to:
```
{customers_dir}/<Customer>/outputs/mutual-close-plan/mutual-close-plan-<YYYY-MM-DD>-<Descriptor>.md
```
Append `-v2` etc. for same-day duplicates. User can suppress with `--no-save`.

### Source Coverage

Include a Source Coverage section reporting: biz-qual Paper Process / EB read, poc-plan end date, transcripts referenced (line counts), and which steps are customer-confirmed vs. [confirm].

### SE Identity

Read `config_file` (per playbook → Workspace Paths) for the `[SE name]` field.

### Then offer to

1. **Draft the close-plan email to the customer** wrapping the mutual-agreement ask (invoke `follow-up-email`)
2. **Add the close plan to the customer's Notion page** as a live checklist the SE updates
3. **Feed the target signature date into `internal-prep`** (forecast) — this plan's target date is the forecast date
4. **Update memory** — if the compelling event or a hard procurement constraint surfaced, propose a project memory

---

## Changelog

- **2026-07-10** — Initial creation. Backward-planned path from POC-success to signature with owners + dates on both sides; anchored to the compelling event (D2); two-sided responsibilities as a close-phase Sandler upfront contract; critical-path/risk read (security review as the usual gate); explicit "not mutual until agreed" state. Distinct from poc-plan (tech proof) — this is the path to a signed contract. Per playbook → Operating Disciplines.
