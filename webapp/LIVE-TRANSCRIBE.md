# Live Transcribe + AI Copilot

**Status: shipped** (2026-06-22). This doc is the design record + how it works + what's left to do. The original feature plan is preserved at the bottom.

## What it is

A **🎙 Live Transcribe** button on each opportunity page → a dedicated copilot sub-page (`#/live/<account>/<slug>/<oppName>`) that:
1. Transcribes a live Zoom call in near-real-time, **locally** (faster-whisper, CPU — customer audio never leaves the Mac).
2. Has an AI ask-bar to ask questions grounded in the rolling transcript mid-call — quick ones answer from the transcript; deep ones ("is this connector feasible?", "troubleshoot this") hit the Airbyte codebase + SE skills.
3. On **Stop & Save**, writes the transcript to `01-customers/_transcripts/<Customer>-MM.DD.YY.txt` so `post-call` consumes it.

## How it works (as built)

**Capture → STT.** Mac audio via **BlackHole** → `sounddevice` InputStream per channel → resample to 16k mono float32 → buffer into ~5s windows (RMS-gated for silence) → **faster-whisper** `model.transcribe()` on a worker thread. One shared model; up to two capture streams.
- STT is **faster-whisper used directly** — NOT RealtimeSTT (its realtime path deadlocks on macOS multiprocessing/spawn; discovered in the Phase-1 spike). Direct is simpler and fast: `tiny` ≈45× real-time on CPU; default model `small`, override via `SE_WHISPER_MODEL`.

**Speaker labels (optional two-channel).** Your **mic = "You"**, the **Zoom output (everyone else) = "Call"** — labels by *audio source*, not acoustic diarization. Can't separate your AE from the customer (they share Zoom's pipe). Single-stream (mic only, no labels) is the graceful fallback when no Call device is picked.

**Transport.** Transcript segments push to the browser over **SSE** (`EventSourceResponse`); the ask-bar's quick answers also stream over SSE.

**AI ask-bar routing (auto).** A keyword heuristic (`connector`, `feasib`, `codebase`, `troubleshoot`, `cdc`, `poc`, `meddpicc`, …) classifies each question:
- **Quick ⚡** → Claude API (`anthropic`, `claude-sonnet-4-6`) streaming, with the rolling transcript tail as context.
- **Deep 🔧** → spawns `claude -p` via the existing `_run_job`/`JOBS` system (full repo + skill access), page polls `/api/jobs/{id}` and renders the result.
- No `ANTHROPIC_API_KEY` → quick path returns `needs_deep` so it degrades to the deep path.

**Layout.** Two columns — transcript left (~60%), Q&A thread right (~40%, newest at bottom to match the transcript), full-width sticky ask-bar. Recording timer + pulse; Stop & Save shows a confirmation + "Run post-call summary →" shortcut.

## Files
- `app.py` — `LiveSession` + `_Channel` + `SESSIONS`; endpoints `GET /api/audio-devices`, `POST /api/transcribe/start`, `GET …/{id}/stream` (SSE), `POST …/{id}/ask` (routing), `POST …/{id}/stop` (save+teardown). Audio/LLM deps imported lazily so the app boots without them. Reuses `_safe`, `_titlecase_folder`, `CUSTOMERS_DIR`, `_run_job`/`JOBS`.
- `static/app.js` — `pageLive()`, the `live` router case, Live Transcribe button in `pageOpportunity`. Reuses `pollJob`, `mdToHtml`, `openInvoke`, `accountCrumbs`, `esc`.
- `static/style.css` — `.live-layout`, `.live-askbar`, transcript/segment styling, You/Call chips, recording pulse.
- `README.md` — the one-time audio setup guide.

## One-time setup (recap — full version in README.md)
```bash
brew install portaudio       # for sounddevice (DONE on this machine)
brew install blackhole-2ch   # for system-audio capture (still needed)
```
Audio MIDI Setup: a **Multi-Output** (speakers + BlackHole, so you still hear the call) and optionally an **Aggregate** (mic + BlackHole) for You/Call labels. Set `ANTHROPIC_API_KEY` for the quick path.

## Verified
Single-stream capture → transcription → SSE panel, a deep "is a Snowflake connector feasible?" → `claude -p` → codebase-grounded answer, Stop & Save → transcript file written. Session recovery and custom mic/call labels are covered by deterministic tests but not yet exercised live — needs BlackHole + an API key on the machine. Audio capture cannot survive a process restart; only the transcript is recovered.

## TODO / future ideas (where to pick up)
- [ ] **Verify the two-channel label path live** once BlackHole is installed (start with both mic + BlackHole devices; confirm labels merge correctly by timestamp and that custom mic/call labels render).
- [ ] **Verify the quick ⚡ streaming path** once `ANTHROPIC_API_KEY` is set (token-by-token render).
- [ ] **Rolling-window + running summary** for long calls — currently the ask-bar sends the transcript *tail* (last ~12k chars). For 1hr+ calls, add a periodic running summary of older content + prompt-caching on the stable prefix (per the `claude-api` skill).
- [ ] **Partial (interim) transcript segments** — currently only finalized ~5s windows show. Could show greyed live partials for lower perceived latency.
- [x] **Session recovery** — live sessions are now persisted to disk and recovered on startup; a server restart mid-call preserves the transcript and offers to save it. If state cannot be written, a warning toast tells the SE the transcript may not survive a restart.
- [ ] **Per-person labels** — only achievable with a meeting bot (Recall.ai) tapping per-participant audio; deliberately out of scope.
- [ ] **Auto-run post-call on Stop** — optional, instead of the manual shortcut.
- [ ] **whisper.cpp (Metal) escape hatch** if `small`/`medium` latency is too high on a given Mac.

---
---

# Original plan (preserved)

<details>
<summary>The pre-implementation plan that produced this feature</summary>

(See git history / the sections above — the implementation matched the plan except: STT switched from RealtimeSTT to faster-whisper-direct after the Phase-1 spike found the RealtimeSTT realtime path deadlocks on macOS. All five phases shipped.)

</details>
