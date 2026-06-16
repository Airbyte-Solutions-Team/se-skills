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
