---
name: full-qual
description: Runs a complete qualification pass in one go — chains biz-qual (MEDDPICC business qualification) and tech-qual (technical fit assessment) back-to-back, producing two separate, clean documents. Use when the user says "full qual", "full qualification", "qualify everything", "run both quals", "complete qualification", or wants both business and technical qualification in a single step. This is a convenience wrapper — it does NOT merge the two docs; each remains independently runnable.
---

# Full Qualification Skill (wrapper)

You are helping a Solutions Engineer at Airbyte run a **complete qualification pass** — both business (MEDDPICC) and technical fit — in a single invocation.

This skill is a **thin orchestration wrapper**. It does not define its own output format or framework. It runs the two real qualification skills in dependency order and produces **two separate documents**, exactly as if the SE had run each on its own. Keeping them separate is deliberate: they serve different readers (AE vs SE), have different refusal rules, and stay individually scannable and re-runnable.

## Input

The user provides a customer/account name (and optionally deal context). Everything else is derived from transcripts + prior outputs, per the underlying skills.

## Hard Prerequisite: Call Data Required

**This wrapper requires at least one customer transcript.** Both underlying skills refuse without customer voice, so there's nothing to run.

Before doing anything else, check:
1. `~/airbyte-work/01-customers/_transcripts/` for files matching the customer
2. If none local, check Gong (14-day window for existing customer, 7-day for new prospect per `_se-playbook.md` Source Freshness Check)

**If zero transcripts exist: REFUSE TO RUN.** Output:
> "Cannot run full-qual for [Customer] — zero transcripts available. Both qualification skills require customer voice. Recommend: run `prep-call` to plan the first discovery call, then re-run `full-qual` after the transcript is saved."

## What it does

Run the two skills **in this order**, letting each save its own output before starting the next:

1. **`biz-qual`** — MEDDPICC business qualification. Produces the standard biz-qual document.
2. **`tech-qual`** — technical fit assessment. Produces the standard tech-qual document. (tech-qual reads the just-generated biz-qual to avoid re-deriving shared context — that's the intended flow.)

Do **not** merge, summarize, or reformat the two outputs into one doc. Each stands alone.

### Handling a one-sided call

The two skills have different data needs, and that's the point of keeping them separate:

- **biz-qual** runs on any transcript.
- **tech-qual** requires a transcript containing **technical discovery** — it refuses otherwise.

So a legitimate outcome of full-qual is **one doc, not two**:

- If the call was purely business/exec (no technical discovery), biz-qual runs and tech-qual will correctly refuse. **Do not force tech-qual to produce a hollow doc.** Report it plainly:
  > "Ran biz-qual ✓. Skipped tech-qual — this call had no technical discovery to qualify against. Run `tech-qual` after a technical call."
- If the call was purely technical with no business signal, the reverse can happen (rare — biz-qual is more permissive).

**Never fabricate qualification to fill a doc.** A missing half is a finding, not a gap to paper over.

## Output

Two independent documents (or one + a skip note, per above), each auto-saved to its own `outputs/<skill>/` folder exactly as the standalone skills do. After both run, give a one-line summary of what was produced and where:

> "Full qualification for [Customer]:
> &nbsp;&nbsp;• biz-qual ✓ → `outputs/biz-qual/…`
> &nbsp;&nbsp;• tech-qual ✓ → `outputs/tech-qual/…`
> Next up per the workflow: `connector-feasibility`, then `poc-plan`."

## Notes

- This wrapper adds no new analysis. If you find yourself writing MEDDPICC or technical-scope content directly here, stop — that belongs in the underlying skill.
- `--brief` and `--no-save` flags pass through to both underlying skills.
- The individual skills remain fully available; full-qual is purely a convenience for when you know you want both.

## Changelog

- **2026-07-07** — Initial creation. Convenience wrapper that chains biz-qual → tech-qual, producing two separate docs. Added after considering (and rejecting) a hard merge of the two skills — separation preserves distinct frameworks, refusal rules, and audiences (see `_se-playbook.md` Skill Sequencing Rules).
