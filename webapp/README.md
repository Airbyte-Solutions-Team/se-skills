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
| **Claude Code CLI** (`claude`) | the app invokes skills via `claude -p` | already have it if you use Claude Code; `claude --version` to check |
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
2. Save it where the app looks (env var, or a `~/.mcp/*.env` file — outside the repo so it's never committed):
   ```bash
   echo 'ANTHROPIC_API_KEY=sk-ant-…' >> ~/.mcp/anthropic.env && chmod 600 ~/.mcp/anthropic.env
   ```
3. Restart the app. (Treat the key like a password; rotate it in the Console if it leaks.)

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
   - **Your mic (You)** → pick your microphone.
   - **Call audio (everyone else)** → pick **BlackHole 2ch** (or the Aggregate) to label the customer/AE side as "Call". Leave it on "none" for a single unlabeled stream (mic only).
4. Press **Start**, run your Zoom call, ask the copilot anything; **Stop & Save** writes the transcript to `01-customers/_transcripts/<Customer>-MM.DD.YY.txt` — which `post-call` then consumes.

Notes:
- Transcription is **local** (faster-whisper, CPU). Set `SE_WHISPER_MODEL` (tiny/base/small/medium, default `small`) to trade speed for accuracy.
- The **quick** Q&A path uses the Claude API — set `ANTHROPIC_API_KEY` (or it falls back to the `claude -p` deep path). Customer **audio never leaves your Mac**; only typed quick-questions + transcript text hit the Claude API.
- Speaker labels are **You** (your mic) vs **Call** (everyone on Zoom) — it can't separate your AE from the customer (they share the Zoom audio pipe).
- **Echo de-dupe:** if you run on **speakers** (not headphones), your mic also hears the call, which would double every line. The app suppresses these — a near-identical "You" line within ~2.5s of a "Call" line is dropped as an echo. **Headphones avoid it entirely** (your mic never hears the call) and give the cleanest transcript.

## What it does

- **Main page** — solutions team members (from `team-members.yaml`, or your `.se-config.yaml` if absent)
- **Member page** — that member's accounts (folders in `~/airbyte-work/01-customers/`), with a **+ Create Account** box. Can also bulk-create accounts from a Salesforce preview (per-account failures surface for retry).
- **Account / opportunity page** — every saved skill output (newest first, concise titles), plus **⚡ Invoke Skill** to run any skill. The invoke picker is grouped into tiers (Workflow 1–7 / Late-stage 8–9 / Anytime / When unsure) reflecting real dependency order.
- **Output reader** — a rich document view of any saved skill output: decision-first layout (exec card + tiles), top-risks strip, collapsible audit sections, grouped TOC, a **follow-up chat bar** (ask questions about the doc, or launch another skill from the chat), and an **output review panel** to approve, comment on, or correct a generated doc. Feedback is stored in a sidecar JSONL file next to the output so it travels with the doc and can be read by anyone who opens it later.
- **Export & share** — from a **3-dots options menu** on any output: download **PDF** (server-rendered, paginated) or **MD**, or **Export to internal HTML** (a self-contained rs-group page for internal.airbyte.ai). Coverage-handoff outputs additionally support **push-to-repo** — a one-click PR to internal.airbyte.ai with open-PR detection.
- **Live Transcribe** — transcribe a live call with an AI copilot ask-bar (see the Live Transcribe section below).
- **Skill-completion toasts** — run a skill, navigate away, and a top-right banner tells you when it's ready with an Open deep-link.

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
- **Exported PDF / internal HTML** run through `nh3` HTML sanitization. `<script>` tags, inline event handlers (`onclick`), `javascript:` links, and unsupported URL schemes are stripped, while normal Markdown (headings, tables, lists, code blocks, admonitions, highlights, status dots) still renders.
- **Markdown reader links** (`webapp/static/app.js::mdToHtml`) only allow `http:`, `https:`, `mailto:`, `tel:`, and relative URLs. `javascript:`, `data:`, `blob:`, and other arbitrary schemes are replaced with `#`.
- **Secrets in errors** are redacted by `webapp/security.py` before they appear in subprocess output, exception messages, or the UI. Covered patterns include `Authorization: Bearer/Token/...` headers, Anthropic/GitHub tokens, credentials in URLs, and `*_KEY` / `*_TOKEN` / `*_SECRET` / `*_PASSWORD` environment-style assignments.
- **Input boundaries** are enforced by Pydantic `max_length` on `PushToRepo`, `OutputPdf`, `OutputAsk`, `InvokeBody`, `StartLive`, and `AskLive`; `_safe` blocks path metacharacters in names; `_html_escape` is applied to handover-card `meta`/description/account text.
- **Claude Code permissions** are unchanged in this phase. Skills are invoked with `claude -p ... --permission-mode acceptEdits` because the skills need (a) write access to save markdown under `01-customers/`, (b) shell + git access for the `coverage-handoff` push-to-repo flow, and (c) MCP access for Salesforce/Gong enrichment. A stricter mode would break these flows. `IMPLEMENTATION-PLAN.md` SEC-007 tracks the future work to design per-skill permission profiles or an explicit approval gate.

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
