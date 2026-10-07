# Connector Builder Demo Playbook
**Reusable process for building and rehearsing Airbyte Connector Builder demos for prospects**
*Created 2026-06-07 from a GoRest-based parent/child auto-discovery build. Update this file whenever the Builder UI changes.*

---

## Phase 0: Frame the demo around the prospect's question

Don't demo the Builder. Demo the answer to their question, using the Builder.

1. Write down the prospect's core technical question in one sentence (e.g. an ad-platform prospect: "can a connector auto-discover child ad accounts and pick up new ones without reconfiguration?").
2. Map that question to specific Builder primitives:

| Prospect asks about | Builder primitive to demo |
|---|---|
| Auth (API key, token, OAuth2 refresh, session) | Authenticator block (show the type dropdown) |
| Large datasets / "thousands of records" | Paginator (PageIncrement, Offset, Cursor) |
| Parent/child hierarchies, per-account fan-out | SubstreamPartitionRouter |
| New entities appearing over time | Partition router re-reads parent every sync (the "money moment") |
| Only pull what changed | Incremental Sync / DatetimeBasedCursor |
| Weird APIs (GraphQL-ish, sessions, CSV) | Custom components / YAML view |

3. Build a mapping table (their concept → stand-in API concept → Builder feature). Put it at the top of the working session doc. This table is the single most persuasive artifact in the session.

> **Don't conflate "new-record detection" with "incremental sync."** They look identical on screen ("I added rows, they showed up") but a technical buyer knows the difference:
> - **New-record detection** = the substream/parent stream is re-read every sync (a *full-refresh* read), so new parent records and their children appear automatically. Demo it by creating rows and re-syncing. No cursor needed.
> - **Incremental sync** = only records changed since a saved high-water mark flow, via a cursor + state.
>
> The Builder's Incremental Sync section offers **two cursor types** (verified live 2026-06-07): **Datetime Based Cursor** and **Incrementing Count Cursor**.
> - **Incrementing Count Cursor** keys off a monotonically increasing integer (e.g. an auto-increment `id`). It catches **new** rows only - it can't detect edits to existing rows. Works on any API with an ever-increasing id, **including GoRest** (`id`). Tests green and captures state `{"<field>": <max>}`, visible in the test panel's **State tab**; the **"Use as Input State"** button replays state into the next run.
> - **Datetime Based Cursor** keys off a modified-date field (e.g. `updated_at`/`lastmodifieddate`) and catches **edits too**, but requires (a) a date/time cursor field on the record AND (b) ideally the ability to filter/request by that field server-side. GoRest has neither, so modified-record incremental needs a different API (the June 2026 worked example used HubSpot's `lastmodifieddate`).
>
> **Important nuance for the demo:** if the API can't filter by the cursor **server-side** (GoRest can't filter by `id`), the Builder **test panel won't visibly shrink** the record list when you replay state - it shows all fetched rows. The incremental effect lives in the **saved state** and only becomes visible at the **destination** with Incremental | Append. So demonstrate incremental *in the Builder* by pointing at the **State tab high-water mark**, and show the "only new rows land" payoff at the destination. Label each moment correctly: parent-child auto-discovery (substream re-read) ≠ incremental-append (new rows) ≠ modified-record incremental (edits).

## Phase 1: Pick the stand-in API

Criteria, in priority order:
1. **Not in the Airbyte catalog** (authentic "custom connector" story).
2. **Matches the protocol shape** of the prospect's real API: same auth family, same pagination style, same hierarchy depth.
3. **Zero or trivial credential setup** (free token, public reads).
4. **Live-writable** if you need an "add new data, re-sync, it appears" moment.
5. Stable and fast (public sandbox rate limits are fine for a test panel demo).

Proven options:
- **GoRest** (`gorest.co.in/public/v2`): Bearer token, page/per_page pagination, 3-level hierarchy (users → posts → comments), free writable sandbox. Best for parent-child / auto-discovery stories.
- **HubSpot sandbox**: real OAuth2 refresh + `lastmodifieddate` incremental. Best as a reference manifest for OAuth2/incremental when the live API can't show those.
- Any prospect-specific public API that passes the criteria above.

If one API can't show every required feature, split: live-build on the simple API, and keep a validated **reference manifest** for the missing features (OAuth2, incremental). State this split explicitly in the session doc.

## Phase 2: Pre-build and rehearse (always, 1+ day before)

