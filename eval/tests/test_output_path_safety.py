"""Path-safety tests for the output lifecycle.

These tests verify that `OutputService` (and the routes that delegate to it)
reject traversal, absolute paths, sibling-prefix trickery, and symlink escapes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import routes.outputs as outputs_routes
from routes.outputs import OutputDiff
from services.output_service import OutputError, OutputService


def _svc(customers_dir: Path) -> OutputService:
    return OutputService(
        customers_dir=customers_dir,
        workspace=customers_dir,
        repo_root=customers_dir,
        se_config=lambda: {},
        safe_name=lambda n: n,
        slug=lambda n: n.replace(" ", "-").lower(),
        run_cmd=None,
        internal_repo=None,
    )


def _make_md(customers_dir: Path, rel: str) -> Path:
    f = customers_dir / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# Safe\n", encoding="utf-8")
    return f


def test_valid_nested_output_path(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    rel = "Acme/outputs/skill/file.md"
    _make_md(tmp_path, rel)
    assert "# Safe" in svc.read_output_content(rel)


def test_traversal_rejected(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    with pytest.raises(OutputError) as exc:
        svc.read_output_content("../etc/passwd")
    assert exc.value.status_code == 404


def test_absolute_path_rejected(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    with pytest.raises(OutputError) as exc:
        svc.read_output_content("/etc/passwd")
    assert exc.value.status_code == 404


def test_sibling_prefix_directory_rejected(tmp_path: Path) -> None:
    """A directory named `customers-archive` next to `customers` must not match."""
    root = tmp_path / "customers"
    root.mkdir()
    sibling = tmp_path / "customers-archive"
    sibling.mkdir()
    (sibling / "secret.md").write_text("# leaked\n", encoding="utf-8")

    svc = _svc(root)
    with pytest.raises(OutputError) as exc:
        svc.read_output_content("../customers-archive/secret.md")
    assert exc.value.status_code == 404


def test_symlinked_file_outside_rejected(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    rel = "Acme/outputs/skill/file.md"
    _make_md(tmp_path, rel)

    outside = tmp_path.parent / "outside.md"
    outside.write_text("# outside\n", encoding="utf-8")
    link = tmp_path / "Acme" / "outputs" / "skill" / "link.md"
    link.symlink_to(outside)

    with pytest.raises(OutputError) as exc:
        svc.read_output_content("Acme/outputs/skill/link.md")
    assert exc.value.status_code == 404


def test_symlinked_directory_outside_rejected(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    outside_dir = tmp_path.parent / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "file.md").write_text("# outside\n", encoding="utf-8")

    link_dir = tmp_path / "Acme" / "outputs" / "skill-dir"
    link_dir.parent.mkdir(parents=True, exist_ok=True)
    link_dir.symlink_to(outside_dir)

    with pytest.raises(OutputError) as exc:
        svc.read_output_content("Acme/outputs/skill-dir/file.md")
    assert exc.value.status_code == 404


def test_delete_outside_workspace_rejected(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    with pytest.raises(OutputError) as exc:
        svc.delete_output("../escape.md")
    assert exc.value.status_code == 404


def test_delete_symlinked_outside_rejected(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    outside = tmp_path.parent / "outside.md"
    outside.write_text("# outside\n", encoding="utf-8")
    link = tmp_path / "escape.md"
    link.symlink_to(outside)
    with pytest.raises(OutputError) as exc:
        svc.delete_output("escape.md")
    assert exc.value.status_code == 404


def test_download_outside_workspace_rejected(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    with pytest.raises(OutputError) as exc:
        svc.export_internal_html("../escape.md")
    assert exc.value.status_code == 404


def test_diff_with_outside_path_rejected(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    left = "Acme/outputs/skill/file.md"
    _make_md(tmp_path, left)
    with pytest.raises(OutputError) as exc:
        svc.diff_outputs(OutputDiff(left=left, right="../customers-archive/secret.md"))
    assert exc.value.status_code == 404


def test_route_read_rejects_traversal(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(output_service=svc, feedback_service=None)))
    with pytest.raises(Exception) as exc:
        outputs_routes.api_output_content("../etc/passwd", req)
    # FastAPI route wraps OutputError in HTTPException.
    assert exc.value.status_code == 404


def test_route_delete_rejects_traversal(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(output_service=svc, feedback_service=None)))
    with pytest.raises(Exception) as exc:
        outputs_routes.api_delete_output("../etc/passwd", req)
    assert exc.value.status_code == 404


def test_route_diff_rejects_outside_path(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    left = "Acme/outputs/skill/file.md"
    _make_md(tmp_path, left)
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(output_service=svc, feedback_service=None)))
    with pytest.raises(Exception) as exc:
        outputs_routes.api_output_diff(OutputDiff(left=left, right="/etc/passwd"), req)
    assert exc.value.status_code == 404
