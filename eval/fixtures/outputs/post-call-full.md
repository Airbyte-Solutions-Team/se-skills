# Call Summary: Acme — June 11, 2026

**Date:** June 11, 2026

### At a Glance
- **Call type:** Discovery · **Duration:** 45 min
- **Call date:** June 11, 2026 · **Attendees:** ==[4]==
- **Action items:** ==[2]== · **Next step:** Schedule technical deep-dive for June 18
- **Deal-assessment update needed?** yes — EB confirmed budget

**Jump to:** [At a Glance](#at-a-glance) · [Key Takeaways](#key-takeaways) · [Scope & Technical Changes](#scope--technical-changes) · [Deal Impact](#deal-impact) · [Objections & Open Questions](#objections--open-questions) · [Actions & Next Step](#actions--next-step) · [Coaching Observations](#coaching-observations) · [Source Coverage](#source-coverage)

## Key Takeaways
- [stated — VP Engineering] The data engineering team spends 520 hours/year maintaining broken pipelines.
- [inferred — pricing page] They need a solution before the Fivetran renewal in March.
- [inferred — call structure] POC must start by July 1 to hit renewal.

## Scope & Technical Changes
- **Sources & Destinations:**
  | System | Role | Notes |
  |---|---|---|
  | Salesforce | Source | Cloud, new this call |
  | Snowflake | Destination | Existing warehouse |
- **Technical Notes:**
  - **Volume / scale / frequency:** 10M rows/day [stated]
  - **Deployment / infra / security:** VPC residency required

> [!info] Feeds connector-feasibility & tech-qual
> Run or update connector-feasibility to check Airbyte coverage for these systems.

## Deal Impact
- **Positive signals:** EB confirmed budget and champion pushed timeline up.
- **Negative signals:** Security review may add 2 weeks.
- **Recommended Deal Assessment update?** yes — EB confirmed budget

> [!verdict] Budget confirmed by EB
> The economic buyer signed off on a $200K budget, which moves the deal forward.

## Objections & Open Questions
- [stated — Security lead] Security team raised VPC residency as a new concern.

> [!risk] VPC residency
> The security lead asked whether Airbyte Cloud can keep data within their VPC.

## Actions & Next Step
- [ ] **SE** — Schedule technical deep-dive with security lead by June 14
- [ ] **Champion** — Introduce SE to the security reviewer

Schedule technical deep-dive with the security lead for June 18.

## Coaching Observations
- [inferred — call structure] Anchor the workshop agenda to the stated security outcome.

## Source Coverage
Read Acme-06.11.26.txt in full (612 / 612 lines). Cross-referenced SE config to determine attribution.
