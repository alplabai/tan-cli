# SPDX-License-Identifier: Apache-2.0
"""Render `alp_project.py`'s emit modes in-process, from `tan.planner`.

`tan.planner_root.emit` covers the eight modes alp-sdk's `alp_orchestrate` CLI
exposed. This module covers the OTHER front door: the modes whose renderer moved
into `tan/planner/` but whose `--emit` flag lived on `scripts/alp_project.py`.
`tan generate` is the only caller, and until it routed through here `tan` shelled
that script -- so nothing had actually moved off alp-sdk's Python, whatever the
relocation commit said.

**All twelve modes `tan generate` can reach now render here.** The last six --
`dts-overlay`, `native-sim-overlay`, `hw-info-h`, `west-libraries`,
`carrier-netlist` and `zephyr-board` -- relocated in the same MOVE-not-rewrite
shape as the first five, plus `composed-route-table` (the older debug view of
`carrier-netlist`'s own route rows), so `IN_PROCESS_MODES` is the whole
reachable set and `tan generate` spawns nothing:

    zephyr-conf          kconfig.py                  _slice_alp_conf
    cmake-args           kconfig.py                  _slice_cmake_args
    yocto-conf           kconfig.py                  _slice_local_conf
    os-topology          topology.py                 emit_os_topology
    ipc-contract-h       headers.py                   emit_ipc_contract_h
    dts-overlay          project_emit/dts.py          _emit_dts_overlay
    native-sim-overlay   project_emit/native_sim.py   _emit_native_sim_overlay
    hw-info-h            project_emit/hw_info.py      _emit_hw_info_h
    west-libraries       project_emit/west_libs.py    _emit_west_libraries
    carrier-netlist      project_emit/bom_netlist.py  _emit_carrier_netlist
    composed-route-table project_emit/bom_netlist.py  _emit_composed_route_table
    zephyr-board         zephyr_board.py              emit_zephyr_board

The mapping is not guessed: `metadata/emit-registry-v1.json` names each mode's
`owner.module`, and every module above is one of those owners, relocated.

**Byte-identity is the whole contract**, so the dispatch below mirrors
`_run_v2_per_core_emit` and `main()` in `scripts/alp_project.py` line for line --
including the details a rewrite loses:

* `os: off` cores are skipped before the OS-class check;
* `zephyr-conf --core <id>` emits the slice with NO `# --- core: ... ---` marker
  while every other per-core combination emits one;
* the trailing newline is added only when the last part lacks it;
* `dts-overlay` and `west-libraries` union across ZEPHYR/BAREMETAL cores when
  unscoped, while `hw-info-h` unions across ALL cores and picks a primary;
* `dts-overlay` needs the Zephyr/baremetal core-id LIST, not just the union, so
  the AEN i3c wiring can ask whether `m55_he` is in scope at all;
* `native-sim-overlay` takes no `--core` scoping of any kind;
* `carrier-netlist` and `composed-route-table` never build a per-core slice
  model -- both read the raw board.yaml dict, the SoM preset and the
  inline-or-preset board, exactly as `main()` does before the per-core
  machinery is ever reached.

**`write` uses `newline=""`, matching the SDK's own `_write_or_print`.** Windows
text mode would translate every `\\n` to `\\r\\n`; the SDK's emits are LF on every
host, and a CRLF `alp.conf` flips every emit-snapshot golden in alp-sdk.

**Which OS classes a per-core mode accepts is metadata, not a table here.**
`compatible.os` in the emit registry carries it (`zephyr-conf` -> zephyr,
`cmake-args` -> baremetal+zephyr, `yocto-conf` -> yocto) and alp-sdk's
`scripts/check_emit_registry.py` keeps it honest against the real code. Copying
it into `tan` would be a second source of truth for a fact tan must not learn
(ADR-0017 / I-26).

**`metadata/**` never relocated.** Every emitter here reads it out of the
RESOLVED `sdk_root` via `tan.planner.paths`, which is why `bind_sdk_root` runs
before the planner is imported at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tan.planner_root import bind_sdk_root

__all__ = [
    "IN_PROCESS_MODES",
    "TREE_MODES",
    "PlannerEmitError",
    "render",
    "render_tree",
    "unavailable",
    "write",
    "write_tree",
]

#: Every mode `tan generate` can reach, all of them served here (see the module
#: docstring for how the registry decides the set).
IN_PROCESS_MODES = frozenset({
    "zephyr-conf",
    "cmake-args",
    "yocto-conf",
    "os-topology",
    "ipc-contract-h",
    "dts-overlay",
    "native-sim-overlay",
    "hw-info-h",
    "west-libraries",
    "carrier-netlist",
    "composed-route-table",
    "zephyr-board",
})

#: The modes that write a DIRECTORY of files rather than one stream, and so go
#: through `render_tree`/`write_tree` instead of `render`/`write`. One today:
#: `--emit zephyr-board` emits a whole Zephyr board tree named per SKU+core.
TREE_MODES = frozenset({"zephyr-board"})

#: The three per-core config slices, each mapped to the `tan.planner` renderer
#: `alp_project.py` calls for it. Rendered by name via `getattr` so this table
#: cannot drift from the package's own public surface.
_SLICE_RENDERER = {
    "zephyr-conf": "_slice_alp_conf",
    "yocto-conf": "_slice_local_conf",
    "cmake-args": "_slice_cmake_args",
}

#: `carrier-netlist` / `composed-route-table` share one resolution shape (see
#: `_render_route_table`); this maps each to the emitter function that owns it.
_ROUTE_TABLE_EMITTER = {
    "carrier-netlist": "_emit_carrier_netlist",
    "composed-route-table": "_emit_composed_route_table",
}

#: The emit registry, relative to the SDK checkout root.
_REGISTRY = ("metadata", "emit-registry-v1.json")

#: The one file whose absence means "this is not a usable alp-sdk checkout".
#: The same probe `build_cmd._emit_plan` makes, deliberately: the planner is
#: tan's now, so requiring the SDK to still ship `alp_orchestrate` would refuse
#: precisely the releases this relocation exists to enable.
_SDK_PROBE = ("metadata", "schemas", "board.schema.json")


class PlannerEmitError(Exception):
    """The planner refused this emit. Carries the message a user should read."""


def _joined(root: Path, parts: tuple[str, ...]) -> Path:
    for part in parts:
        root = root / part
    return root


def _os_classes(sdk_root: Path) -> dict[str, tuple[str, ...]]:
    """`{mode: (os_class, ...)}` from the emit registry, for the modes that
    restrict one. `compatible.os == ["any"]` means no restriction, so it is
    absent from the result -- mirroring `_EMIT_OS_CLASSES.get(mode) is None`.
    """
    try:
        doc = json.loads(_joined(sdk_root, _REGISTRY).read_text(encoding="utf-8"))
        modes = doc["modes"]
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as err:
        raise PlannerEmitError(
            f"`{_joined(sdk_root, _REGISTRY)}` is missing or unreadable ({err}); "
            "it declares which OS classes each per-core emit mode accepts."
        ) from err
    classes: dict[str, tuple[str, ...]] = {}
    for entry in modes:
        allowed = ((entry.get("compatible") or {}).get("os")) or []
        if allowed and "any" not in allowed:
            classes[entry.get("mode")] = tuple(allowed)
    return classes


def unavailable(sdk_root: Path) -> str | None:
    """Why the in-process path cannot serve `sdk_root`, or `None` when it can.

    A REASON, never a bool: `tan generate` reports it, because a fallback nobody
    can see is how a port gets believed before it happened.

    Three ways it comes back non-`None`, and all three are a customer's layout
    rather than a bug: the path is not a usable checkout, its emit registry is
    unreadable, or this build of `tan` cannot import the planner at all (a
    source checkout or frozen binary without `PyYAML`/`jsonschema` -- which
    `tan.planner.loader` reports by calling `sys.exit`, not by raising).
    """
    probe = _joined(sdk_root, _SDK_PROBE)
    if not probe.is_file():
        return f"`{probe}` is absent, so `{sdk_root}` is not a usable alp-sdk checkout"
    try:
        _os_classes(sdk_root)
        bind_sdk_root(sdk_root)
        import tan.planner  # noqa: F401,PLC0415  -- the availability probe IS the import
    except SystemExit as err:
        return f"the relocated planner exited on import (code {err.code})"
    except PlannerEmitError as err:
        return str(err)
    except Exception as err:  # noqa: BLE001 -- any import failure is a fallback, not a crash
        return f"the relocated planner could not be imported: {type(err).__name__}: {err}"
    return None


def render(
    mode: str,
    *,
    sdk_root: Path,
    board_yaml: Path,
    core: str | None = None,
) -> str:
    """One relocated emit mode as text. Raises on any refusal.

    Every exception is the CALLER's to turn into an envelope: in a subprocess a
    planner blow-up arrived as a non-zero exit code, and in-process it lands
    here instead.

    `TREE_MODES` are refused here rather than silently mis-served: they write a
    directory, so a caller that asked for text would get a path it never wrote.
    """
    if mode in TREE_MODES:
        raise PlannerEmitError(
            f"--emit {mode} writes a directory of files, not a stream; "
            "render_tree() serves it.")

    bind_sdk_root(sdk_root)
    import tan.planner as planner  # noqa: PLC0415 -- must follow the bind

    if mode in _ROUTE_TABLE_EMITTER:
        # NOT via `load_board_yaml`: `alp_project.main()` dispatches these two
        # modes before the per-core slice machinery is reached, off the raw
        # board.yaml dict alone. Routing them through the v2 loader would
        # resolve a topology the emitter never looks at -- and could refuse a
        # board the SDK's own front door serves.
        return _render_route_table(mode, board_yaml)

    project = planner.load_board_yaml(Path(board_yaml))
    if mode in _SLICE_RENDERER:
        return _render_per_core(planner, project, mode, core=core, sdk_root=sdk_root)
    if mode == "os-topology":
        # Not in `emit_artefact`: alp-sdk's `alp_orchestrate` CLI never exposed
        # it, only `alp_project.py` did.
        return planner.emit_os_topology(project)
    if mode in _V1_SHAPED_MODES:
        return _render_v1_shaped(project, mode, core=core)
    # `ipc-contract-h` (and anything else relocated later that the planner CLI
    # already knows) goes through the ONE dispatch, so a mode cannot render
    # differently depending on which front door reached it.
    from tan.planner.cli import emit_artefact  # noqa: PLC0415

    return emit_artefact(project, mode, board_yaml=Path(board_yaml), core=core)


def render_tree(
    mode: str,
    *,
    sdk_root: Path,
    board_yaml: Path,
    core: str | None = None,
) -> dict[str, str]:
    """`{filename: content}` for a mode that writes a DIRECTORY.

    Keys are the FILE names, with the generator's own `<board_dir>/` prefix
    already stripped -- exactly the split `alp_project.py` does before joining
    each name onto `--output`, because `--output` names the board directory
    itself (`docs/porting-new-som.md`'s `--output build/boards/<board>/`, then
    `west build --board-root build/boards`).
    """
    if mode not in TREE_MODES:
        raise PlannerEmitError(
            f"--emit {mode} writes a single stream, not a directory; "
            "render() serves it.")

    bind_sdk_root(sdk_root)
    import tan.planner as planner  # noqa: PLC0415 -- must follow the bind

    if core is None:
        # `alp_project.py` refuses the same way: one board tree is one core's.
        raise PlannerEmitError(
            f"--emit {mode} requires --core (it generates one core's board tree)")

    project = planner.load_board_yaml(Path(board_yaml))
    if core not in project.cores:
        raise PlannerEmitError(
            f"--core {core} not present in board.yaml "
            f"(known: {sorted(project.cores.keys())})")

    from tan.planner.paths import METADATA_ROOT  # noqa: PLC0415
    from tan.planner.zephyr_board import emit_zephyr_board  # noqa: PLC0415

    files = emit_zephyr_board(project.sku, core, METADATA_ROOT)
    return {relpath.split("/", 1)[1]: content for relpath, content in files.items()}


def _render_route_table(mode: str, board_yaml: Path) -> str:
    """`--emit carrier-netlist` / `--emit composed-route-table`, mirroring
    `alp_project.main()`'s own branch (the two share one dispatch there too)."""
    from tan.planner.loader import _load_yaml  # noqa: PLC0415
    from tan.planner.paths import METADATA_ROOT  # noqa: PLC0415
    from tan.planner.project_loader import (  # noqa: PLC0415
        _resolve_inline_or_preset_board,
        _resolve_sku,
    )

    project = _load_yaml(Path(board_yaml))
    # PHRASED, not a bare KeyError. alp-sdk reached this point only after its
    # rich diagnostic validator had run; here the schema check happens in
    # `load_board_yaml`, which this mode deliberately does not call, so the one
    # field the emitter dereferences is checked by hand.
    sku = (project.get("som") or {}).get("sku")
    if not isinstance(sku, str) or not sku:
        raise PlannerEmitError(
            f"{board_yaml}: `som.sku` is missing or is not a string; "
            f"--emit {mode} renders the SoM's pad routes against it.")
    sku_preset = _resolve_sku(sku, METADATA_ROOT)
    board_preset = _resolve_inline_or_preset_board(project, METADATA_ROOT)
    from tan.planner.project_emit import bom_netlist  # noqa: PLC0415

    emitter = getattr(bom_netlist, _ROUTE_TABLE_EMITTER[mode])
    return emitter(project, sku_preset, board_preset, METADATA_ROOT)


