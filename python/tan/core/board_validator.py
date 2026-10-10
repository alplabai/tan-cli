# SPDX-License-Identifier: Apache-2.0
"""board.yaml validator with rich diagnostics, in-process.

PORTED from alp-sdk `scripts/alp_cli/validator.py` (tan-cli#270). Runs three
passes -- schema (ALP-B001..B004, in `board_validator_schema`), xref
(ALP-B005..B009, here) and compat (ALP-B010, in `board_validator_compat`).

The only deliberate departures from the SDK file, none behavioural:

* No module-scope root. The SDK file derived `REPO`/`METADATA` from
  `__file__`; inside tan that would be tan's own tree. `metadata_root` and
  `PlannerFacts.repo_root` are required parameters, rooted at the BOUND SDK
  checkout (ADR-0017).
* The three things the SDK file lazily imported from `alp_orchestrate`
  (`_BLOCK_SLUGS`, `plan_cameras`, `resolve_cores`) arrive in a `PlannerFacts`
  instead, so this module loads without `tan.planner` -- which needs a bound
  SDK root at import. `board_validator_run` builds it from `tan.planner`;
  `PlannerFacts()` with no callables degrades exactly as the SDK file did when
  that import failed (chip manifests only, no camera-owner checks).
* `fast_safe_load` is local: `tan.planner.strict_loaders` cannot be imported
  before an SDK root is bound.
"""

from __future__ import annotations

from difflib import get_close_matches
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tan.core.board_diagnostic import Diagnostic, DiagnosticCollector
from tan.core.board_validator_compat import _compat_pass
from tan.core.board_validator_schema import _schema_pass
from tan.core.board_yaml_pos import load_with_positions, node_position

_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


@dataclass(frozen=True)
class PlannerFacts:
    """The SDK-tree facts and planner leaves the validator consults."""

    repo_root: Path
    block_slugs: frozenset[str] = frozenset()
    plan_cameras: Callable[..., Any] | None = None
    resolve_cores: Callable[..., Any] | None = None


def fast_safe_load(text: str) -> Any:
    """Exactly `yaml.safe_load(text)`, parsed by libyaml when available."""
    return yaml.load(text, Loader=_YAML_LOADER)


def validate_board_yaml(
    path: Path, *, metadata_root: Path, facts: PlannerFacts
) -> DiagnosticCollector:
    """Validate a board.yaml file. Returns a DiagnosticCollector.

    *metadata_root* is the SoM / board-preset / SoC / schema search root:
    the bound SDK checkout's ``metadata/`` (or a customer's out-of-tree copy
    of it, which validates identically everywhere).
    """
    collector = DiagnosticCollector()
    text = path.read_text(encoding="utf-8")
    try:
        data = load_with_positions(text, source=path)
    except Exception as exc:  # YAML parse error
        collector.add(
            Diagnostic(
                severity="error",
                path=path,
                line=1,
                col=1,
                span=1,
                code="ALP-B000",
                message=f"YAML parse error: {exc}",
                hint=None,
            )
        )
        return collector

    root = metadata_root
    som_dir = root / "e1m_modules"
    preset_dir = root / "boards"
    soc_dir = root / "socs"
    chip_dir = root / "chips"
    camera_module_dir = root / "camera_modules"
    schema_path = root / "schemas" / "board.schema.json"

    _schema_pass(data, path, collector, schema_path=schema_path)
    _xref_pass(data, path, collector, som_dir=som_dir, preset_dir=preset_dir,
               chip_dir=chip_dir, camera_module_dir=camera_module_dir,
               facts=facts)
    _compat_pass(data, path, collector, som_dir=som_dir, soc_dir=soc_dir)
    return collector


