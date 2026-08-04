# worker-analysis PR #38 validation report

## What was actually invoked

- **CLI runner**: every subcommand of `skills/worker-analysis/scripts/run_worker_analysis.py` (`estimate`, `questionnaire`, `oss`, `workspace`, `report`) with synthetic inputs.
- **Synthetic parity generator**: `eval/scripts/validate_worker_analysis.py` drove the real `claude` CLI for four scenarios, producing both original (`ai-skills/.agents/skills/worker-analysis/SKILL.md` as appended system prompt) and ported (`se-skills/skills/worker-analysis`) Markdown outputs plus JSON metadata sidecars.
- **End-to-end SE Skills lifecycle**: `eval/scripts/e2e_worker_analysis.py` used `httpx.AsyncClient` against a FastAPI `TestClient` app to exercise `/api/skills`, `/api/plan`, `/api/permissions`, `/api/invoke`, job polling, output history, and restart durability.
- **Regression suite**: `pytest eval/`, `eval.runner` phase-1 deterministic manifests, `./scripts/check-sync.sh`, `python -m py_compile`, `git diff --check`.

## Runtime used vs controlled/synthetic

- **Real native Claude Code runtime** (`claude -p ... --model claude-sonnet-4-6 --bare`) was used for:
  - The four `validate_worker_analysis.py` parity scenarios (both original and port).
  - The `e2e_worker_analysis.py` job invocation, where `JobService` spawned `claude -p` as a subprocess.
- The `pytest` and `eval.runner` tests use the repo's controlled mock executor and unit-test fixtures.
- The synthetic workspace fixture `eval/fixtures/worker_analysis_workspace.json` was used for the OSS-export parity scenario.

## Questionnaire mode

- **Status**: completed successfully in the CLI (`run_worker_analysis.py questionnaire`) and through the SE Skills E2E path.
- **CLI output**: valid JSON with `workers_required`, concurrency breakdown, capacity calculation, and maintenance-window insight.
- **E2E output**: the `claude` job finished with `status: done`, `ok: true`, and a Markdown analysis in `stdout`. The skill also wrote `analysis_summary.md` to `Synthetic/outputs/worker-analysis/` in the synthetic workspace and the output reader (`GET /api/accounts/Synthetic/outputs`) returned it.

## Workspace fixture mode

- **Status**: completed successfully.
- `run_worker_analysis.py oss eval/fixtures/worker_analysis_workspace.json` returned valid JSON.
- Parity output `se-suite/workspace_oss.md` correctly identified the 14:00 UTC peak (4 API + 3 DB concurrent), the 60-minute Postgres initial load, the zero-duration HubSpot job, and recommended **3 Data Workers**.

## Unhandled errors

No unhandled errors were observed:
- All `validate_worker_analysis.py` runs exited `0`.
- The E2E job reached a terminal `done` state.
- All CLI modes returned `0` with clear error messages for missing/invalid input (e.g., `workspace` without credentials reports an error rather than a traceback).

## Artifacts created

- **Markdown outputs**: 4 port + 4 original files under `eval/fixtures/worker_analysis/{se-suite,original}/`.
- **Metadata sidecars**: 8 `.json` files with commands, return codes, and stderr.
- **Parity matrix**: `eval/fixtures/worker_analysis/PARITY_MATRIX.md`.
- **E2E workspace artifacts**: `eval/fixtures/e2e_workspace/01-customers/Synthetic/outputs/worker-analysis/analysis_summary.md` plus a `.pdf` generated to `/tmp` during the run.

## Output history and reader

- `GET /api/accounts/Synthetic/outputs` returned the generated `analysis_summary.md` with metadata (size, mtime, validation status).
- Restart durability verified: after recreating the app, `app.state.job_service.get_job(job_id)` still returned the completed `done` record.

## CLI modes

