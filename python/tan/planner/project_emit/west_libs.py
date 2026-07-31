# SPDX-License-Identifier: Apache-2.0
"""west.yml fragment emission (`--emit west-libraries`).

RELOCATED (was alp-sdk `scripts/alp_project_emit/west_libs.py`) -- see this
package's `__init__.py` for the move's contract.

Only the west.yml half came across. That file's other half --
`_emit_library_hw_backends` + `_SOC_FAMILY_TOKEN`, the per-library
HW-accelerator Kconfig matcher -- is NOT reached by `--emit west-libraries` at
all (`_slice_alp_conf` calls it, and that is the `zephyr-conf` path), and it
already relocated with the rest of the Kconfig emitter into
`tan/planner/libraries.py`. A second copy here would be a second source of truth
for which accelerator a SKU gets. `_library_alias_table` is imported from there
for the same reason.
"""

from __future__ import annotations

from typing import Any

import yaml

from ..libraries import _library_alias_table
from ..paths import METADATA_ROOT

# ---------------------------------------------------------------------
# west.yml fragment emission (libraries -> Zephyr-module name-allowlist)
# ---------------------------------------------------------------------
#
# Closes the second v0.4 gap docs/board-config.md flagged: customers
# whose board.yaml declares `libraries: [lvgl, mbedtls]` should not
# also have to hand-add those modules to their app's west.yml
# `name-allowlist:`.  The emitter produces a paste-ready fragment
# they import via a self-referencing `import:` block.


# Canonical library name -> Zephyr module name the workspace's west.yml must
# import.  Keyed by the CANONICAL manifest name (metadata/libraries/<name>.yaml)
# because the v2 `libraries:` resolution feeds canonical names here; the
# conservative allowlist stays four upstream Zephyr modules (the vendored /
# header-only libraries deliberately do NOT get a west entry -- `west update`
# would reject a name it can't resolve).  Mirrors zephyr/modules.git; LittleFS
# ships as `fs/littlefs` while the rest match their names 1:1.
_LIBRARY_WEST_MODULES: dict[str, str] = {
    "lvgl":          "lvgl",
    "mbedtls":       "mbedtls",
    "cmsis-dsp":     "cmsis-dsp",
    "littlefs":      "fs/littlefs",
    # The four header-only C++ libraries (etl / fmt / nlohmann_json /
    # doctest) are not Zephyr modules today -- they land in v0.4 via
    # the per-library profile + include-path hook in the loader, not
    # via west.yml.  Listing them here would emit an entry that
    # `west update` rejects.
}


# OTA provider -> Zephyr module name the workspace's west.yml must
# import.  Hawkbit and MCUmgr ship in Zephyr upstream so no entry --
# only out-of-tree clients need a west.yml line.  See ADR 0009.
_OTA_PROVIDER_WEST_MODULES: dict[str, str] = {
    "mender":  "mender-mcu-client",
    # hawkbit -- in Zephyr upstream
    # mcumgr  -- in Zephyr upstream
}


def _load_curated_library_manifest(lib: str) -> dict[str, Any] | None:
    """Load a top-level ADR 0018 library manifest if one exists."""
    path = METADATA_ROOT / "libraries" / f"{lib}.yaml"
    if not path.is_file():
        return None
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return doc if isinstance(doc, dict) else None


