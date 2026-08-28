#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Per-core OS-class taxonomy + the topology view (issue #95).

A core's runtime is fixed by its Cortex class -- Cortex-A -> Yocto (Linux),
Cortex-M -> Zephyr (RTOS) -- not chosen freely. This module owns the
allowed-OS set read from the board schema, plus `core_os_topology` /
`emit_os_topology`, the per-core OS facts an IDE/tool renders. Extracted from
alp_orchestrate as the #285 topology seam and re-exported from the package
__init__, so callers + alp_project.py keep importing the same names unchanged.

The default-OS rule itself (`_default_os_from_core_type` / `CLASS_RUNTIMES` /
`_cross_class_os` below) is IMPORTED, not defined here, as of tan-cli#870:
`tan.core.os_class` now owns it, re-exported under these same names so every
caller and test that already imports them from this module is unaffected. See
that module's docstring for why the rule moved somewhere `tan.planner`'s
process-global SDK-root binding cannot reach -- `tan presets`' `allowedOs`
field needs the identical convention without paying this package's bind-first
import cost.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tan.core.os_class import CLASS_RUNTIMES  # noqa: F401  (re-export: unchanged public name)
from tan.core.os_class import allowed_os_for_core as _allowed_os_for_core_shared
from tan.core.os_class import cross_class_os as _cross_class_os  # noqa: F401  (re-export: validate.py's `from .topology import ... _cross_class_os`)
from tan.core.os_class import default_os_from_core_type as _default_os_from_core_type

from .models import OrchestratorError
from .paths import BOARD_SCHEMA

if TYPE_CHECKING:
    from .models import BoardProject