def _xref_pass(
    data: dict[str, Any],
    path: Path,
    collector: DiagnosticCollector,
    *,
    som_dir: Path,
    preset_dir: Path,
    chip_dir: Path,
    camera_module_dir: Path,
    facts: PlannerFacts,
) -> None:
    som = data.get("som")
    # #602: a schema-invalid `som:` (wrong type, e.g. a string/list instead
    # of a mapping) is already reported by _schema_pass as ALP-B004 -- don't
    # let this semantic pass crash on it, just skip SoM/preset xref work.
    som = som if isinstance(som, dict) else {}
    sku = som.get("sku")
    som_doc: dict[str, Any] | None = None
    if isinstance(sku, str):
        sku_path = _sku_path(sku, som_dir=som_dir)
        if sku_path is None:
            line, col = node_position(som, "sku", target="value")
            collector.add(
                Diagnostic(
                    severity="error",
                    path=path,
                    line=line,
                    col=col,
                    span=len(sku),
                    code="ALP-B005",
                    message=f"SoM SKU '{sku}' does not resolve to a known module",
                    hint=_sku_suggestion(sku, som_dir=som_dir),
                )
            )
        else:
            som_doc = _load_metadata_yaml(sku_path)

    preset = data.get("preset")
    board_doc: dict[str, Any] | None = None
    if isinstance(preset, str):
        preset_path = preset_dir / f"{preset}.yaml"
        if not preset_path.is_file():
            line, col = node_position(data, "preset", target="value")
            collector.add(
                Diagnostic(
                    severity="error",
                    path=path,
                    line=line,
                    col=col,
                    span=len(preset),
                    code="ALP-B006",
                    message=f"board preset '{preset}' does not exist",
                    hint=_preset_suggestion(preset, preset_dir=preset_dir),
                )
            )
        else:
            board_doc = _load_metadata_yaml(preset_path)

    if isinstance(preset, str) and isinstance(sku, str) and som_doc and board_doc:
        _check_board_hosts_som_family(
            data, path, collector, sku, som_doc, preset, board_doc,
            preset_dir=preset_dir)

    _check_chips_known(data, path, collector, chip_dir=chip_dir,
                       block_slugs=facts.block_slugs)
    # With a `preset:` the connectors come from that preset; if the preset is
    # missing (ALP-B006 already reported) there is nothing to check against.
    if not isinstance(preset, str):
        _check_cameras(data, path, collector, data.get("camera_connectors"),
                       camera_module_dir=camera_module_dir, inline=True,
                       som_doc=som_doc, facts=facts)
    elif board_doc is not None:
        _check_cameras(data, path, collector, board_doc.get("camera_connectors"),
                       camera_module_dir=camera_module_dir, som_doc=som_doc,
                       facts=facts)


def camera_connector_problems(connectors: Any, e1m_routes: Any, *,
                              shield_dir: Path,
                              families: Any = None) -> list[str]:
    """Cross-checks of `camera_connectors:` that the schema cannot express,
    shared by this validator (inline project boards) and
    scripts/validate_metadata.py (board presets):

    * every macro a connector names (`i2c`, `enable`, `reset`, `select[].gpio`)
      must be declared in the same board's `e1m_routes:` (`buses` for `i2c`,
      `gpio` for the rest) -- connectors reference routes, never restate pads;
    * `lane_polarity` carries one flag per clock + data lane: `lanes + 1`;
    * every `zephyr_shields` entry is a directory under zephyr/boards/shields/;
    * `linux: true` only on CAM0 of a board for a `renesas-rzv2n*` SoM family
      (`families`: the preset's `hosts_som_families`, or the project SoM's
      family for an inline board; None skips the family half) -- the only
      place a Linux camera path exists.
      Follow-up: tighten to "scripts/gen_camera_dt.py (#2736) emits a
      <board>-cam<n>-<module>.dtsi for this connector"; that needs the board
      name, which an inline project board does not have.
    """
    if not isinstance(connectors, dict):
        return []
    routes = e1m_routes if isinstance(e1m_routes, dict) else {}

    def macros(section: str) -> set[Any]:
        return {e.get("macro") for e in (routes.get(section) or [])
                if isinstance(e, dict)}

    gpio, buses = macros("gpio"), macros("buses")
    msgs: list[str] = []
    for name, c in connectors.items():
        if not isinstance(c, dict):
            continue
        refs = [("i2c", c.get("i2c"), buses, "buses"),
                ("enable", c.get("enable"), gpio, "gpio"),
                ("reset", c.get("reset"), gpio, "gpio")]
        refs += [("select.gpio", sel.get("gpio"), gpio, "gpio")
                 for sel in (c.get("select") or []) if isinstance(sel, dict)]
        for field, macro, known, section in refs:
            if macro is not None and macro not in known:
                msgs.append(f"camera_connectors.{name}.{field}: `{macro}` is not a "
                            f"macro in e1m_routes.{section}")
        if c.get("linux"):
            if name != "CAM0":
                msgs.append(f"camera_connectors.{name}.linux: only CAM0 has a Linux "
                            f"camera path (the sensor DT is generated for CAM0)")
            if families is not None and not any(
                    str(f).startswith("renesas-rzv2n") for f in families):
                msgs.append(f"camera_connectors.{name}.linux: no Linux camera path "
                            f"for SoM family {', '.join(map(str, families)) or 'none'} "
                            f"(only renesas-rzv2n*)")
        for sh in c.get("zephyr_shields") or []:
            if not (shield_dir / str(sh)).is_dir():
                msgs.append(f"camera_connectors.{name}.zephyr_shields: `{sh}` is not "
                            f"a shield under zephyr/boards/shields/")
        pol, lanes = c.get("lane_polarity"), c.get("lanes")
        if isinstance(pol, list) and isinstance(lanes, int) and len(pol) != lanes + 1:
            msgs.append(f"camera_connectors.{name}.lane_polarity: {len(pol)} entries "
                        f"for {lanes} data lane(s); expected {lanes + 1} "
                        f"(clock lane + data lanes)")
    return msgs


