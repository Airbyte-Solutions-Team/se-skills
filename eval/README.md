# SE Skills Evaluation Framework

Lightweight, deterministic evaluation for the Claude Code skills in this repo.
The framework is built to work **without real customer data** and without a
local `claude` CLI. By default it runs synthetic scenarios against a mock
executor; a `--run-skills` pytest flag lets you invoke the actual skills when
the `claude` binary is available. A standalone `eval.runner` CLI is also
provided for running single scenarios or whole suites against real skill
execution.

## Quick start

```bash
uv run --extra dev pytest eval/ -v
```

This runs:

- `eval/tests/test_invariants.py` — unit tests for assertion helpers, the safe
  expression evaluator, and the mock output builder.
- `eval/tests/test_manifests.py` — one test per Phase 1 scenario loaded from
  `eval/manifests/phase1/*.yaml`.
- `eval/tests/test_runner.py` — deterministic coverage for the runner CLI,
  workspace isolation, executor behavior, report categorization, semantic
  evaluator, and prerequisite modes.

A combined report is written to `eval/results/phase1-report.json`.

## Framework tests vs live skill evaluations

The fast test suite is **purely deterministic**: it loads manifests, builds
synthetic workspaces, and checks assertions against mock skill output. It never
invokes a model and never leaves the repository (except for temp directories).

Live skill evaluations (`--executor claude` or `--run-skills`) run the actual
`claude` CLI against an isolated temporary workspace. They are optional, slow,
expensive, and may not be available in all environments. Passing the mock suite
does **not** prove that the real model will behave the same way; it only proves
that the evaluation framework can express the desired invariants.

## Prerequisite modes

Each manifest can declare how missing upstream qualification documents should
be handled:

```yaml
execution:
  prerequisite_mode: enforce          # default
  classification: normal               # normal | missing_input | degraded
```

- `enforce` (default): the skill must satisfy its own prerequisites. A refusal
  or halt is a valid outcome.
- `provide_fixtures`: the runner synthesizes the upstream qualification
  documents required for normal downstream execution so the scenario can test
  the downstream skill in isolation.
- `explicit_override`: the runner tells the skill it may skip missing
  prerequisites. This is only appropriate when the scenario intentionally
  tests degraded behavior.

Overrides are never automatic. When an override is used, the report records
`override_used: true` and the manifest classification must be `degraded` so the
result is not presented as normal production behavior.

## Scenario classifications

All six Phase 1 manifests are classified in their `execution.classification`
field:

| Manifest | Classification | Prerequisite mode | Rationale |
|---|---|---|---|
| `hourly-sync-constraint` | `normal` | `provide_fixtures` | Synthetic `biz-qual`, `deployment-qual`, and `connector-feasibility` fixtures are provided so `poc-plan` can run normally; the test is whether it preserves hourly sync. |
| `missing-technical-input` | `missing_input` | `enforce` | The transcript contains only business discovery, so `tech-qual` is expected to refuse. |
| `next-move-low-evidence` | `normal` | `enforce` | No upstream docs are required; the test is whether `next-move` expresses low confidence and recommends early-stage actions. |
| `sfdc-transcript-conflict` | `normal` | `enforce` | No upstream docs are required; the test is whether `deal-assessment` and `next-move` flag the SFDC vs transcript conflict. |
| `unverified-connector` | `normal` | `enforce` | `connector-feasibility` and `tech-qual` can run from the transcript alone; the test is unverified-connector handling. |
| `unverified-entitlement` | `normal` | `provide_fixtures` | `poc-plan` needs upstream qualification docs; the runner synthesizes them so the test can verify BYOK/KMS entitlement handling. |

## Run against real skills with pytest

> Warning: this invokes the `claude` CLI for every scenario and is slow/expensive.
> It may also fail in non-interactive environments because `claude` skill slash
> commands are not always available in `claude -p` / `--print` mode.

```bash
uv run --extra dev pytest eval/ -v --run-skills
```

This skips the mock executor and calls `claude -p` for each skill. The
workspace is a temporary directory, so no real customer data is touched.

## Run the evaluation CLI

List available manifests:

```bash
uv run python -m eval.runner list
```

Run a single scenario against the real `claude` CLI:

```bash
uv run python -m eval.runner run \
  --manifest eval/manifests/phase1/hourly-sync-constraint.yaml \
  --executor claude
```

Run the whole Phase 1 suite:

```bash
uv run python -m eval.runner run-suite \
  --manifest-dir eval/manifests/phase1 \
  --executor claude
```

Use `--retain-workspace` to keep the temporary workspace after a single run,
or `--retain-failures` to keep only failed suite workspaces for debugging.
Add `--semantic` to run the optional LLM-as-judge evaluator.

## Run model-dependent judge tests

```bash
uv run --extra dev pytest eval/ -v --run-model-judge
```

Requires `ANTHROPIC_API_KEY`. The deterministic suite never needs an API key.

## Fast CI vs optional model-dependent tests

What runs on every PR and every push to `main` (`.github/workflows/eval-deterministic.yml`):

- `uv run --extra dev pytest eval/ -v` (all deterministic tests).
- `uv run python -m eval.runner run-suite --manifest-dir eval/manifests/phase1 --executor mock` (mock suite).

What does **not** run automatically:

- Any test that invokes the `claude` CLI or Anthropic API.
- Any test that needs `ANTHROPIC_API_KEY`.
- Any test that writes to or reads from a real customer workspace.

