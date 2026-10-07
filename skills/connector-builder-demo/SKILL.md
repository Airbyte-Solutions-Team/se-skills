---
name: connector-builder-demo
description: >
  Plan and scaffold a live Airbyte Connector Builder demo or workshop for a prospect: frame it around
  their hardest integration question, pick a public stand-in API that mirrors their real API's shape,
  map the question to Builder primitives (authenticator, paginator, SubstreamPartitionRouter,
  incremental cursors), and generate a filled-in working session doc plus an HTML screenshare
  walkthrough. Use when the user says "connector builder demo for X", "prep a connector workshop",
  "build a custom connector demo for [account]", "connector builder session prep", or asks which
  stand-in API mirrors a prospect's API or how to show a Builder feature live.
---

> [!info] `connector-builder-demo` output architecture
> This is a specialized artifact — it does not follow the normal report H2/H3 architecture.
> It produces a working session doc with its own section order (ending in Source Coverage) and a
> self-contained HTML screenshare walkthrough.


# Connector Builder Demo

Prepare a live Airbyte **Connector Builder** demo for a prospect: figure out what they actually need
to see, pick a public stand-in API that mirrors their real API's shape, then generate a filled-in
working session doc and an HTML screenshare walkthrough the SE runs from.

The point is never "here's the Builder." It's "here's the answer to your hardest integration question,
proven live in the Builder." Everything below serves that.

Before generating anything, read `~/.claude/skills/_se-playbook.md` → **Workspace Paths**,
**Source Coverage Transparency** and **Salesforce Enrichment**. For Builder mechanics and the full
process, read the bundled `references/playbook.md` and `references/ui-gotchas.md`.

## Input

- **Account + opportunity** (the webapp passes both; if run manually and none given, ask).
- Optional: the prospect's question in their words, the API they need to connect, and the session date.

## Workflow

### 1. Get the prospect's real question and API shape

Don't guess. Gather context first, in this order:

1. **Prior skill outputs** in `{customers_dir}/<Customer>/outputs/` and the opportunity's
   `opportunities/<slug>/outputs/`: `connector-feasibility` (is it really a custom-connector case?),
   `tech-qual`, `post-call`, `prep-call`.
2. **Transcripts**: `{transcripts_dir}/<Customer>-*`, most recent in full. Apply the playbook's
   Source Freshness rule; if the newest local transcript is stale, check Gong.
3. **Salesforce** (optional, per playbook): `Most_important_sources__c`,
   `Required_features_functionality__c`, `Use_case_description__c`.
4. **Raw notes** in `{customers_dir}/<Customer>/raw/` (API docs, sample payloads).

Extract the API's auth family, pagination style, hierarchy depth (parent/child entities), incremental
needs (new records vs. edits) and data volume. Distill it to **one sentence**, for example: "Can a
connector auto-discover child ad accounts and pick up new ones with zero reconfiguration?" That sentence
drives the whole session.

If the API specifics genuinely can't be found, ask the SE a few targeted questions (auth type? parent/
child entities? incremental or just new records?) rather than inventing details.

If `connector-feasibility` says a certified connector already covers the source, say so first: the
demo may not be needed, or it should be framed as "extend/customize" rather than "build".

### 2. Map the question to Builder primitives

Use the mapping table in `references/playbook.md` (Phase 0). In short:

| Prospect asks about | Builder primitive |
|---|---|
| Auth (API key, token, OAuth2 refresh, session) | Authenticator block |
| Large datasets / "thousands of records" | Paginator (PageIncrement / Offset / Cursor) |
| Parent/child hierarchies, per-account fan-out | SubstreamPartitionRouter |
| New entities appearing over time | Partition router re-reads the parent every sync (the money moment) |
| Only pull what changed | Incremental Sync / DatetimeBasedCursor |
| Weird APIs (GraphQL-ish, sessions, CSV) | Custom components / YAML view |

Be precise about a distinction technical buyers care about: **new-record detection** (substream
re-read, full refresh, no cursor) ≠ **incremental-append** (new rows via an incrementing-count cursor)
≠ **modified-record incremental** (edits, via a datetime cursor + server-side filter). The playbook
explains when each applies and which APIs can show which. Label every demo moment correctly.

### 3. Pick the stand-in API

Criteria, in priority order (playbook Phase 1): not in the Airbyte catalog; matches the prospect's
protocol shape (auth + pagination + hierarchy); zero or trivial credentials; live-writable if you need an
"add data → re-sync → it appears" moment; stable and fast.

