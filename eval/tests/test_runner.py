"""Deterministic tests for the evaluation runner, workspace isolation, executors, and reports."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from eval.runner import (
    ClaudeExecutor,
    EvaluationReport,
    ManifestEvaluator,
    ManifestResult,
    MockExecutor,
    SemanticEvaluator,
    SkillEvaluationResult,
    SkillResult,
    WorkspaceBuilder,
    _approved_temp_roots,
    _is_approved_temp_dir,
    _rmtree_surfacing,
    _safe_join,
    main,
)
from eval.schemas.manifest import Manifest


def _write_minimal_manifest(
    path: Path,
    skills: List[str],
    *,
    prerequisite_mode: str = "enforce",
    classification: str = "normal",
    existing_outputs: List[Dict[str, str]] | None = None,
    assertions: List[Dict[str, Any]] | None = None,
) -> None:
    """Write a tiny manifest suitable for runner unit tests."""
    skills_yaml = "\n  - ".join(skills)
    assertions = assertions or [
        {
            "name": "source coverage present",
            "target": "markdown",
            "check": "has_section(output, 'Source Coverage')",
            "severity": "blocker",
        }
    ]
    data: Dict[str, Any] = {
        "manifest_version": "1.0",
        "id": path.stem,
        "title": path.stem.replace("-", " ").title(),
        "skills_under_test": skills,
        "fixtures": {"config": "fixtures/config/synthetic-se-config.yaml"},
        "execution": {
            "prerequisite_mode": prerequisite_mode,
            "classification": classification,
        },
        "expected_sections": ["At a Glance", "Source Coverage"],
        "deterministic_assertions": assertions,
    }
    if existing_outputs:
        data["fixtures"]["existing_outputs"] = existing_outputs
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.fixture
def minimal_manifest(tmp_path: Path, repo_root: Path) -> Manifest:
    """A loaded manifest with no model dependency."""
    path = tmp_path / "minimal.yaml"
    _write_minimal_manifest(path, ["next-move"])
    return Manifest.from_yaml(path)


# ---------------------------------------------------------------------------
# CLI behavior
# ---------------------------------------------------------------------------


def test_cli_list_manifests(capsys) -> None:
    """`list` prints every manifest in the requested directory."""
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        (tmp_dir / "a.yaml").write_text(
            yaml.safe_dump(
                {
                    "manifest_version": "1.0",
                    "id": "a",
                    "title": "Alpha",
                    "skills_under_test": ["next-move"],
                }
            ),
            encoding="utf-8",
        )
        (tmp_dir / "b.yaml").write_text(
            yaml.safe_dump(
                {
                    "manifest_version": "1.0",
                    "id": "b",
                    "title": "Beta",
                    "skills_under_test": ["next-move"],
                }
            ),
            encoding="utf-8",
        )
        assert main(["list", "--manifest-dir", str(tmp_dir)]) == 0
        out = capsys.readouterr().out
        assert "a: Alpha" in out
        assert "b: Beta" in out
    finally:
        _rmtree_surfacing(tmp_dir)


def test_cli_run_selects_manifest(tmp_path: Path, repo_root: Path) -> None:
    """`run` evaluates the manifest supplied by --manifest."""
    manifest_path = tmp_path / "manifest.yaml"
    report_dir = tmp_path / "reports"
    _write_minimal_manifest(manifest_path, ["next-move"])

    assert (
        main(
            [
                "run",
                "--manifest",
                str(manifest_path),
                "--report-dir",
                str(report_dir),
            ]
        )
        == 0
    )

    report = json.loads((report_dir / manifest_path.stem).with_suffix(".json").read_text())
    assert report["manifest_id"] == manifest_path.stem
    assert report["status"] == "passed"
    assert report["execution"]["prerequisite_mode"] == "enforce"


def test_cli_run_suite_discovers_manifests(tmp_path: Path, repo_root: Path) -> None:
    """`run-suite` evaluates every .yaml file in --manifest-dir."""
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    report_dir = tmp_path / "reports"
    _write_minimal_manifest(manifest_dir / "one.yaml", ["next-move"])
    _write_minimal_manifest(manifest_dir / "two.yaml", ["next-move"])

    assert main(["run-suite", "--manifest-dir", str(manifest_dir), "--report-dir", str(report_dir)]) == 0

    combined = json.loads((report_dir / "phase1-report.json").read_text())
    assert combined["status"] == "passed"
    ids = {m["manifest_id"] for m in combined["manifests"]}
    assert ids == {"one", "two"}


def test_cli_invalid_manifest_path_returns_nonzero(tmp_path: Path, capsys) -> None:
    """A missing manifest file yields a clear non-zero exit."""
    assert main(["run", "--manifest", "/nonexistent/manifest.yaml"]) == 1
    assert "error:" in capsys.readouterr().err


def test_cli_unimplemented_executor_returns_nonzero(
    tmp_path: Path, repo_root: Path, capsys
) -> None:
    """The `anthropic` choice is valid for argparse but raises a clear runtime error."""
    manifest_path = tmp_path / "manifest.yaml"
    _write_minimal_manifest(manifest_path, ["next-move"])

    assert (
        main(
            [
                "run",
                "--manifest",
                str(manifest_path),
                "--executor",
                "anthropic",
                "--report-dir",
                str(tmp_path / "reports"),
            ]
        )
        == 1
    )
    assert "anthropic" in capsys.readouterr().err.lower()


def test_cli_default_executor_is_mock(tmp_path: Path, repo_root: Path) -> None:
    """When --executor is omitted, the runner uses the mock executor and passes."""
    manifest_path = tmp_path / "manifest.yaml"
    report_dir = tmp_path / "reports"
    _write_minimal_manifest(manifest_path, ["next-move"])

    # No --executor argument is supplied.
    assert (
        main(
            [
                "run",
                "--manifest",
                str(manifest_path),
                "--report-dir",
                str(report_dir),
            ]
        )
        == 0
    )

    report = json.loads((report_dir / manifest_path.stem).with_suffix(".json").read_text())
    assert report["status"] == "passed"


def test_cli_missing_claude_returns_nonzero(
    tmp_path: Path, repo_root: Path, monkeypatch, capsys
) -> None:
    """--executor claude without the CLI on PATH fails clearly, not silently."""
    monkeypatch.setattr("shutil.which", lambda _name: None)
    manifest_path = tmp_path / "manifest.yaml"
    _write_minimal_manifest(manifest_path, ["next-move"])

    assert (
        main(
            [
                "run",
                "--manifest",
                str(manifest_path),
                "--executor",
                "claude",
                "--report-dir",
                str(tmp_path / "reports"),
            ]
        )
        == 1
    )
    assert "claude" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# Workspace isolation
# ---------------------------------------------------------------------------


def test_workspace_builds_in_approved_temp_dir(minimal_manifest, tmp_path: Path, repo_root: Path) -> None:
    """WorkspaceBuilder succeeds inside pytest's temp directory."""
    workspace = WorkspaceBuilder(minimal_manifest, tmp_path, repo_root).build()
    assert workspace.root.exists()
    assert (workspace.root / ".se-config.yaml").exists()


