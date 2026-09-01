# SPDX-License-Identifier: Apache-2.0
"""`tan generate --target zephyr-board` must transcribe hardware facts, never
inherit them from a sibling SKU, and must refuse rather than fail open.

tan-cli#493 / tan-cli#591, fixed upstream in alp-sdk#1352 and carried here by
the `zephyr_board.py` hand-port re-sync. The defects these pin:

* **E8 facts on every `E1M-AEN*` SKU.** `"Alif Ensemble E8"`, the
  `#include <alif/ensemble_e8_peripherals.dtsi>` line and the
  `"The Ensemble E8 RTSS-<role> has CONFIG_NUM_IRQS=480"` comment were
  generator constants while `_sku_family_slug()` routes E3/E4/E6 silicon
  down the same path -- so following alp-sdk `docs/porting-new-som.md` §10
  for an E1M-AEN301 emitted an E3 board tree labelled E8, including the E8
  overlay, at exit 0.
* **`atoc` absent blamed the customer's SoM metadata** (tan-cli#591): every
  released alp-sdk predates alp-sdk#1289's SE-owned ATOC reservation, so
  `tan generate` failed on every AEN board with a message that sent readers
  to a `board.yaml` and a preset that are both fine.
* **Three more fail-open paths**: a half-authored `memory_map:` fell back to
  the stock layout on top of the sibling core's declared window; a
  `silicon_variant:` matching no `order_code` fell through to the reverse
  lookup; neither partition branch checked extents.

These generators read maintainer-authored metadata out of a bound alp-sdk
checkout, so the only way to exercise a refusal is to author a bad SoM preset
/ SoC JSON. `_MutatedMetadata` copies the bound checkout's `metadata/` to a
temp dir and mutates the one fact under test, leaving the checkout untouched.
Without a bound checkout there is no metadata to mutate and this module
SKIPS -- visibly, naming the missing variable.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

# `_bound_sdk` is a pytest fixture, imported for its side effect -- the
# same idiom `_baremetal_support`'s consumers use for `bound_sdk_root`.
from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- these tests mutate a copy of that checkout's "
           "metadata/, so there is nothing to mutate. A SKIP about the "
           "missing root, not a pass.",
)

#: NOT an E3 hardware fact, and not a plausible-looking one either: real Alif
#: TCM globals are `0x5xxxxxxx`-shaped. `_port_aen301` below must put SOMETHING
#: in `itcm_global_base` / `dtcm_global_base` for the emit to run at all, and
#: `TBD` is not available -- the generator's own `_is_tbd` would make it refuse.
#: So a deliberately impossible value is used, nothing in this module asserts
#: on it, and it is named here rather than left as a bare `0` in an argument
#: list so nobody mining this fixture for a real `metadata/socs/alif/ensemble/
#: e3.json` can cargo-cult it out. The real E3 numbers are TBD and belong
#: upstream, authored from the datasheet.
_FIXTURE_TCM_BASE = 0

AEN801_PRESET = "e1m_modules/E1M-AEN801.yaml"
AEN301_PRESET = "e1m_modules/E1M-AEN301.yaml"
E8_SOC = "socs/alif/ensemble/e8.json"
E3_SOC = "socs/alif/ensemble/e3.json"

def _emit(*args):
    """Imported inside the call so the module imports before `bind_sdk_root`
    has run (collection order), the same reason `paths` is imported lazily
    across `tan/planner`."""
    from tan.planner.zephyr_board import emit_zephyr_board

    return emit_zephyr_board(*args)


def _emit_error():
    from tan.planner.zephyr_board import ZephyrBoardEmitError

    return ZephyrBoardEmitError


def _sdk_too_old_error():
    from tan.planner.sdk_capability import SdkTooOldError

    return SdkTooOldError


class _MutatedMetadata:
    """Copy the bound checkout's `metadata/` to a temp dir so a test can
    mutate one fact. Same shape as alp-sdk's own
    `tests/scripts/test_gen_zephyr_board.py::_MutatedMetadata`, so a future
    re-sync can diff the two test bodies as well as the two generators."""

    def __enter__(self) -> "_MutatedMetadata":
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "metadata"
        shutil.copytree(SDK / "metadata", self.root)
        return self

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()

    def sub(self, relpath: str, old: str, new: str) -> None:
        path = self.root / relpath
        text = path.read_text(encoding="utf-8")
        assert old in text, f"{old!r} not found in {relpath}"
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def drop_lines(self, relpath: str, needle: str) -> None:
        path = self.root / relpath
        kept = [ln for ln in path.read_text(encoding="utf-8").split("\n")
                if needle not in ln]
        path.write_text("\n".join(kept), encoding="utf-8")

    def json_set(self, relpath: str, key: str, value: object) -> None:
        path = self.root / relpath
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec[key] = value
        path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")

    def json_del(self, relpath: str, key: str) -> None:
        path = self.root / relpath
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec.pop(key, None)
        path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")

    def json_core_set(self, relpath: str, core_id: str, **fields: object) -> None:
        path = self.root / relpath
        spec = json.loads(path.read_text(encoding="utf-8"))
        for core in spec["cores"]:
            if core.get("id") == core_id:
                core.update(fields)
                break
        else:  # pragma: no cover -- a typo'd core id is a broken test, not a finding
            raise AssertionError(f"{relpath} declares no core {core_id!r}")
        path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")

    def drop_quality_task(self, task_id: str) -> None:
        """Simulate an alp-sdk checkout that predates the commit which
        registered *task_id* in `metadata/quality-tasks-v1.json` --
        `tan.planner.sdk_capability.quality_task_declared`'s probe."""
        path = self.root / "quality-tasks-v1.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        before = len(doc["tasks"])
        doc["tasks"] = [t for t in doc["tasks"] if t.get("id") != task_id]
        assert len(doc["tasks"]) == before - 1, (
            f"quality-tasks-v1.json declares no task {task_id!r}")
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    def drop_schema_property(self, schema_relpath: str, prop: str) -> None:
        """Simulate an alp-sdk checkout that predates the commit which added
        *prop* to `metadata/schemas/<schema_relpath>` --
        `tan.planner.sdk_capability.schema_declares_property`'s probe."""
        path = self.root / "schemas" / schema_relpath
        spec = json.loads(path.read_text(encoding="utf-8"))
        assert prop in spec.get("properties", {}), (
            f"{schema_relpath} does not declare property {prop!r}")
        spec["properties"].pop(prop)
        path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")


