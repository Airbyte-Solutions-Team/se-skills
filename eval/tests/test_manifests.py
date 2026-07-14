"""Deterministic evaluation of Phase 1 synthetic scenarios."""

from pathlib import Path

import pytest

from eval.conftest import record_manifest_result
from eval.runner import EvaluationReport, ManifestEvaluator
from eval.schemas.manifest import Manifest


def _format_failures(result) -> str:
    lines = [f"Manifest '{result.manifest_id}' ({result.title}) failed:"]
    for failure in result.failures:
        lines.append(f"  - {failure}")
    return "\n".join(lines)


@pytest.mark.model_dependent
@pytest.mark.skipif(
    not __import__("os").environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set; model judge requires an API key",
)
def test_model_judge_placeholder():
    """Stub for the optional LLM-as-judge harness.

    A full implementation will evaluate generated outputs against the
    `model_judge` criteria in each manifest. It is intentionally not part of the
    fast deterministic suite.
    """
    pytest.skip("Model judge harness is planned for Phase 3")


def test_manifest(manifest: Manifest, workspace, executor, repo_root: Path) -> None:
    """Run every skill in a manifest and assert deterministic behavior."""
    evaluator = ManifestEvaluator(manifest, executor)
    result = evaluator.evaluate(workspace)

    report_dir = repo_root / "eval" / "results"
    report_path = EvaluationReport.write(result, report_dir=report_dir)
    result.report_path = report_path
    record_manifest_result(result)

    assert result.passed, _format_failures(result)