#: The four modes `alp_project.py` re-fitted onto the v2 schema by projecting the
#: resolved project back into the v1 `board:`-wrapper dict its emitters read.
#: That projection is `_run_v2_per_core_emit`'s `project_v1_shaped`, reproduced
#: verbatim in `_v1_shaped_project` -- these emitters take a plain dict, not a
#: `BoardProject`, and changing that would be the rewrite this move is not.
_V1_SHAPED_MODES = frozenset({
    "dts-overlay", "native-sim-overlay", "hw-info-h", "west-libraries",
})


def _v1_shaped_project(project) -> dict[str, Any]:
    """The legacy `board:`-wrapper dict the four relocated emitters consume.

    Verbatim from `alp_project._run_v2_per_core_emit`. The public board.yaml
    schema no longer uses this wrapper, but it is the shape those emitters read,
    and reshaping them instead would be a rewrite.
    """
    return {
        "som": {
            "sku":    project.sku,
            "hw_rev": project.hw_rev,
        },
        "pins": list(project.raw.get("pins") or []),
        "board": ({
            "name":   project.board_name,
            "hw_rev": project.board_hw_rev,
        } if project.board_name else None),
    }


def _render_v1_shaped(project, mode: str, *, core: str | None) -> str:
    """`dts-overlay` / `native-sim-overlay` / `hw-info-h` / `west-libraries`,
    mirroring `alp_project._run_v2_per_core_emit`'s project-wide section.

    The `--core` validity check runs first for all four, as it does there: an
    unknown core id is a refusal, never a `KeyError` from inside an emitter.
    """
    if core is not None and core not in project.cores:
        raise PlannerEmitError(
            f"--core {core} not present in board.yaml "
            f"(known: {sorted(project.cores.keys())})")

    from tan.planner.project_emit.dts import _emit_dts_overlay  # noqa: PLC0415
    from tan.planner.project_emit.hw_info import _emit_hw_info_h  # noqa: PLC0415
    from tan.planner.project_emit.native_sim import (  # noqa: PLC0415
        _emit_native_sim_overlay,
    )
    from tan.planner.project_emit.west_libs import (  # noqa: PLC0415
        _emit_west_libraries,
    )

    shaped = _v1_shaped_project(project)

    if mode == "dts-overlay":
        # The DTS overlay is shaped by the board header (bus aliases +
        # alp,pin-array) which is a SoM-mounting fact, not a per-core fact.
        # v2 contributes only the peripherals list: union across
        # Zephyr/baremetal cores (or one core when --core is set).
        if core is not None:
            slice_ = project.cores[core]
            return _emit_dts_overlay(
                shaped, project.som_preset, project.board_preset,
                v2_peripherals=sorted(set(slice_.peripherals)),
                v2_core_id=core,
                v2_core_os=slice_.os,
                v2_core_ids=[core],
            )
        union: set[str] = set()
        zephyr_core_ids: list[str] = []
        for core_id, slice_ in project.cores.items():
            if slice_.os in ("zephyr", "baremetal"):
                union.update(slice_.peripherals)
                zephyr_core_ids.append(core_id)
        return _emit_dts_overlay(
            shaped, project.som_preset, project.board_preset,
            v2_peripherals=sorted(union),
            v2_core_ids=zephyr_core_ids,
        )

    if mode == "native-sim-overlay":
        # native_sim GPIO emulation -- board-agnostic (the E1M pad map is a
        # SoM-mounting fact), so no --core / peripheral scoping applies.
        return _emit_native_sim_overlay(shaped)

    if mode == "hw-info-h":
        # hw-info-h is a project-level emit even under v2 -- consumers
        # `#include` it from any slice. --core picks which slice's OS lands in
        # ALP_HW_BUILD_OS; absent --core, primary-core rules apply.
        return _emit_hw_info_h(
            shaped, project.som_preset, project.board_preset,
            v2_cores={cid: s.os for cid, s in project.cores.items()},
            v2_selected_core=core,
        )

    # west-libraries
    if core is not None:
        v2_libraries = sorted(set(project.cores[core].libraries))
    else:
        union_l: set[str] = set()
        for slice_ in project.cores.values():
            if slice_.os in ("zephyr", "baremetal"):
                union_l.update(slice_.libraries)
        v2_libraries = sorted(union_l)
    return _emit_west_libraries(
        shaped, project.som_preset, project.board_preset,
        v2_libraries=v2_libraries,
        v2_project_libraries=sorted(project.libraries),
    )


