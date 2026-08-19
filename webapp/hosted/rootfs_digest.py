"""Canonical digest for a materialized sandbox root filesystem."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import stat
import sys
import tarfile
import tempfile
from typing import BinaryIO


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


def digest_rootfs_tar(stream: BinaryIO) -> str:
    """Hash a Docker-export tar stream using the materialized-tree contract."""
    entries: list[tuple[str, str, int, int, int, str | None, bytes | None]] = []
    content_digests: dict[str, bytes] = {}
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            relative = member.name.removeprefix("./").rstrip("/")
            if not relative:
                continue
            entry_type = (
                "directory"
                if member.isdir()
                else "file"
                if member.isreg() or member.islnk()
                else "symlink"
                if member.issym()
                else "other"
            )
            target = member.linkname if member.issym() else None
            content = None
            if member.isreg():
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError("regular tar entry has no content")
                content = _bytes_digest(extracted)
                content_digests[relative] = content
            elif member.islnk():
                content = content_digests.get(member.linkname)
                if content is None:
                    raise ValueError("hardlink target is missing or comes later")
            entries.append(
                (
                    relative,
                    entry_type,
                    stat.S_IMODE(member.mode),
                    member.uid,
                    member.gid,
                    target,
                    content,
                )
            )

    digest = hashlib.sha256()
    for relative, entry_type, mode, uid, gid, target, content in sorted(
        entries, key=lambda item: item[0]
    ):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry_type.encode("ascii"))
        digest.update(b"\0")
        digest.update(f"{mode:o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(str(uid).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(gid).encode("ascii"))
        digest.update(b"\0")
        if target is not None:
            digest.update(target.encode("utf-8"))
        elif content is not None:
            digest.update(content)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def normalize_rootfs_tar(source: BinaryIO, destination: BinaryIO) -> None:
    """Write a deterministic tar stream while preserving numeric metadata."""
    with tempfile.TemporaryDirectory(prefix="se-skills-rootfs-") as temp_dir:
        members: list[tuple[tarfile.TarInfo, Path | None]] = []
        with tarfile.open(fileobj=source, mode="r|") as archive:
            for index, member in enumerate(archive):
                relative = member.name.removeprefix("./").rstrip("/")
                if not relative:
                    continue
                member.name = relative
                member.mtime = 0
                content_path = None
                if member.isreg():
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise ValueError("regular tar entry has no content")
                    content_path = Path(temp_dir) / str(index)
                    with content_path.open("wb") as content:
                        shutil.copyfileobj(extracted, content)
                members.append((member, content_path))
        with tarfile.open(
            fileobj=destination, mode="w|", format=tarfile.PAX_FORMAT
        ) as archive:
            for member, content_path in sorted(
                members, key=lambda item: item[0].name
            ):
                if content_path is None:
                    archive.addfile(member)
                else:
                    with content_path.open("rb") as content:
                        archive.addfile(member, content)


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


def _bytes_digest(stream: BinaryIO) -> bytes:
    file_digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        file_digest.update(chunk)
    return file_digest.digest()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--tar":
        with sys.stdin.buffer as stream:
            print(digest_rootfs_tar(stream))
    elif len(sys.argv) == 4 and sys.argv[1] == "--normalize-tar":
        with Path(sys.argv[2]).open("rb") as source, Path(sys.argv[3]).open("wb") as destination:
            normalize_rootfs_tar(source, destination)
    elif len(sys.argv) == 2:
        print(digest_rootfs(Path(sys.argv[1])))
    else:
        raise SystemExit(
            "usage: python -m webapp.hosted.rootfs_digest ROOTFS | --tar -"
        )
