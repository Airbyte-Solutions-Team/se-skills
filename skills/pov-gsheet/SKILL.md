---
name: pov-gsheet
description: >
  Create and pre-fill Airbyte POV (Proof of Value) Success Criteria documents in Google Sheets
  for prospects. Use this skill whenever the user asks to create a POV document, success criteria sheet,
  POV tracker, or anything related to preparing a POV for a prospect. Also triggers when asked to
  "fill out the POV template", "set up POV tracking for [prospect]", "create success criteria for
  [account]", "prep the POV sheet", or any variation involving POV + prospect/account name.
  This skill uses a Google Sheets template (not XLSX) with dropdown fields and pulls data from the
  existing SE workspace — prior skill outputs, workspace transcripts, and optional external evidence
  (Salesforce, Gong, meeting notes, Gmail, Slack) when those MCP integrations are configured.
---

> [!info] Canonical output architecture for `pov-gsheet`
> This skill follows the shared content-architecture contract. Produce the H2 sections below in this exact order; use H3 (`###`) for the listed sub-topics.
>
> **Canonical H2 order:**
> 1. `Receipt`
> 2. `Source Coverage`
>
> Source Coverage must be the **final H2**. The `At a Glance` block is an H3 under the title block (not a navigable section).
>


# POV Google Sheets Creator

Creates pre-filled Airbyte POV Success Criteria Google Sheets for prospects. It gathers context
deterministically from the workspace, builds a structured POV context, then uses Chrome/
computer-use (clipboard + Name Box paste) to copy the configured template, fill the seven tabs,
and move the new sheet into the correct prospect folder in Google Drive.

Before generating anything, read `~/.claude/skills/_se-playbook.md` → **Output Document Format** and
**Source Coverage Transparency**.

## Preflight checks (fail fast)

Stop immediately and report a clear, actionable error if any required dependency or configuration is
missing. Do not open Chrome or copy a template until these checks pass.

1. **Locate the SE config file.** The webapp runs `claude -p` from the workspace root, so start by
   reading `.se-config.yaml` in the current directory. If it is missing, fall back to
   `$SE_WORKSPACE/.se-config.yaml`, then `~/.se-skills/.se-config.yaml`, then `~/airbyte-work/.se-config.yaml`.
2. **Read the `pov_gsheet` block.** It must contain:
   - `template_url` — the Google Sheets template to copy
   - `drive_target_folder_url` — the Drive "Customer" folder where prospect subfolders are created
   - `se_name` — the SE name shown on the Contacts sheet
   - `se_title` — the SE title shown on the Contacts sheet

   If the block or any required key is missing, stop and say:
   > `pov-gsheet` is missing required configuration. Copy `config/se-config.example.yaml` to your workspace root as `.se-config.yaml`, uncomment the `pov_gsheet:` block, and set `template_url`, `drive_target_folder_url`, `se_name`, and `se_title`.
3. **Confirm the account identifier is available.** The webapp passes the selected account and
   opportunity. If invoked outside the webapp, ask for them or infer from the prompt context. The
   account name must match the workspace customer folder name.
4. **Confirm context-gathering is possible.** Run the deterministic context loader:
   ```bash
   REPO_DIR=$(cd "$(dirname "$(readlink -f ~/.claude/skills/pov-gsheet/SKILL.md)")/.." && pwd)
   CTX_FILE="${OUT_DIR:-/tmp}/pov-gsheet-context.json"
   python "$REPO_DIR/webapp/pov_gsheet_context.py" \
     --account "$ACCOUNT" \
     --opportunity "$OPPORTUNITY" \
     --workspace "$(pwd)" \
     --out "$CTX_FILE" --pretty
   ```
   If the loader cannot be found, stop and say:
   > `pov-gsheet` cannot locate `webapp/pov_gsheet_context.py`. Run `install.sh` to symlink the skills repository into `~/.claude/skills/`.
5. **Confirm the run is not `blocked`.** Read `CTX_FILE` and look at `status` (`complete`, `partial`, or
   `blocked`) and `warnings`. If `blocked`, do not create a sheet. Write the local receipt and explain
   what is missing and which upstream skill to run.
