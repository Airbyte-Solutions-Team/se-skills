---
name: follow-up-email
description: Drafts a customer follow-up email in Gary's voice — conversational, advisory, direct. Pulls from call transcripts and notes for context. Produces either a recap email (after a call) or an issue/resolution email (when reporting a problem). Use when the user says "follow-up email", "draft email", "email recap", "write email to <customer>", or "send <customer> a follow-up".
---

# Follow-Up Email Skill

You are helping a Solutions Engineer at Airbyte draft a customer email in his established voice. The tone and structure are defined in `~/airbyte-work/CLAUDE.md` under "Customer Email Communication Style" — follow it exactly.

## Input

The user will indicate:
- **Customer name** (look up transcripts and notes)
- **Email purpose** — one of:
  - **Recap** (post-call: summarize takeaways, action items, next steps)
  - **Issue report** (something is broken — state issue, what's happening, why, how to fix)
  - **Answering a question** (customer asked something — give a clear advisory answer)
  - **Nudge** (re-engage a stalled deal or follow up on an open item)
- **Any specifics** they want included (specific person to address, action items, dates)

If purpose isn't clear, ask.

## How to Draft

1. **Prefer call summaries over raw transcripts.** Check `01-customers/<Customer>/call-summary-*.md` first — these are pre-digested. Fall back to raw transcripts in `01-customers/_transcripts/<Customer>-*.txt` only if no summary exists or if a specific quote/detail is needed.
2. **Read all relevant source material.** Notes in `01-customers/<Customer>/`, recent transcripts, prior emails in `01-customers/<Customer>/emails/`, and (if relevant) memory records.
3. **Identify the recipient(s).** Pull email addresses from:
   - Prior email threads in the customer folder
   - Transcript attendee names (cross-reference Notion or memory for emails)
   - If unknown, ask Gary
4. **Match purpose to structure** (see below).
5. **Write in Gary's voice** — conversational, advisory, direct. No corporate fluff.

## Output mode

Emails are already short by design — brief mode is the default.

If user signals "longer" or "more context": expand the email by 2-3 sentences with additional rationale, but never sacrifice the direct-answer-first structure. The CLAUDE.md voice rules (fits a phone screen) trump everything else. See `_se-playbook.md` "Output Mode" for rules.

## Structure by Purpose

### Recap Email
```
To: [recipients — extracted from prior thread or transcript attendees]

Hi [name],

Thanks for the time today. Quick recap of where we landed:

**What we covered**
- [bullet 1]
- [bullet 2]

**Action items**
- [Owner] — [action] (by [date])
- [Owner] — [action] (by [date])

**Next step**
[The single concrete next thing — meeting, doc, decision]

Let me know if I missed anything.

Gary
```

### Issue Report Email
Follow the four-part structure from CLAUDE.md exactly:

```
To: [recipients — extracted from prior thread or transcript attendees]

Hi [team/name],

[1. State the issue — one sentence, what's broken]

[2. What's happening — symptoms, behavior the customer is seeing]

[3. Why it's happening — root cause in plain language]

[4. How to fix it — clear steps, primary + alternative if applicable]

[Question / next step]

Gary
```

### Answering a Question

When a customer asks a question, there's usually a concern *behind* it. Apply Voss labeling — name the underlying concern before answering. This makes the answer land instead of sounding defensive.

```
To: [recipient — extracted from prior thread or transcript attendee list]

Hi [name],

[Optional: Voss label of the underlying concern — "It sounds like your security team is reviewing auth options closely." Skip if the question is purely technical with no concern behind it.]

[Direct answer to the question — first sentence]

[Context or reasoning — 2-3 sentences max]

[Recommendation or what you'd do in their shoes]

[Optional: offer to dig deeper or hop on a call]

[Sign-off]
```

**Example with label applied:**
> Customer asked: "Do you support OAuth?"
>
> Email opener: "Sounds like your security team is reviewing how third-party tools authenticate — happy to address. Yes, we support OAuth 2.0 with [details]..."

### Nudge
```
To: [recipients — extracted from prior thread or transcript attendees]

Hi [name],

Wanted to check in on [specific thing — POC, decision, evaluation]. Last we left it, [specific status from prior call/email].

[What you can do to help unblock — be concrete]

When's a good time to reconnect?

Gary
```

## Style (per CLAUDE.md voice rules)

- **Conversational and advisory** — advising them, not reporting at them
- **Direct and clear** — no corporate jargon, no "we hope this email finds you well"
- **Upfront about problems** — don't sugarcoat, don't use "we're looking into it" without specifics
- **Assume technical intelligence** — don't lecture or over-explain
- **Short** — fits a phone screen unless there's a hard technical reason it can't
- **Cite source material** when drafting — call summary date or transcript reference

## After Drafting

Output the email in chat. Ask if Gary wants to:
1. **Refine** (tone tweaks, add/remove sections)
2. **Save as draft** in `~/airbyte-work/01-customers/<Customer>/emails/<MM-DD-YY>-<purpose>.md`
3. **Send via Gmail MCP** (only if he explicitly says send — never auto-send)

Default: chat output only. Never send without explicit instruction.

---

## SE Best Practices Applied to Follow-Up Emails

Read `~/.claude/skills/_se-playbook.md` for full framework details.

### Source Freshness Check (Gong Fallback)
Per `_se-playbook.md` ("Source Freshness Check"): if the most-recent local transcript is more than **14 days old**, search Gong for newer calls before drafting. A recap or nudge based on stale context will sound disconnected from the customer's current state. For Nudge emails especially, recent silence is the substance of the email — knowing whether Gong has anything new is essential.
- Pull the **most recent call only** — do not bulk-pull
- Save to `_transcripts/<Customer-Name>-MM.DD.YY.txt` BEFORE using it (per CLAUDE.md)

### Every email must end with a concrete next step
"Weak next-step setting" is a top SE anti-pattern. Bad: "I'll follow up next week." Good: "Tuesday 2pm, 30 min, you + your security lead + me, agenda: encryption-at-rest and network egress." Reject any draft that doesn't have date + attendees + agenda in the next step.

### For Recap emails: tie action items to MEDDPICC gaps
Don't just list action items — connect them to qualification gaps. Example:
- "[You] — get me 15 min with [EB name] before Friday" (closes Economic Buyer gap)
- "[Me] — send security questionnaire response by Wednesday" (advances Paper Process)

This trains both parties to see the deal as a shared path forward.

### For Issue Report emails: don't sugarcoat (per CLAUDE.md)
Use the four-part structure exactly (state issue → what's happening → why → how to fix). If we don't have a fix yet, say so plainly: "We don't have a permanent fix for this yet. Here's what I'm doing: [specific actions]. I'll have an update by [date]." Vague "we're looking into it" damages trust.

### For Nudge emails: use Sandler negative reverse
If a deal has stalled, don't write another polite "checking in." Use negative reverse framing:

> Hi [name],
>
> Last we talked, [specific status]. I haven't heard back, which usually means one of two things — it's not a priority right now, or there's a blocker on your side I can help with. Either is fine; I just want to know where we stand. Want to either close the file for now or set up a 15-min call to unblock?

This forces a real answer rather than another non-response.

### For Answering a Question emails: apply Voss labeling
If the customer asked a question that has a concern behind it (e.g., "do you support OAuth?" → probably means "our security team will reject this if you don't"), label the underlying concern before answering. "It sounds like your security team is reviewing auth options closely. To answer directly: yes, we support OAuth 2.0 with [details]. If it's useful, I can share our SOC 2 report ahead of the security review."

### Voice consistency check
Every email must pass these tests (from CLAUDE.md):
- No "I hope this email finds you well" or corporate fluff
- No "we're looking into it" without specifics
- Direct answer in the first sentence (for question replies) or first paragraph (for recaps/issues)
- Short — fits a phone screen unless there's a hard technical reason it can't

### Anti-patterns to avoid in this skill
- Closing with "let me know if you have questions" instead of a directive next step
- Recap emails that summarize the meeting without surfacing what's now on the customer's plate
- Issue reports that hide bad news in paragraph 4
- Nudge emails that don't force a real yes/no

---

## After Generating

### Auto-save (default)

Per `_se-playbook.md` "Output Persistence (Auto-Save)" rule, save drafted emails to:
```
~/airbyte-work/01-customers/<Customer>/outputs/emails/email-<YYYY-MM-DD>-<purpose>.md
```

Filename examples: `email-2026-05-28-recap.md`, `email-2026-05-28-nudge.md`, `email-2026-05-28-issue-403-secret.md`. User can suppress with `--no-save`.

### Source Coverage

Include a Source Coverage section at the top: which call summary, transcript, or notes were used to ground the email content.

### SE Identity

Sign-off uses the `name` from `~/airbyte-work/.se-config.yaml`. Don't hardcode "Gary".

### Sending

Never send via Gmail MCP unless the user explicitly says "send it." Default is draft to disk + chat output only.

---

## Changelog

- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Prefers call summaries over raw transcripts. Recipient extraction from prior threads/transcripts. `To:` line in all 4 templates. Voss labeling in Answering-a-Question template (label the concern before answering). 14-day Gong freshness check. Style section normalized to "Style (per CLAUDE.md voice rules)".
- **2026-05-27** — Initial scaffold.
