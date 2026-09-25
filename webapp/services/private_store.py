"""Private on-disk primitives shared by the Command Center local stores.

Directories are created 0700 and files 0600 on POSIX; on Windows `os.chmod`
cannot express this and the stores rely on inherited user-profile ACLs.
`exclusive_file_lock` is an advisory cross-process lock (`fcntl.flock` on POSIX,
`msvcrt.locking` on Windows) that callers combine with an in-process lock.
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


DIR_MODE = 0o700
FILE_MODE = 0o600


def mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    if os.name == "posix":
        os.chmod(path, DIR_MODE)


def atomic_write_private(path: Path, payload: bytes) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(temp, FILE_MODE)
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def exclusive_file_lock(lock_path: Path) -> Iterator[None]:
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, FILE_MODE)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)