Build the whole connector yourself in the actual workspace before the session. The goal is to discover UI changes and workspace quirks while there is still time to adapt.

Current UI flow (verified 2026-06-07):
1. Builder → New custom connector → Start from scratch → **"Skip and start manually"** (AI Assistant screen).
2. Rename connector (top-left chip).
3. First stream: name, full **API Endpoint URL** (no global base URL anymore), then enable the **Authenticator** and pick the type. **Keep the default config reference** (e.g. `{{ config['api_key'] }}`) - the builder auto-creates the matching user input. Do NOT rename the field ID or paste a different reference; renaming mid-build leaves orphaned required spec entries (cost an hour in the 2026-06 rehearsal).
4. Inputs page → **gear icon → Secret OFF → Save changes** (see gotchas), then enter the testing value → **Done** → wait for "Draft saved". Back on the stream: Record Selector field path, Primary Key, Paginator. Test.
5. Child streams: **+ → "Use an existing stream configuration"** → copy from the parent stream, then change only the URL and add the Partition Router (field is now called **"Current Parent Key Value Identifier"**).
6. Test every stream green. Reload the page and confirm everything (including testing values) persisted.
7. Leave the draft in the workspace as the **fallback** for the live session.

### Known gotchas (check each rehearsal whether they still apply)
- **Secret testing values may fail to persist** (server 500, "Draft can't be saved", test errors with "<field> is a required property"). Workaround: set the input **Secret OFF** during build/test; flip ON at publish.
- **Brace auto-pairing** in UI fields mangles Jinja (`{{ ... }}` becomes `{{ { ... }} }}` → TemplateSyntaxError). **Paste** Jinja expressions; never type them live. Fix mistakes in the YAML view.
- **Test preview is capped** (1000 records / 5 partitions x 5 pages). Set expectations in the doc; show the Request tab to prove pagination params are injected.
- **Password-manager extensions** (1Password) hijack secret fields during screenshare. Disable for the demo.
- The **Request/Response tabs** and the **"Other Requests"** panel (shows parent-stream reads) are your best narration props for fan-out.

## Phase 3: Write the working session doc

One markdown file per session, saved to `{customers_dir}/<Customer>/outputs/connector-builder-demo/` next to the exported manifests (paths per `_se-playbook.md` → Workspace Paths). Required sections:

1. **Context**: prospect's question, why this stand-in API, the mapping table.
2. **UI Changes and Gotchas**: everything Phase 2 surfaced, numbered, with workarounds.
3. **Feature coverage table**: each feature they asked for → where it's shown → live vs reference.
4. **Agenda with timings.**
5. **Pre-session checklist**: workspace, token (marked internal, regenerate after), fallback draft, paste-ready Jinja snippets, extension hygiene, optional destination.
6. **Step-by-step build** with exact current UI labels, paste-ready values, and the verified test results for each stream.
7. **The "money moment" script**: exact clicks for the live wow (e.g. create new account → re-test → new partition appears). Rehearse it end to end.
8. **Mapping back to their real stack** + the one-sentence answer to their question.
9. **Live test evidence table** with the rehearsal date.
10. **Next steps** table with owners.

Style rules: paste-ready code spans for anything typed into the UI; neutral demo data names ("Demo Account 001") on shared/public sandboxes; never the prospect's name in public sandboxes; mark internal tokens clearly and regenerate them after the session.

## Phase 4: Day-of execution

- Open side by side: the session doc (for pasting), the Builder (fresh build), and the fallback draft in a second tab.
- Build live following the doc. If anything breaks, switch to the fallback draft and keep narrating; debug after the call.
- Narrate with the test panel: Records grid (auto-detected schema), Request tab (injected params, auth header), Partition selector (fan-out), Other Requests (parent reads).
- End on the mapping table and the one-sentence answer.

## Phase 5: Post-session

- Regenerate any demo tokens used.
- Delete demo records created in public sandboxes.
- Update this playbook with any new UI changes or gotchas found.
- Log next steps (POV doc, real-API rebuild) in the prospect folder and Salesforce.

---

## Asset inventory pattern

```
{customers_dir}/<Customer>/outputs/connector-builder-demo/
  <api>-demo-manifest.yaml          # exported/reference manifest of the live build
  <other>-reference-manifest.yaml   # features the live API can't show (OAuth2, incremental)
  Working-Session-Walkthrough-<date>.md
```

Workspace: keep the tested connector draft named for the prospect (e.g. "GoRest Demo") as the live fallback.
