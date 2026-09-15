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
import re
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

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

    def drop_yaml_key(self, relpath: str, *keys: str) -> None:
        """Delete a nested YAML key via a `yaml.safe_load`/`safe_dump`
        round-trip -- simulates an alp-sdk checkout whose YAML predates the
        key's introduction (alp-sdk#2036's `e1m_i2c0` on-module link), the
        YAML counterpart of `json_del` for a JSON spec."""
        path = self.root / relpath
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        node = doc
        for key in keys[:-1]:
            node = node[key]
        assert keys[-1] in node, f"{relpath} has no {'.'.join(keys)}"
        del node[keys[-1]]
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

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


def _flat(text: str) -> str:
    r"""Collapse a C block comment's `\n * ` continuations and runs of
    whitespace, so an assertion can match prose that `_c_comment()` /
    `textwrap` REFLOWED to comment width without pinning where the line
    breaks happen to land. alp-sdk's own
    `tests/scripts/test_gen_zephyr_board.py` grew the same helper for the
    same reason (alp-sdk#2046, alp-sdk#2062).

    It deliberately does NOT undo a HYPHEN break: `textwrap` (with
    `break_on_hyphens`, the default `_c_comment` uses) may split
    `bench-validated` across lines, which flattens to `bench- validated`.
    So a literal spanning a hyphen is still unsafe here -- match on a
    hyphen-free fragment, or on one whose wrap position is fixed because
    nothing variable precedes it.
    """
    return " ".join(re.sub(r"\n\s*\*\s?", " ", text).split())


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
    #
    # ONE exception, scrubbed from pinctrl.dtsi ONLY and only after pinning
    # that it occurs there exactly once: `_aen_e1m_i2c0_pinctrl_group()`
    # cites the E8 bench run as the PROVENANCE of a pad config that
    # on-module-links.yaml itself scopes to the whole AEN family
    # ("Family-scoped -- one file backs every AEN SKU and both M55 cores"),
    # not as a claim about THIS SKU's own silicon identity -- unlike the
    # header banner / overlay include / IRQ-count facts this loop exists to
    # catch, which genuinely differ per SoC and must never leak.
    #
    # UPDATED for alp-sdk#2046, which deliberately did NOT silence that
    # citation on a non-E8 part the way alp-sdk#1988 silences
    # `rtc_alarm.risk`: the emitted pad VALUE (bias-pull-down) is unchanged
    # and still correct for every part, so the citation stays and is
    # QUALIFIED ("bench-validated ONLY on the E8 ... has NOT been
    # independently repeated on the E3"). Two consequences for this scrub,
    # both load-bearing:
    #   * the sanctioned text is now a whole PARAGRAPH, not alp-sdk#2036's
    #     single sentence; and
    #   * `_c_comment()` REFLOWS it to comment width, so
    #     "bench-validated 2026-06-15 on the E8" no longer occurs as a
    #     contiguous substring at all -- it wraps as "bench-\n\t * validated
    #     2026-06-15 on the E8". Scrubbing that literal now removes NOTHING
    #     and the old form of this assertion read 0 == 1.
    # So the scrub runs on the FLATTENED text and is delimited by the
    # paragraph's own opening and closing clauses, neither of which a line
    # break can split (see `_flat`). It is still exact: anything outside
    # that one span fails, and `.dts` / `Kconfig.defconfig` get no
    # exception whatsoever.
    #
    # Whether E8-evidenced `bias-pull-down` holds on E3 silicon is alp-sdk's
    # question (the #1988 per-part-map class); tan emits the bytes verbatim.
    bench_open = "input-enable + bias-pull-down:"
    bench_close = "but that silence is not proof either way."
    flat_pinctrl = _flat(pinctrl)
    assert flat_pinctrl.count(bench_open) == 1, (
        "the one sanctioned E8 bench-provenance paragraph must open exactly "
        "once, in pinctrl.dtsi -- a second copy is not covered by this "
        "exception")
    assert flat_pinctrl.count(bench_close) == 1, (
        "the sanctioned paragraph must close exactly once -- if its wording "
        "moved, re-pin this scrub rather than widening it")
    start = flat_pinctrl.index(bench_open)
    end = flat_pinctrl.index(bench_close, start) + len(bench_close)
    sanctioned = flat_pinctrl[start:end]
    # The citation must still be EMITTED and part-qualified: alp-sdk#2046's
    # whole point is that silence is not the fix, so a scrub that passed
    # because the paragraph vanished would be the wrong kind of green.
    assert "bench-validated ONLY on the E8" in sanctioned
    assert "independently repeated on the E3" in sanctioned
    scrubbed = flat_pinctrl[:start] + flat_pinctrl[end:]
    assert "E8" not in scrubbed, (
        "pinctrl.dtsi carries an E8 fact OUTSIDE the sanctioned "
        "bench-provenance paragraph")
    for name, emitted in (("dts", dts), ("Kconfig.defconfig", kconfig)):
        assert "E8" not in emitted, f"{name} still carries an E8 fact"
    for name, emitted in (("dts", dts), ("Kconfig.defconfig", kconfig),
                          ("pinctrl.dtsi", pinctrl)):
        assert "ensemble_e8" not in emitted, f"{name} still includes the E8 overlay"