6. **Confirm Chrome / computer-use access is available.** The Google Sheets portion of the skill needs
   Chrome running with remote debugging on `localhost:29229` (the webapp default) and either the
   `computer-use` MCP with `clipboardWrite: true` or the optional Node/Playwright runner
   `webapp/scripts/pov-gsheet-runner.mjs` with Playwright installed. If the first browser action fails
   with a missing-tool or permission error, stop and say:
   > `pov-gsheet` needs a signed-in Google account in Chrome, the computer-use MCP with `clipboardWrite`, or a working Playwright + Chrome CDP setup. See `config/se-config.example.yaml` for setup notes.

## Configuration used by this skill

From `.se-config.yaml` (`pov_gsheet` block), assign these values for the rest of the run:

- `template_url` → `TEMPLATE_URL`
- `drive_target_folder_url` → `DRIVE_TARGET_FOLDER_URL`
- `se_name` → `SE_NAME`
- `se_title` → `SE_TITLE`

Use these variables everywhere the original steps referenced the shared template, shared folder, or
SE contact details. Do not use the placeholder URLs or example names below.

## Template

**Source template:** `TEMPLATE_URL`

This template is expected to contain exactly these 7 sheets:

1. **Contacts** — Airbyte team + prospect contacts
2. **POV Milestones** — Timeline tracking (Demo, Discovery, Success Criteria, Environment Setup, POV Execution, Wrap-up)
3. **Business Objectives** — Strategic goals the prospect wants to achieve
4. **POV - Success Criteria** — The core sheet: connectors, use cases, validation criteria, priorities
5. **In-scope Apps** — Source/destination systems with sync methods
6. **Feature Requests** — Product asks surfaced during discovery
7. **Architecture Diagrams** — Placeholder for diagrams

If the copied template is missing any of these tabs or the tabs are renamed, stop before pasting and
report which tabs are missing or unexpected.

## Workflow

### Step 0: Build the structured POV context

#### 0a. Gather external evidence automatically

The skill calls the configured MCP tools, writes the raw tool responses to a job-scoped temporary
directory, and uses `webapp/pov_gsheet_bridge.py` to normalize them into `ExternalEvidence` JSON.
The SE does **not** manually query MCPs, prepare evidence JSON files, or pass `--*-evidence` flags
unless they are deliberately overriding a source.

Set a temporary job directory for the run:

```bash
JOB_DIR="${OUT_DIR:-/tmp/pov-gsheet-$(date +%s)}"
mkdir -p "$JOB_DIR"
```

**Salesforce** — use `mcp__salesforce__run_soql_query` (read-only SOQL) and scope to the account/
opportunity.

1. Run the SOQL queries below (or combine the returned record arrays into one raw JSON list):
   * Account: `SELECT Id, Name, Type FROM Account WHERE Name LIKE '%<Account>%' LIMIT 1`
   * Opportunities: `SELECT Id, Name, StageName, Amount, CloseDate, Type, Owner.Name, SE_Name__c, Next_Step__c, Account.Name, Account.Id, Required_features_functionality__c, Most_important_sources__c, Most_Important_Destinations__c, Use_case_description__c, Airbyte_Use_Case__c FROM Opportunity WHERE Account.Name LIKE '%<Account>%' ORDER BY CloseDate DESC`
   * Contacts: `SELECT Id, FirstName, LastName, Name, Email, Title, Account.Name FROM Contact WHERE Account.Name LIKE '%<Account>%'`
2. Save the raw response(s) as `$JOB_DIR/pov-gsheet-salesforce-raw.json`.
3. Normalize:
   ```bash
   uv run --quiet --python 3.11 --script "$REPO_DIR/webapp/pov_gsheet_bridge.py" \
     --source salesforce --account "$ACCOUNT" --opportunity "$OPPORTUNITY" \
     --raw-input "$JOB_DIR/pov-gsheet-salesforce-raw.json" \
     --out "$JOB_DIR/pov-gsheet-salesforce-evidence.json"
   ```
   If the Salesforce MCP is unavailable or returns an error, instead run:
   ```bash
   uv run --quiet --python 3.11 --script "$REPO_DIR/webapp/pov_gsheet_bridge.py" \
     --source salesforce --account "$ACCOUNT" --opportunity "$OPPORTUNITY" \
     --status unavailable --note "Salesforce MCP not configured or failed" \
     --out "$JOB_DIR/pov-gsheet-salesforce-evidence.json"
   ```

