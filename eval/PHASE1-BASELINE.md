# Phase 1 Baseline Report

**Generated:** 2026-07-14 by `uv run --extra dev pytest eval/ -v`
**Executor:** `MockExecutor` (deterministic; no `claude` CLI required)
**Combined report:** `eval/results/phase1-report.json`

This baseline validates the evaluation harness against synthetic scenarios.
It does **not** invoke the live `claude` skills; that requires `--run-skills`.

| Scenario | Skills under test | Deterministic result | Notes |
|---|---|---|---|
| phase1-hourly-sync-constraint | tech-qual, poc-plan | passed | Preserves hourly sync requirement; no frequency reduction. |
| phase1-unverified-connector | connector-feasibility, tech-qual | passed | Flags `source-foo-bar` as unverified; does not claim availability. |
| phase1-missing-technical-input | tech-qual | passed | Refuses to generate tech-qual from a business-only transcript. |
| phase1-sfdc-transcript-conflict | deal-assessment, next-move | passed | Flags SFDC-vs-transcript conflict and defers POC. |
| phase1-next-move-low-evidence | next-move | passed | Low confidence; recommends discovery, not POC. |
| phase1-unverified-entitlement | deployment-model-qual, tech-qual, poc-plan | passed | Parks deployment and POC due to unavailable BYOK/KMS entitlement. |

## Invariants covered

- **Constraint preservation:** sync-frequency and capacity requirements are not silently relaxed.
- **Graceful degradation:** unverified connectors and entitlements are flagged, not fabricated.
- **Hard gates:** skills refuse when required customer voice is missing.
- **Conflict detection:** Salesforce data is not treated as ground truth when it conflicts with the transcript.
- **Workflow routing:** low-evidence scenarios route to discovery, not later-stage skills.

## Running against real skills

```bash
uv run --extra dev pytest eval/ -v --run-skills
```

This will invoke `claude -p` for each skill. The temporary workspace is
destroyed after the test run, so real customer data is never touched.