def _dts(files: dict[str, str]) -> str:
    return next(v for k, v in files.items() if k.endswith(".dts"))


# ======================================================================
# tan-cli#493 (1): the E8 facts
# ======================================================================


def _port_aen301(mm: _MutatedMetadata) -> None:
    """Do to the metadata copy exactly what alp-sdk
    `docs/porting-new-som.md` §10 tells a porter to do for a new AEN SKU:
    a `zephyr_cpucluster` / `itcm_global_base` / `dtcm_global_base` per core
    in the SoC JSON, a `topology.<core>.zephyr_full_name` in the SoM preset,
    and the SoC's own `zephyr_peripherals_dtsi`.

    The TCM base addresses are `_FIXTURE_TCM_BASE` -- see that constant for
    why they are deliberately impossible rather than plausible. Nothing here
    asserts on them; every assertion in the tests that use this helper is
    about the part designator and the peripherals-overlay include, which is
    what tan-cli#493 is about. The real E3 numbers are TBD and belong
    upstream, in `metadata/socs/alif/ensemble/e3.json`, alongside a real
    `zephyr/dts/alif/ensemble_e3_peripherals.dtsi`.
    """
    for core in ("m55_hp", "m55_he"):
        mm.json_core_set(
            E3_SOC, core,
            zephyr_cpucluster=f"rtss_{core.split('_')[1]}",
            itcm_global_base=_FIXTURE_TCM_BASE,
            dtcm_global_base=_FIXTURE_TCM_BASE,
        )
    mm.json_set(E3_SOC, "zephyr_peripherals_dtsi",
                "alif/ensemble_e3_peripherals.dtsi")
    mm.sub(AEN301_PRESET,
           "    board: alp_e1m_aen301_m55_hp        # Zephyr board target",
           "    board: alp_e1m_aen301_m55_hp        # Zephyr board target\n"
           "    zephyr_full_name: Alp E1M-AEN301 M55-HP")


