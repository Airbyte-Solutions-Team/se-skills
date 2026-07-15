---
name: pov-gsheet
description: >
  Create and pre-fill Airbyte POV (Proof of Value) Success Criteria documents in Google Sheets
  for prospects. Use this skill whenever the user asks to create a POV document, success criteria sheet,
  POV tracker, or anything related to preparing a POV for a prospect. Also triggers when asked to
  "fill out the POV template", "set up POV tracking for [prospect]", "create success criteria for
  [account]", "prep the POV sheet", or any variation involving POV + prospect/account name.
  This skill uses a Google Sheets template (not XLSX) with dropdown fields and pulls data from
  Gong calls, Salesforce, and Granola meeting notes via the se-assistant skill.
---

# POV Google Sheets Creator

Creates pre-filled Airbyte POV Success Criteria Google Sheets for prospects by pulling data from prior discovery calls and CRM records.

## Preflight checks (fail fast)

Stop immediately and report a clear, actionable error if any required dependency or configuration is missing. Do not open Chrome or copy a template until these checks pass.

1. **Locate the SE config file.** Look for `.se-config.yaml` in the current working directory (the webapp runs `claude -p` from the workspace root). If it is missing, fall back to `$SE_WORKSPACE/.se-config.yaml` then `~/.se-skills/.se-config.yaml`.
2. **Read the `pov_gsheet` block.** It must contain:
   - `template_url` — the Google Sheets template to copy
   - `drive_target_folder_url` — the Drive "Customer" folder where prospect subfolders are created
   - `se_name` — the SE name shown on the Contacts sheet
   - `se_title` — the SE title shown on the Contacts sheet

   If the block or any required key is missing, stop and say:
   > `pov-gsheet` is missing required configuration. Copy `config/se-config.example.yaml` to your workspace root as `.se-config.yaml`, uncomment the `pov_gsheet:` block, and set `template_url`, `drive_target_folder_url`, `se_name`, and `se_title`.
3. **Verify `se-assistant` is installed.** Run a shell check for `~/.claude/skills/se-assistant/SKILL.md`. If it is missing, stop and say:
   > `pov-gsheet` depends on the `se-assistant` skill, which is not bundled with this repository. Obtain it from your team's source and place it at `~/.claude/skills/se-assistant/`, then re-run. Verify with: `ls ~/.claude/skills/se-assistant/SKILL.md`.
4. **Confirm Chrome / computer-use access is available.** The skill needs the Chrome browser automation MCP with `clipboardWrite: true`. If the first browser action fails with a missing-tool or permission error, stop and say:
   > `pov-gsheet` needs the computer-use MCP and `clipboardWrite` permission. Make sure Chrome is running and you have accepted the permission prompt when it appears.

## Configuration used by this skill

From `.se-config.yaml` (`pov_gsheet` block), assign these values for the rest of the run:

- `template_url` → `TEMPLATE_URL`
- `drive_target_folder_url` → `DRIVE_TARGET_FOLDER_URL`
- `se_name` → `SE_NAME`
- `se_title` → `SE_TITLE`

Use these variables everywhere the original steps referenced the shared template, shared folder, or SE contact details. Do not use the placeholder URLs or example names below.

## Template

**Source template:** `TEMPLATE_URL`

This template has 7 sheets:
1. **Contacts** — Airbyte team + prospect contacts
2. **POV Milestones** — Timeline tracking (Demo, Discovery, Success Criteria, Environment Setup, POV Execution, Wrap-up)
3. **Business Objectives** — Strategic goals the prospect wants to achieve
4. **POV - Success Criteria** — The core sheet: connectors, use cases, validation criteria, priorities
5. **In-scope Apps** — Source/destination systems with sync methods
6. **Feature Requests** — Product asks surfaced during discovery
7. **Architecture Diagrams** — Placeholder for diagrams

## Workflow

### Step 1: Gather Prospect Data

Before touching the Google Sheet, gather all available information about the prospect. Use the **se-assistant** skill to query:

1. **Salesforce** — Account details, opportunity stage, AE name, contacts
2. **Gong calls** — Transcripts from discovery/demo calls (look for technical requirements, pain points, systems mentioned, integration patterns)
3. **Granola meeting notes** — If available, check for recent meeting notes with the prospect