**Gong** — use `mcp__gong__search_calls` scoped by customer domain/participant email and a bounded
14-day lookback, then fetch `gong://calls/{callId}/transcript` for each relevant call.

1. Call `mcp__gong__search_calls` with `from_date_time` = 14 days ago and `to_date_time` = now.
2. For each returned call, fetch `gong://calls/{callId}/transcript`.
3. Save `search_calls` output as `$JOB_DIR/pov-gsheet-gong-search.json` and a `{callId: transcript, ...}` object as `$JOB_DIR/pov-gsheet-gong-transcripts.json`.
4. Normalize:
   ```bash
   uv run --quiet --python 3.11 --script "$REPO_DIR/webapp/pov_gsheet_bridge.py" \
     --source gong --account "$ACCOUNT" --opportunity "$OPPORTUNITY" \
     --raw-input "$JOB_DIR/pov-gsheet-gong-search.json" \
     --transcripts "$JOB_DIR/pov-gsheet-gong-transcripts.json" \
     --out "$JOB_DIR/pov-gsheet-gong-evidence.json"
   ```
   If the Gong MCP is unavailable, use `--status unavailable --note "Gong MCP not configured or failed"`.

**Granola / Gmail / Slack** — check whether a usable MCP is configured. If one is available, retrieve
scoped data and normalize it through the bridge. If not, write an unavailable marker:
```bash
uv run --quiet --python 3.11 --script "$REPO_DIR/webapp/pov_gsheet_bridge.py" \
  --source <granola|gmail|slack> --account "$ACCOUNT" --opportunity "$OPPORTUNITY" \
  --status unavailable --note "<Source> not configured in this environment" \
  --out "$JOB_DIR/pov-gsheet-<source>-evidence.json"
```

Temporary evidence rules:
- Keep all raw and normalized files inside `$JOB_DIR`.
- Do not include credentials, OAuth tokens, or full email bodies.
- Preserve provenance: `source`, `source_id`, `retrieved_at`, `account_name`, `opportunity_name`, and `direct_customer`.
- Do not mark a source as `searched` unless the tool was actually invoked.

#### 0b. Run the deterministic context loader

The loader merges workspace sources and the normalized JSON evidence files into a single
`PovContext`. It is deterministic and produces one JSON file the rest of the run consumes.

```bash
REPO_DIR=$(cd "$(dirname "$(readlink -f ~/.claude/skills/pov-gsheet/SKILL.md)")/.." && pwd)
JOB_DIR="${JOB_DIR:-/tmp/pov-gsheet-$(date +%s)}"
CTX_FILE="$JOB_DIR/pov-gsheet-context.json"

uv run --quiet --python 3.11 --script "$REPO_DIR/webapp/pov_gsheet_context.py" \
  --account "$ACCOUNT" \
  --opportunity "$OPPORTUNITY" \
  --workspace "$(pwd)" \
  --salesforce-evidence "$JOB_DIR/pov-gsheet-salesforce-evidence.json" \
  --gong-evidence "$JOB_DIR/pov-gsheet-gong-evidence.json" \
  --granola-evidence "$JOB_DIR/pov-gsheet-granola-evidence.json" \
  --gmail-evidence "$JOB_DIR/pov-gsheet-gmail-evidence.json" \
  --slack-evidence "$JOB_DIR/pov-gsheet-slack-evidence.json" \
  --out "$CTX_FILE" --pretty
```

If `uv` is not on PATH, install it first — the bridge and loader declare their dependencies via PEP 723 inline metadata, which is only honored when run as `uv run --script <path>`.

Inspect the context JSON. It has these top-level keys:

```yaml
prospect:                # account, opportunity, stage, owner, se_name, se_title, dates
contacts:                # internal (Airbyte) and prospect contact lists
business_objectives:     # list of customer-derived objectives
technical_scope:         # sources, destinations, use_cases, requirements, dependencies
success_criteria:        # list of validation criteria
milestones:              # list of POV milestones with statuses
feature_requests:        # list of product asks
architecture_notes:      # list of notes / placeholders
unknowns:                # list of unresolved items
source_coverage:         # which sources were checked, available, material
status:                  # complete | partial | blocked
warnings:                # human-readable gaps
```

If `status == "blocked"`, do not proceed to Google Sheets. Write the local receipt (see Step 4) and
explain which upstream skill to run (e.g., `biz-qual`, `tech-qual`, `poc-plan`, `connector-feasibility`).

