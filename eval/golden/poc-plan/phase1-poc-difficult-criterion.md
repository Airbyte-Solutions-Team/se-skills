# Acme — Poc Plan: 🟡 viable with a hard success criterion
**Date:** 2026-07-14 · **Skill:** poc-plan

## At a Glance
- **Verdict:** 🟡 viable with a hard success criterion
- **Confidence:** Medium
- **Source confidence:** Synthetic transcript only

## Success Criteria
- **Must-have:** Sync 50M rows end-to-end within 5 minutes. This is a hard, customer-stated requirement and is preserved as a must-have success criterion.
- **Must-have:** Demonstrate incremental sync for the Postgres transactions table.
- **Nice-to-have:** SE can configure the connector without engineering support.

## Scope
- **Minimum viable POC scope:** Postgres transactions → Snowflake incremental sync; validate schema, auth, and basic throughput.
- **Optional stretch scope:** Increase volume test to 50M rows if time permits.
- **Production requirements:** 50M rows end-to-end within 5 minutes remains a production requirement if not fully proven in the POC.
- **POC-specific simplifications:** Test uses a representative subset with agreed proxy validation; full 50M at-scale validation is out of POC scope unless stretch scope is reached.

## Timeline
*Section content for Timeline.

## Risks & Mitigations
*Section content for Risks & Mitigations.

## Source Coverage
- Synthetic transcript used for evaluation.
- Workspace: `TMP_DIR` (replaced at runtime)
- Environment flags: {"registry_available": false, "airbyte_platform_available": false, "airbyte_enterprise_available": false, "salesforce_enabled": false, "connector_models_enabled": false}