If `se-assistant` cannot be invoked or returns no data, stop and report which source failed. Do not invent prospect data.

Focus on extracting:
- Prospect contacts (names, titles, emails)
- Source systems (databases, SaaS apps, APIs)
- Destination systems (warehouses, lakes)
- Specific technical requirements (CDC, schema drift, performance, IaC)
- Pain points and use cases
- Feature requests or product gaps mentioned
- Timeline and milestone dates

Track which sources were checked and which fields could not be found — you will need them for the receipt.

### Step 2: Copy the Template

Open `TEMPLATE_URL` in Chrome and make a copy:
1. Navigate to `TEMPLATE_URL`
2. Use File > Make a copy
3. Rename the copy to: `Airbyte || [Prospect Name] - POV Success Criteria`

### Step 3: Fill the Sheets Using the Clipboard Paste Method

Google Sheets editing via browser automation requires a specific technique that works reliably. The method below avoids validation popup errors and cell misalignment issues.

#### The Reliable Editing Method

**Prerequisites:**
- Request computer-use access with `clipboardWrite: true`
- Have the Chrome MCP tools available

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
     Value1	Value2	Value3\nValue4	Value5	Value6
     ```

3. **Paste with Cmd+V** (or Ctrl+V on Windows):
   - The data flows into cells starting from the active cell
   - Tabs map to column separators
   - Newlines map to row separators

**Important notes on dropdowns:**
- Google Sheets dropdown cells accept pasted text that matches valid options
- You MUST use the EXACT dropdown values below — any other text will show as "Invalid"
- Known valid dropdown values in this template:
  - **Status** (POV Milestones): "Not Started", "Needs Scheduling", "In Progress", "Completed", "Blocked"
  - **In-scope for POV** (Success Criteria): "Yes", "No"
  - **Priority Level** (Success Criteria, Column D): "Must Have", "Nice to Have", "Out of Scope"
  - **Source or Destination** (In-scope Apps, Column B): "Source", "Destination"
  - **App / System Role - Use Cases** (In-scope Apps, Column C): "Use Case #1", "Use Case #2", "Use Case #3"
  - **Integration Type** (In-scope Apps, Column D): "API", "OAuth", "Other means of integration"
  - **Airbyte Product Area** (Feature Requests): "Connectors", "Platform", "Cloud", "Enterprise"

**CRITICAL — Dropdown value mapping guidance for In-scope Apps:**
- When filling Column C (App / System Role), map each app to its primary use case number:
  - Use Case #1 = Primary/core data pipeline use case (e.g., database replication, warehouse loading)
  - Use Case #2 = Secondary use case (e.g., CRM/SaaS integrations)
  - Use Case #3 = Tertiary/exploratory use case (e.g., custom APIs, homegrown systems)
- When filling Column D (Integration Type), map the connector type:
  - "API" = REST API, SaaS connectors (e.g., Salesforce, HubSpot), custom API sources
  - "OAuth" = OAuth-based authentication connectors
  - "Other means of integration" = Database connectors (CDC, replication), warehouse loads, file transfers, Iceberg, or any non-API method

#### What NOT to do
- Don't try typing directly into cells with Tab to advance — it's unreliable
- Don't use the Google Sheets API (requires OAuth tokens not available in browser context)
- Don't use `navigator.clipboard.writeText()` from JavaScript — it fails with "Document not focused"
- Don't manually click each cell and type — too slow and error-prone

### Step 4: Sheet-by-Sheet Fill Guide

#### Contacts Sheet
- **Row 3-6 (Airbyte team):** AE name + title, `SE_NAME` (`SE_TITLE`), and any other Airbyte contacts
- **Row 11+ (Prospect contacts):** Names, titles, emails from Salesforce contacts and Gong call participants

#### POV Milestones Sheet
- Fill dates and statuses for milestones that have occurred (Demo, Technical Discovery)
- Set "Success Criteria" milestone to current date with "In Progress" status
- Leave future milestones as "Not Started"

#### Business Objectives Sheet
- Extract 2-4 business objectives from call transcripts
- Common patterns: "reduce data pipeline maintenance", "improve data freshness", "consolidate ELT tooling", "enable self-service analytics"

#### POV - Success Criteria Sheet
This is the most important sheet. Structure it as:

**Section 1: Initial Setup (Row 1 header)**
- List each connector (source and destination) as a row
- Include validation criteria, In-scope = "Yes", Priority = "Must Have" / "Nice to Have" / "Out of Scope"

**Section 2+: Use Cases (separate header rows)**
- Group related requirements under use case headers
- Each requirement gets: Feature description, Validation/Acceptance criteria, In-scope, Priority, Notes
- Common Airbyte use cases:
  - CDC reliability & performance (Debezium, LSN tracking, auto-resync)
  - Schema drift management (detection, notification, propagation)
  - Performance benchmarking (throughput, latency targets)
  - Infrastructure as Code (Terraform provider coverage)
  - Monitoring & observability (alerting, logging, dashboards)

#### In-scope Apps Sheet
- List each source and destination system
- Columns: A = In-scope App / System name, B = Source or Destination (dropdown), C = App / System Role (dropdown: Use Case #1/2/3), D = Integration Type (dropdown: API / OAuth / Other means of integration)
- Column C mapping: assign each app to its primary use case number (Use Case #1 for core pipelines, #2 for secondary, #3 for exploratory)
- Column D mapping: use "API" for REST/SaaS connectors, "OAuth" for OAuth-based, "Other means of integration" for database CDC, replication, warehouse loads, file transfers, Iceberg, etc.
- DO NOT use free-text values like "CDC (Debezium)", "Database Replication", "API (REST)", "Warehouse Load" — these are NOT valid dropdown options and will show as Invalid

#### Feature Requests Sheet
- List any product gaps or feature requests mentioned in calls
- Include: Date, Airbyte Product Area, Task/Description, Priority

### Step 5: Move to Prospect Folder (REQUIRED — every PoV sheet)

Every PoV sheet MUST end up inside a prospect-specific subfolder under the canonical Drive target. There is no exception. The shape is:

```
Sales (shared drive)
  └─ Customer
       └─ [Prospect Name]            ← create if it does not exist
            └─ Airbyte || [Prospect Name] - POV Success Criteria