Optional model-dependent runs are defined in
`.github/workflows/eval-model-dependent.yml` and can only be triggered
manually from the GitHub UI (`workflow_dispatch`). They require the
`ANTHROPIC_API_KEY` repository secret and run with a 90-minute timeout and
`max-parallel: 1` concurrency control.

## Reproduce CI locally

```bash
uv run --extra dev pytest eval/ -v
uv run python -m eval.runner run-suite --manifest-dir eval/manifests/phase1 --executor mock
```

## Why mock passes are not a complete behavioral baseline

The mock executor produces plausible Markdown that satisfies the manifest's
deterministic assertions. It does not exercise the real skill prompt, the
model's reasoning, or its ability to follow source-coverage rules. Two
successful live runs are also not a complete baseline: they sample a tiny
fraction of possible model behavior and may not surface rare regressions. The
framework is designed to catch obvious structural and business-invariant
violations quickly; it does not replace prompt-level review or broader
model-evaluation practices.

## Live skill execution limitations

In non-interactive environments, `claude -p` may not load local `SKILL.md`
files as slash commands. If `claude` does not recognize `/skill-name`, the
`ClaudeExecutor` will not be able to invoke the intended skill and may hang or
exit with an error. When that happens, the runner reports the invocation
failure and the scenario fails rather than producing a fabricated result.

## Add a scenario

1. Add a synthetic transcript under `eval/fixtures/transcripts/`.
2. Add any existing-output fixtures under `eval/fixtures/outputs/`.
3. Create a YAML manifest in `eval/manifests/phase1/`.
4. The manifest schema is enforced by `eval/schemas/manifest.py`.
5. Choose the correct `execution.prerequisite_mode` and `classification`.

### Minimal manifest

```yaml
manifest_version: "1.0"
id: my-scenario
title: What this scenario checks
skills_under_test:
  - tech-qual
  - poc-plan
execution:
  prerequisite_mode: enforce
  classification: normal
fixtures:
  transcripts:
    - source: fixtures/transcripts/my-transcript.txt
      target: _transcripts/Acme-07.01.26.txt
  config: fixtures/config/synthetic-se-config.yaml
environment:
  salesforce: false
customer_constraints:
  - "A relevant business or technical constraint."
available_evidence:
  - "One transcript containing X."
required_behavior:
  - "The skill must do Y."
forbidden_behavior:
  - "The skill must not do Z."
expected_sections:
  - At a Glance
  - Source Coverage
per_skill_expected_sections:
  tech-qual:
    - Data Volume & Scale
deterministic_assertions:
  - name: does not invent data
    target: markdown
    check: "not contains_case_insensitive(output, 'real customer name')"
    severity: blocker
  - name: section is present
    target: markdown
    check: "has_section(output, 'Data Volume & Scale')"
    severity: blocker
failure_severity: blocker
```

## Assertion language

Each `check` and `when` expression is a safe Python boolean expression. Only
whitelisted helpers and variables are available:

- `output` — the Markdown text returned by the skill.
- `manifest` — the manifest as a dictionary.
- `env` — workspace environment booleans (e.g. `env['airbyte_platform_available']`).
- Helpers: `contains_case_insensitive`, `contains_any_case_insensitive`,
  `contains_all_case_insensitive`, `has_section`, `section_contains`,
  `section_contains_any_case_insensitive`, `extract_section`.

`when` lets an assertion apply conditionally. For example:

```yaml
when: "env['airbyte_platform_available'] is not True"
check: "contains_any_case_insensitive(output, ['park', 'verify with team'])"
```

## Business invariants

The Phase 1B manifests focus on high-risk behaviors that should be caught by
deterministic checks:

- Sync-frequency preservation (hourly must not silently become daily).
- Customer constraints are not silently removed.
- Alternatives are not presented as primary recommendations.
- Unverified connectors or entitlements are not stated as confirmed.
- Missing inputs result in uncertainty or a request for validation.
- Salesforce data does not override a transcript without acknowledging the conflict.
- `next-move` does not invent a definitive action without evidence.
- Source Coverage and recommendation rationale are present.

When a check cannot be expressed safely with the assertion language, mark the
manifest with `model_judge` criteria so the optional semantic evaluator can
review the output.

## Fixtures

- `eval/fixtures/transcripts/` — synthetic Gong-style transcripts.
- `eval/fixtures/config/synthetic-se-config.yaml` — workspace config template.
  The runner replaces `TMP_DIR` with the path to a temporary workspace.
- `eval/fixtures/outputs/` — optional existing skill outputs that get copied
  into the temporary workspace before a skill runs.

## Design notes

- `eval/` does not modify `skills/` or `webapp/`.
- `MockExecutor` generates plausible, scenario-aware Markdown so the framework
  can be exercised without the `claude` CLI.
- `ClaudeExecutor` shells out to `claude -p` in an isolated temporary home with
  a copy of `skills/`, stubs for external tools, and `SE_WORKSPACE` pointing at
  the synthetic workspace. It is only used when explicitly requested.
- All temporary output is written to `eval/results/` and temporary
  directories; no real customer workspace under `~/.se-skills` or
  `~/airbyte-work` is touched.
- Workspace isolation is fail-closed: the runner refuses to build a workspace
  outside an approved temporary root, rejects path-traversal fixture targets,
  and surfaces cleanup failures.
- `eval/results/` is gitignored; reports are generated locally per run.
