---
name: testing-webapp
description: How to run and manually test the se-skills local webapp (Solutions Team Hub) end-to-end without real customer data, Claude, or Gong. Use when validating webapp UI, output rendering/validation, Output Gallery behavior, or skill planner behavior.
---

# Testing the se-skills webapp locally

Use synthetic data only. Do not use real customer information for test fixtures or screenshots.

## Start and stop

```bash
cd webapp && uv run app.py     # http://127.0.0.1:8787

# Always clean up afterwards; a leftover process blocks the user's own run.
pkill -f app.py
lsof -ti:8787 | xargs -r kill -9
```

If `pkill` does not free the port, kill the PID from `lsof -ti:8787` directly.

## Synthetic workspace

The workspace root is resolved in `webapp/config.py` and defaults to `~/.se-skills/customers` unless overridden. Create account data under:

```text
~/.se-skills/customers/
  <Account-Slug>/opportunities/<opp_slug>/outputs/<skill>/<file>.md
  _transcripts/<Account-Slug>-MM.DD.YY.(txt|rtf|md|vtt)
```

Prefer committed synthetic fixtures under `eval/fixtures/outputs/` and `eval/fixtures/gallery/`.

To create an intentionally invalid output, start from a canonical fixture and introduce one targeted defect, for example:

- unresolved placeholders such as `[Owner]` or `[Customer Name]`
- partial transcript coverage such as `400 / 612 lines`
- a missing required section

Placeholder detection is Markdown-aware. Links like `[text](#anchor)` and evidence tags such as `[stated — Name]` / `[inferred — src]` must not be reported as placeholders.

## Useful direct URLs

- Planner: `/api/plan?account=<Account>&skill=<skill>&opp_slug=<opp>`
- Output deep link: `/#/output/<Account>/<opp_slug>/<Opp Name>/<double-url-encoded relative path>`
- Output Gallery: `/gallery.html`

Without Salesforce sync, extra on-disk opportunities may not appear in account lists. Deep-link them with:

```text
http://127.0.0.1:8787/#/opp/<Account>/<opp_slug>/<Display Name>
```

## Validation metadata consistency

Verify the same status badge/details through all three paths:

1. open from the opportunity page
2. go Back, then reopen
3. reload the output deep link

The reload path historically diverged. If browser behavior looks stale, verify the `app.js?v=` cache-bust in `static/index.html` and hard reload.

`window.docStatus(meta)` drives both the status bar and output-list badge:

- `ok` → Ready
- `info` → Checks unavailable
- `warn` → Review sources
- `error` → Output incomplete

Two independent inputs matter:

- `validation_supported` comes from `output_schema.skill_has_schema(skill)`.
- `reference_sources_tracked` comes from `reference_freshness.get_relevant_sources(skill)`.

Schema-backed skills are `biz-qual`, `tech-qual`, `deployment-model-qual`, `poc-plan`, `connector-feasibility`, `pov-gsheet`, and `post-call`. `post-call` is strict.

Reference-consuming skills are `tech-qual`, `deployment-model-qual`, `connector-feasibility`, `poc-plan`, and `objection-handler`.

Useful synthetic states:

- red invalid: post-call missing required sections or complete `N / N lines` coverage
- neutral Checks unavailable: a non-strict schema-backed, non-reference skill using legacy headings only
- amber Review sources: legacy reference-consuming skill without a generation snapshot
- green Ready for a reference-consuming skill: write a sidecar with an explicit generation-time reference snapshot

`read_or_parse_sidecar` must never invent a generation snapshot.

The invalid-state details show only the first three validation errors. Keep hostile-looking text within those first errors when testing tooltip escaping; it must render literally with no alert or DOM breakage.

Quick parser check:

```bash
cd webapp
uv run python -c "import output_schema as o; print(o.parse_output('<skill>', open(p).read()).validation_status)"
```

## Planner behavior

