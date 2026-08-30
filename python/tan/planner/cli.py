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

`--emit` also accepts `alp_project.py`'s OWN modes (`zephyr-conf`,
`carrier-netlist`, ...), rendered through `tan.planner_emit` -- the same
in-process engine `tan generate` uses, so a mode still has exactly one renderer
whichever front door reached it. They print RAW ARTEFACT BYTES, undecorated,
because alp-sdk's `scripts/check_emit_snapshots.py` diffs this stdout against a
committed golden; a trailing newline of our own would fail it.

`--emit scaffold` is the third family, and the one mode that takes NO board.yaml
-- it materialises a NEW project, so it is dispatched before `--input` is read,
the same order `alp_project.main()` dispatches it in. It renders through
`tan.planner.template` (relocated from `scripts/alp_template.py`) against the
LIVE catalog of the bound SDK checkout. `tan init` does NOT come this way and
must not: it serves a customer with no SDK at all, from tan's own vendored
capture (`tan.core.scaffold`, invariant I-32).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, Optional

from tan import planner_emit

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
from .paths import REPO
from .template import (
    TemplateError,
    emit_scaffold,
    find_template_by_cores,
    load_catalog,
)

if False:  # typing-only; keeps the runtime import graph unchanged
    from .models import BoardProject

#: The eight modes alp-sdk's `alp_orchestrate` CLI exposed, every one of them
#: served by `emit_artefact`.
ORCHESTRATOR_MODES = (
    "system-manifest", "ipc-contract-h", "dts-reservations", "dts-partitions",
    "storage-mounts-c", "tfm-sysbuild-conf", "build-plan", "kconfig",
)

#: The rest of `--emit`: `alp_project.py`'s modes, served by `tan.planner_emit`.
#: DERIVED from `IN_PROCESS_MODES` rather than listed again -- a second list is
#: how `tan generate` and this entry would drift apart on which modes exist.
#: `ipc-contract-h` is in both sets and stays with `emit_artefact`; sorted, so
#: `--help` does not reorder itself between interpreters.
PROJECT_MODES = tuple(
    sorted(planner_emit.IN_PROCESS_MODES.difference(ORCHESTRATOR_MODES)))

#: The modes served by `tan.planner.template`, which take `--template`/`--sku`
#: instead of a board.yaml. Deliberately NOT folded into `IN_PROCESS_MODES`:
#: that set is `tan generate`'s target list, and `tan generate --output` writes
#: ONE artefact, where a scaffold is a whole project's worth of files. The
#: customer surface is frozen (`tan init` scaffolds); this entry is the gate's.
TEMPLATE_MODES = ("scaffold",)


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


def _parse_cores_arg(raw: str) -> dict[str, str]:
    """`--cores` (alp-sdk#1652): `core_id:os[,core_id:os...]` -- the
    topology an IDE wizard already knows (which cores it's targeting and
    which OS each runs), NOT a directory. RELOCATED verbatim from
    `alp_project._parse_cores_arg`. `--cores` is a SELECTOR over the
    catalog's existing templates (see `find_template_by_cores`'s
    docstring for why this stops short of an arbitrary core -> app-dir
    renderer): the directory each core lands in is whatever the matched
    template already uses, same as picking that template by `--template`
    would give.
    """
    cores: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError(
                f"--cores entry {entry!r} must be core_id:os (e.g. "
                f"m55_hp:zephyr)")
        core_id, _, os_name = entry.partition(":")
        core_id, os_name = core_id.strip(), os_name.strip()
        if not core_id or not os_name:
            raise ValueError(
                f"--cores entry {entry!r} must be core_id:os (e.g. "
                f"m55_hp:zephyr)")
        if core_id in cores:
            raise ValueError(f"--cores names {core_id!r} more than once")
        cores[core_id] = os_name
    if not cores:
        raise ValueError("--cores must name at least one core_id:os pair")
    return cores


