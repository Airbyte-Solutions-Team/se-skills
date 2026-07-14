"""Shared pytest fixtures and hooks for the SE Skills evaluation framework."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List

import pytest

from eval.runner import ClaudeExecutor, MockExecutor, Workspace, WorkspaceBuilder
from eval.schemas.manifest import Manifest


# In-process collection of manifest results so the session-finish hook can write
# a combined Phase 1 report.
_manifest_results: List["ManifestResult"] = []  # type: ignore[name-defined]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-skills",
        action="store_true",
        default=False,
        help="Invoke the real `claude` CLI for skills instead of using mock outputs.",
    )
    parser.addoption(
        "--run-model-judge",
        action="store_true",
        default=False,
        help="Run the optional LLM-as-judge assertions (requires ANTHROPIC_API_KEY).",
    )


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "manifest_path" in metafunc.fixturenames:
        repo_root = Path(__file__).parent.parent
        phase1_dir = repo_root / "eval" / "manifests" / "phase1"
        paths = sorted(phase1_dir.glob("*.yaml"))
        if not paths:
            raise FileNotFoundError(f"No manifest files found in {phase1_dir}")
        metafunc.parametrize("manifest_path", paths, ids=lambda p: p.stem)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).parent.parent


@pytest.fixture
def manifest(manifest_path: Path) -> Manifest:
    return Manifest.from_yaml(manifest_path)


@pytest.fixture
def workspace(manifest: Manifest, tmp_path: Path, repo_root: Path) -> Workspace:
    builder = WorkspaceBuilder(manifest, tmp_path, repo_root)
    return builder.build()


@pytest.fixture
def executor(request: pytest.FixtureRequest) -> MockExecutor | ClaudeExecutor:
    if request.config.getoption("--run-skills"):
        if not shutil.which("claude"):
            pytest.skip("--run-skills requested but `claude` CLI is not on PATH")
        return ClaudeExecutor()
    return MockExecutor()


def record_manifest_result(result: "ManifestResult") -> None:
    """Append a result to the in-session report list."""
    _manifest_results.append(result)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Write a combined Phase 1 report after all manifests have been evaluated."""
    if not _manifest_results:
        return
    # Import here to avoid circular imports during collection.
    from eval.runner import EvaluationReport

    repo_root = Path(__file__).parent.parent
    report_dir = repo_root / "eval" / "results"
    EvaluationReport.write_combined(_manifest_results, report_dir=report_dir)