def test_the_ae822_port15_risk_note_is_scoped_to_actual_e8_silicon():
    """The regression `test_a_non_e8_aen_sku_includes_its_own_peripherals_overlay`
    above caught for real: `on-module-links.yaml`'s `rtc_alarm.risk` string
    is evidenced ONLY against "The AE822 DFP" (the E8 part's own datasheet),
    but the file scopes itself to "the E1M-AEN family", not to a part -- so
    an unscoped emitter put an E8-specific register-layout warning into
    every AEN SKU's `.dts`, E3/E4/E6 included. The fix gates it on the SoC
    JSON's own `part`, not on the on-module wiring (which IS shared across
    the family): an E1M-AEN301 (E3) must not carry it, an E1M-AEN801 (E8,
    unmutated -- this IS the AE822 part the note is evidenced against) must
    still carry it.
    """
    with _MutatedMetadata() as mm:
        dts_e8 = _dts(_emit("E1M-AEN801", "m55_hp", mm.root))
    assert "AE822 DFP warns for port 15" in dts_e8
    assert "LPGPIO_CTRL_n register" in dts_e8

    with _MutatedMetadata() as mm:
        _port_aen301(mm)
        dts_e3 = _dts(_emit("E1M-AEN301", "m55_hp", mm.root))
    assert "AE822 DFP warns for port 15" not in dts_e3
    assert "LPGPIO_CTRL_n register" not in dts_e3


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


# ======================================================================
# alp-sdk#2073: the whole-device-alias exception in _aen_check_map_overlaps
# ======================================================================

#: The real E1M-AEN801 `mram_main` row, whose `base` ships as the `"TBD"`
#: sentinel precisely BECAUSE resolving it used to trip the overlap check
#: (the preset's own comment says so, and says not to resolve it again
#: until the exception lands). Every test below resolves it on a copy.
_MRAM_MAIN_TBD = 'name: mram_main, base: "TBD",      size_kib: 5632'
_MRAM_MAIN_RESOLVED = "name: mram_main, base: 0x80000000, size_kib: 5632"


def test_whole_device_alias_does_not_overlap_its_own_partitions():
    """`mram_main` deliberately spans the same 5632 KiB window that
    `mcuboot`/`he_slot0`/`hp_slot0`/`reserved`/`storage`/`atoc` subdivide
    (alp-sdk#2073) -- once its `base` resolves to a real address it must
    NOT be reported as overlapping every region inside it.
    `tan.planner.aperture.classify_region()` already carries this exact
    "extent == aperture exactly" exception; this pins that
    `_aen_check_map_overlaps()` now agrees with it instead of refusing the
    very shape the SoM presets declare on purpose."""
    with _MutatedMetadata() as mm:
        mm.sub(AEN801_PRESET, _MRAM_MAIN_TBD, _MRAM_MAIN_RESOLVED)
        files = _emit("E1M-AEN801", "m55_hp", mm.root)
    assert "alp_e1m_aen801_m55_hp/board.yml" in files


