# SPDX-License-Identifier: Apache-2.0
"""Render `alp_project.py`'s relocated emit modes in-process, from `tan.planner`.

`tan.planner_root.emit` covers the eight modes alp-sdk's `alp_orchestrate` CLI
exposed. This module covers the OTHER front door: the modes whose renderer moved
into `tan/planner/` but whose `--emit` flag lives on `scripts/alp_project.py`.
`tan generate` is the only caller, and until it routed through here `tan`
shelled that script for every target -- so nothing had actually moved off
alp-sdk's Python, whatever the relocation commit said.

**The split is read off `metadata/emit-registry-v1.json`, not guessed.** A mode
is renderable here when its `owner.module` is `scripts/alp_orchestrate/<x>.py`
-- that directory is exactly what relocated. Of the eleven modes `tan generate`
can reach, five qualify:

    zephyr-conf     kconfig.py   _slice_alp_conf
    cmake-args      kconfig.py   _slice_cmake_args
    yocto-conf      kconfig.py   _slice_local_conf
    os-topology     topology.py  emit_os_topology
    ipc-contract-h  headers.py   emit_ipc_contract_h

The remaining six stay in alp-sdk and keep being spawned: `dts-overlay`,
`native-sim-overlay`, `hw-info-h`, `west-libraries` and `carrier-netlist` are
owned by `scripts/alp_project_emit/`, and `zephyr-board` by
`scripts/gen_zephyr_board.py`. None of those relocated.

**Byte-identity is the whole contract**, so the per-core dispatch below mirrors
`_run_v2_per_core_emit` in `scripts/alp_project.py` line for line -- including
the three details a rewrite loses: `os: off` cores are skipped before the
OS-class check, `zephyr-conf --core <id>` emits the slice with NO
`# --- core: ... ---` marker while every other combination emits one, and the
trailing newline is added only when the last part lacks it.

**`write` uses `newline=""`, matching the SDK's own `_write_or_print`.** Windows
text mode would translate every `\\n` to `\\r\\n`; the SDK's emits are LF on
every host, and a CRLF `alp.conf` flips every emit-snapshot golden in alp-sdk.

**Which OS classes a per-core mode accepts is metadata, not a table here.**
`compatible.os` in the emit registry carries it (`zephyr-conf` -> zephyr,
`cmake-args` -> baremetal+zephyr, `yocto-conf` -> yocto) and alp-sdk's
`scripts/check_emit_registry.py` keeps it honest against the real code. Copying
it into `tan` would be a second source of truth for a fact tan must not learn
(ADR-0017 / I-26).
"""

from __future__ import annotations

import json
from pathlib import Path

from tan.planner_root import bind_sdk_root

__all__ = ["IN_PROCESS_MODES", "PlannerEmitError", "render", "unavailable", "write"]

#: The relocated modes reachable through `tan generate` (see the module
#: docstring for how the registry decides this set).
IN_PROCESS_MODES = frozenset(
    {"zephyr-conf", "cmake-args", "yocto-conf", "os-topology", "ipc-contract-h"}
)

#: The three per-core config slices, each mapped to the `tan.planner` renderer
#: `alp_project.py` calls for it. Rendered by name via `getattr` so this table
#: cannot drift from the package's own public surface.
_SLICE_RENDERER = {
    "zephyr-conf": "_slice_alp_conf",
    "yocto-conf": "_slice_local_conf",
    "cmake-args": "_slice_cmake_args",
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
    """
    bind_sdk_root(sdk_root)
    import tan.planner as planner  # noqa: PLC0415 -- must follow the bind

    project = planner.load_board_yaml(Path(board_yaml))
    if mode in _SLICE_RENDERER:
        return _render_per_core(planner, project, mode, core=core, sdk_root=sdk_root)
    if mode == "os-topology":
        # Not in `emit_artefact`: alp-sdk's `alp_orchestrate` CLI never exposed
        # it, only `alp_project.py` did.
        return planner.emit_os_topology(project)
    # `ipc-contract-h` (and anything else relocated later that the planner CLI
    # already knows) goes through the ONE dispatch, so a mode cannot render
    # differently depending on which front door reached it.
    from tan.planner.cli import emit_artefact  # noqa: PLC0415

    return emit_artefact(project, mode, board_yaml=Path(board_yaml), core=core)


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
