---
name: connector-feasibility
description: Validates whether Airbyte connectors actually solve a customer's specific use case — not just whether the connector exists. Reconstructs the use case and requirements from transcripts/SFDC/qual docs, validates each needed connector against those requirements (objects, sync modes, auth, volume, latency), surfaces constraints and edge cases given the customer's context, and generates per-connector questions the SE should ask the customer to fully confirm fit. Use when the user says "connector feasibility", "check connectors", "do we have connectors for X", "feasibility check", or provides a list of sources/destinations during tech qual.
---

# Connector Feasibility Skill

You are helping a Solutions Engineer at Airbyte determine whether Airbyte's connectors **actually solve this customer's specific use case** — not merely whether a connector with the right name exists. A connector existing is necessary but not sufficient: it has to support the customer's *objects, sync modes, auth model, volume, and latency*, and there are usually constraints and edge cases that only matter given the customer's particular context.

The most valuable thing this skill does is **surface what the SE still needs to ask the customer** to fully confirm fit — the requirements a connector eval depends on that the customer hasn't yet specified.

## Input

The user provides a customer name (preferred — so the skill can reconstruct the full use case) or an explicit list of systems.

## Step 1 — Reconstruct the use case + requirements

Before checking any connector, build a picture of what the customer is actually trying to do. Pull from:
- **Transcripts** (`01-customers/_transcripts/<Customer>-*`) — what systems, what data, what they said about volume/latency/history
- **Salesforce** (per `_se-playbook.md` Salesforce Enrichment): `Most_important_sources__c`, `Most_Important_Destinations__c`, `No_of_Databases__c`, `No_of_API_Sources__c`, `Monthly_Data_Volume__c`, `Refresh_Frequency__c`, `Use_case_description__c`, `Required_features_functionality__c`
- **Prior qual docs** (`tech-qual-*.md`, `biz-qual-*.md`) in the customer's outputs folder

Produce a short **Use Case Summary** at the top: what data flows from where to where, why, at what volume/cadence, with what history needs. If the use case is thin (little said), note that — it directly drives the "questions to ask" section.

If only a bare system list is given with no customer context, say so and lean heavily on Step 3 (questions to ask) since you can't validate against requirements you don't have.

## Output mode

Default = full feasibility doc (coverage summary, per-connector table, gap analysis with build paths, reframe talk track, TCO, next steps).

If user signals brief mode (`--brief`, `quick coverage check`, `coverage summary`): produce just Coverage Summary table (counts by status) + bullet list of gaps with one-line build recommendations. Skip per-connector tables, reframe talk track, TCO callout. See `_se-playbook.md` "Output Mode" for the unified brief-mode rule.

## Tool & skill dependencies (what's required vs. optional)

This skill reaches for several external tools and skills. **Only the registry lookup is load-bearing; everything else is an optional enhancement that must degrade gracefully.** Never claim coverage from a tool/skill that isn't available — note it as "not available" in Source Coverage instead.