def test_whole_device_alias_exception_does_not_swallow_a_real_overlap():
    """The whole-device-alias exception must be narrow: a genuine overlap
    between two ordinary partitions is still refused even while
    `mram_main` (also spanning the whole window) sits in the same
    `memory_map:`."""
    with _MutatedMetadata() as mm:
        mm.sub(AEN801_PRESET, _MRAM_MAIN_TBD, _MRAM_MAIN_RESOLVED)
        mm.sub(AEN801_PRESET, "name: hp_slot0,  base: 0x802b0000",
               "name: hp_slot0,  base: 0x802a0000")
        with pytest.raises(_emit_error()) as excinfo:
            _emit("E1M-AEN801", "m55_hp", mm.root)
    assert "overlap" in str(excinfo.value)


def test_two_whole_device_aliases_raise_instead_of_going_uncompared():
    """The whole-device-alias exclusion drops matching rows from the
    pairwise overlap comparison entirely -- so two rows that BOTH match the
    aperture exactly would otherwise never be compared against each other
    at all, silently accepting a duplicate alias. `classify_region()` would
    call both `flash`, so neither becomes an IPC carve-out target either
    way, but a duplicate whole-device alias is still a bad input and must
    be refused, not passed through quietly (review of alp-sdk#2073)."""
    with _MutatedMetadata() as mm:
        mm.sub(AEN801_PRESET, _MRAM_MAIN_TBD, _MRAM_MAIN_RESOLVED)
        mm.sub(
            AEN801_PRESET,
            "- { name: mram_main, base: 0x80000000, size_kib: 5632, "
            "accessible_from: [a32_cluster, m55_he, m55_hp], "
            "cacheable: true, write_authority: composite }",
            "- { name: mram_main, base: 0x80000000, size_kib: 5632, "
            "accessible_from: [a32_cluster, m55_he, m55_hp], "
            "cacheable: true, write_authority: composite }\n"
            "  - { name: mram_dup,  base: 0x80000000, size_kib: 5632, "
            "accessible_from: [a32_cluster, m55_he, m55_hp], "
            "cacheable: true, write_authority: customer_runtime }")
        with pytest.raises(_emit_error()) as excinfo:
            _emit("E1M-AEN801", "m55_hp", mm.root)
    message = str(excinfo.value)
    assert "'mram_main'" in message
    assert "'mram_dup'" in message
    assert "only one whole-device alias" in message


def test_same_base_smaller_size_is_not_a_whole_device_alias():
    """A region flush with the aperture's low edge but one KiB short of its
    full extent is a genuine (mis-sized) partition, not the whole-device
    alias -- it must still overlap `mcuboot` at the same base and be
    refused. This is the one test that still catches a predicate loosened
    to `lo == full_lo` alone (dropping the `hi == full_hi` half) IF the
    duplicate-whole-device-alias guard above is ever removed -- today that
    loosening also makes `mcuboot` match as a second "alias", so the
    duplicate-alias refusal fires first and two other tests here go red
    too; this test is what still fails on the loosening alone (review of
    alp-sdk#2073)."""
    with _MutatedMetadata() as mm:
        mm.sub(AEN801_PRESET, _MRAM_MAIN_TBD,
               "name: mram_main, base: 0x80000000, size_kib: 5631")
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


# ======================================================================
# alp-sdk#2036: e1m_i2c0 (SoC I2C2) board-layer pinctrl group + DT node
# ======================================================================


