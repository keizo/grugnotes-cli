from __future__ import annotations

import errno
import os
from pathlib import Path

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
NOFOLLOW_OPEN_FLAG = getattr(os, "O_NOFOLLOW", 0)


def ensure_permissions(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def ensure_private_dir(path: Path, *, stop_at: Path | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    stop_resolved = stop_at.resolve() if stop_at is not None else None
    current = path
    while True:
        ensure_permissions(current, PRIVATE_DIR_MODE)
        if stop_resolved is not None and current.resolve() == stop_resolved:
            break
        parent = current.parent
        if parent == current or stop_resolved is None:
            break
        current = parent


def write_private_text(
    path: Path,
    text: str,
    *,
    stop_at: Path | None = None,
    newline: str | None = None,
) -> None:
    ensure_private_dir(path.parent, stop_at=stop_at)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | NOFOLLOW_OPEN_FLAG
    try:
        fd = os.open(path, flags, PRIVATE_FILE_MODE)
    except OSError as exc:
        if NOFOLLOW_OPEN_FLAG and exc.errno == errno.ELOOP:
            raise ValueError(f"Refusing to follow symlinked file: {path}") from exc
        raise
    with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as handle:
        handle.write(text)
    ensure_permissions(path, PRIVATE_FILE_MODE)
