# SPDX-License-Identifier: Apache-2.0
"""The planner CLI: argparse + main(), over one mode->text dispatch.

Kept in-package (imported by __init__ for the public `main` re-export, and by
__main__ as the `python -m tan.planner` entry) so nothing has to import
__main__ -- importing __main__ re-enters the package under runpy and warns.

RELOCATED (was alp-sdk `scripts/alp_orchestrate/cli.py`). The mode dispatch was
lifted out of `main()` into `emit_artefact()` so the in-process caller
(`tan.planner_root.emit`, which is how `tan build` gets its plan) and this argv
path render through the SAME branch table. Two copies of an eight-way `--emit`
switch is how one surface silently starts emitting differently from the other.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, Optional

from . import (
    OrchestratorError,
    emit_build_plan,
    emit_dts_partitions,
    emit_dts_reservations,
    emit_ipc_contract_h,
    emit_kconfig,
    emit_storage_mounts_c,
    emit_system_manifest,
    emit_tfm_sysbuild_conf,
    load_board_yaml,
)

if False:  # typing-only; keeps the runtime import graph unchanged
    from .models import BoardProject


def emit_artefact(project: "BoardProject", mode: str, *, board_yaml: Path,
                  build_root: Path = Path("build"),
                  core: Optional[str] = None) -> str:
    """Render one `--emit` mode for an already-loaded project.

    `core` is honoured by `kconfig` alone; every other mode ignores it -- the
    same asymmetry the flag's own help text records.
    """
    if mode == "system-manifest":
        return emit_system_manifest(project)
    if mode == "ipc-contract-h":
        return emit_ipc_contract_h(project)
    if mode == "dts-reservations":
        return emit_dts_reservations(project)
    if mode == "dts-partitions":
        return emit_dts_partitions(project)
    if mode == "storage-mounts-c":
        return emit_storage_mounts_c(project)
    if mode == "tfm-sysbuild-conf":
        return emit_tfm_sysbuild_conf(project)
    if mode == "build-plan":
        return emit_build_plan(project, board_yaml=board_yaml,
                               build_root=build_root)
    if mode == "kconfig":
        return emit_kconfig(project, core)
    raise OrchestratorError(f"unknown emit mode '{mode}'")


def main(argv: Optional[Iterable[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Planner/emit CLI for board.yaml (the executor was "
                     "retired -- ADR-0020 Phase 4).")
    parser.add_argument("--input", type=Path, default=Path("board.yaml"),
                        help="Path to the project's board.yaml.")
    parser.add_argument("--build-root", type=Path,
                        default=Path("build"),
                        help="Build root directory (used by --emit build-plan).")
    parser.add_argument("--core", default=None,
                        help="Core id to scope a per-core emit mode to "
                             "(required by --emit kconfig; every other "
                             "mode ignores it).")
    parser.add_argument("--emit", default=None,
                        choices=["system-manifest", "ipc-contract-h",
                                 "dts-reservations", "dts-partitions",
                                 "storage-mounts-c",
                                 "tfm-sysbuild-conf", "build-plan",
                                 "kconfig"],
                        help="Skip the build; just emit one of the "
                             "generated artefacts to stdout.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        project = load_board_yaml(args.input)
    except OrchestratorError as e:
        print(f"alp-orchestrate: {e}", file=sys.stderr)
        return 1

    if args.emit:
        try:
            sys.stdout.write(emit_artefact(
                project, args.emit, board_yaml=args.input,
                build_root=args.build_root, core=args.core))
        except OrchestratorError as e:
            print(f"alp-orchestrate: {e}", file=sys.stderr)
            return 1
        return 0

    # ADR-0020 Phase 4 (preview): the SDK-side executor was retired --
    # this module only plans/emits now.  Building is an external
    # consumer's job (against `--emit build-plan`'s JSON contract).
    print("alp-orchestrate: no executor -- pass --emit to print a "
          "generated artefact (e.g. --emit build-plan)", file=sys.stderr)
    return 2
