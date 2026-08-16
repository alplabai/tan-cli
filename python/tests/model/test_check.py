# SPDX-License-Identifier: Apache-2.0
"""tan.model.check -- the glue `tan model check` calls (ADR-0028 amendment,
tan-cli#782 Tasks 4/6): which backends a SKU actually declares, and the
opportunistic `--exact` upgrade for ethos_u.

Two test tiers, the same split `test_analyze.py`/`test_targets.py` already
use: format/exact-flag behaviour needs no real alp-sdk checkout (a throwaway
synthetic `e1m_modules/` + `socs/` + `npu_ops/` tree under `tmp_path` is
enough); resolving REAL SKUs (E1M-V2N101, E1M-V2M101, ...) needs the real,
committed metadata and is gated on a bound `ALP_SDK_ROOT` -- skip LOUDLY,
naming the missing var, never a silent pass.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from tan.model import check as check_mod
from tan.model.adapters import Blob
from tan.model.check import (
    _headline_ethos_u_accel_config,
    check_model_backends,
    resolve_check_backends,
)
from tan.model.tensorio import OpDesc
from tests.conftest import sdk_root

SDK = sdk_root()

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "models" / "tiny_int8.tflite"

# Named so a reader who hits the skip knows what to install and why -- not
# just "tflite not installed" (importorskip's own bare default message), but
# which extra actually puts it on PATH. `pip install -e ./python` alone (the
# bare shape `ci.yml`'s `gates` job installs, no extras) never gets this;
# only the real read-a-real-.tflite tests need it.
_MODEL_IO_SKIP_REASON = "tflite reader missing -- pip install alp-tan[model-io] for real .tflite parsing"


def _write_som(meta: Path, sku: str, silicon: str, *, ethos_u_variant: str | None = None) -> None:
    d = meta / "e1m_modules"
    d.mkdir(parents=True, exist_ok=True)
    body = f"silicon: {silicon}\n"
    if ethos_u_variant:
        body += f"inference:\n  ethos_u_variant: {ethos_u_variant}\n"
    (d / f"{sku}.yaml").write_text(body)


def _write_soc(meta: Path, silicon: str, npus: list[dict]) -> None:
    vendor, family, part = silicon.split(":")
    d = meta / "socs" / vendor / family
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{part}.json").write_text(json.dumps({"ref": silicon, "npus": npus}))


def _write_table(meta: Path, backend: str, filename: str, *, variant: str, supported: list[str]) -> None:
    d = meta / "npu_ops" / backend
    d.mkdir(parents=True, exist_ok=True)
    path = d / filename
    path.write_text(json.dumps({
        "applies_to": {"variant": variant, "products": [], "toolchain": "x", "toolchain_version": "1"},
        "op_namespace": "tflite", "authority": "tool-generated", "stance": "screening",
        "provenance": {}, "supported_ops": supported,
    }))


# ---------------------------------------------------------------------------
# resolve_check_backends -- cpu excluded, real board-level errors propagate
# ---------------------------------------------------------------------------

def test_resolve_check_backends_excludes_cpu(tmp_path):
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    assert resolve_check_backends("E1M-FAKE", metadata_root=tmp_path) == ["ethos_u"]


def test_resolve_check_backends_raises_for_an_unresolvable_sku(tmp_path):
    # A board-level fact (no SoM preset at all) -- the caller (model_cmd's
    # `_require_check_backends`) turns this into `model.check-sku-unresolved`
    # for the WHOLE run, never a per-model issue.
    with pytest.raises(FileNotFoundError):
        resolve_check_backends("E1M-DOES-NOT-EXIST", metadata_root=tmp_path)


# ---------------------------------------------------------------------------
# check_model_backends -- one report per declared backend, ops walked once
# ---------------------------------------------------------------------------

def test_check_model_backends_screens_the_real_tflite_fixture(tmp_path):
    # Needs the REAL reader: the assertion depends on _FIXTURE's actual
    # operator content (a single FULLY_CONNECTED op) being extracted by the
    # real `tflite` parser, not a stand-in -- this is the one tier-1 case
    # (tests/model/test_analyze.py's docstring split) that cannot be reworked
    # around the `model-io` extra without testing something else entirely.
    pytest.importorskip("tflite", reason=_MODEL_IO_SKIP_REASON)
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 supported=["FULLY_CONNECTED"])
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=False)
    assert len(reports) == 1
    assert reports[0].backend == "ethos_u"
    assert reports[0].npu_coverage == "full-eligible"
    assert reports[0].basis == "static-screen"


def test_check_model_backends_reports_format_not_accepted_never_cpu_only(tmp_path, monkeypatch):
    # The format gate (analyze.analyze_backend's own documented order: format
    # gate, THEN table resolution, THEN the per-op walk) fires before any op
    # extraction is even consulted for scoring -- so this needs a non-empty
    # *extracted* op list to prove the per-op verdicts it produces, not a real
    # `tflite` parse of a real file. Monkeypatching `extract_ops` (rather than
    # requiring the `model-io` extra) keeps this test meaningful on exactly
    # the bare-install shape `ci.yml`'s `gates` job runs, which is where the
    # format gate matters most (it is the ONLY thing standing between a
    # .tflite source and a wrongly-scored onnx-only backend).
    fake_op = OpDesc(op="FULLY_CONNECTED", op_namespace="tflite")
    monkeypatch.setattr(check_mod, "extract_ops", lambda source, **kw: [fake_op])
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:multi")
    _write_soc(tmp_path, "fake:soc:multi", [{"type": "drp-ai3", "subtype": "drp", "mac_per_cycle": 1}])
    reports = check_model_backends(backends=["drpai"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=False)
    assert reports[0].backend == "drpai"
    assert reports[0].npu_coverage == "undetermined"
    assert reports[0].ops[0].reason == "format-not-accepted"       # .tflite vs drpai's onnx ingest
    assert reports[0].npu_coverage != "cpu-only"


def test_check_model_backends_propagates_an_unreadable_source(tmp_path):
    # check_model_backends itself does not swallow this -- `model_cmd._check_
    # one_model`'s try/except is the seam that turns it into a coded
    # `model.check-failed` issue; the engine layer stays honest about failure.
    # Needs no `tflite` reader at all: `extract_ops` now reads (or fails to
    # read) @source BEFORE it ever checks whether `tflite` is importable
    # (tan.model.tensorio), so a missing source raises OSError on a bare
    # install exactly as it does with the `model-io` extra present.
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    with pytest.raises(OSError):
        check_model_backends(backends=["ethos_u"], sku="E1M-FAKE",
                              source=tmp_path / "missing.tflite", metadata_root=tmp_path, exact=False)


# ---------------------------------------------------------------------------
# MAJOR 2 review: a missing `tflite` reader must not read identically to "this
# model genuinely has no operators" -- both hit extract_ops() -> [], but only
# one of them is fixable by the customer, and only one of them means
# `check` is *actually* inoperative for every .tflite model on this host.
# ---------------------------------------------------------------------------

def test_reader_missing_note_names_the_model_io_extra_not_model_compile(tmp_path, monkeypatch):
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 supported=["FULLY_CONNECTED"])
    # `sys.modules["tflite"] = None` forces `import tflite` to raise ImportError
    # even on a host where the model-io extra IS installed -- the same trick
    # test_tensorio.py's own test_extract_ops_missing_tflite_reader_returns_
    # empty uses for the identical reason.
    monkeypatch.setitem(sys.modules, "tflite", None)
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=False)
    rep = reports[0]
    assert rep.npu_coverage == "undetermined"          # still not a verdict on the model
    assert rep.ops == []
    assert len(rep.notes) == 1
    assert "model-io" in rep.notes[0]
    assert "model-compile" not in rep.notes[0]         # the WRONG extra (--exact's), not this one
    assert "tflite" in rep.notes[0] and "not installed" in rep.notes[0]


def test_a_genuinely_empty_model_keeps_the_generic_note_when_the_reader_is_present(tmp_path):
    # Contrast case: the reader IS available, but the source itself yields no
    # operators (unparseable/empty bytes) -- this must keep _empty_ops_report's
    # ordinary "nothing was extracted" note, not the reader-specific one, since
    # nothing here is actually the reader's fault.
    pytest.importorskip("tflite", reason=_MODEL_IO_SKIP_REASON)
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 supported=["FULLY_CONNECTED"])
    garbage = tmp_path / "garbage.tflite"
    garbage.write_bytes(b"TFL3-NOT-REALLY-GARBAGE-BYTES")
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=garbage,
                                    metadata_root=tmp_path, exact=False)
    rep = reports[0]
    assert rep.npu_coverage == "undetermined"
    assert "model-io" not in rep.notes[0]
    assert "no operators were extracted" in rep.notes[0]


def test_reader_missing_never_flips_format_not_accepted_into_a_reader_note(tmp_path, monkeypatch):
    # A backend that never ingests .tflite at all (drpai) must keep its own
    # honest format-not-accepted note -- the reader-missing swap only applies
    # to a backend whose table actually resolved (report.table is not None).
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:multi")
    _write_soc(tmp_path, "fake:soc:multi", [{"type": "drp-ai3", "subtype": "drp", "mac_per_cycle": 1}])
    monkeypatch.setitem(sys.modules, "tflite", None)
    reports = check_model_backends(backends=["drpai"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=False)
    rep = reports[0]
    assert rep.table is None
    assert rep.npu_coverage == "undetermined"
    assert "does not ingest" in rep.notes[0]        # the ORDINARY format-not-accepted note
    assert "model-io" not in rep.notes[0]


# ---------------------------------------------------------------------------
# --exact: the ethos_u opportunistic upgrade (Task 6)
# ---------------------------------------------------------------------------

def test_exact_degrades_cleanly_and_says_so_when_vela_is_absent(tmp_path, monkeypatch):
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 supported=["FULLY_CONNECTED"])
    monkeypatch.setattr(shutil, "which", lambda name: None)
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=True)
    assert reports[0].basis == "static-screen"          # degraded, not crashed
    assert any(n.startswith("--exact") and "vela is not on PATH" in n for n in reports[0].notes)


def test_exact_runs_the_real_compiler_and_returns_basis_compiled(tmp_path, monkeypatch):
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 supported=["FULLY_CONNECTED"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)

    def _fake_compile(self, source, *, accel_config, out_dir, opts=None):
        assert accel_config == "ethos-u55-256"
        return Blob(format="vela_tflite", payload=b"x", arena_bytes=123,
                    compiler_version="vela 3.9.0", req_sram_kib=4,
                    cpu_op_count=0, npu_op_count=1)

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _fake_compile)
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=True)
    rep = reports[0]
    assert rep.basis == "compiled"
    assert rep.confidence == "certain"
    assert rep.npu_coverage == "fits"          # the ONLY basis allowed to say this
    assert rep.ops == []
    assert rep.compute_on_npu_pct_max == 100.0
    assert "vela 3.9.0" in rep.notes[0]
    assert "123" in rep.notes[0] and "4" in rep.notes[0]


# ---------------------------------------------------------------------------
# --exact BLOCKER regression: vela exits 0 on a full CPU fallback by design
# (tan-cli#782 review) -- "compile() didn't raise" must never read as "fits".
# ---------------------------------------------------------------------------

def test_exact_never_reports_fits_when_vela_places_zero_ops_on_the_npu(tmp_path, monkeypatch):
    # Asserts on report.ops content, which needs a REAL op walk of _FIXTURE
    # (the `tflite` reader, the `model-io` extra) -- the compile itself is
    # monkeypatched, but the KEPT static verdicts underneath it are not.
    pytest.importorskip("tflite", reason=_MODEL_IO_SKIP_REASON)
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 supported=["FULLY_CONNECTED"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)

    def _fake_compile(self, source, *, accel_config, out_dir, opts=None):
        # A clean, zero-exit-code compile that placed NOTHING on the NPU --
        # exactly what vela does for a model it rejects outright (measured:
        # RC=0, "NPU operators = 0 (0.0%)").
        return Blob(format="vela_tflite", payload=b"x", arena_bytes=384,
                    compiler_version="vela 5.1.0", req_sram_kib=0,
                    cpu_op_count=1, npu_op_count=0)

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _fake_compile)
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=True)
    rep = reports[0]
    assert rep.basis == "compiled"                 # a real compile DID run
    assert rep.confidence == "certain"
    assert rep.npu_coverage == "cpu-only"           # never "fits" at 0% placement
    assert rep.npu_coverage != "fits"
    assert rep.compute_on_npu_pct_max == 0.0
    # the static per-op verdicts are KEPT, not thrown away as `ops: []`
    assert rep.ops and rep.ops[0].op == "FULLY_CONNECTED"
    assert "0/1" in rep.notes[0] and "0%" in rep.notes[0]


def test_exact_reports_partial_and_keeps_the_static_per_op_verdicts(tmp_path, monkeypatch):
    pytest.importorskip("tflite", reason=_MODEL_IO_SKIP_REASON)
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 supported=["FULLY_CONNECTED"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)

    def _fake_compile(self, source, *, accel_config, out_dir, opts=None):
        return Blob(format="vela_tflite", payload=b"x", arena_bytes=100,
                    compiler_version="vela 5.1.0", req_sram_kib=1,
                    cpu_op_count=1, npu_op_count=1)

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _fake_compile)
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=True)
    rep = reports[0]
    assert rep.basis == "compiled"
    assert rep.npu_coverage == "partial"
    assert rep.npu_coverage != "fits"
    assert rep.compute_on_npu_pct_max == 50.0
    assert rep.ops and rep.ops[0].op == "FULLY_CONNECTED"     # kept, not discarded
    assert "1/2" in rep.notes[0] and "50%" in rep.notes[0]


def test_exact_degrades_to_the_static_screen_when_placement_cannot_be_parsed(tmp_path, monkeypatch):
    # A compiled Blob with no placement info at all (an unexpected vela output
    # shape) must not be read as "0 CPU ops, so it must be all-NPU" -- it
    # degrades exactly like every other "cannot verify" path in this module.
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 supported=["FULLY_CONNECTED"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)

    def _fake_compile(self, source, *, accel_config, out_dir, opts=None):
        return Blob(format="vela_tflite", payload=b"x", arena_bytes=100,
                    compiler_version="vela 5.1.0", req_sram_kib=1)   # no cpu/npu_op_count

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _fake_compile)
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=True)
    rep = reports[0]
    assert rep.basis == "static-screen"             # degraded, not a fabricated verdict
    assert rep.npu_coverage != "fits"
    assert any(n.startswith("--exact") and "placement" in n and "could not be read" in n
               for n in rep.notes)


def test_exact_real_vela_compile_of_a_model_it_rejects_never_reports_fits(tmp_path):
    """The exact BLOCKER scenario (tan-cli#782 review), driven through a REAL
    `vela` process -- monkeypatching `VelaAdapter.compile` cannot prove this,
    since the bug was trusting a REAL clean exit code as proof of placement.
    `float32_fc.tflite` is unquantized on purpose: vela's Ethos-U backend
    rejects FLOAT32 feature-map tensors outright (Generic constraint), so the
    model's one FULLY_CONNECTED op falls back to the CPU and vela still exits
    0 -- ``ethos-u-vela`` 5.1.0 measured: "NPU operators = 0 (0.0%)"."""
    if shutil.which("vela") is None:
        pytest.skip("vela (ethos-u-vela) is not installed on PATH")
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u85", ethos_u_variant="u85")
    _write_soc(tmp_path, "fake:soc:u85", [{"type": "ethos-u85", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u85@vela-1.0.0.json", variant="u85",
                 supported=["FULLY_CONNECTED"])
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "models" / "float32_fc.tflite"
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=fixture,
                                    metadata_root=tmp_path, exact=True)
    rep = reports[0]
    assert rep.basis == "compiled"                  # the real compile DID run and succeed
    assert rep.npu_coverage != "fits"                # the exact overclaim this fix removes
    assert rep.npu_coverage == "cpu-only"
    assert rep.compute_on_npu_pct_max == 0.0
    assert rep.ops and rep.ops[0].op == "FULLY_CONNECTED"      # kept, not `ops: []`


def test_exact_degrades_on_a_vela_compile_failure(tmp_path, monkeypatch):
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 supported=["FULLY_CONNECTED"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)

    def _boom(self, source, *, accel_config, out_dir, opts=None):
        raise RuntimeError("vela failed for ethos-u55-256: bad shape")

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _boom)
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=True)
    assert reports[0].basis == "static-screen"          # degraded, not crashed
    assert any(n.startswith("--exact") and "vela failed" in n for n in reports[0].notes)


def test_exact_is_unavailable_for_drpai_and_deepx_but_never_crashes(tmp_path):
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:multi")
    _write_soc(tmp_path, "fake:soc:multi", [
        {"type": "drp-ai3", "subtype": "drp", "mac_per_cycle": 1},
        {"type": "dx", "subtype": "npu", "mac_per_cycle": 1},
    ])
    onnx_source = tmp_path / "model.onnx"
    onnx_source.write_bytes(b"not-a-real-onnx-file")          # ops=[] either way (Task 3 scope)
    reports = check_model_backends(backends=["drpai", "deepx_dxm1"], sku="E1M-FAKE",
                                    source=onnx_source, metadata_root=tmp_path, exact=True)
    assert len(reports) == 2
    for rep in reports:
        assert rep.basis == "static-screen"
        assert any(n.startswith("--exact is not available") and "license-gated" in n
                   for n in rep.notes)


def test_headline_accel_config_picks_the_highest_mac_per_cycle_at_the_primary_variant(tmp_path):
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:dual", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:dual", [
        {"type": "ethos-u55", "subtype": "hp", "mac_per_cycle": 256},
        {"type": "ethos-u55", "subtype": "he", "mac_per_cycle": 128},
    ])
    assert _headline_ethos_u_accel_config("E1M-FAKE", tmp_path) == "ethos-u55-256"


def test_headline_accel_config_is_none_without_an_ethos_u_variant(tmp_path):
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:multi")
    _write_soc(tmp_path, "fake:soc:multi", [{"type": "drp-ai3", "subtype": "drp", "mac_per_cycle": 1}])
    assert _headline_ethos_u_accel_config("E1M-FAKE", tmp_path) is None


# ---------------------------------------------------------------------------
# Real alp-sdk metadata (ALP_SDK_ROOT-gated)
# ---------------------------------------------------------------------------

pytestmark_real_sdk = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- resolve_check_backends needs the real, committed "
           "metadata/e1m_modules/*.yaml and metadata/socs/** content.",
)
_META = SDK / "metadata" if SDK is not None else None


@pytestmark_real_sdk
def test_real_aen501_resolves_only_ethos_u():
    assert resolve_check_backends("E1M-AEN501", metadata_root=_META) == ["ethos_u"]


@pytestmark_real_sdk
def test_real_v2m101_resolves_both_onnx_backends():
    backends = resolve_check_backends("E1M-V2M101", metadata_root=_META)
    assert set(backends) >= {"drpai", "deepx_dxm1"}
    assert "ethos_u" not in backends


@pytestmark_real_sdk
@pytest.mark.parametrize("sku", ["E1M-V2N101", "E1M-V2M101"])
def test_a_tflite_model_against_a_real_v2n_v2m_sku_reports_onnx_backends_undetermined(sku):
    """The exact scenario tan-cli#782 calls out: a .tflite model against a
    V2N/V2M SKU must report the SKU's real (ONNX-ingesting) backends as
    `undetermined` + `format-not-accepted` -- never `cpu-only`, which would
    read as "this model won't run on this board" rather than "wrong format
    to screen it in"."""
    backends = resolve_check_backends(sku, metadata_root=_META)
    assert backends            # at least drpai; V2M also carries deepx_dxm1
    reports = check_model_backends(backends=backends, sku=sku, source=_FIXTURE,
                                    metadata_root=_META, exact=False)
    for rep in reports:
        assert rep.npu_coverage == "undetermined"
        assert rep.ops and rep.ops[0].reason == "format-not-accepted"
        assert rep.npu_coverage != "cpu-only"