def test_the_e1m_i2c0_pinctrl_group_carries_the_metadata_pads_and_pinmux_order():
    """SoC I2C2 (e1m_i2c0, portable alp-i2c0) gets its own board-layer
    pinctrl group.  Pinmux order is SCL then SDA, matching the
    bench-validated `aen-i2c2-eeprom-regcheck` / `aen-eeprom-manifest`
    overlays verbatim -- NOT the SDA-first order `_aen_i2c_pinctrl_group`'s
    BRD_I2C group above happens to use."""
    with _MutatedMetadata() as mm:
        pinctrl = _emit("E1M-AEN801", "m55_hp", mm.root)[
            "alp_e1m_aen801_m55_hp/alp_e1m_aen801_m55_hp-pinctrl.dtsi"]
    assert "\tpinctrl_i2c2: pinctrl_i2c2 {\n" in pinctrl
    assert (
        "\t\t\tpinmux = <PIN_P5_6__I2C2_SCL_C>, <PIN_P5_7__I2C2_SDA_C>;\n"
        in pinctrl
    )
    assert "\t\t\tinput-enable;\n" in pinctrl
    assert "\t\t\tbias-pull-down;\n" in pinctrl


def test_the_e1m_i2c0_dts_node_and_alias_are_emitted():
    """The `&i2c2` board-layer node + the `alp-i2c0` alias
    `alp_i2c_open(.bus_id = ALP_E1M_I2C0)` resolves through
    `DT_ALIAS(alp_i2c0)` -- alp-sdk#2036's `_aen_e1m_i2c0_dts()`."""
    with _MutatedMetadata() as mm:
        dts = _dts(_emit("E1M-AEN801", "m55_hp", mm.root))
    assert "&i2c2 {\n" in dts
    assert '\tstatus = "okay";\n' in dts
    assert "\tpinctrl-0 = <&pinctrl_i2c2>;\n" in dts
    assert "\tclock-frequency = <I2C_BITRATE_STANDARD>;\n" in dts
    assert "\t\talp-i2c0 = &i2c2;\n" in dts


def test_a_missing_e1m_i2c0_on_module_link_is_refused():
    """Every AEN board tree needs `e1m_i2c0` alongside `brd_i2c` /
    `rtc_alarm` in `on-module-links.yaml` -- alp-sdk#2036 hard-requires it
    the same way those two are already hard-required, no SDK-vintage floor
    (every released alp-sdk predates `on-module-links.yaml` entirely, so
    this emitter already needs a post-release checkout regardless)."""
    with _MutatedMetadata() as mm:
        mm.drop_yaml_key(
            "e1m_modules/aen/on-module-links.yaml", "on_module_links",
            "e1m_i2c0")
        with pytest.raises(_emit_error()) as excinfo:
            _emit("E1M-AEN801", "m55_hp", mm.root)
    message = str(excinfo.value)
    assert "'e1m_i2c0'" in message


# ======================================================================
# alp-sdk#2046: the E8 bench citation is scoped to the part it was measured
# on; tan-cli#1253: both per-part maps are shape-checked on load
# ======================================================================

ON_MODULE_LINKS = "e1m_modules/aen/on-module-links.yaml"


