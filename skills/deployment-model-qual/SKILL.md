---
name: deployment-model-qual
description: Qualifies a customer's deployment model fit BEFORE diving into connectors or technical scoping. Routes the customer to one of the two live shapes — Airbyte Cloud (Pro) or Enterprise Flex (hybrid — cloud control plane + customer-hosted data plane) — or flags a genuine no-fit (Self-Managed Enterprise is retired / not currently offered, may return). Use when the user says "deployment qual", "deployment model", "cloud vs self-managed", "cloud vs flex", "is this a cloud deal", or at the start of any new customer engagement before tech qual.
---

# Deployment Model Qualification Skill

You are helping a Solutions Engineer at Airbyte answer the single most important early-deal question: **which deployment shape does this customer need — Cloud or Flex — or is this a genuine no-fit today?**

Product reality (the basis for this skill — see "Product reality these questions assume" below for the full statement, and verify it's current): Airbyte currently offers **two** live deployment shapes. **Cloud (Pro)** — Airbyte runs control + data plane. **Enterprise Flex (hybrid)** — Airbyte-hosted control plane + a **customer-hosted self-managed data plane** in the customer's own VPC (data never leaves their environment; full connector parity); **sellable to new customers with caveats** (region availability, deal-desk/commercial approval, possible limited-availability gating — confirm current terms). **Self-Managed Enterprise** (customer runs everything incl. control plane; BYOK/KMS; true air-gap) is **retired — not currently offered; it may return.** The job is to route to the right shape *early* — discovering air-gap, data-residency, or isolation requirements *late* wastes cycles and damages trust. Flex means most data-isolation requirements are now **winnable**, not walk-away — but BYOK/KMS, full-control-plane-in-VPC, and true air-gap requirements fall outside the Flex boundary and, with SME retired, route to **🔴 park / no fit today**.

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

**Exception:** If the SE explicitly provides answers in chat ("they told me they need air-gap deployment"), proceed with that as the source — but cite it as "user-provided in conversation" not as transcript data, and flag that it should be confirmed in a call.

If you have transcripts but the 5 questions weren't asked, partial-fill the table with ⬜ Unknown for missing questions and recommend asking them on the next call — don't refuse, but flag the gaps loudly.

## Output mode

Default = full deployment-qual doc (5-question table, implications per answer, recommended next action, discovery questions).

If user signals brief mode (`--brief`, `quick deployment check`, `cloud or not`): produce just Verdict (🟢/🟡/🔴) + one-sentence rationale + Recommended Next Action. Skip the 5-question table detail and discovery questions list. See `_se-playbook.md` "Output Mode" for the unified brief-mode rule.

## The Five Qualifying Questions

These five questions are the canonical deployment gate — defined **here** so this skill is self-contained and reviewable (they mirror the workspace `CLAUDE.md` "Customer Qualification — Deployment Model" section, but this SKILL.md is the source of record for the qualification logic). Answer each one for the customer using available evidence:

1. **What is their deployment preference?** Cloud SaaS, hybrid (cloud control plane + own data plane), or fully self-hosted?
2. **Do they have data residency or air-gap requirements?** Distinguish carefully: *"data can't leave our VPC/environment"* → **Flex** (customer-hosted data plane) resolves it. *True air-gap* (no outbound to a cloud control plane at all) → beyond the Flex boundary → **🔴 no currently-offered fit; park** (this was historically SME territory).
3. **Do they have multi-tenancy concerns?** *"Our data can't share compute"* → **Flex** (dedicated customer-run data plane). Broader control-plane/tenancy mandate (control plane must also be customer-run) → beyond the Flex boundary → **🔴 no currently-offered fit; park** (historically SME).
4. **Do they need to bring their own KMS or secrets manager?** Not supported on Cloud **or Flex** (both use Airbyte-managed secrets) — and **not available on any currently-offered shape** (BYOK/KMS was Self-Managed Enterprise, which is retired — may return). A hard BYOK/KMS requirement is therefore a **🔴 no currently-offered fit; park**. This is the sharpest Flex-boundary divider.
5. **Do they require VPC isolation — for the data plane, or the whole platform?** Data-plane-only → **Flex** (runs in their VPC). Entire platform incl. control plane in-VPC → beyond the Flex boundary → **🔴 no currently-offered fit; park** (historically SME).

**Product reality these questions assume (the basis for every verdict — verify it's still current):**
- **Cloud (Pro)** — Airbyte runs control + data plane; managed regions US + EU. Control plane is US-hosted; cursor & primary-key values transit it even for EU data planes (a real compliance nuance — probe it for regulated data).
- **Enterprise Flex (hybrid)** — Airbyte-hosted control plane + **customer-hosted self-managed data plane** in their VPC (Helm on K8s, or Airbox single-node); full 600+ connector parity; per-region data planes possible. **Sellable to new customers with caveats** — region availability, deal-desk/commercial approval, possible limited-availability gating. Does **not** support BYOK/customer KMS, full control-plane-in-VPC, or true air-gap.
- **Self-Managed Enterprise (retired — not currently offered; may return)** — historically the fully self-hosted shape (control plane included), the only shape that supported BYOK/KMS, full VPC isolation of the whole platform, and true air-gap. **This offering is retired as of now and cannot be sold.** Its capabilities (BYOK/KMS, control-plane-in-VPC, air-gap) are therefore **not available on any currently-offered shape** — requirements that need them route to **🔴 park / no fit today**, not to an SME motion. Escalate only if SME is revived.

This product reality changes over time. Flex availability/terms in particular are caveat-gated and can shift — always confirm current Flex availability for the customer's region before committing to it in a verdict. If Self-Managed Enterprise is ever revived, re-run this qualification — verdicts that currently land 🔴 for BYOK/air-gap/control-plane-in-VPC would change. See the product-reality stamp in the Verdict section.

## Output Format

Document structure follows `_se-playbook.md` → Output Document Format (At-a-Glance + Jump-to index, H2-per-section, callouts, `==key==` emphasis).

---

# Deployment Model Qualification: [Customer]
**Date:** [today — long form per `_se-playbook.md`, e.g. June 11, 2026, NOT 2026-06-11 or MM.DD.YY] · **Sources:** [transcripts, notes, Notion pages used]

### At a Glance
*Decision card — lead with the verdict (see `_se-playbook.md` → Decision-First Layout).*
- **Verdict:** 🟢 Cloud Pro viable / 🟦 Flex viable (data-plane isolation) / 🔴 genuine blocker (park / no fit today) — [3–6 word headline]
- **Hard constraint:** [the single requirement that drives the verdict, or "none — no hard blockers surfaced"]
- **Recommended motion:** [Proceed with Cloud Pro / Position Flex (confirm availability) / Pause & clarify / Park-or-disqualify]
- **Next gate:** [what resolves the open constraint — e.g. "confirm KMS requirement with CISO"]
- **Source confidence:** [one line — N transcripts; which of the 5 questions are actually answered vs. assumed]

**Jump to:** [At a Glance](#at-a-glance) · [Source Coverage](#source-coverage) · [Verdict](#verdict) · [The Five Questions](#the-five-questions) · [Implications by Answer](#implications-by-answer) · [Recommended Next Action](#recommended-next-action) · [Discovery Questions for Next Call](#discovery-questions-for-next-call)

---

## Verdict

This is a **3-way** verdict — route to the right *live* shape (Cloud or Flex), or flag a genuine no-fit. Don't just gate Cloud in/out. Render as a callout, picking the type by status: `[!verdict]` if 🟢 Cloud viable, `[!info]` if 🟦 Flex viable, `[!blocker]` if 🔴 genuine blocker.

- 🟢 **Cloud Pro viable** — no data-isolation / KMS / control-plane requirement. Default happy path.
- 🟦 **Flex viable** — data must stay in the customer's VPC (data-plane isolation) but they don't need to control the control plane or bring their own KMS. Cloud control plane + customer-hosted data plane. *Confirm current Flex availability/terms for their region before committing.*
- 🔴 **Genuine blocker (park / no fit today)** — a requirement no currently-offered shape meets: BYOK/customer-managed KMS, full control-plane-in-VPC, true air-gap, or any capability gap Flex can't cover. Park or disqualify. (Self-Managed Enterprise, which historically covered BYOK/air-gap/control-plane-in-VPC, is not currently offered; it may return — escalate only if that changes.)

```markdown
> [!info] 🟦 Flex viable — data-plane isolation in customer VPC
> Customer requires all data to stay in their AWS VPC (confirmed by CISO 06.10), but has no BYOK mandate and is fine with an Airbyte-hosted control plane. Flex fits: customer-hosted data plane, cloud control plane. NEXT: confirm Flex availability + terms for their region with AE/deal-desk before scoping.
```
```markdown
> [!blocker] 🔴 Genuine blocker — customer-managed KMS mandate (park / no fit today)
> Customer requires customer-managed KMS (hard requirement, confirmed by CISO 06.10). Neither Cloud nor Flex supports BYOK. No currently-offered shape meets this — park or disqualify. (Self-Managed Enterprise, which historically covered this, is not currently offered; it may return — escalate only if that changes.)
```

- **Status:** 🟢 Cloud viable / 🟦 Flex viable / 🔴 genuine blocker (park / no fit today)
- **One-sentence rationale:** [punchy verdict]

### Product-reality stamp (verdict can go stale)

State the product-capability basis and its date with the verdict: "Verdict assumes Flex is sellable-with-caveats to new customers as of [reference date], Cloud managed regions = [list], and that Self-Managed Enterprise is retired as of [reference date] — so BYOK/KMS, full control-plane-in-VPC, and true air-gap are not available on any currently-offered shape. If any changed (esp. an SME revival), re-run — the verdict can flip." Two stale-fact guards:
- If a **🟦 Flex** verdict depends on Flex being available for the customer's region/segment and you can't confirm current terms, mark it **🟦 Flex viable — availability unconfirmed** and make "confirm Flex terms with deal-desk" the next gate. Don't promise Flex on a stale availability assumption.
- If a customer requirement hinges on any capability you can't verify as current, issue a **Provisional** verdict ("verify current product state before acting") rather than a hard route. A 🔴 park verdict is expensive to get wrong on a stale fact — if SME (or another shape covering BYOK/air-gap/control-plane-in-VPC) has been revived, the deal may be winnable — and so is a 🟦 that promises Flex before terms are confirmed.

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

For each answer that rules out Cloud Pro, state explicitly:
- **What it breaks:** Cloud Pro cannot support this requirement because [reason].
- **Which shape fixes it:** **Flex** (data-plane isolation in customer VPC — data residency, VPC isolation, "our data can't share compute") — or **no currently-offered fit** (BYOK/KMS, full control-plane-in-VPC control, true air-gap fall outside the Flex boundary; historically SME, now retired). Name which, and why.
- **Path forward:** Position Flex (confirm availability/terms) / 🔴 Park or disqualify — no currently-offered shape meets this (note what would need to be true, e.g. an SME revival).

For each 🟡 answer (ambiguous or partially answered):
- **What's unclear:** [what we still need to know]
- **Question to ask:** [specific question the SE should ask in next call]

For each 🟢 answer:
- Briefly confirm and move on. Don't pad.

---

## Recommended Next Action
ONE of:
1. **Proceed with Cloud Pro.** All five questions answered 🟢. Move to tech qual / connector feasibility.
2. **Position Enterprise Flex.** Data-plane isolation is required (data must stay in customer VPC) but no BYOK/control-plane mandate. Confirm current Flex availability + terms for their region with the AE/deal-desk, then proceed to tech qual scoped for a customer-hosted data plane.
3. **Park / no fit today.** BYOK/KMS, full control-plane-in-VPC control, or true air-gap makes Cloud and Flex non-viable. No currently-offered shape meets this — park or disqualify. (Self-Managed Enterprise, which historically covered this, is not currently offered; it may return — escalate only if that changes.) Document the requirement and the fact that it would need an SME revival (or equivalent) to become winnable.
4. **Pause and clarify.** Multiple Unknowns, or the Flex-boundary divider (KMS? control-plane vs data-plane isolation?) is unresolved. Run a deployment-model-focused conversation before further investment.
5. **Park / escalate on availability.** Flex is required but unavailable for their region/segment. Document the requirement, escalate to leadership, revisit when terms change.

---

## Discovery Questions for Next Call

If any answer is Unknown or ambiguous, draft 3-5 specific questions the SE can ask to close the gap. Avoid generic phrasing — use SPIN/Sandler tactics. Examples:

- "Walk me through how your security team thinks about data leaving your environment. Where's the line?"
- "If we ran the data plane in your VPC vs. ours, would that change the conversation with your CISO?"
- "Is BYOK a hard requirement for this project, or a 'we'd prefer it'? What would change if it wasn't available?"

---

## Style

- **Route early, don't just gate.** The job is to land on the right shape (Cloud / Flex) — or a clean 🔴 no-fit — in week 1, not 6 months in. A clean 🔴 park verdict still saves wasted cycles — but with Flex in the picture, many "data can't leave our VPC" requirements are now a 🟦 *route to Flex*, not a walk-away. Don't reflexively disqualify what Flex can win.
- **But don't over-rescue with Flex either.** BYOK/KMS and full-control-plane-in-VPC requirements fall outside the Flex boundary — positioning Flex there just moves the dead-end later. With SME retired, those are a 🔴 no-fit today, not a shape you can sell. Be honest about the Flex boundary.
- **No spin.** If no currently-offered shape meets their requirement, say so plainly — including that SME (which used to) is retired. Customers smell hedging.
- **Cite sources.** Every answer in the table must reference a transcript date or note "not asked yet."
- **Flag the cost of not knowing.** If the SE hasn't asked these questions yet, the output should be a list of questions, not a list of guesses.

---

## SE Best Practices Applied

Read `~/.claude/skills/_se-playbook.md` for full framework details.

### Source Freshness Check (Gong Fallback)
Per `_se-playbook.md` ("Source Freshness Check"): if the most-recent local transcript is more than **14 days old**, search Gong for newer calls before answering the 5 deployment questions. Deployment requirements (data residency, KMS, VPC) often surface in the latest call when InfoSec or compliance enters the conversation.
- Pull the **most recent call only** — do not bulk-pull
- Save to `_transcripts/<Customer-Name>-MM.DD.YY.txt` BEFORE using it (per CLAUDE.md)

### This is the single most important Sandler "qualify + route" moment
A deployment-model verdict is the cleanest routing signal in the funnel. A 🔴 blocker verdict is a legitimate qualify-out — don't soften it; per Sandler, "this isn't a fit for us today" is a feature of mutual qualification, not a failure. But routing is now three-way, not in/out: a 🟦 Flex verdict *keeps a winnable deal alive* on the right motion rather than parking it. The discipline is to name the shape honestly — including saying "no currently-offered shape fits this today (the shape that used to, SME, is retired)" when that's true.

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
- Routing a BYOK/air-gap/control-plane-in-VPC requirement to "the SME motion" — SME is retired; that's a 🔴 park / no fit today, not a sellable hand-off

---

## After Generating

### Auto-save (default)

Per `_se-playbook.md` "Output Persistence (Auto-Save)" rule, save to:
```
{customers_dir}/<Customer>/outputs/deployment-qual/deployment-qual-<YYYY-MM-DD>.md
```

Append `-v2` etc. for same-day duplicates. User can suppress with `--no-save`.

### Source Coverage

Include a Source Coverage section at the top reporting: which transcripts answered which of the 5 questions, who said what (cite speaker), and which questions remain unasked.

### SE Identity

Read `config_file` (per playbook → Workspace Paths) for the `[SE name]` field.

### Then offer to

1. **Add to Notion Overview page** as a deployment fit section
2. **Update biz-qual** with the deployment model row in Decision Criteria
3. **Draft a follow-up email** with the specific discovery questions (invoke `follow-up-email`)
4. **Update memory** — if the verdict is 🟦 Flex or 🔴 blocker (park / no fit today), or the customer has a hard requirement that meaningfully constrains the deal (BYOK mandate, air-gap, data-residency/VPC-isolation boundary, Flex-availability dependency), propose adding a project memory. Don't update for 🟢 Cloud verdicts — those are the default state.

---

## Changelog

- **2026-07-10** — **Self-Managed Enterprise retired.** SME is no longer sellable, so the verdict is now **3-way**, not 4-way: 🟢 Cloud / 🟦 Flex / 🔴 genuine blocker (park / no fit today). Dropped the 🟠 SME lane as a routable verdict. Requirements only SME ever met — BYOK/customer-managed KMS, full control-plane-in-VPC control, true air-gap — now route to **🔴 park / no fit today** ("No currently-offered shape meets this — park or disqualify") instead of "requalify to SME." The 5 questions keep the Flex split but their former-SME branch now reads "beyond the Flex boundary → 🔴 park" while still explaining *why* it's a no-fit and what would change if SME returns. BYOK reframed from "SME-only" to "not available on any currently-offered shape (was SME — retired, may return)." Product-reality stamp now records SME as retired-as-of-now so a future revival re-runs the verdict. SME is framed throughout as **retired / not currently offered (may return)** — kept as a real capability fact, but removed as a sales motion.
- **2026-07-10** — **Flex is back.** Verdict moved from 2-way (Cloud in/out) to **4-way** routing: 🟢 Cloud / 🟦 Flex (customer-hosted data plane in their VPC, cloud control plane — sellable to new customers *with caveats*: region/deal-desk/limited-availability, confirm current terms) / 🟠 SME (BYOK/KMS, full control-plane control, true air-gap) / 🔴 genuine blocker. Reframed the 5 questions to split *data-plane isolation* (→ Flex) from *full-platform control / BYOK* (→ SME). Product-reality stamp now guards a stale **Flex-availability** assumption (mark "availability unconfirmed" rather than promise Flex). Added the control-plane-in-US / cursor-&-PK compliance probe. Qualify-out reframed to qualify-*and-route*.
- **2026-07-09** — Added product-reality as-of stamp; capability-dependent verdicts render 🟡 Provisional when current product state is unverifiable. Inlined the 5 qualifying questions + the product reality they assume (Cloud Pro only; Flex not GA; SME separate motion) so the skill is self-contained — this SKILL.md is now the source of record, mirroring but no longer dependent on the workspace CLAUDE.md.
- **2026-06-18** — Output adopts the shared Output Document Format (_se-playbook.md): At-a-Glance + Jump-to index, H2-per-section, callouts, ==key== emphasis.

- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Hard prerequisite: refuses to run without transcripts (exception: user-provided answers in chat). 5-question table now tracks who answered (name + role) — senior security voice wins on conflicts. Authority matters as much as recency. Memory-write proposal for 🔴 verdicts. References CLAUDE.md 5 questions as source-of-truth.
- **2026-05-27** — Initial scaffold.
