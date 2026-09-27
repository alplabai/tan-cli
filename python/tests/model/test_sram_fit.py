# SPDX-License-Identifier: Apache-2.0
"""tan.model.sram_fit -- the arena + SRAM0 residency check for a `Sram_Only`
ethos_u compile (tan-cli#1288).

Hermetic throughout: a synthetic `e1m_modules/` + `socs/` tree under
`tmp_path`, the same trick `test_check.py`/`test_build.py` use, no
`ALP_SDK_ROOT` required. `VelaAdapter.compile` is monkeypatched so the
build/check integration tests exercise the REAL `build_model`/
`check_model_backends` glue around a FAKE compile result -- the numbers in
the issue (arena 72 vs 64/128, blob 263/5000 KiB vs SRAM0 4096) are exact
inputs a real vela process would be expensive and non-deterministic to
reproduce on demand.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tan.model import check as check_mod
from tan.model.adapters import Blob
from tan.model.build import SramNoFitRefused, build_model
from tan.model.check import check_model_backends
from tan.model.sram_fit import (
    DEFAULT_ARENA_KIB,
    FIT_UNVERIFIED,
    NO_FIT,
    SKIPPED,
    evaluate_sram_fit,
    resolve_arena_budget,
    resolve_sram0_kib,
)

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "models" / "tiny_int8.tflite"


def _write_som_preset(meta: Path, sku: str, silicon: str, *,
                      ethos_u_variant: str | None = None) -> None:
    """A `schema_version: 1` SoM preset -- required by
    `tan.commands.build_output.read_sdk_som_and_soc` (via `_read_som_preset`),
    which `resolve_sram0_kib` reuses. Deliberately NOT `test_check.py`'s own
    `_write_som` helper: that one never writes `schema_version`, which is
    fine for `resolve_targets`/`check_model_backends` (they never read it)
    but makes `read_sdk_som_and_soc` report "unreadable" every time --
    exactly the `SKIPPED` shape this file tests separately, below.

    @ethos_u_variant, when given, also writes `inference.ethos_u_variant` --
    needed by `_headline_ethos_u_target`/`resolve_ethos_u_variant` for the
    `check_model_backends(exact=True)` tests below to reach a real compile at
    all (without it `--exact` degrades to the static screen before ever
    calling `VelaAdapter.compile`)."""
    d = meta / "e1m_modules"
    d.mkdir(parents=True, exist_ok=True)
    body = f"schema_version: 1\nsilicon: {silicon}\n"
    if ethos_u_variant:
        body += f"inference:\n  ethos_u_variant: {ethos_u_variant}\n"
    (d / f"{sku}.yaml").write_text(body, encoding="utf-8")


def _write_soc_with_variant(meta: Path, silicon: str, npus: list[dict], *, sku: str,
                            sram_banks_kb: dict, memory_mode: str = "Sram_Only") -> None:
    vendor, family, part = silicon.split(":")
    d = meta / "socs" / vendor / family
    d.mkdir(parents=True, exist_ok=True)
    spec = {
        "ref": silicon,
        "npus": npus,
        "npu_toolchain": {"vela": {"memory_mode": memory_mode}},
        "variants": [{"order_code": "X", "alp_module_skus": [sku],
                      "sram_banks_kb": sram_banks_kb}],
    }
    (d / f"{part}.json").write_text(json.dumps(spec), encoding="utf-8")


def _write_table(meta: Path, backend: str, filename: str, *, variant: str, supported: list[str]) -> None:
    d = meta / "npu_ops" / backend
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(json.dumps({
        "applies_to": {"variant": variant, "products": [], "toolchain": "x", "toolchain_version": "1"},
        "op_namespace": "tflite", "authority": "tool-generated", "stance": "screening",
        "provenance": {}, "supported_ops": supported,
    }))


# ---------------------------------------------------------------------------
# resolve_arena_budget -- pure. tan-cli#1288 review (HIGH/MEDIUM): the E8/E6/
# E4 SoC specs declare NO `paired_core` for their own Ethos-U85 -- the
# flagship SKU's own headline `--exact`/build target -- so a resolver that
# defaulted straight to 128 on `paired_core is None` silently ignored the
# board's real `cores.m55_hp.inference.default_arena_kib` override on every
# real AEN SoM. These tests pin the CORRECTED scoping rules instead.
# ---------------------------------------------------------------------------

def test_a_declared_paired_core_is_single_and_certain_even_with_no_override():
    # Rule 1: the core key EXISTS under `cores:` -- that alone is what makes
    # it certain, whether or not it bothers to tune `default_arena_kib`.
    board_doc = {"cores": {"m55_hp": {"inference": {"default_arena_kib": 64}}}}
    budget = resolve_arena_budget(board_doc, "m55_hp")
    assert budget.kind == "single"
    assert budget.single_kib == 64
    assert budget.cores == ("m55_hp",)


def test_a_declared_paired_core_with_no_inference_block_defaults_but_stays_certain():
    board_doc = {"cores": {"m55_hp": {}}}
    budget = resolve_arena_budget(board_doc, "m55_hp")
    assert budget.kind == "single"
    assert budget.single_kib == DEFAULT_ARENA_KIB == 128  # board.schema.json's own default
    assert budget.cores == ("m55_hp",)


def test_a_paired_core_absent_from_board_yaml_is_unresolved_not_a_128_guess():
    """Rule 2/review MEDIUM: a paired core the board never declares at all
    (E1M-AEN801's own `m55_he`, undeclared in its board.yaml template) must
    never default to 128 and must never be treated as certain enough to
    refuse a build."""
    board_doc = {"cores": {"m55_hp": {"inference": {"default_arena_kib": 64}}}}
    budget = resolve_arena_budget(board_doc, "m55_he")
    assert budget.kind == "unresolved"
    assert "m55_he" in budget.reason and "not declared" in budget.reason


def test_an_unpaired_npu_with_exactly_one_inference_core_is_single_and_certain():
    """Rule 1's `paired_core is None` branch, the ordinary AEN801 shape: the
    Ethos-U85 pairs to no core of its own, but exactly one board core
    (`m55_hp`) declares an `inference:` block at all -- that one core is used,
    CERTAIN, not the bare schema default. THIS is the exact bug tan-cli#1288
    review found: before the fix, `paired_core is None` fell straight to 128
    and silently ignored `m55_hp`'s own 64 KiB override."""
    board_doc = {"cores": {"m55_hp": {"inference": {"default_arena_kib": 64}},
                            "a32_cluster": {"os": "off"}}}
    budget = resolve_arena_budget(board_doc, None)
    assert budget.kind == "single"
    assert budget.single_kib == 64
    assert budget.cores == ("m55_hp",)


def test_an_unpaired_npu_with_several_inference_cores_is_a_range():
    board_doc = {"cores": {
        "m55_hp": {"inference": {"default_arena_kib": 64}},
        "m55_he": {"inference": {"default_arena_kib": 32}},
    }}
    budget = resolve_arena_budget(board_doc, None)
    assert budget.kind == "range"
    assert budget.min_kib == 32 and budget.max_kib == 64
    assert budget.cores == ("m55_he", "m55_hp")


def test_an_unpaired_npu_with_no_inference_core_at_all_is_unresolved():
    board_doc = {"cores": {"a32_cluster": {"os": "off"}}}
    budget = resolve_arena_budget(board_doc, None)
    assert budget.kind == "unresolved"
    assert "declares no core running an inference workload" in budget.reason


def test_resolve_arena_budget_is_unresolved_when_board_doc_is_absent():
    budget = resolve_arena_budget(None, "m55_hp")
    assert budget.kind == "unresolved"
    assert "no board.yaml" in budget.reason
    budget_unpaired = resolve_arena_budget(None, None)
    assert budget_unpaired.kind == "unresolved"


def test_resolve_arena_budget_ignores_a_malformed_bool_value():
    # bool is an int subclass in Python -- `default_arena_kib: true` is not a
    # KiB count, so the declared core still defaults, but stays CERTAIN.
    board_doc = {"cores": {"m55_hp": {"inference": {"default_arena_kib": True}}}}
    budget = resolve_arena_budget(board_doc, "m55_hp")
    assert budget.kind == "single"
    assert budget.single_kib == DEFAULT_ARENA_KIB


def test_the_e8_shaped_u85_yields_a_certain_no_fit_at_arena_72_vs_m55_hp_64():
    """The exact scenario tan-cli#1288 review asked to be pinned: the E8's
    own Ethos-U85 (`paired_core: None`, real metadata shape) on the AEN801
    board.yaml template (`cores.m55_hp.inference.default_arena_kib: 64`,
    the ONLY inference-declaring core) -- `m55_hp` is used as the sole
    inference core, CERTAIN, and 72 KiB needed against a 64 KiB budget is a
    real `NO_FIT`, not a false pass against the bare schema default."""
    board_doc = {"cores": {"a32_cluster": {"os": "off"},
                           "m55_hp": {"app": "./src", "inference": {"default_arena_kib": 64}}}}
    fit = evaluate_sram_fit(
        memory_mode="Sram_Only", req_sram_kib=72, blob_len_bytes=0,
        board_doc=board_doc, paired_core=None,  # the E8's own Ethos-U85 shape
        sku="E1M-FAKE", metadata_root=Path("/nonexistent"),
    )
    assert fit.arena.verdict == NO_FIT
    assert fit.arena.needed_kib == 72 and fit.arena.limit_kib == 64
    assert fit.no_fit is True


# ---------------------------------------------------------------------------
# resolve_sram0_kib -- real filesystem reads, synthetic tree
# ---------------------------------------------------------------------------

def test_resolve_sram0_kib_reads_the_resolved_variant(tmp_path):
    _write_som_preset(tmp_path, "E1M-FAKE", "fake:soc:e8")
    _write_soc_with_variant(tmp_path, "fake:soc:e8", [], sku="E1M-FAKE",
                            sram_banks_kb={"SRAM0": 4096, "SRAM1": 4096})
    kib, reason = resolve_sram0_kib("E1M-FAKE", tmp_path)
    assert kib == 4096
    assert reason is None


def test_resolve_sram0_kib_skips_on_an_unreadable_som_preset(tmp_path):
    # No `schema_version: 1` at all -- exactly what `test_check.py`'s own
    # `_write_som` writes, and exactly what `read_sdk_som_and_soc` refuses.
    d = tmp_path / "e1m_modules"
    d.mkdir(parents=True)
    (d / "E1M-FAKE.yaml").write_text("silicon: fake:soc:e8\n", encoding="utf-8")
    kib, reason = resolve_sram0_kib("E1M-FAKE", tmp_path)
    assert kib is None
    assert "unreadable" in reason


def test_resolve_sram0_kib_skips_when_no_variant_resolves(tmp_path):
    _write_som_preset(tmp_path, "E1M-FAKE", "fake:soc:e8")
    vendor_dir = tmp_path / "socs" / "fake" / "soc"
    vendor_dir.mkdir(parents=True)
    (vendor_dir / "e8.json").write_text(json.dumps({"ref": "fake:soc:e8", "variants": []}),
                                        encoding="utf-8")
    kib, reason = resolve_sram0_kib("E1M-FAKE", tmp_path)
    assert kib is None
    assert "no SoC variant" in reason


def test_resolve_sram0_kib_skips_when_the_variant_declares_no_sram0(tmp_path):
    _write_som_preset(tmp_path, "E1M-FAKE", "fake:soc:e8")
    _write_soc_with_variant(tmp_path, "fake:soc:e8", [], sku="E1M-FAKE",
                            sram_banks_kb={"SRAM1": 4096})
    kib, reason = resolve_sram0_kib("E1M-FAKE", tmp_path)
    assert kib is None
    assert "no SRAM0 bank" in reason


# ---------------------------------------------------------------------------
# evaluate_sram_fit -- pure decision, exact numbers from the issue
# ---------------------------------------------------------------------------

def test_arena_72_vs_default_arena_kib_64_is_a_no_fit(tmp_path):
    _write_som_preset(tmp_path, "E1M-FAKE", "fake:soc:e8")
    _write_soc_with_variant(tmp_path, "fake:soc:e8", [], sku="E1M-FAKE",
                            sram_banks_kb={"SRAM0": 4096})
    fit = evaluate_sram_fit(
        memory_mode="Sram_Only", req_sram_kib=72, blob_len_bytes=0,
        board_doc={"cores": {"m55_hp": {"inference": {"default_arena_kib": 64}}}},
        paired_core="m55_hp", sku="E1M-FAKE", metadata_root=tmp_path,
    )
    assert fit.arena.verdict == NO_FIT
    assert fit.arena.needed_kib == 72 and fit.arena.limit_kib == 64
    assert fit.no_fit is True


def test_arena_72_vs_128_default_passes_unverified(tmp_path):
    _write_som_preset(tmp_path, "E1M-FAKE", "fake:soc:e8")
    _write_soc_with_variant(tmp_path, "fake:soc:e8", [], sku="E1M-FAKE",
                            sram_banks_kb={"SRAM0": 4096})
    # `m55_hp` is DECLARED (rule 1) but tunes no override -- 128 default,
    # still CERTAIN (unlike a bare board_doc=None, which is unresolved/
    # skipped -- see test_resolve_arena_budget_is_unresolved_when_board_doc_
    # is_absent above).
    fit = evaluate_sram_fit(
        memory_mode="Sram_Only", req_sram_kib=72, blob_len_bytes=0,
        board_doc={"cores": {"m55_hp": {}}}, paired_core="m55_hp",
        sku="E1M-FAKE", metadata_root=tmp_path,
    )
    assert fit.arena.verdict == FIT_UNVERIFIED
    assert fit.arena.limit_kib == 128
    assert fit.arena.reason == "board arena budget not verified against firmware"
    assert fit.no_fit is False


#: Both SRAM0 scenarios below reuse this SAME board-declared arena budget
#: (72 KiB) -- `arena_kib` in `SramFit.sram0.needed_kib` is the BOARD's
#: declared/default budget, never the model's own `req_sram_kib` (the issue's
#: own "blob 263 KiB + arena 72" framing: `arena_kib` is a fixed board fact
#: the blob's own size is compared alongside, not the per-model arena need).
_BOARD_ARENA_72 = {"cores": {"m55_hp": {"inference": {"default_arena_kib": 72}}}}


def test_blob_263kib_plus_arena_72_vs_sram0_4096_passes_unverified(tmp_path):
    _write_som_preset(tmp_path, "E1M-FAKE", "fake:soc:e8")
    _write_soc_with_variant(tmp_path, "fake:soc:e8", [], sku="E1M-FAKE",
                            sram_banks_kb={"SRAM0": 4096})
    fit = evaluate_sram_fit(
        memory_mode="Sram_Only", req_sram_kib=72, blob_len_bytes=263 * 1024,
        board_doc=_BOARD_ARENA_72, paired_core="m55_hp", sku="E1M-FAKE",
        metadata_root=tmp_path,
    )
    assert fit.blob_kib == 263
    assert fit.sram0.verdict == FIT_UNVERIFIED
    assert fit.sram0.needed_kib == 335 and fit.sram0.limit_kib == 4096
    assert "firmware's own SRAM0 use not checked" in fit.sram0.reason
    assert fit.no_fit is False


def test_blob_5000kib_is_a_no_fit_against_sram0_4096(tmp_path):
    _write_som_preset(tmp_path, "E1M-FAKE", "fake:soc:e8")
    _write_soc_with_variant(tmp_path, "fake:soc:e8", [], sku="E1M-FAKE",
                            sram_banks_kb={"SRAM0": 4096})
    fit = evaluate_sram_fit(
        memory_mode="Sram_Only", req_sram_kib=72, blob_len_bytes=5000 * 1024,
        board_doc=_BOARD_ARENA_72, paired_core="m55_hp", sku="E1M-FAKE",
        metadata_root=tmp_path,
    )
    assert fit.sram0.verdict == NO_FIT
    assert fit.sram0.needed_kib == 5072 and fit.sram0.limit_kib == 4096
    assert fit.no_fit is True


def test_non_sram_only_mode_skips_both_checks(tmp_path):
    fit = evaluate_sram_fit(
        memory_mode="Shared_Sram", req_sram_kib=999999, blob_len_bytes=999999 * 1024,
        board_doc=None, paired_core="m55_hp", sku="E1M-FAKE", metadata_root=tmp_path,
    )
    assert fit.arena.verdict == SKIPPED
    assert fit.sram0.verdict == SKIPPED
    assert fit.no_fit is False
    assert "Sram_Only" in fit.arena.reason


def test_no_axis_ever_reports_a_bare_fits(tmp_path):
    _write_som_preset(tmp_path, "E1M-FAKE", "fake:soc:e8")
    _write_soc_with_variant(tmp_path, "fake:soc:e8", [], sku="E1M-FAKE",
                            sram_banks_kb={"SRAM0": 4096})
    fit = evaluate_sram_fit(
        memory_mode="Sram_Only", req_sram_kib=1, blob_len_bytes=1024,
        board_doc=_BOARD_ARENA_72, paired_core="m55_hp", sku="E1M-FAKE", metadata_root=tmp_path,
    )
    assert fit.arena.verdict != "fits"
    assert fit.sram0.verdict != "fits"
    assert {fit.arena.verdict, fit.sram0.verdict} <= {FIT_UNVERIFIED, NO_FIT, SKIPPED}


def test_an_undeclared_paired_core_is_skipped_never_a_certain_no_fit(tmp_path):
    """tan-cli#1288 review MEDIUM: a target whose paired core the board never
    declares (E1M-AEN801's own `m55_he`, undeclared in its template) must
    never refuse -- even a wildly over-budget `req_sram_kib` skips, with a
    reason, rather than comparing against an invented number."""
    _write_som_preset(tmp_path, "E1M-FAKE", "fake:soc:e8")
    _write_soc_with_variant(tmp_path, "fake:soc:e8", [], sku="E1M-FAKE",
                            sram_banks_kb={"SRAM0": 4096})
    fit = evaluate_sram_fit(
        memory_mode="Sram_Only", req_sram_kib=999999, blob_len_bytes=0,
        board_doc={"cores": {"m55_hp": {"inference": {"default_arena_kib": 64}}}},
        paired_core="m55_he", sku="E1M-FAKE", metadata_root=tmp_path,
    )
    assert fit.arena.verdict == SKIPPED
    assert "m55_he" in fit.arena.reason and "not declared" in fit.arena.reason
    assert fit.sram0.verdict == SKIPPED
    assert fit.no_fit is False


@pytest.mark.parametrize(
    "req_sram_kib, expected_verdict",
    [(20, FIT_UNVERIFIED),   # <= min (32) -- fits every declared core's budget
     (48, SKIPPED),          # strictly between min (32) and max (64) -- ambiguous
     (65, NO_FIT)],          # > max (64) -- fails even the most generous candidate
)
def test_an_ambiguous_multi_core_range_gives_the_three_documented_outcomes(
        tmp_path, req_sram_kib, expected_verdict):
    """Rule 1's `"range"` shape: paired_core is None and MORE THAN ONE board
    core declares an `inference:` block (32 and 64 KiB here) -- nothing
    sourced picks between them, so only the two bounds themselves ever earn
    an assertive word."""
    _write_som_preset(tmp_path, "E1M-FAKE", "fake:soc:e8")
    _write_soc_with_variant(tmp_path, "fake:soc:e8", [], sku="E1M-FAKE",
                            sram_banks_kb={"SRAM0": 4096})
    board_doc = {"cores": {
        "m55_hp": {"inference": {"default_arena_kib": 64}},
        "m55_he": {"inference": {"default_arena_kib": 32}},
    }}
    fit = evaluate_sram_fit(
        memory_mode="Sram_Only", req_sram_kib=req_sram_kib, blob_len_bytes=0,
        board_doc=board_doc, paired_core=None, sku="E1M-FAKE", metadata_root=tmp_path,
    )
    assert fit.arena.verdict == expected_verdict
    assert fit.arena.verdict != "fits"
    # The SRAM0 axis always uses the MIN (32) regardless of the arena verdict
    # -- an independent certain-lower-bound proof (see `_evaluate_sram0`).
    assert fit.sram0.needed_kib == 32  # blob_kib=0 + min arena 32


# ---------------------------------------------------------------------------
# build_model() integration -- a certain no-fit refuses the WHOLE build
# ---------------------------------------------------------------------------

def _fake_compile_factory(*, arena_bytes: int, req_sram_kib: int, blob_len: int):
    def _fake_compile(self, source, *, accel_config, out_dir, opts=None,
                      vela_memory_mode=None, vela_system_config=None,
                      vela_vendor_system_config=None,
                      vela_vendor_config_filename=None, soc_declares_dram=None):
        return Blob(format="vela_tflite", payload=b"x" * blob_len, arena_bytes=arena_bytes,
                    compiler_version="vela 5.1.0", req_sram_kib=req_sram_kib,
                    cpu_op_count=0, npu_op_count=1)
    return _fake_compile


def test_build_model_refuses_the_whole_model_on_a_certain_no_fit(tmp_path, monkeypatch):
    _write_som_preset(tmp_path, "E1M-FAKE", "fake:soc:e8")
    _write_soc_with_variant(
        tmp_path, "fake:soc:e8",
        [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256, "paired_core": "m55_hp"}],
        sku="E1M-FAKE", sram_banks_kb={"SRAM0": 4096})
    from tan.model.adapters.ethos_u import VelaAdapter as _RealVelaAdapter
    monkeypatch.setattr(_RealVelaAdapter, "compile",
                        _fake_compile_factory(arena_bytes=73728, req_sram_kib=72,
                                               blob_len=5000 * 1024))
    monkeypatch.setattr(_RealVelaAdapter, "is_available", lambda self: True)

    board_doc = _BOARD_ARENA_72
    src = tmp_path / "tiny.tflite"
    shutil.copy(_FIXTURE, src)
    with pytest.raises(SramNoFitRefused) as exc:
        build_model(sku="E1M-FAKE", name="tiny", source=src, out_dir=tmp_path,
                   metadata_root=tmp_path, adapters=[_RealVelaAdapter()],
                   board_doc=board_doc)
    assert "SRAM0 needs 5072 KiB" in str(exc.value)
    assert not list(tmp_path.glob("*.alpmodel"))          # no partial package