@functools.lru_cache(maxsize=None)
def _core_os_choices(metadata_root: Path) -> tuple[str, ...]:
    """The runtimes a core's `os:` may resolve to, read from *metadata_root*'s
    board schema's `$defs/core_entry/properties/os` enum.

    Derived (not re-typed) so the value-set has exactly one source of truth
    and cannot drift between the schema and the code.  `off` skips the core
    (no slice is built).  *metadata_root* is REQUIRED and the cache is keyed
    on it -- a fixed in-tree default here silently ignored a project's
    `--metadata-root` override (#1485).

    Falls back to the in-tree `BOARD_SCHEMA` when *metadata_root* has no
    `schemas/` of its own (e.g. a synthetic test root) -- the same fallback
    `loader._validate_board` applies, so a scratch root without a schema copy
    still resolves instead of raising a raw `FileNotFoundError`.
    """
    schema_path = Path(metadata_root) / "schemas" / "board.schema.json"
    if not schema_path.is_file():
        schema_path = BOARD_SCHEMA
    if not schema_path.is_file():
        raise OrchestratorError(f"board schema not found: {schema_path}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return tuple(schema["$defs"]["core_entry"]["properties"]["os"]["enum"])


def _runtime_class(core_type: str) -> str:
    """`linux` for Cortex-A, `rtos` for Cortex-M, else `other`.

    `core_type` is typed `str`, and today's one caller (`core_os_topology`)
    already normalises a non-string `type` to `""` before this is reached
    (`soc_types`'s `isinstance` guard, tan-cli#957/#962) -- so the
    `isinstance` check below is defense-in-depth, not a live bug fix: it is
    currently unreachable, not currently wrong. It is added anyway so this
    shared-shaped helper does not repeat the exact assumption ("my caller
    surely guarded this") that let the bare idiom survive unnoticed at both
    `presets_cmd.core_type_lookup` and `kconfig._emit_inference` (tan-cli#962)
    -- a future caller of this function gets the same backstop those two
    now have, instead of inheriting the crash-or-leak choice again.
    """
    t = core_type.lower() if isinstance(core_type, str) else ""
    if t.startswith("cortex-a"):
        return "linux"
    if t.startswith("cortex-m"):
        return "rtos"
    return "other"


def _allowed_os_for_core(core_type: str, metadata_root: Path) -> list[str]:
    """The os: values valid for this core: every runtime minus the other
    class's OS -- e.g. Cortex-A -> [yocto, baremetal, off], Cortex-M ->
    [zephyr, baremetal, off].

    Delegates to `tan.core.os_class.allowed_os_for_core` (tan-cli#914) rather
    than open-coding the same subtraction `_cross_class_os` performs: an
    unresolved `core_type` (`""` -- `core_os_topology`'s own
    `soc_types.get(core_id, "")`) degrades to `[]` there rather than the
    plausible-but-wrong cross-class subtraction, the SAME degrade
    `presets_cmd.allowed_os_lookup` needs -- see that function's docstring for
    why the two must never drift apart on this again.

    KNOWN DIVERGENCE FROM UPSTREAM (tan-cli#938): alp-sdk's own
    `scripts/alp_orchestrate/topology.py` still open-codes the cross-class
    subtraction inline and returns `["baremetal", "off"]` for an unresolved
    `core_type`, because upstream never carried #914's guard. Do NOT
    "resync" this function toward upstream's `["baremetal", "off"]` -- that
    is the exact plausible-but-wrong guess #870/#914 exist to remove, and
    reintroducing it here would reopen the alp-sdk-vscode#538-shaped defect
    on this emit specifically. This reaches `core_os_topology`'s
    `allowed_os` field, which `test_planner_emit_parity` pins byte-for-byte
    against the oracle; the relocation-freshness gate
    (`tests/gates/test_planner_relocation_freshness.py`) cannot see this
    divergence because it only hashes upstream's `topology.py`, never this
    file -- record here, don't reconcile.
    """
    return _allowed_os_for_core_shared(core_type, _core_os_choices(metadata_root))


def core_os_topology(project: "BoardProject") -> dict[str, Any]:
    """Per-core OS facts for an IDE / tool (issue #95).

    The runtime is *determined by the core class* -- Cortex-A -> Yocto (Linux),
    Cortex-M -> Zephyr (RTOS) -- and is not user-selectable.  For each resolved
    core this reports its `runtime_class`, the class `default_os`, the
    `effective_os` (after a `baremetal`/`off` override -- the only board.yaml
    knobs), whether it is `enabled`, and the per-core `allowed_os` set (the
    valid dropdown).  Lets the Board Configurator show the SDK's selection +
    the legal overrides instead of guessing or offering a cross-class OS.
    """
    # `c.get("type", "")` is UNVALIDATED against `soc-spec-v1.schema.json`'s
    # own `"type": {"type": "string"}` -- the identical gap `presets_cmd.
    # core_type_lookup` closed for tan-cli#957, same class, this call site
    # pre-dating it. `core_type` below feeds `_runtime_class`/
    # `_default_os_from_core_type`'s `(core_type or "").lower()` AND is
    # written verbatim to the emitted `core_type` field, so a non-string
    # here is an `AttributeError` (build/`--emit os-topology` abort) or a
    # wire leak, same two failure modes. Normalises to the same `""`
    # unresolved sentinel a missing `type`/entry already produces.
    soc_types = {
        c["id"]: c["type"] if isinstance(c.get("type"), str) else ""
        for c in (project.soc_spec.get("cores") or []) if "id" in c
    }
    rows: list[dict[str, Any]] = []
    for core_id, sl in sorted(project.cores.items()):
        core_type = soc_types.get(core_id, "")
        default_os = _default_os_from_core_type(core_type)
        rows.append({
            "core_id":       core_id,
            "core_type":     core_type,
            "runtime_class": _runtime_class(core_type),
            "default_os":    default_os,
            "effective_os":  sl.os,
            "enabled":       sl.os != "off",
            "overridden":    sl.os != default_os,
            "allowed_os":    _allowed_os_for_core(
                core_type, project.effective_metadata_root()),
        })
    return {
        "schema_version": 1,
        "sku":            project.sku,
        "cores":          rows,
    }


def emit_os_topology(project: "BoardProject") -> str:
    """JSON for `alp_project.py --emit os-topology` (see core_os_topology).

    Sorted keys + a trailing newline so the output is byte-deterministic.
    """
    return json.dumps(core_os_topology(project), indent=2) + "\n"