def test_e1m_i2c0_bench_validation_is_scoped_to_the_part_it_was_measured_on():
    """`on_module_links.e1m_i2c0.bench_validation` is a per-part map, same
    pattern as `rtc_alarm.risk` (alp-sdk#1988) -- its only entry (`E8`) is
    evidenced against an E8 bench run, so a board tree for any OTHER part
    must say so instead of inheriting the E8 citation unqualified
    (alp-sdk#2046). Unlike `rtc_alarm.risk`, the citation itself must still
    appear (not be silently dropped) because the emitted pad VALUE is
    unchanged and correct for every part -- only the claim of WHICH part it
    was bench-validated on must not overreach."""
    with _MutatedMetadata() as mm:
        mm.json_set(E8_SOC, "part", "E3")
        pinctrl = _emit("E1M-AEN801", "m55_hp", mm.root)[
            "alp_e1m_aen801_m55_hp/alp_e1m_aen801_m55_hp-pinctrl.dtsi"]
    # Comment prose is reflowed to house-style width, so match on the
    # flattened text rather than a literal that could straddle a wrapped
    # line break -- see `_flat`.
    flat = _flat(pinctrl)
    assert "bench-validated ONLY on the E8" in flat
    assert "NOT been" in flat
    assert "independently repeated on the E3" in flat
    # Still bias-pull-down: alp-sdk#2046 is explicit that the emitted pad
    # VALUE never changes on the strength of either part-scoping or either
    # pull-direction reading.
    assert "bias-pull-down;" in pinctrl
    assert "Ensemble E8" not in pinctrl
    assert "Alif E8" not in pinctrl

    # The genuine, unmutated E8 board must still cite its own bench run
    # plainly, without the "NOT been independently repeated" hedge.
    with _MutatedMetadata() as mm:
        real_pinctrl = _emit("E1M-AEN801", "m55_hp", mm.root)[
            "alp_e1m_aen801_m55_hp/alp_e1m_aen801_m55_hp-pinctrl.dtsi"]
    assert (
        "input-enable + bias-pull-down: bench-validated 2026-06-15 on "
        "the E8," in real_pinctrl)
    assert "bench-validated ONLY on the E8" not in real_pinctrl


def test_e1m_i2c0_bench_validation_must_be_a_part_keyed_map():
    """`_load_aen_on_module_links()` shape-checks
    `e1m_i2c0.bench_validation` the same way it already shape-checks
    `rtc_alarm.risk` (alp-sdk#1988, alp-sdk#2046): reverting the YAML to a
    flat-string form must raise `ZephyrBoardEmitError`, naming the file,
    rather than an uncaught `AttributeError` from the `.get(part)` lookup
    deep in `_aen_e1m_i2c0_pinctrl_group()`."""
    with _MutatedMetadata() as mm:
        mm.sub(ON_MODULE_LINKS,
               "    bench_validation:\n      E8: >-\n",
               "    bench_validation: >-\n")
        with pytest.raises(_emit_error()) as excinfo:
            _emit("E1M-AEN801", "m55_hp", mm.root)
    message = str(excinfo.value)
    assert "on-module-links.yaml" in message
    assert "e1m_i2c0.bench_validation" in message


def test_rtc_alarm_risk_must_be_a_part_keyed_map():
    """tan-cli#1253. tan carried alp-sdk#1988's per-part `risk:` READER
    (`(alarm.get("risk") or {}).get(part)` in `_aen_brd_i2c_dts`) without
    the matching shape CHECK in `_load_aen_on_module_links()`, so a
    metadata author who wrote the pre-#1988 flat-string form got
    `AttributeError: 'str' object has no attribute 'get'` from inside the
    emitter -- an uncurated traceback naming neither the file nor the
    field, for an ordinary authoring mistake.

    A curated `ZephyrBoardEmitError` must replace it, and must name both
    the file and `rtc_alarm.risk` so the author knows what to edit."""
    with _MutatedMetadata() as mm:
        mm.sub(ON_MODULE_LINKS,
               "    risk:\n      E8: >-\n",
               "    risk: >-\n")
        with pytest.raises(_emit_error()) as excinfo:
            _emit("E1M-AEN801", "m55_hp", mm.root)
    message = str(excinfo.value)
    assert "on-module-links.yaml" in message
    assert "rtc_alarm.risk" in message
    # The curated refusal must also teach the SHAPE, not just name the
    # field -- this is the message that replaces the AttributeError.
    assert "must be a map keyed by" in message


# ======================================================================
# alp-sdk#2062: OSPI0 NOR + HyperRAM population is read from the SoM preset
# ======================================================================