def test_workspace_rejects_real_customer_paths(minimal_manifest, repo_root: Path) -> None:
    """Paths under real customer workspace roots are refused."""
    for forbidden in (Path.home() / ".se-skills", Path.home() / "airbyte-work"):
        with pytest.raises(ValueError, match="Refusing"):
            WorkspaceBuilder(minimal_manifest, forbidden, repo_root).build()


def test_workspace_rejects_repo_root(minimal_manifest, repo_root: Path) -> None:
    """The repository root is not an approved temporary root."""
    with pytest.raises(ValueError, match="Refusing"):
        WorkspaceBuilder(minimal_manifest, repo_root, repo_root).build()


def test_safe_join_rejects_traversal(tmp_path: Path) -> None:
    """Relative paths containing `..` are rejected."""
    with pytest.raises(ValueError, match="path traversal"):
        _safe_join(tmp_path, "foo/../bar")


def test_safe_join_rejects_absolute(tmp_path: Path) -> None:
    """Absolute target paths are rejected."""
    with pytest.raises(ValueError, match="absolute paths"):
        _safe_join(tmp_path, "/etc/passwd")


def test_safe_join_rejects_symlink_escape(tmp_path: Path) -> None:
    """Symlinks that resolve outside the base directory are rejected."""
    outside = tmp_path.parent / "se-eval-outside-dir"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="escapes"):
            _safe_join(tmp_path, "link")
    finally:
        _rmtree_surfacing(outside)