```

**Canonical target folder URL (the `Customer` folder):** `DRIVE_TARGET_FOLDER_URL`

#### 5.1 Open the target folder in a second Chrome tab

Use `tabs_create_mcp` to open a new tab, then `navigate` to `DRIVE_TARGET_FOLDER_URL`. Keep this tab alongside the sheet tab — you'll use it to create the subfolder and to confirm the move at the end.

#### 5.2 Check for an existing prospect subfolder

Look for a subfolder named exactly after the prospect (e.g. `Power Digital Marketing`, `Wise PLC`). Folder name should match the prospect name as it appears in Salesforce, with no `Inc`, `Ltd`, or other legal suffix unless that is part of the SFDC account name.

- If it exists, skip to **5.4** (move into existing subfolder).
- If it does not exist, continue to **5.3**.

#### 5.3 Create the prospect subfolder

In the target Drive tab:

1. Click the `+ New` button in the top-left sidebar.
2. Click `New folder`.
3. Replace the default `Untitled folder` text with the prospect name (exactly as it appears in SFDC).
4. Click `Create`.
5. After creation, double-click the new folder to navigate INTO it. Note the URL — it will look like `https://drive.google.com/drive/u/0/folders/[NEW_SUBFOLDER_ID]`. Copy that URL; you'll paste it into the Move dialog in 5.4.

#### 5.4 Move the sheet into the prospect subfolder

Switch back to the sheet tab. Open the Move dialog (the folder-with-arrow icon next to the title).

1. Click the search icon (magnifying glass) at the right side of the tab strip.
2. Click into the `Search folders or paste URL` input.
3. Paste the new subfolder URL from 5.3 (or the prospect name if it already existed).
4. The folder will appear as a result. Single-click to select.
5. Click `Move`.
6. If Drive prompts `Change ownership to a shared drive?`, click `Move` again to confirm the cross-drive ownership change. This is the expected prompt when moving from My Drive (where the template-copy lands by default) into the `Sales` shared drive.

#### 5.5 IMPORTANT — cross-shared-drive constraint

