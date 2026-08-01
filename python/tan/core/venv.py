# SPDX-License-Identifier: Apache-2.0
"""Workspace-venv resolution, shared by every command that spawns a tool the
`tan bootstrap` venv owns.

Port of `crates/tan-cli/src/venv.rs`. `west` normally lives ONLY inside the
bootstrapped venv -- nothing activates it for a GUI-launched editor, so the
ambient PATH has no `west` at all. This module is what lets `tan` find it
anyway.

Moved here from `tan.commands.build_cmd` (tan-cli#289): `_find_workspace_venv`
was private to that module but already imported across two others
(`west_forward_cmd.py`, and indirectly `generate_cmd.py`/`model_cmd.py`/
`monitor_cmd.py`/`new_som_cmd.py` via `_planner_python`) -- and three more
consumers (`build.execute`'s slice dispatch, `flash_cmd`'s tool gate and
spawn) needed the SAME search wired in rather than a second copy of it. This
is the one shared module `flash_cmd.py`'s own docstring asked for.
"""
from __future__ import annotations

import os
from pathlib import Path

from tan.core.bootstrap import VenvLayout, venv_layout


def _has_west(venv: Path, layout: VenvLayout) -> bool:
    return (venv / layout.bin_dir / layout.west).is_file()


def find_workspace_venv(start: str, sdk_root: str | None) -> Path | None:
    """Locate the west-capable workspace `.venv`, mirroring Rust's
    `find_workspace_venv`: gated on `west` actually being present under the
    candidate (not just the directory existing), in this order:

      1. a `.venv` in the project tree, searched from `start` upward;
      2. the workspace venv derived from `$ZEPHYR_BASE`
         (`<ZEPHYR_BASE>/../.venv`);
      3. the SDK's canonical `<sdk-parent>/.venv` (post-alp-sdk#782) or the
         legacy `<sdk-parent>/zephyrproject/.venv`.

    `None` when none resolve (CI, an activated venv, the contract harness).
    """
    layout = venv_layout(os.name == "nt")

    directory: Path | None = Path(start)
    while directory is not None:
        candidate = directory / ".venv"
        if _has_west(candidate, layout):
            return candidate
        parent = directory.parent
        directory = parent if parent != directory else None

    zephyr_base = os.environ.get("ZEPHYR_BASE")
    if zephyr_base:
        candidate = Path(zephyr_base).parent / ".venv"
        if _has_west(candidate, layout):
            return candidate

    if sdk_root is not None:
        parent = Path(sdk_root).parent
        for workspace in (parent, parent / "zephyrproject"):
            candidate = workspace / ".venv"
            if _has_west(candidate, layout):
                return candidate

    return None


def venv_bin_dir(start: str, sdk_root: str | None) -> Path | None:
    """The west-capable workspace venv's executable directory (`bin`,
    `Scripts` on Windows), mirroring Rust's `venv_bin_dir`. This is the handle
    a spawning command needs: the directory to look tool names up in, and the
    directory to put on the child's PATH. `None` when no west-capable venv
    resolves."""
    venv = find_workspace_venv(start, sdk_root)
    if venv is None:
        return None
    return venv / venv_layout(os.name == "nt").bin_dir


def west_workspace_dir(start: str, sdk_root: Path | None) -> Path | None:
    """Port of `workspace.rs::west_workspace_dir`: the directory holding
    `.west/` that a `west alp-*` extension command -- and `tan flash`'s
    spawned children (tan-cli#289/#61) -- must run from. Discovered only from
    a workspace manifest, never the app dir alone. Checked in order: the
    project tree upward, `$ZEPHYR_BASE/..` (manifest-verified against
    `sdk_root` when one resolved), then the SDK-derived layouts
    (`<sdk-parent>` and the legacy `<sdk-parent>/zephyrproject`). `None` when
    nothing resolves -- the caller then keeps the old app-dir cwd, matching
    the oracle exactly.

    **Moved here from `tan.commands.west_forward_cmd` (tan-cli#289 review).**
    `flash_cmd.py` needed the SAME resolver `west_forward_cmd.py` already
    had -- a second copy risked the two disagreeing the moment one changed,
    the exact class of bug `find_workspace_venv` (above) already exists to
    avoid. `west_forward_cmd` keeps a `_west_workspace_dir` alias bound to
    this function for its own call site and its existing tests; a THIRD,
    partial copy still lives in `tan.commands.doctor_cmd._resolve_
    west_workspace_dir` and is a follow-up, not retired here.
    """

    def is_workspace(d: Path) -> bool:
        return (d / ".west").is_dir()

    directory: Path | None = Path(start)
    while directory is not None:
        if is_workspace(directory):
            return directory
        parent = directory.parent
        directory = parent if parent != directory else None

    zephyr_base = os.environ.get("ZEPHYR_BASE")
    if zephyr_base:
        found = _zephyr_base_workspace(Path(zephyr_base), sdk_root)
        if found is not None:
            return found

    if sdk_root is not None:
        parent = sdk_root.parent
        for workspace in (parent, parent / "zephyrproject"):
            if is_workspace(workspace):
                return workspace

    return None


