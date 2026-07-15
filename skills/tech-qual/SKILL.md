---
name: tech-qual
description: Generates a technical qualification document for a prospect evaluating Airbyte. Requires at least one transcript with technical discovery (refuses otherwise). Reads prior deployment-qual, biz-qual, and connector-feasibility outputs to avoid re-deriving; defers deployment-model decision to deployment-model-qual. Covers data sources/destinations, volume and latency requirements, infrastructure preferences (with verification caveat on compliance certifications), and integration complexity. Use when the user says "tech qual", "technical qualification", "assess their stack", or wants to understand if Airbyte is technically a fit.
---

# Technical Qualification Skill

You are helping a Solutions Engineer at Airbyte assess whether a prospect is a strong technical fit and identify any implementation risks or gaps.

## Input

The user will provide one or more of:
- Company name and deal context
- Notes from technical discovery calls
- Information about their current data stack

## Hard Prerequisite: Call Data Required

**This skill requires at least one customer transcript containing technical discovery.** Technical fit assessment is synthesis of stated requirements — without customer voice on their stack, volume, security, and integration needs, the output is hypothesis.

If zero transcripts exist (local + Gong checked): **REFUSE TO RUN.** Output:
> "Cannot generate tech-qual for [Customer] — no technical discovery in source material. Recommend: run `prep-call` to plan a technical discovery call, then re-run after transcript is saved."

## Before generating: read prior outputs

Tech qual builds on earlier work. Before generating, check the customer's `outputs/` folder (`{customers_dir}/<Customer>/outputs/<skill>/`, per playbook → Workspace Paths) for and read:
- **`outputs/deployment-qual/deployment-qual-*.md`** — if exists, the deployment model is already qualified. Reference its verdict in your Deployment Model section; don't re-derive. If it doesn't exist and the customer has non-trivial requirements, **suggest running `deployment-model-qual` first**.
- **`outputs/biz-qual/biz-qual-*.md`** — pulls in MEDDPICC Decision Criteria, which directly inform what to evaluate technically
- **`outputs/connector-feasibility/connector-feasibility-*.md`** — already-done connector coverage analysis; reference it instead of re-doing
- **Prior `outputs/tech-qual/tech-qual-*.md`** — if one exists, compare and note movement

Cite the source documents inline (filename + date) when pulling in prior conclusions.

## Output mode

Default = full tech-qual doc (all 7 areas with detailed assessment).

If user signals brief mode (`--brief`, `quick tech qual`, `tech summary`): produce just the Technical Fit Summary scorecard table + top 3 technical risks + recommended next actions. Skip detailed area sections, team readiness, and questions-still-needed. See `_se-playbook.md` "Output Mode" for the unified brief-mode rule.

## Output Format

Document structure follows `_se-playbook.md` → Shared Skill Boilerplate → Output format reference.

---

