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
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


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
    module's own docstring for both halves of that reasoning.

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
    pathext = [e for e in env.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
    names = [tool] if Path(tool).suffix else [tool + ext for ext in pathext]
    for directory in path.split(os.pathsep):
        if not directory:
            continue
        for name in names:
            candidate = Path(directory) / name
            if candidate.is_file():
                return ToolResolution(str(candidate), f"PATH: {path}")
    return ToolResolution(None, f"PATH: {path}")


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
