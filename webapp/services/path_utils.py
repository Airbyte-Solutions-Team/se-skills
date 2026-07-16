"""Filesystem containment helpers for the webapp services layer."""

from __future__ import annotations

from pathlib import Path


def resolve_within(root: Path, path: str | Path) -> Path:
    """Resolve `path` under `root` and confirm it stays inside.

    Symlinks are followed by `Path.resolve()`. If the resolved path is not a
    descendant of `root`, a `ValueError` is raised.

    The existence of the final path is not checked here; callers that need an
    existing file or directory should verify that separately.
    """
    root_resolved = Path(root).resolve()
    candidate = (root_resolved / path).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"Path {path!r} escapes the allowed directory {root_resolved}") from exc
    return candidate