def test_a_non_e8_aen_sku_includes_its_own_peripherals_overlay():
    """The tan-cli#493 headline, reproduced through the documented
    new-AEN-SKU procedure: an E1M-AEN301 board tree must name the E3 and
    include the E3's overlay.

    Before alp-sdk#1352 this emitted `#include
    <alif/ensemble_e8_peripherals.dtsi>` and the header line "Alif Ensemble
    E8" for E3 silicon, exit 0, no warning -- while the twister `.yaml` from
    the same run, which sources its name from the preset, said "Alif Ensemble
    E3". One generated tree contradicted itself, and the board built against
    the wrong SoC's peripheral node set (the E8 overlay declares `ethosu85`;
    an E3 carries 2x Ethos-U55 and no U85).
    """
    with _MutatedMetadata() as mm:
        _port_aen301(mm)
        files = _emit("E1M-AEN301", "m55_hp", mm.root)
        dts = _dts(files)
        kconfig = files["alp_e1m_aen301_m55_hp/Kconfig.defconfig"]
        pinctrl = files[
            "alp_e1m_aen301_m55_hp/alp_e1m_aen301_m55_hp-pinctrl.dtsi"]

    assert "#include <alif/ensemble_e3_peripherals.dtsi>" in dts
    assert "(Alif Ensemble E3, AE302F80F55D5LE)" in dts
    assert "Reuses the upstream Alif E3 SoC" in dts
    assert "The Ensemble E3 RTSS-HP has CONFIG_NUM_IRQS=480" in kconfig
    assert "(Alif Ensemble E3)" in pinctrl
    # No E8 fact may survive anywhere in the E3 tree. The SoC-JSON path in
    # the generated-file banner legitimately says `e3.json`, never `e8`.
    for name, emitted in (("dts", dts), ("Kconfig.defconfig", kconfig),
                          ("pinctrl.dtsi", pinctrl)):
        assert "E8" not in emitted, f"{name} still carries an E8 fact"
        assert "ensemble_e8" not in emitted, f"{name} still includes the E8 overlay"


def test_the_peripherals_overlay_is_read_from_the_soc_json():
    """Not merely derived from the part name: change only the declared
    overlay and the emitted include follows it."""
    with _MutatedMetadata() as mm:
        mm.json_set(E8_SOC, "zephyr_peripherals_dtsi",
                    "alif/ensemble_e9_peripherals.dtsi")
        dts = _dts(_emit("E1M-AEN801", "m55_hp", mm.root))
    assert "#include <alif/ensemble_e9_peripherals.dtsi>" in dts
    assert "ensemble_e8_peripherals.dtsi" not in dts


def test_a_soc_declaring_no_peripherals_overlay_is_refused():
    """The fail-open path becoming a real refusal. Inheriting a sibling
    part's overlay produces a board that builds and then misbehaves on
    silicon, so a SoC that declares none is refused rather than defaulted.

    The bound checkout's OWN schema still declares `zephyr_peripherals_dtsi`
    here (only this SoC's JSON omits it), so `require_capability` sees the
    capability and this stays the plain authoring-gap message -- the
    vintage case below is what fires when the SCHEMA doesn't have it."""
    with _MutatedMetadata() as mm:
        mm.json_del(E8_SOC, "zephyr_peripherals_dtsi")
        with pytest.raises(_emit_error()) as excinfo:
            _emit("E1M-AEN801", "m55_hp", mm.root)
    message = str(excinfo.value)
    assert "zephyr_peripherals_dtsi" in message
    assert "alif:ensemble:e8" in message
    assert "predates" not in message


