# Call Summary: Acme — June 11, 2026

**Date:** June 11, 2026

### At a Glance
- **Call type:** Discovery · **Duration:** 45 min
- **Call date:** June 11, 2026 · **Attendees:** ==[4]==
- **Action items:** ==[2]== · **Next step:** Schedule technical deep-dive for June 18
- **Deal-assessment update needed?** yes — EB confirmed budget

## Key Takeaways
- The data engineering team spends 520 hours/year maintaining broken pipelines [stated]
- They need a solution before the Fivetran renewal in March [inferred]

## Deal Health Signals
- **Positive signals:** EB confirmed budget and champion pushed timeline up.
- **Negative signals:** Security review may add 2 weeks.
- **Recommended Deal Assessment update?** yes — EB confirmed budget

> [!verdict] Budget confirmed by EB
> The economic buyer signed off on a $200K budget, which moves the deal forward.

## New Objections / Concerns Surfaced
- Security team raised VPC residency as a new concern [stated]

> [!risk] VPC residency
> The security lead asked whether Airbyte Cloud can keep data within their VPC.

## Action Items
- [ ] **SE** — Schedule technical deep-dive with security lead by June 14
- [ ] **Champion** — Introduce SE to the security reviewer

## Sources & Destinations
| System | Role | Notes |
|---|---|---|
| Salesforce | Source | Cloud, new this call |
| Snowflake | Destination | Existing warehouse |

> [!info] Feeds connector-feasibility & tech-qual
> Run or update connector-feasibility to check Airbyte coverage for these systems.

## Technical Notes
- **Volume / scale / frequency:** 10M rows/day [stated]
- **Deployment / infra / security:** VPC residency required

## Open Questions / Follow-ups
- Can Airbyte Cloud support VPC egress for this account?

## Attendees
- **Airbyte:** Jane Doe (SE)
- **Customer:** John Smith (VP Engineering)

## Next Step
Schedule technical deep-dive with the security lead for June 18.

## Source Coverage
Read Acme-06.11.26.txt in full (612 / 612 lines). Cross-referenced SE config to determine attribution.

## Coaching Observations
- The SE anchored the next step with a specific date and attendee list.

## MEDDPICC Quick Pass
| Letter | Status |
|---|---|
| M | green |