#: Anchors for the `_MutatedMetadata.sub()` calls below -- the real,
#: unmutated E1M-AEN801.yaml text for the ospi0/hyperram `chip:` +
#: `assembled:` pair, kept in one place so every test below shares it.
_OSPI0_BLOCK = (
    "      chip:           IS25WX256-JHLE      # ISSI xSPI NOR, "
    "U10 footprint (same part E1M-AEN803 fits, #2041)\n"
    "      # NOT populated on this SKU.  E1M-AEN803 is the SKU "
    "that fits both external\n"
    "      # memories; E1M-AEN801 fits neither and runs from "
    "the SoC's on-die MRAM.\n"
    "      # The footprint exists on the shared PCB -- see the "
    "R2 netlist -- which is\n"
    "      # why the part is still described here.\n"
    "      assembled:      false")
_HYPERRAM_BLOCK = (
    "    chip:           S80KS5122GABHM02  # Infineon/Cypress HyperRAM, "
    "U9 footprint (same part E1M-AEN803 fits, #2041)\n"
    "    # NOT populated on this SKU -- see the ospi0 note above.  "
    "This key is\n"
    "    # load-bearing: `assembled` defaults to TRUE, so omitting it "
    "made every\n"
    "    # consumer treat the HyperRAM as fitted, and the boot banner "
    "advertised\n"
    "    # 256 Mbit of external RAM on a module that has none.\n"
    "    assembled:      false")


def test_ospi0_storage_banner_names_are_read_from_the_som_preset():
    """The boot/storage banner's OSPI0 NOR + HyperRAM part names used to be
    generator constants (Macronix `MX25UM25645` + Winbond `W958D8NB`)
    applied to every AEN SKU -- alp-sdk#2062: E1M-AEN803 fits the same
    U10/U9 footprint with a different, measured part (ISSI NOR,
    Infineon/Cypress HyperRAM). The real, unmutated E1M-AEN801 board must
    name its own preset's parts, not the old hardcoded ones, say NOT
    populated (its real `assembled: false` state on BOTH devices), and the
    MRAM-partition-map comment further down must say the same thing -- not
    the old, separately-hardcoded "not populated on this batch" that could
    (and did) disagree with the banner above it."""
    with _MutatedMetadata() as mm:
        flat = _flat(_dts(_emit("E1M-AEN801", "m55_hp", mm.root)))
    assert "IS25WX256-JHLE" in flat
    assert "S80KS5122GABHM02" in flat
    assert (
        "OSPI0 NOR (IS25WX256-JHLE) + HyperRAM (S80KS5122GABHM02) are "
        "not populated, so there is no external XIP / flash device" in flat)
    assert (
        "MRAM-only: OSPI0 NOR (IS25WX256-JHLE) + HyperRAM "
        "(S80KS5122GABHM02) are not populated, so boot" in flat)
    assert "not populated on this batch" not in flat
    assert "MX25UM25645" not in flat
    assert "W958D8NB" not in flat


