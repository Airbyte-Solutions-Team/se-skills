# Acme — Tech Qual: 🔴 park / no fit today
**Date:** 2026-07-14 · **Skill:** tech-qual

## At a Glance
- **Verdict:** 🔴 park / no fit today
- **Confidence:** Low
- **Source confidence:** Synthetic transcript only

## Deployment Model
Deployment model is **park / no fit** because BYOK/KMS is a hard requirement. If the requirement is waived, Enterprise Flex (data plane in customer VPC) becomes viable.

## Security & Compliance
BYOK/customer-managed KMS is **not supported** on any currently offered Airbyte shape per the product-reality stamp. If the `airbyte-platform` entitlement checkout is unavailable, this claim is marked **believed — verify with [team]** and confidence is capped.

## Technical Fit Summary
*Section content for Technical Fit Summary.

## Data Sources & Destinations
*Section content for Data Sources & Destinations.

## Data Volume & Scale
- Volume and latency assumptions are labeled as `[confirm]`.

## Recommended Next Actions
*Section content for Recommended Next Actions.

## Source Coverage
- Synthetic transcript used for evaluation.
- Workspace: `TMP_DIR` (replaced at runtime)
- Environment flags: {"registry_available": false, "airbyte_platform_available": false, "airbyte_enterprise_available": false, "salesforce_enabled": false, "connector_models_enabled": false}