def test_fixture_target_cannot_escape_workspace(
    tmp_path: Path, repo_root: Path
) -> None:
    """A transcript fixture target containing `..` fails closed."""
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "manifest_version": "1.0",
                "id": "escape",
                "title": "Escape",
                "skills_under_test": ["next-move"],
                "fixtures": {
                    "config": "fixtures/config/synthetic-se-config.yaml",
                    "transcripts": [
                        {
                            "source": "fixtures/transcripts/acme-2026-06-15-intro.txt",
                            "target": "../etc/passwd",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    manifest = Manifest.from_yaml(manifest_path)
    with pytest.raises(ValueError, match="path traversal"):
        WorkspaceBuilder(manifest, tmp_path / "work", repo_root).build()


def test_cleanup_removes_workspace_by_default(
    tmp_path: Path, repo_root: Path, monkeypatch
) -> None:
    """The default run deletes the temporary workspace after writing the report."""
    manifest_path = tmp_path / "manifest.yaml"
    work_dir = tmp_path / "work"
    _write_minimal_manifest(manifest_path, ["next-move"])

    from eval.runner import _run_manifest

    # Pin the runner-created temp directory so we can assert it is removed.
    monkeypatch.setattr("tempfile.mkdtemp", lambda *args, **kwargs: str(work_dir))

    result = _run_manifest(
        manifest_path=manifest_path,
        repo_root=repo_root,
        executor_name="mock",
        retain_workspace=False,
        semantic=False,
        report_dir=tmp_path / "reports",
    )
    assert result.passed
    assert not work_dir.exists()


def test_retain_workspace_preserves_directory(
    tmp_path: Path, repo_root: Path
) -> None:
    """--retain-workspace leaves the workspace on disk and reports its path."""
    manifest_path = tmp_path / "manifest.yaml"
    work_dir = tmp_path / "work"
    _write_minimal_manifest(manifest_path, ["next-move"])

    from eval.runner import _run_manifest

    result = _run_manifest(
        manifest_path=manifest_path,
        repo_root=repo_root,
        executor_name="mock",
        retain_workspace=True,
        semantic=False,
        report_dir=tmp_path / "reports",
        work_dir=work_dir,
    )
    assert result.passed
    assert work_dir.exists()


def test_cleanup_failure_is_surfaced(tmp_path: Path) -> None:
    """A failing cleanup raises RuntimeError instead of swallowing the error."""
    fake = tmp_path / "not-a-directory"
    with pytest.raises(RuntimeError, match="failed to remove"):
        _rmtree_surfacing(fake)


# ---------------------------------------------------------------------------
# Executor behavior
# ---------------------------------------------------------------------------


def test_mock_executor_produces_expected_output(
    tmp_path: Path, repo_root: Path
) -> None:
    """MockExecutor writes a deterministic Markdown file and returns a SkillResult."""
    manifest_path = tmp_path / "manifest.yaml"
    _write_minimal_manifest(manifest_path, ["next-move"])
    manifest = Manifest.from_yaml(manifest_path)
    workspace = WorkspaceBuilder(manifest, tmp_path / "work", repo_root).build()

    executor = MockExecutor()
    result = executor.run(
        skill="next-move",
        workspace_root=workspace.root,
        account="Acme",
        extra_prompt="",
        output_dir=workspace.customer_dir / "outputs",
        manifest=manifest,
    )

    assert result.returncode == 0
    assert result.output_path is not None
    assert result.output_path.exists()
    assert "At a Glance" in result.output_text
    assert "Source Coverage" in result.output_text


def test_claude_executor_builds_expected_command(
    tmp_path: Path, repo_root: Path, monkeypatch
) -> None:
    """ClaudeExecutor constructs an isolated command without actually calling `claude`."""
    calls: List[Dict[str, Any]] = []

    def fake_run(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return subprocess.CompletedProcess(
            args=args[0] if args else [],
            returncode=0,
            stdout="# Acme — next-move: pass\n\n## At a Glance\n- Verdict: pass\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    manifest_path = tmp_path / "manifest.yaml"
    _write_minimal_manifest(manifest_path, ["next-move"])
    manifest = Manifest.from_yaml(manifest_path)
    workspace = WorkspaceBuilder(manifest, tmp_path / "work", repo_root).build()

    executor = ClaudeExecutor(timeout=30)
    result = executor.run(
        skill="next-move",
        workspace_root=workspace.root,
        account="Acme",
        extra_prompt="synthetic evaluation",
        output_dir=workspace.customer_dir / "outputs",
        manifest=manifest,
    )

    assert result.returncode == 0
    assert len(calls) == 1
    cmd = calls[0]["args"][0]
    env = calls[0]["kwargs"]["env"]
    assert cmd[0] == "claude"
    assert cmd[1] == "-p"
    assert "--bare" in cmd
    assert "--permission-mode" in cmd
    assert "--disallowed-tools" in cmd
    assert any("Bash(sf *)" in arg for arg in cmd)
    assert env["HOME"].startswith(str(tmp_path))
    assert env["SE_WORKSPACE"] == str(workspace.root)
    assert (Path(env["HOME"]) / "bin" / "sf").exists()
    assert "GITHUB_TOKEN" not in env


def test_claude_executor_missing_cli_raises(tmp_path: Path, repo_root: Path, monkeypatch) -> None:
    """`claude` not on PATH produces a clear RuntimeError."""
    def raise_not_found(*args, **kwargs):
        raise FileNotFoundError("claude")

    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr("subprocess.run", raise_not_found)
    executor = ClaudeExecutor()
    assert not executor.available()
    workspace = WorkspaceBuilder(
        Manifest(
            id="x",
            title="x",
            skills_under_test=["next-move"],
            fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
        ),
        tmp_path / "work",
        repo_root,
    ).build()
    with pytest.raises(RuntimeError, match="claude CLI is not installed"):
        executor.run(
            skill="next-move",
            workspace_root=workspace.root,
            account="Acme",
            extra_prompt="",
            output_dir=workspace.customer_dir / "outputs",
            manifest=Manifest(
                id="x",
                title="x",
                skills_under_test=["next-move"],
                fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
            ),
        )


def test_claude_executor_subprocess_failure_categorized(
    tmp_path: Path, repo_root: Path, monkeypatch
) -> None:
    """A nonzero `claude` exit is recorded as an invocation error."""
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)

    manifest = Manifest(
        id="x",
        title="x",
        skills_under_test=["next-move"],
        fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
        expected_sections=["At a Glance", "Source Coverage"],
        deterministic_assertions=[
            {
                "name": "source coverage present",
                "target": "markdown",
                "check": "has_section(output, 'Source Coverage')",
                "severity": "blocker",
            }
        ],
    )
    workspace = WorkspaceBuilder(manifest, tmp_path / "work", repo_root).build()
    executor = ClaudeExecutor(timeout=30)
    result = executor.run(
        skill="next-move",
        workspace_root=workspace.root,
        account="Acme",
        extra_prompt="",
        output_dir=workspace.customer_dir / "outputs",
        manifest=manifest,
    )

    evaluator = ManifestEvaluator(manifest, executor=None)
    evaluation = evaluator._evaluate_skill("next-move", result, env={})

    assert evaluation.invocation_errors
    assert not evaluation.passed
    assert "claude exited with code 1" in evaluation.invocation_errors[0]


def test_claude_executor_timeout_categorized(
    tmp_path: Path, repo_root: Path, monkeypatch
) -> None:
    """A `claude` timeout is reported as an invocation failure."""
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0] if args else ["claude"], timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    manifest = Manifest(
        id="x",
        title="x",
        skills_under_test=["next-move"],
        fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
        expected_sections=["At a Glance", "Source Coverage"],
        deterministic_assertions=[
            {
                "name": "source coverage present",
                "target": "markdown",
                "check": "has_section(output, 'Source Coverage')",
                "severity": "blocker",
            }
        ],
    )
    workspace = WorkspaceBuilder(manifest, tmp_path / "work", repo_root).build()
    executor = ClaudeExecutor(timeout=1)
    result = executor.run(
        skill="next-move",
        workspace_root=workspace.root,
        account="Acme",
        extra_prompt="",
        output_dir=workspace.customer_dir / "outputs",
        manifest=manifest,
    )

    assert result.returncode == -1
    assert "timed out" in result.stderr


def test_claude_executor_env_is_restricted(
    tmp_path: Path, repo_root: Path, monkeypatch
) -> None:
    """The isolated environment keeps secrets out of the child process."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")

    manifest = Manifest(
        id="x",
        title="x",
        skills_under_test=["next-move"],
        fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
    )
    workspace = WorkspaceBuilder(manifest, tmp_path / "work", repo_root).build()
    env = ClaudeExecutor._isolated_env(Path(tmp_path / "home"), workspace.root)

    assert env["ANTHROPIC_API_KEY"] == "fake-key"
    assert "GITHUB_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["HOME"] == str(tmp_path / "home")
    assert env["SE_WORKSPACE"] == str(workspace.root)
    assert env["PATH"].startswith(str(Path(tmp_path / "home") / "bin"))


def test_secrets_not_in_report_output(
    tmp_path: Path, repo_root: Path, monkeypatch
) -> None:
    """Reports do not contain secret-looking values even if the env has them."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-secret")
    manifest_path = tmp_path / "manifest.yaml"
    report_dir = tmp_path / "reports"
    _write_minimal_manifest(manifest_path, ["next-move"])

    assert (
        main(
            [
                "run",
                "--manifest",
                str(manifest_path),
                "--report-dir",
                str(report_dir),
            ]
        )
        == 0
    )

    report_text = (report_dir / manifest_path.stem).with_suffix(".json").read_text()
    assert "sk-test-secret" not in report_text


# ---------------------------------------------------------------------------
# Report categorization
# ---------------------------------------------------------------------------


def test_report_categorizes_invocation_failure() -> None:
    """A subprocess failure ends up in invocation_errors and fails the result."""
    manifest = Manifest(
        id="x",
        title="x",
        skills_under_test=["next-move"],
        fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
        expected_sections=["At a Glance", "Source Coverage"],
    )
    result = SkillResult(
        skill="next-move",
        output_text="",
        output_path=None,
        returncode=1,
        stdout="",
        stderr="subprocess failed",
        files_written=[],
        refused=False,
    )
    evaluation = ManifestEvaluator(manifest, executor=None)._evaluate_skill("next-move", result, env={})
    assert evaluation.invocation_errors
    assert not evaluation.passed


def test_report_categorizes_structural_failure() -> None:
    """A missing required section is recorded as a structural failure."""
    manifest = Manifest(
        id="x",
        title="x",
        skills_under_test=["next-move"],
        fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
        expected_sections=["At a Glance", "Source Coverage"],
    )
    result = SkillResult(
        skill="next-move",
        output_text="# Other\ncontent",
        output_path=None,
        returncode=0,
        stdout="",
        stderr="",
        files_written=[],
        refused=False,
    )
    evaluation = ManifestEvaluator(manifest, executor=None)._evaluate_skill("next-move", result, env={})
    assert evaluation.structural_failures
    assert not evaluation.passed


def test_report_categorizes_invariant_failure() -> None:
    """A failing deterministic assertion is recorded as an invariant failure."""
    manifest = Manifest(
        id="x",
        title="x",
        skills_under_test=["next-move"],
        fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
        expected_sections=["Source Coverage"],
        deterministic_assertions=[
            {
                "name": "must not recommend reducing frequency",
                "target": "markdown",
                "check": "not contains_case_insensitive(output, 'reduce')",
                "severity": "blocker",
            }
        ],
    )
    result = SkillResult(
        skill="next-move",
        output_text="We can reduce frequency.",
        output_path=None,
        returncode=0,
        stdout="",
        stderr="",
        files_written=[],
        refused=False,
    )
    evaluation = ManifestEvaluator(manifest, executor=None)._evaluate_skill("next-move", result, env={})
    assert evaluation.invariant_failures
    assert not evaluation.passed


def test_report_categorizes_warning() -> None:
    """A refusal that is not expected is recorded as a warning."""
    manifest = Manifest(
        id="x",
        title="x",
        skills_under_test=["next-move"],
        fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
        expected_sections=["Source Coverage"],
    )
    result = SkillResult(
        skill="next-move",
        output_text="# Refused\nCannot generate.",
        output_path=None,
        returncode=0,
        stdout="",
        stderr="",
        files_written=[],
        refused=True,
    )
    evaluation = ManifestEvaluator(manifest, executor=None)._evaluate_skill("next-move", result, env={})
    assert evaluation.warnings
    assert "refused unexpectedly" in evaluation.warnings[0]


def test_report_categorizes_passed() -> None:
    """A valid output with all checks passing is marked passed."""
    manifest = Manifest(
        id="x",
        title="x",
        skills_under_test=["next-move"],
        fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
        expected_sections=["Source Coverage"],
        deterministic_assertions=[
            {
                "name": "source coverage present",
                "target": "markdown",
                "check": "has_section(output, 'Source Coverage')",
                "severity": "blocker",
            }
        ],
    )
    result = SkillResult(
        skill="next-move",
        output_text="# Source Coverage\n- transcript",
        output_path=None,
        returncode=0,
        stdout="",
        stderr="",
        files_written=[],
        refused=False,
    )
    evaluation = ManifestEvaluator(manifest, executor=None)._evaluate_skill("next-move", result, env={})
    assert evaluation.passed
    assert not evaluation.failures


def test_passed_requires_usable_output() -> None:
    """A scenario must not be passed when invocation produced no usable output."""
    manifest = Manifest(
        id="x",
        title="x",
        skills_under_test=["next-move"],
        fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
        expected_sections=["Source Coverage"],
        deterministic_assertions=[
            {
                "name": "source coverage present",
                "target": "markdown",
                "check": "has_section(output, 'Source Coverage')",
                "severity": "blocker",
            }
        ],
    )
    result = SkillResult(
        skill="next-move",
        output_text="# Source Coverage\n- transcript",
        output_path=None,
        returncode=1,
        stdout="",
        stderr="error",
        files_written=[],
        refused=False,
    )
    evaluation = ManifestEvaluator(manifest, executor=None)._evaluate_skill("next-move", result, env={})
    assert not evaluation.passed
    assert evaluation.invocation_errors


def test_report_contains_execution_metadata(
    tmp_path: Path, repo_root: Path
) -> None:
    """The JSON report records prerequisite mode, classification, and override flag."""
    manifest_path = tmp_path / "manifest.yaml"
    _write_minimal_manifest(
        manifest_path,
        ["next-move"],
        prerequisite_mode="explicit_override",
        classification="degraded",
    )
    manifest = Manifest.from_yaml(manifest_path)
    workspace = WorkspaceBuilder(manifest, tmp_path / "work", repo_root).build()
    executor = MockExecutor()
    result = ManifestEvaluator(manifest, executor).evaluate(workspace)
    report_path = EvaluationReport.write(result, report_dir=tmp_path / "reports")

    report = json.loads(report_path.read_text())
    assert report["execution"]["prerequisite_mode"] == "explicit_override"
    assert report["execution"]["classification"] == "degraded"
    assert report["execution"]["override_used"] is True


# ---------------------------------------------------------------------------
# Semantic evaluator
# ---------------------------------------------------------------------------


def test_semantic_parse_valid_json() -> None:
    """A well-formed JSON array is normalized into semantic results."""
    evaluator = SemanticEvaluator()
    proc = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='[{"criterion": "x", "passed": true, "confidence": "High", "excerpt": "abc", "reasoning": "ok"}]',
        stderr="",
    )
    results = evaluator._parse_result(proc, ["x"])
    assert len(results) == 1
    assert results[0]["criterion"] == "x"
    assert results[0]["passed"] is True
    assert results[0]["excerpt"] == "abc"


def test_semantic_parse_malformed_json() -> None:
    """Malformed JSON yields a single failure result."""
    evaluator = SemanticEvaluator()
    proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")
    results = evaluator._parse_result(proc, ["x"])
    assert len(results) == 1
    assert results[0]["passed"] is False
    assert "JSON" in results[0]["reasoning"]


def test_semantic_parse_missing_required_fields() -> None:
    """Items missing fields get safe defaults."""
    evaluator = SemanticEvaluator()
    proc = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='[{}]',
        stderr="",
    )
    results = evaluator._parse_result(proc, ["x"])
    assert results[0]["criterion"] == ""
    assert results[0]["passed"] is False
    assert results[0]["confidence"] == "Low"


