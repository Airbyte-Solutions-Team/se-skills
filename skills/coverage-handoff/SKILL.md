---
name: coverage-handoff
description: Generates a PTO coverage handoff — a self-contained styled HTML page that lets a covering Solutions Engineer confidently take over an opportunity while the owner SE is out. Synthesizes players, history, current state, open items, in-flight commitments, live technical threads, and access/logistics from transcripts + Salesforce + prior qual docs/deal-assessments, and injects SFDC hard facts verbatim. Consumes PTO form context (who's out, covering SE, coverage window). Output is portable to internal.airbyte.ai. Use when the user says "coverage handoff", "PTO handoff", "handoff for X", "cover my accounts", or is preparing to hand an account to another SE.
---

# Coverage Handoff Skill

You are helping a Solutions Engineer at Airbyte hand off an **opportunity** to a **covering SE** while the owner is on PTO. The covering SE will read this cold and must be able to support the account confidently — answer questions, run the next call, avoid stepping on landmines — without the owner present.

The deliverable is a **self-contained styled HTML page** in the internal.airbyte.ai "rs-group" design system, so it drops straight into that repo. It is NOT markdown.

## What this skill is — and isn't

**Is:** an actionable, complete takeover briefing for a specific opportunity — who the players are, what's happened, where it stands, what's open, what's been promised, what technical threads are live, and how to access things for the coverage window.

**Is NOT:** a deal-health verdict (that's `deal-assessment`) or a customer-facing document. It is internal, candid, and covering-SE-focused.

## Input

- **Account + opportunity** (the webapp passes both; if run manually and none given, ask).
- **PTO context**, passed in the invocation as free text (from the webapp modal form). Expect only: SE out, covering SE, coverage window (start/end). Use these verbatim in the coverage banner. If missing, render "Not specified". Everything else — players, meetings, open items — you derive from transcripts + Salesforce (do not expect a form to supply them).

## Sources (synthesize everything, same discipline as other skills)

Read and weave together — per `~/.claude/skills/_se-playbook.md`:

1. **Local artifacts** — `{customers_dir}/<Customer>/outputs/` (qual docs, deal-assessments, call summaries, prep docs, connector-feasibility) and the opportunity's own `opportunities/<slug>/outputs/`, plus `raw/` (manual notes — often where org/workspace IDs, Slack channels, and promises live). Paths per playbook → Workspace Paths.
2. **Transcripts** — `{transcripts_dir}/<Customer>-*` — most recent in full; older skimmed for the arc and for **promises/commitments** made to the customer.
3. **Memory** — `memory_dir` (skip if unset) for active blockers / project context (often records live technical threads and access details).
4. **Salesforce** — per `_se-playbook.md` "Salesforce Enrichment": pull the opportunity (matching rule) + account arc. Fields to pull and **inject verbatim** (see below).
5. **Source Freshness** — apply the Gong session-dedupe + 14-day rule. If the most recent local transcript is stale, check Gong.

Apply **Source Coverage transparency** and **assertive SFDC-vs-reality flagging** (per playbook). Graceful degradation if SFDC/Gong unavailable — mark missing facts "Unknown", never fabricate.

### SFDC hard facts — inject VERBATIM (do not paraphrase or reconstruct)

Copy these straight from the SFDC query into the page; IDs and numbers must be exact:
`Name`, `StageName`, `Stage_Number__c`, `Amount`, `CloseDate`, `Type`, `Owner.Name`, `SE_Name__c`, `Probability__c`, `Next_Step_Date__c`, `Days_Since_Last_Activity__c`, `Champion__c`, `Economic_Buyer__c`, `Identify_Pain__c`, `Primary_Competitor__c`, `Why_buy_anything__c`, `Why_buy_now__c`, `Most_important_sources__c`, `Most_Important_Destinations__c`, `Use_case_description__c`, `Region__c`, `SE_Deal_Risks__c`, `At_risk__c`. If org/workspace IDs are known (raw notes / memory / prior handover), include them in Access & Logistics verbatim.

## Output Format — HTML in the rs-group design system

**Produce one self-contained HTML file.** Use `template.html` in this skill's own directory (the installed skill lives at `~/.claude/skills/coverage-handoff/`, so read `~/.claude/skills/coverage-handoff/template.html`) as the exact skeleton:

- Copy its `<style>` block **verbatim** (no external CSS — the file must render standalone and inside the internal repo).
- Keep the `.airbyte-auth-marker` and `.footer`.
- Replace every `{{PLACEHOLDER}}` and every example/`REPEAT` row with real content.
- **Delete a section only if its data is genuinely absent** — otherwise fill it, or state "None captured" / "Not specified". Do NOT invent commitments or contacts.
- Dates in the body: long form (e.g. June 11, 2026), per `_se-playbook.md`.
- Emphasis: the header stats carry the key numbers (deal size, stage, win band); don't over-decorate.

### The 11 sections (all present in the template, in order)

1. **Header + Coverage banner** — Customer, opp name, deal size / stage / win band stats; coverage banner (SE out, covering SE, window, trajectory) — SE-out/covering-SE/window from the form, trajectory from SFDC.
2. **The 10-Second Version** — what this deal is, what they're evaluating, where it stands now.
3. **Deal Snapshot + Deal Health** (two cards) — SFDC facts verbatim; health = trajectory, days-since-activity, driver, top MEDDPICC gap, what would lose it.
4. **Who's Who** — table: Name / Title / Role (EB, Champion, technical lead, quiet) / Notes (+contact if known). Flag SFDC names absent from transcripts.
5. **The Story So Far** — dated chronological bullets.
6. **Where Things Stand** — current-state narrative, last contact (days ago), use case; **any upcoming meetings during the coverage window inferred from transcripts/SFDC**; risk callout if SFDC stage ≠ reality.
   - **Surface the deployment shape at a glance** so the covering SE isn't guessing (or re-deriving) it: pull the **current deployment verdict** (Cloud / Flex / park) from the deployment-qualification doc in `outputs/`, and any **`self_managed_only` or enterprise-connector flags** already computed in connector-feasibility's Availability column, and state them in this narrative in one line — e.g. "Deployment: Enterprise Flex (per deployment-qual); one source (`source-db2`) is Self-Managed-only, one is enterprise-tier." This is **display-only** — read the derived verdicts from the saved docs; do NOT pull the connector registry or repos to re-derive them (that's the analytical skills' job, per playbook → Product & Connector Reference Data). If those docs don't exist yet, note "deployment not yet assessed" / "connector availability not yet assessed" — no gate, no refusal.
7. **What's Open** — checkbox list of open items / unanswered questions / pending actions.
8. **In-Flight Commitments** — what the owner SE promised the customer, owner, due, status. (The #1 coverage failure is a dropped promise — mine transcripts/notes hard for these.)
9. **Live Technical Thread** — mid-investigation questions/blockers, state, who it's waiting on.
10. **Access & Logistics** — SFDC opp link, Airbyte Org ID, workspace IDs, Slack channel, Notion/drive links, prior SE doc filenames. SFDC/raw-derived; mark unknowns.
    - **"If you get stuck" (internal escalation) sub-block** — a covering SE inheriting a deal cold needs a routing map: **owner SE's return date** (from the PTO form), **the AE**, **the internal Slack channel**, and **the one eng/PS contact** for any live technical thread (§9). Names or `TBD` — never invented. This is the human analog of "who do I escalate to when blocked"; keep it in Access & Logistics so it's one glance from the IDs/links.
11. **Source Coverage** — what was read, with dates (anti-hallucination). **Note which `template.html` version this was built from** (its top-of-file version/date comment) so that if the internal.airbyte.ai rs-group design system changes, a stale handoff is caught and re-generated rather than hand-edited.

## After Generating

### Auto-save path
Per `~/.claude/skills/_se-playbook.md` → Shared Skill Boilerplate → After Generating (saving skills), save the HTML to the opportunity's outputs dir the caller specifies (the webapp passes an `out_dir` — save under `<out_dir>/coverage-handoff/`). Filename:
```
coverage-handoff-<YYYY-MM-DD>-<Customer>.html
```

### Porting to internal.airbyte.ai
This file is drop-in for `src/solutions-team/members/<member-slug>/accounts/<account-slug>/index.html`. At the end of your run, print that suggested target path so the SE can copy it into the repo and PR it. Do **not** push to the repo yourself.

---

## Style

- **Written for someone who has never touched this account.** Spell out acronyms, name the champion, explain the use case in one line.
- **Facts verbatim, story synthesized.** Never round or reconstruct an ARR, org ID, or close date — copy it. Narrative sections can synthesize.
- **Commitments and open threads are the point.** A covering SE most needs: what's promised, what's live, what's open. Give those weight.
- **Don't invent PTO context.** If the form didn't provide SE-out / covering-SE / window, say "Not specified" — never fabricate.
- **Complete but not bloated.** Every section earns its place; thin accounts get short sections, not padding.

## SE Best Practices Applied

Read `~/.claude/skills/_se-playbook.md` for the source-reading pattern, SFDC field map, freshness rules, and output conventions.

### Anti-patterns to avoid
- Paraphrasing SFDC IDs/amounts instead of copying them
- Inventing commitments or meetings not in the sources
- Drifting into a deal-health verdict (that's `deal-assessment`)
- Emitting markdown instead of the styled HTML
- Linking external CSS (the page must be self-contained)

## Changelog

- **2026-07-10** — "Where Things Stand" now surfaces the deployment shape at a glance for the covering SE: displays the current deployment verdict (Cloud / Flex / park) from the deployment-qual doc and any `self_managed_only`/enterprise-connector flags from connector-feasibility's Availability column. Display-only — reads the derived verdicts from saved `outputs/`, does NOT pull the registry/repos to re-derive; "not yet assessed" if the docs are absent (no gate). Fits the existing section 6 — no new HTML template section.
- **2026-07-10** — Repointed hardcoded `~/airbyte-work/` paths to the workspace-path resolver (`{customers_dir}`/`{transcripts_dir}`/`memory_dir`/`config_file`) per playbook → Workspace Paths; the `template.html` reference is now skill-relative (`~/.claude/skills/coverage-handoff/template.html`) instead of assuming the repo lives at `~/airbyte-work/02-repos/se-skills`. Portable across SE machines.
- **2026-07-09** — Added an "If you get stuck" internal-escalation sub-block to Access & Logistics (owner SE return date, AE, internal Slack channel, one eng/PS contact — names or TBD, never invented) so a covering SE has a routing map when blocked; added a template-version note (Source Coverage records which `template.html` version was used; template now carries a `TEMPLATE VERSION` stamp) so an rs-group design-system change triggers re-generation instead of hand-editing.
- **2026-07-06** — Initial creation. PTO coverage handoff → self-contained HTML in the internal.airbyte.ai rs-group design system. SFDC hard facts injected verbatim; consumes PTO modal-form context; portable to the internal repo (no auto-push).
- **2026-07-06** — Slimmed to 11 sections: removed the form-fed Scheduled-During-Coverage, Guardrails, and Escalation-Contacts sections (meetings are now inferred from transcripts+SFDC). Modal input reduced to SE-out / covering-SE / coverage-window.
