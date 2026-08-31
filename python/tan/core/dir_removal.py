# SPDX-License-Identifier: Apache-2.0
"""Shared, hardened directory-removal primitives (tan-cli#790).

**Extracted from `tan.commands.clean_cmd`, verbatim logic**, where these six
names (`is_link`, `os_error_text`, `_reraise_removal_failure`,
`_retry_after_clearing_readonly`, `_RMTREE_HOOK`, `remove_dir`) first landed
for `tan clean` and were battle-tested against the frozen v0.4.1 oracle on
real read-only build outputs, real junctions, and real symlinked build roots
(see each function's own docstring for the specific oracle-diff that shaped
it). `tan sdk remove` needs the identical hardening -- a real `tan bootstrap`
tree can carry a read-only `.git/objects/**` entry from a shallow clone, and
neither command's job is to reinvent "how does this host actually let me
delete a stubborn directory" independently.

**Lives in `tan.core`, not a second copy in `tan.commands.sdk_cmd`, and not
left where it was**, for the same reason `sdk_discovery.py`'s own module
docstring gives for why IT moved out of a command module:  `tan.core` may
import no `tan.commands.*` module (the invariant `tan.core.shapes` and
`tan.core.sdk_discovery` already state and rely on), so a `tan/core/`
caller — this module has none yet, but `tan.core.sdk_removal` (tan-cli#790)
is the first — could not reach these primitives while they lived inside
`clean_cmd.py` without creating exactly the `tan.core` -> `tan.commands`
inversion that rule exists to forbid.

`clean_cmd.py` re-imports every one of these six names at its own module
level (`from tan.core.dir_removal import ...`) rather than losing them: its
own internal call sites still say the bare `_remove_dir(...)` /
`_retry_after_clearing_readonly(...)`, resolved through `clean_cmd`'s own
module globals at call time, so `monkeypatch.setattr(clean_cmd, "_remove_dir",
...)` in the existing test suite keeps working unchanged -- the monkeypatch
only cares which module attribute it is overwriting, never which module
originally defined the value that attribute pointed at.
"""
from __future__ import annotations

import os
import shutil
import stat
import sys


def is_link(path: str) -> bool:
    """Whether `path` is a link that must not be followed -- a POSIX symlink, a
    Windows directory symlink, OR a Windows JUNCTION.

    **`os.path.islink` is not this test.** On Windows `ntpath.islink` returns
    True only for `IO_REPARSE_TAG_SYMLINK`; a junction is
    `IO_REPARSE_TAG_MOUNT_POINT`, and `stat.S_ISLNK` is False for it as well.
    Measured on this host: for `build/` junctioned at an out-of-tree directory,
    `os.path.islink` and `S_ISLNK` both report False while
    `st_reparse_tag == IO_REPARSE_TAG_MOUNT_POINT`. A guard written on
    `os.path.islink` therefore lets a junction reach `shutil.rmtree` -- which
    has its OWN, correct check (`shutil._rmtree_islink`, mirrored here) and
    refuses, so nothing outside the tree is destroyed, but the junction is then
    never cleaned and the run reports a spurious `remove-failed`. This was a
    live defect in the first cut of `tan clean`'s port, caught only by diffing
    against the Rust binary.
    """
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attributes = getattr(st, "st_file_attributes", 0)
    return bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        and getattr(st, "st_reparse_tag", 0)
        == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)
    )


def os_error_text(err: BaseException) -> str:
    """An `OSError` rendered the way Rust's `io::Error` Display renders it:
    `<system message> (os error <code>)`.

    Python's own `str(OSError)` is `[WinError 32] <message>: '<path>'`, which
    both differs from the oracle and repeats a path the message already names.
    The Windows error code (`winerror`) is preferred over the translated
    `errno`, matching Rust, which reports the raw OS code.

    One character still differs on Windows: `FormatMessageW` ends its sentences
    with a period and Rust keeps it, while Python's `strerror` strips it. Not
    synthesized here -- guessing at punctuation inside a system message is worse
    than a documented one-character divergence in a warning string.
    """
    if not isinstance(err, OSError):
        return str(err)
    code = getattr(err, "winerror", None) or err.errno
    if err.strerror is None or code is None:
        return str(err)
    return f"{err.strerror} (os error {code})"


#: The two functions `shutil.rmtree` hands its error hook that CANNOT be called
#: with one positional argument. On POSIX `rmtree` runs the fd-based
#: `_rmtree_safe_fd` walk, which reports failures of `os.open` (shutil 3.12
#: lines 682 and 781) and `os.close` (692/712/791/808) through the same hook as
#: the one-argument `os.scandir`/`os.unlink`/`os.rmdir`/`os.lstat`. `os.open`
#: needs `flags` and `os.close` takes an fd, not the path the hook is handed --
#: so retrying either is a `TypeError`, not a repair. Windows' `_rmtree_unsafe`
#: never passes these, which is why the crash class this guards is POSIX-only.
_NOT_RETRYABLE_WITH_PATH_ALONE = frozenset({os.open, os.close})


def _reraise_removal_failure(func, path, exc) -> None:
    """Re-raise the failure `shutil.rmtree` reported, so the caller's
    `except (OSError, ValueError)` sees it and can report it as a removal
    failure rather than a silent no-op.

    `exc` is the exception under `onexc` and an `exc_info` TUPLE under the
    deprecated `onerror` (see `_RMTREE_HOOK`), so both shapes are unwrapped
    here. A hook that has nothing to re-raise still must not return quietly --
    `rmtree` would then report the tree as removed -- so the fallback states the
    operation that failed.
    """
    if isinstance(exc, tuple) and len(exc) == 3:
        exc = exc[1]
    if isinstance(exc, BaseException):
        raise exc
    raise OSError(f"{getattr(func, '__name__', func)} failed on {path}")