def test_semantic_unknown_criterion() -> None:
    """The evaluator preserves the criterion text returned by the judge."""
    evaluator = SemanticEvaluator()
    proc = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='[{"criterion": "unknown thing", "passed": true, "confidence": "Medium", "excerpt": "", "reasoning": ""}]',
        stderr="",
    )
    results = evaluator._parse_result(proc, ["rubric item"])
    assert results[0]["criterion"] == "unknown thing"
    assert results[0]["passed"] is True


def test_semantic_missing_excerpt() -> None:
    """An empty excerpt is allowed and recorded."""
    evaluator = SemanticEvaluator()
    proc = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='[{"criterion": "x", "passed": true, "confidence": "High", "excerpt": "", "reasoning": "ok"}]',
        stderr="",
    )
    results = evaluator._parse_result(proc, ["x"])
    assert results[0]["excerpt"] == ""


def test_semantic_invalid_confidence() -> None:
    """Confidence values are normalized to strings."""
    evaluator = SemanticEvaluator()
    proc = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='[{"criterion": "x", "passed": true, "confidence": 123, "excerpt": "", "reasoning": ""}]',
        stderr="",
    )
    results = evaluator._parse_result(proc, ["x"])
    assert results[0]["confidence"] == "123"


def test_semantic_timeout_or_api_failure() -> None:
    """A nonzero judge exit is reported as a semantic failure."""
    evaluator = SemanticEvaluator()
    proc = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="rate limited")
    results = evaluator._parse_result(proc, ["x"])
    assert len(results) == 1
    assert results[0]["passed"] is False
    assert "rate limited" in results[0]["reasoning"]


