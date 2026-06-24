# Web App — Session Log & Resume Notes

A running record of what's been built/changed on the Solutions Team Hub web app, so work can be picked back up after a context reset. Code is all committed + pushed (origin = `Airbyte-Solutions-Team/se-skills`, mine = `gyairbyte/SE-Workflow`). Feature design lives in `LIVE-TRANSCRIBE.md`; setup in `README.md`.

_Last updated: June 24, 2026 — HEAD `28a52fa`._

## What the app is
Local FastAPI + vanilla-JS UI (no build step) over the SE skills suite. `cd webapp && uv run app.py` → http://127.0.0.1:8787 (needs `CPATH/LIBRARY_PATH` for portaudio on this Mac — see "Run" below). Browse team → member's accounts → an account's opportunities → generated outputs; invoke skills; ask follow-ups on outputs; Live Transcribe a Zoom call with an AI copilot.

## Run it (this machine)
```bash
cd ~/airbyte-work/02-repos/se-skills/webapp
export CPATH="/opt/homebrew/include:$CPATH" LIBRARY_PATH="/opt/homebrew/lib:$LIBRARY_PATH"
uv run --python 3.11 app.py    # port 8787
# ALWAYS kill after testing: pkill -f app.py; lsof -ti:8787 | xargs kill -9
```

## Built this session (newest first — see `git log`)
- **Account Outputs count** now sums per-opportunity outputs (`_account_meta` scans `opportunities/*/outputs/` too, skips hidden `.runs/`). Was showing 0 for opp-scoped work.
- **Live Transcribe echo de-dupe** — on speakers the mic re-hears the call (double transcript); a "You" line near-identical to a "Call" line within ~2.5s is dropped (Jaccard ≥ 0.5). Headphones avoid it. `ECHO_HOLD_SEC`/`ECHO_SIM` in app.py.
- **Complete setup docs** in `README.md` (prereqs table, fresh-clone walkthrough, ANTHROPIC_API_KEY how-to, audio routing).
- **Output reader rework**: one connected "doc sheet" (sections divided inside, not floating cards); dense `**Label:** value` → label/value grid; tinted At-a-Glance panel; concise section headers (match sidebar); follow-up **chat bar** at the bottom (quick→Claude API streamed / deep→`claude -p`, `POST /api/output/ask`, shared `askThread()`); Back returns to the specific opp page in one step.
- **Opp page**: removed inline run preview (results go to Generated Outputs, list refreshes on completion); top bar restyled as the accent "command bar"; Generated Outputs grouped by date with PRETTY skill names.
- **Account table**: Type + Close Date columns, closed stage in red; fixed Owner column overflowing the row (retuned the 9-col grid); fixed long `==highlight==` overflow (wrap).
- **Output redesign (renderer)**: auto TOC sidebar, `> [!verdict|risk|blocker|info]` callouts, `==key==` highlights, heading IDs + scroll. Shared "Output Document Format" contract in `_se-playbook.md`; all 13 skills adopted it.
- **Live Transcribe feature** (see `LIVE-TRANSCRIBE.md`): BlackHole sysaudio → sounddevice → faster-whisper (direct, NOT RealtimeSTT — deadlocks on macOS); SSE transcript stream; two-channel You/Call labels; smart ask-bar; reconnect to a still-running session on return; saves to `_transcripts/<Customer>-MM.DD.YY.txt`.

## Environment state (this machine)
- **BlackHole 2ch installed** + loaded; **portaudio** installed (`brew`). Multi-Output Device created (Speakers + BlackHole). NOTE: user still had **ZoomAudioDevice checked** in the Multi-Output — should be unchecked (echo risk); confirm.
- **`ANTHROPIC_API_KEY`** set in `~/.mcp/anthropic.env` (chmod 600) — powers the quick ⚡ ask path; deep 🔧 questions use `claude -p` (no key).
- Whisper model via `SE_WHISPER_MODEL` (default `small`).

## Open TODO / not yet done (pick-up points)
- [ ] Verify two-channel **You/Call labels** live with BlackHole (only single-stream was exercised end-to-end).
- [ ] Verify the **quick ⚡ streaming** path in the live copilot during a real call (output-reader quick path is verified).
- [ ] Echo de-dupe is a heuristic — if a *real* line ever gets dropped, tighten the timing window. Heavily-divergent echoes (Whisper transcribes the two channels very differently, ~0.3 similarity) may still slip through as doubles.
- [ ] Rolling-window + running summary for very long (1hr+) live transcripts (ask-bar currently sends the tail ~12–16k chars).
- [ ] Live sessions are in-memory — survive leaving/returning the page, but NOT an `app.py` restart mid-call (saved transcript file is safe once stopped).
- [ ] Repo skills still hardcode "Gary" as the SE name in ~47 spots (genericize if the repo goes more broadly shared). Real customer names in `_se-playbook.md` examples (Invesco/Raghu/Lumitec) too — fine while private.
- [ ] Both repos are **private**. Don't make public without scrubbing the above.
- [ ] `favicon.ico` 404 in logs — harmless; add an inline favicon to silence if desired.

## Conventions / gotchas to remember
- Kill the 8787 test instance after every test (leftover blocks the user's own run).
- Bump `app.js?v=…` cache-bust in `index.html` on JS changes (CSS auto-busts via Date.now()).
- Customer data never goes in the repo (`01-customers/` gitignored). Genericize repo skill copies.
- Push to **both** remotes (`git push origin main && git push mine main`).
- `./scripts/check-sync.sh` (pre-commit hook) guards the skill-set; content parity (live `~/.claude/skills/` vs repo `skills/`) is manual discipline.