def test_build_model_ships_when_the_blob_fits(tmp_path, monkeypatch):
    _write_som_preset(tmp_path, "E1M-FAKE", "fake:soc:e8")
    _write_soc_with_variant(
        tmp_path, "fake:soc:e8",
        [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256, "paired_core": "m55_hp"}],
        sku="E1M-FAKE", sram_banks_kb={"SRAM0": 4096})
    from tan.model.adapters.ethos_u import VelaAdapter as _RealVelaAdapter
    monkeypatch.setattr(_RealVelaAdapter, "compile",
                        _fake_compile_factory(arena_bytes=73728, req_sram_kib=72,
                                               blob_len=263 * 1024))
    monkeypatch.setattr(_RealVelaAdapter, "is_available", lambda self: True)

    board_doc = {"cores": {"m55_hp": {"inference": {"default_arena_kib": 128}}}}
    src = tmp_path / "tiny.tflite"
    shutil.copy(_FIXTURE, src)
    out = build_model(sku="E1M-FAKE", name="tiny", source=src, out_dir=tmp_path,
                      metadata_root=tmp_path, adapters=[_RealVelaAdapter()],
                      board_doc=board_doc)
    assert out.is_file()


def test_build_model_keeps_a_target_whose_paired_core_is_undeclared(tmp_path, monkeypatch):
    """tan-cli#1288 review MEDIUM: an undeclared-paired-core SKIPPED verdict
    must never refuse the whole model -- even a wildly over-budget
    `req_sram_kib` (72 KiB against a board that never declares `m55_he` at
    all) ships fine, exactly like a non-`Sram_Only` target would."""
    _write_som_preset(tmp_path, "E1M-FAKE", "fake:soc:e8")
    _write_soc_with_variant(
        tmp_path, "fake:soc:e8",
        [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256, "paired_core": "m55_he"}],
        sku="E1M-FAKE", sram_banks_kb={"SRAM0": 4096})
    from tan.model.adapters.ethos_u import VelaAdapter as _RealVelaAdapter
    monkeypatch.setattr(_RealVelaAdapter, "compile",
                        _fake_compile_factory(arena_bytes=73728, req_sram_kib=72,
                                               blob_len=5000 * 1024))
    monkeypatch.setattr(_RealVelaAdapter, "is_available", lambda self: True)

    # The board declares `m55_hp` only -- `m55_he` (this target's own
    # `paired_core`) is undeclared, exactly the AEN801-template shape.
    board_doc = {"cores": {"m55_hp": {"inference": {"default_arena_kib": 64}}}}
    src = tmp_path / "tiny.tflite"
    shutil.copy(_FIXTURE, src)
    out = build_model(sku="E1M-FAKE", name="tiny", source=src, out_dir=tmp_path,
                      metadata_root=tmp_path, adapters=[_RealVelaAdapter()],
                      board_doc=board_doc)
    assert out.is_file()