def test_semantic_pass_does_not_override_deterministic_failure(
    tmp_path: Path, repo_root: Path
) -> None:
    """A semantic evaluator pass cannot hide a deterministic invariant failure."""
    manifest = Manifest(
        id="x",
        title="x",
        skills_under_test=["next-move"],
        fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
        expected_sections=["Source Coverage"],
        deterministic_assertions=[
            {
                "name": "must mention a term that is absent",
                "target": "markdown",
                "check": "contains_case_insensitive(output, 'mandatory-missing-term')",
                "severity": "blocker",
            }
        ],
    )
    workspace = WorkspaceBuilder(manifest, tmp_path / "work", repo_root).build()

    class FakeSemantic(SemanticEvaluator):
        def evaluate(self, output: str, manifest: Manifest, skill: str) -> List[Dict[str, Any]]:
            return [{"criterion": "x", "passed": True, "confidence": "High", "excerpt": "", "reasoning": ""}]

    executor = MockExecutor()
    evaluator = ManifestEvaluator(manifest, executor, semantic_evaluator=FakeSemantic())
    result = evaluator.evaluate(workspace)

    assert not result.passed
    assert any("must mention a term that is absent" in f for f in result.failures)


# ---------------------------------------------------------------------------
# Prerequisite modes
# ---------------------------------------------------------------------------


