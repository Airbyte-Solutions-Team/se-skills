# worker-analysis PR #38 validation report

## What was actually invoked

- **CLI runner**: every subcommand of `skills/worker-analysis/scripts/run_worker_analysis.py` (`estimate`, `questionnaire`, `oss`, `workspace`, `report`) with synthetic inputs.
- **Synthetic parity generator**: `eval/scripts/validate_worker_analysis.py` drove the real `claude` CLI for four scenarios, producing both original (`ai-skills/.agents/skills/worker-analysis/SKILL.md` as appended system prompt) and ported (`se-skills/skills/worker-analysis`) Markdown outputs plus JSON metadata sidecars. The complete-questionnaire scenario was also run **five times** with identical inputs using the port skill to verify repeatability.
- **End-to-end SE Skills lifecycle**: `eval/scripts/e2e_worker_analysis.py` used `httpx.AsyncClient` against a FastAPI `TestClient` app to exercise `/api/skills`, `/api/plan`, `/api/permissions`, `/api/invoke`, job polling, output history, and restart durability.
- **Regression suite**: `pytest eval/`, `eval.runner` phase-1 deterministic manifests, `./scripts/check-sync.sh`, `python -m py_compile`, `git diff --check`.

## Runtime used vs controlled/synthetic

- **Real native Claude Code runtime** (`claude -p ... --model claude-sonnet-4-6 --bare`) was used for:
  - The four `validate_worker_analysis.py` parity scenarios (both original and port).
  - The five repeated identical-input complete-questionnaire runs.
  - The `e2e_worker_analysis.py` job invocation, where `JobService` spawned `claude -p` as a subprocess.
- The `pytest` and `eval.runner` tests use the repo's controlled mock executor and unit-test fixtures.
- The synthetic workspace fixture `eval/fixtures/worker_analysis_workspace.json` was used for the OSS-export parity scenario.

## Deterministic burst calculation

The worst-case burst calculation is implemented in `skills/worker-analysis/worker_analysis/src/questionnaire_calculator.py`. It is always executed when the questionnaire inputs contain a daily/scheduled workload group, a defined peak/maintenance window, and multiple environments:

- **Steady-state concurrency**: `API_conc = Σ(api_count_by_freq × duration / interval)` and `DB_conc = Σ(db_count_by_freq × duration / interval)`.
- **Worst-case burst concurrency**: adds the full count of daily (or less frequent) connections to the steady-state sub-hourly + hourly concurrency, modelling every daily sync firing simultaneously at the window start.
- **Workers**: `ceil(API_conc / 5) + ceil(DB_conc / 2)` for each view.
- **Seven sizing views** returned as structured evidence:
  1. `steady_state_workers`
  2. `peak_window_drain_workers`
  3. `worst_case_burst_workers`
  4. `production_only_workers`
  5. `combined_prod_staging_workers`
  6. `future_growth_workers`
  7. `recommended_contract_or_deployment_workers`
- **Final recommendation**: `max(combined_prod_staging + headroom, future_growth_workers)`, where headroom is at least 1 when daily syncs or multiple environments are present and up to 2 to absorb part of the burst delta.

For the complete-questionnaire fixture this yields:
- Steady-state workers: 4
- Peak-window drain workers: 2
- Worst-case burst workers: 11
- Production-only workers: 4
- Combined prod + staging workers: 6
- Future-growth (80 connections) workers: 8
- Recommended contract/deployment capacity: 8

## Five identical-input questionnaire runs

| Run | Steady-state | Worst-case burst | Prod-only | Prod + staging | Future (80) | Final recommendation |
|---|---|---|---|---|---|---|
| 1 | 4 | 11 | 4 | 6 | 8 | 8 |
| 2 | 4 | 11 | 4 | 6 | 8 | 8 |
| 3 | 4 | 11 | 4 | 6 | 8 | 8 |
| 4 | 4 | 11 | 4 | 6 | 8 | 8 |
| 5 | 4 | 11 | 4 | 6 | 8 | 8 |

All five real-runtime runs produced the same deterministic sizing views and the same 8-worker headline recommendation. Minor wording and table formatting differed between runs; no material recommendation alternation occurred.

## Parity reconciliation

This section reconciles the parity matrix against the **saved output artifacts** under `eval/fixtures/worker_analysis/original/` and `eval/fixtures/worker_analysis/se-suite/`, which are the source of truth.

### Scenario 1 — Complete questionnaire

- **Original artifact:** `eval/fixtures/worker_analysis/original/questionnaire_complete.md`
- **Port artifact:** `eval/fixtures/worker_analysis/se-suite/questionnaire_complete.md`
- **Inputs:** 45 connections, 30 API / 15 DB, 20% sub-hourly, 30% hourly, 50% daily, 10-min avg, 2–6 AM peak, 2 environments, 80-connection growth target.