def _check_cameras(
    data: dict[str, Any],
    path: Path,
    collector: DiagnosticCollector,
    connectors: Any,
    *,
    camera_module_dir: Path,
    facts: PlannerFacts,
    inline: bool = False,
    som_doc: Any = None,
) -> None:
    """ALP-B003 for camera declarations the schema cannot judge:

    * a `cameras:` entry naming a connector the resolved board does not
      expose, a module with no `metadata/camera_modules/<module>.yaml`, or a
      connector listed twice (both valid identifiers to the schema, so a typo
      would only surface when a generator looks the name up);
    * a module with no `zephyr_shield:` while a Zephyr core is in use (it
      could never be selected: the Zephyr build takes `-DSHIELD` from it);
    * for an INLINE board, a `camera_connectors:` block whose macros do not
      resolve in the project's own `e1m_routes:` or whose `lane_polarity` has
      the wrong length (presets get the same check from validate_metadata.py).

    Anchored on the `cameras` (or `camera_connectors`) key because the
    position loader does not track individual list items.
    """
    def report(key: str, message: str) -> None:
        line, col = node_position(data, key, target="key")
        collector.add(
            Diagnostic(severity="error", path=path, line=line, col=col,
                       span=len(key), code="ALP-B003", message=message))

    if inline:
        fam = som_doc.get("family") if isinstance(som_doc, dict) else None
        for message in camera_connector_problems(
                connectors, data.get("e1m_routes"),
                shield_dir=facts.repo_root / "zephyr" / "boards" / "shields",
                families=[fam] if fam else None):
            report("camera_connectors", message)

    cameras = data.get("cameras")
    if not isinstance(cameras, list):
        return
    known_connectors = sorted(connectors) if isinstance(connectors, dict) else []
    seen: set[str] = set()
    for entry in cameras:
        if not isinstance(entry, dict):
            continue  # schema pass reports the wrong type
        connector = entry.get("connector")
        module = entry.get("module")
        if isinstance(connector, str):
            if connector not in known_connectors:
                report("cameras",
                       f"cameras: connector '{connector}' is not a camera connector of "
                       f"this board (known: {', '.join(known_connectors) or 'none'})")
            if connector in seen:
                report("cameras",
                       f"cameras: connector '{connector}' is listed more than once "
                       f"(one module per connector)")
            seen.add(connector)
        if isinstance(module, str) and not (camera_module_dir / f"{module}.yaml").is_file():
            report("cameras",
                   f"cameras: unknown camera module '{module}' "
                   f"(no metadata/camera_modules/{module}.yaml)")
    _check_camera_owners(data, cameras, connectors, som_doc, report,
                         camera_module_dir=camera_module_dir, facts=facts)


def _check_camera_owners(data, cameras, connectors, som_doc, report, *,
                         camera_module_dir: Path, facts: PlannerFacts) -> None:
    """Ownership + static build checks via the shared leaf
    `alp_orchestrate/camera_owner.py` (the same rule the planner applies):
    an explicit/implied owner core, a module `zephyr_shield`, connector
    `zephyr_shields`, and a shield overlay for the owner's board target.

    The two leaves come in through *facts* (see the module docstring); absent
    ones skip the check, as the SDK file's failed lazy import did."""
    if not isinstance(som_doc, dict):
        return
    plan_cameras, resolve_cores = facts.plan_cameras, facts.resolve_cores
    if plan_cameras is None or resolve_cores is None:
        return
    modules = {}
    for entry in cameras:
        mod = entry.get("module") if isinstance(entry, dict) else None
        if isinstance(mod, str) and (camera_module_dir / f"{mod}.yaml").is_file():
            modules[mod] = _load_metadata_yaml(camera_module_dir / f"{mod}.yaml") or {}
    cores = resolve_cores(data.get("cores"), som_doc.get("topology"))
    for plan in plan_cameras(cameras, connectors, cores, modules, facts.repo_root):
        for message in plan.errors:
            report("cameras", message)


