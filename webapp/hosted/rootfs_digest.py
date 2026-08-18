"""Canonical digest for a materialized sandbox root filesystem."""
from __future__ import annotations

import hashlib
import stat
import sys
from pathlib import Path


def digest_rootfs(rootfs: Path) -> str:
    """Hash every rootfs entry's identity, metadata, and file content."""
    if not rootfs.is_dir():
        raise ValueError("rootfs must be a directory")

    digest = hashlib.sha256()
    entries = sorted(
        (path for path in rootfs.rglob("*")),
        key=lambda path: path.relative_to(rootfs).as_posix(),
    )
    for path in entries:
        relative = path.relative_to(rootfs).as_posix()
        metadata = path.lstat()
        entry_type = _entry_type(metadata.st_mode)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry_type.encode("ascii"))
        digest.update(b"\0")
        digest.update(f"{stat.S_IMODE(metadata.st_mode):o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(str(metadata.st_uid).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(metadata.st_gid).encode("ascii"))
        digest.update(b"\0")
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(path.readlink().as_posix().encode("utf-8"))
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(_file_digest(path))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _entry_type(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _file_digest(path: Path) -> bytes:
    file_digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            file_digest.update(chunk)
    return file_digest.digest()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m webapp.hosted.rootfs_digest ROOTFS")
    print(digest_rootfs(Path(sys.argv[1])))
