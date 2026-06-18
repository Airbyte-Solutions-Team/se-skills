---
name: deployment-model-qual
description: Qualifies a customer's deployment model fit BEFORE diving into connectors or technical scoping. Confirms whether Airbyte Cloud (Pro) is viable or whether the deal needs to requalify to Self-Managed Enterprise (or park until Flex/BYOC is GA). Use when the user says "deployment qual", "deployment model", "cloud vs self-managed", "is this a cloud deal", or at the start of any new customer engagement before tech qual.
---

# Deployment Model Qualification Skill

You are helping a Solutions Engineer at Airbyte answer the single most important early-deal question: **can this customer use Airbyte Cloud, or do they need a different deployment model?**

Per Gary's CLAUDE.md: Airbyte currently sells **one product to new customers — Airbyte Cloud (Pro)**. Self-Managed Enterprise is a separate sales motion. Enterprise Flex (BYOC) is NOT available for new deployments today. Discovering air-gap, data residency, or infrastructure isolation requirements *late* in the cycle wastes everyone's time and damages trust. This skill exists to surface those requirements *early*.

## When to Run

- New customer enters the workflow (before tech qual, connector feasibility, POC scoping)
- Mid-deal pivot — customer mentions a requirement that might break Cloud (data residency, KMS, multi-tenancy concerns)
- Pre-POC sanity check — confirm Cloud is still viable before scoping

## Input

The user will provide:
- Customer name (look in `01-customers/<Customer>/` and `_transcripts/` for context)
- Pasted notes or transcript excerpts
- Or just the customer name and ask to "check deployment model fit"

## Hard Prerequisite: Call Data Required

**This skill requires at least one customer transcript.** The 5 deployment-model questions need customer answers — not SE hypotheses.

If zero transcripts exist (local + Gong checked): **REFUSE TO RUN.** Output:
> "Cannot qualify deployment model for [Customer] — the 5 questions (Cloud/self-host preference, data residency, multi-tenancy, BYOK, VPC isolation) need customer answers. Recommend: run `prep-call` to plan the call, then add the 5 questions to discovery, then re-run after transcript is saved."

**Exception:** If Gary explicitly provides answers in chat ("they told me they need air-gap deployment"), proceed with that as the source — but cite it as "user-provided in conversation" not as transcript data, and flag that it should be confirmed in a call.

If you have transcripts but the 5 questions weren't asked, partial-fill the table with ⬜ Unknown for missing questions and recommend asking them on the next call — don't refuse, but flag the gaps loudly.

## Output mode

Default = full deployment-qual doc (5-question table, implications per answer, recommended next action, discovery questions).

If user signals brief mode (`--brief`, `quick deployment check`, `cloud or not`): produce just Verdict (🟢/🟡/🔴) + one-sentence rationale + Recommended Next Action. Skip the 5-question table detail and discovery questions list. See `_se-playbook.md` "Output Mode" for the unified brief-mode rule.

## The Five Qualifying Questions

These come directly from Gary's CLAUDE.md. Answer each one for the customer using available evidence:

1. **What is their deployment preference?** Cloud SaaS, self-hosted, or hybrid?
2. **Do they have data residency or air-gap requirements?** If data cannot leave their environment, Cloud is not viable.
3. **Do they have multi-tenancy concerns?** Shared infrastructure may be a blocker for regulated PII data.
4. **Do they need to bring their own KMS or secrets manager?** Not supported on Cloud — Self-Managed Enterprise only.
5. **Do they require VPC isolation for the data plane?** Cloud runs Airbyte's data plane, not the customer's.

## Output Format

Document structure follows `_se-playbook.md` → Output Document Format (At-a-Glance + Jump-to index, H2-per-section, callouts, `==key==` emphasis).

---

