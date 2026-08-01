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


def _resolve_layout(venv: Path) -> VenvLayout | None:
    """Which bin directory actually has `west`, POSIX first -- the SAME
    directory-wins rule creation already applies
    (`bootstrap_cmd.Workspace.venv_bin`/`venv_present`), so a
    Git-Bash/MSYS-created `Scripts/` venv (or a `bin/` venv found on a
    Windows host) that bootstrap accepted is not invisible here. `None` when
    `west` is present under neither layout.

    Deliberately NOT `venv_layout(os.name == "nt")` (host-only): that was
    tan-cli#291 -- bootstrap already derives the executable NAMES from the
    winning directory (`core.bootstrap.venv_exe_names`), not the host, and
    resolution disagreeing let a venv bootstrap just populated be invisible
    to `build`/`flash`. The Rust oracle (`crates/tan-cli/src/venv.rs`) is
    still host-only; tan-cli#291 identifies that as a latent oracle bug this
    port inherited, not a divergence to keep copying now that creation has
    already fixed it on its side (`3879cae`, tan-cli#289).
    """
    for is_windows in (False, True):
        layout = venv_layout(is_windows)
        if (venv / layout.bin_dir / layout.west).is_file():
            return layout
    return None


def find_workspace_venv(start: str, sdk_root: str | None) -> Path | None:
    """Locate the west-capable workspace `.venv`, mirroring Rust's
    `find_workspace_venv` but resolving the bin-dir layout directory-wins
    (see `_resolve_layout`) rather than host-only: gated on `west` actually
    being present under EITHER layout (not just the directory existing), in
    this order:

      1. a `.venv` in the project tree, searched from `start` upward;
      2. the workspace venv derived from `$ZEPHYR_BASE`
         (`<ZEPHYR_BASE>/../.venv`), manifest-guarded against `sdk_root` (see
         `_zephyr_base_venv`, tan-cli#292 consequence 2);
      3. the SDK's canonical `<sdk-parent>/.venv` (post-alp-sdk#782) or the
         legacy `<sdk-parent>/zephyrproject/.venv`.

    `None` when none resolve (CI, an activated venv, the contract harness).
    """
    directory: Path | None = Path(start)
    while directory is not None:
        candidate = directory / ".venv"
        if _resolve_layout(candidate) is not None:
            return candidate
        parent = directory.parent
        directory = parent if parent != directory else None

    zephyr_base = os.environ.get("ZEPHYR_BASE")
    if zephyr_base:
        candidate = _zephyr_base_venv(Path(zephyr_base), sdk_root)
        if candidate is not None:
            return candidate

    if sdk_root is not None:
        parent = Path(sdk_root).parent
        for workspace in (parent, parent / "zephyrproject"):
            candidate = workspace / ".venv"
            if _resolve_layout(candidate) is not None:
                return candidate

    return None


def _zephyr_base_venv(zephyr_base: Path, sdk_root: str | None) -> Path | None:
    """Step 2 of `find_workspace_venv`: `zephyr_base`'s PARENT's `.venv`, but
    only when it is both west-capable AND (when `sdk_root` resolved) its
    workspace manifest is verifiably alp-sdk's -- port of the SAME guard
    `_zephyr_base_workspace` already applies to the topdir search
    (tan-cli#61), which this venv search lacked (tan-cli#292 consequence 2,
    kept consistent with the tan-cli#301 fix one layer up: both now source
    "which Zephyr/venv" from the RESOLVED workspace rather than a bare
    `$ZEPHYR_BASE` read).

    Without this, a stale exported `$ZEPHYR_BASE` naming an unrelated Zephyr
    workspace shadowed the canonical `<sdk-parent>/.venv` (step 3) even when
    the caller passed an explicit `--sdk-root` -- reproduced against the
    published v0.5.0-rc2 asset on a clean host with no alp-sdk checkout
    anywhere (tan-cli#278). `sdk_root` absent means there is nothing to
    verify against, so the old unconditional accept stands, exactly as
    `_zephyr_base_workspace` does.
    """
    workspace = zephyr_base.parent
    candidate = workspace / ".venv"
    if _resolve_layout(candidate) is None:
        return None
    if sdk_root is not None:
        # Lazy for the same reason `_zephyr_base_workspace` is lazy: `tan.
        # commands.bootstrap_cmd` pulls in typer + the whole bootstrap
        # command surface, and this `tan.core` module must not depend on it
        # at import time -- only this one, rarely-taken branch ($ZEPHYR_BASE
        # set AND an sdk_root resolved) ever needs the manifest check at all.
        from tan.commands.bootstrap_cmd import _manifest_points_at  # noqa: PLC0415

        if not _manifest_points_at(workspace, Path(sdk_root)):
            return None
    return candidate


def venv_bin_dir(start: str, sdk_root: str | None) -> Path | None:
    """The west-capable workspace venv's executable directory (`bin` or
    `Scripts`, chosen directory-wins -- see `_resolve_layout`, tan-cli#291).
    This is the handle a spawning command needs: the directory to look tool
    names up in, and the directory to put on the child's PATH. `None` when no
    west-capable venv resolves."""
    venv = find_workspace_venv(start, sdk_root)
    if venv is None:
        return None
    layout = _resolve_layout(venv)
    assert layout is not None  # find_workspace_venv only returns what _resolve_layout accepted
    return venv / layout.bin_dir


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
    """Resolve `tool` INSIDE an already-located venv bin dir: returns its
    absolute path when it is really a file there. `.exe` is appended when
    `bin_dir` IS the Windows-layout dir (its name matches
    `venv_layout(True).bin_dir`, i.e. `Scripts`) unless the caller already
    spelled it -- keyed on the directory that won (tan-cli#291), not
    `os.name`: `bin_dir` is whatever `_resolve_layout`'s directory-wins probe
    picked, which can be `Scripts/` on a POSIX-reporting host or `bin/` on
    Windows when that is the layout actually on disk. `None` means "this venv
    does not provide that tool" -- the caller then keeps its PATH behaviour
    instead of spawning a path that doesn't exist."""
    name = tool
    if bin_dir.name == venv_layout(True).bin_dir and not tool.lower().endswith(".exe"):
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
