"""Golden fixture regression tests for skill outputs.

Each Phase 1 manifest is executed with the deterministic mock executor. The
output for every skill under test is compared against the committed golden
fixture in `eval/golden/{skill}/{manifest_id}.md`. If the fixture is missing,
the test is skipped unless `--update-golden` is passed, in which case the
fixture is written from the current output.
"""

from pathlib import Path

import pytest

from eval.conftest import record_manifest_result
from eval.golden import load_golden, save_golden
from eval.runner import EvaluationReport, ManifestEvaluator, MockExecutor
from eval.schemas.manifest import Manifest


def _format_mismatch(mismatches: list[str]) -> str:
    return "Golden fixture mismatches:\n" + "\n".join(f"  - {m}" for m in mismatches)


def test_skill_output_matches_golden(
    manifest: Manifest,
    workspace,
    repo_root: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Run every skill in the manifest and diff against golden fixtures."""
    update = request.config.getoption("--update-golden")

    evaluator = ManifestEvaluator(manifest, MockExecutor())
    result = evaluator.evaluate(workspace)

    report_dir = repo_root / "eval" / "results"
    report_path = EvaluationReport.write(result, report_dir=report_dir)
    result.report_path = report_path
    record_manifest_result(result)

    assert result.passed, _format_manifest_failures(result)

    mismatches: list[str] = []
    for sr in result.skill_results:
        golden = load_golden(sr.skill, manifest.id)
        if golden is None:
            if update:
                save_golden(sr.skill, manifest.id, sr.output_text)
                continue
            mismatches.append(f"[{sr.skill}] missing golden fixture; run with --update-golden")
            continue
        if sr.output_text != golden:
            if update:
                save_golden(sr.skill, manifest.id, sr.output_text)
                continue
            mismatches.append(f"[{sr.skill}] output differs from golden fixture")

    if mismatches:
        pytest.fail(_format_mismatch(mismatches))


def _format_manifest_failures(result) -> str:
    lines = [f"Manifest {result.manifest_id} failed deterministic checks:"]
    for sr in result.skill_results:
        for f in sr.failures:
            lines.append(f"  [{sr.skill}] {f}")
    return "\n".join(lines)