| | Original | Port | Notes |
|---|---|---|---|
| Current recommendation | 8 Data Workers | 8 Data Workers | Identical. |
| Future-growth calculation (80 conns) | 8 workers | 8 workers | Identical combined prod+staging at 80. |
| Future headline recommendation at 80 | 8 in sizing view and summary; 10 in growth-trajectory table | 8 Data Workers | The original artifact is internally contradictory. The port's deterministic output matches the original's explicit 8-worker future-growth sizing view. The 10 appears to be an additional step-up trigger or model inconsistency, not a separate calculation. |

The contradiction in the original output is **not** a port defect. The parity classification for the current recommendation is **Exact**; the future headline recommendation is **Materially equivalent** because the original artifact gives two numbers and the port picks the one that aligns with the deterministic calculation.

### Scenario 2 — Cadence preservation

- **Original artifact:** `eval/fixtures/worker_analysis/original/cadence_preservation.md`
- **Port artifact:** `eval/fixtures/worker_analysis/se-suite/cadence_preservation.md`
- **Inputs:** 50 connections, 35 API / 15 DB, 25% sub-hourly, 40% hourly, 35% daily, 12-min avg, 1–5 AM peak, 2 environments, 80-connection growth target.

| | Original | Port | Notes |
|---|---|---|---|
| Current recommendation | 10 Data Workers | 10 Data Workers | Identical. |
| Future recommendation at 80 | 10 Data Workers ("6-month growth capacity | ✅ Covered at 80 connections") | 10 Data Workers | Identical. |

The earlier parity matrix claimed the original recommended "18–20 workers" at month 6. That number is **not present** in the saved original artifact; it came from an earlier or different validation run. The classification is now **Exact** for both current and future recommendations.

### Scenario 3 — Incomplete evidence

- **Original artifact:** `eval/fixtures/worker_analysis/original/incomplete_evidence.md`
- **Port artifact:** `eval/fixtures/worker_analysis/se-suite/incomplete_evidence.md`
- **Inputs:** ~40 connections, API/DB split and duration unknown, some hourly and some daily, growth expected but unspecified. The port additionally assumes 40% hourly / 60% daily, 8-min avg duration, 2 environments, and 60-connection growth target.

| | Original | Port | Notes |
|---|---|---|---|
| Executive summary / headline | "6–7 Data Workers (recommended starting capacity)" | "6 Data Workers" | The previous matrix mislabeled the 2-worker steady-state figure as the final recommendation. The port's 6 is inside the original's 6–7 range. |
| Steady-state base case | 2–3 workers | 2 workers | The 2 in the previous matrix was this steady-state value, not the headline. |
| Prod + staging base case | 4–5 workers | 4 workers | |
| Worst-case burst | 8–9 workers | 8–12 workers | Both give a similar burst ceiling range. |
| Future-growth view | Not modeled as a row; notes 2× (80 conns) would be 8–9 | 60-conns future-growth = 4–5 workers, still recommended 6 | The future target was not the same, so the numbers are not directly comparable. Both state that growth pushes the recommendation up. |

The classification for the headline recommendation is **Materially equivalent**; the future-growth view is a **Runtime-required difference** because the two outputs assumed different growth targets.

### Reconciliation conclusion

All contradictions traced to either:

