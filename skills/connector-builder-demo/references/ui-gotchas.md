# Connector Builder - UI Gotchas (verify each rehearsal)

The single most reusable artifact in this project. Every one of these cost real time to
discover during a live build or rehearsal. Re-check each one during your dry run, prune what's
been fixed, and add anything new. **Last full UI verification: 2026-06-07 (Cloud Builder, manifest 7.17.1).**

| # | Gotcha | Symptom | Fix / workaround |
|---|---|---|---|
| 1 | **Secret testing values don't persist** | Silent server 500, "Draft can't be saved", every test errors `"'<field>' is a required property"` | On the Inputs page, **gear → Secret OFF → Save changes** *before* entering the testing value. Flip Secret back ON only at publish. |
| 2 | **Brace auto-pairing mangles Jinja** | Typing `{{ ... }}` becomes `{{ { ... }} }}` → `TemplateSyntaxError` | **Paste** Jinja, never type it live. Fix mistakes in the YAML view, not the UI field. |
| 3 | **No global base URL anymore** | Looking for a "base URL" field that no longer exists | Put the **full API Endpoint URL** on each stream. |
| 4 | **Auto-created auth input** | Renaming the config reference leaves orphaned required spec entries (cost ~1hr in 2026-06 rehearsal) | When you enable the Authenticator, **keep the default** `{{ config['api_key'] }}` reference. The builder auto-creates the matching user input - don't rename it. |
| 5 | **AI Assistant start screen** | New connector flow drops you on an AI screen | New custom connector → Start from scratch → **"Skip and start manually."** |
| 6 | **Copy-stream toggle for children** | Rebuilding auth/paginator on every child stream | New child stream → **"Use an existing stream configuration"** → copy from parent, then change only the URL + add the Partition Router. |
| 7 | **Partition router field renamed** | Looking for old "parent key value" label | It's now **"Current Parent Key Value Identifier."** |
| 8 | **Test preview is capped** | Can't show "thousands of records" totals (cap ~1000 records / 5 partitions × 5 pages; grid shows 50 for a 50/page stream) | Don't promise totals. Prove pagination via the **Request tab** (`?per_page=50`) and fan-out via the **Other Requests** panel. |
| 9 | **Password-manager hijack** | 1Password overlay grabs the secret field mid-screenshare | Disable the extension for the demo. |
| 10 | **Testing values live on the Inputs page** | Looking for them on the stream | Enter testing values on the **Inputs** page, then wait for "Draft saved." Reload once to confirm they persisted. |

## Best narration props (use these to tell the story)
- **Records grid** - auto-detected schema, proves the connector understood the API.
- **Request tab** - injected pagination params and the auth header.
- **Partition selector** - the fan-out across parent records.
- **Other Requests panel** - shows the parent-stream reads that drive auto-discovery (the "money moment").

## If automating with browser tools (Cowork)
The Builder's Jinja fields mangle typed braces. Set values via the **YAML view** (Monaco editor:
`window.monaco.editor.getModels()[0]` get/setValue) rather than typing into UI fields. Secret-type
inputs also fight password-manager extensions.
