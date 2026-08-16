# SPDX-License-Identifier: Apache-2.0
"""SDK-provenance git helpers, extracted out of `tan.commands.doctor_cmd`
(tan-cli#797 module-size budget) rather than left to grow that file further.

Every spawn here resolves `git` through `tan.core.tool_lookup.resolve_tool`
FIRST and threads the result through as `executable=` on the actual
`subprocess`/`probe` call -- never a bare `"git"` argv[0], which on Windows
`CreateProcess` searches the current directory for ahead of `%PATH%`
(tan-cli#797, the same hazard class tan-cli#567/#488 closed for
flash/size/west). `doctor_cmd` re-exports every name here (a plain
`from tan.core.doctor_git import ...`) so `doctor_cmd._resolve_git_executable`
etc. keep resolving for every existing caller and test.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tan.core.probe import PROBE_TIMEOUT_S, probe
from tan.core.tool_lookup import resolve_tool


def _resolve_git_executable() -> str | None:
    """The absolute path to `git`, resolved through the shared
    `tool_lookup.resolve_tool` walk -- never `shutil.which`, which on Windows
    inserts the current directory ahead of `%PATH%` (see `tool_lookup`'s own
    docstring). `None` when `git` is not on PATH at all, which every caller
    below must treat as "no git provenance to report", not a crash
    (tan-cli#797): a doctor that raises because git is absent is worse than
    the bare-argv0 spawn it replaces.
    """
    return resolve_tool("git", os.environ).resolved


def _is_own_git_checkout(root: str, git_exe: str | None) -> bool:
    """Whether `root` is itself the TOP of a git checkout, not merely nested
    somewhere inside an unrelated ENCLOSING one (tan-cli#488 defect 4).

    `git -C <root> ...` discovery walks UPWARD looking for a `.git`, so an SDK
    with no `.git` of its own -- an extracted release archive, vendored under
    a customer's own application repository, which this port explicitly
    supports (`tan.commands.sdk_cmd.check_sdk_readiness`'s own docstring names
    exactly this shape) -- answers every git query with the ENCLOSING repo's
    state instead of "not a checkout". `_git_short_commit`/`_git_behind_
    upstream` below both call this FIRST and refuse (`None`) rather than
    misattribute a foreign repository's commit or upstream-skew as the SDK's
    own.

    Compares the RESOLVED `--show-toplevel` against the RESOLVED `root`:
    lexical string comparison alone would false-negative on a symlinked or
    differently-cased path to the same directory.

    `git_exe` -- the PATH-RESOLVED absolute `git`, from `_resolve_git_executable`
    (tan-cli#797). `None` when `git` is not on PATH at all: refuses outright
    rather than falling back to a bare `probe(["git", ...])`, which would
    re-open the exact cwd-shadowing hazard this resolution exists to close.
    """
    if git_exe is None:
        return False
    top = probe(["git", "-C", root, "rev-parse", "--show-toplevel"], executable=git_exe)
    if top is None:
        return False
    try:
        return Path(top.strip()).resolve() == Path(root).resolve()
    except OSError:
        return False


def _git_short_commit(root: str, git_exe: str | None) -> str | None:
    """`git -C <root> rev-parse --short HEAD`, or `None` when `root` is not a
    git checkout of its own (e.g. an extracted SDK release archive -- including
    one vendored inside a customer's own git repository, see
    `_is_own_git_checkout`) or `git_exe` is `None` (git not on PATH)."""
    if not _is_own_git_checkout(root, git_exe):
        return None
    out = probe(["git", "-C", root, "rev-parse", "--short", "HEAD"], executable=git_exe)
    if out is None:
        return None
    commit = out.strip()
    return commit or None


def _git_behind_upstream(root: str, git_exe: str | None) -> int | None:
    """Commit count `HEAD` is behind its upstream tracking ref, without
    fetching. `None` when there is no upstream, `root` is not a git checkout
    of its own (see `_is_own_git_checkout`), or `git_exe` is `None` (git not
    on PATH)."""
    if not _is_own_git_checkout(root, git_exe):
        return None
    out = probe(
        ["git", "-C", root, "rev-list", "--count", "HEAD..@{upstream}"], executable=git_exe
    )
    if out is None:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def classify_git_core_longpaths(exit_code: int | None, stdout: str) -> bool | None:
    """The three-way verdict for a `git config --get core.longpaths`
    invocation -- the git-side counterpart to `doctor_cmd._long_paths_enabled`'s
    registry read, split out as its own pure function (mirroring
    `tan_core::host_env::classify_git_core_longpaths`) so the exact mapping
    tan-cli#306 argues hardest about is unit-tested without needing a real
    `git` invocation for every case.

    * exit 0 -> the stdout value, parsed with git's own boolean grammar.
    * exit 1 -> `False`. `git config --get` documents this code as "the key
      is not set in any scope (system/global/local)" -- git's own default,
      and the state a fresh `HOME` is in (tan-cli#306's exact repro).
    * anything else (`git` not on PATH, a malformed config file, a
      permissions error) -> `None`: uncertain, not guessed.
    """
    if exit_code == 0:
        value = stdout.strip().lower()
        return value not in ("false", "no", "off", "0")
    if exit_code == 1:
        return False
    return None


def _git_core_longpaths(git_exe: str | None) -> bool | None:
    """Read git's own EFFECTIVE `core.longpaths` (system -> global -> local
    precedence, resolved by `git config --get` itself rather than tan
    re-implementing that precedence by hand) via a real `git` subprocess.

    A SEPARATE axis from `doctor_cmd._long_paths_enabled` on purpose
    (tan-cli#306): the registry governs manifested Win32 API calls; it does
    nothing for git, which `west update` uses for every project
    clone/checkout and which refuses a long path unless ITS OWN setting says
    so -- the registry read alone reported `pass` on a fresh `HOME` while
    `west update` died on `hal_nxp`'s `tf-psa-crypto` tree.

    Not built on `tan.core.probe.probe`: `probe()` collapses "ran and exited
    non-zero" (exit 1, meaning "unset") and "could not run at all" (meaning
    "unknown") to the same `None`, and `classify_git_core_longpaths` needs to
    tell those apart.

    `git_exe` -- the PATH-RESOLVED absolute `git` from `_resolve_git_executable`
    (tan-cli#797). `None` when git is not on PATH: answers "unknown" rather
    than spawning the bare `"git"` this used to hand straight to
    `subprocess.run`, which on Windows searches the CURRENT DIRECTORY (the
    customer project's, since doctor never `chdir`s) ahead of `%PATH%`.
    """
    if git_exe is None:
        return None
    try:
        out = subprocess.run(
            ["git", "config", "--get", "core.longpaths"],
            executable=git_exe,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return classify_git_core_longpaths(out.returncode, out.stdout)