def test_ospi0_storage_banner_reflects_a_populated_preset():
    """A SoM preset that DOES populate OSPI0 must get banner prose (and the
    MRAM-partition-map comment) that says so, naming whatever parts its own
    preset declares for BOTH devices independently -- mutating only `ospi0`
    (leaving `hyperram`'s real chip: string untouched) would let a
    hardcoded HyperRAM name pass this test unnoticed, which is exactly the
    shape of bug this fix exists for (mutated off E1M-AEN801, since no
    shipped SKU with a real board tree populates it today)."""
    with _MutatedMetadata() as mm:
        mm.sub(AEN801_PRESET, _OSPI0_BLOCK,
               "      chip:           TEST-NOR-PART\n"
               "      assembled:      true")
        mm.sub(AEN801_PRESET, _HYPERRAM_BLOCK,
               "    chip:           TEST-RAM-PART\n"
               "    assembled:      true")
        flat = _flat(_dts(_emit("E1M-AEN801", "m55_hp", mm.root)))
    assert "TEST-NOR-PART" in flat
    assert "TEST-RAM-PART" in flat
    # alp-sdk#2062 review round 3: a true die-level fact ("OSPI_XIP_SER does
    # not exist") does not prove XIP is impossible here -- Alif's own
    # ospi_psram_xip.c still calls aes_enable_xip() under that same guard.
    # The real, non-overreaching reason: hal_alif's OWN XIP enable path
    # targets that absent register, and flash_ospi_alif.c ships no
    # flash_driver_api at all (#915) -- true regardless of silicon
    # capability.
    assert (
        "OSPI0 NOR (TEST-NOR-PART) + HyperRAM (TEST-RAM-PART) are "
        "populated; neither is used for XIP boot here (hal_alif's "
        "alif_hal_ospi_xip_enable() targets the XIP_SER register, "
        "absent on this die, and flash_ospi_alif.c ships no "
        "flash_driver_api -- #915)" in flat)
    assert (
        "MRAM-only regardless: OSPI0 NOR (TEST-NOR-PART) + HyperRAM "
        "(TEST-RAM-PART) are populated; hal_alif's "
        "alif_hal_ospi_xip_enable() targets the XIP_SER register, "
        "absent on this die, and flash_ospi_alif.c ships no "
        "flash_driver_api -- #915" in flat)
    assert "not populated" not in flat
    # The die-level fact must never stand alone as the "why" -- if a future
    # edit reintroduces "so" right after it, this is the wrong conclusion
    # the fact alone does not support (round 3 finding).
    assert "does not exist on this die -- so" not in flat
    assert "XIP_SER does not exist on this die, so" not in flat


def test_ospi0_storage_banner_describes_mixed_population_per_device():
    """NOR populated, HyperRAM not -- alp-sdk#2062 review round 2: a naive
    `bool(ospi0.assembled) or bool(hyperram.assembled)` printed "OSPI0 NOR +
    HyperRAM are populated" for this case, false for the HyperRAM half.
    Each device must be described on its own instead of merged into one
    shared verb once they disagree, and (round 4) the XIP sentence must say
    "the populated device is not used" -- not "neither", which implies both
    declared devices agree when only one of the two actually does."""
    with _MutatedMetadata() as mm:
        mm.sub(AEN801_PRESET, _OSPI0_BLOCK,
               "      chip:           TEST-NOR-PART\n"
               "      assembled:      true")
        flat = _flat(_dts(_emit("E1M-AEN801", "m55_hp", mm.root)))
    assert (
        "OSPI0 NOR (TEST-NOR-PART) is populated; HyperRAM "
        "(S80KS5122GABHM02) is not populated; the populated device is "
        "not used for XIP boot here" in flat)
    assert "NOR + HyperRAM are populated" not in flat
    assert "neither is used" not in flat


def test_ospi0_storage_banner_describes_bom_optional():
    """`assembled: "optional"` (the real state of every
    E1M-AEN{301,401,501,601,701} preset) is Python-truthy -- `bool(
    "optional")` -- so a naive check printed "are populated on this SKU"
    for the exact BOM question that field exists to leave open. Mutated off
    E1M-AEN801 because every SKU that carries `"optional"` for real fails
    earlier in `emit_zephyr_board()` and so can't reach this code any other
    way today.

    Round 2 finding: "BOM-optional, not assumed populated" followed by an
    unqualified "there is no external XIP / flash device" contradicts
    itself -- a BOM-optional part MAY be fitted. The sentence must hedge
    with "assumed" on both halves."""
    with _MutatedMetadata() as mm:
        mm.sub(AEN801_PRESET, _OSPI0_BLOCK,
               "      chip:           TEST-NOR-PART\n"
               "      assembled:      optional")
        mm.sub(AEN801_PRESET, _HYPERRAM_BLOCK,
               "    chip:           TEST-RAM-PART\n"
               "    assembled:      optional")
        flat = _flat(_dts(_emit("E1M-AEN801", "m55_hp", mm.root)))
    assert (
        "OSPI0 NOR (TEST-NOR-PART) + HyperRAM (TEST-RAM-PART) are "
        "BOM-optional, not assumed populated, so no external XIP / "
        "flash device is assumed" in flat)
    assert "are populated" not in flat
    assert "there is no external XIP / flash device;" not in flat


