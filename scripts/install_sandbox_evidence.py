#!/usr/bin/env python3
"""Install a released sandbox evidence bundle under a trusted host root."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path

from webapp.hosted.supply_chain_manifest import CANONICAL_IMAGE_DIGEST

MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _ensure_trusted_directory(
    path: Path, owner_uid_fn: Callable[[Path], int]
) -> None:
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    if owner_uid_fn(path) != 0:
        raise PermissionError(f"trusted directory is not root-owned: {path}")
    path.chmod(0o755)


def _resolved_child(root: Path, child: Path) -> Path:
    resolved_root = root.resolve()
    resolved_child = child.resolve(strict=False)
    try:
        resolved_child.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("evidence path escapes the trusted root") from exc
    return resolved_child


def _read_source(path: Path) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"evidence source cannot be opened safely: {path}") from exc
    try:
        facts = os.fstat(descriptor)
        if not stat.S_ISREG(facts.st_mode):
            raise ValueError(f"evidence source is not a regular file: {path}")
        if facts.st_size > MAX_EVIDENCE_BYTES:
            raise ValueError(f"evidence source exceeds {MAX_EVIDENCE_BYTES} bytes: {path}")
        chunks: list[bytes] = []
        remaining = facts.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"evidence source changed while reading: {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _atomic_install(
    path: Path,
    content: bytes,
    owner_uid_fn: Callable[[Path], int],
) -> None:
    expected_hash = hashlib.sha256(content).hexdigest()

    def existing_is_identical() -> bool:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"existing evidence destination is not a regular file: {path}")
        if owner_uid_fn(path) != 0:
            raise PermissionError(f"existing evidence destination is not root-owned: {path}")
        existing = _read_source(path)
        if hashlib.sha256(existing).hexdigest() != expected_hash:
            raise FileExistsError(
                f"refusing to replace different evidence at {path}; rotate or remove it explicitly"
            )
        return True

    if path.exists() or path.is_symlink():
        existing_is_identical()
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o644,
    )
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            existing_is_identical()
        else:
            os.unlink(temporary)
            path.chmod(0o644)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


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
    if not isinstance(digest, str) or not CANONICAL_IMAGE_DIGEST.fullmatch(digest):
        raise ValueError("published manifest must contain a sha256 image digest")
    trusted_root = output_manifest.parent.resolve()
    resolved_evidence_root = _resolved_child(trusted_root, evidence_root)
    digest_dir = _resolved_child(
        resolved_evidence_root,
        resolved_evidence_root / digest.removeprefix("sha256:"),
    )
    resolved_output = _resolved_child(trusted_root, output_manifest)
    _ensure_trusted_directory(trusted_root, owner_uid_fn)
    _ensure_trusted_directory(resolved_evidence_root, owner_uid_fn)
    _ensure_trusted_directory(digest_dir, owner_uid_fn)
    installed = dict(manifest)
    for field in ("sbom_name", "provenance_name"):
        name = manifest.get(field)
        if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
            raise ValueError(f"manifest {field} is missing or unsafe")
        source = _resolved_child(bundle_dir.resolve(), bundle_dir / name)
        destination = _resolved_child(digest_dir, digest_dir / name)
        content = _read_source(source)
        _atomic_install(destination, content, owner_uid_fn)
        installed[field.removesuffix("_name") + "_path"] = str(destination)
    _ensure_trusted_directory(resolved_output.parent, owner_uid_fn)
    manifest_bytes = (
        json.dumps(installed, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    _atomic_install(resolved_output, manifest_bytes, owner_uid_fn)
    return resolved_output


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