def _zephyr_base_workspace(zephyr_base: Path, sdk_root: Path | None) -> Path | None:
    """Step 2 of `west_workspace_dir`: port of `workspace.rs::zephyr_base_workspace`.
    `zephyr_base`'s PARENT, but only when it is both a west workspace AND (when
    `sdk_root` resolved) its manifest is verifiably alp-sdk's -- a stock/unrelated
    Zephyr checkout's parent is a west workspace by the bare `.west`-dir test
    alone, yet carries no alp-sdk extension commands at all. `sdk_root` absent
    means there is nothing to verify against, so the old unconditional accept
    stands rather than refusing a workspace this call can't check.
    """
    workspace = zephyr_base.parent
    if not (workspace / ".west").is_dir():
        return None
    if sdk_root is not None:
        # Lazy, deliberately: `tan.commands.bootstrap_cmd` is a `tan.commands`
        # module (pulls in typer + the whole bootstrap command surface), and
        # this `tan.core` module must not depend on it at import time -- only
        # this one, rarely-taken branch ($ZEPHYR_BASE set AND an sdk_root
        # resolved) ever needs the manifest check at all.
        from tan.commands.bootstrap_cmd import _manifest_points_at  # noqa: PLC0415

        if not _manifest_points_at(workspace, sdk_root):
            return None
    return workspace


def tool_in_venv(bin_dir: Path, tool: str) -> str | None:
    """Resolve `tool` INSIDE an already-located venv bin dir, mirroring
    Rust's `tool_in_venv`: returns its absolute path when it is really a file
    there. `.exe` is appended on Windows unless the caller already spelled
    it. `None` means "this venv does not provide that tool" -- the caller
    then keeps its PATH behaviour instead of spawning a path that doesn't
    exist."""
    name = tool
    if os.name == "nt" and not tool.lower().endswith(".exe"):
        name = f"{tool}.exe"
    candidate = bin_dir / name
    return str(candidate) if candidate.is_file() else None


def west_program(start: str, sdk_root: str | None) -> str:
    """Resolve the `west` program to launch: the west-capable workspace
    venv's `west` binary (see [`find_workspace_venv`]), falling back to
    `"west"` on PATH when none resolve (CI, an activated venv, the contract
    harness) -- behaving exactly as before in those environments. Mirrors
    Rust's `west_program`."""
    bin_dir = venv_bin_dir(start, sdk_root)
    if bin_dir is None:
        return "west"
    return tool_in_venv(bin_dir, "west") or "west"


def venv_python(start: str, sdk_root: str | None) -> str | None:
    """The west-capable workspace venv's `python` (see
    [`find_workspace_venv`]), mirroring Rust's `venv_python`. `None` when no
    workspace venv resolves, or the one that does has no `python` binary --
    the caller then keeps its own PATH-name fallback rather than spawning a
    path that doesn't exist.

    Forward-slashed: a caller substitutes this verbatim into `${PYTHON}`,
    which lands in a `-DPython3_EXECUTABLE=<...>` CMake `-D` argument, and a
    Windows backslash there is an escape character (alp-sdk#849).
    """
    bin_dir = venv_bin_dir(start, sdk_root)
    if bin_dir is None:
        return None
    resolved = tool_in_venv(bin_dir, "python")
    return resolved.replace("\\", "/") if resolved is not None else None


def prepend_path(env: dict[str, str], directory: Path) -> dict[str, str]:
    """Put `directory` at the FRONT of `env`'s `PATH`, mirroring Rust's
    `prepend_path`. Returns a NEW dict -- `env` itself is never mutated, so a
    caller holding onto the original (e.g. `dict(os.environ)`) is unaffected."""
    existing = env.get("PATH", "")
    joined = os.pathsep.join([str(directory), existing]) if existing else str(directory)
    return {**env, "PATH": joined}


def with_venv_on_path(env: dict[str, str], tool: str) -> dict[str, str]:
    """When `tool` resolved to an absolute (venv) path, prepend its
    directory to `env`'s `PATH` -- mirroring Rust's `with_venv_on_path`. The
    `alp-*`/`west` program spawned at that path may itself shell a nested
    `west`/`bitbake`, which resolves purely via PATH. A bare tool name (the
    PATH fallback) is left untouched, matching the oracle's early return for
    a non-absolute `tool`."""
    bin_path = Path(tool)
    if not bin_path.is_absolute():
        return env
    return prepend_path(env, bin_path.parent)
