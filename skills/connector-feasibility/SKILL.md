---
name: connector-feasibility
description: Checks a customer's source/destination list against the Airbyte connector registry. Flags missing connectors, identifies candidates for custom builds (manifest-only vs full CDK), and surfaces known issues. Use when the user says "connector feasibility", "check connectors", "do we have connectors for X", "feasibility check", or provides a list of sources/destinations during tech qual.
---

# Connector Feasibility Skill

You are helping a Solutions Engineer at Airbyte assess whether a customer's required data sources and destinations are covered by Airbyte's connector catalog. This is a critical step in technical qualification.

## Input

The user will provide either:
- A list of sources and destinations (e.g., "Salesforce, NetSuite, internal Postgres → Snowflake")
- A customer name (look in `01-customers/<Customer>/` and transcripts for mentioned systems)
- A pasted list from a customer email or RFP

If the input is ambiguous, ask which connectors are sources vs. destinations.

## Output mode

Default = full feasibility doc (coverage summary, per-connector table, gap analysis with build paths, reframe talk track, TCO, next steps).

If user signals brief mode (`--brief`, `quick coverage check`, `coverage summary`): produce just Coverage Summary table (counts by status) + bullet list of gaps with one-line build recommendations. Skip per-connector tables, reframe talk track, TCO callout. See `_se-playbook.md` "Output Mode" for the unified brief-mode rule.

## How to Check

For each system in the list:

1. **Search the Airbyte registry** — use the `mcp__airbyte-mcp__list_connectors` tool to find matches. Returns connector existence and basic metadata.
2. **Get connector details for capability check** — for matched connectors, use `mcp__airbyte-mcp__get_connector_info` to verify auth methods, supported streams/objects, sync modes. "Salesforce exists" ≠ "Salesforce supports the customer's exact use case."
3. **For close-but-not-exact matches** (e.g., customer says "Postgres") — confirm flavor: Postgres CDC, Postgres standard source, etc.
4. **For missing connectors** — assess buildability:
   - Use `shared-airbyte-skills:connector-type-identification` to determine the right build path (manifest-only YAML vs. low-code with Python vs. full Python CDK vs. Java/Kotlin CDK) — don't guess
   - Check the API documentation publicly available
   - Use `discovering-connectors` skill for capability framing
5. **For health of known connectors** — use `mcp__airbyte-ops-mcp__query_prod_failed_sync_attempts_for_connector` or `shared-airbyte-skills:connector-health-check` to surface known reliability issues. Flag any connector that's currently in rollout or has elevated failure rates.

## Output Format

---

