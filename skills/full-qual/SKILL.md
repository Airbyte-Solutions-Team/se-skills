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

Before doing anything else, run the pre-flight source check per `_se-playbook.md` → Shared Skill Boilerplate → Pre-flight source check (qualification and synthesis skills).

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

### Partial-failure / atomic completion

A wrapper that chains two skills can end in a **partial completion** even when the transcript is mixed. Handle it as an honest pass-through, not a silent success:

1. **Run the first child to completion before starting the second.** Do not start `tech-qual` until `biz-qual` has produced a saved output. If `biz-qual` refuses, stop the chain and explain why — do not proceed to `tech-qual` with a missing business context.
2. **If a child refuses or exits with an error, do not leave an incomplete/stale document for that child.** If `tech-qual` refuses, the `tech-qual` output folder must not contain a partial or empty file. The wrapper's own summary is the record for the skipped child.
3. **State exactly which child produced output and which did not.** The closing summary must list each child with a status (`✓ produced`, `✗ refused`, or `✗ failed`) and a one-line reason. Do not use an aggregate "full qualification complete" message unless both children actually completed.
4. **Surface the reason from the child, not from memory.** If `tech-qual` refused because the transcript lacked technical discovery, say that — do not invent a different reason. If `biz-qual` refused because the transcript was empty or too thin, say that.

**Plain examples:**
- "Ran biz-qual ✓ → `outputs/biz-qual/…`. Skipped tech-qual — transcript has no technical discovery."
- "Skipped biz-qual — no usable transcript. Did not attempt tech-qual."

**Never fabricate qualification to fill a doc.** A missing half is a finding, not a gap to paper over.

## Output

Two independent documents (or one + a skip note, per above), each auto-saved to its own `outputs/<skill>/` folder exactly as the standalone skills do. After both run, give a closing summary of what was produced and where — with explicit status for each child:

> "Full qualification for [Customer]:
> &nbsp;&nbsp;• biz-qual ✓ produced → `outputs/biz-qual/…`
> &nbsp;&nbsp;• tech-qual ✗ refused — transcript has no technical discovery. Run `tech-qual` after a technical call.
> Next up per the workflow: `connector-feasibility`, then `poc-plan`."

Use `✓ produced`, `✗ refused`, or `✗ failed` so a reader can see at a glance which child completed. Only use an aggregate "full qualification complete" message if **both** children produced a document.

In that closing summary, also surface (don't re-derive) the child docs' product-truth so the SE sees it without opening both: the **3-way deployment verdict** (🟢 Cloud / 🟦 Flex / 🔴 park-no-fit) and tech-qual's **entitlement-backed compliance self-check** — including any unverified-compliance flag it raised. Just point at what the children already produced; add no new format or logic.

## Notes

- This wrapper adds no new analysis. If you find yourself writing MEDDPICC or technical-scope content directly here, stop — that belongs in the underlying skill.
- **Owns no logic; inherits child contracts as-is.** full-qual chains biz-qual then tech-qual at their *current* contracts — if either child's prerequisites or refusal rules change, this sequence inherits them automatically. Do NOT re-implement or override a child's refusal/format here; the wrapper's only job is ordering + pass-through.
- `--brief` and `--no-save` flags pass through to both underlying skills.
- The individual skills remain fully available; full-qual is purely a convenience for when you know you want both.

## Changelog

- **2026-07-14** — Added explicit partial-failure / atomic-completion handling: run `biz-qual` before `tech-qual`, never leave a partial/stale doc for a refused child, and list each child with `✓ produced`, `✗ refused`, or `✗ failed` plus a one-line reason in the closing summary. No new analysis or logic; still a pure pass-through wrapper.
- **2026-07-10** — Closing summary now surfaces the children's derived product-truth — the 3-way deployment verdict and tech-qual's entitlement-backed compliance self-check / any unverified-compliance flag — so the SE sees them without opening both docs. Still owns no logic and re-derives nothing; pure pass-through of what the children produced.
- **2026-07-10** — Repointed hardcoded `~/airbyte-work/` paths to the workspace-path resolver (`{customers_dir}`/`{transcripts_dir}`/`{notes_dir}`/`config_file`/`memory_dir`) per playbook → Workspace Paths. Portable across SE machines.
- **2026-07-09** — Clarified that the wrapper owns no logic and inherits its child skills' prerequisites/refusals at their current contracts — don't re-implement or override them here; ordering + flag pass-through only.
- **2026-07-07** — Initial creation. Convenience wrapper that chains biz-qual → tech-qual, producing two separate docs. Added after considering (and rejecting) a hard merge of the two skills — separation preserves distinct frameworks, refusal rules, and audiences (see `_se-playbook.md` Skill Sequencing Rules).