The standard Move dialog blocks moves from one Shared Drive directly to another (e.g. from the `Company` shared drive to the `Sales` shared drive). The `Sales` drive will appear greyed out in the Shared Drives picker.

This is why the workflow assumes the template copy is sitting in **My Drive** when Step 5 runs. My Drive → `Sales` shared drive is allowed; `Company` shared drive → `Sales` shared drive is not. Do NOT pre-move the copy into another shared drive before Step 5, or you will be stuck. If you find the sheet already lives in another shared drive, the only recourse is `File > Make a copy` into the prospect subfolder followed by deleting the original (the file ID will change).

#### 5.6 Verify

Refresh the target Drive tab. Confirm:

- The prospect subfolder exists under `Sales > Customer`.
- The sheet is inside that subfolder (open the folder and look).
- The sheet's title in the tab still reads `Airbyte || [Prospect Name] - POV Success Criteria`.

**Deprecated path — do NOT use:** `Company > 4_Sales > 4. Customers > [Prospect Name]`. Old PoV sheets live there for historical reasons; new ones do not.

### Step 6: Share the Link

Provide the user with the direct link to the completed Google Sheet.

### Step 7: Write a Local Receipt

The external Google Sheet is the primary artifact, but the SE workflow also needs a local record that the run happened, what inputs were available, and what still needs to be filled. Write a Markdown receipt before finishing.

**Where to save:**
- If the invocation provided an `{out_dir}` path (the webapp does: `{out_dir}/pov-gsheet/`), save it there.
- If no `{out_dir}` is provided, save under `{workspace_root}/customers/<Account>/outputs/pov-gsheet/` (or `{workspace_root}/customers/<Account>/opportunities/<Opportunity>/outputs/pov-gsheet/` if an opportunity was given).
- Create the `pov-gsheet/` subfolder if it does not exist.

**Filename:** `pov-gsheet-YYYY-MM-DD-<Prospect-Name>.md` (use `v2`, `v3`, etc. for same-day reruns).

**Receipt content (follow the shared output-document format):**

```markdown
# <Account> — POV Google Sheet: created
**Date:** <Month DD, YYYY> · **Sheet URL:** <url> · **Status:** created

### At a Glance
- **Google Sheet URL:** <url>
- **Prospect:** <prospect name>
- **Status:** created
- **SE:** SE_NAME (SE_TITLE)
- **Drive Folder:** DRIVE_TARGET_FOLDER_URL

## Receipt
- **Prospect data sources checked:** Salesforce, Gong, Granola (list which ones returned data)
- **Inputs used:** [list of what was actually pre-filled from the sources]
- **Unresolved fields:** [list fields that could not be filled and why — e.g., "No Granola notes found", "AE not in SFDC contacts"]
- **Drive move completed:** Yes / No (with subfolder path)
- **Sheet title:** `Airbyte || [Prospect Name] - POV Success Criteria`

## Source Coverage
- `.se-config.yaml` (`pov_gsheet` block): read
- `se-assistant` skill: invoked for [Salesforce / Gong / Granola — list results]
- Chrome browser automation: used for copy, fill, move
- Google Sheets template: copied from TEMPLATE_URL
- Google Drive target: DRIVE_TARGET_FOLDER_URL
```

If any required source could not be reached, set the `Status` in At a Glance to `created with gaps` and explain in `Unresolved fields`.

## Navigation Tips

**Switching between sheet tabs:**
- Use `find` tool to locate sheet tab buttons at the bottom of the page
- Click the appropriate tab to switch sheets

**Scrolling within the Move dialog:**
- The Move dialog in Google Sheets can be finicky with breadcrumb navigation
- A more reliable approach: open Google Drive in a separate tab, search for the file, and use the Move option from there (right-click > Organize or toolbar Move icon)

## Data Source Reference

This skill works best in combination with the **se-assistant** skill which provides access to:
- **DuckDB database** at `~/Documents/Claude/sales-data/db/db_sales_data.duckdb`
  - Salesforce: opportunities, accounts, contacts
  - Gong: call transcripts, topics, trackers, participants
- **Granola meeting notes** via the Granola MCP tools
- **generate_pov.py** script for automated POV data extraction

## Changelog

- 2026-07-15 — Added preflight checks, `.se-config.yaml` configuration, fail-early behavior, and local receipt output.
