#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""`--emit kconfig` -- the board-scoped, user-settable Kconfig symbol menu
for the vscode `prj.conf` LSP (#893); unblocks tan-cli #35 (`tan kconfig`).

Deliberately separate from `kconfig.py` (the alp.conf/local.conf string
templater, which never runs kconfiglib/west -- see its module docstring):
this module is the SDK's first workspace-dependent emit.  Every other
`--emit` mode is hermetic (provable from board.yaml + this repo's own
metadata alone); this one needs a bootstrapped Zephyr workspace
(`ZEPHYR_BASE`, v4.4.0) because only the real Kconfig solver knows which
symbols are user-promptable for a given board.

Two independent halves:

  * `_envelope` -- pure JSON shaping (the schemaVersion/board/core/symbols
    wrap), unit-tested with NO Zephyr installed (see
    tests/core/test_kconfig_symbols.py). The actual symbol projection
    ("first prompt-bearing node, not nodes[0]" -- alp-sdk #893) lives ONLY
    inside `_DUMPER_SOURCE` below, the rendered dumper script: it has to run
    as a standalone subprocess inside Zephyr's own Kconfig env, importing
    nothing from this package, so there is no separate copy here for it to
    import -- `tests/core/test_kconfig_symbols.py` asserts the rendered
    projection directly against a literal expected result (an independent
    oracle) instead of hand-syncing a second copy.
  * `_load_board_symbols` -- Approach A: a stub `west build --cmake-only`
    registers Zephyr's `EXTRA_KCONFIG_TARGET` custom-target mechanism
    (`cmake/modules/kconfig.cmake` ~199-243 -- the same seam `west build -t
    menuconfig` uses) pointed at a dumper script this module RENDERS fresh
    onto disk for every run (`_DUMPER_SOURCE`, written under the run's own
    scratch tree -- never a file living in alp-sdk, and never a PyInstaller
    onefile extraction path, which is deleted at process exit and would
    leave the baked-in CMake command pointing at nothing); `west build -t
    alpkconfigjson` then runs it INSIDE the exact Kconfig env Zephyr's own
    CMake computed for that board/toolchain/module set (no env
    reconstruction needed -- Zephyr hands it over directly). This is
    workspace-dependent and is skipped locally without a bootstrapped
    ZEPHYR_BASE; alp-sdk's own dumper copy under `scripts/kconfig/
    alp_kconfig_dump.py` is verified by the pr-twister CI job's
    `scripts/check_emit_kconfig_contract.py` -- that gate covers alp-sdk's
    copy only, not tan's rendered one above.

    An earlier version tried overriding `-DPYTHON_EXECUTABLE=<spy>` to
    capture kconfig.py's own env instead -- discarded: Zephyr's
    `cmake/modules/python.cmake` re-derives `PYTHON_EXECUTABLE` via a
    plain `set()` (not `_ifndef`), so a `-D` override is silently
    clobbered before `kconfig.cmake` ever runs, and kconfig.py never
    actually invokes the spy.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Optional

from .models import BoardProject, OrchestratorError

# RELOCATED divergence from alp-sdk's own scripts/alp_orchestrate/
# kconfig_symbols.py (tan-cli#459 review): `west_program` resolves the
# ABSOLUTE `west` inside `tan bootstrap`'s workspace venv, the same helper
# `build`/`flash`/`west_forward_cmd` already spawn `west` through -- alp-sdk's
# own copy runs AS a `west` extension, so a bare `"west"` on PATH is always
# already the one that found it; tan's frozen binary has no such shell, and a
# literal `"west"` here resolved to nothing (tan-cli#453's actual defect --
# the caller's PATH-prepend was a per-command mitigation for this, not a fix
# of it). `tan.core.venv` is importable here: this file, unlike alp-sdk's, is
# `tan/planner/kconfig_symbols.py`, part of the `tan` package.
from tan.core.venv import west_program

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------
# Workspace guard (Task 1)
# ---------------------------------------------------------------------


def _kconfig_dir(zephyr_base: Path) -> Path:
    return zephyr_base / "scripts" / "kconfig"


def _kconfiglib_path(zephyr_base: Path) -> Path:
    return _kconfig_dir(zephyr_base) / "kconfiglib.py"


def _require_workspace() -> Path:
    """Return the bootstrapped `ZEPHYR_BASE`, or print the actionable
    message + exit(2) -- this mode's one deliberate deviation from the
    rest of the orchestrator's `OrchestratorError` -> exit(1) convention,
    marking "no workspace" as distinct from an ordinary usage error.

    This process itself never imports kconfiglib (only the rendered dumper
    does -- see `_DUMPER_SOURCE` -- in its own subprocess running inside
    Zephyr's own env; see `_load_board_symbols`); this just probes
    `kconfiglib.py`'s presence as the "is this ZEPHYR_BASE real" check.
    """
    raw = os.environ.get("ZEPHYR_BASE")
    if raw:
        zephyr_base = Path(raw)
        if _kconfiglib_path(zephyr_base).is_file():
            return zephyr_base
    print(
        "alp_orchestrate: --emit kconfig requires a bootstrapped Zephyr "
        "workspace (set ZEPHYR_BASE; west init/update v4.4.0)",
        file=sys.stderr,
    )
    sys.exit(2)


# ---------------------------------------------------------------------
# Hermetic symbol projection + envelope (Task 2) -- no kconfiglib import
# at module scope, so this half unit-tests with no Zephyr installed.
# ---------------------------------------------------------------------


def _envelope(board: str, core: str, symbols: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "board":         board,
        "core":          core,
        "symbols":       symbols,
    }


# ---------------------------------------------------------------------
# Approach-A Zephyr load (Task 3) -- workspace-dependent
# ---------------------------------------------------------------------

_STUB_CMAKELISTS = textwrap.dedent("""\
    # SPDX-License-Identifier: Apache-2.0
    # Auto-generated by alp_orchestrate/kconfig_symbols.py (#893) -- a
    # throwaway stub app whose sole purpose is to make Zephyr's own CMake
    # configure the Kconfig tree for one board so kconfiglib can be
    # pointed at the real env it computed.  Never built.
    cmake_minimum_required(VERSION 3.20.0)
    find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
    project(alp_emit_kconfig_stub)
    target_sources(app PRIVATE src/main.c)
    """)

_STUB_MAIN_C = "int main(void)\n{\n\treturn 0;\n}\n"

_KCONFIG_TARGET = "alpkconfigjson"

# The dumper Zephyr's `EXTRA_KCONFIG_TARGET` custom target runs, RENDERED onto
# disk fresh for every `_load_board_symbols` call rather than read off a fixed
# path -- see `_load_board_symbols`'s docstring for the PyInstaller-onefile /
# stale-build-dir hazard a fixed path would reintroduce. This replaces the
# earlier design: first a `__file__`-relative sibling walk, then a path
# anchored on the bound alp-sdk checkout (`REPO / "scripts" / "kconfig" /
# "alp_kconfig_dump.py"`) -- both coupled this module to a file living
# outside `tan`.
#
# Deliberately self-contained: this text becomes a standalone .py file that
# runs as a *subprocess*, inside Zephyr's own Kconfig environment, with its
# own path baked into the generated build tree by CMake -- so it must import
# NOTHING from `tan` or `alp_orchestrate` (neither package is guaranteed to
# be importable from wherever this file ends up, and under a frozen
# PyInstaller build there is no `tan` *source* on disk to import at all).
# Only the stdlib plus `kconfiglib`, which Zephyr's own env injects via
# ZEPHYR_BASE.
#
# `_project_symbols` therefore lives ONLY here, as plain literal source, not
# imported and not mirrored elsewhere in this module;
# `tests/core/test_kconfig_symbols.py` execs this constant in an isolated
# namespace (with a fake `kconfiglib` module) and asserts its projection
# output against a literal expected result -- an independent oracle, so a
# regression here fails a fast, hermetic test instead of surfacing only in a
# Zephyr-bootstrapped CI job.
_DUMPER_SOURCE = '''\
#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Dumps one board's promptable Kconfig symbols as JSON.

RENDERED BY tan (tan.planner.kconfig_symbols._load_board_symbols) fresh for
every `--emit kconfig` run, into that run's own scratch build tree -- this
file is never checked in and never lives in alp-sdk.

Runs INSIDE Zephyr's own Kconfig environment via the `EXTRA_KCONFIG_TARGET`
mechanism (`cmake/modules/kconfig.cmake` ~199-243, the same seam
`menuconfig`/`guiconfig`/`hardenconfig`/`traceconfig` use): a
`west build --cmake-only -- -DEXTRA_KCONFIG_TARGETS=alpkconfigjson
-DEXTRA_KCONFIG_TARGET_COMMAND_FOR_alpkconfigjson=<this>;--output;<path>`
registers a custom target that (per kconfig.cmake:227-242) runs

    ${CMAKE_COMMAND} -E env <COMMON_KCONFIG_ENV_SETTINGS...> \\\\
        ${PYTHON_EXECUTABLE} <this> --output <path> ${KCONFIG_ROOT}

i.e. Zephyr's CMake hands this script the *exact* env it computed for that
board/toolchain/module set (srctree, ARCH, BOARD_DIR, module Kconfigs, ...)
plus `KCONFIG_ROOT` as the trailing positional -- no env reconstruction
needed. `west build -d <build> -t alpkconfigjson` (a second, separate
invocation -- `add_custom_target` isn't built by `--cmake-only` alone) is
what actually runs it. See `tan.planner.kconfig_symbols._load_board_symbols`
for the caller.

Deliberately self-contained (imports nothing from `tan` or
`alp_orchestrate`): this file's own path is baked into the generated build
tree by the CMake invocation above, so it has to keep working from wherever
`tan` chose to write it -- see the caller for why that path is a fresh
scratch directory rather than a fixed one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# kconfiglib is a plain module file under $ZEPHYR_BASE/scripts/kconfig, not
# pip-installed -- ZEPHYR_BASE is guaranteed set in this script's env (see
# module docstring: COMMON_KCONFIG_ENV_SETTINGS always sets it).
sys.path.insert(0, os.path.join(os.environ["ZEPHYR_BASE"], "scripts", "kconfig"))
import kconfiglib  # noqa: E402


def _project_symbols(syms):
    """Project promptable symbols to `{name, type, prompt, depends,
    default, help}`, sorted by name.

    Only symbols with a prompt on ANY of their `MenuNode`s are kept -- a
    symbol can be declared in multiple locations (Zephyr's own
    `scripts/kconfig/kconfig.py::promptless()` checks the same way, with the
    same reasoning: "the symbol might be defined in multiple locations, we
    need to check all locations"). This matters in practice: many board/SoC
    `Kconfig.defconfig` fragments redeclare a symbol with a promptless
    `default`-only override block, and `Kconfig.zephyr` sources those BEFORE
    the canonical declaration -- so taking `nodes[0]` alone silently dropped
    real, user-facing symbols like `LOG` / `SERIAL` / `MAIN_STACK_SIZE`
    (alp-sdk #893). This is the only copy of this projection logic (see the
    module docstring) -- verified against a literal expected result in
    `tests/core/test_kconfig_symbols.py`, so the "first prompt-bearing node,
    not nodes[0]" behavior is pinned on purpose.
    """
    projected = []
    for sym in syms:
        node = next((n for n in sym.nodes if n.prompt), None)
        if node is None:
            continue
        default = None
        if sym.orig_defaults:
            default_expr, _cond = sym.orig_defaults[0]
            default = kconfiglib.expr_str(default_expr)
        projected.append({
            "name":    sym.name,
            "type":    kconfiglib.TYPE_TO_STR.get(sym.type, "unknown"),
            "prompt":  node.prompt[0],
            "depends": kconfiglib.expr_str(sym.direct_dep) or "",
            "default": default,
            "help":    getattr(node, "help", None) or "",
        })
    projected.sort(key=lambda entry: entry["name"])
    return projected


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("kconfig_root")
    args = ap.parse_args()

    kconf = kconfiglib.Kconfig(args.kconfig_root, warn=True, warn_to_stderr=False)
    symbols = _project_symbols(kconf.unique_defined_syms)
    # write-text-newline-exempt: scratch build dir, read back only by
    # tan.planner.kconfig_symbols._load_board_symbols
    args.output.write_text(json.dumps(symbols), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _write_stub_app(app_dir: Path) -> None:
    (app_dir / "src").mkdir(parents=True)
    # write-text-newline-exempt: throwaway stub app in a scratch build dir
    (app_dir / "CMakeLists.txt").write_text(_STUB_CMAKELISTS, encoding="utf-8")
    # write-text-newline-exempt: throwaway stub app in a scratch build dir
    (app_dir / "prj.conf").write_text("", encoding="utf-8")
    # write-text-newline-exempt: throwaway stub app in a scratch build dir
    (app_dir / "src" / "main.c").write_text(_STUB_MAIN_C, encoding="utf-8")


def _write_dumper(dumper_path: Path) -> None:
    """Render `_DUMPER_SOURCE` onto disk at `dumper_path`.

    Called once per `_load_board_symbols` run, into that run's own scratch
    tree (never a fixed/cached path) -- see `_DUMPER_SOURCE`'s docstring for
    why: a fixed path under a PyInstaller onefile extraction directory would
    be deleted at process exit while the CMake command baked into the build
    tree still names it, and a fixed path anywhere else would let a stale
    render survive a project change instead of being regenerated.
    """
    dumper_path.parent.mkdir(parents=True, exist_ok=True)
    # write-text-newline-exempt: rendered fresh into a scratch build dir
    dumper_path.write_text(_DUMPER_SOURCE, encoding="utf-8")


def _load_board_symbols(zephyr_base: Path, board_triple: str) -> list[dict[str, Any]]:
    """Approach A: configure a stub app for `board_triple`, render the
    dumper (`_DUMPER_SOURCE`) into this run's own scratch tree, then run it
    INSIDE Zephyr's own Kconfig env via the `EXTRA_KCONFIG_TARGET` mechanism
    (the same seam `west build -t menuconfig` uses -- see `_DUMPER_SOURCE`'s
    own module docstring). Returns the already-projected `list[dict]` the
    dumper wrote.

    `west build` is a west extension command -- it needs to run from
    inside a west workspace (a `.west/` upward from cwd), which
    `alp_orchestrate` itself is never a part of. `$ZEPHYR_BASE/..` is
    that workspace's topdir under every documented Zephyr `west init`
    layout (Getting Started's `~/zephyrproject/{.west, zephyr,
    modules,...}`, and the same layout pr-twister.yml's own `west init
    -m .../zephyr .` step produces).

    The dumper is rendered fresh into `tmp_dir` on every call and `tmp_dir`
    is deleted in this function's own `finally` below -- well after both
    `west build` invocations that read the rendered path have already run,
    so the render neither survives to be reused stale by a later call nor
    depends on anything outliving this process (see `_DUMPER_SOURCE`).
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="alp-emit-kconfig-"))
    try:
        app_dir = tmp_dir / "stub"
        _write_stub_app(app_dir)

        dumper_path = tmp_dir / "alp_kconfig_dump.py"
        _write_dumper(dumper_path)

        build_dir = tmp_dir / "build"
        output_json = build_dir / "alp_kconfig.json"
        # A CMake list (semicolon-separated -- COMMAND_EXPAND_LISTS on the
        # custom target splits it into argv tokens): the dumper script,
        # then its own `--output <path>` -- Zephyr appends ${KCONFIG_ROOT}
        # as the final token itself (kconfig.cmake:238).
        target_cmd = f"{dumper_path};--output;{output_json}"

        # `zephyr_base.parent` IS the workspace topdir (this function's own
        # docstring above), already the one `west_workspace_dir` verified
        # before `ZEPHYR_BASE` was ever set -- so `sdk_root=None` here is not
        # a second, unverified guess, just skipping a manifest re-check the
        # caller already made.
        west = west_program(str(zephyr_base.parent), None)
        configure_cmd = [
            west, "build", "--cmake-only",
            "-b", board_triple,
            "-d", str(build_dir),
            str(app_dir),
            "--",
            f"-DEXTRA_KCONFIG_TARGETS={_KCONFIG_TARGET}",
            f"-DEXTRA_KCONFIG_TARGET_COMMAND_FOR_{_KCONFIG_TARGET}={target_cmd}",
        ]
        proc = subprocess.run(configure_cmd, cwd=zephyr_base.parent,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise OrchestratorError(
                f"--emit kconfig: `west build --cmake-only -b "
                f"{board_triple}` failed:\n{proc.stderr.strip()}")

        # `add_custom_target` never runs at configure time -- `-t` builds
        # it explicitly (a second, separate `west build`).
        build_cmd = [west, "build", "-d", str(build_dir), "-t", _KCONFIG_TARGET]
        proc = subprocess.run(build_cmd, cwd=zephyr_base.parent,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise OrchestratorError(
                f"--emit kconfig: `west build -t {_KCONFIG_TARGET}` failed "
                f"for board '{board_triple}':\n{proc.stderr.strip()}")
        if not output_json.is_file():
            raise OrchestratorError(
                f"--emit kconfig: `west build -t {_KCONFIG_TARGET}` "
                f"completed but never wrote {output_json} -- never emit a "
                f"partial/empty menu")

        raw = output_json.read_text(encoding="utf-8")
        if not raw.strip():
            # `west build -t <target>` can report success (rc 0) while the
            # custom target's own command silently produced nothing -- a
            # 0-byte `output_json` used to reach `json.loads` below and
            # surface as a bare, uncoded `JSONDecodeError` rather than a
            # loud refusal. Never emit a partial/empty menu from an empty
            # artefact.
            raise OrchestratorError(
                f"--emit kconfig: `west build -t {_KCONFIG_TARGET}` "
                f"reported success but wrote an empty {output_json} -- "
                f"never emit a partial/empty menu")
        try:
            symbols = json.loads(raw)
        except json.JSONDecodeError as e:
            raise OrchestratorError(
                f"--emit kconfig: {output_json} is not valid JSON ({e}) -- "
                f"the `{_KCONFIG_TARGET}` dumper wrote a corrupt artefact, "
                f"not a partial/empty menu") from e
        if not isinstance(symbols, list):
            raise OrchestratorError(
                f"--emit kconfig: {output_json} parsed as JSON but is not a "
                f"list of symbols (got {type(symbols).__name__}) -- the "
                f"`{_KCONFIG_TARGET}` dumper wrote a corrupt artefact, not "
                f"a partial/empty menu")
        return symbols
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------


def emit_kconfig(project: BoardProject, core: Optional[str]) -> str:
    """`--emit kconfig`: the board-scoped, user-settable Kconfig symbol
    menu for the resolved `--core <id>` slice, as JSON.

    Validates `core` against the project's own resolved slices first
    (a plain `OrchestratorError` -- exit(1) via the cli's usual path),
    THEN checks the Zephyr workspace is bootstrapped (exit(2) -- see
    `_require_workspace`).
    """
    if not core:
        raise OrchestratorError(
            "--emit kconfig requires --core <id> (the SDK plans one "
            "Kconfig symbol menu per board, not per project)")
    if core not in project.cores:
        raise OrchestratorError(
            f"--core {core} not present in this board.yaml's cores "
            f"(have: {', '.join(sorted(project.cores))})")
    slice_ = project.cores[core]
    if not slice_.board:
        raise OrchestratorError(
            f"core '{core}' has no resolved Zephyr board target "
            f"(os: {slice_.os}); --emit kconfig only applies to "
            f"Zephyr cores")

    zephyr_base = _require_workspace()

    # The rendered dumper (`_DUMPER_SOURCE`, run inside Zephyr's own Kconfig
    # env -- see `_load_board_symbols`) already projects with the real
    # kconfiglib, so this is just the envelope wrap.
    symbols = _load_board_symbols(zephyr_base, slice_.board)
    envelope = _envelope(slice_.board, core, symbols)
    return json.dumps(envelope, indent=2) + "\n"