def _known_chip_slugs(*, chip_dir: Path, block_slugs: frozenset[str]) -> set[str]:
    """Every valid `chips:` token: real chip manifests plus the small set
    of non-chip SDK 'block' helpers `chips:` may also legitimately name
    (`button_led`, `pdm_mic`) -- see `_BLOCK_SLUGS` in
    `alp_orchestrate/slugs.py`, the dependency-free leaf that is the single
    source of truth for that distinction (kconfig.py's emitter and
    check_example_portability.py both consult the same set; e.g.
    `examples/connectivity/iot-connected-camera/board.yaml`'s
    `chips: [ssd1306, button_led]`).

    A manifest with `driver_status: planned` OR `driver_status: none`
    (issue #1224 review; #1270 widened this from `planned`-only after
    `metadata/chips/dp83825.yaml` shipped the tree's first `none`) ships no
    `chips/<id>/` driver and declares no `ALP_SDK_CHIP_<NAME>` Kconfig
    symbol -- naming either in `chips:` would emit the same
    undeclared-symbol Kconfig line ALP-B008 exists to catch, so both
    statuses are excluded from "known" the same way an unknown chip is.
    `partial`/`complete`/`stub` all ship a real `chips/<id>/` driver (even
    a stub has a `.c` file and a declared Kconfig symbol -- see
    `chips/ar0234/`) so only these two "no driver at all" tiers apply.

    *block_slugs* is that `_BLOCK_SLUGS` set, passed in (see the module
    docstring); empty means "chip manifests only", which is also what the
    SDK file did when it could not import `alp_orchestrate`.
    """
    known = {
        p.stem for p in chip_dir.glob("*.yaml")
        if (_load_metadata_yaml(p) or {}).get("driver_status") not in (
            "planned", "none")
    }
    return known | block_slugs


def _check_chips_known(
    data: dict[str, Any],
    path: Path,
    collector: DiagnosticCollector,
    *,
    chip_dir: Path,
    block_slugs: frozenset[str],
) -> None:
    """Reject a `chips:` entry that names no real chip manifest or SDK
    block helper (issue #1224).

    `chips:` is schema-validated only against a permissive identifier
    regex (`metadata/schemas/board.schema.json`), so a typo (e.g.
    `no_such_chip_xyz`) passes schema validation clean and is emitted
    verbatim into the generated `alp.conf` as
    `CONFIG_ALP_SDK_CHIP_<TYPO>=y` -- a Kconfig symbol nothing declares,
    so the driver is silently not built and the app fails at runtime
    instead of at validation time.  Cross-check every token against the
    real chip registry (`metadata/chips/*.yaml`, `chip_id:` == filename
    stem for every manifest) the same way `som.sku` (ALP-B005) and
    `preset:` (ALP-B006) are already cross-checked.
    """
    chips = data.get("chips")
    if not isinstance(chips, list):
        return

    known = _known_chip_slugs(chip_dir=chip_dir, block_slugs=block_slugs)
    # Point at the `chips` KEY, not the list's VALUE node: for a
    # multi-entry list target="value" gives every offending entry the
    # position of the FIRST list item, so a caret sized to a LATER
    # unknown entry (`span=len(chip)`) would underline an unrelated,
    # valid entry instead (review round 3).  The YAML position loader
    # (alp_cli/yaml_pos.py) doesn't track individual list-item
    # positions, so the precise fix -- a caret on the offending token
    # itself -- needs a loader change; anchoring on the unambiguous
    # `chips` key is the correct diagnostic today.
    line, col = node_position(data, "chips", target="key")
    seen: set[str] = set()
    for chip in chips:
        # Non-string / pattern-invalid entries are already reported by
        # the schema pass (ALP-B003/ALP-B004); nothing new to say here.
        if not isinstance(chip, str) or chip in known or chip in seen:
            continue
        seen.add(chip)
        # A manifest with `driver_status: planned` or `driver_status: none`
        # IS a real, committed metadata/chips/<chip>.yaml -- `_known_chip_slugs`
        # excludes it because it declares no ALP_SDK_CHIP_<NAME> Kconfig
        # symbol, not because the manifest is missing.  Say that, not "no
        # metadata/chips/<chip>.yaml" (false -- the file is right there)
        # and don't offer a "did you mean" against an unrelated chip for
        # an entry that was spelled correctly (review round 3 major).
        chip_path = chip_dir / f"{chip}.yaml"
        if chip_path.is_file():
            status = (_load_metadata_yaml(chip_path) or {}).get("driver_status")
            message = (
                f"chips: '{chip}' has driver_status: {status} "
                f"-- no Alp SDK driver or ALP_SDK_CHIP_{chip.upper()} "
                f"symbol yet"
            )
            hint = None
        else:
            message = (
                f"chips: unknown chip '{chip}' "
                f"(no metadata/chips/{chip}.yaml)"
            )
            hint = _chip_suggestion(chip, chip_dir=chip_dir, block_slugs=block_slugs)
        collector.add(
            Diagnostic(
                severity="error",
                path=path,
                line=line,
                col=col,
                span=len("chips"),
                code="ALP-B008",
                message=message,
                hint=hint,
            )
        )