def test_enforce_mode_no_override_prompt() -> None:
    """enforce mode issues the baseline synthetic-run prompt."""
    manifest = Manifest(
        id="x",
        title="x",
        skills_under_test=["next-move"],
        execution={"prerequisite_mode": "enforce", "classification": "missing_input"},
        fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
    )
    prompt = ManifestEvaluator(manifest, executor=None)._extra_prompt_for_mode()
    assert "skip" not in prompt.lower()
    assert "synthesized" not in prompt.lower()


def test_explicit_override_adds_skip_instruction() -> None:
    """explicit_override tells the skill it may skip missing upstream docs."""
    manifest = Manifest(
        id="x",
        title="x",
        skills_under_test=["poc-plan"],
        execution={"prerequisite_mode": "explicit_override", "classification": "degraded"},
        fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
    )
    prompt = ManifestEvaluator(manifest, executor=None)._extra_prompt_for_mode()
    assert "skip" in prompt.lower()
    assert "missing upstream" in prompt.lower()


def test_provide_fixtures_prompt() -> None:
    """provide_fixtures tells the skill that upstream docs are already synthesized."""
    manifest = Manifest(
        id="x",
        title="x",
        skills_under_test=["poc-plan"],
        execution={"prerequisite_mode": "provide_fixtures", "classification": "normal"},
        fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
    )
    prompt = ManifestEvaluator(manifest, executor=None)._extra_prompt_for_mode()
    assert "upstream qualification documents have been synthesized" in prompt.lower()