def _emit_scaffold(
    template: Optional[str], sku: Optional[str], cores: Optional[str] = None,
) -> int:
    """`--emit scaffold --template <id> --sku <SKU>`, or `--emit scaffold
    --cores <core_id:os,...> --sku <SKU>` (alp-sdk#1652), straight to
    stdout.

    `--template`/`--cores` are mutually exclusive alternative ways to pick
    the template -- `--cores` resolves to a `template_id` via
    `find_template_by_cores`, then falls into the exact same
    `emit_scaffold()` call `--template` would reach, exactly as
    `alp_project._run_scaffold_emit` refuses/resolves them. `--sku` is
    still required either way and refused ONE AT A TIME, in that order,
    so a caller that omitted both still learns about `--template`/
    `--cores` first. `argparse` cannot express "required only for this
    `--emit`", which is why this is a hand-check.
    """
    if template and cores:
        print("alp-orchestrate: --emit scaffold takes --template OR "
              "--cores, not both", file=sys.stderr)
        return 1
    if not template and not cores:
        print("alp-orchestrate: --emit scaffold requires --template <id> "
              "or --cores <core_id:os,...>", file=sys.stderr)
        return 1
    if not sku:
        print("alp-orchestrate: --emit scaffold requires --sku <SKU>",
              file=sys.stderr)
        return 1
    if cores:
        try:
            cores_topology = _parse_cores_arg(cores)
        except ValueError as e:
            print(f"alp-orchestrate: {e}", file=sys.stderr)
            return 1
        try:
            record = find_template_by_cores(load_catalog(), cores_topology)
        except TemplateError as e:
            print(f"alp-orchestrate: {e}", file=sys.stderr)
            return 1
        template = record["id"]
    try:
        sys.stdout.write(emit_scaffold(template, sku))
    except TemplateError as e:
        print(f"alp-orchestrate: {e}", file=sys.stderr)
        return 1
    return 0


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
    parser.add_argument("--template", default=None,
                        help="metadata/templates/catalog-v1.json template id; "
                             "required for --emit scaffold (unless --cores is "
                             "given instead).")
    parser.add_argument("--cores", default=None,
                        help="core_id:os[,core_id:os...] topology (e.g. "
                             "'m55_hp:zephyr,m55_he:zephyr'); an alternative "
                             "to --template for --emit scaffold -- selects "
                             "whichever catalog template's own cores: "
                             "topology matches exactly (alp-sdk#1652). "
                             "Mutually exclusive with --template.")
    parser.add_argument("--sku", default=None,
                        help="Target SoM SKU (e.g. the one a scaffold is "
                             "retargeted onto); required for --emit scaffold, "
                             "and must be one of the template's declared "
                             "supported.som_skus.")
    parser.add_argument("--emit", default=None,
                        choices=[*ORCHESTRATOR_MODES, *PROJECT_MODES,
                                 *TEMPLATE_MODES],
                        help="Skip the build; just emit one of the "
                             "generated artefacts to stdout.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.emit in TEMPLATE_MODES:
        # Before `--input` is touched: a scaffold IS the new project, so there
        # is no board.yaml to load yet.
        return _emit_scaffold(args.template, args.sku, args.cores)

    if args.emit in PROJECT_MODES:
        # Straight to the relocated renderers, deliberately WITHOUT the
        # `load_board_yaml` below: `carrier-netlist` / `composed-route-table`
        # read the raw board.yaml dict (mirroring `alp_project.main()`, which
        # dispatches them before its per-core machinery), so loading here would
        # refuse a board the SDK's own front door serves.
        try:
            sys.stdout.write(planner_emit.render(
                args.emit, sdk_root=REPO, board_yaml=args.input,
                core=args.core))
        except (OrchestratorError, planner_emit.PlannerEmitError) as e:
            print(f"alp-orchestrate: {e}", file=sys.stderr)
            return 1
        return 0

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