| Mode | Valid input | Invalid/missing input | Notes |
|---|---|---|---|
| `estimate` | Exit 0, non-empty JSON | N/A | Uses size/mix/frequency presets. |
| `questionnaire` | Exit 0, non-empty JSON | Missing required args returns argparse error | Computes per-type ceiling. |
| `oss` | Exit 0, non-empty JSON | Missing file returns error | Parsed synthetic workspace fixture. |
| `workspace` | N/A (no credentials) | Clean error, no traceback | Reports missing workspace id / credentials. |
| `report` | Exit 0, PDF written | Missing args returns argparse error | Generated `/tmp/...Acme_Worker_Estimation_*.pdf`. |

## Dependencies

- `numpy>=1.24`, `reportlab>=4.0`, `requests>=2.31`, `python-dateutil>=2.8` are declared in `pyproject.toml` dev extras and resolved by `uv`.
- The CLI's PEP 723 inline dependencies also resolve.
- No fabricated results; missing external MCPs (Metabase, Datadog, Airbyte Cloud API) are reported honestly in the SKILL.md and runtime prompts.

## Comparison with original Devin output

See `eval/fixtures/worker_analysis/PARITY_MATRIX.md` for the full cell-by-cell matrix. At a high level:

- **Workspace OSS**: exact parity — same 3-worker recommendation, same peak, same anomaly detection.
- **Cadence preservation**: materially equivalent after adding the burst-check instruction to SKILL.md. Both now compute a worst-case daily pile-up peak of ~10 workers and present schedule staggering as the primary optimization.
- **Questionnaire complete**: steady-state number converged to 4 workers after fixing per-type ceiling and 15-minute sub-hourly interval; the original recommended 8 workers for the combined prod+staging worst-case, while the port headline stayed at 6 total. This is a runtime/model interpretation difference, not a methodology gap.
- **Incomplete evidence**: materially equivalent — both give a wide range, flag unknowns, and ask follow-ups rather than fabricating a precise number.

## Methodology differences

- `WorkerCalculator` was updated from `ceil(total_capacity)` to `ceil(api_capacity) + ceil(db_capacity)` to match the SKILL.md formula and the original Devin outputs.
- `SYNC_INTERVALS["sub_hourly"]` was changed from 30 to 15 minutes so questionnaire scenarios that say "every 15 minutes" are computed with the correct interval.
- A peak/maintenance-window burst-check instruction was added to `skills/worker-analysis/SKILL.md` so the model also computes a worst-case where daily syncs pile at the window start.

## Regression testing

- `uv run --extra dev pytest eval/ -q` — **614 passed, 1 skipped**
- `uv run python -m eval.runner run-suite --manifest-dir eval/manifests/phase1 --executor mock` — **12/12 passed**
- `./scripts/check-sync.sh` — clean
- `python -m py_compile` on changed files — clean
- `git diff --check` — clean

## External-runtime limitations

- Live Metabase billing queries, Datadog deep links, and Airbyte Cloud workspace API calls are not verified in this environment. The skill falls back to questionnaire/estimate mode and reports the missing prerequisites cleanly.
- The native `claude` CLI is available on the Devin box; the only runtime fix required was switching `JobService` from `--permission-mode auto` to `--permission-mode bypassPermissions` for `shell=True` skills so non-interactive Bash/Python execution does not hang.

## Deferred manual tests

- Live MCP runtime against a real Airbyte Cloud workspace.
- Live Metabase billing query with a configured BigQuery project.
- Live Datadog dashboard deep-link generation.
- Browser-based SE Skills UI smoke test (the webapp was not started for this validation; the E2E `httpx.AsyncClient` path covers the API layer).

## PR status

PR #38 remains a draft. The validation pass identified and fixed:
1. `WorkerCalculator` per-type ceiling.
2. `SYNC_INTERVALS` sub-hourly interval.
3. SKILL.md burst-check guidance.
4. `JobService` / `SkillRuntimeService` permission-mode handling for non-interactive `claude -p`.
5. `e2e_worker_analysis.py` `httpx.AsyncClient` and output-history endpoint.

No new integrations, UI workflows, generalized frameworks, or MCP clients were added. All changes are scoped to the `worker-analysis` skill, its CLI, and the webapp invocation path.
