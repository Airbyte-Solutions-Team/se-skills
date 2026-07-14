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

A combined report is written to `eval/results/phase1-report.json`.

## Run against real skills with pytest

> Warning: this invokes the `claude` CLI for every scenario and is slow/expensive.

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

## Add a scenario

1. Add a synthetic transcript under `eval/fixtures/transcripts/`.
2. Add any existing-output fixtures under `eval/fixtures/outputs/`.
3. Create a YAML manifest in `eval/manifests/phase1/`.
4. The manifest schema is enforced by `eval/schemas/manifest.py`.

### Minimal manifest

```yaml
manifest_version: "1.0"
id: my-scenario
title: What this scenario checks
skills_under_test:
  - tech-qual
  - poc-plan
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
  the synthetic workspace.
- All temporary output is written to `eval/results/` and temporary
  directories; no real customer workspace under `~/.se-skills` or
  `~/airbyte-work` is touched.
- `eval/results/` is gitignored; reports are generated locally per run.