# ---------------------------------------------------------------------------
# check_model_backends() integration -- --exact attaches sramFit
# ---------------------------------------------------------------------------

def test_check_exact_attaches_a_no_fit_sram_fit(tmp_path, monkeypatch):
    _write_som_preset(tmp_path, "E1M-FAKE", "fake:soc:e8", ethos_u_variant="u55")
    _write_soc_with_variant(
        tmp_path, "fake:soc:e8",
        [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256, "paired_core": "m55_hp"}],
        sku="E1M-FAKE", sram_banks_kb={"SRAM0": 4096})
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                supported=["FULLY_CONNECTED"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)
    monkeypatch.setattr(check_mod.VelaAdapter, "compile",
                        _fake_compile_factory(arena_bytes=73728, req_sram_kib=72,
                                               blob_len=5000 * 1024))

    board_doc = {"cores": {"m55_hp": {"inference": {"default_arena_kib": 128}}}}
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=True, board_doc=board_doc)
    rep = reports[0]
    assert rep.basis == "compiled"
    assert rep.sram_fit is not None
    assert rep.sram_fit.no_fit is True
    assert rep.sram_fit.sram0.verdict == NO_FIT


def test_check_exact_attaches_a_passing_sram_fit(tmp_path, monkeypatch):
    _write_som_preset(tmp_path, "E1M-FAKE", "fake:soc:e8", ethos_u_variant="u55")
    _write_soc_with_variant(
        tmp_path, "fake:soc:e8",
        [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256, "paired_core": "m55_hp"}],
        sku="E1M-FAKE", sram_banks_kb={"SRAM0": 4096})
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                supported=["FULLY_CONNECTED"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)
    monkeypatch.setattr(check_mod.VelaAdapter, "compile",
                        _fake_compile_factory(arena_bytes=73728, req_sram_kib=72,
                                               blob_len=263 * 1024))

    board_doc = {"cores": {"m55_hp": {"inference": {"default_arena_kib": 128}}}}
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=True, board_doc=board_doc)
    rep = reports[0]
    assert rep.sram_fit is not None
    assert rep.sram_fit.no_fit is False
    assert rep.sram_fit.arena.verdict == FIT_UNVERIFIED
    assert rep.sram_fit.sram0.verdict == FIT_UNVERIFIED


def test_static_screen_carries_no_sram_fit_at_all(tmp_path):
    _write_som_preset(tmp_path, "E1M-FAKE", "fake:soc:e8")
    _write_soc_with_variant(
        tmp_path, "fake:soc:e8",
        [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256, "paired_core": "m55_hp"}],
        sku="E1M-FAKE", sram_banks_kb={"SRAM0": 4096})
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                supported=["FULLY_CONNECTED"])
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=False)
    assert reports[0].basis == "static-screen"
    assert reports[0].sram_fit is None