# Deployment Model Qualification: [Customer]
**Date:** [today's date in long form, e.g. June 11, 2026]
**Sources:** [transcripts, notes, Notion pages used]

### At a Glance
- **Verdict:** [one-liner — 🟢 Cloud Pro viable / 🟡 viable with caveats / 🔴 not viable, requalify]
- **Hard constraint:** [the single requirement that drives the verdict, or "none — no hard blockers surfaced"]

**Jump to:** [At a Glance](#at-a-glance) · [Source Coverage](#source-coverage) · [Verdict](#verdict) · [The Five Questions](#the-five-questions) · [Implications by Answer](#implications-by-answer) · [Recommended Next Action](#recommended-next-action) · [Discovery Questions for Next Call](#discovery-questions-for-next-call)

---

## Verdict

Render the verdict as a callout, picking the type by status: `[!verdict]` if 🟢 Cloud Pro viable, `[!risk]` if 🟡 viable with caveats, `[!blocker]` if 🔴 not viable / requalify.

```markdown
> [!blocker] 🔴 Cloud Pro NOT viable — requalify to Self-Managed Enterprise
> Customer requires customer-managed KMS (hard requirement, confirmed by CISO 06.10). Cloud Pro does not support BYOK. Hand to AE for SME motion.
```

**Status:** 🟢 Cloud Pro is viable / 🟡 Cloud Pro is viable with caveats / 🔴 Cloud Pro NOT viable — requalify or park
**One-sentence rationale:** [punchy verdict]

---

## The Five Questions

| # | Question | Customer Answer | Who answered (name + role) | Source (date) | Risk |
|---|----------|-----------------|----------------------------|---------------|------|
| 1 | Deployment preference (Cloud / self-host / hybrid) | [answer or Unknown] | [name + role, or "not asked"] | [transcript date] | 🟢/🟡/🔴 |
| 2 | Data residency / air-gap requirement | | | | |
| 3 | Multi-tenancy concerns for regulated data | | | | |
| 4 | Customer-managed KMS / secrets manager required | | | | |
| 5 | VPC isolation for data plane required | | | | |

**Authority matters as much as recency.** A data engineer saying "we prefer cloud" doesn't override a CISO saying "we require VPC isolation." When answers conflict by stakeholder, the most senior security/compliance voice wins. Flag conflicts explicitly.

---

## Implications by Answer

For each 🔴 answer above, state explicitly:
- **What it breaks:** Cloud Pro [can/cannot] support this requirement because [reason].
- **Path forward:** Requalify to Self-Managed Enterprise / Park until Flex is GA / Disqualify.

For each 🟡 answer (ambiguous or partially answered):
- **What's unclear:** [what we still need to know]
- **Question to ask:** [specific question Gary should ask in next call]

For each 🟢 answer:
- Briefly confirm and move on. Don't pad.

---

## Recommended Next Action
ONE of:
1. **Proceed with Cloud Pro.** All five questions answered 🟢. Move to tech qual / connector feasibility.
2. **Proceed with caveats.** Mostly 🟢, with 1-2 🟡. Specific questions to resolve in next call before scoping POC.
3. **Pause and clarify.** Multiple Unknowns. Run a deployment-model-focused conversation before further investment.
4. **Requalify to Self-Managed Enterprise.** Cloud Pro is not viable due to [specific reason]. Hand to AE for SME motion.
5. **Park.** Customer needs Flex/BYOC which isn't GA. Document the requirement, park the opportunity, revisit when Flex is available.

---

## Discovery Questions for Next Call

If any answer is Unknown or ambiguous, draft 3-5 specific questions Gary can ask to close the gap. Avoid generic phrasing — use SPIN/Sandler tactics. Examples:

- "Walk me through how your security team thinks about data leaving your environment. Where's the line?"
- "If we ran the data plane in your VPC vs. ours, would that change the conversation with your CISO?"
- "Is BYOK a hard requirement for this project, or a 'we'd prefer it'? What would change if it wasn't available?"

---

## Style

- **Bias toward "no" early.** A clean 🔴 verdict in week 1 saves 6 months of wasted cycles. This skill exists to disqualify when needed — not to keep deals alive.
- **No spin.** If Cloud can't meet their requirement, say so. Customers smell hedging.
- **Cite sources.** Every answer in the table must reference a transcript date or note "not asked yet."
- **Flag the cost of not knowing.** If Gary hasn't asked these questions yet, the output should be a list of questions, not a list of guesses.

---

## SE Best Practices Applied

Read `~/.claude/skills/_se-playbook.md` for full framework details.

### Source Freshness Check (Gong Fallback)
Per `_se-playbook.md` ("Source Freshness Check"): if the most-recent local transcript is more than **14 days old**, search Gong for newer calls before answering the 5 deployment questions. Deployment requirements (data residency, KMS, VPC) often surface in the latest call when InfoSec or compliance enters the conversation.
- Pull the **most recent call only** — do not bulk-pull
- Save to `_transcripts/<Customer-Name>-MM.DD.YY.txt` BEFORE using it (per CLAUDE.md)

### This is the single most important Sandler "qualify-out" moment
A 🔴 deployment-model verdict is the cleanest disqualification signal in the funnel. Don't soften it. Per Sandler: mutual qualification means saying "this isn't a fit" is a feature of the process, not a failure of it.

### Apply MEDDPICC Decision Criteria
Deployment preferences are formal Decision Criteria. They belong in biz-qual too. If this skill surfaces a deployment requirement that's not in the customer's biz-qual doc, suggest updating it.

### Use Voss labeling for sensitive answers
If a customer says "we can't use shared cloud," don't argue. Label: "It sounds like there's a specific compliance or security boundary that defines this — can you walk me through it?" Often the boundary is narrower than the blanket "no" suggests.

### Reframe (Challenger) only after disqualifying questions are answered honestly
Don't lead with reframes here. The job is to find the truth, not to convert. If Cloud genuinely doesn't work, no reframe will save it. *After* you confirm what's hard-required vs. preference, then you can reframe preferences.

### Anti-patterns to avoid in this skill
- Filling in answers with optimistic guesses instead of "Unknown — not asked"
- Treating "we'd prefer self-hosted" as a hard 🔴 (it's often a 🟡 — preference, not requirement)
- Skipping this skill and going to tech qual first (the order matters)
- Soft-pedaling a 🔴 verdict to avoid losing the deal — losing 6 months of cycles is worse

---

## After Generating

### Auto-save (default)

Per `_se-playbook.md` "Output Persistence (Auto-Save)" rule, save to:
```
~/airbyte-work/01-customers/<Customer>/outputs/deployment-qual/deployment-qual-<YYYY-MM-DD>.md
```

Append `-v2` etc. for same-day duplicates. User can suppress with `--no-save`.

Inside the document, write dates in long form (`June 11, 2026`) per `_se-playbook.md` "Date format inside documents".

### Source Coverage

Include a Source Coverage section at the top reporting: which transcripts answered which of the 5 questions, who said what (cite speaker), and which questions remain unasked.

### SE Identity

Read `~/airbyte-work/.se-config.yaml` for the `[SE name]` field.

### Then offer to

1. **Add to Notion Overview page** as a deployment fit section
2. **Update biz-qual** with the deployment model row in Decision Criteria
3. **Draft a follow-up email** with the specific discovery questions (invoke `follow-up-email`)
4. **Update memory** — if the verdict is 🔴 (Cloud Pro not viable) or the customer has a hard requirement that meaningfully constrains the deal (BYOK mandate, air-gap, data residency boundary), propose adding a project memory. Don't update for 🟢 verdicts — those are the default state.

---

## Changelog

- **2026-06-18** — Output adopts the shared Output Document Format (_se-playbook.md): At-a-Glance + Jump-to index, H2-per-section, callouts, ==key== emphasis.

- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Hard prerequisite: refuses to run without transcripts (exception: user-provided answers in chat). 5-question table now tracks who answered (name + role) — senior security voice wins on conflicts. Authority matters as much as recency. Memory-write proposal for 🔴 verdicts. References CLAUDE.md 5 questions as source-of-truth.
- **2026-05-27** — Initial scaffold.