## Connector Feasibility: [Customer Name]
**Date:** [today's date]
**Source of list:** [transcript / email / user-provided]

---

### Coverage Summary
| Status | Count |
|--------|-------|
| Available (certified) | X |
| Available (community/alpha) | X |
| Missing — custom build needed | X |
| Unclear — needs clarification | X |

---

### Available Connectors

| System | Connector | Type | Cert Level | Notes |
|--------|-----------|------|------------|-------|
| Salesforce | source-salesforce | Source | Certified | OAuth, full incremental support |
| Snowflake | destination-snowflake | Destination | Certified | Recommended for analytics workloads |

For each: flag if there are known issues (sync failures, slow performance, rollout in progress).

---

### Missing / Gap Connectors

For each missing connector, provide:

**[System Name]**
- **Customer use case:** [why they need it]
- **API type:** [REST / GraphQL / SOAP / DB / file-based / other]
- **API documentation quality:** [good / sparse / undocumented]
- **Auth complexity:** [simple API key / OAuth 2.0 / OAuth with refresh / custom / undocumented]
- **Pagination type:** [cursor / page-based / offset / undocumented]
- **Schema stability:** [stable / occasional changes / unstable]
- **Build path:** [manifest-only YAML / low-code with Python / full Python CDK / Java/Kotlin CDK] — based on `connector-type-identification` shared skill
- **Effort estimate (rough, SE judgment required):**
  - Manifest-only YAML, simple auth, well-documented: 1-3 days
  - Manifest-only YAML, OAuth or complex pagination: 3-7 days
  - Low-code with Python: 1-2 weeks
  - Full Python CDK: 2-6 weeks depending on API complexity
  - Java/Kotlin CDK: typically only for DB sources/destinations — 4+ weeks
  - **Flag estimate as "needs SE judgment" if any factor is undocumented or unknown — don't guess narrow ranges**
- **Alternative:** [is there a workaround, e.g., export to CSV and use file source?]

---

### Clarifications Needed
- [Any ambiguity in the customer's list — e.g., "Postgres" could be self-hosted or RDS, source or destination]

---

### Recommended Next Steps
1. [Confirm ambiguous items with customer]
2. [Decide on custom build path for gaps — internal eng vs. partner vs. customer-led]
3. [Schedule POC scoping if coverage is sufficient]

---

## Style

- **Be specific.** "source-salesforce v2.x" beats "yes we have Salesforce".
- **Don't oversell.** If a connector is community-tier or has reliability issues, say so. Surprises in POC are deal-killers.
- **Build effort estimates should be ranges.** "2–5 days for manifest-only, longer if pagination is unusual."
- **Flag deployment model implications.** If they need a connector that's only on Cloud (or only Self-Managed), call it out — ties to deployment model qualification.

## After Generating

### Auto-save (default)

Per `_se-playbook.md` "Output Persistence (Auto-Save)" rule, save to:
```
~/airbyte-work/01-customers/<Customer>/outputs/connector-feasibility/connector-feasibility-<YYYY-MM-DD>-<descriptor>.md
```

Append `-v2` etc. for same-day duplicates. User can suppress with `--no-save`.

### Source Coverage

Include a Source Coverage section at the top reporting: MCP queries run (`list_connectors`, `get_connector_info`, etc.), transcripts referenced for source/dest list, and connectors verified for known issues.

### SE Identity

Read `~/airbyte-work/.se-config.yaml` for the `[SE name]` field if applicable.

### Then offer to

1. Add a section to the customer's Notion Overview page
2. Suggest invoking `tech-qual` next if not already done

---

## SE Best Practices Applied to Connector Feasibility

Read `~/.claude/skills/_se-playbook.md` for full framework details.

### Salesforce Enrichment (active opp — seed the source/dest list)
Per `_se-playbook.md` "Salesforce Enrichment." Pull `Most_important_sources__c` and `Most_Important_Destinations__c` from the active opp to seed the coverage check. **Reconcile against the transcripts** — the SFDC list may be incomplete or stale vs. what the customer said. If they disagree, use the union and flag which systems came from where. If SFDC unavailable, skip per graceful-degradation and use the transcript/user-provided list.

### Source Freshness Check (Gong Fallback)
Per `_se-playbook.md` ("Source Freshness Check"): if the customer's source/destination list came from a transcript more than **14 days old**, search Gong for newer calls. Source lists often evolve as customers learn what's possible — using a stale list risks recommending against connectors the customer has already deprioritized (or missing ones they've added).
- Pull the **most recent call only** — do not bulk-pull
- Save to `_transcripts/<Customer-Name>-MM.DD.YY.txt` BEFORE using it (per CLAUDE.md)

### Reframe the connector-count comparison (Challenger)
If the customer is comparing total connector counts to Fivetran/Stitch/Matillion, that's a Reframe opportunity. The real question isn't count — it's:
1. Coverage of *their* stack (which you're measuring in this doc)
2. How the long tail gets built when something's missing (manifest-only builder + custom CDK)
3. Schema-drift and reliability over time, which count doesn't measure

Add a `### Reframe Talk Track` section at the end with 2-3 sentences Gary can use if the customer reverts to a count comparison.

### Anchor gaps in stated value (SPIN Implication)
For each missing connector, don't just note effort — note the cost of not having it. Example:
- "Customer needs X system. Manual export workaround costs ~5 hrs/week of analyst time = $50K/year in opportunity cost."

This converts a connector gap from a feature debate into a quantified business problem.

### Surface the "build it ourselves" alternative (MEDDPICC Competition)
For every missing connector, ask: what's the customer's mental model of "we'll just build this"? They often think it's a week of work. Reality:
- Initial build: 1-3 weeks
- Maintenance: 5-15% of build time per year, every year, forever
- Schema drift + auth refresh + pagination edge cases: ongoing
- On-call burden when it breaks at 2am: real

Include a short `### Build-vs-Adopt TCO` callout for any gap where customer might consider building. Numbers, not adjectives.

### Don't oversell — Sandler honesty
If a connector is community-tier or has known reliability issues, say so plainly. Surprises in POC kill deals. Always include "Known issues" or "Reliability watch-outs" column when applicable. Customer trust > pretty coverage table.

### Tie deployment model implications (Challenger Tailor)
If a connector is only available on Cloud (or only on Self-Managed), flag it. This connects to the deployment-model qualification — a Cloud-only connector for an air-gap customer is a deal-killer surfaced here, not at procurement.

### Anti-patterns to avoid in this skill
- Coverage table without effort estimates for gaps
- "We can build it" as the answer without TCO context
- Hiding community-tier or known-issue connectors in the "Available" column
- Connector counts without coverage-of-their-stack framing

---

## Changelog

- **2026-05-28** — Salesforce enrichment added (reads from sf-mcp via mcp__salesforce__run_soql_query). Pulls AE-view MEDDPICC / technical / forecast fields per the playbook field map; assertive SFDC-vs-reality mismatch flagging; graceful degradation if SFDC disabled. Org alias + query dir from .se-config.yaml.

- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Concrete MCP tool calls (list_connectors, get_connector_info, query_prod_failed_sync_attempts_for_connector). References connector-type-identification shared skill for build paths. Effort estimates with explicit ranges per build type + "needs SE judgment" flag for unknowns. Gap connector format expanded with auth complexity, pagination type, schema stability. Reframe + TCO callouts. 14-day Gong freshness check.
- **2026-05-27** — Initial scaffold.