def test_ospi0_storage_banner_assembled_key_absent_reads_as_populated():
    """A DECLARED device block with no `assembled:` key at all reads as
    populated (schema default `true`) -- distinct from an entirely ABSENT
    block (next test), which must NOT. Mutation-checked: changing
    `dev.get("assembled", True)` to `dev.get("assembled")` turns this test
    red (the key-absent NOR would read as `not_populated` instead);
    restoring makes it green again."""
    with _MutatedMetadata() as mm:
        mm.sub(AEN801_PRESET, _OSPI0_BLOCK,
               "      chip:           TEST-NOR-PART")
        flat = _flat(_dts(_emit("E1M-AEN801", "m55_hp", mm.root)))
    assert "OSPI0 NOR (TEST-NOR-PART) is populated" in flat


def test_ospi0_storage_banner_chip_tbd_prints_no_part_name():
    """`chip: TBD` on an otherwise-populated device must not print a literal
    "(TBD)" part name. Mutation-checked: deleting the `_is_tbd()` guard
    (`if not chip or _is_tbd(chip): chip = None`) turns this test red
    ("(TBD)" would appear); restoring makes it green again."""
    with _MutatedMetadata() as mm:
        mm.sub(AEN801_PRESET, _OSPI0_BLOCK,
               "      chip:           TBD\n"
               "      assembled:      true")
        flat = _flat(_dts(_emit("E1M-AEN801", "m55_hp", mm.root)))
    assert "OSPI0 NOR is populated" in flat
    assert "(TBD)" not in flat


def test_ospi0_storage_banner_omits_an_undeclared_device_block():
    """The schema only requires `on_module.silicon` -- omitting `hyperram:`
    (or `ospi_memories:`) entirely is valid, and is a DIFFERENT fact from a
    declared-but-key-omitted block: the old code's
    `on_module.get("hyperram") or {}` collapsed both to `{}`, so an
    undeclared HyperRAM hit the same `assembled` default as a
    declared-with-omitted-key one and was printed as "populated" (round 4).
    An undeclared device must be left out of the clause entirely -- never
    named "not populated" either, since there is no declared device to
    describe. Mutation-checked: reverting `hyperram =
    on_module.get("hyperram")` to `... or {}` turns this test red
    ("HyperRAM is populated" would appear); restoring makes it green
    again."""
    with _MutatedMetadata() as mm:
        mm.sub(AEN801_PRESET, _OSPI0_BLOCK,
               "      chip:           TEST-NOR-PART\n"
               "      assembled:      true")
        mm.sub(
            AEN801_PRESET,
            "  # External HyperRAM -- volatile XIP / scratch RAM, "
            "separate from the\n"
            "  # NOR flash above.  Shares the OSPI0 octal controller "
            "with the flash,\n"
            "  # separated only by chip-select (HyperRAM = CS0, NOR = "
            "CS1).\n"
            "  hyperram:\n"
            f"{_HYPERRAM_BLOCK}\n"
            "    capacity_mbit:  512             # 512 Mbit (64 MiB) "
            "-- the part, if fitted\n"
            "    interface:      ospi0\n"
            "    chip_select:    0                 # OSPI0 CS0 -- U9 "
            "-> OSPI0_SS0 per the R2 netlist\n",
            "")
        flat = _flat(_dts(_emit("E1M-AEN801", "m55_hp", mm.root)))
    assert "OSPI0 NOR (TEST-NOR-PART) is populated" in flat
    assert "HyperRAM" not in flat
    assert "not populated" not in flat
