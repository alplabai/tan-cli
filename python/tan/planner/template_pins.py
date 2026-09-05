# SPDX-License-Identifier: Apache-2.0
"""SoM/board-metadata reads for `template.py`'s `--emit scaffold` sku swap.

SPLIT out of `tan/planner/template.py` (tan-cli#1142; that module was 2206
lines against `MODULE_CAP = 800`). This is NOT a second hand-port: both this
module and `template_rewrite.py` are the SAME `HAND_PORT_SOURCES` entry
(`scripts/alp_template.py`) as `template.py` itself -- see
`test_planner_relocation_freshness.py::HAND_PORT_SOURCES` and
`test_hand_port_tan_side.py::HAND_PORT_TAN_SIDE`, both of which map all three
tan files to the one upstream source, the same many-to-one shape
`alp_project_loader.py` already has for `project_loader.py` + `som_metadata.py`.

`_module_size_budget_core.MIRRORED_PREFIX` ("upstream's to split, not this
repo's") does NOT bar this split: that comment describes `PINNED_HASHES`
modules, which are 3-way MERGED against a moving upstream file and would
conflict on every future upstream hunk if they diverged in shape here.
`template.py` is `HAND_PORT_SOURCES`-tracked instead -- FLAGGED, never merged
(`test_planner_relocation_freshness.py`'s own module docstring) -- so there is
no base/theirs/ours triple for a shape change to break. See
`_module_size_budget_core.py`'s corrected `MIRRORED_PREFIX` comment and
`test_module_size_budget.py::test_the_mirrored_planner_is_named_as_out_of_scope`,
which now checks only the `PINNED_HASHES` subset.

WHAT'S HERE: every read of `metadata/e1m_modules/<sku>.yaml` /
`metadata/boards/<board>.yaml` and the sku/core/pin RE-DERIVATION this module's
functions compute from them -- `_load_som_doc` through `_derive_pin_doc_renames`,
plus `_core_board` (moved here despite sitting elsewhere in the original file,
alongside the other `_topology_for_sku` reader rather than beside the
`board.yaml` TEXT rewriters in `template_rewrite.py`, which is where it lived
physically before the split). Every function here reads metadata and returns a
plain value or a rename map; none of them touch `board.yaml`/CMakeLists.txt/
README.md TEXT -- that half is `template_rewrite.py`.

Every docstring below is UNCHANGED from `template.py` -- issue numbers, tan-cli
numbers and worked examples all still refer to that module's own history,
because this split moved code, not its provenance.

Imports `TemplateError` / `_require_field` / `_read_yaml_mapping` back FROM
`.template` rather than the reverse: `template.py` still owns the
`DocumentGuards(TemplateError)` binding (`_GUARDS` and friends), since
`_cmake_core_map`/`_rendered_bytes`/`render_to_envelope` stayed there and need
it too, and `cli.py`'s `from .template import (TemplateError, emit_scaffold,
find_template_by_cores, load_catalog)` must keep resolving unchanged. That
makes the import direction CIRCULAR at the module-object level (`template.py`
imports names from this module for its own re-exports, and this module imports
`TemplateError`/`_require_field`/`_read_yaml_mapping` back from `template.py`)
-- which works only because `template.py` binds all three before its own
`from .template_pins import ...` line runs; see that file's own comment at the
same spot. Do not move this module's import of `.template` above where
`template.py` defines those three names, and do not move `template.py`'s
import of this module above its own `_GUARDS` block.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .template import TemplateError, _read_yaml_mapping, _require_field


def _load_som_doc(sku: str, metadata_root: Path) -> dict[str, Any]:
    """Parse metadata/e1m_modules/<sku>.yaml -- shared by
    `_default_preset_for_sku` (the `default_board:` field),
    `_derive_core_renames` (the `topology:` block), and `_core_board`
    (also `topology:`), so all three read the exact same doc for the
    same `(sku, metadata_root)`."""
    som_path = metadata_root / "e1m_modules" / f"{sku}.yaml"
    # Mirrors `resolve_targets`'s `preset` guard (tan-cli#1010,
    # `tan/model/targets.py:312-323`): a SoM YAML that parses but is not
    # a mapping (e.g. a bare list or a bare scalar) must not reach a
    # caller's bare `.get(...)` -- every caller of this function does
    # exactly that -- which raises a raw AttributeError instead of a
    # curated error a caller can distinguish from any other such error
    # in the call stack (tan-cli#1025). Shared with the module's two
    # other outer-document reads since tan-cli#1052; the message is
    # unchanged.
    #
    # tan-cli#1133: the READ and the PARSE are guarded too, and the
    # `is_file()` pre-flight this used to open with is gone -- see
    # `_read_yaml_mapping`. The missing-file message is byte-identical to
    # the one that pre-flight raised (pinned by `tests/planner/
    # test_render_to_envelope_malformed_example_board.py`).
    return _read_yaml_mapping(
        som_path, what="SoM preset",
        absent=f"no metadata/e1m_modules/{sku}.yaml for sku {sku!r}")


def _default_preset_for_sku(sku: str, metadata_root: Path) -> str:
    """The board preset a fresh project targeting `sku` ships with by
    default -- metadata/e1m_modules/<sku>.yaml's `default_board:`,
    lower-cased to match the `preset:` value every example board.yaml
    already uses (e.g. `E1M-EVK` -> `e1m-evk`, `E1M-X-EVK` ->
    `e1m-x-evk`). This is the SAME field board.yaml's own comments point
    customers at by hand ("copy this directory, change som.sku ...,
    edit the preset:") -- render_to_envelope just does that edit for
    them."""
    board = _load_som_doc(sku, metadata_root).get("default_board")
    if not board:
        raise TemplateError(
            f"metadata/e1m_modules/{sku}.yaml has no default_board")
    # Same document-class guard as `_load_som_doc` itself (tan-cli#1025
    # sweep): a `default_board:` that parses but isn't a scalar string
    # (e.g. a YAML list) must not reach `.lower()` bare -- that raises
    # `AttributeError: 'list' object has no attribute 'lower'` instead
    # of a curated error naming the file.
    _require_field(board, str, doc=f"metadata/e1m_modules/{sku}.yaml",
                   field="default_board")
    return board.lower()


def _topology_for_sku(sku: str, metadata_root: Path) -> dict[str, Any]:
    """`metadata/e1m_modules/<sku>.yaml` `topology:` -- shared by
    `_derive_core_renames` and `_core_board`, both of which key into the
    result by core id (`.items()`/`.get(core_id)`) and then read a
    `board:`/`app:` field off each core's own entry (`spec.get(...)`).

    Same document-class guard as `_load_som_doc` itself (tan-cli#1025
    sweep, round 2 -- the first round guarded the outer document but
    left every nested read this function serves unguarded): a
    `topology:` block that parses but is not itself a mapping (e.g. a
    bare list) must not reach either caller's bare `.items()`/`.get()`
    -- that raises a raw AttributeError instead of a curated error
    naming the file and the actual type. Guarding the OUTER shape here,
    once, also closes a quieter failure mode a plain per-caller
    AttributeError guard would not: `_derive_core_renames`'s own `cid
    not in topology` membership test does not raise against a list
    `topology` at all (a list supports `in`), so an unguarded list would
    silently resolve `stale == []` and return `None` -- a byte-
    identical-passthrough verdict that is not true, instead of
    surfacing the malformed document.

    Each core's own entry is validated too, for the same reason one
    level down: `_derive_core_renames`'s `spec.get("board")` and
    `_core_board`'s `topology.get(core_id).get("board")` both assume
    the per-core value is itself a mapping. A `topology:` that IS a
    mapping but whose value for some core id is a bare list/scalar
    (legal YAML, illegal `som-preset` schema) would otherwise reach
    those bare `.get()` calls unguarded -- validating every entry here,
    once, closes that for both callers the same way the outer check
    does."""
    som_yaml = f"metadata/e1m_modules/{sku}.yaml"
    topology = _require_field(
        _load_som_doc(sku, metadata_root).get("topology") or {}, dict,
        doc=som_yaml, field="topology")
    for core_id, spec in topology.items():
        _require_field(spec, dict, doc=som_yaml, field=f"topology.{core_id}")
    return topology


def _derive_core_renames(
    original_core_ids: list[str], sku: str, metadata_root: Path,
) -> dict[str, str] | None:
    """Re-derive every STALE core id a catalog template's `cores:`
    block declares, for `sku`'s OWN SoM topology (issue #864 follow-up:
    the shallow "byte-copy the example + swap som.sku" `render_to_
    envelope()` #864 shipped hard-coded the CANONICAL example's own
    core id -- e.g. `m55_hp`, an Alif-only Zephyr cluster -- into every
    substituted board.yaml/CMakeLists.txt, emitting a non-buildable
    scaffold for any cross-SoM-family sku: `alp_project.py --emit
    zephyr-conf --core m55_hp` against an E1M-V2N101 board.yaml fails
    with rc=1, "unknown core id ... did you mean ['a55_cluster',
    'm33_sm']").

    EVERY key `cores:` declares must exist in the target sku's own
    topology -- `alp_orchestrate.loader._validate_topology_cores` hard-
    errors on an unmatched key unconditionally, whether or not that
    core is `os: off` -- so a template that also declares the OTHER
    cluster explicitly disabled (edge-ai's `cores.a32_cluster: {os:
    off}`, alongside the active `m55_hp`) needs THAT id renamed too,
    not just the one core the app actually runs on.

    Returns `None` when every declared id already exists in `sku`'s
    `metadata/e1m_modules/<sku>.yaml` `topology:` -- the canonical
    example's own SoM, or a same-family sibling that shares its core
    ids -- a byte-identical passthrough, nothing to rewrite. Otherwise
    returns `{old_core_id: new_core_id, ...}` for every stale id: each
    replacement is `sku`'s own topology core sharing the same leading
    core-class letter (`m`/`a`), additionally requiring a Zephyr
    `board:` target for an `m`-class replacement (only that core is
    ever `--core`-buildable, which is why CMakeLists.txt needs it too
    -- see `_substitute_cmake_core`); an `a`-class utility core carries
    no such requirement (it's only ever `os: off` in every template
    that declares one today).

    Candidates are picked in `topology:`'s OWN declaration order, NOT
    alphabetically (issue #864 Fable-review MAJOR D): a multi-m-core
    SoM's PRIMARY app core is whichever one the SoM preset author
    listed first, not whichever sorts first -- E1M-AEN801's `topology:`
    declares `m55_hp` (the real app core) before `m55_he` (a stock-shim
    peer core), but `m55_he` sorts first alphabetically.
    `alp_project_emit.hw_info._pick_primary_core_os` picks its "primary
    core" alphabetically -- that convention is NOT reused here for
    exactly this reason; it would silently rename onto the wrong core
    the day any multi-m-core SKU joins a template's supported set
    (verified: `_derive_core_renames(["m33_sm"], "E1M-AEN801", ...)`
    resolved `m55_he`, not the real app core `m55_hp`, before this
    fix -- unreachable today, since no template's `supported.som_skus`
    combo exercises it, but latently wrong).
    """
    topology = _topology_for_sku(sku, metadata_root)
    stale = [cid for cid in original_core_ids if cid not in topology]
    if not stale:
        return None
    claimed = set(original_core_ids) & set(topology)
    renames: dict[str, str] = {}
    for old in stale:
        prefix = old[0]
        require_board = prefix == "m"
        candidates = [
            cid for cid, spec in topology.items()
            if cid.startswith(prefix) and cid not in claimed
            and cid not in renames.values()
            and (spec.get("board") if require_board else True)
        ]
        if not candidates:
            raise TemplateError(
                f"metadata/e1m_modules/{sku}.yaml topology has no "
                f"{prefix!r}-class core"
                + (" with a Zephyr `board:` target" if require_board else "")
                + f" to replace {old!r}")
        renames[old] = candidates[0]
    return renames


_ROUTE_SECTIONS = ("gpio", "buses", "pwm", "adc", "dac", "i2s", "can", "qenc")


def _board_route_entries(board_name: str, metadata_root: Path) -> list[dict[str, Any]]:
    """Every metadata/boards/<board_name>.yaml `e1m_routes:` entry,
    flattened across sections -- mirrors
    scripts/gen_portability_matrix.py's `_route_entries` (same section
    list, same join; mirrored here rather than imported across module
    boundaries for a handful of lines).

    Same document-class guard as `_load_som_doc`/`_topology_for_sku`
    (tan-cli#1037, its own round-two amendment, plus a same-shape
    third level a PR #1048 review found still bare): a `metadata/
    boards/<board_name>.yaml` that parses but is not itself a mapping
    (e.g. a bare list) must not reach the outer `.get("e1m_routes")`
    bare; an `e1m_routes:` that parses but is not itself a mapping
    must not reach the per-section `.get(section)` one line down; and
    a per-section value (`e1m_routes.<section>`) that parses but is
    not itself a list must not reach the `for entry in ...` below --
    all three raised a raw `AttributeError`/`TypeError` instead of a
    curated error naming the file, the section, and the actual type
    (tan-cli#1037's Measured, PR #1034's round-two review, and PR
    #1048's review, respectively). The third level mirrors
    `_topology_for_sku`'s own outer-plus-per-entry shape: guarding
    every entry, not just the container, closes the same class one
    level down that guarding only `e1m_routes:` itself would leave
    open."""
    board_path = metadata_root / "boards" / f"{board_name}.yaml"
    # tan-cli#1133, same change as `_load_som_doc`'s: no `is_file()`
    # pre-flight, and the read and the parse are curated rather than bare.
    doc = _read_yaml_mapping(
        board_path, what="board metadata",
        absent=f"no metadata/boards/{board_name}.yaml for board {board_name!r}")
    routes = _require_field(doc.get("e1m_routes") or {}, dict,
                            doc=board_path, field="e1m_routes")
    for section in _ROUTE_SECTIONS:
        _require_field(routes.get(section) or [], list,
                       doc=board_path, field=f"e1m_routes.{section}")
    return [
        entry for section in _ROUTE_SECTIONS
        for entry in (routes.get(section) or [])
        if isinstance(entry, dict)
    ]


def _board_alias_to_entry(board_name: str, metadata_root: Path) -> dict[str, dict[str, Any]]:
    """{board_alias: route_entry} -- mirrors
    scripts/gen_portability_matrix.py's `_route_by_alias`."""
    out: dict[str, dict[str, Any]] = {}
    for entry in _board_route_entries(board_name, metadata_root):
        alias = entry.get("board_alias")
        if isinstance(alias, str):
            out[alias] = entry
    return out


def _pin_pad_and_macro(item: Any) -> tuple[str | None, str | None]:
    """A `pins:` list item is either a bare E1M pad string, or a
    `{e1m, macro?, doc?}` mapping -- normalise both shapes to
    `(pad, macro)`, `None` for whichever half a bare string doesn't
    carry."""
    if isinstance(item, str):
        return item, None
    if isinstance(item, dict):
        pad = item.get("e1m")
        macro = item.get("macro")
        return (
            pad if isinstance(pad, str) else None,
            macro if isinstance(macro, str) else None,
        )
    return None, None


def _alias_for_pin(entries: list[dict[str, Any]], pad: str, macro: str | None) -> str | None:
    """Resolve a `pins:` entry's `board_alias` on its OWN board --
    MACRO-FIRST (issue #876 review MAJOR 1). A pad can carry more
    than one `board_alias:` (e.g. e1m-evk's `E1M_PWM1` is BOTH
    `BOARD_PWM_LED_BLUE` and `BOARD_PWM_ARD1`, at different `macro:`
    entries), and macro names are unique per board -- every one is
    compiled into a single C header (scripts/gen_board_header.py), so
    two entries sharing a macro would be a duplicate-symbol build
    error -- so matching by `macro:` first is unambiguous.

    Only when the pin carries no `macro:` at all (a bare pad-string
    entry) does this fall back to matching by `e1m:` alone, and only
    when exactly ONE route entry claims that pad: a bare-string entry
    naming a multi-alias pad has nothing to disambiguate with, so
    `None` is returned (the caller hard-errors) rather than silently
    picking whichever entry happens to come first in the board's
    `e1m_routes:` -- the same class of silent-wrong-pin bug a naive
    `{alias: pad}` dict inversion (this function's predecessor) had
    for the macro-bearing case."""
    if macro is not None:
        for entry in entries:
            if entry.get("macro") == macro:
                alias = entry.get("board_alias")
                return alias if isinstance(alias, str) else None
        return None
    matches = [entry for entry in entries if entry.get("e1m") == pad]
    if len(matches) != 1:
        return None
    alias = matches[0].get("board_alias")
    return alias if isinstance(alias, str) else None


def _resolve_pin_target(
    item: Any, sku: str, source_preset: str, metadata_root: Path,
) -> dict[str, Any] | None:
    """Resolve ONE `pins:` entry to its target-board route entry
    (issue #876, hardened by the review's MAJOR 1): looks up the
    entry's `board_alias` on `source_preset` via `_alias_for_pin`
    (macro-first, so a multi-alias pad resolves to the RIGHT alias
    instead of whichever wins a lossy pad->alias dict inversion), then
    looks that alias up on `sku`'s own target board preset.

    Returns `None` when `sku`'s own default board preset IS
    `source_preset` -- the canonical example's own sku, or a same-
    family sibling that shares its board preset -- a byte-identical
    passthrough, nothing to resolve.

    DESIGN DECISION (maintainer-approved default): a pad with no
    unambiguous `board_alias:` on `source_preset` -- no cross-EVK
    correspondence declared for that role at all, or a multi-alias
    pad with no `macro:` to disambiguate (issue #876 review MINOR 3)
    -- is a hard error, same philosophy as `_derive_core_renames`'s
    missing-core-class error: a genuinely unsupportable combo must
    fail loudly here, never emit a `pins:` entry that's silently
    stale (or a scaffold `--emit zephyr-conf` then rejects)."""
    target_preset = _default_preset_for_sku(sku, metadata_root)
    if target_preset == source_preset:
        return None
    pad, macro = _pin_pad_and_macro(item)
    if pad is None:
        return None
    alias = _alias_for_pin(_board_route_entries(source_preset, metadata_root), pad, macro)
    if alias is None:
        raise TemplateError(
            f"metadata/boards/{source_preset}.yaml `e1m_routes:` has no "
            f"unambiguous `board_alias:` for pins: entry {item!r} -- can't "
            f"re-derive it for sku {sku!r} (board {target_preset!r})")
    target_entry = _board_alias_to_entry(target_preset, metadata_root).get(alias)
    if target_entry is None:
        raise TemplateError(
            f"metadata/boards/{target_preset}.yaml `e1m_routes:` has no "
            f"route for board_alias {alias!r} (needed to re-derive pins: "
            f"entry {item!r} for sku {sku!r})")
    return target_entry


def _derive_pin_renames(
    original_pins: list[Any], sku: str, source_preset: str, metadata_root: Path,
) -> dict[str, str]:
    """Re-derive every catalog template's `pins:` entries -- each an
    E1M pad name (`E1M_GPIO_IO4`) taken from the CANONICAL example's
    own board preset (`source_preset`, e.g. `e1m-evk`) -- for `sku`'s
    OWN default board preset (issue #876: the #864/#877 stopgap that
    dropped E1M-V2N101 from `peripheral`/`sensor`/`edge-ai`'s
    `supported.som_skus` rather than shipping a scaffold whose `pins:`
    block named an E1M-EVK-only pad that an E1M-X-EVK-resolved
    board.yaml's `e1m_routes:` doesn't have).

    Each item is resolved via `_resolve_pin_target` (macro-first
    `board_alias` match -- see its docstring for the multi-alias
    fix). Returns `{}` when `sku`'s own default board preset IS
    `source_preset` -- a byte-identical passthrough, nothing to
    rewrite. Raises if two DIFFERENT `pins:` entries name the SAME
    source pad but resolve to two different target pads (only
    possible if a template ever lists one pad twice under two
    different `board_alias` roles) -- ambiguous for the flat
    `{old_pad: new_pad}` map `_substitute_board_yaml_pins` applies
    across the whole file, so this fails loudly rather than silently
    keeping whichever resolution happened to run last."""
    renames: dict[str, str] = {}
    for item in original_pins:
        target = _resolve_pin_target(item, sku, source_preset, metadata_root)
        if target is None:
            continue
        pad, _ = _pin_pad_and_macro(item)
        new_pad = target.get("e1m")
        if not isinstance(new_pad, str):
            raise TemplateError(
                f"metadata/boards/{_default_preset_for_sku(sku, metadata_root)}"
                f".yaml route for pins: entry {item!r} has no `e1m:` pad")
        if new_pad == pad:
            continue
        if pad in renames and renames[pad] != new_pad:
            raise TemplateError(
                f"pad {pad!r} re-derives to two different targets "
                f"({renames[pad]!r} and {new_pad!r}) across `pins:` "
                f"entries for sku {sku!r} -- ambiguous")
        renames[pad] = new_pad
    return renames


def _derive_pin_macro_renames(
    original_pins: list[Any], sku: str, source_preset: str, metadata_root: Path,
) -> dict[str, str]:
    """Companion to `_derive_pin_renames`: re-derives a `pins:` entry's
    `macro:` field (`EVK_PIN_ENCODER_SW` -> `XEVK_PIN_ENCODER_SW`)
    alongside whichever pad it renames. Needed because
    `alp_orchestrate.loader._validate_topology_cores`'s `pins:` cross-
    check hard-errors when a declared `macro:` doesn't match the
    resolved board's OWN macro for the (possibly re-derived) pad, not
    only on an unrecognised `e1m:` pad (verified: re-deriving `e1m:`
    alone against `peripheral`/E1M-V2N101 still failed `--emit
    zephyr-conf` with `pins[0].macro: EVK_PIN_ENCODER_SW does not
    match the resolved board 'E1M-X-EVK's macros for pad
    E1M_X_GPIO_IO28: ['XEVK_PIN_ENCODER_SW']`). A bare-string `pins:`
    entry (no `macro:` at all) contributes nothing here -- there's no
    macro to keep in sync. Same passthrough/ambiguity-collision
    philosophy as `_derive_pin_renames` (whose pad-rename result this
    always agrees with -- both resolve the SAME target entry via
    `_resolve_pin_target`, just read a different column off it)."""
    renames: dict[str, str] = {}
    for item in original_pins:
        if not isinstance(item, dict):
            continue
        old_macro = item.get("macro")
        if not isinstance(old_macro, str):
            continue
        target = _resolve_pin_target(item, sku, source_preset, metadata_root)
        if target is None:
            continue
        new_macro = target.get("macro")
        if not isinstance(new_macro, str):
            raise TemplateError(
                f"metadata/boards/{_default_preset_for_sku(sku, metadata_root)}"
                f".yaml route for pins: entry {item!r} has no `macro:`")
        if new_macro == old_macro:
            continue
        if old_macro in renames and renames[old_macro] != new_macro:
            raise TemplateError(
                f"macro {old_macro!r} re-derives to two different targets "
                f"({renames[old_macro]!r} and {new_macro!r}) across "
                f"`pins:` entries for sku {sku!r} -- ambiguous")
        renames[old_macro] = new_macro
    return renames


def _derive_pin_doc_renames(
    original_pins: list[Any], sku: str, source_preset: str, metadata_root: Path,
) -> dict[str, str | None]:
    """Companion to `_derive_pin_renames`: re-derives a `pins:`
    entry's `doc:` field to the TARGET route's own `doc:` (issue #876
    review MAJOR 2) -- a renamed pin's `doc:` otherwise keeps
    describing the SOURCE board's physical pad/electricals (e.g.
    e1m-evk's encoder-switch doc names a PEC12R-4222F-S0024 debounce
    network; e1m-x-evk's own doc for the same role describes a
    different part with RC debounce), which is actively wrong prose
    once the pad itself has changed -- the same "copy the target's
    own doc" behaviour scripts/gen_portability_matrix.py's
    `_remap_pins` already applies for its own (unrelated) board-preset
    swap path.

    A value of `None` in the returned map means DROP the `doc:` field
    entirely -- the target route has no `doc:` of its own, and the
    loader already falls back to the resolved board's own `doc:` in
    that case (metadata/schemas/board.schema.json), so dropping it is
    safe, not a silent content gap. An entry without its own `doc:`
    at all contributes nothing (nothing to re-derive).

    Same ambiguity-collision philosophy as `_derive_pin_renames` and
    `_derive_pin_macro_renames` (issue #1394): a `doc:` string two
    `pins:` entries legitimately SHARE -- one sentence describing a
    debounce network, a bus, or a connector common to both pads --
    keys ONE entry in the flat map `_substitute_board_yaml_pin_docs`
    applies across the whole file, so two entries re-deriving it to
    two different targets must fail loudly instead of silently
    keeping whichever resolution ran last (i.e. whichever `pins:`
    ordering the source file happened to use). `None` participates in
    that check on both sides: "rename it" and "drop it" are
    contradictory instructions for one key, and so are "keep it" (a
    target `doc:` byte-identical to `old_doc`, which contributes no
    map entry) and "drop it" -- the latter pair being the one that
    loses documentation from a pin whose own re-derived `doc:` was
    perfectly good. Hence the separate `resolved` map: it records
    EVERY entry's resolution, including the keep-it ones `renames`
    deliberately omits."""
    renames: dict[str, str | None] = {}
    resolved: dict[str, str | None] = {}
    for item in original_pins:
        if not isinstance(item, dict):
            continue
        old_doc = item.get("doc")
        if not isinstance(old_doc, str):
            continue
        target = _resolve_pin_target(item, sku, source_preset, metadata_root)
        if target is None:
            continue
        new_doc = target.get("doc")
        new = new_doc if isinstance(new_doc, str) else None
        if old_doc in resolved and resolved[old_doc] != new:
            raise TemplateError(
                f"doc {old_doc!r} re-derives to two different targets "
                f"({resolved[old_doc]!r} and {new!r}) across `pins:` "
                f"entries for sku {sku!r} -- ambiguous")
        resolved[old_doc] = new
        if new != old_doc:
            renames[old_doc] = new
    return renames


def _core_board(sku: str, core_id: str | None, metadata_root: Path) -> str | None:
    """`metadata/e1m_modules/<sku>.yaml` `topology.<core_id>.board` --
    the qualified Zephyr board id (`<board>/<soc>/<cpucluster>`) `west
    build -b` needs. `None` for a missing/off/a-class core (no Zephyr
    target) so callers can skip the README board-target rewrite
    cleanly instead of guessing."""
    if not core_id:
        return None
    topology = _topology_for_sku(sku, metadata_root)
    return (topology.get(core_id) or {}).get("board")
