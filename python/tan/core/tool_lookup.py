# SPDX-License-Identifier: Apache-2.0
"""ONE "where is this tool" lookup, for every command that spawns one.

**tan-cli#567 / #532.** The hardened lookup lived in five independent
hand-rolled copies, and tan-cli#510 was caused by exactly that shape: an
availability CHECK that walked `%PATH%` by hand so the current directory could
not shadow a tool, and a SPAWN, in the same function, that handed the bare
identity back to the platform's own resolver -- whose Windows search order
(`CreateProcess` with `lpApplicationName=NULL`) puts *the current directory for
the parent process* AHEAD of `PATH`. The check protected against a hazard the
spawn then re-opened.

`resolve_tool` is that lookup, moved out of `tan.commands.build.execute` where
#510 hardened it, so the flash write path and the size path can spawn the SAME
resolved path instead of maintaining a third and fourth opinion about it. The
callers keep their own policy about what to do when a tool is missing; only the
*finding* is shared.

**Why not `shutil.which` on Windows.** Its stdlib implementation inserts
`os.curdir` ahead of every `PATH` entry ("the current directory takes
precedence on Windows" -- its own source comment), reproducing `CreateProcess`'s
native search order. `where.exe` behaves the same way. The Rust oracle
(`crates/tan-cli/src/util.rs::command_on_path`) walks `%PATH%` by hand
precisely so a project checked out with its own `west.exe`/`openocd.exe`/
`size.exe` at its root can never be picked up in place of the real tool; this
is that walk.

**The Windows candidate set -- the one deliberate behaviour change here.**
For a bare, extension-less identity this walk tries `tool` + each `%PATHEXT%`
suffix and NEVER `tool` itself ([`windows_candidate_names`]). That is a
Windows-arm behaviour change relative to `doctor_cmd.on_path`, one of the five
hand-rolled lookups #532 consolidates, whose extension list is `[""] + PATHEXT`
-- it accepts an extension-less `%PATH%` file, and accepts it AHEAD of every
suffixed sibling. Three reasons the oracle's shape is the one to consolidate on:

* **Oracle parity.** `crates/tan-cli/src/util.rs::find_on_path` is
  `if has_ext { dir.join(command) } else { for ext in &exts { ... } }` -- no
  bare candidate. The oracle is the fixed point this port is measured against,
  and `build/execute.py::_resolve_tool` (the copy this module IS) already
  agreed with it. Only `on_path` did not -- and since tan-cli#532 it no
  longer exists as a separate walk: `doctor_cmd.on_path` delegates here, so
  the divergence this section describes is now historical, and the behaviour
  change it records has taken effect for the doctor and `faultdecode_cmd`
  as well.
* **Windows itself never selects one for a bare identity.** `CreateProcess`
  with `lpApplicationName=NULL` appends ONLY `.exe` to an unqualified name and
  does not consult `%PATHEXT%` at all -- the documented reason
  `subprocess.run(["npm"])` raises `FileNotFoundError` while `npm.cmd` sits on
  `%PATH%` (measured in `tests/commands/test_execute.py::_copy_interpreter_as`).
  `cmd.exe` and PowerShell resolve a typed command name through `%PATHEXT%`
  too. This walk is meant to be a HARDENED version of that search -- more
  permissive along one axis only (`%PATHEXT%`), never admitting files no
  Windows resolver would produce for the name.
* **It would be unsafe now that the value is SPAWNED, not reduced to a bool.**
  The extension-less files really found on a Windows `%PATH%` are
  overwhelmingly POSIX shims -- `npm`/`yarn`/`git-*` sh scripts shipped beside
  their `.cmd` sibling in the SAME directory -- and `""` going first means the
  sh script wins. Since tan-cli#567 that value goes to `subprocess` as
  `executable=`, i.e. `lpApplicationName` with no search, where a non-PE file
  is `[WinError 193] %1 is not a valid Win32 application`. The leniency would
  turn a working `.cmd` spawn into a hard spawn failure, and would make the
  go/no-go gate approve a file the spawn cannot launch: the very check/spawn
  disagreement #567 closes, re-entered from the other side.

What is given up is bare-identity DISCOVERY of an extension-less file that is
a valid PE. Windows will execute one of those -- `.exe` is appended only to a
name carrying no path -- and that capability is untouched here: an absolute
`tool` is answered by existence alone and spawned verbatim, so a plan may
still name one. A venv rewrite is the narrower case: `venv.tool_in_venv`
(`tan/core/venv.py:277`) appends `.exe` whenever the winning directory is
`Scripts`, which is what `_resolve_layout` picks for a stock Windows venv, so
a rewrite can only name an extension-less file in the `bin`-on-Windows layout.
Only finding it by bare name on `%PATH%` is refused, and the refusal names the
`%PATH%` it walked.
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath


@dataclass(frozen=True)
class ToolResolution:
    """One answer to both "is `tool` available" and "where is it" --
    `resolved` is the absolute path to actually spawn (`None` when nothing
    was found), `searched` is always populated, found or not, and describes
    where this looked.

    Replaces the old `_command_on_path`/`_tool_is_available` pair
    (tan-cli#510): that pair answered a bare bool from one hardened lookup,
    and the spawn below then repeated a SEPARATE, unhardened one -- handing
    the bare identity straight to the platform's own resolver, whose Windows
    search order includes a current-directory step the availability check
    was written specifically to exclude. One resolver now; the check and the
    spawn can never again disagree about what ran."""

    resolved: str | None
    searched: str


def resolve_tool(tool: str, env: Mapping[str, str]) -> ToolResolution:
    """Resolve `tool` -- an identity per ADR-0020 (never a path in a
    well-formed plan), or an absolute path a caller already resolved
    (`west_program`'s workspace-venv rewrite, `execute_slices`;
    `flash_cmd._programs_resolved_in_venv`) -- to the absolute path to spawn.

    An absolute `tool` is answered by existence alone: whoever produced it
    already did the searching, and re-walking PATH for something that is
    already a path would be wrong.

    A bare/relative name is walked exactly the way the old
    `_command_on_path` did -- this IS that lookup, now doubling as the
    resolution the spawn itself uses instead of a second, divergent one:
    POSIX via `shutil.which` (no CWD special-case there); Windows via a
    hand-rolled `%PATH%` walk, deliberately NOT `shutil.which` -- see this
    module's own docstring for both halves of that reasoning. The candidate
    NAMES that walk tries are [`windows_candidate_names`], which is where the
    one deliberate Windows-arm behaviour change this consolidation makes (an
    extensionless PATH file is no longer a hit, matching the oracle rather
    than `doctor_cmd.on_path`) is recorded and justified.

    `env` -- MAJOR 2 of the tan-cli#510 review: this used to read
    `os.environ["PATH"]` directly, 46 lines before `execute_slices`
    assembled the slice's OWN `env` (the one the spawn below actually gets),
    so a plan that pinned a different `PATH` in `command.env` resolved
    against the PARENT's PATH and then spawned a DIFFERENT binary than the
    one the check just approved -- the pre-fix `Command::new(&tool)` case
    this whole issue exists to close, reintroduced one call up. Callers pass
    the FULLY ASSEMBLED child env (post `assemble_slice_env`/
    `with_venv_on_path`, or `flash_cmd`'s `prepend_path(os.environ,
    venv_bin)`), never `os.environ` when the child gets something else -- a
    plan that pins a PATH is asking for that PATH to be used, matching
    pre-fix POSIX `Popen`, which always selected via `os.get_exec_path(env)`
    (the spawn's OWN env), never the calling process's.

    `searched` is populated on every return, including a hit: a missingTool
    refusal that can only say "not found" is a support ticket; one that
    names the literal PATH entries this walked is a fix the customer
    applies themselves (tan-cli#510 acceptance)."""
    if Path(tool).is_absolute():
        if Path(tool).exists():
            return ToolResolution(tool, f"`{tool}` (given as an absolute path)")
        # tan-cli#510 review, minor: the old wording ("searched `<path>`
        # (given as an absolute path)") echoed the same path back as if a
        # search had run one -- nothing was searched, the path was just
        # checked. Naming the miss plainly avoids the tautology.
        return ToolResolution(None, f"`{tool}` -- that path does not exist")

    if os.name != "nt":
        # `os.get_exec_path(env)` mirrors what a POSIX `Popen(..., env=env)`
        # itself consults to resolve a bare argv[0] (`os.defpath` when `env`
        # carries no `PATH` at all) -- the same fallback `shutil.which`'s own
        # `path=None` default would use from `os.environ`, reproduced here
        # against `env` instead.
        #
        # **EMPTY entries are dropped first (tan-cli#567).** This is not
        # tidiness: `PATH="$PATH:"`, `PATH=":$PATH"` and a `;;` on Windows are
        # all routine, and an empty entry means "the current directory" to
        # every POSIX PATH consumer. `shutil.which(tool, path=":/nope:")`
        # joins `""` with the name and probes it RELATIVE TO CWD -- measured on
        # this tree, `resolve_tool("cwdprobe", {"PATH": ":/nope:"})` answered
        # `'cwdprobe'` (relative!) for a file that exists only in the process's
        # own working directory. That is the exact hazard the hand-rolled walk
        # exists to prevent, reachable on the BUILD path since tan-cli#510 and
        # missed because the Windows branch below already filtered and the
        # POSIX branch did not. `size_cmd`'s now-retired private copy had the
        # filter; consolidating without it would have made three commands
        # share one weaker lookup.
        search_dirs = [d for d in os.get_exec_path(dict(env)) if d]
        if not search_dirs:
            return ToolResolution(None, "PATH is unset")
        path = os.pathsep.join(search_dirs)
        return ToolResolution(shutil.which(tool, path=path), f"PATH: {path}")

    path = env.get("PATH")
    if not path:
        return ToolResolution(None, "PATH is unset")
    names = windows_candidate_names(tool, env.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"))
    for directory in path.split(os.pathsep):
        if not directory:
            continue
        for name in names:
            # `os.path`, not `pathlib`, and the difference is MEASURED rather
            # than stylistic (tan-cli#811). This is the only remaining site
            # with the shape: a full miss probes every `%PATH%` entry x every
            # `%PATHEXT%` suffix, which on the host that issue measured is 92
            # dirs x 15 names = 1380 candidates, and a `Path` object is built
            # and thrown away for each. Same loop, same candidate set, only
            # the construction and stat swapped: 0.0389 -> 0.0177 s per miss,
            # 2.2x. POSIX never reaches here (it returns via `shutil.which`
            # above), so this is a Windows-only cost and a Windows-only fix.
            #
            # **This is not a pure cost swap, and the difference is the
            # SHIPPED floor, not an edge case** (review of #874). On 3.14
            # `Path.is_file()` IS `os.path.isfile` (`if follow_symlinks:
            # return os.path.isfile(self)`), so the two are the same call.
            # On 3.12 and 3.13 they are not: `Path.is_file()` re-raises any
            # `OSError` outside `_IGNORED_ERRNOS`/`_IGNORED_WINERRORS`, while
            # `genericpath.isfile` swallows every `OSError` and `ValueError`.
            # `pyproject.toml` pins `requires-python = ">=3.12"` and the
            # frozen binaries customers install are built on 3.12, so the
            # divergence ships. Measured on this tree, one unreadable
            # directory, same file:
            #
            #     3.12.14  Path.is_file() -> PermissionError errno=13
            #              os.path.isfile() -> False
            #     3.13.15  Path.is_file() -> PermissionError errno=13
            #              os.path.isfile() -> False
            #     3.14.7   both -> False
            #
            # Reachable on Windows: `ERROR_ACCESS_DENIED (5)` and
            # `ERROR_SHARING_VIOLATION (32)` both map to `EACCES` in
            # `PC/errmap.h`, so a `%PATH%` entry on a share whose credentials
            # have expired, or an ACL-locked directory, used to abort the
            # whole walk -- even when the tool sat in a LATER entry.
            #
            # Skipping it is the DELIBERATE choice, not a side effect. This
            # loop carries no `try` of its own, so whatever the predicate
            # raises escapes `resolve_tool` entirely; no call site guards
            # it and `tan/cli.py` installs no top-level handler, so on
            # 3.12/3.13 that `PermissionError` was a traceback where an
            # envelope belongs. `tan/core/shapes.py` already states this
            # repo's position for exactly this shape -- "a path with an
            # embedded NUL or an unreadable parent must read as 'not an SDK
            # root', never as a traceback where a refusal belongs" -- and a
            # `%PATH%` entry tan cannot stat is the same question. An entry
            # that is skipped is not silent: a tool found nowhere answers
            # `resolved=None`, which `build_cmd` routes through
            # `executionPolicy.missingTool` (`build_cmd._dispatch`, default
            # skip) and names in the refusal. Pinned by
            # `test_an_unreadable_path_entry_is_skipped_not_raised`.
            #
            # One asymmetry this leaves, small but real: the string PROBED is
            # no longer the string RETURNED. `os.path.join` keeps a `.` or a
            # duplicated separator the `%PATH%` entry carried where the
            # `PureWindowsPath` return below collapses it, so
            # `C:\\Windows\\.\\System32` probes 30 characters and returns 28.
            # Against the 260-char `MAX_PATH` on a host without long paths
            # enabled, a candidate within two characters of the limit can now
            # miss where it previously hit.
            if os.path.isfile(os.path.join(directory, name)):
                # The RETURN is still built the pathlib way, once, on the
                # winner -- deliberately NOT `os.path.join`'s own string.
                # `os.path.join` preserves whatever separators the `%PATH%`
                # entry carried (`os.path.join("C:/Windows/System32",
                # "cmd.exe")` is `'C:/Windows/System32\\cmd.exe'`) where
                # `str(PureWindowsPath(...) / ...)` normalises to
                # `'C:\\Windows\\System32\\cmd.exe'`, and this string is
                # customer-visible: `build_cmd` publishes it as
                # `data.slices[].resolvedTool` (`build_cmd._slice_result`
                # sets it unconditionally).
                # tan-cli#811 proposed `os.path.normpath` on the return to
                # close that gap; it does, but it ALSO collapses `..`, which
                # pathlib does not, so a `%PATH%` entry written relative to a
                # parent would come back rewritten. Rebuilding the winner
                # with the original expression is byte-identical instead of
                # merely equivalent, and costs one path object per HIT, never
                # per candidate.
                #
                # `PureWindowsPath`, not `Path`, and that is a TESTABILITY
                # fix rather than a behaviour one (review of #874). The two
                # produce the same string on Windows by inheritance, not by
                # coincidence: `WindowsPath.__mro__` contains
                # `PureWindowsPath`, `__str__` and `__truediv__` are the SAME
                # objects on both, and both carry the frozen `ntpath` parser
                # (verified on 3.12.14 and 3.14.7). But `Path` here made the
                # hit path unreachable from a POSIX test -- `WindowsPath /
                # str` refuses to build -- which is how this walk's only
                # regression test decayed into grepping its own source. This
                # module already uses `PureWindowsPath` for the same reason
                # in `windows_candidate_names` below.
                return ToolResolution(str(PureWindowsPath(directory) / name), f"PATH: {path}")
    return ToolResolution(None, f"PATH: {path}")


def windows_candidate_names(tool: str, pathext: str) -> list[str]:
    """The file names the `%PATH%` walk above may consider for the bare
    identity `tool`: `tool` verbatim once it already carries an extension,
    and otherwise `tool` + each `%PATHEXT%` suffix -- **never the bare,
    extensionless name itself.**

    Split out as a pure function so the Windows candidate set can be
    unit-tested from any host, mirroring the Rust oracle, which splits its own
    search core out for the same reason and says so
    (`crates/tan-cli/src/util.rs::find_on_path`, "Pure search core split out
    of `windows_path_lookup` purely for unit testing").

    What this said before tan-cli#811 -- that `resolve_tool`'s Windows branch
    is unreachable from POSIX because `pathlib.Path` "dispatches on `os.name`
    at construction, so patching it raises" -- was wrong in both halves, and
    three tests here were built on the false version. Measured on 3.12.14 and
    3.14.7: `Path("git")` under a patched `os.name` CONSTRUCTS a
    `WindowsPath` (`Path.__new__` reaches `object.__new__(cls)`, bypassing
    the raising subclass `__new__`), and the lexical and stat methods then do
    not raise, they LIE -- `Path("/usr/bin/git").is_absolute()` is `False`.
    Where a refusal does happen (`WindowsPath / str`, or `WindowsPath(...)`
    directly) it is a plain `NotImplementedError` on the SHIPPED floor:
    `pathlib` has no `UnsupportedOperation` attribute at all on 3.12, which
    3.13 renamed it into as a `NotImplementedError` subclass.

    So since tan-cli#811 the walk is DELIBERATELY reachable from POSIX and is
    driven there -- the miss loop probes through `os.path`, the hit spells
    with `PureWindowsPath` -- because while it was not, this walk's only
    regression test decayed into grepping the module's own source.

    A patched-`os.name` test proves less than it runs: `os.path` stays
    `posixpath`, so the probe joins with `/` against a real POSIX filesystem
    while the return spells with `\\`. Such a test pins candidate ORDER, the
    skip-don't-raise decision and the returned SPELLING -- not Windows path
    syntax on the probe, which is this function's own job.

    Excluding the bare name is the one deliberate Windows-arm behaviour
    change tan-cli#567 makes; this module's own docstring carries the whole
    justification under "The Windows candidate set", and
    `test_bare_argv0_spawn.py::test_the_windows_walk_never_considers_the_bare
    _extensionless_name` pins it.
    """
    if PureWindowsPath(tool).suffix:
        return [tool]
    # `;` literally, not `os.pathsep`: `%PATHEXT%` is a Windows-only variable
    # and is always `;`-separated (the oracle splits it on `';'` too). They are
    # the same character on the platform this runs on -- spelling it makes the
    # function answer the Windows question from any host, which is the point of
    # extracting it.
    return [tool + ext for ext in pathext.split(";") if ext]


def resolve_program_positions(
    argv: list[str], env: Mapping[str, str], separator: str | None = None
) -> tuple[list[str], str | None]:
    """Rewrite every PROGRAM position in `argv` -- `argv[0]`, plus the token
    right after each `separator` (a `"|"` pipeline token, when one is given) --
    to the absolute path [`resolve_tool`] found for it against `env`, the SAME
    environment the spawn will hand the child.

    Returns `(argv, unresolved)`. `unresolved` names the FIRST program position
    that resolved to nothing, and is `None` when every one of them resolved.
    Arguments are never touched.

    A program that ALREADY carries a path separator is left exactly as it is
    and never counts as unresolved: it is not a PATH identity, so there is no
    PATH lookup to harden. That covers the absolute paths a caller resolved
    itself (`flash_cmd._programs_resolved_in_venv`'s venv rewrite, which
    already probed the file) without re-searching them, and it keeps this
    agreeing with `is_rust_absolute` on the rooted-but-driveless Windows form
    that `Path.is_absolute` and `os.path.isabs` disagree about across
    interpreter versions (`tan.core.flash_plan`'s own convention).

    **A bare program that does not resolve is left BARE, and the caller
    decides.** This function never invents a path. Its callers on the write
    path refuse the spawn outright rather than pass a bare name to
    `CreateProcess`, because "not on PATH and not in the venv" leaves the
    current directory as the only remaining supplier -- which is the whole
    hazard. Pure: no IO beyond the `is_file`/`exists` probes `resolve_tool`
    already does.
    """
    out: list[str] = []
    unresolved: str | None = None
    is_program = True
    for arg in argv:
        if is_program and not _has_path_separator(arg):
            resolution = resolve_tool(arg, env)
            if resolution.resolved is None:
                if unresolved is None:
                    unresolved = arg
                out.append(arg)
            else:
                out.append(resolution.resolved)
        else:
            out.append(arg)
        is_program = separator is not None and arg == separator
    return out, unresolved


def _has_path_separator(arg: str) -> bool:
    """Whether `arg` names a location rather than a PATH identity. `os.altsep`
    is checked too, so a forward-slashed path on Windows -- which the OS
    accepts and half this repo's fixtures write -- is not mistaken for a bare
    command name."""
    return os.sep in arg or (os.altsep is not None and os.altsep in arg)
