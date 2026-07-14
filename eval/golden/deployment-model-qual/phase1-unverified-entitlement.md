# Acme — Deployment Model Qual: 🔴 park / no fit today
**Date:** 2026-07-14 · **Skill:** deployment-model-qual

## At a Glance
- **Verdict:** 🔴 park / no fit today
- **Confidence:** Low
- **Source confidence:** Synthetic transcript only

## The Five Qualifying Questions
| Question | Answer | Implication |
|---|---|---|
| Deployment preference | Cloud/Flex | TBD from transcript |
| Data residency | None | Cloud viable |
| Multi-tenancy | None | Cloud viable |
| BYOK/KMS | **Yes, hard requirement** | 🔴 no fit on any offered shape |
| VPC isolation | Data-plane only | Flex viable if BYOK resolved |

## Verdict
**🔴 park / no fit today.** A hard BYOK/KMS requirement is not supported on Cloud Pro or Enterprise Flex as of the latest product reality. Entitlement claim should be verified with the product team before confirming.

## Recommended Next Action
*Section content for Recommended Next Action.

## Source Coverage
- Synthetic transcript used for evaluation.
- Workspace: `TMP_DIR` (replaced at runtime)
- Environment flags: {"registry_available": false, "airbyte_platform_available": false, "airbyte_enterprise_available": false, "salesforce_enabled": false, "connector_models_enabled": false}