def test_provide_fixtures_generates_upstream_docs(
    tmp_path: Path, repo_root: Path
) -> None:
    """provide_fixtures mode creates the docs a downstream skill needs."""
    manifest = Manifest(
        id="x",
        title="x",
        skills_under_test=["poc-plan"],
        execution={"prerequisite_mode": "provide_fixtures", "classification": "normal"},
        fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
    )
    workspace = WorkspaceBuilder(manifest, tmp_path / "work", repo_root).build()

    outputs = workspace.customer_dir / "outputs"
    for upstream in ("biz-qual", "deployment-qual", "tech-qual", "connector-feasibility"):
        matches = list((outputs / upstream).glob(f"{upstream}-*.md"))
        assert matches, f"missing synthetic {upstream} document"


def test_provide_fixtures_prefers_existing_outputs(
    tmp_path: Path, repo_root: Path
) -> None:
    """If fixtures.existing_outputs provides an upstream doc, it is not overwritten."""
    manifest_path = tmp_path / "manifest.yaml"
    _write_minimal_manifest(
        manifest_path,
        ["poc-plan"],
        prerequisite_mode="provide_fixtures",
        existing_outputs=[
            {
                "source": "fixtures/outputs/hourly-biz-qual.md",
                "target": "customers/Acme/outputs/biz-qual/biz-qual-2026-07-01.md",
            }
        ],
    )
    manifest = Manifest.from_yaml(manifest_path)
    workspace = WorkspaceBuilder(manifest, tmp_path / "work", repo_root).build()

    dest = workspace.customer_dir / "outputs" / "biz-qual" / "biz-qual-2026-07-01.md"
    assert dest.exists()
    assert "Acme" in dest.read_text(encoding="utf-8")

    # The auto-generator should see the existing biz-qual doc and not add another.
    matches = list((workspace.customer_dir / "outputs" / "biz-qual").glob("biz-qual-*.md"))
    assert len(matches) == 1