- **Registry (primary):** `airbyte-ops-mcp` (`list_connectors_in_registry`, `get_connector_registry_entry`/`_spec`) — the source of truth for existence/version/spec. Requires that MCP + GCS creds. If absent, fall back to local source (`airbyte_repos_dir`) + published docs and cap confidence, noting it.
- **Optional external skills (skip cleanly if not installed — they ship separately from Airbyte's connector-skills marketplace, not with this repo):**
  - `shared-airbyte-skills:connector-type-identification` — connector type → which files matter. If absent, infer type from the on-disk files directly (`manifest.yaml` → manifest-only; `metadata.yaml` `language` field; presence of Python/Java source).
  - `shared-airbyte-skills:connector-health-check` — fleet health. If absent, use `airbyte-ops-mcp` `query_prod_failed_sync_attempts_for_connector` instead.
  - `shared-airbyte-skills:query-airbyte-docs` — Kapa docs search (internal/Devin env). If absent, browse docs or use deepwiki.
  - `discovering-connectors` — connector discovery. If absent, use the registry list.
- **Optional MCPs (skip silently if not connected):** deepwiki (upstream vendor API docs, public/no-auth), Kapa, Sentry, Datadog.
- **Optional local checkouts:** `airbyte_repos_dir` (see "Reading connector source locally").

When an optional skill/tool is missing, do the fallback named above and **record both the attempt and the fallback in Source Coverage** — the SE should know whether the build-path reasoning came from a first-class tool or a fallback.

## Step 2 — Validate each connector AGAINST the use case (not just existence)

For each system the customer needs, go through this chain. "The connector exists" is step 2a, not the answer.

1. **Exists?** — `mcp__airbyte-ops-mcp__list_connectors_in_registry` to find matches. For "Postgres"-type names, confirm the flavor (Postgres CDC vs. standard source).
2. **Capability fit** — `mcp__airbyte-ops-mcp__get_connector_registry_entry` (metadata: version, support level, breaking-change history) and `mcp__airbyte-ops-mcp__get_connector_registry_spec` (the published spec: auth methods, supported streams/objects, sync modes) to pull the connector's actual capabilities. **This is the live source of truth for existence, version, support level, and the published spec** — always trust it over the local checkout for "does it exist / what version / which streams." (These `airbyte-ops-mcp` tools read the GCS registry and require that MCP + GCS credentials; if unavailable, note it and fall back to local source / published docs.) Then **compare against what the customer needs:**
   - **Objects/streams:** do they need a specific object/table the connector doesn't expose? (e.g., a custom Salesforce object, a NetSuite saved search)
   - **Sync mode:** do they need incremental/CDC on a stream that only supports full refresh? This is a frequent silent dealbreaker.
   - **Auth model:** does the connector's auth match what the customer can provide? (OAuth app approval, IP allowlisting, service account, etc.)
   - **Latency:** their `Refresh_Frequency__c` / stated cadence vs. what the connector + tier supports (sub-hourly is Pro-only).
   - **Volume:** their `Monthly_Data_Volume__c` vs. realistic throughput; flag rate-limit risk on API sources.
   - **Read the connector source for the details the registry spec doesn't expose** — see "Reading connector source locally" below. The on-disk `manifest.yaml` (declarative) or stream classes (Python) reveal the *actual* pagination, cursor fields, incremental behaviour, sub-stream relationships, and known quirks (`BEHAVIOR.md`). Use this when the spec alone can't confirm a stream supports the sync mode / object the customer needs.
3. **Constraints & edge cases given THEIR context** — surface the gotchas that bite *this* use case specifically, e.g.:
   - API rate limits at their volume / number of instances (multi-tenant Shopify, per-account API quotas)
   - CDC prerequisites not yet enabled (Oracle LogMiner, MySQL binlog, Postgres WAL)
   - Nested/unstructured data or schema drift on the streams they care about
   - Historical backfill limits (API only returns N months; they need 3 years)
   - Network reachability (on-prem DB, NAS, PrivateLink requirement)
   - PII / compliance on specific streams that affects deployment model
   - For documented limitations & config gotchas, search the docs: `shared-airbyte-skills:query-airbyte-docs` (Kapa Docs MCP — internal/Devin env; may be absent on a local machine, skip silently if so) and, for *upstream* API/library behaviour the connector depends on, the **deepwiki MCP** (see below).
4. **Health** — `mcp__airbyte-ops-mcp__query_prod_failed_sync_attempts_for_connector` or `shared-airbyte-skills:connector-health-check` for reliability/rollout issues on the connectors they need. **If runtime-observability MCPs are connected** (Sentry, Datadog), use them for error stack traces / error-rate / latency on the specific connector — these are richer than prod failed-sync counts for diagnosing *why* a connector is unhealthy. They are not configured on every machine; check availability and skip silently if absent (don't claim health you couldn't verify).
5. **If missing** — assess buildability via `shared-airbyte-skills:connector-type-identification` (build path) + the **upstream vendor API docs** (browse, or query the **deepwiki MCP** for the vendor's repo to assess endpoints/auth/pagination/rate-limits) + `discovering-connectors`. Also check whether a **similar connector already exists that could be extended** — read its local source as the template (see below). State the build approach (declarative YAML vs. low-code-with-Python vs. Java) and an effort range with a "needs SE judgment" flag where unknown.

### Reading connector source locally

The connector **source code** is not in any MCP — the MCPs serve registry metadata, specs, and runtime data *about* connectors, not their implementation. The code lives in local Airbyte repo checkouts.

**Optional — graceful degradation.** Local source reading requires `airbyte_repos_dir` (resolved per playbook → Workspace Paths from `.se-config.yaml`). If it's **unset or the directory is missing**, skip this whole section — rely on MCP/registry data only, and note in Source Coverage: "local connector source not read (`airbyte_repos_dir` not configured) — build-path depth reduced." Do NOT fail. All paths below are relative to `{airbyte_repos_dir}`:

- **Connector:** `{airbyte_repos_dir}/airbyte/airbyte-integrations/connectors/<connector-name>/` — `manifest.yaml` (declarative connectors), `metadata.yaml` (version, support level, breaking-change history), `BEHAVIOR.md` (known quirks, certified low-code connectors), `CLAUDE.md`, `README.md`, Python `source_<name>/` stream classes, and `integration_tests/` / `unit_tests/`.
- **Python CDK:** `{airbyte_repos_dir}/airbyte-python-cdk/` — base classes, HTTP stream behaviour, error handling, pagination, incremental/cursor logic that declarative + Python connectors build on.
- **Java CDK:** `{airbyte_repos_dir}/airbyte/airbyte-cdk/java/` — for Java/Kotlin connectors.
- **User-facing docs + changelogs:** `{airbyte_repos_dir}/airbyte/docs/integrations/sources/<name>.md` and `.../destinations/<name>.md`.

Use `shared-airbyte-skills:connector-type-identification` to determine the connector type first (manifest-only / low-code / Python CDK / Java) — it dictates which files matter.

**Freshness guard (do this BEFORE relying on local source).** The checkout can be stale, which matters for build-path/CDK reasoning (it does *not* matter for existence/version — that comes from the live registry in step 2). Before reading source for a feasibility verdict:
1. Check the checkout's age: `git -C {airbyte_repos_dir}/airbyte log -1 --format=%cd --date=short` (and same for `{airbyte_repos_dir}/airbyte-python-cdk`).
2. If it's more than ~14 days old, refresh before reading: `git -C {airbyte_repos_dir}/airbyte fetch --depth=1 origin master && git -C {airbyte_repos_dir}/airbyte pull --ff-only` (and `airbyte-python-cdk` on `main`). If the pull fails (local changes / network), fall back to reading as-is and **report the checkout date in Source Coverage** so the SE knows how fresh the build-path reasoning is.
3. Never let a stale local checkout override the live registry. If the registry says a connector exists but the local checkout doesn't have it (or vice-versa), trust the registry and note the discrepancy — the local copy is just behind.

## Step 3 — Surface what the SE still needs to ask

This is the highest-value output. For **each connector**, compare what a confident feasibility call *requires knowing* against what the customer has *actually said* (from Step 1). Every gap becomes a specific question for the SE to ask.

The per-connector "needs to know" checklist:
- Which exact **objects/streams/tables**? (named, not "their data")
- **Sync mode** needed — full refresh, incremental, or CDC?
- **Auth** they can provide — and any approval/security process around it?
- **Volume** (rows/records) and **frequency** (how fresh)?
- **Historical backfill** depth required?
- **Network access** — cloud-reachable, or on-prem/VPN/PrivateLink?
- Any **transformation/filtering** expected at extract time?

Only surface questions for items **not already answered** in the transcripts/SFDC. If the customer already said "we need Salesforce Opportunity + Account, incremental, OAuth, ~2M rows, hourly" — don't re-ask; mark it validated. If they said "we need Salesforce" and nothing else — surface all of the above as open questions. Be specific: "Ask which NetSuite objects — our connector supports SuiteTalk REST records but not all SuiteAnalytics datasets."

## Output Format

Document structure follows `_se-playbook.md` → Output Document Format (At-a-Glance + Jump-to index, H2-per-section, callouts, `==key==` emphasis).

---

# Connector Feasibility: [Customer Name]
**Date:** [today's date — long form per `_se-playbook.md`, e.g. June 11, 2026, NOT 2026-06-11] · **Sources read:** [transcripts (with dates) / SFDC / qual docs]

### At a Glance
*Decision card — lead with the judgment (see `_se-playbook.md` → Decision-First Layout).*
- **Feasibility:** 🟢 All needs covered / 🟡 Covered with gaps to build / 🔴 Hard gap blocks use case — [3–6 word headline]
- **Coverage:** ==[N of M]== connectors validated · **Gaps:** [count build-needed] · **Open questions:** [count]
- **Recommended motion:** [e.g. "Proceed to POC scoping" / "Confirm gaps before committing"]
- **Primary risk:** [the biggest unvalidated assumption or hard gap — one line]
- **Source confidence:** [one line — N transcripts + SFDC; "see Source Coverage"]

**Jump to:** [At a Glance](#at-a-glance) · [Fit Verdict](#fit-verdict) · [Use Case Summary](#use-case-summary) · [Missing / Gap Connectors](#missing-gap-connectors) · [Constraints & Edge Cases](#constraints-edge-cases-given-their-context) · [Questions to Ask the Customer](#questions-to-ask-the-customer-to-fully-validate-fit) · [Recommended Next Steps](#recommended-next-steps) · [Source Coverage](#source-coverage)

*(Section order is decision-first: the verdict and gaps come before the use-case recap and the audit trail. Source Coverage is the last content section — see `_se-playbook.md` → Progressive disclosure.)*

### Fail loud on unavailable tools (per `_se-playbook.md` → Fail loud on missing sources/tools)

This skill's rigor depends on external sources. In Source Coverage, list each as used/unavailable:
- live connector registry (`get_connector_registry_entry` / `get_connector_registry_spec`) · local connector source checkout · prod failed-sync data · docs MCPs (deepwiki/Kapa) · observability (Sentry/Datadog).

If the registry OR local source OR prod-failure data was unavailable, this is NOT a full feasibility verdict. Cap the Feasibility confidence at 🟡 and lead with the caveat:
> "Fit assessed from registry metadata only — connector source and prod failure data were unavailable, so pagination/auth/reliability risks are unverified. Treat as a first-pass screen, not a validated verdict."

Also label build-effort ranges as planning estimates: "1–3 wks build is a planning estimate, not a commitment."

---

## Fit Verdict
*Lead with the answer — this is the first section after the decision card.* For each needed connector, the verdict is not just exists/missing — it's **does it solve their use case**:

If every needed connector is fully validated, open with a verdict callout:

```markdown
> [!verdict] All ==N of N== connectors validated for the use case
> source-salesforce, source-oracle (CDC), and destination-snowflake all support the required objects, sync modes, and volume. No open questions block POC scoping.
```

| System | Connector | Exists? | Use-case fit | Confidence | Top risk / gap |
|--------|-----------|---------|--------------|------------|----------------|
| Salesforce | source-salesforce | ✅ Certified | 🟢 Validated / 🟡 Likely, needs confirmation / 🔴 Gap | High / Med / Low | [e.g., need to confirm incremental on custom objects] |
| Oracle (CDC) | source-oracle | ✅ Pro | 🟡 Needs confirmation | Med | LogMiner not confirmed enabled |
| [System] | — | ❌ Missing | 🔴 Build needed | — | [build path + effort] |

**Confidence** reflects how much is *validated* vs. *assumed* — Low confidence means the "Questions to ask" section below has open items for this connector.

For each available connector, flag known reliability issues (sync failures, rollout in progress) inline.

Any missing/gap connector that **blocks the use case** (no connector and no viable workaround) → render as a `[!blocker]`:

```markdown
> [!blocker] No connector for [System] — blocks the [use-case] flow
> [System] has no Airbyte connector. Build path: full Python CDK, ~2-6 weeks. No CSV-export workaround because they need near-real-time. This gates the POC.
```

---

## Use Case Summary
*Context recap — placed after the verdict so the reader gets the answer first, then the framing.* [2-4 sentences reconstructed from Step 1: what data, from where to where, why, at what volume/cadence, with what history. State explicitly if the use case is thin.]

---

## Missing / Gap Connectors

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

## Constraints & Edge Cases (given their context)
*The gotchas that matter for THIS use case — not generic. Placed after coverage + gaps: it qualifies HOW the connectors behave in their environment.*
- [e.g., "14 Shopify instances → per-store API rate limits; parallelism + scheduling matter for the 15-min target"]
- [e.g., "Oracle CDC requires LogMiner enabled — not confirmed; without it, only full refresh / cursor available"]
- [e.g., "NetSuite historical backfill: SuiteTalk REST returns limited history; confirm how far back they need"]

---

## Questions to Ask the Customer (to fully validate fit)
*The highest-value output. Per connector, only the items NOT yet answered in the transcripts/SFDC. These are what the SE should raise to confirm the connector actually solves the use case. Be specific and explain why each matters.* Wrap the per-connector questions in a `[!info]` callout:

```markdown
> [!info] Salesforce — open questions before POC
> - [ ] Which Salesforce objects do you need synced — standard only, or custom objects too? *Our connector covers standard + custom, but custom objects need API access enabled on their side.*
> - [ ] Do you need change-data-capture (every change) on Orders, or is an hourly snapshot enough? *Determines whether we use CDC vs. incremental, which changes setup + cost.*

> [!info] [Connector / System] — open questions
> - [ ] [Specific question] — *why it matters: …*
```

*If a connector is fully validated (all needs-to-know answered), say "✓ Fully validated — no open questions" rather than inventing questions.*

---

## Recommended Next Steps
*Action table — each action has a goal, a definition of "done," and a fallback. Render `TBD` for Owner when unstated — never invent.*

| # | Next Action | Goal | Success criteria | Fallback | Owner |
|---|-------------|------|------------------|----------|-------|
| 1 | Ask the open questions above (before POC scoping) | Confirm each connector solves the use case | All Low/Med-confidence rows resolved | Stage unresolved items as POC risks | [name or **TBD**] |
| 2 | Decide build path for any gaps | A committed plan per gap connector | internal eng / partner / customer-led chosen | Defer gap to phase 2 | [name or **TBD**] |
| 3 | Schedule POC scoping if coverage sufficient | Move to a scoped POC | POC plan drafted | — | [name or **TBD**] |

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
{customers_dir}/<Customer>/outputs/connector-feasibility/connector-feasibility-<YYYY-MM-DD>-<Descriptor>.md
```

Append `-v2` etc. for same-day duplicates. User can suppress with `--no-save`.

### Source Coverage

Include a Source Coverage section at the top reporting: MCP queries run (`list_connectors_in_registry`, `get_connector_registry_entry`/`_spec`, etc.), transcripts referenced for source/dest list, and connectors verified for known issues. **Also report, when used:** which connectors' local source was read (and the `{airbyte_repos_dir}/airbyte` checkout date — so the SE can gauge build-path freshness), any docs queries run (Kapa / deepwiki), and any runtime-observability checks (Sentry / Datadog). If a tool or checkout was unavailable on this machine (e.g. `airbyte_repos_dir` unset, Kapa MCP, Sentry, Datadog not configured), don't list it as consulted — note "not available" rather than implying coverage you didn't have.

### SE Identity

Read `config_file` (per playbook → Workspace Paths) for the `[SE name]` field if applicable.

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

Add a `## Reframe Talk Track` section at the end with 2-3 sentences the SE can use if the customer reverts to a count comparison.

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

Include a short `## Build-vs-Adopt TCO` section for any gap where customer might consider building. Numbers, not adjectives.

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

- **2026-07-10** — **Portability + MCP fix.** Repointed hardcoded `~/airbyte-work/` output/config paths to the resolver (`{customers_dir}`/`config_file`); local Airbyte repo checkouts now resolve via `{airbyte_repos_dir}` (optional — skill degrades to MCP/registry-only when unset, noted in Source Coverage). **Fixed dead MCP tool names:** `mcp__airbyte-mcp__list_connectors` → `mcp__airbyte-ops-mcp__list_connectors_in_registry`, and `mcp__airbyte-mcp__get_connector_info` → `mcp__airbyte-ops-mcp__get_connector_registry_entry` + `get_connector_registry_spec` (there is no `airbyte-mcp` server; the old names would have failed on every machine). These require the `airbyte-ops-mcp` server + GCS creds; skill notes and falls back if unavailable. Added a **Tool & skill dependencies** section declaring what's required (registry) vs. optional-with-graceful-degradation (`shared-airbyte-skills:*`, `discovering-connectors`, deepwiki/Kapa/Sentry/Datadog, local checkouts) — each with a named fallback, since those skills ship separately from this repo and aren't guaranteed present.
- **2026-07-09** — Genericized hardcoded "Gary" SE-identity prose → "the SE" (reframe talk-track note).
- **2026-07-09** — Fail-loud on unavailable tools: Source Coverage lists each dependency used/unavailable; metadata-only runs are capped at 🟡 with a lead caveat; effort ranges labeled as estimates.
- **2026-06-26** — Richer troubleshooting/feasibility kit wired in. Step 2 now reads **local connector source** (`02-repos/airbyte/airbyte-integrations/connectors/<name>/` manifest/metadata/BEHAVIOR + Python/Java CDK) for implementation details the registry spec doesn't expose (pagination, cursors, sub-streams, quirks) — the MCPs serve metadata, not code. Added a **freshness guard**: check the checkout's age and `git pull --ff-only` if >~14 days stale before relying on it; live registry (`get_connector_info`) stays the source of truth for existence/version/streams, never overridden by a stale checkout. Added **deepwiki MCP** for upstream vendor-API/library docs (public, no auth) and referenced **Kapa Docs MCP** (internal/Devin) + **Sentry/Datadog** for runtime observability — all guarded as "skip silently / note 'not available' if not configured on this machine." Source Coverage now reports local-source-read + checkout date + which docs/observability tools were (un)available.

- **2026-06-18** — Output adopts the shared Output Document Format (_se-playbook.md): At-a-Glance + Jump-to index, H2-per-section, callouts, ==key== emphasis.

- **2026-06-16** — Reworked from a catalog lookup into a use-case feasibility assessment: reconstructs the use case + requirements from transcripts/SFDC/qual docs, validates each connector against the customer's actual objects/sync-modes/auth/volume/latency (not just existence), surfaces context-specific constraints & edge cases, and generates per-connector "Questions to Ask the Customer" from requirement gaps so the SE knows what to confirm. Fit Verdict table replaces plain coverage list.

- **2026-05-28** — Salesforce enrichment added (reads from sf-mcp via mcp__salesforce__run_soql_query). Pulls AE-view MEDDPICC / technical / forecast fields per the playbook field map; assertive SFDC-vs-reality mismatch flagging; graceful degradation if SFDC disabled. Org alias + query dir from .se-config.yaml.

- **2026-05-28** — Auto-save to outputs/<skill>/ folder (default; --no-save to suppress). Source Coverage section required (anti-hallucination). Reads SE identity from ~/airbyte-work/.se-config.yaml. Output filename: <skill>-YYYY-MM-DD-<descriptor>.md.

- **2026-05-27** — Concrete MCP tool calls (list_connectors, get_connector_info, query_prod_failed_sync_attempts_for_connector). References connector-type-identification shared skill for build paths. Effort estimates with explicit ranges per build type + "needs SE judgment" flag for unknowns. Gap connector format expanded with auth complexity, pagination type, schema stability. Reframe + TCO callouts. 14-day Gong freshness check.
- **2026-05-27** — Initial scaffold.