Proven options:
- **GoRest** (`gorest.co.in/public/v2`): Bearer token, page/per_page pagination, 3-level hierarchy
  (users → posts → comments), free writable sandbox. Best for parent-child / auto-discovery.
- **HubSpot sandbox**: real OAuth2 refresh + `lastmodifieddate` incremental. Best as a *reference
  manifest* when the live API can't show OAuth2 or modified-record incremental.

If one API can't show every required feature, split: live-build on the simple one, keep a validated
reference manifest for the rest, and state the split explicitly in the doc.

### 4. Generate the deliverables

Save to `{out_dir}/connector-builder-demo/` when the invocation provides `{out_dir}` (the webapp does);
otherwise to `{customers_dir}/<Customer>/outputs/connector-builder-demo/` (or the opportunity's
`opportunities/<slug>/outputs/connector-builder-demo/`). Create the folder if needed.

1. **Working session doc**: `connector-builder-demo-YYYY-MM-DD-<api>-session.md`. Copy
   `assets/working-session-template.md` and fill in: the one-sentence question, the mapping table, the
   feature-coverage table (live vs. reference), the agenda with timings, the stand-in API quick
   reference, the pre-session checklist, the step-by-step build with **paste-ready** values and the
   verified test result per stream, the **money-moment script**, the mapping back to their real stack,
   next steps, and **Source Coverage** last.
2. **HTML screenshare walkthrough**: `connector-builder-demo-YYYY-MM-DD-<api>-walkthrough.html`. Copy
   `assets/walkthrough-template.html` and replace every `{{PLACEHOLDER}}`. This is the page the SE keeps on
   a second monitor and pastes from. Keep it self-contained (inline CSS, no external assets).

Use `-v2`, `-v3` for same-day reruns. Pass `--no-save` to suppress saving.

Style rules that matter: paste-ready code spans for anything typed into the UI; **neutral demo data
names** ("Demo Account 001") on shared/public sandboxes, never the prospect's name; mark internal tokens
clearly and note they must be regenerated after the session.

### 5. Flag the rehearsal (don't skip it)

Tell the SE, every time: build the whole connector yourself in the Solutions Engineering workspace at
least a day before, leave a tested draft named for the demo as the live fallback, and re-verify the
gotcha list against the current Builder UI. Every gotcha in `references/ui-gotchas.md` was found in a
rehearsal. The Builder UI changes often; the bundled list was last fully verified on 2026-06-07.

## Gotchas

Read `references/ui-gotchas.md` and surface the relevant ones in the generated doc. The three that burn
people most:
- **Secret toggle OFF** on the Inputs page before entering testing values (otherwise a silent 500 or
  "required property" on every test).
- **Paste Jinja, never type it** (brace auto-pairing → TemplateSyntaxError; fix it in the YAML view).
- **Test preview is capped**: prove pagination via the Request tab; don't promise record totals.

## Source Coverage

End the working session doc with a **Source Coverage** table (per playbook): each source checked
(prior outputs, transcripts/Gong, Salesforce, raw notes, stand-in API docs, rehearsal), whether it was
available, and what it contributed. Never claim a source was checked if it wasn't.

## Scope boundaries

Do not:
- Use the prospect's name, data or credentials in a public sandbox
- Promise record counts, throughput or features the rehearsal didn't verify
- Present a reference manifest as something built live
- Replace `connector-feasibility` (coverage) or `poc-plan` (POC scope); link to them instead

## Maintenance

After each real session, fold any new UI change or gotcha back into `references/ui-gotchas.md` and
`references/playbook.md` in this repo (with the verification date) so the next SE's demo starts from it.

## Bundled files

- `references/playbook.md`: the full 5-phase process (frame → pick API → rehearse → execute → clean up).
- `references/ui-gotchas.md`: verified gotcha list with symptoms and fixes.
- `assets/working-session-template.md`: blank session doc to copy and fill.
- `assets/walkthrough-template.html`: blank HTML screenshare walkthrough to copy and fill.

## Changelog

- 2026-10-07 — Added to the suite (contributed by Ryan Waskewich). Adapted from a personal skill:
  workspace paths and outputs follow `_se-playbook.md`, context comes from prior skill outputs,
  transcripts and Salesforce, the session doc ends in Source Coverage, and customer-specific worked
  examples were removed.