def _retry_after_clearing_readonly(func, path, exc=None) -> None:
    """`shutil.rmtree` error hook: clear the read-only bit and retry once.

    Rust's `remove_dir_all` deletes a read-only file on Windows outright (it
    passes `FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE`), where `shutil.rmtree`
    fails the WHOLE tree with `[WinError 5] Access is denied`. Measured against
    the Rust binary, from `tan clean`'s own port: one read-only file inside
    `build/` had Rust remove the build dir and exit 0 while a naive port left
    every artefact in place and warned. Read-only build outputs -- and, for
    `tan sdk remove`'s own caller, read-only `.git/objects/**` entries left by
    a shallow `git clone` -- are ORDINARY, not exotic, so this is the primary
    path here, not a fallback.

    `st_mode | S_IWUSR` rather than a bare `S_IWRITE`: on POSIX the latter would
    replace the whole mode with `0o200` and strip the owner's read/execute bits
    from a directory mid-walk. A failure here propagates out of `rmtree` and is
    the caller's to report.

    The retry is only attempted for a `func` that a path alone can drive
    ([`_NOT_RETRYABLE_WITH_PATH_ALONE`]). It used to end in a bare `func(path)`,
    so a build directory the invoking user OWNS but cannot open -- a
    `chmod -R a-r` or tar-preserved tree, where `os.chmod` SUCCEEDS and
    `os.open` is what failed -- raised `TypeError: open() missing required
    argument 'flags' (pos 2)`. That is neither `OSError` nor `ValueError`, so it
    used to sail past `tan clean`'s best-effort guard, abort the target loop and
    hit its outer catch-all: exit 5, `clean.internal-failure`, and every
    remaining target silently skipped. Measured on the oracle for the same
    tree: exit 0, a warning, and the rest of the run completing -- which is
    what re-raising the original failure (rather than a `TypeError`) restores.

    **Also clears the PARENT directory's own write bit** (tan-cli#790,
    `sdk.remove`'s own maintainer follow-up correction). On POSIX, `unlink`
    and `rmdir` consult only the CONTAINING directory's write+execute
    permission -- a file or directory's OWN mode bits are irrelevant to
    whether it can be removed. So the chmod above, alone, fixes nothing on
    POSIX for the one case that is genuinely common there: a read-only
    directory (`chmod 555 somedir`, or exactly what a bootstrapped SDK tree's
    `.git/objects/xx/` subdirectories carry after certain git versions'
    packing) defeats the unlink of every child inside it, and `path` here is
    the CHILD's path, whose own mode was never the problem. Measured: with
    only `path` chmod'd, `shutil.rmtree` over a tree containing one such
    directory still raised `PermissionError` on the first child inside it.
    Chmod'ing `os.path.dirname(path)` too -- best-effort, a failure here is
    swallowed rather than propagated, because the ORIGINAL failure (or
    whatever `func(path)` raises next) is the honest one to report if this
    does not fix it -- closes that gap. This is additive to the Windows
    read-only-ATTRIBUTE fix above, not a replacement for it: the two failure
    shapes (an NTFS attribute on the file itself; a POSIX permission bit on
    its parent) are independent and this function now clears both.
    """
    if func in _NOT_RETRYABLE_WITH_PATH_ALONE:
        _reraise_removal_failure(func, path, exc)
    os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
    parent = os.path.dirname(path)
    if parent and parent != path:
        try:
            os.chmod(parent, os.stat(parent).st_mode | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            pass
    try:
        func(path)
    except TypeError:
        # Belt and braces for a future `shutil` that routes one more
        # many-argument callable through this hook: the removal still failed,
        # and it must be reported as such rather than escaping as a `TypeError`.
        _reraise_removal_failure(func, path, exc)


#: `shutil.rmtree`'s error-hook keyword. `onerror` is deprecated from 3.12 and
#: scheduled for removal; `onexc` does not exist before it. Selected once here so
#: the call site stays a single expression on either interpreter -- the handler
#: signature is compatible because it ignores its third argument, which is the
#: only thing the two hooks disagree about (`exc_info` tuple vs exception).
_RMTREE_HOOK = "onexc" if sys.version_info >= (3, 12) else "onerror"


def remove_dir(path: str) -> None:
    """Remove a directory target recursively, never following a link out of the
    tree, and clearing a read-only attribute/bit in the way instead of failing
    the whole removal on it.

    A link ([`is_link`]) is unlinked ITSELF, exactly as the oracle's
    `remove_dir_all` does on Windows: verified against the Rust binary with
    `build/` junctioned at an out-of-tree directory -- the junction goes, the
    target's contents stay. `shutil.rmtree` handles the ordinary case and never
    recurses through a link INSIDE the tree, so both arms are contained.

    `os.rmdir` before `os.unlink`: on Windows a junction or directory symlink is
    removed by `RemoveDirectory`, and `unlink` fails on it; on POSIX `rmdir`
    fails on a symlink and `unlink` is what removes it.
    """
    if is_link(path):
        try:
            os.rmdir(path)
        except OSError:
            os.unlink(path)
        return
    shutil.rmtree(path, **{_RMTREE_HOOK: _retry_after_clearing_readonly})
