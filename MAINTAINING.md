# Maintaining the SE Skills suite

How to keep everything in sync when you change things. Most drift is now either
**impossible** (auto-derived) or **caught automatically** (drift check + pre-commit
hook) — this doc is the human-readable contract behind that.

## One-time setup after cloning

```bash
./install.sh              # symlink skills into ~/.claude/skills/
./scripts/install-hooks.sh  # install the pre-commit drift check
```

## What stays current automatically (do nothing)

| Surface | Source it derives from |
|---|---|
| Web app skill dropdown (`/api/skills`) | The `skills/` folders — a new skill folder appears automatically |
| Skills Help page & Invoke-modal hint (`/api/skills/help`) | Each `SKILL.md` (frontmatter + parsed sections), read live |
| Account / output lists in the app | The filesystem under `01-customers/` |

Because of this, **you cannot make the app forget a new skill** — adding a
`skills/<name>/SKILL.md` is enough for it to show up.

## What you must update by hand

### When you ADD / REMOVE / RENAME a skill
Update these two human references (the drift check enforces it):
1. **`README.md`** — the skill table under "What's in the suite"
2. **`skills/_se-playbook.md`** — line 3, the `(skill-a, skill-b, …)` list

Optionally, for nicer presentation in the app:
3. **`webapp/app.py` → `SKILL_PRESENTATION`** — add a label/blurb/order entry
   (not required — a new skill auto-derives a default label and sorts last)

Then run `./scripts/check-sync.sh` (or just commit — the hook runs it).

### When you change a skill's BEHAVIOR (not the set)
- The skill's own **`## Changelog`** — append a dated entry. Always.
- **`_se-playbook.md` "Salesforce Enrichment"** field map — if SFDC fields change
- **`reference/airbyte-objection-reference.md`** — if Airbyte product positioning changes
- Memory (`~/.claude/projects/.../memory/`) — if MCP setup or active-engagement facts change

### When you change the WEB APP
- **`webapp/README.md`** and/or the app section in the main **`README.md`** if setup/behavior changes
- **`config/se-config.example.yaml`** if you add a config field the skills/app read

## The drift check

`scripts/check-sync.sh` compares the skill folders on disk against the README
table and the playbook line, and exits non-zero if they diverge. It runs:
- **on demand:** `./scripts/check-sync.sh`
- **automatically:** as a pre-commit hook (installed via `install-hooks.sh`)

Bypass in a pinch with `git commit --no-verify` — but then fix the drift.

## Quick mental model

> Adding a skill = create the folder, add 2 lines (README + playbook), done.
> Everything app-facing follows automatically. The hook stops you if you forget the 2 lines.
