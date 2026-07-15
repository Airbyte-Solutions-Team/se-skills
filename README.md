# SE Skills Suite — Solutions Engineering Workflow for Claude Code

A suite of Claude Code skills that automate the Solutions Engineering deal lifecycle — call prep, qualification, post-call summaries, deal assessments, objection handling, and internal meeting prep. Grounded in established sales frameworks (MEDDPICC, SPIN, Sandler, Challenger, Voss), wired into Gong (transcripts) and Salesforce (CRM), with outputs auto-saved per customer.

Built by Gary Yang (Solutions Engineer, Airbyte). Designed to be team-shareable.

---

## What's in the suite

| Skill | What it does | Trigger phrases |
|---|---|---|
| `prep-call` | Tech-discovery call prep; inherits the AE's Gong discovery, goes deeper | "prep call X", "call prep" |
| `post-call` | Summarizes a call transcript → takeaways, action items, next steps | "post-call for X", "summarize call" |
| `biz-qual` | MEDDPICC business qualification scorecard | "biz qual", "qualify this deal" |
| `tech-qual` | Technical fit assessment (sources, volume, security, integration) | "tech qual", "assess their stack" |
| `full-qual` | Convenience wrapper — runs biz-qual + tech-qual back-to-back (two separate docs) | "full qual", "run both quals" |
| `deployment-model-qual` | Deployment gate — 3-way verdict (Cloud / Enterprise Flex / park), the 5 deployment questions | "deployment qual", "cloud or flex" |
| `connector-feasibility` | Checks customer source/dest list vs. Airbyte registry | "connector feasibility", "do we have X" |
| `poc-plan` | Structured POC plan with mutual commitments + success criteria | "poc plan", "proof of concept" |
| `deal-assessment` | Honest deal-health read with probability bands | "deal assessment", "is this deal real" |
| `roi-business-case` | Quantified TCO/ROI + one-slide business case for the economic buyer | "roi", "business case", "tco" |
| `mutual-close-plan` | Backward-planned path to signature, owners + dates both sides | "close plan", "mutual action plan", "path to close" |
| `follow-up-email` | Drafts customer emails in your voice | "follow-up email", "draft email" |
| `objection-handler` | Voss-style talk track for a customer objection | "objection", "how do I respond to X" |
| `internal-prep` | Internal meeting prep (ae-sync / forecast / exec-readout / deal-review) | "internal prep", "forecast prep" |
| `account-refresher` | Fast "catch me up" briefing on an account (players, history, state, open items) | "refresh me on X", "catch me up on X" |
| `next-move` | Diagnoses where a customer sits + recommends the next skill | "where am I on X", "what's next for X" |
| `coverage-handoff` | PTO coverage handoff — self-contained HTML page for a covering SE | "coverage handoff", "PTO handoff for X" |
| `pov-gsheet` | Create and pre-fill a POV Success Criteria Google Sheet for a prospect | "POV sheet", "success criteria for X", "prep the POV" |

Plus the shared reference (not a skill): **`_se-playbook.md`** — the SE-craft canon all skills read from. It now also contains a `Shared Skill Boilerplate` section (`Output format reference`, `Pre-flight source check`, `After Generating (saving skills)`) that individual `SKILL.md` files reference as `~/.claude/skills/_se-playbook.md` instead of duplicating.

### Local web app (optional)

`webapp/` is a **local** UI over the suite — browse team member → their accounts → an account's opportunities → generated outputs, invoke any skill with a button, ask follow-up questions on an output, and **Live Transcribe** a Zoom call with an AI copilot. Runs on your machine using your existing Claude Code + MCPs + local files:

```bash
cd webapp && uv run app.py   # → http://127.0.0.1:8787
```