### Step 0c: Check for an existing POV sheet

Before copying the template, search the configured Drive target folder (and any prospect subfolder)
for a sheet titled `Airbyte || [Prospect Name] - POV Success Criteria`. If one exists, stop and
present:

- The existing sheet title
- The direct Google Sheet URL
- The Drive folder where it lives

Then ask the user to choose one of:

1. **Use the existing sheet** — stop and return the link.
2. **Create a new version** — proceed to Step 1, but name the copy `Airbyte || [Prospect Name] - POV Success Criteria v2` (or v3, etc.).
3. **Stop** — do nothing.

Do not silently overwrite or duplicate an existing POV sheet.

### Step 1: Copy the Template

**With a Google Drive MCP (preferred when configured).** If a Drive MCP exposing `copy_file`,
`create_file` and `search_files` is available (for example the claude.ai Google Drive connector),
no browser is needed:

1. Find the prospect subfolder: `search_files` with
   `parentId = '<DRIVE_TARGET_FOLDER_ID>' and mimeType = 'application/vnd.google-apps.folder'`, matching
   the prospect name case- and accent-insensitively. If none exists, create it with `create_file`
   (`contentMimeType: application/vnd.google-apps.folder`, `parentId: <DRIVE_TARGET_FOLDER_ID>`).
2. `copy_file` the template with `parentId` = the prospect subfolder and `title` =
   `Airbyte || [Prospect Name] - POV Success Criteria`. The copy is created directly in the shared
   drive, so the Move dialog and the cross-shared-drive restriction in Step 4 do not apply.
3. Confirm with `get_file_metadata` that `parentId` and title are right.

**With Chrome.** Open `TEMPLATE_URL` in Chrome and make a copy. The fastest reliable path is the `/copy` URL with the
prospect subfolder as the destination:

```
https://docs.google.com/spreadsheets/d/<TEMPLATE_ID>/copy?copyDestination=<FOLDER_ID>&title=Airbyte%20%7C%7C%20<Prospect>%20-%20POV%20Success%20Criteria
```

If that creates the copy directly in the target folder, confirm the title. If it creates a "Copy of …"
file in My Drive instead, rename it to `Airbyte || [Prospect Name] - POV Success Criteria` and move it
(see Step 3).

If you cannot use the `/copy` URL, open `TEMPLATE_URL` and use **File > Make a copy**.

### Step 2: Fill the Sheets

Always build the fill plan first. It maps the context onto the template's fixed rows and columns
(Step 3) and only emits valid dropdown values:

```bash
node "$REPO_DIR/webapp/scripts/pov-gsheet-runner.mjs" \
  --context "$CTX_FILE" --template-url "$TEMPLATE_URL" \
  --drive-folder-url "$DRIVE_TARGET_FOLDER_URL" \
  --copy-title "Airbyte || $ACCOUNT - POV Success Criteria" \
  --dry-run --out-receipt "$JOB_DIR/pov-gsheet-plan.json"
```

`plan.tabs[<tab>].blocks` lists every write as `{range, values, startCell, tsv}`. `plan.warnings`
lists anything that did not fit (extra contacts, unmatched milestones, a fourth use case) and
`plan.placeholders_to_clear` lists template placeholder cells left behind. Carry both into the receipt.

**Validation is your job.** The template's dropdowns are "strict" in the UI, but neither a clipboard
paste nor the Sheets API rejects an invalid value: it is written and shows a red "Invalid" chip. Only
write values from the dropdown table below (the runner already does this).

#### 2A. With a Google Sheets MCP (preferred when configured)

If a Sheets MCP exposing `update_values` and `get_values` is available (for example the claude.ai
Google Sheets connector), write each plan block with `update_values(range=block.range,
values=block.values)`. Independent blocks can be written in parallel. Then read every written range
back with `get_values` and compare it with the plan. Dates such as `2026-10-07` are stored as dates
(the Milestones tab displays them as `10/7/26`); that is expected.

#### 2B. With Chrome: the clipboard paste method

Google Sheets editing via browser automation requires a specific technique that works reliably. The
method below avoids validation popup errors and cell misalignment issues. Paste each plan block at its
`startCell`; the runner's `--run` mode does exactly this.

#### The Reliable Editing Method

**Prerequisites:**

