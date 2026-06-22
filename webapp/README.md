# SE Skills — Local Hub (web app)

A **local** web UI over the SE skills suite. Browse the team → a member's accounts →
an account's generated outputs, and invoke any skill with a button.

> **Local only.** This runs on your machine, as you, using your already-authed
> Claude Code + MCPs (Gong/Salesforce) + local `~/airbyte-work` files. It is NOT
> a hosted multi-tenant app — see "Why local" below.

## Run it

```bash
cd ~/airbyte-work/02-repos/se-skills/webapp
uv run app.py
# open http://127.0.0.1:8787
```

(`uv run` reads the inline script deps in `app.py` — no separate install needed.)

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

## What it does

- **Main page** — solutions team members (from `team-members.yaml`, or your `.se-config.yaml` if absent)
- **Member page** — that member's accounts (folders in `~/airbyte-work/01-customers/`), with a **+ Create Account** box
- **Account page** — every saved skill output for that account (newest first), click to read; plus **⚡ Invoke Skill** to run any skill on that account

Invoking a skill shells out to Claude Code headless:
```
claude -p "Use the <skill> skill for <Account>." --permission-mode acceptEdits
```
run from `~/airbyte-work` so your skills, MCPs, and files all resolve exactly as they
do in the terminal. The skill auto-saves its output to
`01-customers/<Account>/outputs/<skill>/`, which then shows up in the UI.

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

If the team later wants a shared deployment, the backend (`app.py`) is the seed — but
it would need: hosted auth, per-user credential isolation, and a central (or per-user)
data store instead of `~/airbyte-work`.

## Config

- `team-members.yaml` — who shows on the main page. Edit to add teammates.
- Ownership is per-account via the `.owner` file; unowned accounts show to everyone.
