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
| `deployment-model-qual` | Cloud-vs-Self-Managed gate (the 5 deployment questions) | "deployment qual", "cloud or not" |
| `connector-feasibility` | Checks customer source/dest list vs. Airbyte registry | "connector feasibility", "do we have X" |
| `poc-plan` | Structured POC plan with mutual commitments + success criteria | "poc plan", "proof of concept" |
| `deal-assessment` | Honest deal-health read with probability bands | "deal assessment", "is this deal real" |
| `follow-up-email` | Drafts customer emails in your voice | "follow-up email", "draft email" |
| `objection-handler` | Voss-style talk track for a customer objection | "objection", "how do I respond to X" |
| `internal-prep` | Internal meeting prep (ae-sync / forecast / exec-readout / deal-review) | "internal prep", "forecast prep" |
| `account-refresher` | Fast "catch me up" briefing on an account (players, history, state, open items) | "refresh me on X", "catch me up on X" |
| `next-move` | Diagnoses where a customer sits + recommends the next skill | "where am I on X", "what's next for X" |
| `coverage-handoff` | PTO coverage handoff — self-contained HTML page for a covering SE | "coverage handoff", "PTO handoff for X" |

Plus the shared reference (not a skill): **`_se-playbook.md`** — the SE-craft canon all skills read from.

### Local web app (optional)

`webapp/` is a **local** UI over the suite — browse team member → their accounts → an account's opportunities → generated outputs, invoke any skill with a button, ask follow-up questions on an output, and **Live Transcribe** a Zoom call with an AI copilot. Runs on your machine using your existing Claude Code + MCPs + local files:

```bash
cd webapp && uv run app.py   # → http://127.0.0.1:8787
```

Local-only by design (invoking a skill needs compute + your auth + your data, which a static deploy can't provide).

**Full setup — prerequisites (`uv`, `claude` CLI, portaudio/BlackHole for Live Transcribe, optional `ANTHROPIC_API_KEY`), a fresh-clone walkthrough, and the audio routing — is in [`webapp/README.md`](webapp/README.md).**

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
deployment-model-qual     ← gate: is Cloud Pro even viable?
        ↓
biz-qual + tech-qual      ← qualify business + technical fit
        ↓
connector-feasibility     ← coverage check
        ↓
poc-plan                  ← scope the POC
        ↓
[ ongoing ]
deal-assessment           ← honest health read (every ~2 weeks)
follow-up-email           ← drafts in your voice, as needed
objection-handler         ← when a concern surfaces
internal-prep             ← AE syncs, forecasts, exec readouts

next-move        ← run anytime: "what should I do next on X?"
```

**Not sure what to run?** Invoke `next-move` ("where am I on Acme") — it inspects the customer's state and tells you. **Just need to get oriented before a call?** Invoke `account-refresher` ("catch me up on Acme") — it briefs you on the state of play without the routing.

---

## Setup (for a new team member)

### 1. Skills
Copy the skill folders + `_se-playbook.md` + `README.md` into your `~/.claude/skills/`.

### 2. SE identity config
Create `~/airbyte-work/.se-config.yaml` with your details:
```yaml
name: "Your Name"
email: "you@airbyte.io"
slack_handle: "@you"
role: "Solutions Engineer"
aliases: ["Nickname"]
ae_pairings:
  - name: "Your AE"
    role: "AE"
salesforce:
  org_alias: "airbyte-prod"
  query_directory: "~/airbyte-work"
  enabled: true
```
Skills read this for the `[SE name]` placeholder, call attribution, email signatures, and SFDC org alias.

### 3. Gong MCP (transcripts)
Already configured if you use the team Gong MCP. Skills pull AE call transcripts automatically.

### 4. Salesforce MCP (CRM enrichment) — optional but high-value
```bash
npm install -g @salesforce/cli          # the sf CLI (NOT brew — deprecated/Gatekeeper)
sf org login web --alias airbyte-prod --set-default   # browser SSO auth
npm install -g @salesforce/mcp          # the MCP server (global, NOT npx — lock-timeout)
```
Then add to `~/.claude.json` under `mcpServers`:
```json
"salesforce": {
  "command": "sf-mcp-server",
  "args": ["--orgs", "DEFAULT_TARGET_ORG", "--toolsets", "data,orgs,users", "--allow-non-ga-tools"]
}
```
Restart Claude Code. If you skip this, skills degrade gracefully (no SFDC enrichment, everything else works).

---

## Where outputs go

```
~/airbyte-work/01-customers/<Customer>/
├── outputs/<skill-name>/      ← auto-saved skill outputs (call-prep/, biz-qual/, etc.)
├── raw/                       ← manual notes, technical docs, AE summaries
└── (transcripts in ~/airbyte-work/01-customers/_transcripts/)
```

Filename format: `<skill>-YYYY-MM-DD-<descriptor>[-vN].md`. Outputs auto-save by default; pass `--no-save` to suppress. (Exception: `next-move` is ephemeral — saves only on request.)

---

## Design principles

- **Frameworks, applied — not name-dropped.** MEDDPICC/SPIN/Sandler/Challenger/Voss tactics are baked into each skill at the right moment, not listed abstractly.
- **No hallucinated qualification.** `biz-qual`, `deal-assessment`, `tech-qual`, etc. refuse to run without real customer voice (a transcript). They won't invent a MEDDPICC score from thin air.
- **Source coverage transparency.** Every synthesizing skill reports what it actually read (line counts, files) — anti-hallucination.
- **CRM is a hypothesis, not truth.** Salesforce data is the AE's narrative; transcripts are ground truth. Skills flag SFDC-vs-reality mismatches assertively (this is the highest-value CRM signal).
- **Brief mode.** Any skill: add `--brief` for a tight version.
- **Multi-user.** Identity comes from `.se-config.yaml`, so the suite works for any SE who sets up their own config.
- **Graceful degradation.** No Salesforce? No Gong? Skills still run on what's available.

---

## Adapting to other roles

The suite is grounded in MEDDPICC but the frameworks live in `_se-playbook.md` — swap that out to retarget the suite (e.g., a post-sales Solutions Architect workflow, a different sales methodology). The skill structure (source gathering → framework application → structured output → auto-save) is methodology-agnostic.

---

## Maintenance

- Each skill has a `## Changelog` at the bottom — append dated entries when you modify it
- Airbyte-specific positioning (objections, product capabilities) lives in `~/airbyte-work/04-notes/airbyte-objection-reference.md` — update there, not in the skills, when the product changes
- The SFDC field map lives in `_se-playbook.md` "Salesforce Enrichment" — update if SFDC fields change
