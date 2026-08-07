# SPDX-License-Identifier: Apache-2.0
"""A durable atomic-write helper for a text file more than one process or SDK
checkout can share -- fsync'd before the rename, not just atomic-in-naming.

tan-cli#516: `bootstrap_cmd.reconcile_west_manifest_path` wrote its temp
sibling with `Path.write_text` and a bare `os.replace`, with no `os.fsync` on
the temp before the rename and no directory `fsync` after it. `os.replace`'s
atomicity guarantee covers the RENAME only -- it says nothing about whether
the renamed-to content has actually reached stable storage. A crash between
the rename landing and the temp's data blocks flushing can leave `.west/
config` existing under its real name with truncated or missing content, on
any filesystem that does not happen to order the two together: ext4's
`auto_da_alloc` heuristic covers the common Linux case; XFS, btrfs, APFS,
NTFS via `MoveFileExW`, and network filesystems generally do not.

This is the SAME durability + symlink-safety shape
`debug_config_cmd._atomic_write_launch_json` already carries (tan-cli#489,
review findings 4+5) for the identical reason -- that function's own opening
comment already called its shape "matching `bootstrap_cmd.reconcile_west_
manifest_path`'s own pattern", so the two were always meant to agree; #516 is
that agreement breaking (the fsync fix landed on one call site, not the other,
and outlived it). Extracted here rather than re-copied inline a second time,
so a future edit to the durability sequence itself cannot re-diverge between
call sites the way it just did -- tan-cli#510 was exactly this failure mode:
two independent copies of a resolver that drifted apart.

**Mode/ACL and umask handling are deliberately NOT reproduced here.** Unlike
`.vscode/launch.json` -- a file a shared checkout convention may set to a
non-default mode the write must preserve across the inode swap, and which
`_atomic_write_launch_json` may be creating for the very first time -- this
helper's only caller rewrites an EXISTING `.west/config` `west init` itself
already created (with the process umask, on a real `west init -l`), so there
is no first-write case to default sensibly for and no observed narrower
convention to preserve. A caller that later needs either can still reach for
`_atomic_write_launch_json`'s fuller shape rather than growing this one to
match a requirement it does not have.
"""

from __future__ import annotations

import os
import tempfile


def atomic_write_text(path: str, content: str, *, encoding: str = "utf-8") -> None:
    """Write `content` to `path` atomically and durably. Raises `OSError` on
    any failure; the caller decides how to report it.

    * **Symlink-safe.** `os.path.realpath` resolves the real target FIRST,
      both for where the temp sibling is created (the same directory, and so
      the same filesystem -- `os.replace` requires that for its atomicity
      guarantee and fails outright with `EXDEV` without it) and for what
      `os.replace` itself targets: writing the temp beside the UNresolved
      `path` and replacing that would put a regular file where a symlink was,
      destroying the link, while the real file silently stops being updated.
    * **Durable, not just atomic-in-naming.** `os.fsync` on the temp file's
      own descriptor, before the rename, gets the CONTENT onto stable
      storage; a POSIX directory `fsync` after the rename covers the RENAME
      ENTRY surviving a crash too (Windows has no directory handle to fsync,
      and `ReplaceFile`/`MoveFileExW` already journal the rename itself).
    * **Cleans up its own temp on failure** -- the caller never needs a
      matching `unlink` in its own `except` block.
    """
    resolved = os.path.realpath(path)
    directory = os.path.dirname(resolved)
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tan-tmp")
    try:
        try:
            handle = os.fdopen(fd, "w", encoding=encoding, newline="")
        except OSError:
            # `fdopen` failing before a file object takes ownership of `fd`
            # would otherwise leak the raw descriptor `mkstemp` opened.
            os.close(fd)
            raise
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, resolved)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    if os.name != "nt":
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Best-effort insurance for the RENAME entry surviving a crash, on
            # top of the file's own fsync above (which already protects the
            # content): some filesystems/mounts (overlayfs, certain FUSE/NFS
            # setups) refuse a bare directory fsync outright.
            pass
