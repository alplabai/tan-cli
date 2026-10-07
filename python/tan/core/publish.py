# SPDX-License-Identifier: Apache-2.0
"""Publish a finished temp file to its final name without ever overwriting or
following a link -- shared by `tan model add` and `tan model prep`."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def publish_exclusive(tmp: Path, dest: Path) -> None:
    """Move the verified temp file to `dest` without ever overwriting or
    following a link: a hard link fails if `dest` exists (even dangling); on a
    filesystem without hard links, `O_EXCL|O_NOFOLLOW` creates it instead.
    Raises `FileExistsError` / `OSError`."""
    try:
        os.link(tmp, dest)
        return
    except FileExistsError:
        raise
    except (OSError, NotImplementedError):
        pass
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(dest, flags, 0o644)
    try:
        with open(tmp, "rb") as src, os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(src, out)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
