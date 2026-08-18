#!/usr/bin/env python3
"""Install a released sandbox evidence bundle under a trusted host root."""
from __future__ import annotations

import argparse
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path


def _ensure_trusted_directory(
    path: Path, owner_uid_fn: Callable[[Path], int]
) -> None:
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    if owner_uid_fn(path) != 0:
        raise PermissionError(f"trusted directory is not root-owned: {path}")
    path.chmod(0o755)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=Path("/etc/se-skills/evidence"),
    )
    parser.add_argument("--output-manifest", type=Path, required=True)
    return parser.parse_args()


def install_evidence(
    bundle_dir: Path,
    manifest_path: Path,
    evidence_root: Path,
    output_manifest: Path,
    geteuid_fn: Callable[[], int] = os.geteuid,
    owner_uid_fn: Callable[[Path], int] = lambda path: path.stat().st_uid,
) -> Path:
    """Copy named release evidence into a digest-specific trusted directory."""
    if geteuid_fn() != 0:
        raise PermissionError("sandbox evidence installation must run as root")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = manifest.get("image_digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ValueError("published manifest must contain a sha256 image digest")
    digest_dir = evidence_root / digest.removeprefix("sha256:")
    _ensure_trusted_directory(evidence_root, owner_uid_fn)
    _ensure_trusted_directory(digest_dir, owner_uid_fn)
    installed = dict(manifest)
    for field in ("sbom_name", "provenance_name"):
        name = manifest.get(field)
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError(f"manifest {field} is missing or unsafe")
        source = bundle_dir / name
        if source.is_symlink() or not source.is_file():
            raise FileNotFoundError(source)
        destination = digest_dir / name
        if destination.is_symlink():
            raise ValueError(f"destination {destination} is unsafe")
        if destination.exists() and owner_uid_fn(destination) != 0:
            raise PermissionError(f"evidence destination is not root-owned: {destination}")
        shutil.copyfile(source, destination)
        destination.chmod(0o644)
        installed[field.removesuffix("_name") + "_path"] = str(destination)
    _ensure_trusted_directory(output_manifest.parent, owner_uid_fn)
    output_manifest.write_text(
        json.dumps(installed, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    output_manifest.chmod(0o644)
    return output_manifest


def main() -> int:
    args = _parse_args()
    install_evidence(
        args.bundle_dir,
        args.manifest,
        args.evidence_root,
        args.output_manifest,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