def _chip_suggestion(
    chip: str, *, chip_dir: Path, block_slugs: frozenset[str]
) -> str | None:
    known = sorted(_known_chip_slugs(chip_dir=chip_dir, block_slugs=block_slugs))
    match = get_close_matches(chip, known, n=1)
    return f"did you mean '{match[0]}'?" if match else None


def _load_metadata_yaml(path: Path) -> dict[str, Any] | None:
    try:
        # libyaml-backed yaml.safe_load: the hottest metadata read in the
        # validator (#2328).
        doc = fast_safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    return doc if isinstance(doc, dict) else None


def _sku_path(sku: str, *, som_dir: Path) -> Path | None:
    for candidate in som_dir.rglob(f"{sku}.yaml"):
        return candidate
    return None


def _check_board_hosts_som_family(
    data: dict[str, Any],
    path: Path,
    collector: DiagnosticCollector,
    sku: str,
    som_doc: dict[str, Any],
    preset: str,
    board_doc: dict[str, Any],
    *,
    preset_dir: Path,
) -> None:
    family = som_doc.get("family")
    allowed = board_doc.get("hosts_som_families") or []
    if not isinstance(family, str) or not isinstance(allowed, list):
        return
    allowed = [str(item) for item in allowed]
    if family in allowed:
        return

    line, col = node_position(data, "preset", target="value")
    compatible = _compatible_presets(family, preset_dir=preset_dir)
    hint = (
        f"use a board preset whose hosts_som_families includes '{family}'"
    )
    if compatible:
        hint += f" (for example: {', '.join(compatible)})"
    hint += ", or define a compatible board inline"
    collector.add(
        Diagnostic(
            severity="error",
            path=path,
            line=line,
            col=col,
            span=len(preset),
            code="ALP-B007",
            message=(
                f"board preset '{preset}' hosts SoM families {allowed}, "
                f"but {sku} is family '{family}'"
            ),
            hint=hint,
        )
    )


def _compatible_presets(family: str, *, preset_dir: Path) -> list[str]:
    out: list[str] = []
    for board_path in sorted(preset_dir.glob("*.yaml")):
        doc = _load_metadata_yaml(board_path) or {}
        families = doc.get("hosts_som_families") or []
        if isinstance(families, list) and family in [str(item) for item in families]:
            out.append(board_path.stem)
    return out


def _all_skus(*, som_dir: Path) -> list[str]:
    return sorted(p.stem for p in som_dir.rglob("*.yaml") if p.stem.startswith("E1M-"))


def _all_presets(*, preset_dir: Path) -> list[str]:
    return sorted(p.stem for p in preset_dir.glob("*.yaml"))


def _sku_suggestion(sku: str, *, som_dir: Path) -> str | None:
    match = get_close_matches(sku, _all_skus(som_dir=som_dir), n=1)
    return f"did you mean '{match[0]}'?" if match else None


def _preset_suggestion(preset: str, *, preset_dir: Path) -> str | None:
    match = get_close_matches(preset, _all_presets(preset_dir=preset_dir), n=1)
    return f"did you mean '{match[0]}'?" if match else None