1. **Labeling/documentation errors in the previous parity matrix** (Scenario 2's "18–20" and Scenario 3's "2 workers today"), or
2. **Genuine internal ambiguity in the generated original output** (Scenario 1's 8 vs 10 at 80 connections).

No production code was changed. The deterministic calculator and the permission allowlist remain unchanged.

## Original-vs-ported questionnaire parity

The complete-questionnaire scenario no longer contains a material parity gap for current sizing, worst-case burst, combined-environment recommendation, or the headline Data Worker recommendation:

- Original Devin current recommendation: **8 Data Workers** for prod + staging today.
- SE-suite current recommendation: **8 Data Workers** for prod + staging today.
- Future-growth combined requirement at 80: both **8 workers**.
- The original artifact's future headline is ambiguous (8 and 10); the port consistently returns **8 Data Workers**, matching the explicit 8-worker future-growth sizing view in the original.

See `eval/fixtures/worker_analysis/PARITY_MATRIX.md` for the full cell-by-cell matrix.

## Questionnaire mode

- **Status**: completed successfully in the CLI (`run_worker_analysis.py questionnaire`) and through the SE Skills E2E path.
- **CLI output**: valid JSON with `sizing` containing all seven deterministic views plus a `legacy_estimate` for backwards compatibility.
- **E2E output**: the `claude` job finished with `status: done`, `ok: true`, and a Markdown analysis in `stdout` that included the deterministic sizing table. The skill also wrote `analysis_summary.md` and a PDF to `Synthetic/outputs/worker-analysis/`; the output reader returned it.

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
- **Repeatability outputs**: 5 additional `se-suite/questionnaire_complete_run_{1-5}.md` files.
- **Metadata sidecars**: `.json` files with commands, return codes, and stderr.
- **Parity matrix**: `eval/fixtures/worker_analysis/PARITY_MATRIX.md`.
- **Validation report**: this file.
- **E2E workspace artifacts**: `eval/fixtures/e2e_workspace/01-customers/Synthetic/outputs/worker-analysis/analysis_summary.md` plus a PDF generated to `/tmp` during the run.

## Output history and reader

- `GET /api/accounts/Synthetic/outputs` returned the generated `analysis_summary.md` with metadata (size, mtime, validation status).
- Restart durability verified: after recreating the app, `app.state.job_service.get_job(job_id)` still returned the completed `done` record.

## CLI modes

|| Mode | Valid input | Invalid/missing input | Notes |
|---|---|---|---|
|| `estimate` | Exit 0, non-empty JSON | N/A | Uses size/mix/frequency presets. |
|| `questionnaire` | Exit 0, non-empty JSON | Missing required args returns argparse error | Now emits all seven deterministic sizing views. |
|| `oss` | Exit 0, non-empty JSON | Missing file returns error | Parsed synthetic workspace fixture. |
|| `workspace` | N/A (no credentials) | Clean error, no traceback | Reports missing workspace id / credentials. |
|| `report` | Exit 0, PDF written | Missing args returns argparse error | Generated `/tmp/...Acme_Worker_Estimation_*.pdf`. |

## bypassPermissions blast radius and final rule

- **Skills currently with `shell=True`**: `connector-feasibility`, `freeform`, `pov-gsheet`, `worker-analysis`.
- **Skills that receive `bypassPermissions`**: only the same four skills, and only because they are explicitly listed in `SHELL_BYPASS_ALLOWLIST` in `webapp/services/skill_runtime_service.py` **and** declare `shell=True` in `SKILL_PERMISSIONS`.
- **Final eligibility rule**: `permission_mode == "bypassPermissions"` if and only if `profile.shell is True` and `skill_id in SHELL_BYPASS_ALLOWLIST`. Everything else uses `permission_mode == "auto"`.
- **Backend approval gate**: `SkillRuntimeService.invoke` checks `approve_permissions=True` before launching any job. The user must call `/api/permissions` to see the plan and then `POST /api/invoke` with `approve_permissions=true`.
- **Unknown / arbitrary skills**: cannot obtain `bypassPermissions` merely by declaring shell usage. A skill must be added to the hard-coded allowlist in code; discovered skill folders on disk do not influence the allowlist.
- **Untrusted/malformed skill**: cannot exploit this path unless both `SKILL_PERMISSIONS` and `SHELL_BYPASS_ALLOWLIST` are modified in the source.
- **worker-analysis permission configuration**: requires `write=True` (to save outputs) and `shell=True` (to run the Python toolkit scripts), with `git=False`. It uses `bypassPermissions` only because it is in the allowlist.
- **Existing non-shell skills**: retain `write=True`, `shell=False`, `git=False`, `permission_mode="auto"`.
- **Failure / timeout**: the permission block returns a `blocked` dict rather than launching; the service remains usable for subsequent requests.

## Permission-gate security tests

Tests in `eval/tests/test_webapp_permissions.py` prove:

- `worker-analysis` cannot run until the backend approval gate succeeds.
- The permission plan (`GET /api/permissions`) is returned before approval and includes `write`, `shell`, `git`, `permission_mode`, `requires_approval`, and a human-readable `summary`.
- A rejected permission plan (`approve_permissions=false`) prevents invocation and returns no `job_id`.
- An unknown skill with `shell=True` but not in `SHELL_BYPASS_ALLOWLIST` receives `permission_mode="auto"`, not `bypassPermissions`.
- Only the four allowlisted shell skills receive `bypassPermissions`.
- `worker-analysis` receives only `write` + `shell` (no git and no extra permissions).
- Existing non-shell skills retain write-only auto permission behavior.
- A blocked invocation does not break the service; a subsequent approved invocation succeeds.

## Regression testing

- `uv run --extra dev pytest eval/ -q` — **628 passed, 1 skipped**
- `uv run python -m eval.runner run-suite --manifest-dir eval/manifests/phase1 --executor mock` — **12/12 passed**
- `./scripts/check-sync.sh` — clean
- `python -m py_compile` on changed files — clean
- `git diff --check` — clean

## External-runtime limitations

- Live Metabase billing queries, Datadog deep links, and Airbyte Cloud workspace API calls are not verified in this environment. The skill falls back to questionnaire/estimate mode and reports the missing prerequisites cleanly.
- The native `claude` CLI is available on the Devin box; the non-interactive hang for shell-enabled skills is prevented by passing `--permission-mode bypassPermissions` only for the reviewed allowlist.

## PR status

PR #38 remains a draft. This validation pass resolved the two focused merge blockers:

1. **Deterministic worst-case burst parity**: the burst calculation is in `questionnaire_calculator.py`, the seven sizing views are always returned, and the complete-questionnaire fixture consistently recommends 8 Data Workers across repeated real-runtime runs.
2. **`bypassPermissions` safety boundary**: the permission mode is limited to an explicit reviewed-skill allowlist, with security tests proving the approval gate and blast radius.

No new integrations, UI workflows, generalized frameworks, or MCP clients were added. All changes stay within the `worker-analysis` skill, its CLI, and the `SkillRuntimeService`/`JobService` permission path.