# Technical Qualification: [Company Name]
**Date:** [today's date, long form] · **SE owner:** [SE name]

### At a Glance
*Decision card — lead with the judgment (see `_se-playbook.md` → Decision-First Layout).*
- **Technical fit:** 🟢 Strong / 🟡 Moderate / 🔴 Weak / ⬜ Insufficient info — [3–6 word headline]
- **Recommended motion:** [the one next move — e.g. "Proceed to POC scoping" / "Validate destination first"]
- **Primary risk:** [the single biggest technical thing that could blow it up — one line]
- **Confidence:** [low / medium / medium-high / high — and what it's pending on]
- **Next gate:** [the concrete checkpoint that resolves the primary risk — e.g. "live Postgres connection in workshop"]
- **Scope:** [sources → destination] · **# connectors:** ==[N]== · **Volume:** ==[e.g., 50M rows/day]== · **Latency:** [e.g., 15 min]
- **Compliance:** [omit if none in scope; else "N/A" or "compliance claims pending verification" if any cert/security line is unverified — see Security & Compliance]
- **Source confidence:** [one line — N transcripts + SFDC + qual docs; "see Source Coverage"]

**Jump to:** [At a Glance](#at-a-glance) · [Technical Fit Summary](#technical-fit-summary) · [Technical Requirements & Scope](#technical-requirements--scope) · [Data Sources & Destinations](#data-sources--destinations) · [Data Volume & Scale](#data-volume--scale) · [Deployment Model](#deployment-model) · [Security & Compliance](#security--compliance) · [Current Stack & Integration Context](#current-stack--integration-context) · [Team & Implementation Readiness](#team--implementation-readiness) · [Technical Risks & Open Items](#technical-risks--open-items) · [Questions Still Needed](#questions-still-needed) · [Recommended Next Actions](#recommended-next-actions) · [Source Coverage](#source-coverage)

*(Section order is verdict-first, then architecture (sources → volume → deployment → security), then risks and actions. Source Coverage is the last content section — see `_se-playbook.md`.)*

## Technical Fit Summary
**Overall fit:** 🟢 Strong / 🟡 Moderate / 🔴 Weak / ⬜ Insufficient info

**Pre-save fit gate:** Do not label the overall technical fit as `🟢 Strong` when any scorecard row is `⬜ Unknown` or `🔴 Weak` unless that row is explicitly a **solvable implementation risk** (not a critical blocker). If a critical requirement is unverified, cap the overall fit at `🟡 Moderate` or lower and make the recommended motion conditional on resolving the open item. An "Insufficient info" call is better than an inflated "Strong."

*Scorecard — the "Why it matters" column states the consequence, not a restatement of the area.*

| Area | Status | Why it matters |
|------|--------|----------------|
| Data sources | 🟢 / 🟡 / 🔴 / ⬜ | [consequence — e.g. "all standard connectors; no build risk"] |
| Destinations | 🟢 / 🟡 / 🔴 / ⬜ | [e.g. "Postgres supported but live path unproven — gating"] |
| Volume & scale | 🟢 / 🟡 / 🔴 / ⬜ | [e.g. "well within range; ~1 data worker"] |
| Deployment model | 🟢 / 🟡 / 🔴 / ⬜ | [e.g. "Cloud accepted; on-prem reachability open"] |
| Security & compliance | 🟢 / 🟡 / 🔴 / ⬜ | [e.g. "no regulated PII; trust-center sufficient"] |
| Integration complexity | 🟢 / 🟡 / 🔴 / ⬜ | [e.g. "1 custom CDK connector needed"] |
| Team capability | 🟢 / 🟡 / 🔴 / ⬜ | [e.g. "capable integration owners; can self-serve"] |

## Technical Requirements & Scope
*The one-place scope snapshot — consolidates the key technical asks across all calls/docs so the SE doesn't have to reassemble them from per-call summaries. The detailed sections below expand each line. This is the canonical scope; if a later call revises a number, update it HERE.*

| Dimension | Requirement |
|-----------|-------------|
| **Sources** | [systems they're moving data FROM — e.g., Salesforce, SAP Hana, Coupa CLM] |
| **Destinations** | [where it lands — e.g., Postgres CDR, Snowflake] |
| **Scale / volume** | ==[rows-or-events/day, # connections]== |
| **Sync frequency / latency** | [real-time / daily / monthly · latency tolerance] |
| **Deployment method** | [Cloud Pro / Flex-hybrid — verdict from deployment-qual; note any on-prem/residency/VPC constraint] |
| **Auth pattern(s)** | [e.g., OAuth2, API key, Entra/Okta SSO — the pattern repeated across sources] |
| **Pricing & sizing** | [data workers needed (e.g., ==1 + burst==), enterprise-connector add-ons (Hana/Oracle/NetSuite @ +$/yr), capacity-vs-volume preference] |

**Pricing & sizing detail:**
- **Estimated data workers:** [N — note if customer/SE believes <1 or needs burst capacity]
- **Enterprise connectors in scope:** [list — each is a separate add-on cost]
- **Pricing-model fit signal:** [e.g., "strongly prefers capacity-based/fixed-cost — easier board story" — quote if stated]
- **Note:** Commercial ownership stays with `biz-qual` (economic buyer, budget, paper process). This line is the *technical sizing* that informs the quote — record what was stated, don't negotiate it here.

## Data Sources & Destinations

**Sources (what data are they moving FROM):**
| Source | Connector exists? | Notes |
|--------|-------------------|-------|
| [e.g., Salesforce] | ✅ / ⚠️ custom needed / ❓ unknown | |

**Destinations (where does data need to land):**
| Destination | Connector exists? | Notes |
|-------------|-------------------|-------|
| [e.g., Snowflake] | ✅ / ⚠️ custom needed / ❓ unknown | |

**Connector gaps:**
- [ ] [Any sources or destinations not covered by Airbyte's catalog]

## Data Volume & Scale
- **Estimated rows/events per day:** 
- **Number of pipelines / connections needed:** 
- **Sync frequency required:** [Real-time / hourly / daily / weekly]
- **Data retention / history requirements:** 
- **Peak load characteristics:** 

**Assessment:**
- [Notes on whether this is within Airbyte's supported scale range]
- [Flag any volume or latency requirements that could be a fit risk]

## Deployment Model
*If `outputs/deployment-qual/deployment-qual-*.md` exists for this customer, summarize its verdict here and reference the doc — don't re-derive. Use this section for technical implications, not for re-qualifying.*

- **Verdict (from deployment-qual):** [🟢 Cloud Pro viable / 🟡 with caveats / 🔴 not viable]
- **Preferred deployment:** [Airbyte Cloud / Enterprise Flex / Open Source]
- **Cloud provider:** [AWS / GCP / Azure / Multi-cloud]
- **Region requirements:** [US / EU / APAC / specific region]
- **Network/VPC requirements:** [VPC peering, private link, etc.]
- **On-prem or air-gapped requirement:** [Yes / No]

**Assessment:**
- [Notes on deployment fit and any constraints]

## Security & Compliance
- **Compliance requirements:** [SOC 2 / HIPAA / GDPR / PCI / ISO 27001 / other — flag whether real (handling regulated data) vs. aspirational]
- **Data residency requirements:** 
- **SSO/SAML required:** [Yes / No]
- **RBAC requirements:** 
- **Audit logging required:** [Yes / No]
- **Encryption requirements:** [at rest / in transit / customer-managed keys — note that customer-managed KMS is not available on any currently-offered shape (was Self-Managed Enterprise — retired, may return); not on Cloud or Flex]

> [!risk] Compliance self-check (mandatory before save)
> Every certification/security claim must be either:
> (a) cited to an authoritative source with a date, or
> (b) written as "believed — SE to verify with [team] before customer confirmation."
> Never state a certification (SOC 2, HIPAA, region availability, etc.) as fact from memory. Separate `[customer requires]` from `[Airbyte supports — verified]` from `[Airbyte supports — unverified]`. If any compliance line is unverified, note it in the At-a-Glance ("compliance claims pending verification").
>
> **Ground each security/compliance requirement in a NAMED entitlement (DS2), not memory.** Per `_se-playbook.md` → "Product & Connector Reference Data" (DS2 = `reference_data.repos.airbyte_platform`), the source of truth is the real `airbyte-commons-entitlements/src/main/kotlin/io/airbyte/commons/entitlements/models/EntitlementDefinitions.kt` — reading it is how you verify a capability exists. Map each requirement to its `feature-*` entitlement when one exists:
> - SSO / identity federation → `feature-sso`
> - RBAC / roles / groups → `feature-rbac-roles`, `feature-groups`
> - In-pipeline row filtering / hashing / field encryption → `feature-mappers`
> - Network isolation / PrivateLink → `feature-privatelink`
> - Data residency / customer-VPC data plane → `feature-self-managed-regions`
> - Sub-hourly / 15-min sync latency → `feature-15-minute-sync-frequency`
>
> OR — if the requirement is **customer-managed KMS / BYOK, full control-plane-in-VPC, or true air-gap** — flag it as "**no entitlement on any currently-offered shape → not supported today** (was Self-Managed Enterprise, retired / may return)." This is a no-fit boundary, not a capability to position; both Cloud and Flex use Airbyte-managed secrets. Route it to deployment-model-qual for the verdict.
>
> Per `_se-playbook.md` graceful-degradation: if the `airbyte-platform` checkout isn't available, mark the claim "believed — verify with [team]" and cap confidence — don't assert an entitlement you couldn't read.
>
> This is a save gate, not a suggestion: if a compliance row is neither cited (or entitlement-grounded) nor marked "verify with [team]," fix it before the doc is written.

> [!info] Keep entitlement feature-ids INTERNAL
> The `feature-*` ids are internal reasoning only (guardrail per `_se-playbook.md`). Customer-facing prose says "available on Enterprise Flex" — never "gated behind `feature-privatelink`." Use the entitlement to ground your own verification; translate to plan/edition language for the customer.

## Current Stack & Integration Context
- **Current ETL/ELT tools:** [Fivetran / Stitch / custom / dbt / etc.]
- **Orchestration layer:** [Airflow / Prefect / dbt Cloud / none]
- **Data warehouse/lake:** 
- **Transformation layer:** 
- **Monitoring/observability:** 
- **Migration complexity:** [Lift-and-shift / re-architecture needed / greenfield]

**Assessment:**
- [Notes on integration with existing tooling]
- [Migration risks or dependencies]

## Team & Implementation Readiness
- **Data engineering team size:** 
- **Technical champion:** [name / role]
- **Internal capacity for implementation:** [High / Medium / Low]
- **Implementation timeline expectation:** 
- **Need for professional services:** [Yes / No / Possibly]

## Technical Risks & Open Items
Classify every item in this table into one of four buckets so the SE knows whether the deal is qualified, blocked, or merely unfinished:

- **Confirmed fit** — validated against evidence; no residual uncertainty
- **Solvable implementation risk** — known gap with a feasible mitigation; does not block qualification
- **Critical blocker** — requirement Airbyte cannot meet or is unverified; prevents a `🟢 Strong` fit
- **Open validation item** — evidence is missing; must be resolved before the fit call is final

| Risk | Severity | Classification | Why it matters / mitigation |
|------|----------|----------------|------------------------------|
| [e.g., Custom connector needed for key source] | High | Solvable implementation risk | [consequence + how to de-risk] |
| [e.g., Sub-minute latency requirement] | Medium | |
| [e.g., Air-gapped deployment not yet validated] | High | |

> [!blocker] [Only if a High-severity blocker exists — e.g., air-gapped deployment not validated, or deployment model unresolved]
> [State the hard blocker and what must be true to clear it. Omit this callout if no High-severity blocker.]

## Questions Still Needed
*Decision table (per `_se-playbook.md`). Render `TBD` for Owner/Needed-By when the source doesn't state them — never invent a name or date.*

| Open Question | Owner | Needed By | Why it matters | Status |
|---------------|-------|-----------|----------------|--------|
| [unanswered technical question] | [name or **TBD**] | [gate/date or **TBD**] | [decision it unblocks] | Open |

## Recommended Next Actions
*Action table — each action has a goal, a definition of "done," and a fallback.*

| # | Next Action | Goal | Success criteria | Fallback | Owner |
|---|-------------|------|------------------|----------|-------|
| 1 | [e.g., Validate connector coverage for source X] | [what it proves] | [what "done" looks like] | [plan B] | [name or **TBD**] |
| 2 | [e.g., Walk through deployment architecture] | | | | |

## Source Coverage
*Audit trail — last content section (progressive disclosure per `_se-playbook.md`).* [Transcripts read with line counts, prior qual docs consulted, MCP queries run, certification claims marked "needs verification" — see After Generating.]

**DS2 product-truth (per `_se-playbook.md` → fail-loud):** report whether the `airbyte-platform` checkout (`reference_data.repos.airbyte_platform`) was used to verify entitlement claims in Security & Compliance — with the checkout date — e.g. "entitlement claims grounded in `EntitlementDefinitions.kt`, airbyte-platform checkout [date]"; or "airbyte-platform not available — compliance/entitlement claims reasoned from memory and marked 'verify with [team]', confidence capped." Never assert an entitlement you couldn't verify against the file.

---

## Style (tech-qual skill guidance — not part of output template)

- Specific over vague — replace "TBD" with explicit open questions naming who/what is needed
- Flag blockers clearly with High severity
- Extract technical signals from transcripts; cite source (transcript date + speaker) for each material claim
- Use ✅ ⚠️ ❓ for connector status, 🟢 🟡 🔴 ⬜ for fit assessments
- Don't assert Airbyte certification status without verifying — mark as "needs verification" if mission-critical

---

## SE Best Practices Applied to Technical Qualification

Read `~/.claude/skills/_se-playbook.md` for full framework details.

### Salesforce Enrichment (active opp — technical fields the AE captured)
Per `_se-playbook.md` "Salesforce Enrichment." Pull the **active opp** technical fields:
- `Most_important_sources__c`, `Most_Important_Destinations__c` → sources/destinations
- `No_of_Databases__c`, `No_of_API_Sources__c`, `Monthly_Data_Volume__c` → volume & scale
- `Refresh_Frequency__c` → sync/latency
- `Required_features_functionality__c` → decision criteria
- `Use_case_description__c`, `Airbyte_Use_Case__c` → use case
- `Region__c`, `Billing_Country__c` → data residency (feeds deployment-model)
- `Support_SLA__c`, `Contracted_Data_Workers__c` → enterprise needs / sizing
- `POV_Created__c`, `POV_Completed__c` → POC status

**How to use it:** SFDC has the AE's technical capture. Use it to pre-fill the tech-qual scaffold, then validate + deepen against the transcripts. **Gaps between the SFDC tech fields and the transcript detail = exactly what to confirm on the next call** (flag these). If SFDC unavailable, skip per graceful-degradation.

### Source Freshness Check (Gong Fallback)
Per `_se-playbook.md` ("Source Freshness Check"): if the most-recent local transcript is more than **14 days old**, search Gong for newer calls before scoring technical fit. Technical requirements (volume, latency, connectors, deployment) often firm up in the most recent call.
- Pull the **most recent call only** — do not bulk-pull
- Save to `_transcripts/<Customer-Name>-MM.DD.YY.txt` BEFORE using it (per CLAUDE.md)

### Deployment model FIRST (per the deployment-model guidance)
Before any other section, confirm the shape. Two live shapes today: **Cloud Pro** and **Enterprise Flex** (hybrid — cloud control plane + customer-hosted data plane). Data-residency and VPC-isolation requirements often route to **Flex** rather than killing the deal. But customer-managed KMS / BYOK, full control-plane-in-VPC, or a true air-gap with no Flex path is a **park / no-fit** — Self-Managed Enterprise, which covered these, is retired / not currently offered (may return) — **not** a requalify-to-SME. Don't fill out the rest of this doc if deployment model isn't sorted. Flag an unresolved model as a 🔴 blocker.

### Apply SPIN to technical questions
Volume, latency, and connector questions should not be flat "what's your volume?" Use the Problem → Implication chain:
- **Situation:** "How are you moving data from Salesforce to Snowflake today?"
- **Problem:** "What breaks most often?"
- **Implication:** "When that breaks, what's the downstream impact — who notices first, what reports go stale, what decisions get delayed?"
- **Need-Payoff:** "If schema changes were handled automatically with no engineering involvement, what would your team work on instead?"

Add an Implication-style follow-up under each technical section.

### Surface paper-process landmines early (MEDDPICC P)
Technical qual is when security/legal landmines surface. Don't wait. Add specific questions:
- "Does InfoSec require a security questionnaire? When can it start?"
- "Will legal require a redline cycle on the DPA? What's the typical timeline?"
- "Is there a vendor risk management process we should be in flight on?"

Map answers to a Paper Process section. Vague answers = high risk.

### Reframe connector-count comparisons (Challenger)
If the customer is comparing connector counts to Fivetran/Matillion, that's a Reframe opportunity, not a feature debate. Add a `### Reframe Opportunities` callout: it's not count, it's coverage of *their* stack + how the long tail gets built (manifest-only + custom CDK). Flag for the SE to use in next call.

### "Build it ourselves" is the competition (MEDDPICC C)
Always include "build internally" as a competitor in tech qual — it's often the strongest alternative. Surface their actual current-state cost: engineer hours/week, on-call burden, schema-drift handling, opportunity cost. Without this number, you can't win the TCO conversation.

### Anti-patterns to avoid in this skill
- Marking the deal `🟢 Strong` while a critical requirement is still `⬜ Unknown` or `🔴 Weak`
- Tech qual filled out before deployment model is confirmed
- Connector list without coverage assessment ("we have Salesforce" ≠ "we have the auth + objects + sync mode they need")
- Volume/latency answers taken at face value without asking what happens when they're missed
- Listing compliance requirements without checking which are real vs. aspirational ("HIPAA" — are they actually handling PHI in this pipeline?)

---

## After Generating

### Auto-save path
Per `_se-playbook.md` → Shared Skill Boilerplate → After Generating (saving skills), save to:
```
{customers_dir}/<Customer>/outputs/tech-qual/tech-qual-<YYYY-MM-DD>-<Descriptor>.md
```

### Source Coverage
Include a Source Coverage section at the top reporting transcripts read (with line counts), prior qual docs consulted, MCP queries run (`list_connectors` etc.), and Airbyte certification claims marked as "needs verification."

### SE Identity
Read `config_file` (per playbook → Workspace Paths) for the `[SE name]` field.

---

## Changelog

- **2026-07-14** — **Phase 3 guardrails: no `🟢 Strong` with critical unknowns; four-bucket classification.** Added a pre-save fit gate to the Technical Fit Summary: an unverified or weak critical requirement caps the overall fit at `🟡 Moderate` or lower. Added a four-bucket classification (Confirmed fit / Solvable implementation risk / Critical blocker / Open validation item) to the Technical Risks & Open Items table so a definitive qualification cannot be built on a missing critical input.
- **2026-07-10** — **DS2 entitlement grounding of the compliance self-check.** The Security & Compliance self-check now maps each customer security/compliance requirement to a NAMED `feature-*` entitlement from the real `EntitlementDefinitions.kt` (DS2 = `reference_data.repos.airbyte_platform`, per `_se-playbook.md` → "Product & Connector Reference Data") rather than asserting capabilities from memory: SSO → `feature-sso`, RBAC/groups → `feature-rbac-roles`/`feature-groups`, in-pipeline row filtering/hashing/encryption → `feature-mappers`, PrivateLink/network isolation → `feature-privatelink`, data residency → `feature-self-managed-regions`, sub-hourly latency → `feature-15-minute-sync-frequency`. Made the customer-managed KMS/BYOK / full-control-plane-in-VPC / air-gap boundary concrete: no entitlement on any currently-offered shape → not supported today (was SME, retired/may return), a no-fit rather than a positionable capability. Feature-ids kept internal per guardrail (customer-facing = "available on Enterprise Flex," never the feature-id). Source Coverage now reports whether the airbyte-platform checkout was used (+ date) or unavailable, with capped confidence and "verify with [team]" degradation. Additive — refusal rule, transcript prerequisite, and section order unchanged.
- **2026-07-10** — SME-retirement phrasing fixes. Deployment method row now Cloud Pro / Flex-hybrid (dropped SME as an option); Preferred deployment now Cloud / Enterprise Flex / Open Source; customer-managed KMS reframed as not available on any currently-offered shape (was SME — retired, may return); deployment-first block now names the two live shapes (Cloud / Flex-hybrid), routes residency/VPC to Flex, and treats BYOK / full-VPC / air-gap-with-no-Flex-path as a park/no-fit rather than requalify-to-SME. Kept SME as a retired-may-return note, not deleted.
- **2026-07-10** — Repointed hardcoded `~/airbyte-work/` paths to the workspace-path resolver (`{customers_dir}`/`{transcripts_dir}`/`{notes_dir}`/`config_file`/`memory_dir`) per playbook → Workspace Paths. Portable across SE machines.
- **2026-07-09** — Genericized hardcoded "Gary" SE-identity prose → "the SE"; "Gary's CLAUDE.md" ref → the deployment-model guidance.
- **2026-07-09** — Fixed prior-doc read paths: now reads `deployment-qual`/`biz-qual`/`connector-feasibility`/`tech-qual` from `outputs/<skill>/` (was the customer root, where nothing is saved) so the "read prior outputs" chaining actually finds them.
- **2026-07-09** — Compliance claims now a mandatory pre-save self-check: cite+date or mark "verify with [team]"; never assert certifications from memory; verified/unverified/required labeled distinctly (`[customer requires]` / `[Airbyte supports — verified]` / `[Airbyte supports — unverified]`). Unverified compliance surfaces in At-a-Glance ("compliance claims pending verification").
- **2026-06-18** — Output adopts the shared Output Document Format (_se-playbook.md): At-a-Glance + Jump-to index, H2-per-section, callouts, ==key== emphasis.

- **2026-05-28** — Salesforce enrichment added (reads from sf-mcp via mcp__salesforce__run_soql_query). Pulls AE-view MEDDPICC / technical / forecast fields per the playbook field map; assertive SFDC-vs-reality mismatch flagging; graceful degradation if SFDC disabled. Org alias + query dir from .se-config.yaml.

- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Hard prerequisite: refuses to run without technical-discovery transcript. Reads prior outputs (deployment-qual, biz-qual, connector-feasibility). Deployment Model section now references deployment-qual verdict instead of re-deriving. Security & Compliance "verify before asserting" caveat for certifications. SPIN-style technical questions. Style normalized.
- **2026-05-27** — Initial scaffold.