Do not confuse warnings, choices, and hard blockers.

### Advisory warnings

`plan.warnings` are source-resolvable conditions such as a missing local transcript for post-call, biz-qual, or tech-qual. They render as amber warning toasts and must not produce a native confirm dialog.

To exercise a warning through the command bar, clear the input, type the exact skill name, and click its dropdown suggestion. If the text does not match a suggestion, Run treats it as freeform and skips the planner.

A run may then fail because the `claude` CLI is unavailable in the test environment; that does not invalidate the pre-invoke warning test.

### Structured choices

`plan.choices` covers missing upstream qualification documents for `poc-plan`, `roi-business-case`, and `mutual-close-plan`.

The frontend confirms the choice, then re-invokes with `acknowledged_choices: [<choice ids>]`. `POST /api/invoke` remains blocked until those IDs are echoed back. A job starting proves the acknowledgement round-trip worked.

Inspect the second `POST /api/invoke` payload in browser DevTools if necessary. Do not look for `acknowledged_choices` in `.runs/<skill>.json`; they are kept in job metadata and the generated prompt, not persisted there.

### Hard blockers

Only `ready:false` plus `missing` should trigger the older Run anyway? confirmation flow.

## Output Gallery

`http://127.0.0.1:8787/gallery.html` renders committed synthetic fixtures from `eval/fixtures/gallery/*.md` through the production reader path:

```text
POST /api/output/render → static/reader.js
```

Fixture tabs come from `GET /api/gallery/fixtures` and use allowlisted names only.

Use the gallery for presentation and reader validation without creating a customer workspace.

### Gallery review procedure

Check all of the following:

- Desktop, Tablet (780px), and Narrow (420px)
- dark and light themes
- representative short and long fixtures
- connector records
- wide generic tables
- Source Coverage ordering
- browser console and `#gallery-error`

The width selector may require keyboard interaction under xdotool: click it, use Up/Down, then Return.

The theme toggle uses `document.documentElement[data-theme]` and persists `localStorage["se-hub-theme"]`, the same contract as the main app. Light mode has no reader-specific override layer, so explicitly check washed-out surfaces, borders, code spans, chips, tables, and system records.

Assert containment instead of relying only on visual inspection:

```js
document.documentElement.scrollWidth === document.documentElement.clientWidth
```

For wide tables, `.md-table-wrap` should have `scrollWidth > clientWidth` while the page itself remains at `scrollLeft === 0`.

A 2–4px excess on a collapsible `.md-h2` can be padding rounding rather than visible clipping; verify visually before treating it as a defect.

Connector-shaped tables become `.sys-record` entries. Unrelated tables remain tables inside `.md-table-wrap`.

Clicking an H2 collapses its section. If content appears to disappear, click the heading again before diagnosing a rendering bug.

`#gallery-error` should remain hidden throughout the pass.

## Manual visual acceptance checklist

At minimum inspect:

- light and dark mode
- normal laptop width
- 780px tablet width
- 420px narrow width
- Connector Feasibility with enough systems to stress the layout
- long Post Call
- Deal Assessment
- POC Plan
- ROI / Mutual Close

Look for:

- page-level horizontal overflow
- clipped content
- weak heading hierarchy
- decorative semantic colors
- dense stakeholder formatting
- unreadable tables
- Source Coverage visually competing with the main narrative
- sidebar/document-order mismatch
- browser errors

## Not testable in a no-secrets local environment

Real generation requires the Claude CLI and Gong MCP. Do not substitute mocked browser behavior as proof of real model/source behavior. Validate planner, renderer, validation metadata, gallery, and committed synthetic fixtures locally; report real Claude/Gong acceptance as deferred when unavailable.

## Secrets needed

None for local UI, planner, validation, or Output Gallery testing.

## Changelog

- 2026-08-22: Initial testing guide, including Output Gallery, planner semantics, validation-state checks, responsive/theme acceptance, and known testing traps.