def test_a_missing_peripherals_overlay_names_the_alp_sdk_vintage_when_the_checkout_predates_it():
    """tan-cli#591 part 2. Every released alp-sdk (measured against v0.15.0,
    the newest tag as of this writing) predates alp-sdk#1352, which added
    `zephyr_peripherals_dtsi` to `soc-spec-v1.schema.json` -- so on a real
    checkout `_aen_peripherals_dtsi` is where `tan generate --target
    zephyr-board` fails on EVERY E1M-AEN801 board today, not the `atoc`
    branch below (`d639e777` predates `7d58ef32`, but is not what a v0.15.0
    checkout is missing when it also lacks `7d58ef32`).

    Dropping the schema property (not just the SoC JSON's own value)
    reproduces that vintage shape directly, rather than inferring it from
    the SoC JSON alone -- the schema is the checkout-level fact; the SoC
    JSON's own omission is a red herring an old checkout ALSO exhibits, but
    is not what the refusal should be blaming.
    """
    with _MutatedMetadata() as mm:
        mm.drop_schema_property("soc-spec-v1.schema.json", "zephyr_peripherals_dtsi")
        mm.json_del(E8_SOC, "zephyr_peripherals_dtsi")
        with pytest.raises(_sdk_too_old_error()) as excinfo:
            _emit("E1M-AEN801", "m55_hp", mm.root)
    message = str(excinfo.value)
    assert "this alp-sdk predates" in message
    assert "alp-sdk#1352" in message
    assert "upgrade alp-sdk" in message


def test_the_part_designator_is_read_from_the_soc_json():
    with _MutatedMetadata() as mm:
        mm.json_set(E8_SOC, "part", "E9")
        files = _emit("E1M-AEN801", "m55_hp", mm.root)
        dts, kconfig = _dts(files), files["alp_e1m_aen801_m55_hp/Kconfig.defconfig"]
    assert "(Alif Ensemble E9, AE822FA0E5597LS0)" in dts
    assert "The Ensemble E9 RTSS-HP has CONFIG_NUM_IRQS=480" in kconfig
    assert "Ensemble E8" not in dts and "Ensemble E8" not in kconfig


def test_the_defconfig_console_pads_come_from_the_pinmux():
    """The `_defconfig` console comment spelled the AEN801 pads `P3_4/P3_5`
    as a literal on every SKU while the sibling `.dts` and `-pinctrl.dtsi`
    read them from `metadata/pinmux/aen.yaml`."""
    with _MutatedMetadata() as mm:
        mm.sub("pinmux/aen.yaml",
               'silicon_peripheral: "UART5_RX_A", silicon_pad: "P3_4"',
               'silicon_peripheral: "UART5_RX_A", silicon_pad: "P7_0"')
        mm.sub("pinmux/aen.yaml",
               'silicon_peripheral: "UART5_TX_A", silicon_pad: "P3_5"',
               'silicon_peripheral: "UART5_TX_A", silicon_pad: "P7_1"')
        files = _emit("E1M-AEN801", "m55_hp", mm.root)
        defconfig = files[
            "alp_e1m_aen801_m55_hp/"
            "alp_e1m_aen801_m55_hp_ae822fa0e5597ls0_rtss_hp_defconfig"]
    assert '(E1M edge "UART0", P7_0/P7_1)' in defconfig
    assert "P3_4" not in defconfig


# ======================================================================
# tan-cli#591: the ATOC message named the wrong culprit
# ======================================================================


def test_a_missing_atoc_names_the_alp_sdk_vintage_when_the_checkout_predates_it():
    """tan-cli#591. An alp-sdk checkout from before alp-sdk#1289 does not
    list `atoc-reservation` in `metadata/quality-tasks-v1.json` -- the
    checkout-level fact `require_capability` actually checks -- and
    (consistent with that vintage) has no `atoc` region in this SoM's own
    `memory_map:` either. `tan generate` used to fail on every AEN board
    with *"AEN disjoint-slot0 memory_map is missing an integer-`base` region
    named 'atoc'"*, which reads as a defect in the consumer's own SoM
    metadata; it must now name the real cause instead.

    Before this change the vintage message fired off a HEURISTIC over the
    SoM preset's own shape ("every other region is present, so this one
    must be a vintage checkout") -- see
    `test_a_missing_atoc_reads_as_an_authoring_gap_when_the_checkout_has_the_capability`
    below for why that was imprecise, and is not what this test exercises
    any more.
    """
    with _MutatedMetadata() as mm:
        mm.drop_quality_task("atoc-reservation")
        mm.drop_lines(AEN801_PRESET, "name: atoc")
        with pytest.raises(_sdk_too_old_error()) as excinfo:
            _emit("E1M-AEN801", "m55_hp", mm.root)
    message = str(excinfo.value)
    assert "this alp-sdk predates the SE-owned ATOC reservation" in message
    assert "alp-sdk#1289" in message
    assert "upgrade alp-sdk" in message


