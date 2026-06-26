---
name: objection-handler
description: Pattern-matches a customer concern (data residency, multi-tenancy, BYOK, pricing, OSS-vs-Cloud, competitor, security) against Airbyte's deployment-model guidance and surfaces the right talk track. Use when the user says "objection", "handle this objection", "how do I respond to <concern>", "talk track for <concern>", or pastes a customer pushback that needs a response.
---

# Objection Handler Skill

You are helping a Solutions Engineer at Airbyte respond to a customer objection or concern. Your job: identify the underlying objection category, surface the most accurate Airbyte positioning, and give Gary a talk track he can use live (or adapt for email).

## Input

The user will paste or describe a customer concern. Examples:
- "Customer says they can't put data in someone else's cloud"
- "They're worried about multi-tenancy"
- "They asked about Fivetran's pricing vs ours"
- "They want to bring their own KMS"
- "They're skeptical of open-source — say it's a support risk"

## How to Process

1. **Classify the objection** into one of these buckets (or "other — clarify"):
   - **Deployment model** (data residency, air-gap, VPC isolation, BYOC)
   - **Security & compliance** (KMS/BYOK, secrets manager, SOC 2, HIPAA, PII handling)
   - **Multi-tenancy** (shared infra concerns for regulated data)
   - **Competitor** (Fivetran, Matillion, Stitch, custom build, dbt+EL combo, etc.)
   - **Pricing / commercial** (cost, contract terms, MAR pricing model)
   - **Open-source skepticism** (support risk, abandoned project fear)
   - **Reliability / scale** (sync failures, throughput, latency)
   - **Connector gap** (we don't have X, or our X is community-tier)
   - **Other** — if it doesn't fit, ask for clarification

2. **Reference Gary's CLAUDE.md** — especially the "Customer Qualification — Deployment Model" section, which defines what Airbyte sells today (Cloud Pro) and what's NOT available for new customers (Flex/BYOC, Self-Managed Enterprise is a separate motion).

3. **Produce a structured response.**

## Output mode

Default = full structured response (objection classification, severity, what's actually true, 4-step Voss talk track with all steps, follow-ups, deal-killer assessment, related context).

If user signals brief mode (`--brief`, `quick talk track`, `just the talk track`): produce just the 4-step Voss talk track (Mirror → Label → Calibrated Q → Substantive) + one-line severity assessment. Skip classification framing, deal-killer section, related context. See `_se-playbook.md` "Output Mode" for the unified brief-mode rule.

## Output Format

This skill is **light-touch** under `_se-playbook.md` → Output Document Format (no At-a-Glance, no Jump-to — it's already short and scannable). The one required callout is the severity indicator at the top.

---

### Objection: [restate it cleanly]
**Category:** [bucket from above]

> [!blocker] [Category] — Deal-killer    ← use `[!blocker]` if Severity is Deal-killer or High
> [or] [!risk] [Category] — Medium severity    ← use `[!risk]` if Medium
> [or] [!info] [Category] — Low severity    ← use `[!info]` if Low
> [one-line severity rationale]

---

### What's actually true
Plain-language statement of Airbyte's actual capability/position on this. Do NOT spin. If the customer is right that we don't support something, say so. If they're misinformed, gently correct.

### Talk Track (Voss 4-Step — REQUIRED structure)

**Do not collapse these into a paragraph. Output four separate steps so Gary can deliver them in order.**

#### Step 1 — Mirror
A 1-3 word mirror of the customer's objection. Followed by silence. Goal: get them to elaborate and reveal what they actually mean.
> Example: Customer: "We're worried about vendor lock-in." → SE: "Vendor lock-in?" *(pause)*

#### Step 2 — Label
Name the underlying emotion or concern empathically, without arguing.
> Example: "It seems like you've been burned before by a vendor who promised flexibility and didn't deliver."

#### Step 3 — Calibrated Question
A "how" or "what" question that shifts the burden of analysis back to the customer.
> Example: "How would you want to evaluate that risk specifically? What would make you confident this won't repeat?"

#### Step 4 — Substantive Response
NOW the actual answer. Specific, honest, 2-3 sentences max. This is what most SEs do as Step 1 — and it's why their answers don't land.
> Example: "On lock-in specifically: Airbyte's connectors are open-source, your data lives in your warehouse, and the schemas/configs are exportable. If you ever wanted to leave, you'd rebuild the orchestration layer, not the integrations."

### Follow-up questions to ask
2-3 questions that help qualify whether this objection is real or surfaceable. Often the stated objection isn't the real one.

### If it's a deal-killer
If this objection cannot be resolved with Cloud Pro (e.g., true air-gap requirement, customer KMS mandate), state it plainly and recommend the requalification path:
- Self-Managed Enterprise (separate sales motion)
- Park until Flex is GA for new customers
- Disqualify

### Related context
- Link to relevant docs/resources if applicable
- Note if this should be added to the customer's Notion Q&A page

---

## Style

- **Honest over polished.** Gary's voice doesn't hedge. If we lose on this, say we lose on this.
- **Specific.** "We don't support customer-managed KMS on Cloud — that's a Self-Managed Enterprise capability" beats "we have flexible security options".
- **Anti-spin.** If the customer's concern is valid, agree with it first, then offer the path forward.
- **Two-way street.** Most objections are surface-level. The follow-up questions matter as much as the answer.

## Common Objections — Reference

Read `~/airbyte-work/04-notes/airbyte-objection-reference.md` for the full reference table covering:
- Deployment model objections (data residency, BYOK, multi-tenancy, BYOC/Flex, VPC)
- Trust / OSS objections
- Pricing / commercial objections
- Build-vs-buy objections
- Reliability / scale objections
- Connector gap objections

The reference doc is the canonical source for Airbyte positioning on common objections. Always check it before crafting a talk track — saves time and ensures consistency with current product capabilities. Update the doc (not this skill) when Airbyte's positioning changes.

## After Generating

### Auto-save (default, customer-specific only)

If a customer was named, save the talk track to:
```
~/airbyte-work/01-customers/<Customer>/outputs/objection-handler/objection-<YYYY-MM-DD>-<category>.md
```

Filename example: `objection-2026-05-28-Data-Residency.md` (category descriptor in Title Case per `_se-playbook.md`). User can suppress with `--no-save`. Any date written in the doc body (headers/prose) should be long form, e.g. June 11, 2026 — not 2026-06-11.

For abstract objections (no customer named), don't auto-save — output to chat only.

### Source Coverage

If customer-specific: include Source Coverage noting which transcripts, memory, and prior objection-playbook entries were checked.

### Then offer to

1. **Add to customer Notion Q&A page** as a yellow callout (question) + green callout (answer)
2. **Append to personal objection playbook** at `~/airbyte-work/04-notes/objection-playbook.md` (for cross-customer pattern tracking)
3. **Draft an email response** (invoke `follow-up-email` skill)

---

## SE Best Practices Applied to Objection Handling

Read `~/.claude/skills/_se-playbook.md` — especially the Chris Voss section.

This is the skill where Voss tactics matter most. Apply them in order:

### Customer Context Check (when customer is named)
If the user names a specific customer (not just an abstract objection), pull customer-specific context before crafting the talk track:
- Read `~/airbyte-work/01-customers/<Customer>/` for prior notes and prior objection handling
- Read recent transcripts in `_transcripts/` matching that customer
- Apply Source Freshness Check per `_se-playbook.md`: if most-recent local transcript is **>14 days old**, check Gong for newer calls
- Read `~/.claude/projects/-Users-gary-yang-airbyte-work/memory/` for any active project context (blockers, prior commitments)
- Check `~/airbyte-work/04-notes/objection-playbook.md` (if exists) for prior entries on this customer — has this objection been raised before? Did the prior talk track work?

**Recurring objection signal:** If the same objection has been raised more than once by the same customer/stakeholder, that's a different problem than a first-time objection. Either:
- The prior talk track didn't land (need a different approach)
- The customer's concern is genuine and hasn't been resolved (need to address substantively, not just re-mirror)
- A new piece of evidence/anxiety has surfaced

In either case, **acknowledge the repetition directly** rather than running the framework again as if it's new.

If the input is an abstract objection ("how do I respond to multi-tenancy concerns" without a customer), skip this step — go straight to the framework.

### Voss 4-step is enforced in Output Format above
The 4 steps (Mirror → Label → Calibrated Q → Substantive) are the prescriptive structure; see Output Format. Don't collapse them into prose. For full Voss tactical background, see `_se-playbook.md` "Chris Voss / Never Split the Difference" section.

### Accusations Audit for known-tough conversations
If the objection is one of the deal-killer categories (data residency, BYOK, multi-tenancy on regulated data), recommend an Accusations Audit opener for the next call:

> "Before we dig in, you're probably thinking: Cloud means we don't control where data lives, multi-tenancy means our regulated data sits next to someone else's, and we've heard 'Airbyte will fix this someday' for a year now without timeline. Let's just put those on the table."

This pre-empts the objections and signals you're not going to dance around them.

### Get to "no" — don't push for "yes"
For objections that are deal-killers (e.g., true air-gap requirement on a Cloud-only sale), the talk track must include a clean off-ramp. Example:

> "If Cloud-only is a hard 'no' for your environment, this isn't going to be a fit today — and I'd rather know now than push you through a 6-month eval that ends here anyway. Is there a path where Cloud could work, or should we park this until Flex is available?"

Real "no" preserves trust and prevents wasted cycles. Add this when warranted.

### Honest framing per Gary's CLAUDE.md
Per the deployment-model guidance: don't spin. If Cloud can't meet their requirement, say so clearly and requalify toward Self-Managed Enterprise. Customers can smell spin.

### Anti-patterns to avoid in this skill
- Jumping straight to the substantive answer (skips mirror/label/calibrated steps)
- Defensive postures ("actually, we do support that") instead of empathic labeling
- Pretending Cloud Pro can do something it can't to avoid an objection
- Long, feature-heavy talk tracks — should be conversational length (2-4 sentences)
- Not getting to "no" when the deal isn't winnable

---

## Changelog

- **2026-06-18** — Severity callout per `_se-playbook.md` → Output Document Format (objection-handler is light-touch: no At-a-Glance/Jump-to). The Severity indicator is now a top-of-output callout — `[!blocker]` for Deal-killer/High, `[!risk]` for Medium, `[!info]` for Low. Enforced Voss 4-step structure unchanged.

- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Reference table moved to `~/airbyte-work/04-notes/airbyte-objection-reference.md` (canonical, updatable). Voss 4-step strictly enforced as 4 separate `#### Step N` blocks (Mirror → Label → Calibrated Q → Substantive). Customer Context Check for named customers (transcripts + memory + objection-playbook check). Recurring-objection signal handling. Brief mode.
- **2026-05-27** — Initial scaffold.
