# CLAUDE.md — SE Skills Web App

Guidance for Claude Code when working in `se-skills/webapp/` (the Solutions Team Hub).

## What this is
Local FastAPI + vanilla-JS UI over the SE skills suite. No build step.
Run: `cd webapp && export CPATH="/opt/homebrew/include:$CPATH" LIBRARY_PATH="/opt/homebrew/lib:$LIBRARY_PATH" && uv run --python 3.11 app.py` → http://127.0.0.1:8787
**Always kill the test instance after testing:** `pkill -f app.py; lsof -ti:8787 | xargs kill -9` (a leftover blocks the user's own run).

Read `SESSION-LOG.md` first for full resume context; `LIVE-TRANSCRIBE.md` for that feature; `README.md` for setup.

## Doc-sync contract (IMPORTANT — keep docs current AS YOU GO)
Docs drift silently. When you change code or skills in this repo, update the docs in the SAME session — do not defer:

- **Touched `webapp/` or `skills/`?** → prepend an entry to `webapp/SESSION-LOG.md` under "Built this session" (newest first) and refresh the `_Last updated:_` header line (date + current HEAD). One entry per commit-sized change.
- **Added/changed/removed a user-facing feature?** → update the relevant README: `webapp/README.md` "What it does" for webapp features; repo-root `README.md` skill table for skills.
- **Added/renamed/removed a skill?** → update the repo-root `README.md` skill table AND `skills/_se-playbook.md` line 3 skill list. `./scripts/check-sync.sh` enforces this set (pre-commit hook) — run it before committing.
- **Changed skill behavior/contract?** → update that skill's `SKILL.md` and, if the shared contract changed, `skills/_se-playbook.md`.

## Conventions / gotchas
- Bump the `app.js?v=…` cache-bust in `static/index.html` on any JS change (CSS auto-busts via `Date.now()`).
- Two git remotes — push to **both**: `git push origin main && git push mine main` (origin = `Airbyte-Solutions-Team/se-skills`, mine = `gyairbyte/SE-Workflow`, both private).
- Customer data never goes in the repo (`01-customers/` is gitignored; `webapp/.member-prefs/` too). Genericize any customer names in repo skill copies.
- `skills/*/SKILL.md` are symlinked into `~/.claude/skills/` — editing them hits the live skills directly.