def test_a_missing_atoc_reads_as_an_authoring_gap_when_the_checkout_has_the_capability():
    """The other half of tan-cli#591's fix. When the bound checkout's own
    `metadata/quality-tasks-v1.json` DOES list `atoc-reservation` --
    unmodified here, so this is the checkout's real, current state -- an
    `atoc`-less SoM preset is a genuine authoring gap, not a vintage
    checkout, and must not borrow the vintage wording.

    Before this change, dropping `atoc` from an otherwise-complete map hit
    the vintage message REGARDLESS of whether the bound checkout was
    actually old -- a heuristic inferred from the SoM preset's own shape,
    not from the checkout. This test is the case that heuristic got wrong:
    an author editing a fresh preset who simply hasn't added `atoc` yet was
    told their alp-sdk predates a commit it does not predate.
    """
    with _MutatedMetadata() as mm:
        mm.drop_lines(AEN801_PRESET, "name: atoc")
        with pytest.raises(_emit_error()) as excinfo:
            _emit("E1M-AEN801", "m55_hp", mm.root)
    message = str(excinfo.value)
    assert "'atoc'" in message
    assert "predates" not in message


def test_a_missing_non_atoc_region_still_reads_as_an_authoring_gap():
    """The other half of the same message: only `atoc`'s absence is ever
    checked against the SDK floor, so nothing else may claim to be a
    vintage problem."""
    with _MutatedMetadata() as mm:
        mm.drop_lines(AEN801_PRESET, "name: reserved")
        with pytest.raises(_emit_error()) as excinfo:
            _emit("E1M-AEN801", "m55_hp", mm.root)
    message = str(excinfo.value)
    assert "'reserved'" in message
    assert "predates" not in message


# ======================================================================
# tan-cli#493 (2), (3), (4): the remaining fail-open paths
# ======================================================================


def test_a_half_authored_slot0_map_raises_instead_of_overlaying_the_sibling():
    """Dropping only `hp_slot0` used to fall back to the stock symmetric
    layout, putting `slot0_partition@10000` byte-for-byte on top of the
    `he_slot0` window the same file still declares -- silently undoing
    alp-sdk#1069's disjoint-window fix and re-creating the bench-confirmed
    corruption it exists to prevent."""
    with _MutatedMetadata() as mm:
        mm.drop_lines(AEN801_PRESET, "name: hp_slot0")
        with pytest.raises(_emit_error()) as excinfo:
            _emit("E1M-AEN801", "m55_hp", mm.root)
    message = str(excinfo.value)
    assert "'hp_slot0'" in message
    assert "#1069" in message


def test_a_declared_silicon_variant_naming_no_order_code_raises():
    """`...LS0` typo'd to `...LSO` used to be discarded without a diagnostic,
    the `alp_module_skus` reverse lookup answered instead, and a whole board
    tree came out named after a part number the preset does not declare."""
    with _MutatedMetadata() as mm:
        mm.sub(AEN801_PRESET, "silicon_variant: AE822FA0E5597LS0",
               "silicon_variant: AE822FA0E5597LSO")
        with pytest.raises(_emit_error()) as excinfo:
            _emit("E1M-AEN801", "m55_hp", mm.root)
    message = str(excinfo.value)
    assert "AE822FA0E5597LSO" in message
    assert "AE822FA0E5597LS0" in message


def test_a_tbd_silicon_variant_still_falls_back_to_the_reverse_lookup():
    """The refusal above must not swallow the legitimate case: a preset that
    declares no real variant yet still resolves through `alp_module_skus`."""
    with _MutatedMetadata() as mm:
        mm.sub(AEN801_PRESET, "silicon_variant: AE822FA0E5597LS0",
               "silicon_variant: TBD")
        files = _emit("E1M-AEN801", "m55_hp", mm.root)
    assert "    - name: ae822fa0e5597ls0\n" in files["alp_e1m_aen801_m55_hp/board.yml"]