def test_override_reported_in_result(
    tmp_path: Path, repo_root: Path
) -> None:
    """explicit_override is flagged in the result and report."""
    manifest = Manifest(
        id="x",
        title="x",
        skills_under_test=["next-move"],
        execution={"prerequisite_mode": "explicit_override", "classification": "degraded"},
        fixtures={"config": "fixtures/config/synthetic-se-config.yaml"},
        expected_sections=["Source Coverage"],
    )
    workspace = WorkspaceBuilder(manifest, tmp_path / "work", repo_root).build()
    executor = MockExecutor()
    result = ManifestEvaluator(manifest, executor).evaluate(workspace)

    assert result.override_used is True
    assert result.prerequisite_mode == "explicit_override"
    assert result.classification == "degraded"


def test_refusal_counts_as_pass_when_expected(
    tmp_path: Path, repo_root: Path
) -> None:
    """A refused skill matching expected_refusal_for does not fail the scenario."""
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "manifest_version": "1.0",
                "id": "missing-tech",
                "title": "Missing tech",
                "skills_under_test": ["tech-qual"],
                "fixtures": {"config": "fixtures/config/synthetic-se-config.yaml"},
                "execution": {"prerequisite_mode": "enforce", "classification": "missing_input"},
                "expected_refusal_for": ["tech-qual"],
                "expected_sections": ["Source Coverage"],
                "deterministic_assertions": [
                    {
                        "name": "refuses",
                        "target": "markdown",
                        "check": "contains_case_insensitive(output, 'refuse') or contains_case_insensitive(output, 'cannot generate')",
                        "severity": "blocker",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest = Manifest.from_yaml(manifest_path)
    workspace = WorkspaceBuilder(manifest, tmp_path / "work", repo_root).build()
    executor = MockExecutor()
    result = ManifestEvaluator(manifest, executor).evaluate(workspace)

    assert result.passed
    assert result.skill_results[0].refused is True


# ---------------------------------------------------------------------------
# Approved temp root helpers
# ---------------------------------------------------------------------------


def test_approved_temp_roots_include_pytest_tmp(tmp_path: Path) -> None:
    """The temp-dir policy accepts pytest's tmp_path."""
    assert _is_approved_temp_dir(tmp_path) is True


def test_approved_temp_dir_rejects_se_skills() -> None:
    """The real customer workspace path is rejected."""
    assert _is_approved_temp_dir(Path.home() / ".se-skills") is False
