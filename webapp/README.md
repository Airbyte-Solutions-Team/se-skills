# SE Skills — Local Hub (web app)

A **local** web UI over the SE skills suite. Browse the team → a member's accounts →
an account's generated outputs, and invoke any skill with a button.

> **Local only.** This runs on your machine, as you, using your already-authed
> Claude Code + MCPs (Gong/Salesforce) + local `~/airbyte-work` files. It is NOT
> a hosted multi-tenant app — see "Why local" below.

## Prerequisites

The app runs on your machine, as you, using your own auth + local files. You need:

| Requirement | Why | Install |
|---|---|---|
| **`uv`** | runs the app + its inline Python deps | `brew install uv` (or astral.sh/uv) |
| **Claude Code CLI** (`claude`) | the app invokes skills via `claude -p`; Create/Update Overview use a separate verified constrained-runtime contract | Canonical-state work requires exactly **2.1.272** and fails closed before evidence is constructed or sent if that version cannot be verified |
| **The skills installed** | the app drives the SE skills suite | from the repo root: `./install.sh` (symlinks skills into `~/.claude/skills/`) |
| **`~/airbyte-work/` workspace** | the app reads/writes `01-customers/` here | the standard SE workspace layout (see the repo root `README.md`) |
| **`portaudio`** | only for **Live Transcribe** (audio device access) | `brew install portaudio` |
| **BlackHole 2ch** | only for **Live Transcribe** capturing call audio | `brew install blackhole-2ch` (see Live Transcribe section) |
| **`ANTHROPIC_API_KEY`** | only for the **fast ⚡ ask-bar path**; optional | see "AI ask-bar key" below — without it, questions route through `claude -p` |

Salesforce / Gong MCPs are optional — the app degrades gracefully without them (no SFDC stage/amount enrichment, no Gong pulls), everything else works. See the repo root `README.md` for those.

## First-time setup (from a fresh clone)

```bash
# 1. clone + install the skills
git clone <repo-url> ~/airbyte-work/02-repos/se-skills
cd ~/airbyte-work/02-repos/se-skills
./install.sh                       # symlinks skills/ → ~/.claude/skills/

# 2. create your SE identity config (used for attribution, signatures, SFDC alias)
#    see the repo root README "Setup" → .se-config.yaml

# 3. (Live Transcribe only) audio deps
brew install portaudio blackhole-2ch

# 4. (fast ask-bar only) set your Anthropic key — see "AI ask-bar key" below

# 5. run it
cd webapp && uv run app.py         # → http://127.0.0.1:8787
```

The **first** `uv run` downloads the Python deps (FastAPI, faster-whisper, torch, etc.) into an isolated env — this takes a few minutes once, then boots instantly after. No manual `pip install`.

## Run it

```bash
cd ~/airbyte-work/02-repos/se-skills/webapp
uv run app.py
# open http://127.0.0.1:8787
```

(`uv run` reads the inline script deps in `app.py` — no separate install needed.)

## AI ask-bar key (optional — for fast streaming answers)

The ask-bars (follow-up chat in an output, and the Live Transcribe copilot) route each question two ways:
- **⚡ Quick** (simple questions) → the **Anthropic Claude API**, fast + streaming — needs `ANTHROPIC_API_KEY`.
- **🔧 Deep** (codebase / connector / troubleshoot questions) → `claude -p` with full repo + skill access — **no key needed**.

**Without a key, nothing breaks** — quick questions just fall back to the (slower) `claude -p` path. To enable the fast path:

1. Create a key at **https://console.anthropic.com** → API Keys → Create Key (requires billing enabled). It's pay-as-you-go; these calls are tiny.
2. Store it securely. The app prefers the OS keyring, with an `ANTHROPIC_API_KEY` environment variable as a fallback:
   ```bash
   # preferred: OS credential store (keyring must be available)
   uv run keyring set se-skills ANTHROPIC_API_KEY
   # fallback: environment variable
   export ANTHROPIC_API_KEY=sk-ant-…
   ```
3. Restart the app. (Treat the key like a password; rotate it in the Console if it leaks.)

Previous `~/.mcp/*.env` files are no longer read by the app. Move any key stored there into the keyring or an env var and delete the plain-text file.

