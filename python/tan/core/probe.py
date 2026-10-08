# SPDX-License-Identifier: Apache-2.0
"""The two subprocess primitives every host/tool probe in `tan doctor` (and
its git-provenance/build-side counterparts, `tan.core.doctor_git`) is built
on. Extracted out of `tan.commands.doctor_cmd` (tan-cli#797 module-size
budget) so `tan.core.doctor_git` can depend on them without an import cycle
back into `doctor_cmd` -- `tan/core` is the layer BELOW `tan/commands`, never
the reverse.

Neither function can raise: every subprocess failure mode a fresh, possibly
broken host can produce (missing binary, permission error, a probe that hangs
forever, garbage output) is swallowed and turned into "no answer", never a
traceback that would misrepresent a fixable environment problem as a tan bug.
"""
from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile

from tan.core.subprocess_env import spawn_env

#: Every probe in this module gets the same ceiling: long enough for a slow
#: but genuinely-answering tool, short enough that a hung one does not stall
#: /a build's provenance read indefinitely.
PROBE_TIMEOUT_S = 15


_PYTHON_NAME = re.compile(r"^(py|python[\d.]*[tw]?)(\.exe)?$", re.IGNORECASE)
#: `-c`, or a bundle ending in it (`-Ic`, `-Sc`, `-Bsc`).
_DASH_C = re.compile(r"^-[A-Za-z]*c$")


def isolated_cwd() -> "tempfile.TemporaryDirectory[str]":
    """A fresh EMPTY directory to use as a child's cwd. `python -m <mod>` and
    `python -c` put the cwd on `sys.path`, so a project directory can shadow
    `pip`/`venv`/a stdlib module; every spawn that must not be hijackable runs
    from here, with absolute path arguments (tan-cli#1317, #1331). The one
    helper both the probes and `bootstrap_cmd.Runner.run(isolated=True)` use."""
    return tempfile.TemporaryDirectory(prefix="tan-spawn-", ignore_cleanup_errors=True)


def is_python_dash_c(argv: list[str]) -> bool:
    """`<python> ... -c <code>`. With `-c`, `sys.path[0]` is the CWD, so a
    `west.py`/`json.py` planted in the user's project or SDK dir would run
    during a probe. Such probes run from a fresh EMPTY directory instead."""
    return (
        bool(argv)
        and any(_DASH_C.match(a) for a in argv[1:])
        and bool(_PYTHON_NAME.match(os.path.basename(argv[0])))
    )


def _absolute_for_new_cwd(argv: list[str], executable: str | None):
    """The probe runs from a different cwd, so a RELATIVE program path (one with
    a separator) must be made absolute first or it would stop resolving."""
    def fix(p: str) -> str:
        return os.path.abspath(p) if (os.sep in p or (os.altsep and os.altsep in p)) else p

    return [fix(argv[0]), *argv[1:]], (fix(executable) if executable else executable)


def probe_status(
    argv: list[str], timeout: int = PROBE_TIMEOUT_S, executable: str | None = None
) -> tuple[bool, str | None]:
    """Run `argv` and return `(ran, stdout)`.

    `executable`, when given, is `lpApplicationName`/POSIX `execve`'s own
    program path -- the PATH-RESOLVED absolute location `tool_lookup.resolve_tool`
    found, threaded through by a caller that needs `argv[0]` to stay the bare
    identity (so a tool's own diagnostics keep printing the short name) while
    the OS loads the hardened path instead of re-searching for the bare one
    itself (tan-cli#797, the same `executable=` shape tan-cli#567 established
    for the flash/size spawns).

    `ran` is `True` exactly when the process was actually spawned AND exited
    zero -- the one predicate that answers "could a build slice run this
    binary at all". `stdout` is the captured text only when `ran` is `True`,
    else always `None`. `probe()` below collapses both into a single `None`,
    which is right for most callers (an unparseable version banner and a
    binary that could not be spawned are both just "no answer") but wrong for
    a caller that needs to tell "spawned fine, printed garbage" apart from
    "never ran at all" -- `doctor_cmd.west_resolved_check` is exactly that
    caller (tan-cli#488 defect 1): a `west` launcher whose file survives but
    whose interpreter does not (a relocated workspace, a deleted
    `.venv/bin/python`) is `is_file()`-true and therefore reaches this probe,
    but every real invocation of it fails with `OSError`/a non-zero exit --
    `probe()` alone cannot distinguish that from a `west` that ran and printed
    something this command's regex could not parse, and reporting the former
    as `pass` is the false green this function exists to close.

    See `probe()` for why each failure mode below is swallowed rather than
    raised.

    `env=spawn_env()` (tan-cli#992): every probe in this module IS the
    "does tan think this tool works" verdict, so a probe that leaked tan's
    own bundled `LD_LIBRARY_PATH` into the child would report a perfectly
    fine host tool as broken the moment its own bundled copy of some shared
    library disagreed with the host's -- exactly the failure mode
    `spawn_env` exists to close.
    """
    isolate = is_python_dash_c(argv)
    if isolate:
        argv, executable = _absolute_for_new_cwd(argv, executable)
    scratch = (
        isolated_cwd()
        if isolate
        else contextlib.nullcontext(None)
    )
    try:
        with scratch as cwd:
            out = subprocess.run(
                argv,
                executable=executable,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                env=spawn_env(),
                check=False,
            )
    except (OSError, ValueError, subprocess.SubprocessError):
        # SubprocessError covers TimeoutExpired (the child is already killed by
        # `run`); ValueError catches an empty/garbage argv rather than letting
        # it escape as a traceback.
        return False, None
    return (out.returncode == 0), (out.stdout if out.returncode == 0 else None)


def probe(
    argv: list[str], timeout: int = PROBE_TIMEOUT_S, executable: str | None = None
) -> str | None:
    """Run `argv` and return its stdout, or `None` for every way that can fail.

    `None` means "no answer", never "the answer is bad" -- callers must not read
    it as a verdict. The failure modes this swallows are all real on a fresh
    host: the binary is absent (`FileNotFoundError`), it is a directory or not
    executable (`OSError`/`PermissionError`), it waits forever on a probe that is
    not plugged in (`TimeoutExpired`), or it exits non-zero.

    `stdin` is closed, not inherited: a tool that decides to prompt then reads
    EOF and dies instead of blocking until the timeout. `errors="replace"` is
    the same reason `tests/conformance` uses it -- a tool answering in the
    platform code page must not turn into a `UnicodeDecodeError` crash that
    masquerades as a host problem.

    A thin wrapper over `probe_status` (above) for every caller that only ever
    wanted "the answer, or nothing" and has no use for the ran/did-not-run
    split.
    """
    return probe_status(argv, timeout, executable=executable)[1]