Local-only by design (invoking a skill needs compute + your auth + your data, which a static deploy can't provide).

**Full setup — prerequisites (`uv`, `claude` CLI, portaudio/BlackHole for Live Transcribe, optional `ANTHROPIC_API_KEY`), a fresh-clone walkthrough, and the audio routing — is in [`webapp/README.md`](webapp/README.md).**

### Evaluation framework (`eval/`)

`eval/` runs deterministic, synthetic scenario tests against the skills. By default it uses mock outputs so it works without the `claude` CLI; pass `--run-skills` to invoke the real skills.

```bash
uv run --extra dev pytest eval/ -v       # deterministic suite
uv run --extra dev pytest eval/ -v --run-skills  # real claude invocation
```

See [`eval/README.md`](eval/README.md) for the manifest schema and how to add a scenario.

---

## The workflow chain

```
[ AE does business-discovery call → lands in Gong ]
        ↓
prep-call                 ← inherits AE discovery, preps your tech call
        ↓
[ your tech call → transcript saved to _transcripts/ ]
        ↓
post-call                 ← digest the call
        ↓
deployment-model-qual     ← gate: Cloud / Flex / park (3-way)
        ↓
biz-qual + tech-qual      ← qualify business + technical fit
        ↓
connector-feasibility     ← coverage check
        ↓
poc-plan                  ← scope the POC
        ↓
roi-business-case         ← compile Metrics → the number the EB signs off on
        ↓
mutual-close-plan         ← path from POC-success to signature (owners + dates)
        ↓
[ ongoing ]
deal-assessment           ← honest health read (every ~2 weeks)
follow-up-email           ← drafts in your voice, as needed
objection-handler         ← when a concern surfaces
internal-prep             ← AE syncs, forecasts, exec readouts
pov-gsheet                ← create + pre-fill a POV Success Criteria Google Sheet

next-move        ← run anytime: "what should I do next on X?"
```

**Not sure what to run?** Invoke `next-move` ("where am I on Acme") — it inspects the customer's state and tells you. **Just need to get oriented before a call?** Invoke `account-refresher` ("catch me up on Acme") — it briefs you on the state of play without the routing.

---

## Setup (for a new team member)

### Required

#### 1. Install the skills
From the repo root:
```bash
./install.sh
```
This symlinks every skill (plus `_se-playbook.md` and the `_reference/` objection reference) into `~/.claude/skills/`. Because they're symlinks, a `git pull` instantly updates your installed skills — no reinstall. Restart Claude Code afterward.

#### 2. SE config (identity + workspace paths)
Copy the template to your workspace root and fill it in:
```bash
cp config/se-config.example.yaml ~/.se-skills/.se-config.yaml   # or wherever your workspace_root is
```
Key fields (see the template for the full annotated version):
```yaml
# Where your customer data / transcripts / notes live.
# Resolution order: $SE_WORKSPACE env var > workspace_root > ~/.se-skills (default)
workspace_root: "~/.se-skills"
# Optional layout override if your tree differs from the default (customers/ transcripts/ notes/):
# layout: { customers_dir: "...", transcripts_dir: "...", notes_dir: "..." }
# Per-user Claude Code memory dir (no portable default — leave unset to disable memory features):
# memory_dir: "~/.claude/projects/<your-project-slug>/memory"

name: "Your Name"
email: "you@airbyte.io"
slack_handle: "@you"
role: "Solutions Engineer"
aliases: ["Nickname"]
ae_pairings: [{ name: "Your AE", role: "AE" }]
salesforce: { org_alias: "airbyte-prod", query_directory: "~/.se-skills", enabled: true }
# Optional — grounds connector availability + entitlement reasoning in live product truth
# (see README → "Optional enhancements" and _se-playbook.md → "Product & Connector Reference Data"):
# reference_data:
#   registry: { oss_url: "...oss_registry.json", cloud_url: "...cloud_registry.json", cache_dir: "registry", cache_ttl_hours: 24 }
#   repos:    { airbyte_platform: "airbyte-platform", airbyte_enterprise: "airbyte-enterprise" }  # enterprise is private/optional
#   connector_models: { enabled: false }
```
Skills read this for the workspace paths, `[SE name]` placeholder, call attribution, email signatures, and SFDC org alias. **No skill hardcodes a workspace path** — everything resolves from `workspace_root` (see `_se-playbook.md` → "Workspace Paths"). If you already have a bespoke tree (e.g. `~/airbyte-work/01-customers`), just point `workspace_root` + a `layout:` block at it — no file migration needed.

#### 3. MCP servers
The suite assumes these MCPs are configured in `~/.claude.json`:
- **Gong** (transcripts) — skills pull AE call transcripts automatically. Configured if you use the team Gong MCP.
- **Notion** (optional) — `post-call` and `objection-handler` write to customer Notion pages.
- **Salesforce** (optional, high-value) — CRM enrichment (SOQL via MCP or the `sf` CLI fallback):
  ```bash
  npm install -g @salesforce/cli          # the sf CLI (NOT brew — deprecated/Gatekeeper)
  sf org login web --alias airbyte-prod --set-default   # browser SSO auth
  npm install -g @salesforce/mcp          # the MCP server (global, NOT npx — lock-timeout)
  ```
  ```json
  "salesforce": {
    "command": "sf-mcp-server",
    "args": ["--orgs", "DEFAULT_TARGET_ORG", "--toolsets", "data,orgs,users", "--allow-non-ga-tools"]
  }
  ```
  Skip any of these and skills degrade gracefully — no SFDC/Notion enrichment, everything else works.

#### 4. `pov-gsheet` (Google Sheets POV)
`pov-gsheet` creates a copy of a POV Success Criteria template in Google Sheets and pre-fills it from the SE workspace: prior skill outputs (`biz-qual`, `tech-qual`, `poc-plan`, `deal-assessment`, `connector-feasibility`, `post-call`, `account-refresher`), workspace transcripts, optional Salesforce `sf` CLI data, and optional evidence from configured MCP integrations (Salesforce, Gong, meeting notes, Gmail, Slack). It does **not** import the original `se-assistant` skill or use DuckDB/personal paths.

`pov-gsheet` is currently **partially operational** in this repo:
- The deterministic context loader, receipt format, source-coverage reporting, and dry-run plan are implemented and tested.
- The thin bridge `webapp/pov_gsheet_bridge.py` normalizes raw Salesforce and Gong MCP output into `ExternalEvidence` JSON that the loader already consumes; no manual evidence-file preparation is required in the normal workflow.
- The optional Playwright helper `webapp/scripts/pov-gsheet-runner.mjs` has Google sign-in detection, existing-sheet handling, and Drive-placement verification, but it cannot be exercised end-to-end without a signed-in Google account.
- External MCP integrations require configured MCP servers (currently none in this environment) to verify runtime retrieval.

Before using it:

1. **Configure `pov_gsheet` in `.se-config.yaml`.** Copy the commented block from `config/se-config.example.yaml`, uncomment it, and set your own values:
   - `template_url` — your copy of the POV Success Criteria Google Sheet (must be a Sheets URL, not an `.xlsx`)
   - `drive_target_folder_url` — the Drive "Customer" folder where prospect subfolders will be created
   - `se_name` / `se_title` — the SE contact shown on the Contacts sheet
2. **Configure MCP integrations if you want external context.** Only configure sources you have consent to read. The skill will record which sources were searched, unavailable, or skipped:
   - **Salesforce** — `mcp__salesforce__run_soql_query` (or `sf` CLI fallback).
   - **Gong** — `mcp__gong__search_calls` + `gong://calls/{callId}/transcript`.
   - **Meeting notes** (e.g., Granola-compatible MCP) — list meetings, transcripts, attendees, decisions, action items.
   - **Gmail** — a configured Gmail MCP with scoped thread search; the skill only extracts POV-relevant facts and does not persist full bodies.
   - **Slack** — a configured Slack MCP with scoped channel/thread search; it distinguishes direct customer evidence from internal interpretation.
3. **Chrome / Google authentication.** The skill uses Chrome browser automation (the `computer-use` MCP, or the optional `webapp/scripts/pov-gsheet-runner.mjs` Playwright helper). Chrome must be signed into the Google account that owns the template and Drive folder. When Claude asks for `clipboardWrite` permission during the run, accept it. The `claude -p` invocation runs with `--permission-mode acceptEdits`; when invoked from the webapp it is flagged as `write + shell` and asks for explicit approval.
4. **(Optional) Install Playwright for the Node helper.** If you prefer the helper over manual `computer-use` steps:
   ```bash
   cd webapp/scripts && npm install
   ```
5. **Run `./install.sh` after pulling.** This symlinks the updated `skills/pov-gsheet/SKILL.md` into `~/.claude/skills/pov-gsheet/`.

`pov-gsheet` fails early if the `pov_gsheet` config block is absent, the deterministic context loader cannot find workspace data, or Chrome automation is unavailable. It writes a local Markdown receipt alongside the generated Google Sheet. The receipt's `source-coverage` section lists each source as `searched`, `unavailable`, or `skipped` — never inventing searches.

### Optional enhancements

#### Local Airbyte repos (deeper connector analysis)
`connector-feasibility` can read live connector **source code** (manifests, CDK internals, `BEHAVIOR.md`) for deeper build-path reasoning than the registry metadata alone provides. This is optional — without it the skill uses MCP/registry data and notes the reduced depth.

To enable:
```bash
git clone --depth=1 https://github.com/airbytehq/airbyte.git            ~/airbyte-work/02-repos/airbyte
git clone --depth=1 https://github.com/airbytehq/airbyte-python-cdk.git ~/airbyte-work/02-repos/airbyte-python-cdk
```
Then point config at wherever you cloned them:
```yaml
airbyte_repos_dir: "~/airbyte-work/02-repos"
```
`connector-feasibility` also draws on a few externally-distributed skills when present — `discovering-connectors` and the `shared-airbyte-skills:*` family (`connector-type-identification`, `connector-health-check`, `query-airbyte-docs`). These ship separately (Airbyte's connector-skills marketplace), not with this repo; the skill degrades if they're absent.

The `airbyte-ops-mcp` server (registry/prod queries, requires GCS creds) powers the connector existence/spec lookups. Without it, `connector-feasibility` falls back to local source + published docs and says so in Source Coverage.

#### Product & connector reference data (availability + entitlements)
Skills that reason about **connector availability** and **which capabilities gate to which edition** (`connector-feasibility`, `deployment-model-qual`, `objection-handler`, `tech-qual`, `poc-plan`) can ground that reasoning in live product truth instead of memory. Four sources, all optional and configured under `reference_data:` in your `.se-config.yaml`:
- **Connector registry JSON** (public, no auth) — the source of truth for connector existence, version, support tier (certified/community), release stage, and **Cloud vs. Self-Managed availability** (a connector in the OSS registry but not the Cloud registry is Self-Managed-only). **No manual setup** — the skill fetches the two registry files from the public URLs and caches them under `{airbyte_repos_dir}/registry/` (default 24h TTL). You just need to be online the first time (or when the cache is stale); after that it reads the cache. Nothing to clone.
- **`airbyte-platform`** (public repo — **you clone it**; ~6.7k files, blobless-shallow keeps it light) — the entitlement definitions (`EntitlementDefinitions.kt`: SSO, RBAC, PrivateLink, self-managed regions, sync-frequency tiers, mappers/encryption + the enterprise-connector `ConnectorEntitlement`s) that map a customer requirement to an edition, plus the `charts/v2/airbyte-data-plane` Helm chart behind the Enterprise Flex story. **`deployment-model-qual` reads this** to ground each of its 5 questions in a named entitlement instead of reasoning from memory.
- **`airbyte-enterprise`** (**private repo** — you clone it *if you have access*; access varies by teammate) — the enterprise connectors the monorepo doesn't have (Oracle, NetSuite, SAP HANA, Workday, ServiceNow, SharePoint, DB2). Skills degrade cleanly and say so when it's absent.
- **`airbyte-connector-models`** (public PyPI, opt-in) — typed Pydantic config models; lowest priority (the registry `spec` already covers most auth/config questions).

**What you actually have to set up:** only the two repos (the registry is auto-fetched). Clone them as blobless shallow clones into your `airbyte_repos_dir` — the private `airbyte-enterprise` is optional, skip it if you don't have access:
```bash
# public — needed by deployment-model-qual / tech-qual / objection-handler (DS2)
git clone --filter=blob:none --depth=1 https://github.com/airbytehq/airbyte-platform.git   ~/airbyte-work/02-repos/airbyte-platform
# PRIVATE — needed by connector-feasibility for enterprise-connector detection (DS3); skip if no access
git clone --filter=blob:none --depth=1 https://github.com/airbytehq/airbyte-enterprise.git ~/airbyte-work/02-repos/airbyte-enterprise
```
Then add the config block (paths are relative to `airbyte_repos_dir`):
```yaml
reference_data:
  registry:
    oss_url:   "https://connectors.airbyte.com/files/registries/v0/oss_registry.json"
    cloud_url: "https://connectors.airbyte.com/files/registries/v0/cloud_registry.json"
    cache_dir: "registry"          # auto-created + auto-populated by the skill (under airbyte_repos_dir)
    cache_ttl_hours: 24
  repos:
    airbyte_platform:   "airbyte-platform"     # public — clone required to use DS2
    airbyte_enterprise: "airbyte-enterprise"   # PRIVATE — clone if you have access; omit otherwise
  connector_models:
    enabled: false                 # opt-in typed models
```
Skip any source and the consuming skills fall back and report it in Source Coverage — they never assert availability/entitlement facts from data they couldn't reach. If `airbyte-enterprise` isn't cloned, `connector-feasibility` still runs and just notes that enterprise-connector coverage wasn't checked. Full spec: `_se-playbook.md` → "Product & Connector Reference Data".

---

## Where outputs go

Paths resolve from `workspace_root` (default `~/.se-skills`; see `_se-playbook.md` → "Workspace Paths"):
```
{customers_dir}/<Customer>/
├── outputs/<skill-name>/      ← auto-saved skill outputs (call-prep/, biz-qual/, etc.)
├── raw/                       ← manual notes, technical docs, AE summaries
└── (transcripts in {transcripts_dir})
```

Filename format: `<skill>-YYYY-MM-DD-<descriptor>[-vN].md`. Outputs auto-save by default; pass `--no-save` to suppress. (Exception: `next-move` is ephemeral — saves only on request.)

---

## Design principles

- **Frameworks, applied — not name-dropped.** MEDDPICC/SPIN/Sandler/Challenger/Voss tactics are baked into each skill at the right moment, not listed abstractly.
- **No hallucinated qualification.** `biz-qual`, `deal-assessment`, `tech-qual`, etc. refuse to run without real customer voice (a transcript). They won't invent a MEDDPICC score from thin air.
- **Source coverage transparency.** Every synthesizing skill reports what it actually read (line counts, files) — anti-hallucination.
- **CRM is a hypothesis, not truth.** Salesforce data is the AE's narrative; transcripts are ground truth. Skills flag SFDC-vs-reality mismatches assertively (this is the highest-value CRM signal).
- **Brief mode.** Any skill: add `--brief` for a tight version.
- **Multi-user & portable.** Identity *and* all workspace paths come from `.se-config.yaml` (resolved via `workspace_root`, overridable by `$SE_WORKSPACE`), so the suite works for any SE on any machine layout — nothing is hardcoded to one person's folders.
- **Graceful degradation.** No Salesforce? No Gong? Skills still run on what's available.

---

## Adapting to other roles

The suite is grounded in MEDDPICC but the frameworks live in `_se-playbook.md` — swap that out to retarget the suite (e.g., a post-sales Solutions Architect workflow, a different sales methodology). The skill structure (source gathering → framework application → structured output → auto-save) is methodology-agnostic.

---

## Maintenance

- Each skill has a `## Changelog` at the bottom — append dated entries when you modify it
- Airbyte-specific positioning (objections, product capabilities) lives in the repo at `skills/_reference/airbyte-objection-reference.md` (installed to `~/.claude/skills/_reference/`) — update there, not in the individual skills, when the product changes. It carries a `Last updated` date + owner/refresh cadence; keep it current.
- The SFDC field map lives in `_se-playbook.md` "Salesforce Enrichment" — update if SFDC fields change
