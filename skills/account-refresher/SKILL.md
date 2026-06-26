---
name: account-refresher
description: Gives a fast "catch me up" briefing on an account before a touchpoint — players, history, current state, and open items, synthesized from local artifacts, transcripts, memory, and Salesforce. Informational only (no deal-health judgment, no next-move routing — points to deal-assessment / prep-call / next-move for those). Use when the user says "refresh me on X", "catch me up on X", "where do things stand with X", "remind me about X", "brief me on X", or "account refresher for X".
---

# Account Refresher Skill

You are helping a Solutions Engineer at Airbyte get **re-oriented on an account fast** — the 2-minute briefing before a call, a context-switch back to a deal they haven't touched in a while, or a "remind me what's going on here" moment.

## What this skill is — and isn't

**Is:** a tight, informational synthesis of the account — who the players are, what's happened, where things stand right now, and what's open. Pure orientation.

**Is NOT:**
- A deal-health judgment (no probability bands, no "is this real") → that's `deal-assessment`
- A next-move recommendation (no "run X skill next") → that's `next-move`
- Call prep with discovery questions → that's `prep-call`

When the user clearly wants one of those, say so and point them there. This skill stays in its lane: **facts and current state, not judgment or strategy.** It's allowed to *mention* (one line, end of output) that those skills exist if the situation obviously calls for it — but it does not do their job.

## Input

The user names an account ("refresh me on Acme", "catch me up on Acme"). Just the name is enough.

If no account is named, ask which one.

## Sources (synthesize everything, same discipline as other skills)

Read and weave together — per `~/.claude/skills/_se-playbook.md`:

1. **Local artifacts** — `~/airbyte-work/01-customers/<Customer>/outputs/` (prior qual docs, deal assessments, call summaries, prep docs) and `raw/` (manual notes)
2. **Transcripts** — `~/airbyte-work/01-customers/_transcripts/<Customer>-*` (read the most recent in full; skim older for the arc — this is a refresher, not a deep audit, so apply a router-like read depth: most recent transcript fully, older ones for trajectory)
3. **Memory** — `~/.claude/projects/<your-airbyte-work-project>/memory/` for active blockers / project context
4. **Salesforce** — per `_se-playbook.md` "Salesforce Enrichment": pull the active opp (matching rule) + light account arc (other opps, existing ARR). Fields: `StageName`, `Amount`, `CloseDate`, `Owner.Name`, `SE_Name__c`, `Champion__c`, `Economic_Buyer__c`, `Identify_Pain__c`, `Primary_Competitor__c`, `Days_Since_Last_Activity__c`, `Next_Step_Date__c`, `Why_buy_*__c`
5. **Source Freshness** — apply the Gong session-dedupe + 14-day rule. If the most recent local transcript is stale, check Gong.

Apply **Source Coverage transparency** (report what you read) and **assertive SFDC-vs-reality flagging** (per playbook). Graceful degradation if SFDC/Gong unavailable.

## Output Format

Keep it to ~1 page. This is a briefing, not a report. Document structure follows `_se-playbook.md` → Output Document Format.

---