def _emit_west_libraries(
    project: dict[str, Any],
    sku_preset: dict[str, Any],
    board_preset: dict[str, Any] | None,
    *,
    v2_libraries: list[str] | None = None,
    v2_project_libraries: list[str] | None = None,
) -> str:
    """Emit a west.yml fragment that the customer's manifest can
    import to pin the Zephyr modules board.yaml's `libraries:` array
    requires.  Idempotent: emitting an empty `libraries:` array gives
    an empty (but well-formed) name-allowlist.

    v1 path (`v2_libraries is None`): reads project-level `libraries:`.
    v2 path: callers compute the union across the Zephyr-runtime cores
    (or pick one when `--core <id>` is supplied) and pass it in via
    `v2_libraries`.  `v2_project_libraries` carries the top-level ADR 0018
    curated library manifests; these may either import a Zephyr-owned module
    by name or emit a standalone west project pin from the manifest's
    `integration.zephyr.west` block.
    """
    del sku_preset, board_preset  # unused -- libraries are SoM-agnostic
    if v2_libraries is not None:
        libs = list(v2_libraries)
    else:
        libs = project.get("libraries") or []
    project_libs = list(v2_project_libraries
                        if v2_project_libraries is not None
                        else [])
    modules: list[tuple[str, str]] = []   # (library, Zephyr-owned west module)
    west_projects: list[tuple[str, dict[str, Any]]] = []
    unsupported: list[str] = []
    seen_modules: set[str] = set()
    seen_projects: set[str] = set()

    def add_module(lib: str, mod: str) -> None:
        if mod not in seen_modules:
            modules.append((lib, mod))
            seen_modules.add(mod)

    def add_west_project(lib: str, west: dict[str, Any]) -> None:
        name = str(west.get("name") or "")
        if name and name not in seen_projects:
            west_projects.append((lib, west))
            seen_projects.add(name)

    # Normalise legacy per-core tokens (schemaVersion 1) to their canonical
    # manifest name so the west-module lookup resolves regardless of which
    # spelling the caller passed (v2 resolution already yields canonical).
    alias = _library_alias_table()
    for lib in libs:
        mod = _LIBRARY_WEST_MODULES.get(alias.get(lib, lib))
        if mod is None:
            unsupported.append(lib)
        else:
            add_module(lib, mod)

    for lib in project_libs:
        manifest = _load_curated_library_manifest(lib)
        zephyr = ((manifest or {}).get("integration") or {}).get("zephyr") or {}
        if not zephyr:
            continue
        west = zephyr.get("west")
        if isinstance(west, dict):
            add_west_project(lib, west)
            continue
        mod = zephyr.get("module")
        if isinstance(mod, str) and mod:
            add_module(lib, mod)
        else:
            unsupported.append(lib)

    # OTA provider-driven dispatch (ADR 0009 follow-up): out-of-tree
    # Zephyr OTA clients need their own west.yml entry.  Mender-MCU-client
    # is the only one today; hawkbit and mcumgr ship in Zephyr upstream.
    ota = project.get("ota") or {}
    if isinstance(ota, dict):
        ota_provider = (ota.get("provider") or "").lower()
        ota_mod = _OTA_PROVIDER_WEST_MODULES.get(ota_provider)
        if ota_mod is not None:
            modules.append((f"ota:{ota_provider}", ota_mod))

    lines: list[str] = []
    lines.append("# SPDX-License-Identifier: Apache-2.0")
    lines.append("#")
    lines.append("# Auto-generated by scripts/alp_project.py -- "
                 "do not edit by hand.")
    lines.append("# Regenerate after changes to board.yaml's `libraries:` array.")
    lines.append("#")
    lines.append("# Import into your application's west.yml so `west update`")
    lines.append("# pulls only the Zephyr modules the libraries you enabled")
    lines.append("# actually need.  Drop alongside your west.yml and reference")
    lines.append("# from the `import:` field of the alp-sdk project entry.")
    lines.append("")
    lines.append("manifest:")
    lines.append("  projects:")
    lines.append("    - name: zephyr")
    lines.append("      import:")
    lines.append("        name-allowlist:")
    if modules:
        for lib, mod in modules:
            lines.append(f"          - {mod}        # board.yaml libraries: '{lib}'")
    else:
        lines.append("          # no selected Zephyr-owned modules -- nothing to allowlist.")
        lines.append("          []")

    if west_projects:
        lines.append("")
        lines.append("    # ADR 0018 libraries not imported by Zephyr's own west.yml.")
        for lib, west in west_projects:
            lines.append(f"    - name: {west['name']}")
            lines.append(f"      url: {west['url']}")
            lines.append(f"      revision: {west['revision']}")
            lines.append(f"      path: {west['path']}        # board.yaml libraries: '{lib}'")

    if unsupported:
        lines.append("")
        lines.append("# The following libraries have no Zephyr west project entry today")
        lines.append("# (header-only/profile libraries ride the loader's include path;")
        lines.append("# Yocto-only or in-tree Zephyr subsystems do not need a project pin):")
        for lib in unsupported:
            lines.append(f"#   - {lib}")
    return "\n".join(lines) + "\n"