def test_a_region_grown_past_the_mram_window_raises():
    """A `storage` region grown to 2048 KiB used to emit
    `partition@560000 reg = <0x560000 DT_SIZE_K(2048)>` 1.9 MiB outside its
    own `mram_storage` `reg`, at exit 0.

    MEASURED, not assumed: the guard that catches this one is the whole-map
    cross-check (`_aen_check_map_overlaps`), which runs first and reports the
    region against the App MRAM window. The per-partition extent check
    (`_aen_check_extents`) has its own test below, because a metadata
    mutation cannot reach it without tripping this one first.
    """
    with _MutatedMetadata() as mm:
        mm.sub(AEN801_PRESET,
               "name: storage,   base: 0x80560000, size_kib: 96",
               "name: storage,   base: 0x80560000, size_kib: 2048")
        with pytest.raises(_emit_error()) as excinfo:
            _emit("E1M-AEN801", "m55_hp", mm.root)
    assert "outside the 5632 KiB App MRAM window" in str(excinfo.value)


def test_the_partition_extent_check_refuses_a_partition_past_the_flash_node():
    """`_aen_check_extents` pinned directly.

    It is unreachable from a metadata mutation -- the whole-map check above
    fires first on anything that would trip it -- so pinning it only through
    `emit_zephyr_board` would leave the guard itself untested while the test
    passed for another guard's reason. Called directly instead: the stock
    branch's `offset != total_kib * 1024` assertion is a tautology (all five
    stock sizes derive from `total_kib`) and never a fits-in-MRAM check, so
    this is the only thing standing between a mis-authored map and a
    `partition@` node outside the flash device MCUboot's flash_map reads.
    """
    from tan.planner.zephyr_board import _aen_check_extents

    # In bounds, non-overlapping: accepted.
    _aen_check_extents([("mcuboot", 0, 64), ("image-0", 64 * 1024, 5568)],
                       5632, "unit")

    with pytest.raises(_emit_error()) as past_end:
        _aen_check_extents([("image-0", 0, 6000)], 5632, "unit")
    assert "outside the flash device it is declared in" in str(past_end.value)

    with pytest.raises(_emit_error()) as overlapping:
        _aen_check_extents([("mcuboot", 0, 64), ("image-0", 32 * 1024, 64)],
                           5632, "unit")
    assert "overlaps" in str(overlapping.value)

    with pytest.raises(_emit_error()) as nonpositive:
        _aen_check_extents([("image-0", 0, 0)], 5632, "unit")
    assert "non-positive size" in str(nonpositive.value)


def test_overlapping_memory_map_regions_raise():
    """The sibling core's `<role>_slot0` window is not a partition in this
    core's table, so the partition-level extent check cannot see a map that
    overlaps it -- the whole-map cross-check can."""
    with _MutatedMetadata() as mm:
        mm.sub(AEN801_PRESET, "name: hp_slot0,  base: 0x802b0000",
               "name: hp_slot0,  base: 0x802a0000")
        with pytest.raises(_emit_error()) as excinfo:
            _emit("E1M-AEN801", "m55_hp", mm.root)
    assert "overlap" in str(excinfo.value)


def test_an_mcuboot_base_off_the_mram_window_raises():
    """`mcuboot`'s base IS the soc-nv-flash child's offset-0 origin, but the
    child's own node address was a hardcoded `0x80000000` literal -- so a map
    shifted 32 KiB emitted every partition below its declared physical
    address, with the `.dts` and `_defconfig` disagreeing about where slot0
    is."""
    with _MutatedMetadata() as mm:
        for base in (0x80000000, 0x80010000, 0x802B0000, 0x80550000,
                     0x80560000, 0x80578000):
            mm.sub(AEN801_PRESET, f"base: 0x{base:08x}",
                   f"base: 0x{base + 0x8000:08x}")
        with pytest.raises(_emit_error()) as excinfo:
            _emit("E1M-AEN801", "m55_hp", mm.root)
    message = str(excinfo.value)
    assert "anchors 'mcuboot' at 0x80008000" in message
    assert "0x80000000" in message