def _render_per_core(planner, project, mode: str, *, core: str | None,
                     sdk_root: Path) -> str:
    """`zephyr-conf` / `cmake-args` / `yocto-conf`, mirroring
    `alp_project._run_v2_per_core_emit`'s per-core section exactly."""
    if core is not None and core not in project.cores:
        raise PlannerEmitError(
            f"--core {core} not present in board.yaml "
            f"(known: {sorted(project.cores.keys())})"
        )
    if project.libraries:
        # Resolved up front purely to VALIDATE: an unknown library name or a
        # failed `requires:` constraint must be one clean line (ADR 0018), not a
        # traceback from the middle of a slice render.
        from tan.planner.libraries import resolve_selection  # noqa: PLC0415

        resolve_selection(project)

    allowed_os = _os_classes(sdk_root).get(mode)
    slice_renderer = getattr(planner, _SLICE_RENDERER[mode])
    core_ids = [core] if core is not None else sorted(project.cores.keys())

    parts: list[str] = []
    for core_id in core_ids:
        slice_ = project.cores[core_id]
        if slice_.os == "off":
            continue
        if allowed_os is not None and slice_.os not in allowed_os:
            if core is None:
                # The unscoped sum-across-cores form is explicitly a "give me
                # everything applicable" query, so it skips rather than refuses.
                continue
            raise PlannerEmitError(
                f"--core {core_id} has os: {slice_.os}, which --emit {mode} does "
                f"not support (supported os: {', '.join(allowed_os)})"
            )
        if mode == "zephyr-conf" and core is not None:
            # The canonical per-core path emits the slice VERBATIM -- no section
            # marker -- so a `west build` driven by alp-sdk's CMakeLists.txt and
            # one driven by `tan` off the build plan cannot diverge on Kconfig.
            parts.append(slice_renderer(project, slice_))
        else:
            parts.append(f"# --- core: {core_id} ({slice_.os}) ---")
            parts.append(slice_renderer(project, slice_))
    return "\n".join(parts) + ("\n" if parts and not parts[-1].endswith("\n") else "")


def write(text: str, destination: Path) -> None:
    """Write `text` to `destination`, byte for byte, creating its parent.

    `newline=""` is load-bearing -- see the module docstring. The SDK's
    `_write_or_print` writes exactly this way, and the whole point of the
    in-process path is that the bytes on disk do not change.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8", newline="")


def write_tree(files: dict[str, str], destination: Path) -> None:
    """Write a `render_tree` result into `destination`, which IS the board
    directory. Same `newline=""` contract as `write`, per file.

    Sorted, so the order files appear on disk (and in any log that follows the
    writes) does not depend on dict iteration order across interpreters.
    """
    destination.mkdir(parents=True, exist_ok=True)
    for name in sorted(files):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(files[name], encoding="utf-8", newline="")