**Model configuration:** you can override which Claude model the app uses for quick-ask, live-ask, deep skill runs, or any individual skill by adding a `models:` block to your `.se-config.yaml` (see `config/se-config.example.yaml`). Missing keys fall back to `default`, then to the app default `claude-sonnet-4-6`. Example values currently supported by the Claude API: `claude-sonnet-4-6`, `claude-opus-4-6`, `claude-haiku-4-5`.

## Live Transcribe — one-time audio setup

The opportunity page has a **🎙 Live Transcribe** button → a copilot page that transcribes a live Zoom call (locally, via faster-whisper) and lets you ask the AI questions against the rolling transcript. Quick questions answer from the transcript (Claude API); deep ones ("is this connector feasible?", "troubleshoot this") route to `claude -p` with full codebase + skill access.

It captures your **Mac's audio**, so it needs a one-time setup:

1. **Install the audio driver + system lib:**
   ```bash
   brew install portaudio       # so the app can read audio devices
   brew install blackhole-2ch   # virtual device to capture system (Zoom) audio
   ```
2. **Audio MIDI Setup** (`/Applications/Utilities/Audio MIDI Setup.app`):
   - **Multi-Output Device** (so you still *hear* the call): create one combining **your speakers/headphones + BlackHole 2ch**. Set your speakers as the primary/clock device. Point macOS system output (and Zoom's speaker) at this Multi-Output.
   - **Aggregate Device** (optional — only for **You vs. Call** speaker labels): combine **your mic + BlackHole 2ch** into one input device.
3. **In the Live Transcribe page:**
   - **Your mic** → pick your microphone, then set a label (default "You").
   - **Call audio (everyone else)** → pick **BlackHole 2ch** (or the Aggregate) and set a label (default "Call"). Leave it on "none" for a single unlabeled stream (mic only).
4. Press **Start**, run your Zoom call, ask the copilot anything; **Stop & Save** writes the transcript to `01-customers/_transcripts/<Customer>-MM.DD.YY.txt` — which `post-call` then consumes. If the app restarts mid-call, reopen the opportunity and the recovered transcript appears with a **Save recovered transcript** button. (Audio capture cannot survive a process restart.)

Notes:
- Transcription is **local** (faster-whisper, CPU). Set `SE_WHISPER_MODEL` (tiny/base/small/medium, default `small`) to trade speed for accuracy.
- The **quick** Q&A path uses the Claude API — set `ANTHROPIC_API_KEY` (or it falls back to the `claude -p` deep path). Customer **audio never leaves your Mac**; only typed quick-questions + transcript text hit the Claude API.
- Speaker labels default to **You** (your mic) vs **Call** (everyone on Zoom), but you can edit them before starting. It still can't separate multiple people on the same Zoom audio pipe — one label per channel.
- **Echo de-dupe:** if you run on **speakers** (not headphones), your mic also hears the call, which would double every line. The app suppresses these — a near-identical "You" line within ~2.5s of a "Call" line is dropped as an echo. **Headphones avoid it entirely** (your mic never hears the call) and give the cleanest transcript.

## What it does

- **Main page** — a calm operational overview of the solutions team: summary counts (members, accounts, opportunities, outputs, running jobs, recent failures, outputs needing attention), a focused **Needs attention** list, compact **Recent activity**, and member directory cards showing each person's accounts, outputs, running jobs, recent failures, outputs needing attention, and last activity. The **Needs attention** list keeps human review workflow items (`awaiting review`, `commented`, `corrected`) distinct from output validation issues (`invalid`, `stale`, `incomplete`) and uses only objective signals already in the app (running/failed/completed jobs, output mtimes, sidecar status, and review feedback); no risk scores or health labels.
- **Member page** — that member's accounts (folders in `~/airbyte-work/01-customers/`), with a **+ Create Account** box. Can also bulk-create accounts from a Salesforce preview (per-account failures surface for retry).
- **Account page** — opportunities use a two-line row hierarchy that emphasizes name, stage, recent activity, outputs, and owner while keeping secondary metadata (amount, close date, type, AE) muted and smaller. Running or failed skill jobs appear as a small activity indicator on the relevant row, and empty states explain what is missing and point to the next action. On narrow screens the table hides lower-priority columns progressively so the remaining fields stay readable.
- **Opportunity workspace, Create Overview, and Update Overview (local mode)** — opening an opportunity loads one server-authoritative workspace payload rather than trusting the browser route. Create requires explicit saved-transcript selection; Update compares the current cumulative manifest with current local evidence, labels new or changed account transcripts as available for review, and sends only explicitly selected delta transcript bodies plus current metadata and the validated base state. Unchanged historical evidence is inherited without resending; missing historical files do not invalidate immutable revisions. A metadata/selection no-op is refused. Successful updates create a new checksummed revision with an exact parent, deterministic typed change set, What Changed, and read-only history; stale-base or evidence mutation leaves the prior pointer current. **Generate** remains a separate artifact workflow and its Markdown is never canonical evidence. Live Transcribe, Coverage Handoff, generation, output navigation, review/history, browser Back, account scope, MEDDPICC, Business Case, and stakeholder disclosures remain intact. Canonical-state human overrides and model-derived Tech Eval evidence remain excluded.
- **Tech Eval / POV Readiness tracker (local mode)** — every exact account/opportunity gets a deterministic five-phase operating checklist covering Plan, Prepare, Execute, Validate, and Close. Gate, required, recommended, and optional items support manual status, owner, note, and last-updated fields. The collapsed summary is gate-aware and shows lifecycle phase, textual readiness, applicable completion counts, blocking gates, and the next unresolved gate. The tracker is atomically persisted under its own hidden opportunity directory, reloads after restart, is never written into canonical Overview revisions, and does not call Claude or treat manual checks as model evidence.
- **Skill generation** — the invoke picker is grouped into tiers (Workflow 1–7 / Late-stage 8–9 / Anytime / When unsure) reflecting real dependency order, and has a **↻ refresh** button to pick up new or renamed skills without restarting the app. Before starting a skill, the invoke modal shows a compact skill summary, an expandable details panel, a calm **Prerequisite** disclosure, and a visible **Expected permissions** disclosure (write / shell / git) with the selected skill's tier/step badge. The deterministic planner reports local facts and advisory source-resolution warnings; it does not claim customer evidence is absent when a skill can check Gong. Advisory missing-qualification conditions are presented as structured choices: the SE must explicitly confirm skipping the upstream skill(s), and that acknowledgement is recorded with the run; cancelling leaves the missing skill(s) available from the command bar. Only deterministically proven local blockers trigger the missing-prerequisite confirmation and override path. Skills perform the final source-sufficiency check and may stop after Gong or other sources fail to provide adequate evidence. The SE must confirm permissions before launching Claude with `--permission-mode acceptEdits`.
- **Output reader** — a rich document view of any saved skill output: decision-first layout (exec card + tiles), top-risks strip, collapsible audit sections, a sidebar that mirrors the Markdown source order exactly (Source Coverage always last), a **follow-up chat bar** (ask questions about the doc, or launch another skill from the chat), an **output review panel** to approve, comment on, or correct a generated doc, deterministic golden-fixture regression tests that run in CI against a mock executor (developer-managed via `pytest --update-golden`, not exposed as a user-facing promotion button because the mock executor does not exercise real `SKILL.md` instructions), and a **deal-assessment compare view** to see what materially changed between two saved deal assessments. The compare view starts with a concise semantic summary (sections changed, risks added or removed, actions changed, At a Glance changes), shows section-by-section before/after with item-level additions/removals for risk and action lists, and keeps the raw Markdown line diff behind an expandable "Raw Markdown diff" audit toggle. Outputs for schema-defined skills are validated against a Pydantic schema; runtime metadata exposes `validation_supported` (whether the skill has a current automatic validator) and `reference_sources_tracked` (whether the skill consumes tracked reference sources). ROI Business Case is reference-tracked through the objection reference used for exact pricing. The Document status bar distinguishes invalid outputs, legacy unvalidated outputs whose checks are unavailable, unsupported output types with no validation-attention state, valid outputs, and reference-freshness warnings. A tracked skill with no generation snapshot receives a provenance warning; reference-free skills do not. The action bar separates the primary **Chat** action from secondary actions (Compare, Export, Back) and a destructive **Delete** button that requires confirmation. The review panel starts as a compact **Review status** line and expands the inline form only when the user chooses Approve, Comment, or Correct; existing feedback is color-coded by action. Redundant `Date`/`Skill` metadata is suppressed, and high-stakes sections such as `Deal Blocker`, `What Would Close It`, and `What Would Lose It` are styled as distinct callout cards. The output list shows concise status badges (`Incomplete`, `Stale source`, `Source changed`, `Checks unavailable`, `Review sources`) instead of uniform warning icons, with full explanations in a tooltip. Each Markdown output gets a `<file>.md.json` sidecar that is written immediately after a skill run and regenerated whenever the Markdown is newer or the sidecar is missing/stale.
- **Output visual design system** — every reader surface is produced by one shared presentation module, `webapp/static/reader.js`. It takes the sanitized HTML from `POST /api/output/render` and applies presentation-only transformations: a single typographic hierarchy (title → metadata → summary → H2 → H3 → prose), spacing-driven ordinary sections instead of a page of boxes, and strong surfaces reserved for the profile summary, verdicts, blockers, risks, wins, and the quiet audit trail. Status color is used only where the content carries status, and callouts pair color with a label so meaning never depends on color alone. Information shapes get treatment that matches them: connector tables with a confident canonical shape become one vertical record per system (any other table stays a table inside a horizontally scrollable wrapper), people lists become one person per row with the name prominent, action lists put the action first with owner/due/status as subordinate chips, labeled facts become scannable label/value pairs, and discovery questions lead their supporting "why this matters" text. Inside a canonical `Discovery Plan` section, the `Must-Ask Questions` group is rendered as the primary group (accented, full-weight questions) and its sibling question groups stay legible but subordinate; grouping only happens when the section's own H2 is the discovery section and exactly one of its sibling H3s is the must-ask group, and Markdown order and text are unchanged. Recognition is always driven by document structure (headings, table headers, list shape) — never by keyword-spotting in prose — so unrecognized content is left untouched rather than guessed at. Narrow widths collapse the document grid to one column and stack summary data, people, connector records, and tables without horizontal page overflow.
- **Output Gallery (local dev, `/gallery.html`)** — a developer-facing page that renders committed synthetic fixtures (`eval/fixtures/gallery/*.md`, one per saving skill plus long/short edge cases) through the exact production path: `POST /api/output/render` → `reader.js`. The preview renders inside an iframe, so the width switcher (desktop / tablet 780px / narrow 420px) gives the document a real viewport and the production responsive breakpoints in `style.css` apply exactly as they would in a resized browser window; the gallery adds no responsive rules of its own. `GET /api/gallery/fixtures` and `GET /api/gallery/fixture?name=…` serve only allowlisted committed fixture names and are registered in local mode only; no customer data is involved.
- **Export & share** — from a **3-dots options menu** on any output: download **PDF** (server-rendered, paginated) or **MD**, or **Export to internal HTML** (a self-contained rs-group page for internal.airbyte.ai). Coverage-handoff outputs additionally support **push-to-repo** — a one-click PR to internal.airbyte.ai with open-PR detection.
- **Live Transcribe** — transcribe a live call with an AI copilot ask-bar. Sessions are persisted to disk, so an app restart mid-call recovers the transcript; you can also name the mic and call channels (e.g. "You" / "Customer") instead of the default labels. If state cannot be written, a warning toast tells you the transcript may not survive a restart.
- **Durable background jobs** — skill runs and live copilot deep-asks are persisted while in progress, so a server restart leaves the *job record* recoverable (or clearly marked as lost) rather than silently disappearing. The running child process cannot be reattached; persistence failures surface a warning toast.
- **Canonical-state runtime, versioning, and evidence boundary (local Slices 2A/2B/4)** — Create and Update use distinct `opportunity_state_create` and `opportunity_state_update` jobs, not skill jobs or generated outputs. Evidence is limited to trusted read-only opportunity metadata plus saved local transcripts explicitly selected by opaque, account-scoped IDs. Update snapshots the exact base and authorized delta before execution, re-resolves metadata and selected bytes before promotion, and rechecks the base while holding the state lock. Its cumulative manifest never silently removes inherited evidence. Revision 1 remains compatible without a parent; later revisions require the exact parent, provenance, and bounded application-computed typed diff, and the complete history is checksum/scope/chain validated on read. Slice 4 completes the canonical frameworks with four typed Business Case areas, all eight stable MEDDPICC dimensions, and a bounded evidence-backed Stakeholder map. Stakeholders carry stable keys, customer roles, category, influence, engagement, stance, blocker context, recommended next engagement, evidence, and explicit missing information; key-role gaps and stakeholder changes are deterministic, while older revisions load as honestly Not established. All three framework cards remain collapsed, responsive, and evidence-inspectable. Before constructing an evidence prompt, the executor runs a bounded, stdin-free `claude --version` from a new isolated temporary directory and requires exactly `2.1.272`; the one-turn invocation uses restricted/safe mode, `--tools ""`, `--disallowedTools "mcp__*"`, strict empty MCP configuration, no skills/slash commands, no Chrome, no session persistence, denied permission prompts, schema-constrained JSON, and bounded stdin/stdout/stderr/time. The prompt, transcript bodies, raw model output, version output, and raw stderr are never persisted. Generated outputs, arbitrary paths, filename-based opportunity inference, numeric scoring, manual canonical corrections, external evidence providers, and Salesforce writes remain excluded.

### Create/Update Overview data/account boundary

Create and Update Overview use the Claude Code authentication already configured on the local machine. Only explicitly selected transcript text leaves the local filesystem for that configured Claude account during synthesis; current metadata and the validated prior canonical state are also supplied for Update. The resulting history remains local under the opportunity workspace. Before using work/customer evidence, verify that Claude Code is authenticated to the approved work account and that every selected transcript is authorized for that account. Do not use a personal Claude account for work/customer evidence, mix personal transcripts into a work workspace, or treat this local filesystem adapter as production multi-tenant durability. Development and automated validation use synthetic fixtures only.
- **Skill-completion toasts** — run a skill, navigate away, and a top-right banner tells you when it's ready with an Open deep-link.
- **Hosted beta (optional, `HOSTED_MODE=1`)** — sign in with Google via Supabase Auth, resolve your Airbyte organization membership, and list or create organization-owned accounts. You can select an account or opportunity and upload, list, download, or delete transcript files (`.txt`, `.md`, `.vtt`, `.srt`, up to 10 MiB by default). Deleting a transcript takes effect immediately — it leaves the list and can no longer be downloaded or used for a new run — while the stored file itself is removed in the background, retried until it is actually gone; a transcript with a queued or running job is refused until that run finishes or is cancelled. For each transcript you can enqueue a durable `post-call` job; a separate worker process claims the job, materializes authorized inputs read-only, invokes the runsc-backed `SkillRuntime`, validates the generated Markdown and sidecar outside the runtime, and persists only valid outputs to private org-scoped Storage and Postgres. The SPA polls the job and, on success, displays the rendered Markdown and validation status. The repository now contains the pinned host contract, Ansible role, image-release tooling, and offline/gated smoke entry points for this worker. Applying the role and running the live smoke are deferred to Slice 5B2B2. When `HOSTED_MODE` is unset the app keeps using the local filesystem and local skill workflows.
- **Hosted review, correction, and approval (`HOSTED_MODE=1`)** — an account page lists its valid hosted outputs, and opening one gives a review workspace rendered through the same `reader.js` presentation pipeline as the local reader. The generated output is version 0 and is never modified: it stays the immutable generation evidence with its original Markdown, sidecar, and provenance. A version rail shows V0 plus every correction, marks exactly one version as current and the rest as historical, and lets you read any of them. You can comment on the exact version you are viewing (plain text, append-only, never revoking an approval), edit the current version's Markdown in a plain textarea, preview it through the same sanitized Markdown renderer, and submit it as a correction. A correction is accepted only if it satisfies the authoritative `post-call` output contract — the same validator used at generation time, with the mode and transcript taken from server-side state — and a rejected correction returns the specific contract errors and changes nothing. An accepted correction becomes a new immutable version, becomes current, and returns the document to **Needs review**; approving applies to the current version only, and older approvals remain in the history as evidence rather than being erased. Comments, corrections, and approvals are recorded in an append-only audit trail.
- **Hosted export of an approved output (`HOSTED_MODE=1`)** — once the current version is approved, the review page's **Export MD** and **Export PDF** buttons download it; until then they are disabled and the page says why. The Markdown you get is the approved bytes exactly as reviewed — not re-rendered or reformatted — and the PDF is built from those same bytes, so what you send out is what was approved. Export always follows the current version: submit a correction and export switches off until you approve that correction. Historical versions stay readable but are never exported. Downloads are produced on demand and nothing is stored or shared by link, and the filename carries only the output id and version number so no account or document title travels with the file. Each download is recorded in the audit trail as an identifier-only export event. Nothing in the document is quietly changed to make a PDF fit: a very wide table is laid out row by row with every cell labelled, image alt/title text and table captions are written out as plain text (no image is ever fetched), and if the PDF font cannot draw a character the document contains — checked for the bold, italic, and monospace styles separately, since they are different fonts — the PDF is refused with a message pointing you at the Markdown download rather than shipping a document with substituted or missing characters. Very large or extremely deeply nested documents are refused the same way instead of being trimmed. (For accented, Cyrillic, Greek, and symbol text to render, the machine running the hosted app needs the DejaVu fonts — `fonts-dejavu-core` on Debian/Ubuntu; installing them on the live deployment is still to come.)
- **Hosted action trail (`HOSTED_MODE=1`)** — the actions that touch customer data or spend compute are recorded for later review: uploading a transcript, deleting one, requesting a `post-call` run, and cancelling a run, alongside the existing comment, correction, approval, and export records. Each entry keeps only who did it, in which organization, what it applied to (account, opportunity, transcript, job) and when — never a filename, a file location, a requested skill name, or any transcript or document text. An action that fails records nothing, and retrying a request does not duplicate the record. Deleting a transcript keeps the record that it was deleted; that record means the deletion was accepted and the transcript is no longer reachable, and the stored file is removed separately afterwards. Internal run mechanics (attempts, retries, timeouts, outcomes) are not part of this trail; they stay with the run itself. There is no in-app screen for this yet — the records are queryable evidence, and how long they are kept is still to be decided.

Invoking a skill shells out to Claude Code headless:
```
claude -p "Use the <skill> skill for <Account>." --permission-mode acceptEdits
```
run from `~/airbyte-work` so your skills, MCPs, and files all resolve exactly as they
do in the terminal. The skill auto-saves its output to
`01-customers/<Account>/outputs/<skill>/`, which then shows up in the UI.

## Security notes

The app is **local only**, runs as you, and keeps customer data in `~/airbyte-work` outside the repo. The Phase 2 hardening added small, deterministic protections around inputs and outputs:

- **Salesforce queries** escape account-name characters (`'`, `"`, `\`, `%`, `_`) before they reach SOQL, so names like `O'Reilly` or `50% Acme` are searched literally and cannot change query semantics.
- **Markdown rendering** is handled by a single shared `webapp/md_render.py` parser. The web reader fetches `POST /api/output/render`, the PDF export, and the internal.airbyte.ai HTML export all call the same `markdown_to_body_html` function, then run the result through `nh3` HTML sanitization. `<script>` tags, inline event handlers (`onclick`), `javascript:` links, and unsupported URL schemes are stripped, while headings, tables, lists, code blocks, admonitions, highlights, and status dots still render identically across all three surfaces.
- **Markdown reader links** are sanitized server-side by `webapp/md_render.py`; only `http:`, `https:`, `mailto:`, `tel:`, and relative URLs survive, and `target="_blank"` is added client-side. `javascript:`, `data:`, `blob:`, and other arbitrary schemes are dropped.
- **Secrets in errors** are redacted by `webapp/security.py` before they appear in subprocess output, exception messages, or the UI. Covered patterns include `Authorization: Bearer/Token/...` headers, Anthropic/GitHub tokens, credentials in URLs, and `*_KEY` / `*_TOKEN` / `*_SECRET` / `*_PASSWORD` environment-style assignments.
- **Input boundaries** are enforced by Pydantic `max_length` on `PushToRepo`, `OutputPdf`, `OutputAsk`, `InvokeBody`, `StartLive`, and `AskLive`; `_safe` blocks path metacharacters in names; `_html_escape` is applied to handover-card `meta`/description/account text.
- **Claude Code permissions** are gated by an explicit SE approval. The webapp still invokes `claude -p ... --permission-mode acceptEdits` because the skills need (a) write access to save markdown under `01-customers/`, (b) shell + git access for skills like `connector-feasibility` that refresh local source checkouts, and (c) MCP access for Salesforce/Gong enrichment. Before launching, the invoke modal shows the skill's required permissions (write / shell / git), and the SE must confirm. A stricter `--permission-mode` would still break skills that need file writes, so the gate is implemented as a pre-launch approval rather than a mode change.

## How accounts map to the filesystem

- An "account" = a folder in `~/airbyte-work/01-customers/<Account>/`
- Creating an account makes that folder + `outputs/` + `raw/`, and writes a `.owner` file tagging the member
- Outputs = the `.md` files your skills already save under `outputs/<skill>/`

Nothing new or proprietary — the app is a window onto the structure the skills
already produce.

## Why local (and not deployed to internal.airbyte.ai)

Invoking a skill needs **compute + your Anthropic auth + access to your data sources**.
The team hub (`internal.airbyte.ai`) is a static GCS site — it can't run an agent.
A deployed version would need a backend service, per-user Salesforce/Gong auth, and a
hosted customer-data store (multi-tenant). That's a real product, out of scope here.
Running locally sidesteps all of it: it's just *you*, on *your* machine.

(The app *does* export/push finished outputs to internal.airbyte.ai as static HTML — that's publishing a rendered artifact, not running the agent there. The skill still runs locally.)

If the team later wants a shared deployment, the backend (`app.py`) is the seed — but
it would need: hosted auth, per-user credential isolation, and a central (or per-user)
data store instead of `~/airbyte-work`.

## Config

- `team-members.yaml` — who shows on the main page. Edit to add teammates.
- Ownership is per-account via the `.owner` file; unowned accounts show to everyone.

## Hosted mode (optional)

Set `HOSTED_MODE=1` to use Supabase Auth + Postgres instead of the local filesystem. Required environment variables:

- `SUPABASE_URL` / `SUPABASE_ANON_KEY` — SPA Supabase Auth configuration.
- `DATABASE_URL` — `postgresql://app_user:PASSWORD@host/db` (the runtime role, `NOBYPASSRLS`).
- `DATABASE_ADMIN_URL` — `postgresql://app_admin:PASSWORD@host/db` (used only for migrations, `BYPASSRLS`).
- `MIGRATE_DATABASE_URL` — a superuser/admin connection used to run `scripts/migrate.py`.
- `APP_USER_PASSWORD` / `APP_ADMIN_PASSWORD` — the role passwords created by the migration.
- `HOSTED_CONTEXT_SECRET` — a strong shared secret used to sign tenant-context tokens between the app and the database. Generate a long random value and keep it with `APP_USER_PASSWORD`/`APP_ADMIN_PASSWORD`.
- `HOSTED_JWT_ALGORITHM` (`HS256` for local tests, `RS256` for Supabase) and `HOSTED_JWT_SECRET` or Supabase JWKS.
- `SUPABASE_JWT_SECRET` — the Supabase JWT secret used by the backend to sign short-lived Storage JWTs for the dedicated `app_storage` Postgres role. It is only used server-side and must not be sent to the browser.
- `TRANSCRIPT_MAX_BYTES` (optional, defaults to `10485760` — 10 MiB).
- `ANTHROPIC_API_KEY` / `ANTHROPIC_API_URL` / `ANTHROPIC_API_VERSION` —
  used only by the worker-side model proxy (not the sandbox) to forward
  Anthropic Messages API calls. Hosted production also requires
  `ANTHROPIC_EGRESS_PROXY_URL`, `HOSTED_ANTHROPIC_PROXY_HOST`, and
  `HOSTED_ANTHROPIC_PROXY_PORT`; direct Anthropic egress is disabled.
- `MODEL_PROXY_SECRET` — a strong secret used to sign per-job model-proxy capability tokens.
- `RUNSC_BINARY` — path to the `runsc` executable when selecting `post-call-runsc` runtime.
- `RUNSC_ROOTFS` — path to the pinned sandbox rootfs when selecting `post-call-runsc` runtime.
- `SANDBOX_MANIFEST_PATH` — root-owned approved image/evidence manifest,
  defaulting to `/etc/se-skills/sandbox-manifest.json`.
- `SANDBOX_IMAGE_DIGEST` — required deployed registry manifest digest; it must
  match the approved manifest and any non-null repository pin.
- `RUNSC_BUNDLE_DIR` and `RUNSC_STATE_DIR` — durable bundle and runsc state
  directories, defaulting to `/var/lib/se-skills/bundles` and
  `/var/lib/se-skills/runsc`.
- `HOSTED_APPROVED_HTTPS_DESTINATIONS` — comma-separated IP/CIDR destinations
  approved by the live firewall check. Production preflight fails closed when
  this set is empty.

Run migrations before starting the app:

```bash
HOSTED_MODE=1 \
  MIGRATE_DATABASE_URL=postgresql://postgres:...@db/se-skills \
  APP_USER_PASSWORD=... APP_ADMIN_PASSWORD=... HOSTED_CONTEXT_SECRET=... \
  uv run scripts/migrate.py
```