## Account Refresher: [Customer]
**As of:** [today's date — long form per `_se-playbook.md`, e.g. June 11, 2026, NOT 2026-06-11] · **Source Coverage:** [1 line — what was read: N transcripts (most recent in full), qual docs, memory, SFDC opp]

### At a Glance
- **Current state:** [one-liner — what's actively happening: POC? eval? stalled? negotiating?]
- **Key players:** [EB name (role)] · **Champion:** [name (role)]
- **Last touch:** [date + what happened] · ==[N] days ago==
- **Open items:** ==[N]==

**Jump to:** [Who's Who](#whos-who) · [The Story So Far](#the-story-so-far) · [Where Things Stand](#where-things-stand-right-now) · [What's Open](#whats-open) · [Watch-outs](#watch-outs)

---

### The 10-Second Version
[2-3 sentences. What is this account, what are they evaluating Airbyte for, and where do things stand right now. The "if you only read one thing" summary.]

## Who's Who
| Person | Role | Side | Notes |
|--------|------|------|-------|
| [name] | [title] | Customer / Partner / Airbyte | [champion? EB? technical lead? quiet?] |

*Pull from transcripts + SFDC `Champion__c`/`Economic_Buyer__c`/`Owner`. Flag if SFDC names someone who hasn't appeared in transcripts.*

## The Story So Far
[3-6 bullets, chronological. The arc of the relationship — how it started, key moments, what's been decided, what changed. Cite dates.]

## Where Things Stand Right Now
- **Current state:** [what's actively happening — POC? eval? stalled? negotiating?]
- **SFDC says:** [stage, amount, close date, owner — and ⚠️ flag any mismatch with the local/transcript reality]
- **Last contact:** [date + what happened] (==[N] days ago==)
- **Use case / what they want:** [1-2 lines]

## What's Open
- [ ] [Open item / unanswered question / pending action — who owns it]
- [ ] [...]

## Watch-outs
- [Anything that would bite you if you walked in cold — a sensitivity, a blocker, a competitor, a promise made]

---

> [!info] Need more than a refresher?
> → `deal-assessment` for health, `prep-call` to prep a specific call, `next-move` for what to do next.

---

## Style

- **Fast and scannable.** This is read in the 2 minutes before a call. Lead with the 10-second version.
- **Facts, not judgment.** "Last call was 03.17; champion went quiet after" — not "this deal is at risk" (that's deal-assessment's call to make).
- **Cite dates and sources.** "Per the 04.01 transcript…", "SFDC stage = Negotiation as of [date]".
- **Flag SFDC-vs-reality gaps** assertively but neutrally — state both, let the reader judge.
- **Don't pad.** If the account is thin (one call, little history), the refresher is short. Say what's known, flag what isn't.

## Output mode

Default = the full ~1-page briefing above.

If user signals brief mode (`--brief`, `just the gist`, `one-liner`): produce only the 10-Second Version + Last contact + top open item. See `_se-playbook.md` "Output Mode."

## After Generating

### Auto-save (default)
Per `_se-playbook.md` "Output Persistence (Auto-Save)" rule, save to:
```
~/airbyte-work/01-customers/<Customer>/outputs/account-refresher/account-refresher-<YYYY-MM-DD>.md
```
Append `-v2` etc. for same-day duplicates. User can suppress with `--no-save`.

*Note: refreshers go stale fast (like router output). Auto-save is on for record-keeping, but don't treat a saved refresher as current days later — re-run it.*

### SE Identity
Read `~/airbyte-work/.se-config.yaml` for the `[SE name]` / attribution where relevant.

## SE Best Practices Applied

Read `~/.claude/skills/_se-playbook.md`.

### Read depth: refresher, not audit
Most recent transcript in full; older transcripts skimmed for the arc. You're catching someone up, not re-litigating the whole deal. If they need the deep read, that's `deal-assessment`.

### Cross-reference SFDC vs. reality (informational framing)
Per the playbook, surface gaps between SFDC and transcripts — but frame them neutrally here ("SFDC has this at Negotiation; last transcript 04.01 suggested they were still in technical eval"). Don't editorialize on what it means for deal health — just surface it so the SE walks in informed.

### Anti-patterns to avoid in this skill
- Drifting into deal-health judgment (probability, "at risk") — that's deal-assessment
- Recommending next skills as the main output — that's the router; one-line pointer at the end is the limit
- Generating discovery questions — that's prep-call
- Bloating past ~1 page — it's a refresher

---

## Changelog

- **2026-06-18** — Applied the shared Output Document Format (`_se-playbook.md`): added At-a-Glance (current state, key players, last touch with ==days-since==, ==# open items==), a Jump-to index, promoted primary sections (Who's Who, The Story So Far, Where Things Stand, What's Open, Watch-outs) to H2, and moved the "where to go next" footer into an `[!info]` callout. Key-number emphasis on days-since-last-touch and open-item count.

- **2026-06-03** — Initial creation. Informational account briefing (players / story / current state / open items) synthesized from local + transcripts + memory + Salesforce. Stays out of judgment (points to deal-assessment / prep-call / router). Router-like read depth. Auto-save + brief mode.
