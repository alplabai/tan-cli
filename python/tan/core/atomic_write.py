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

This is the SAME durability + symlink-safety + mode-preservation shape
`debug_config_cmd._atomic_write_launch_json` carried (tan-cli#489, review
findings 4+5) for the identical reason -- that function's own opening comment
already called its shape "matching `bootstrap_cmd.reconcile_west_manifest_
path`'s own pattern", so the two were always meant to agree; #516 is that
agreement breaking (the fsync fix landed on one call site, not the other, and
outlived it). Extracted here rather than re-copied inline a second time, so a
future edit to the durability sequence itself cannot re-diverge between call
sites the way it just did -- tan-cli#510 was exactly this failure mode: two
independent copies of a resolver that drifted apart. `debug_config_cmd.py`'s
own copy is gone as of the #516 review round -- `_write` now calls this
function directly, rather than keeping a second, hand-synchronised
implementation beside it -- so the docstring's promise and the code agree.

**Mode preservation mirrors `_atomic_write_launch_json` exactly**, because
this helper now serves BOTH call sites and one of them (`.vscode/
launch.json`) genuinely needs it: a `664`-mode file a shared checkout
convention set deliberately must not come back at `mkstemp`'s hardcoded
`0600` merely because it was rewritten. `.west/config` -- this helper's other
caller -- has no such convention to preserve, but reading a mode that already
exists and reapplying it after the swap is a no-op cost for that caller, not
a hazard, so one shape serves both rather than growing a caller-specific
branch.
"""

from __future__ import annotations

import os
import stat
import tempfile


def atomic_write_text(path: str, content: str, *, encoding: str = "utf-8") -> None:
    """Write `content` to `path` atomically and durably. Raises on any
    failure; the caller decides how to report it.

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
    * **Mode-preserving.** The existing file's permission bits (`stat.S_IMODE`,
      read BEFORE the write) are re-applied to the real path AFTER a
      successful `os.replace` -- never to the temp file itself, since
      `os.replace` swaps the directory entry, not the inode's own mode bits,
      and `mkstemp`'s hardcoded `0600` would otherwise silently narrow
      whatever the file used to be. A first-ever write (no existing file to
      read a mode from) gets `0666 & ~umask` instead of keeping `mkstemp`'s
      `0600` forever, so it starts out at the ordinary default a plain
      `open(path, "w")` would have produced.
    * **Cleans up its own temp on failure** -- the caller never needs a
      matching `unlink` in its own `except` block. This covers more than
      `OSError`: an unencodable `content` under `encoding` raises
      `UnicodeEncodeError` (a `ValueError`) from `handle.write`, and an
      unknown `encoding=` raises `LookupError` from `os.fdopen` itself --
      both happen strictly between `mkstemp` creating the temp and the
      rename, so both must unlink it too, or it leaks into the caller's
      directory un-unlinked.
    """
    resolved = os.path.realpath(path)
    directory = os.path.dirname(resolved)
    existing_mode: int | None = None
    try:
        existing_mode = stat.S_IMODE(os.stat(resolved).st_mode)
    except OSError:
        pass
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tan-tmp")
    try:
        try:
            handle = os.fdopen(fd, "w", encoding=encoding, newline="")
        except (OSError, LookupError):
            # `fdopen` failing before a file object takes ownership of `fd`
            # would otherwise leak the raw descriptor `mkstemp` opened -- an
            # unknown `encoding=` fails here, as a `LookupError`, not inside
            # the `with` block below.
            os.close(fd)
            raise
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, resolved)
    except (OSError, ValueError, LookupError):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    if existing_mode is not None:
        try:
            os.chmod(resolved, existing_mode)
        except OSError:
            pass
    else:
        try:
            # No race-free way to READ the process umask -- the standard
            # idiom sets a harmless value and reads back the PREVIOUS one,
            # then restores it immediately. `os.umask` is PROCESS-global, so
            # this brief set-then-restore is safe only under tan's own
            # single-threaded use of this helper; a second thread doing
            # anything umask-sensitive inside this same window would observe
            # -- or set -- the wrong value.
            current_umask = os.umask(0o022)
            os.umask(current_umask)
            os.chmod(resolved, 0o666 & ~current_umask)
        except OSError:
            pass
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