- Request computer-use access with `clipboardWrite: true`.
- Have the Chrome MCP tools available, **or** use the optional Node/Playwright runner:
  ```bash
  node "$REPO_DIR/webapp/scripts/pov-gsheet-runner.mjs" \
    --context "$CTX_FILE" \
    --template-url "$TEMPLATE_URL" \
    --drive-folder-url "$DRIVE_TARGET_FOLDER_URL" \
    --copy-title "Airbyte || $ACCOUNT - POV Success Criteria" \
    --run
  ```
  If Playwright is not installed or the run fails, fall back to the manual clipboard steps below.

**For each cell or range you need to fill:**

1. **Navigate to the target cell** using the Name Box:
   - Use `find` tool to locate the Name Box element (it's a textbox showing the current cell reference, e.g., "A1")
   - Use `form_input` on the Name Box ref to set the cell address (e.g., "A3")
   - Press Enter to navigate to that cell

2. **Write data to clipboard** using `mcp__computer-use__write_clipboard`:
   - For single cells: just the value text
   - For multi-cell ranges: use TSV format (tabs between columns, newlines between rows)
   - Example TSV for a 3-column, 2-row paste:
     ```
     Value1	Value2	Value3
     Value4	Value5	Value6
     ```

3. **Paste with Cmd+V** (or Ctrl+V on Windows/Linux):
   - The data flows into cells starting from the active cell
   - Tabs map to column separators
   - Newlines map to row separators

**Dropdown values (read from the template's data validation on 2026-10-07).** Use these exact
strings, **including trailing spaces**: several values end in a space inside the quotes, and a value
without it is "Invalid".

| Tab, column | Allowed values | Notes |
|---|---|---|
| POV Milestones, C (Status) | "Needs Scheduling ", "Scheduled ", "Completed ", "In Progress" | Three of four end in a space. There is no "Not Started" or "Blocked". |
| POV - Success Criteria, C (In-scope for POV) | "Yes", "No" | |
| POV - Success Criteria, D (Priority Level) | "Must Have", "Nice to Have", "Out of Scope" | |
| POV - Success Criteria, E (Status) | "Completed", "Pending" | Only these two. New criteria start "Pending". |
| In-scope Apps, B (Source or Destination) | "Source", "Destination" | |
| In-scope Apps, C (App / System Role - Use Cases) | "Use Case #1", "Use Case #2", "Use Case #3" | Multi-select: join with ", " (e.g. "Use Case #1, Use Case #2"). |
| In-scope Apps, D (Integration Type) | "API", "OAuth", "Other means of integration" | Multi-select, same join. |
| In-scope Apps, E (Prod / Non-prod?) | "Prod", "Non-prod" | Leave blank unless known. |
| In-scope Apps, F (Approval Required for Airbyte Connection?) | "No", "Yes" | Leave blank unless known. |
| Feature Requests, C (Airbyte Product Area) | "Product Area 1" only | The template list is a placeholder. Leave C blank and start Notes (J) with "Product area: Connectors / Platform / Cloud / Enterprise." |
| Feature Requests, E (Requirement Priority) | "1 - Critical", "2 - High", "3 - Medium", "4 - Low", "5 - Not Needed" | |
| Feature Requests, F (Requirement Scope) | "POC ", "Implementation" | "POC " ends in a space. |
| Feature Requests, G (Flagged to Airbyte Product) | "Yes", "No" | |
| Feature Requests, I (Status) | "Not Started", "On Roadmap", "Not Planned", "In Progress - At Risk", "In Progress - Off track", "In Progress - On track", "Completed - Pending Verification", "Completed", "Canceled" | |

**CRITICAL — Dropdown value mapping guidance for In-scope Apps:**

- When filling Column C (App / System Role), map each app to the use case(s) it serves:
  - Use Case #1 = Primary/core data pipeline use case (e.g., database replication, warehouse loading)
  - Use Case #2 = Secondary use case (e.g., CRM/SaaS integrations)
  - Use Case #3 = Tertiary/exploratory use case (e.g., custom APIs, homegrown systems)
  - A destination that serves several use cases gets all of them.
- When filling Column D (Integration Type), map the connector type:
  - "API" = REST API and custom API sources
  - "OAuth" = OAuth-based SaaS connectors (e.g., Salesforce, HubSpot)
  - "Other means of integration" = Database connectors (CDC, replication), warehouses, file transfers, Iceberg, or any non-API method

#### What NOT to do

- Don't try typing directly into cells with Tab to advance — it's unreliable
- Don't call the Sheets REST API from the browser page (there is no OAuth token there); use a configured Sheets MCP (2A) or the clipboard method (2B)
- Don't use `navigator.clipboard.writeText()` from JavaScript — it fails with "Document not focused"
- Don't manually click each cell and type — too slow and error-prone

### Step 3: Sheet-by-Sheet Fill Guide

Use the structured context (`$CTX_FILE`) as the single source of truth. Do not invent data.

The template has fixed rows. Writing outside them overwrites template headers. Row map (verified
2026-10-07):

#### Contacts Sheet (20 rows only)

- **A3:B6 (Airbyte evaluation team):** `Name, Role` in A and email in B. A3:A4 hold the `{name}, Account
  Executive` / `{name}, Solutions Engineer` placeholders: AE first, then `SE_NAME, SE_TITLE`, then other
  contacts from `contacts.internal`.
- **Row 8:** executive sponsor row that ships with the template; leave it unless told otherwise.
- **A11:** replace `<<CustomerName>> Contacts` with `[Prospect Name] Contacts`.
- **A13:B15:** three prospect evaluator slots from `contacts.prospect`, replacing `Evaluator`. For more than
  three, insert rows after row 15 first (Sheets MCP `insert_dimension`, or by hand), then re-read the
  layout before writing. Do not invent emails or roles.
- **Row 17:** "Executive Sponsors" heading for the prospect; add sponsors below it only if known.

#### POV Milestones Sheet (A = Action, B = Date, C = Status, D = Stakeholders)

- **Pre-Evaluation, rows 3-7:** Demo; Technical Discovery / Workshop & Architecture Review; Success
  Criteria & Requirements Review; Executive Business Case; POV Mutual Slack channel. Write only B:C (and D
  when stakeholders are known) on the matching row from `milestones`.
- Past milestones are "Completed ", the Success Criteria review is today with "In Progress", booked
  future items are "Scheduled ", everything else stays "Needs Scheduling ". Do not invent exact dates.
- Rows 9-15 ("[OPTION A - MM]") and 17-25 ("[OPTION B - Enterprise]") are alternative working-session
  blocks; fill the one that matches the deal and leave the other for the SE to delete.
- Rows 27-33 are AE-led post-evaluation items; leave them unless dates are known.

#### Business Objectives Sheet (A2:B, one row per objective)

Use `business_objectives` from the context (2–4 concise customer-specific objectives): column A
`Objective #N: <objective>`, column B the driver, pain and desired outcome with the supporting evidence.
This replaces the template's "[TODO - Account Executive]" text, which must not remain in filled rows.

#### POV - Success Criteria Sheet

This is the most important sheet. Use `success_criteria` from the context. Columns A-F are Feature
Description, Validation Criteria, In-scope for POV, Priority Level, Status, Notes.

| Section | Header row | Column headers | Data rows |
|---|---|---|---|
| Initial Setup: Syncing & Connectors | 1 | 2 | 3-5 (sources, destinations, workspace) |
| Use case #1 | 6 | 7 | 8-16 |
| Use case #2 | 17 | 18 | 19-26 |
| Use case #3 | 27 | 28 | 29+ |

- Write the use-case name into the header cell (`A6`, `A17`, `A27`) as `Use case #N: <name>`; group
  criteria by `use_case`.
- Feature Description: `N - <feature_or_capability>`. Validation Criteria: the validation method plus
  `Acceptance: <acceptance_threshold>` (if null, `Acceptance: TBD, confirm with customer`).
- In-scope default "Yes"; Priority default "Must Have"; Status "Pending" ("Completed" only for something
  already proven). Notes: notes or evidence.
- Clear the unused `1 - Feature Name: Description` placeholders (`A8`, `A19`, `A29`).

If a criterion is not testable as written (e.g., "Validate CDC works well"), rephrase it to a measurable
statement such as "Replicate inserts, updates, and deletes from SQL Server to Snowflake with no
unexplained record loss during the agreed test window."

#### In-scope Apps Sheet (headers on row 3, data from row 4; rows 1-3 frozen)

Use `technical_scope.sources` and `technical_scope.destinations`. Columns:

- A = In-scope App / System name
- B = Source or Destination (dropdown)
- C = App / System Role (dropdown, multi-select)
- D = Integration Type (dropdown, multi-select)
- E = Prod / Non-prod? (dropdown; blank unless known)
- F = Approval Required for Airbyte Connection? (dropdown; blank unless known)
- G = Status / Notes
- H = Airbyte Connector Docs (URL, optional)

Use exact dropdown values. Do not use free-text like "CDC (Debezium)", "Database Replication",
"API (REST)", or "Warehouse Load".

#### Feature Requests Sheet (headers on row 1, data from row 2)

Use `feature_requests` from the context. Include only supported product gaps or asks. Columns:

- A = Reported Date
- B = Ticket ID (Airbyte Internal) (blank unless known)
- C = Airbyte Product Area (leave blank; see the dropdown table)
- D = Task / Description
- E = Requirement Priority (dropdown)
- F = Requirement Scope (dropdown; blank unless known)
- G = Flagged to Airbyte Product (dropdown; blank unless known)
- H = Needed Delivery Date
- I = Status (dropdown; new rows "Not Started")
- J = Notes / Validation Steps, starting with `Product area: <area>.`

#### Architecture Diagrams Sheet

Preserve placeholders or add concise notes describing what diagram is needed. Do not fabricate an
architecture diagram from incomplete context.

### Step 4: Move to Prospect Folder (REQUIRED — every PoV sheet)

Every PoV sheet MUST end up inside a prospect-specific subfolder under the canonical Drive target.
If Step 1 used a Drive MCP `copy_file` into the subfolder, this step is only the verification in 4.6
(a misplaced copy can be moved with the MCP's `update_file` and a new `parentId`).

```
Configured customer target folder
└── [Prospect Name]
    └── Airbyte || [Prospect Name] - POV Success Criteria
```

**Canonical target folder URL:** `DRIVE_TARGET_FOLDER_URL`

#### 4.1 Open the target folder in a second Chrome tab

Keep this tab alongside the sheet tab.

#### 4.2 Check for an existing prospect subfolder

Look for a subfolder named exactly after the prospect. If it exists, skip to **4.4**. If it does not
exist, create it.

#### 4.3 Create the prospect subfolder

1. Click `+ New` > `New folder`.
2. Name it exactly as the prospect appears in the workspace.
3. Click `Create`.
4. Open the new folder and copy its URL.

#### 4.4 Move the sheet into the prospect subfolder

1. Switch to the sheet tab.
2. Open the Move dialog (folder-with-arrow icon next to the title).
3. Paste the new subfolder URL (or the existing one) into the search field.
4. Select the folder and click `Move`.
5. If prompted `Change ownership to a shared drive?`, confirm.

#### 4.5 Cross-shared-drive constraint

Moving directly from one Shared Drive to another is blocked. The template copy must be in **My Drive**
when you run Step 4. If the copy landed in another shared drive, make a new copy into the prospect
subfolder (the file ID will change) and delete the original.

#### 4.6 Verify

Refresh the Drive tab. Confirm:

- The prospect subfolder exists under the customer folder.
- The sheet is inside that subfolder.
- The sheet's title reads `Airbyte || [Prospect Name] - POV Success Criteria`.

Do not report success while the file remains in My Drive or another temporary location.

### Step 5: Write a Local Receipt

The external Google Sheet is the primary artifact, but the SE workflow also needs a local record that
the run happened, what inputs were available, and what still needs to be filled.

**Where to save:**

- If the invocation provided an `{out_dir}` path (the webapp does: `{out_dir}/pov-gsheet/`), save it there.
- If no `out_dir` is provided, save under `customers/<Account>/outputs/pov-gsheet/` (or
  `customers/<Account>/opportunities/<Opportunity>/outputs/pov-gsheet/` if an opportunity was given).
- Create the `pov-gsheet/` subfolder if it does not exist.

**Filename:** `pov-gsheet-YYYY-MM-DD-<Prospect-Name>.md` (use `v2`, `v3`, etc. for same-day reruns).

**Receipt content (follow `~/.claude/skills/_se-playbook.md` → Output Document Format):**

```markdown
# <Account> — POV Google Sheet: <Status>
**Date:** <Month DD, YYYY> · **Sheet URL:** <url> · **Status:** <Status>

### At a Glance
- **Google Sheet URL:** <final verified URL>
- **Prospect:** <prospect name>
- **Status:** <created | created with gaps | blocked>
- **SE:** SE_NAME (SE_TITLE)
- **Drive Folder:** <final verified folder URL>

## Receipt
- **Prospect data sources checked:** [list each source from `source_coverage` with available: true/false]
- **Inputs used:** [what was actually pre-filled from the sources]
- **Unresolved fields:** [what could not be filled and why, including the plan's `warnings` and `placeholders_to_clear`]
- **Drive move completed:** Yes / No (with subfolder path)
- **Sheet title:** `Airbyte || [Prospect Name] - POV Success Criteria`

## Source Coverage
[Mirror the `source_coverage` list from the structured POV context. For each entry show source name, available yes/no, freshness/path, whether it was material, and any note.]
```

If the run was `blocked` or `partial`, set `Status` accordingly and explain the missing evidence in
`Unresolved fields`. Do not claim the sheet was created unless Drive placement was verified.

## Consent and permissions

This skill requests the following access. Surface this clearly to the SE before running if the
platform does not already require approval:

- Read the selected account/opportunity workspace files and prior skill outputs.
- Query Salesforce via the configured `mcp__salesforce__run_soql_query` MCP (read-only SOQL).
- Query Gong via the configured `mcp__gong__search_calls` and `gong://calls/{callId}/transcript` MCP resources.
- Read local workspace transcripts.
- Copy the configured Google Sheets template, write the plan, and place the copy in the configured Drive folder, through a configured Google Drive / Google Sheets MCP or by controlling Chrome.
- Write to the system clipboard.
- Create a Google Sheet and create/use a Drive folder.
- Write a local Markdown receipt under the opportunity `outputs/pov-gsheet/` folder.

Do not request access to sources that are not used (e.g. Gmail or Slack unless a configured MCP exists).

## Scope boundaries

Do not:

- Import external dependency skills or personal database files
- Use personal `~/Documents/...` paths or any Ryan-specific configuration
- Build a generalized MCP orchestration framework
- Build new Granola, Gmail, or Slack integrations from scratch solely for this skill
- Invent prospect contacts, emails, or roles
- Invent customer requirements from generic Airbyte knowledge
- Silently overwrite an existing POV sheet
- Replace `poc-plan` or other qualification skills
- Add a general Google Sheets framework
- Claim Salesforce or Gong was searched if the MCP call failed or was not configured

## Data Source Reference

This skill uses the following sources, in priority order:

- **Workspace account/opportunity metadata** — folder structure, `.sfdc-name`, `.se-config.yaml`
- **Prior skill outputs** — `biz-qual`, `tech-qual`, `poc-plan`, `deal-assessment`, `connector-feasibility`, `post-call`, `account-refresher`, etc.
- **Workspace transcripts** — files in `customers/_transcripts/`
- **Salesforce** — optional, via the configured `mcp__salesforce__run_soql_query` MCP; normalized by `webapp/pov_gsheet_bridge.py`
- **Gong** — optional, via the configured `mcp__gong__search_calls` + `gong://calls/{callId}/transcript` resources; normalized by `webapp/pov_gsheet_bridge.py`
- **Granola / Gmail / Slack** — optional, only when a configured integration exists; otherwise recorded as `unavailable`

The deterministic loader is `webapp/pov_gsheet_context.py`. The normalization bridge is `webapp/pov_gsheet_bridge.py`.
Neither requires external dependency skills, note-taking tools, or personal databases.

## Changelog

- 2026-07-15 — Ported from external dependency skill to repository-native context loader; removed external databases and personal-path references.
- 2026-07-15 — Added `.se-config.yaml` `pov_gsheet` block, preflight checks, and structured POV context output.
- 2026-07-16 — Wired Salesforce and Gong MCP results through `pov_gsheet_bridge.py`; removed manual evidence-file preparation from the normal workflow; added existing-sheet detection and consent/permissions disclosure.
- 2026-10-07 — Matched the real template: exact dropdown values (including trailing spaces) read from the template's data validation, the fixed row/column map for every tab, and the note that neither paste nor the Sheets API enforces strict dropdowns. The runner plan now emits per-tab `blocks` with A1 `range` + `values`; added the Google Drive / Google Sheets MCP path (copy into the prospect folder, `update_values` per block) ahead of the Chrome path. Verified by writing the plan into a fresh copy of the template and reading it back.
