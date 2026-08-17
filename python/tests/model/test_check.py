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

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from tan.model import analyze as analyze_mod
from tan.model import check as check_mod
from tan.model import perf_apply as perf_apply_mod
from tan.model.adapters import Blob
from tan.model.adapters.ethos_u import VelaFootprintRefused, _refuse_zero_sram_footprint
from tan.model.analyze import BackendReport, OpVerdict
from tan.model.check import (
    _headline_ethos_u_target,
    check_model_backends,
    resolve_check_backends,
)
from tan.model.perf import coverage_from_placement
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


#: Every core name a perf-point test in this file writes onto a point's
#: `core`, by default -- so `_write_som` (below) can declare a synthetic
#: `topology:` block covering all of them without every one of the ~30
#: pre-existing call sites having to say so. The topology-existence check
#: (`tan.model.perf_apply._topology_core_ids`, tan-cli#791 round-2 review
#: item 1) refuses a point whose core is not one @sku's OWN preset declares
#: in `topology:` -- so a SoM preset test-fixture with no `topology:` at all
#: would refuse EVERY perf point these tests write, incidentally breaking the
#: paired-core/profile/hw_rev narrowing these tests actually exist to cover.
#: Tests that exercise the topology check ITSELF (below) pass an explicit,
#: narrower `topology=` instead of relying on this default.
_DEFAULT_TEST_TOPOLOGY = ("m55_hp", "m55_he", "a32_cluster")


def _write_som(meta: Path, sku: str, silicon: str, *, ethos_u_variant: str | None = None,
               default_hw_rev: str | None = None,
               topology: tuple[str, ...] | None = _DEFAULT_TEST_TOPOLOGY) -> None:
    d = meta / "e1m_modules"
    d.mkdir(parents=True, exist_ok=True)
    body = f"silicon: {silicon}\n"
    if default_hw_rev:
        body += f"default_hw_rev: {default_hw_rev}\n"
    if ethos_u_variant:
        body += f"inference:\n  ethos_u_variant: {ethos_u_variant}\n"
    if topology:
        body += "topology:\n" + "".join(f"  {core}: {{}}\n" for core in topology)
    (d / f"{sku}.yaml").write_text(body)


def _write_soc(meta: Path, silicon: str, npus: list[dict], *, extra: dict | None = None) -> None:
    """@extra merges top-level SoC-spec keys in -- `npu_toolchain`,
    `external_memory_interfaces` -- for the tests that need `resolve_targets`
    to resolve a real vela profile or DRAM answer out of this tree rather than
    the all-None a bare spec yields."""
    vendor, family, part = silicon.split(":")
    d = meta / "socs" / vendor / family
    d.mkdir(parents=True, exist_ok=True)
    spec: dict = {"ref": silicon, "npus": npus}
    spec.update(extra or {})
    (d / f"{part}.json").write_text(json.dumps(spec))


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


def test_check_model_backends_propagates_an_unreadable_onnx_source(tmp_path):
    # The .onnx twin of the test above (MAJOR-1 review): the suffix
    # short-circuit in tensorio.extract_ops used to return `[]` for ANY
    # non-.tflite source before ever touching the filesystem, so a
    # board.yaml naming a nonexistent .onnx source on a drpai/deepx_dxm1 SKU
    # silently reported `"ok":true` with a verdict-shaped "nothing to score"
    # note instead of `model.check-failed` -- on exactly the ONNX-ingesting
    # backends that are the V2N/V2M headline. extract_ops now reads @source
    # before any suffix check, so this raises identically to the .tflite case.
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:multi")
    _write_soc(tmp_path, "fake:soc:multi", [{"type": "drp-ai3", "subtype": "drp", "mac_per_cycle": 1}])
    with pytest.raises(OSError):
        check_model_backends(backends=["drpai"], sku="E1M-FAKE",
                              source=tmp_path / "missing.onnx", metadata_root=tmp_path, exact=False)


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

    def _fake_compile(self, source, *, accel_config, out_dir, opts=None,
                      vela_memory_mode=None, vela_system_config=None,
                      vela_vendor_system_config=None,
                      vela_vendor_config_filename=None, soc_declares_dram=None):
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
    assert rep.compute_on_npu_pct_max is None  # MAC-weighted field: never set at basis "compiled"
    assert rep.npu_placement_pct_real == 100.0 # the real, op-count placement instead
    assert "vela 3.9.0" in rep.notes[0]
    assert "123" in rep.notes[0] and "4" in rep.notes[0]
    assert len(rep.notes) == 1                 # no caveats on this Blob, none invented


def test_exact_surfaces_the_compilers_own_caveats_into_the_report(tmp_path, monkeypatch):
    """tan-cli#789: vela compiles against its own BUILT-IN default profile
    (no `--system-config`/`--memory-mode` is passed, and none can be -- the
    SoM-authoritative one lives in a proprietary .ini alp-sdk does not
    redistribute), and that default is DRAM-backed on a module that has no
    DRAM. The adapter's caveat must reach the report, or a customer reads a
    default-profile compile as an authoritative one. `basis`/`npu_coverage`
    are untouched by it: it is a caveat about the figures, not a verdict."""
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 supported=["FULLY_CONNECTED"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)
    caveat = ("vela used its BUILT-IN default profile (system-config "
              "Ethos_U85_SYS_DRAM_Mid, memory-mode Dedicated_Sram_384KB) ...")

    def _fake_compile(self, source, *, accel_config, out_dir, opts=None,
                      vela_memory_mode=None, vela_system_config=None,
                      vela_vendor_system_config=None,
                      vela_vendor_config_filename=None, soc_declares_dram=None):
        return Blob(format="vela_tflite", payload=b"x", arena_bytes=123,
                    compiler_version="vela 5.1.0", req_sram_kib=4,
                    cpu_op_count=0, npu_op_count=1, caveats=(caveat,))

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _fake_compile)
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=True)
    rep = reports[0]
    assert rep.basis == "compiled" and rep.npu_coverage == "fits"
    assert rep.notes[-1] == caveat              # carried verbatim, never rewritten
    assert "vela 5.1.0 compiled for" in rep.notes[0]    # the placement note still leads


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

    def _fake_compile(self, source, *, accel_config, out_dir, opts=None,
                      vela_memory_mode=None, vela_system_config=None,
                      vela_vendor_system_config=None,
                      vela_vendor_config_filename=None, soc_declares_dram=None):
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
    assert rep.compute_on_npu_pct_max is None       # MAC-weighted field: never set at basis "compiled"
    assert rep.npu_placement_pct_real == 0.0        # the real, op-count placement instead
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

    def _fake_compile(self, source, *, accel_config, out_dir, opts=None,
                      vela_memory_mode=None, vela_system_config=None,
                      vela_vendor_system_config=None,
                      vela_vendor_config_filename=None, soc_declares_dram=None):
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
    assert rep.compute_on_npu_pct_max is None       # MAC-weighted field: never set at basis "compiled"
    assert rep.npu_placement_pct_real == 50.0       # the real, op-count placement instead
    assert rep.ops and rep.ops[0].op == "FULLY_CONNECTED"     # kept, not discarded
    assert "1/2" in rep.notes[0] and "50%" in rep.notes[0]


def test_exact_carries_uncosted_cpu_op_count_through_a_kept_partial_report(tmp_path, monkeypatch):
    # MINOR review: the compiled BackendReport (`ops=report.ops` kept on a
    # partial/cpu-only real compile) used to leave `uncosted_cpu_op_count` at
    # its dataclass default (0) even though the very verdicts it just kept
    # can carry cpu-certain/macs=0 ops the static screen already counted --
    # measured 2 such ops kept while `uncostedCpuOpCount` still read 0.
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)
    static_report = BackendReport(
        backend="ethos_u", variant="u55", table="/x/u55@vela-1.0.0.json",
        npu_coverage="partial", compute_on_npu_pct_max=66.67, uncosted_cpu_op_count=2,
        ops=[
            OpVerdict(op="CONV_2D", status="npu-eligible", reason="constraint-unchecked", macs=1000),
            OpVerdict(op="SOFTMAX", status="cpu-certain", reason="op-not-in-table", macs=0),
            OpVerdict(op="TOPK_V2", status="cpu-certain", reason="op-not-in-table", macs=0),
        ],
        basis="static-screen", confidence="screening", notes=[],
    )
    monkeypatch.setattr(check_mod, "analyze_backend", lambda **kw: static_report)

    def _fake_compile(self, source, *, accel_config, out_dir, opts=None,
                      vela_memory_mode=None, vela_system_config=None,
                      vela_vendor_system_config=None,
                      vela_vendor_config_filename=None, soc_declares_dram=None):
        return Blob(format="vela_tflite", payload=b"x", arena_bytes=100,
                    compiler_version="vela 5.1.0", req_sram_kib=1,
                    cpu_op_count=1, npu_op_count=2)

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _fake_compile)
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=True)
    rep = reports[0]
    assert rep.basis == "compiled"
    assert rep.npu_coverage == "partial"
    assert rep.uncosted_cpu_op_count == 2           # carried through, not defaulted to 0


def test_exact_fits_report_has_no_uncosted_cpu_op_count_since_ops_is_empty(tmp_path, monkeypatch):
    # The complementary "fits" case: ops=[] (nothing fell back), so there is
    # no kept cpu-certain verdict left to count -- 0 is correct here, not a
    # regression of the fix above.
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 supported=["FULLY_CONNECTED"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)

    def _fake_compile(self, source, *, accel_config, out_dir, opts=None,
                      vela_memory_mode=None, vela_system_config=None,
                      vela_vendor_system_config=None,
                      vela_vendor_config_filename=None, soc_declares_dram=None):
        return Blob(format="vela_tflite", payload=b"x", arena_bytes=123,
                    compiler_version="vela 3.9.0", req_sram_kib=4,
                    cpu_op_count=0, npu_op_count=1)

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _fake_compile)
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=True)
    rep = reports[0]
    assert rep.npu_coverage == "fits"
    assert rep.ops == []
    assert rep.uncosted_cpu_op_count == 0


def test_exact_degrades_to_the_static_screen_when_placement_cannot_be_parsed(tmp_path, monkeypatch):
    # A compiled Blob with no placement info at all (an unexpected vela output
    # shape) must not be read as "0 CPU ops, so it must be all-NPU" -- it
    # degrades exactly like every other "cannot verify" path in this module.
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 supported=["FULLY_CONNECTED"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)

    def _fake_compile(self, source, *, accel_config, out_dir, opts=None,
                      vela_memory_mode=None, vela_system_config=None,
                      vela_vendor_system_config=None,
                      vela_vendor_config_filename=None, soc_declares_dram=None):
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
    # The `rep.ops` assertion below needs REAL extracted operators, and only the
    # `tflite` reader (the `model-io` extra) produces those. `vela` on PATH says
    # nothing about that: on the bare install shape `ci.yml` builds, `rep.ops` is
    # `[]` and the assertion degrades to `assert ([])` instead of skipping.
    pytest.importorskip("tflite", reason=_MODEL_IO_SKIP_REASON)
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
    assert rep.compute_on_npu_pct_max is None       # MAC-weighted field: never set at basis "compiled"
    assert rep.npu_placement_pct_real == 0.0        # the real, op-count placement instead
    assert rep.ops and rep.ops[0].op == "FULLY_CONNECTED"      # kept, not `ops: []`


def test_exact_degrades_on_a_vela_compile_failure(tmp_path, monkeypatch):
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 supported=["FULLY_CONNECTED"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)

    def _boom(self, source, *, accel_config, out_dir, opts=None,
              vela_memory_mode=None, vela_system_config=None,
              vela_vendor_system_config=None,
              vela_vendor_config_filename=None, soc_declares_dram=None):
        raise RuntimeError("vela failed for ethos-u55-256: bad shape")

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _boom)
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=True)
    assert reports[0].basis == "static-screen"          # degraded, not crashed
    assert any(n.startswith("--exact") and "vela failed" in n for n in reports[0].notes)


def test_exact_truncates_a_multiline_vela_traceback_to_its_exception_line(tmp_path, monkeypatch):
    # MAJOR 2 review: a failed vela subprocess's RuntimeError can wrap its
    # own raw, multi-line stderr verbatim (measured: a 750-character,
    # 9-newline Python traceback) -- that must never land whole in a note.
    # tan-cli#789 review: nor may it be cut down to the traceback BANNER,
    # the one line of a traceback with no diagnostic content at all
    # (measured real note: "--exact compile with vela failed (vela failed
    # for ethos-u85-256: Traceback (most recent call last):); reporting the
    # static screen instead." -- discarding "RuntimeError: Compilation
    # failed: No networks defined via GraphAPI"). One line, and it must be
    # the line that says what went wrong.
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 supported=["FULLY_CONNECTED"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)
    traceback_text = "Traceback (most recent call last):\n" + "\n".join(
        f"  File \"vela/x.py\", line {i}, in fn" for i in range(9)) + "\nValueError: bad tensor shape"

    def _boom(self, source, *, accel_config, out_dir, opts=None,
              vela_memory_mode=None, vela_system_config=None,
              vela_vendor_system_config=None,
              vela_vendor_config_filename=None, soc_declares_dram=None):
        raise RuntimeError(traceback_text)

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _boom)
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=True)
    note = next(n for n in reports[0].notes if n.startswith("--exact"))
    assert "\n" not in note                             # never a raw multi-line traceback
    assert "vela/x.py" not in note                       # ... nor any frame body
    assert "Traceback (most recent call last)" not in note   # ... nor the contentless banner
    assert note.startswith("--exact compile with vela failed (ValueError: bad tensor shape)")


def test_a_refused_footprint_is_not_reported_as_a_failed_compile(tmp_path, monkeypatch):
    """tan-cli#789 review NIT 8 + MINOR 5, together -- both about the SAME
    note.

    NIT 8: vela exited 0, wrote its output and printed a real placement
    summary. What was refused was tan's acceptance of the FOOTPRINT it
    reported. "--exact compile with vela failed (...)" sends a customer
    hunting a vela bug that isn't there.

    MINOR 5: at `_VELA_ERR_NOTE_BUDGET` (200) the refusal was cut at
    "... Refusing to report a zero…" -- measured inner length 197 -- so the
    note in the text report AND the JSON envelope carried the diagnosis with
    none of the remediation. The whole point of rewording the refusal was the
    remediation, so it must survive to the reader."""
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u85", ethos_u_variant="u85")
    _write_soc(tmp_path, "fake:soc:u85", [{"type": "ethos-u85", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u85@vela-1.0.0.json", variant="u85",
                 supported=["FULLY_CONNECTED"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)
    # THE ARGUMENTS OF A REAL REFUSAL, NOT A COPY OF ITS MESSAGE. `_refuse`
    # below raises whatever the LIVE `_refuse_zero_sram_footprint` renders from
    # them, so every assertion at the bottom of this test reads the production
    # template.
    #
    # It used to raise a hand-copied literal of that template instead, and that
    # enforced NOTHING (tan-cli#789 review BLOCKER): the copy stayed
    # byte-identical to the template it came from, so rewording the selector
    # clause to "reads req_sram_kib == 0 as fits any envelope" left the whole
    # suite green while that phrase reached this very note and the JSON
    # envelope. Rendering live is what makes the `"fits" not in note` assertion
    # below an enforcement rather than a claim.
    #
    # The values are those of a real refusal on an ALIF ENSEMBLE part
    # (ethos-u-vela 5.1.0, tiny_int8.tflite at ethos-u85-256, a SoC spec with
    # no `npu_toolchain.vela.memory_mode` -- since alp-sdk #1470 that is what
    # it takes to reach this branch), captured by running that compile for
    # real. The two diagnostic facts are e8.json's own: it declares
    # `vendor_config_filename: ensemble_vela.ini`, and its
    # `external_memory_interfaces` lists exactly `HexSPI` and `SD/eMMC`, i.e.
    # no DRAM. Rendered live, that is 623 characters on one line.
    # Deliberately the LONGEST shape -- the same arguments with neither fact
    # (a part that declares no vendor file and whose DRAM answer is unknown)
    # render 499 characters, carrying neither the vendor clause nor the
    # no-DRAM marker -- since truncation is what this test is about.
    # `defaulted` carries vela's OWN names for the two flags, the strings
    # `_defaulted_flags` finds in its stdout.
    real_refusal_args = dict(
        accel_config="ethos-u85-256", npu_ops=1, cpu_ops=0,
        used={"dram": 0.27},
        system_config="Ethos_U85_SYS_DRAM_Mid",
        memory_mode="Dedicated_Sram_384KB",
        defaulted=frozenset({"system configuration", "memory mode"}),
        vendor_config_filename="ensemble_vela.ini",
        soc_declares_dram=False)

    def _refuse(self, source, *, accel_config, out_dir, opts=None,
                vela_memory_mode=None, vela_system_config=None,
                vela_vendor_system_config=None,
                vela_vendor_config_filename=None, soc_declares_dram=None):
        _refuse_zero_sram_footprint(**real_refusal_args)

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _refuse)
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=True)
    note = next(n for n in reports[0].notes if n.startswith("--exact"))
    assert reports[0].basis == "static-screen"          # degraded, not crashed
    # NIT 8: it compiled. It was the footprint that was refused.
    assert "compile with vela failed" not in note
    assert "vela compiled cleanly" in note
    # MINOR 5: the remediation, which sits at the TAIL, survives.
    assert "ensemble_vela.ini" in note
    assert "`tan model build` skips this target and still builds the SKU's others." in note
    assert "…" not in note                               # nothing was truncated at all
    assert "\n" not in note                              # ... and it is still one line
    # Non-negotiable 1, on the surface this note newly reaches: the report is
    # still `basis: "static-screen"`, and a static screen must never emit the
    # retired "fits" vocabulary in ANY form. The refusal explains alp-sdk's
    # selector predicate as "accepts ... against ANY arena size" precisely so
    # that widening the note budget could not walk "fits any envelope" into
    # the text report and the JSON envelope.
    assert "fits" not in note.lower()


@pytest.mark.parametrize("extra,expect_filename,expect_dram", [
    pytest.param(
        {"npu_toolchain": {"vela": {"memory_mode": "Sram_Only",
                                    "system_config_requires_vendor_config": True,
                                    "vendor_config_filename": "ensemble_vela.ini"}},
         "external_memory_interfaces": [{"kind": "HexSPI"}, {"kind": "SD/eMMC"}]},
        "ensemble_vela.ini", False, id="declares-a-vendor-ini-and-no-DRAM"),
    pytest.param(
        {"npu_toolchain": {"vela": {"memory_mode": "Shared_Sram",
                                    "system_config_requires_vendor_config": False}},
         "external_memory_interfaces": [{"kind": "LPDDR4/4X"}, {"kind": "FlexSPI"}]},
        None, True, id="declares-DRAM-and-no-vendor-ini"),
])
def test_exact_hands_the_adapter_the_parts_own_diagnostic_facts(
        tmp_path, monkeypatch, extra, expect_filename, expect_dram):
    """`--exact` resolves the refusal's EVIDENCE from the SAME target it
    resolves the accel config from, and passes it down (tan-cli#789 review (g),
    re-sourced from metadata).

    This used to pin `silicon_ref` -- the SoM preset's `silicon:` ref -- because
    a vendor-prefix match on it was what decided whether the refusal named
    `ensemble_vela.ini`. It no longer decides anything: the file a part needs is
    that part's own `npu_toolchain.vela.vendor_config_filename`, and whether a
    DRAM placement is impossible for it is its own
    `external_memory_interfaces[]`. Both must reach the adapter, or `--exact`'s
    refusal silently loses the two clauses that make it actionable while
    `tan model build`'s keeps them -- the two must read identically for the same
    part. Pinned on BOTH shapes so neither direction can rot: the part that
    declares a vendor `.ini` and no DRAM, and the part that declares DRAM and
    no `.ini`."""
    silicon = "fake:soc:u85"
    _write_som(tmp_path, "E1M-FAKE", silicon, ethos_u_variant="u85")
    _write_soc(tmp_path, silicon, [{"type": "ethos-u85", "subtype": "x", "mac_per_cycle": 256}],
               extra=extra)
    _write_table(tmp_path, "ethos_u", "u85@vela-1.0.0.json", variant="u85",
                 supported=["FULLY_CONNECTED"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)
    seen = {}

    def _fake_compile(self, source, *, accel_config, out_dir, opts=None,
                      vela_memory_mode=None, vela_system_config=None,
                      vela_vendor_system_config=None,
                      vela_vendor_config_filename=None, soc_declares_dram=None):
        seen["filename"] = vela_vendor_config_filename
        seen["dram"] = soc_declares_dram
        return Blob(format="vela_tflite", payload=b"x", arena_bytes=123,
                    compiler_version="vela 5.1.0", req_sram_kib=4,
                    cpu_op_count=0, npu_op_count=1)

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _fake_compile)
    check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                         metadata_root=tmp_path, exact=True)
    assert seen["filename"] == expect_filename
    assert seen["dram"] is expect_dram


def test_the_refusal_note_budget_covers_a_maximal_refusal():
    """The budget above the real message is measured, not guessed.

    `_VELA_REFUSAL_NOTE_BUDGET` is wider than `_VELA_ERR_NOTE_BUDGET` because
    it governs a string tan AUTHORS from a fixed template, not arbitrary vela
    stderr. That only holds while the template stays inside it, so this builds
    a deliberately maximal refusal -- three populated memory areas at five
    significant figures, the longest profile name vela emits, four-digit
    operator counts, AND both metadata-sourced clauses (a declared
    `vendor_config_filename`, and `soc_declares_dram=False`, which adds the
    no-DRAM marker to the `dram` figure) -- straight out of the real
    `_refuse_zero_sram_footprint`, and fails if it no longer fits. A truncated
    refusal loses its remediation tail, which is the whole reason the budget
    was raised (MINOR 5).

    Measured 691 of 700 with a 17-character `ensemble_vela.ini`. That filename
    is the ONE variable-length input in the whole template -- everything else
    is fixed prose or a bounded figure -- so those 9 characters are the
    headroom a longer future `vendor_config_filename` has to fit inside; a
    part declaring a longer one needs this budget re-measured, which is what
    this test is here to force."""
    with pytest.raises(VelaFootprintRefused) as exc:
        _refuse_zero_sram_footprint(
            accel_config="ethos-u85-256", npu_ops=999, cpu_ops=999,
            used={"sram": 0.0, "dram": 9999.99,
                  "on_chip_flash": 9999.99, "off_chip_flash": 9999.99},
            system_config="Ethos_U55_High_End_Embedded",
            memory_mode="Dedicated_Sram_384KB",
            # BOTH flags defaulted -- the widest wording the template emits:
            # the blanket "vela's BUILT-IN default profile" tail plus the full
            # remediation sentence. A half-defaulted run (the shape tan
            # produces now that it passes a metadata-sourced --memory-mode) is
            # strictly shorter.
            defaulted=frozenset({"system configuration", "memory mode"}),
            vendor_config_filename="ensemble_vela.ini",
            soc_declares_dram=False)
    maximal = str(exc.value)
    assert "\n" not in maximal
    assert len(maximal) == 691                               # re-measure if this moves
    assert len(maximal) <= check_mod._VELA_REFUSAL_NOTE_BUDGET
    # ... and the FOREIGN-stderr budget is not what got widened: a 750-char,
    # 9-newline vela traceback must still be cut at 200.
    assert check_mod._VELA_ERR_NOTE_BUDGET == 200
    assert len(check_mod._short_vela_error(exc.value)) <= 201    # 200 + the "…" marker
    assert "ensemble_vela.ini" in maximal    # ... it really is the vendor-clause shape


def test_exact_keeps_the_accel_config_in_front_of_a_wrapped_traceback(tmp_path, monkeypatch):
    """`VelaAdapter.compile` prefixes its own "vela failed for <accel>:" onto
    whatever vela raised, and that prefix is the only thing naming the target
    -- so it survives alongside the exception line, not instead of it.
    Message shape measured on a real failing run."""
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u85", ethos_u_variant="u85")
    _write_soc(tmp_path, "fake:soc:u85", [{"type": "ethos-u85", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(tmp_path, "ethos_u", "u85@vela-1.0.0.json", variant="u85",
                 supported=["FULLY_CONNECTED"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)

    def _boom(self, source, *, accel_config, out_dir, opts=None,
              vela_memory_mode=None, vela_system_config=None,
              vela_vendor_system_config=None,
              vela_vendor_config_filename=None, soc_declares_dram=None):
        raise RuntimeError(
            "vela failed for ethos-u85-256: Traceback (most recent call last):\n"
            "  File \"ethosu/vela/vela.py\", line 1, in main\n"
            "RuntimeError: Compilation failed: No networks defined via GraphAPI")

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _boom)
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=True)
    note = next(n for n in reports[0].notes if n.startswith("--exact"))
    assert "vela failed for ethos-u85-256:" in note                       # which target
    assert "No networks defined via GraphAPI" in note                     # what went wrong
    assert "Traceback (most recent call last)" not in note
    assert "\n" not in note


def test_exact_never_spawns_vela_for_a_format_it_does_not_ingest(tmp_path, monkeypatch):
    # MAJOR 2 review, the actual bug: the old guard
    # (`report.ops and report.ops[0].reason == "format-not-accepted"`) was
    # dead code for exactly ethos_u's own non-.tflite case -- extract_ops
    # never extracts operators from a .onnx source, so report.ops was always
    # [] and the guard never fired, letting VelaAdapter().compile() actually
    # be invoked on a source vela cannot ingest at all.
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    onnx_source = tmp_path / "model.onnx"
    onnx_source.write_bytes(b"not-a-real-onnx-file")

    def _must_not_run(self, *a, **kw):
        raise AssertionError("VelaAdapter.compile must not run for a format it does not accept")

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _must_not_run)
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=onnx_source,
                                    metadata_root=tmp_path, exact=True)
    rep = reports[0]
    assert rep.npu_coverage == "undetermined"
    assert rep.basis == "static-screen"
    assert "does not ingest" in rep.notes[0]              # the ORDINARY format-not-accepted note
    assert not any("vela" in n.lower() for n in rep.notes)


@pytest.mark.skipif(shutil.which("vela") is None, reason="vela (ethos-u-vela) not installed")
def test_exact_with_real_vela_installed_still_never_runs_it_on_an_onnx_source(tmp_path):
    # Same proof as above, with the real `vela` binary on PATH (not mocked
    # away): before the fix this actually spawned vela on the .onnx source
    # and surfaced its raw failure text as a note.
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    onnx_source = tmp_path / "model.onnx"
    onnx_source.write_bytes(b"not-a-real-onnx-file")
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=onnx_source,
                                    metadata_root=tmp_path, exact=True)
    rep = reports[0]
    assert rep.basis == "static-screen"
    assert not any("vela failed" in n.lower() or "traceback" in n.lower() for n in rep.notes)


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
    target = _headline_ethos_u_target("E1M-FAKE", tmp_path)
    assert target.accel_config == "ethos-u55-256"
    # The whole TargetSpec comes back so `--exact` can hand the adapter the
    # SAME target's memory profile and diagnostic facts alongside its accel
    # config (tan-cli#789 review (g)) -- a profile or a vendor `.ini` read off a
    # different target would be exactly the class of mismatch this fix exists
    # to stop.
    assert target.silicon_ref == "fake:soc:dual"


def test_headline_accel_config_is_none_without_an_ethos_u_variant(tmp_path):
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:multi")
    _write_soc(tmp_path, "fake:soc:multi", [{"type": "drp-ai3", "subtype": "drp", "mac_per_cycle": 1}])
    assert _headline_ethos_u_target("E1M-FAKE", tmp_path) is None


# ---------------------------------------------------------------------------
# TIER 2: a bench-measured `metadata/model_perf/` point (the tier-2 plan's
# Task 4). Resolution order is precomputed -> exact-if-toolchain -> static, and
# `basis: "bench"` is what `analyze.py`'s vocabulary reserved from the start.
# ---------------------------------------------------------------------------

#: The REAL digest of the REAL fixture this file screens everywhere else --
#: computed from its bytes, never transcribed, so a point written here is
#: matched by the same sha256 `check_model_backends` computes off the same
#: file. A hand-typed constant would pass by coincidence at best.
_FIXTURE_SHA = hashlib.sha256(_FIXTURE.read_bytes()).hexdigest()


def _write_perf_point(meta: Path, *, sku: str = "E1M-FAKE", hw_rev: str = "r2",
                      core: str = "m55_hp", backend: str = "ethos_u",
                      accel_config: str = "ethos-u55-256",
                      sha256: str | None = None, toolchain: str = "vela",
                      version: str = "5.1.0", memory_mode: str = "Sram_Only",
                      measured: dict | None = None,
                      filename: str = "tiny-int8-aaaa@vela-5.1.0+r2+m55_hp+aaaaaaaaaaaa.json",
                      **extra) -> Path:
    """One published-shaped perf point at the path its own body claims
    (alp-sdk `f724d3e4`'s stem: `...@<toolchain>-<version>+<hw_rev>+<core>+
    <profile12>.json`). Nothing in tan parses that stem -- the body is the
    truth -- so these filenames only ever have to be DISTINCT, which is exactly
    the collision property those segments were added for."""
    doc = {
        "stance": "bench-measured",
        "measured_on": {"sku": sku, "hw_rev": hw_rev, "core": core,
                        "backend": backend, "accel_config": accel_config},
        "model": {"slug": "tiny-int8", "sha256": sha256 or _FIXTURE_SHA,
                   "size_bytes": _FIXTURE.stat().st_size,
                   "source": "tests/fixtures/models/tiny_int8.tflite"},
        # `system_config`/`memory_mode` are REQUIRED on an `ethos_u` point by
        # alp-sdk's own validator, so the fixture carries them: a flagless vela
        # picks a DRAM-backed default and the arena figures then describe THAT
        # profile rather than the module.
        "toolchain": ({"name": toolchain, "version": version,
                        "system_config": "Ethos_U55_High_End_Embedded",
                        "memory_mode": memory_mode} if backend == "ethos_u"
                       else {"name": toolchain, "version": version}),
        "measured": measured if measured is not None else {
            "npu_ops": 1, "cpu_ops": 0, "arena_bytes": 32, "req_sram_kib": 1,
            "latency_ms_mean": 0.4, "latency_ms_p95": 0.5, "runs": 100},
        "capture": {"date": "2026-08-16", "operator": "alpCaner",
                     "reference": "alp-sdk-internal:bench/captures/tiny.log"},
        **extra,
    }
    d = meta / "model_perf" / sku / (accel_config or backend)
    d.mkdir(parents=True, exist_ok=True)
    path = d / filename
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _u55_tree(meta: Path, *, paired_core: str | None = "m55_hp",
              default_hw_rev: str | None = "r2",
              memory_mode: str | None = "Sram_Only") -> None:
    """The synthetic SKU every tier-2 case below screens against: one Ethos-U55
    at 256 MAC/cycle, paired (through the SoC spec's own `npus[].paired_core`,
    the way the real E8 pairs each of its two U55s to an M55) to a named core,
    declaring the vela memory profile the part is compiled under, and carrying
    a `default_hw_rev` the way a real SoM preset does."""
    _write_som(meta, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55",
               default_hw_rev=default_hw_rev)
    npu: dict = {"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}
    if paired_core:
        npu["paired_core"] = paired_core
    extra = ({"npu_toolchain": {"vela": {"memory_mode": memory_mode,
                                          "system_config_requires_vendor_config": True}}}
             if memory_mode else None)
    _write_soc(meta, "fake:soc:u55", [npu], extra=extra)
    _write_table(meta, "ethos_u", "u55@vela-1.0.0.json", variant="u55",
                 supported=["FULLY_CONNECTED"])


def _check(meta: Path, *, exact: bool = False, hw_rev: str | None = None) -> BackendReport:
    return check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                 metadata_root=meta, exact=exact, hw_rev=hw_rev)[0]


def test_a_matched_perf_point_reports_bench_with_the_measured_figures(tmp_path):
    """The whole commercial premise: no toolchain, no silicon, an exact answer.

    Nothing is monkeypatched here -- no vela, no compile, `exact=False` -- and
    the report still carries a measured arena, a measured resident SRAM and a
    measured latency, because Alp Lab already ran this exact model on this
    exact module."""
    _u55_tree(tmp_path)
    _write_perf_point(tmp_path)
    rep = _check(tmp_path)
    assert rep.basis == "bench"
    assert rep.confidence == "certain"
    assert (rep.arena_bytes, rep.req_sram_kib) == (32, 1)
    assert (rep.latency_ms_mean, rep.latency_ms_p95, rep.latency_runs) == (0.4, 0.5, 100)
    assert rep.perf_ref == "alp-sdk-internal:bench/captures/tiny.log"
    assert rep.npu_placement_pct_real == 100.0
    assert rep.compute_on_npu_pct_max is None      # MAC-weighted: never set at a placement basis


def test_the_bench_note_is_one_line_and_traces_back_to_its_capture(tmp_path):
    # A number nobody can trace back to a run is not reproducible, and a perf
    # point is worth exactly what its reproducibility is.
    _u55_tree(tmp_path)
    _write_perf_point(tmp_path)
    rep = _check(tmp_path)
    assert len(rep.notes) == 1
    note = rep.notes[0]
    assert "\n" not in note
    assert "alp-sdk-internal:bench/captures/tiny.log" in note
    assert "E1M-FAKE r2 (core m55_hp) at ethos-u55-256 with vela 5.1.0" in note
    # The memory profile the figures describe -- an arena captured under a
    # DRAM-backed profile is exactly measured and describes the wrong machine,
    # which is why alp-sdk's schema makes both required on an Ethos-U point.
    assert "(Ethos_U55_High_End_Embedded, Sram_Only)" in note
    assert "1/1 operators on the NPU" in note
    assert "arena 32 bytes" in note and "1 KiB resident SRAM" in note
    assert "0.4 ms mean, 0.5 ms p95 over 100 runs" in note
    assert "Alp Lab" in note and "ALP Lab" not in note


def test_a_point_for_a_backend_with_no_memory_profile_says_nothing_about_one(tmp_path):
    # `system_config`/`memory_mode` are a vela concept; a DRP-AI point carries
    # neither, and the note must not invent an empty pair of parentheses -- or
    # a profile -- for one.
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:multi", default_hw_rev="r2")
    _write_soc(tmp_path, "fake:soc:multi", [{"type": "drp-ai3", "subtype": "drp",
                                              "mac_per_cycle": 1}])
    _write_perf_point(tmp_path, backend="drpai", accel_config="",
                      toolchain="translator", version="1.12",
                      filename="tiny-int8-aaaa@translator-1.12+r2+a55_cluster+44136fa355b3.json")
    rep = check_model_backends(backends=["drpai"], sku="E1M-FAKE", source=_FIXTURE,
                                metadata_root=tmp_path, exact=False)[0]
    assert rep.basis == "bench"
    assert "with translator 1.12 --" in rep.notes[0]
    assert "()" not in rep.notes[0]


def test_a_drpai_points_stray_ethos_u_profile_fields_are_never_printed(tmp_path):
    """tan-cli#791 round-2 review item 5: `target` is `None` for every
    backend but `ethos_u`, so nothing here ever narrows OR VERIFIES a
    drpai/deepx_dxm1 point's `toolchain_system_config`/`toolchain_memory_
    mode` against silicon -- printing whatever those two fields happen to
    hold would print unverified, possibly nonsense data as if it were part
    of this point's identity. Measured (the review's own repro): a drpai
    point carrying a stray Ethos-U85 profile pair printed
    "(Ethos_U85_SYS_DRAM_Mid, Dedicated_Sram_384KB)" verbatim into the
    customer-facing note. `_write_perf_point` cannot express this shape
    directly (its `toolchain` dict is hardcoded per-backend), so this
    round-trips the file through JSON the same way
    `test_a_foreign_multiline_capture_citation_cannot_break_the_one_line_
    note` does below."""
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:multi", default_hw_rev="r2")
    _write_soc(tmp_path, "fake:soc:multi", [{"type": "drp-ai3", "subtype": "drp",
                                              "mac_per_cycle": 1}])
    path = _write_perf_point(tmp_path, backend="drpai", accel_config="",
                             toolchain="translator", version="1.12",
                             filename="tiny-int8-aaaa@translator-1.12+r2+m55_hp+cccccccccccc.json")
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["toolchain"]["system_config"] = "Ethos_U85_SYS_DRAM_Mid"
    doc["toolchain"]["memory_mode"] = "Dedicated_Sram_384KB"
    path.write_text(json.dumps(doc), encoding="utf-8")
    rep = check_model_backends(backends=["drpai"], sku="E1M-FAKE", source=_FIXTURE,
                                metadata_root=tmp_path, exact=False)[0]
    assert rep.basis == "bench"
    assert "Ethos_U85_SYS_DRAM_Mid" not in rep.notes[0]
    assert "Dedicated_Sram_384KB" not in rep.notes[0]
    assert "with translator 1.12 --" in rep.notes[0]      # identity still ends cleanly
    assert "()" not in rep.notes[0]


def test_a_foreign_multiline_capture_citation_cannot_break_the_one_line_note(tmp_path):
    # `capture.reference` comes out of a document in ANOTHER repository that
    # this reader deliberately does not schema-validate -- a note is one line
    # in the text report AND the JSON envelope by contract either way.
    _u55_tree(tmp_path)
    _write_perf_point(tmp_path)
    path = next((tmp_path / "model_perf").rglob("*.json"))
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["capture"]["reference"] = "alp-sdk-internal:bench/\n  captures/\ttiny.log"
    path.write_text(json.dumps(doc), encoding="utf-8")
    rep = _check(tmp_path)
    assert rep.basis == "bench"
    assert "\n" not in rep.notes[0] and "\t" not in rep.notes[0]


def test_a_partial_bench_point_keeps_the_static_per_op_verdicts(tmp_path):
    # Same rule the compiled path already follows: a real partial result must
    # erase neither the static verdicts nor the uncosted-op caveat.
    pytest.importorskip("tflite", reason=_MODEL_IO_SKIP_REASON)
    _u55_tree(tmp_path)
    _write_perf_point(tmp_path, measured={"npu_ops": 1, "cpu_ops": 1,
                                           "arena_bytes": 64, "req_sram_kib": 2})
    rep = _check(tmp_path)
    assert rep.basis == "bench"
    assert rep.npu_coverage == "partial"
    assert rep.npu_placement_pct_real == 50.0
    assert rep.ops and rep.ops[0].op == "FULLY_CONNECTED"


# ---------------------------------------------------------------------------
# ABSENCE MUST NEVER DEGRADE. Every non-match path hands the SAME report back.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs,why", [
    ({"sha256": "f" * 64}, "a different model"),
    ({"sku": "E1M-OTHER"}, "a different SKU"),
    ({"accel_config": "ethos-u55-128"}, "a different accel config in the same family"),
    ({"toolchain": "dxcom", "version": "2.3.0"}, "a different toolchain"),
    ({"backend": "drpai", "accel_config": ""}, "a different backend"),
])
def test_a_point_for_another_identity_leaves_the_report_exactly_as_it_was(
        tmp_path, kwargs, why):
    _u55_tree(tmp_path)
    baseline = _check(tmp_path)                    # no perf tree at all yet
    _write_perf_point(tmp_path, **kwargs)
    after = _check(tmp_path)
    assert after == baseline, f"a point for {why} changed the report"
    assert after.basis == "static-screen"
    assert after.npu_coverage != "fits"
    assert not any("fits" in n.lower() for n in after.notes)
    assert not any("bench" in n.lower() for n in after.notes)


def test_a_fixture_bannered_point_never_reaches_the_report(tmp_path):
    # Its `measured` figures are PLACEHOLDERS. Reporting one at `confidence:
    # "certain"` is the single worst output this tier can produce.
    _u55_tree(tmp_path)
    baseline = _check(tmp_path)
    _write_perf_point(tmp_path, _fixture="SYNTHETIC TEST FIXTURE -- NOT A MEASUREMENT")
    assert _check(tmp_path) == baseline


def test_two_points_for_one_identity_leave_the_report_exactly_as_it_was(tmp_path):
    _u55_tree(tmp_path)
    baseline = _check(tmp_path)
    _write_perf_point(tmp_path, filename="a@vela-5.1.0.json")
    _write_perf_point(tmp_path, version="5.2.0", filename="b@vela-5.2.0.json")
    assert _check(tmp_path) == baseline


def test_a_point_that_measured_no_placement_leaves_the_report_exactly_as_it_was(tmp_path):
    """A point with latency but no operator placement cannot answer what
    `npu_coverage` asks. Re-basing on it would have to invent a coverage or
    downgrade a real static verdict to `undetermined` -- both worse than the
    report the customer already had."""
    _u55_tree(tmp_path)
    baseline = _check(tmp_path)
    _write_perf_point(tmp_path, measured={"latency_ms_mean": 0.4, "runs": 100})
    assert _check(tmp_path) == baseline


def test_a_perf_tree_that_does_not_exist_is_not_an_error(tmp_path):
    # `metadata/model_perf/` exists in NO alp-sdk today; this is the state
    # every customer's checkout is in.
    _u55_tree(tmp_path)
    rep = _check(tmp_path)
    assert rep.basis == "static-screen"
    assert rep.npu_coverage == "full-eligible" or rep.npu_coverage == "undetermined"


def test_a_point_measured_on_another_module_revision_leaves_the_report_as_it_was(tmp_path):
    # `default_hw_rev: r2`, an r1 point: an r1 and an r2 measurement of one
    # model on one target are different measurements (alp-sdk `f724d3e4`), and
    # serving the wrong one is the "exactly measured, describes a different
    # machine" failure that put `hw_rev` in the identity.
    _u55_tree(tmp_path)
    baseline = _check(tmp_path)
    _write_perf_point(tmp_path, hw_rev="r1")
    assert _check(tmp_path) == baseline


def test_the_boards_own_declared_hw_rev_wins_over_the_presets_default(tmp_path):
    """`som.hw_rev` is the customer's statement about the module in their hand;
    `default_hw_rev` is only the fallback `board.schema.json` names for an
    omitted key. A customer who says r1 must get the r1 measurement, and the
    r2 one must not reach them."""
    _u55_tree(tmp_path, default_hw_rev="r2")
    _write_perf_point(tmp_path, hw_rev="r1")
    assert _check(tmp_path, hw_rev="r1").basis == "bench"        # theirs
    assert _check(tmp_path, hw_rev="r2").basis == "static-screen"  # the preset's
    assert _check(tmp_path).basis == "static-screen"               # falls back to r2


def test_a_sku_whose_preset_declares_no_default_hw_rev_matches_nothing(tmp_path):
    # Neither the board nor the preset says which revision this is, so tan
    # cannot know which measurement applies -- and hands back the report the
    # customer already had rather than one module revision's number.
    _u55_tree(tmp_path, default_hw_rev=None)
    baseline = _check(tmp_path)
    _write_perf_point(tmp_path)
    assert _check(tmp_path) == baseline


def test_a_point_measured_on_another_core_of_the_same_die_is_not_a_match(tmp_path):
    """The core comes from the SoC spec's own `npus[].paired_core` -- the same
    field that makes the E8's high-perf U55 a different accelerator from its
    high-efficiency one. An A-cluster and an M-class inference of one model
    differ by orders of magnitude; a point measured on the wrong one is not
    this measurement."""
    _u55_tree(tmp_path, paired_core="m55_hp")
    baseline = _check(tmp_path)
    _write_perf_point(tmp_path, core="m55_he")
    assert _check(tmp_path) == baseline
    _write_perf_point(tmp_path, core="m55_hp",
                      filename="tiny-int8-aaaa@vela-5.1.0+r2+m55_hp+bbbbbbbbbbbb.json")
    assert _check(tmp_path).basis == "bench"


def test_a_part_that_pairs_no_core_narrows_on_none_and_infers_nothing(tmp_path):
    """A die with a SINGLE Ethos-U NPU that itself declares no `paired_core`
    -- the E1M-NX9101/imx93 shape (its lone `ethos-u65` names no
    `paired_core`) as well as, since tan-cli#791 round-3's evidence-driven
    narrowing, the E8's own Ethos-U85 (`test_an_unpaired_variant_accepts_
    any_topology_core_the_die_admits`, below): whenever the SPECIFIC
    accelerator being screened declares no pairing of its own, tan no longer
    looks anywhere else on the die for one. The metadata does not know which
    core drove the inference, so tan does not guess: a single published point
    still answers -- and two points differing only in core would leave two
    standing and fall through, which is the multi-match rule doing its job
    rather than a core being invented. Level 1 (`_topology_core_ids`) still
    requires the core to be a REAL one this fixture's own `topology:`
    declares (`m55_hp`/`m55_he`, part of `_DEFAULT_TEST_TOPOLOGY`) -- the
    shape where even level 1 has nothing to catch is covered separately, by
    the imx93-shaped tests below."""
    _u55_tree(tmp_path, paired_core=None)
    _write_perf_point(tmp_path, core="m55_he")
    assert _check(tmp_path).basis == "bench"
    _write_perf_point(tmp_path, core="m55_hp",
                      filename="tiny-int8-aaaa@vela-5.1.0+r2+m55_hp+bbbbbbbbbbbb.json")
    assert _check(tmp_path).basis == "static-screen"


def _e8_shaped_tree(meta: Path, *, memory_mode: str | None = "Sram_Only") -> None:
    """The E1M-AEN801 shape (tan-cli#791 review MAJOR 2): the E8's own
    `ethos-u85` names no `paired_core` (`_headline_ethos_u_target` resolves
    IT for this SKU, since it is the higher `mac_per_cycle` config), but the
    SAME die's two `ethos-u55` rows pair to `m55_hp`/`m55_he` -- so nothing on
    this die ever pairs an Ethos-U NPU of ANY variant to an application-class
    core."""
    _write_som(meta, "E1M-FAKE", "fake:soc:multi", ethos_u_variant="u85",
               default_hw_rev="r2")
    npus = [
        {"type": "ethos-u85", "subtype": "generative", "mac_per_cycle": 256},
        {"type": "ethos-u55", "subtype": "high-perf", "mac_per_cycle": 256,
         "paired_core": "m55_hp"},
        {"type": "ethos-u55", "subtype": "high-efficiency", "mac_per_cycle": 128,
         "paired_core": "m55_he"},
    ]
    extra = ({"npu_toolchain": {"vela": {"memory_mode": memory_mode,
                                          "system_config_requires_vendor_config": True}}}
             if memory_mode else None)
    _write_soc(meta, "fake:soc:multi", npus, extra=extra)
    _write_table(meta, "ethos_u", "u85@vela-1.0.0.json", variant="u85",
                 supported=["FULLY_CONNECTED"])


def test_an_unpaired_variant_accepts_any_topology_core_the_die_admits(tmp_path):
    """ROUND 3, evidence-driven correction of the prior round's MAJOR 2 fix:
    `_headline_ethos_u_target` resolves the E8's Ethos-U85, which pairs to no
    core of its own -- and a point measured on `a32_cluster` (the Cortex-A32
    application cluster) is now ACCEPTED, not refused, because the die's
    OTHER Ethos-U NPUs pairing to `m55_hp`/`m55_he` is not a sourced fact
    about the U85 itself. Newly-sourced silicon evidence (a `NPU_HG_BASE`
    register alias byte-identical across both M55 headers, no `Pname` on its
    vendor DFP element, system-side clock gating) shows the U85 is genuinely
    shared SoC-level silicon, and alp-sdk's own schema says a shared NPU
    legitimately declares no `paired_core` at all -- so a point naming any
    core this SKU's own `topology:` admits must not be refused on the
    strength of a SIBLING NPU's pairing.

    Proves this independently of LEVEL 1 (tan-cli#791 round-2 review item 1's
    mutation-proof bar): `a32_cluster` IS a real core in this SKU's own
    `topology:` (`_DEFAULT_TEST_TOPOLOGY`), so level 1 (`_topology_core_ids`)
    would admit it regardless -- this test fails only if a union-style
    inference over sibling NPUs is reintroduced and refuses it again.

    MUTATION PROOF: re-adding the old union check (`{t.paired_core for t in
    resolve_targets(...) if t.backend == backend and t.paired_core}`, applied
    whenever non-empty) turns this test RED -- `a32_cluster` is not in that
    union (only `m55_hp`/`m55_he` are), so the point would be refused again
    and `rep.basis` would stay `"static-screen"`."""
    _e8_shaped_tree(tmp_path)
    baseline = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE",
                                    source=_FIXTURE, metadata_root=tmp_path,
                                    exact=False, hw_rev="r2")[0]
    assert baseline.basis == "static-screen"
    _write_perf_point(tmp_path, sku="E1M-FAKE", accel_config="ethos-u85-256",
                      core="a32_cluster")
    rep = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE",
                               source=_FIXTURE, metadata_root=tmp_path,
                               exact=False, hw_rev="r2")[0]
    assert rep.basis == "bench"                   # accepted, not refused
    assert not any("refused" in n for n in rep.notes)


def test_an_unpaired_variant_accepts_a_core_a_sibling_npu_does_pair_to(tmp_path):
    # An accelerator that itself pairs to no core does not reject a point
    # measured on a core a DIFFERENT NPU on the same die happens to pair to,
    # either -- `m55_hp` is a real pairing on THIS die, just for a different
    # NPU, and nothing about the U85 contradicts it.
    _e8_shaped_tree(tmp_path)
    _write_perf_point(tmp_path, sku="E1M-FAKE", accel_config="ethos-u85-256",
                      core="m55_hp")
    rep = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE",
                               source=_FIXTURE, metadata_root=tmp_path,
                               exact=False, hw_rev="r2")[0]
    assert rep.basis == "bench"


# ---------------------------------------------------------------------------
# THE MAJOR FIX, round 2 (tan-cli#791 round-2 review item 1): a die where NO
# npu of a backend declares ANY `paired_core` -- the E1M-NX9101/imx93 shape --
# used to leave `_paired_cores_for_backend` answering `{}`, and the old
# `if allowed_cores:` guard then skipped the core check ENTIRELY: a point
# claiming `core: "m55_hp"` (a core the i.MX 93 does not physically have) or
# even `core: "cortex_potato"` (a core NOTHING has) was consumed outright at
# `basis: "bench", confidence: "certain"`. LEVEL 1 (`_topology_core_ids`)
# closes this: a point's core must exist in the SKU's own `topology:` map,
# unconditionally, whether or not anything pairs an NPU to a core at all.
# ---------------------------------------------------------------------------

def _imx93_shaped_tree(meta: Path, *, topology: tuple[str, ...] = ("a55_cluster", "m33")) -> None:
    """The REAL E1M-NX9101/imx93 shape, reproduced synthetically: ONE
    `ethos-u65` NPU that names no `paired_core` at all (imx93.json carries no
    `npus[].paired_core` and no `npu_toolchain` block either, matched here by
    leaving both out), on a SoM preset whose `topology:` is `a55_cluster` +
    `m33` -- the real E1M-NX9101.yaml's own two entries, not `m55_hp` or
    anything Ensemble-shaped. The lone `ethos-u65` target's own `paired_core`
    is `None`, so the exact-match query has nothing to narrow `core` on for
    this die, which is what makes this shape the one where level 1
    (`_topology_core_ids`) is the ONLY thing that can ever refuse a bad
    core -- a test against this fixture is a proof of level 1 in isolation,
    not a proof that happens to pass either way."""
    _write_som(meta, "E1M-FAKE", "fake:soc:u65", ethos_u_variant="u65",
               default_hw_rev="r1", topology=topology)
    _write_soc(meta, "fake:soc:u65", [{"type": "ethos-u65", "subtype": "x", "mac_per_cycle": 256}])
    _write_table(meta, "ethos_u", "u65@vela-1.0.0.json", variant="u65",
                 supported=["FULLY_CONNECTED"])


def _check_imx93(meta: Path) -> BackendReport:
    return check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                 metadata_root=meta, exact=False, hw_rev="r1")[0]


@pytest.mark.parametrize("bogus_core", ["m55_hp", "cortex_potato"])
def test_a_core_absent_from_the_soms_topology_is_refused_even_when_the_die_pairs_nothing(
        tmp_path, bogus_core):
    """The exact production-path bug the round-2 review verified through `tan
    model check` on real E1M-NX9101 metadata: `m55_hp` is a real Ensemble core
    name, not an i.MX 93 one (the i.MX 93's topology is `a55_cluster`/`m33`
    only) -- and `cortex_potato` names no core anywhere, on any SoC. Both must
    be refused, and BEFORE this fix neither was: `_paired_cores_for_backend`
    answers `{}` for imx93's lone, unpaired `ethos-u65`, and the OLD
    `if allowed_cores:` guard skipped the core check entirely the moment that
    set was empty."""
    _imx93_shaped_tree(tmp_path)
    baseline = _check_imx93(tmp_path)
    assert baseline.basis == "static-screen"
    _write_perf_point(tmp_path, sku="E1M-FAKE", hw_rev="r1", core=bogus_core,
                      backend="ethos_u", accel_config="ethos-u65-256",
                      filename=f"tiny-int8-aaaa@vela-5.1.0+r1+{bogus_core}+aaaaaaaaaaaa.json")
    assert _check_imx93(tmp_path) == baseline           # refused, not consumed


def test_a_core_the_soms_topology_does_declare_still_answers_on_an_unpaired_die(tmp_path):
    """The ordinary case must not regress: E1M-NX9101's own `a55_cluster` (a
    REAL core in the SKU's `topology:`) still answers, even though the lone
    `ethos-u65` on this die pairs to no core at all -- core stays unnarrowed
    past the exact-match query here (as alp-sdk's own docs say it must: "do
    not invent the pairing... record the core you actually ran on"), and
    level 1 has nothing to refuse about a core that genuinely exists."""
    _imx93_shaped_tree(tmp_path)
    _write_perf_point(tmp_path, sku="E1M-FAKE", hw_rev="r1", core="a55_cluster",
                      backend="ethos_u", accel_config="ethos-u65-256",
                      filename="tiny-int8-aaaa@vela-5.1.0+r1+a55_cluster+aaaaaaaaaaaa.json")
    assert _check_imx93(tmp_path).basis == "bench"


def test_a_som_preset_missing_topology_entirely_refuses_every_point(tmp_path):
    """LEVEL 1's fail-CLOSED behaviour for a SoM preset that drops `topology:`
    entirely (tan-cli#791 round-2 review, item 1 gap): `_topology_core_ids`'s
    own docstring says `set()` -- "REFUSES every point, never admits one"
    (`perf_apply.py:229`) -- so an otherwise-perfect point (right SKU, right
    hw_rev, right core, right backend, right sha256, right toolchain) must
    still be refused when the preset carries no `topology:` block at all,
    exactly as decisively as `test_a_core_absent_from_the_soms_topology_is_
    refused_even_when_the_die_pairs_nothing` (above) refuses a bogus CORE.

    Mutation-proof: gating the level-1 filter behind `if topology_cores:` --
    the EXACT fail-open shape the old `if allowed_cores:` guard was fixed for
    at what used to be level 2 -- turns this test RED, because an empty
    `topology_cores` would then skip the filter instead of refusing
    everything through it."""
    _imx93_shaped_tree(tmp_path, topology=None)
    baseline = _check_imx93(tmp_path)
    assert baseline.basis == "static-screen"
    _write_perf_point(tmp_path, sku="E1M-FAKE", hw_rev="r1", core="a55_cluster",
                      backend="ethos_u", accel_config="ethos-u65-256",
                      filename="tiny-int8-aaaa@vela-5.1.0+r1+a55_cluster+aaaaaaaaaaaa.json")
    result = _check_imx93(tmp_path)
    assert result == baseline                     # refused, not consumed
    assert not any("refused" in n for n in result.notes)  # silent, per the
    # "absent means refuse" contract `_topology_core_ids`'s own docstring
    # claims -- same as a wrong SKU/hw_rev/model/toolchain refusal elsewhere
    # in this reader; `_resolve_perf_point` no longer produces a note at all.


def test_a_som_preset_with_an_empty_topology_block_refuses_every_point(tmp_path):
    """The other half of the same fail-closed contract: `topology: {}` --
    PRESENT but empty -- reaches `_topology_core_ids` as an empty dict, not a
    missing key. `isinstance({}, dict)` is True, so `set({})` is still
    `set()`, the same refuse-everything answer as a dropped key entirely
    (`_topology_core_ids`'s own docstring: "a preset that dropped `topology:`
    entirely OR left it malformed"). `_write_som`'s `topology=` truthiness
    check (`if topology:`) cannot express this shape on its own -- an empty
    tuple is exactly as falsy as `None` -- so this test appends the block by
    hand onto the preset `_imx93_shaped_tree` already wrote with `topology=
    None`."""
    _imx93_shaped_tree(tmp_path, topology=None)
    som_path = tmp_path / "e1m_modules" / "E1M-FAKE.yaml"
    som_path.write_text(som_path.read_text() + "topology: {}\n", encoding="utf-8")
    baseline = _check_imx93(tmp_path)
    assert baseline.basis == "static-screen"
    _write_perf_point(tmp_path, sku="E1M-FAKE", hw_rev="r1", core="a55_cluster",
                      backend="ethos_u", accel_config="ethos-u65-256",
                      filename="tiny-int8-aaaa@vela-5.1.0+r1+a55_cluster+aaaaaaaaaaaa.json")
    result = _check_imx93(tmp_path)
    assert result == baseline                     # refused, not consumed
    assert not any("refused" in n for n in result.notes)


# ---------------------------------------------------------------------------
# THE BLOCKER FIX (tan-cli#791 review item 1): the profile tiebreak used to
# apply ONLY once two or more points survived every other filter, so a SINGLE
# point captured under a memory profile the part does not declare was handed
# back unfiltered at `confidence: "certain"` -- the realistic first-campaign
# shape (exactly one point per part) hit this every time, not just on a
# collision. Narrowing now happens BEFORE the single-point shortcut whenever
# the part states a profile at all.
# ---------------------------------------------------------------------------

def test_a_lone_point_under_the_wrong_profile_is_refused_not_consumed(tmp_path):
    """The measured case: a DRAM-backed default capture against a part that
    declares `Sram_Only` (no DRAM at all) used to be consumed as `basis:
    "bench", confidence: "certain"` -- overstating resident SRAM by 5.3x in
    the review's own real-metadata measurement."""
    _u55_tree(tmp_path, memory_mode="Sram_Only")
    baseline = _check(tmp_path)
    _write_perf_point(tmp_path, memory_mode="Dedicated_Sram_384KB")  # the wrong machine
    assert _check(tmp_path) == baseline                              # refused, not consumed


def test_a_lone_point_under_the_right_profile_still_answers(tmp_path):
    # The ordinary case must not regress: a single point captured under the
    # SAME profile the part declares still answers.
    _u55_tree(tmp_path, memory_mode="Sram_Only")
    _write_perf_point(tmp_path, memory_mode="Sram_Only")
    assert _check(tmp_path).basis == "bench"


def test_a_lone_point_when_the_part_declares_no_profile_still_answers(tmp_path):
    # Nothing to narrow ON: the SoC spec carries no `npu_toolchain.vela` block
    # at all, so the profile tiebreak must not reject the only point standing
    # -- narrowing on a silent part would be the opposite failure this fix
    # could introduce.
    _u55_tree(tmp_path, memory_mode=None)
    _write_perf_point(tmp_path, memory_mode="Sram_Only")
    assert _check(tmp_path).basis == "bench"


# ---------------------------------------------------------------------------
# THE MULTI-MATCH RULE: the toolchain PROFILE is a point's identity but not the
# match key, so a lookup can legitimately leave two points standing -- captured
# on machines that DIFFER. tan applies exactly one tiebreak, and it is a
# silicon fact: the profile the SoC spec's own `npu_toolchain` block declares.
# ---------------------------------------------------------------------------

def test_two_profiles_are_separated_by_the_profile_this_part_declares(tmp_path):
    """The collision that forced alp-sdk `f724d3e4` left the DRAM-backed
    capture as the arbitrary survivor -- exactly measured, describing a part
    with no DRAM. tan picks by the profile the PART declares
    (`npu_toolchain.vela.memory_mode`), so the surviving point is the one
    `tan model build` would compile this part under, not the one that sorted
    first."""
    _u55_tree(tmp_path, memory_mode="Sram_Only")
    _write_perf_point(tmp_path, memory_mode="Sram_Only", measured={
        "npu_ops": 1, "cpu_ops": 0, "arena_bytes": 32, "req_sram_kib": 1},
        filename="a@vela-5.1.0+r2+m55_hp+aaaaaaaaaaaa.json")
    _write_perf_point(tmp_path, memory_mode="Dedicated_Sram_384KB", measured={
        "npu_ops": 1, "cpu_ops": 0, "arena_bytes": 999999, "req_sram_kib": 0},
        filename="b@vela-5.1.0+r2+m55_hp+bbbbbbbbbbbb.json")
    rep = _check(tmp_path)
    assert rep.basis == "bench"
    assert rep.arena_bytes == 32                     # the module's profile
    assert rep.arena_bytes != 999999                 # ... not the DRAM-backed one
    assert "Sram_Only" in rep.notes[0]


def test_two_profiles_with_no_declared_part_profile_fall_through_rather_than_pick(tmp_path):
    # No `npu_toolchain.vela` on this SoC spec, so there is no silicon fact to
    # choose with. Two real measurements of two different machines stand, and
    # tan reports what it would have reported anyway.
    _u55_tree(tmp_path, memory_mode=None)
    baseline = _check(tmp_path)
    _write_perf_point(tmp_path, memory_mode="Sram_Only",
                      filename="a@vela-5.1.0+r2+m55_hp+aaaaaaaaaaaa.json")
    _write_perf_point(tmp_path, memory_mode="Dedicated_Sram_384KB",
                      filename="b@vela-5.1.0+r2+m55_hp+bbbbbbbbbbbb.json")
    assert _check(tmp_path) == baseline


def test_two_profiles_neither_matching_the_part_fall_through(tmp_path):
    # The part declares `Sram_Only`; both published points were captured under
    # something else. The tiebreak narrows to ZERO, which is a fall-through --
    # never "so take the first one".
    _u55_tree(tmp_path, memory_mode="Sram_Only")
    baseline = _check(tmp_path)
    _write_perf_point(tmp_path, memory_mode="Dedicated_Sram_384KB",
                      filename="a@vela-5.1.0+r2+m55_hp+aaaaaaaaaaaa.json")
    _write_perf_point(tmp_path, memory_mode="Shared_Sram",
                      filename="b@vela-5.1.0+r2+m55_hp+bbbbbbbbbbbb.json")
    assert _check(tmp_path) == baseline


def test_two_toolchain_versions_fall_through_rather_than_pick(tmp_path):
    # The profile tiebreak cannot separate these -- they share one -- and there
    # is no local toolchain to state a version with, so both stand and tan
    # falls through. Decision 1 keeps the version out of the query; the
    # multi-match rule is what makes that safe.
    _u55_tree(tmp_path)
    baseline = _check(tmp_path)
    _write_perf_point(tmp_path, version="5.1.0",
                      filename="a@vela-5.1.0+r2+m55_hp+aaaaaaaaaaaa.json")
    _write_perf_point(tmp_path, version="5.2.0",
                      filename="b@vela-5.2.0+r2+m55_hp+aaaaaaaaaaaa.json")
    assert _check(tmp_path) == baseline


def test_a_backend_with_no_named_toolchain_is_never_asked_for_a_point(tmp_path):
    # `cpu` has no entry in `_PERF_TOOLCHAIN` on purpose -- inventing a name
    # for a backend nobody benches is exactly the kind of authored fact this
    # tier forbids. Read off `tan.model.perf_apply` directly (tan-cli#791
    # round-2 review item 6, NIT (a)): `_PERF_TOOLCHAIN` lives there, and
    # `check.py` no longer keeps a `# noqa: F401` re-export alive purely so
    # this test could reach it through `check_mod`.
    assert "cpu" not in perf_apply_mod._PERF_TOOLCHAIN
    assert set(perf_apply_mod._PERF_TOOLCHAIN) == {"ethos_u", "drpai", "deepx_dxm1"}


# ---------------------------------------------------------------------------
# DECISION 1: a perf point vs the customer's OWN `--exact` compile.
# ---------------------------------------------------------------------------

def _fake_blob_compile(arena: int, sram: int, npu: int, cpu: int):
    def _compile(self, source, *, accel_config, out_dir, opts=None,
                 vela_memory_mode=None, vela_system_config=None,
                 vela_vendor_system_config=None,
                 vela_vendor_config_filename=None, soc_declares_dram=None):
        return Blob(format="vela_tflite", payload=b"x", arena_bytes=arena,
                    compiler_version="vela 5.1.0", req_sram_kib=sram,
                    cpu_op_count=cpu, npu_op_count=npu)
    return _compile


def test_a_bench_point_at_another_toolchain_version_is_still_valid_for_tier_2(tmp_path):
    """Decision 1, premise half: the point's toolchain version is NOT narrowed
    against whatever the customer has installed.

    It is OUR measurement, on OUR bench, and the tier exists for a customer who
    has no toolchain at all -- so there is no local version for it to differ
    from. The version it WAS measured at is put in the note instead, verbatim,
    so the customer can see it and judge."""
    _u55_tree(tmp_path)
    _write_perf_point(tmp_path, version="4.0.0", filename="t@vela-4.0.0.json")
    rep = _check(tmp_path)
    assert rep.basis == "bench"
    assert "vela 4.0.0" in rep.notes[0]


def test_an_agreeing_exact_compile_lets_the_bench_point_win_and_says_so(tmp_path, monkeypatch):
    """Decision 1, agreement half. The customer holds vela and compiled; the
    figures match ours exactly. The POINT wins, because it adds measured
    wall-clock latency and a traceable capture that no compile can produce --
    and the corroboration is said out loud rather than left implicit."""
    _u55_tree(tmp_path)
    _write_perf_point(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)
    monkeypatch.setattr(check_mod.VelaAdapter, "compile",
                        _fake_blob_compile(arena=32, sram=1, npu=1, cpu=0))
    rep = _check(tmp_path, exact=True)
    assert rep.basis == "bench"
    assert rep.npu_coverage == "fits"
    assert rep.latency_ms_mean == 0.4               # the figure only a bench run has
    assert "This host's own compile agrees with it." in rep.notes[0]


@pytest.mark.parametrize("blob_kwargs,expected_diff", [
    pytest.param(dict(arena=32, sram=1, npu=0, cpu=1),
                 "placement (bench fits, local compile cpu-only)", id="placement"),
    pytest.param(dict(arena=99999, sram=1, npu=1, cpu=0),
                 "arena (bench 32 bytes, local compile 99999 bytes)", id="arena"),
    pytest.param(dict(arena=32, sram=307, npu=1, cpu=0),
                 "SRAM (bench 1 KiB, local compile 307 KiB)", id="sram"),
])
def test_a_disagreeing_exact_compile_wins_over_the_bench_point(
        tmp_path, monkeypatch, blob_kwargs, expected_diff):
    """Decision 1, disagreement half -- and what "disagrees" means concretely.

    Defined on the three figures BOTH sides really produce: the placement
    verdict, the tensor arena, and the resident SRAM. When any of them differ,
    the customer's own compile is what gets reported: their vela profile may
    not be ours, and the number they can act on is the one their machine
    produced. Ours is still NAMED in a note, so nothing is hidden and the
    disagreement is visible rather than silently resolved."""
    _u55_tree(tmp_path)
    _write_perf_point(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)
    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _fake_blob_compile(**blob_kwargs))
    rep = _check(tmp_path, exact=True)
    assert rep.basis == "compiled"                  # --exact wins
    assert rep.confidence == "certain"
    note = next(n for n in rep.notes if n.startswith("--exact wins here"))
    assert expected_diff in note
    assert "alp-sdk-internal:bench/captures/tiny.log" in note      # ours is still named
    assert "\n" not in note
    # tan-cli#791 review MINOR 3: the point's own fields ride the report too,
    # not just the note's prose -- a consumer reading `perfRef`/`latencyMs*`
    # must not see `null` while the very same note quotes them in text.
    assert rep.perf_ref == "alp-sdk-internal:bench/captures/tiny.log"
    assert rep.latency_ms_mean == 0.4
    assert rep.latency_ms_p95 == 0.5
    assert rep.latency_runs == 100
    # the LOCAL compile's own numbers are still what's reported for the
    # figures that actually disagreed -- carrying the point's latency/perfRef
    # alongside must never overwrite the winning compile's own coverage/
    # arena/SRAM.
    assert rep.arena_bytes == blob_kwargs["arena"]
    assert rep.req_sram_kib == blob_kwargs["sram"]


def test_a_figure_only_one_side_reported_is_never_a_disagreement(tmp_path, monkeypatch):
    # Absence is not evidence here any more than anywhere else in this tier:
    # a point that omitted `req_sram_kib` does not disagree with a compile
    # that reported one.
    _u55_tree(tmp_path)
    _write_perf_point(tmp_path, measured={"npu_ops": 1, "cpu_ops": 0, "arena_bytes": 32})
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)
    monkeypatch.setattr(check_mod.VelaAdapter, "compile",
                        _fake_blob_compile(arena=32, sram=307, npu=1, cpu=0))
    rep = _check(tmp_path, exact=True)
    assert rep.basis == "bench"                     # agreement, not disagreement
    assert rep.req_sram_kib is None                 # ... and nothing was invented


def test_a_degraded_exact_run_still_gets_the_bench_point(tmp_path, monkeypatch):
    # `--exact` asked for, vela not on PATH: there is no local compile to
    # weigh against the point, so tier 2 answers -- which is strictly better
    # than the static screen the customer would otherwise have got.
    _u55_tree(tmp_path)
    _write_perf_point(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    rep = _check(tmp_path, exact=True)
    assert rep.basis == "bench"
    # tan-cli#791 review MINOR 4: the customer's own `--exact` request is not
    # silently un-answered just because a bench point also matched -- the
    # host-environment diagnostic ("vela is not on PATH") is still true of
    # THIS run and must survive the re-base onto the bench note, not just be
    # present when no point exists.
    assert any(n.startswith("--exact") and "vela is not on PATH" in n for n in rep.notes)
    # tan-cli#791 ROUND-2 review item 4: that survived note's own TAIL used to
    # say "reporting the static screen instead" right alongside a `basis:
    # "bench"` envelope -- a note whose own text contradicts the envelope it
    # rides on. The basis clause must not survive the re-base even though the
    # host fact in front of it does.
    assert not any("reporting the static screen instead" in n.lower() for n in rep.notes)
    assert not any("stays the static screen" in n.lower() for n in rep.notes)


def test_a_license_gated_note_survives_a_bench_match_without_its_false_tail(tmp_path):
    """tan-cli#791 round-2 review item 4, site 2 of 5
    (`_license_gated_exact_note`, reproduced live on E1M-V2N101 drpai in the
    review): the host fact ("dxcom is license-gated") survives a re-base
    onto a matched bench point; "this stays the static screen" must not,
    because the report is `basis: "bench"` by the time anyone reads it."""
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:multi", default_hw_rev="r2")
    _write_soc(tmp_path, "fake:soc:multi", [{"type": "dx", "subtype": "npu", "mac_per_cycle": 1}])
    onnx_source = tmp_path / "model.onnx"
    onnx_source.write_bytes(b"not-a-real-onnx-file")
    sha = hashlib.sha256(onnx_source.read_bytes()).hexdigest()
    _write_perf_point(tmp_path, backend="deepx_dxm1", accel_config="",
                      toolchain="dxcom", version="2.3.0", sha256=sha,
                      filename="model-aaaa@dxcom-2.3.0+r2+m55_hp+eeeeeeeeeeee.json")
    rep = check_model_backends(backends=["deepx_dxm1"], sku="E1M-FAKE", source=onnx_source,
                               metadata_root=tmp_path, exact=True)[0]
    assert rep.basis == "bench"
    assert any(n.startswith("--exact") and "license-gated" in n for n in rep.notes)
    assert not any("stays the static screen" in n.lower() for n in rep.notes)
    assert not any("reporting the static screen instead" in n.lower() for n in rep.notes)


def test_a_refused_footprint_note_survives_a_bench_match_without_its_false_tail(
        tmp_path, monkeypatch):
    """tan-cli#791 round-2 review item 4, site 3 of 5
    (`_footprint_refused_note`): vela's own clean refusal text is a HOST fact
    and survives; "Reporting the static screen instead." is a BASIS clause
    and must not."""
    _u55_tree(tmp_path)
    _write_perf_point(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)

    def _refuse(self, source, *, accel_config, out_dir, opts=None,
                vela_memory_mode=None, vela_system_config=None,
                vela_vendor_system_config=None,
                vela_vendor_config_filename=None, soc_declares_dram=None):
        _refuse_zero_sram_footprint(
            accel_config=accel_config, npu_ops=1, cpu_ops=0,
            used={"dram": 0.27}, system_config="Ethos_U55_High_End_Embedded",
            memory_mode="Sram_Only", defaulted=frozenset(),
            vendor_config_filename=None, soc_declares_dram=True)

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _refuse)
    rep = _check(tmp_path, exact=True)
    assert rep.basis == "bench"
    assert any(n.startswith("--exact:") and "vela compiled cleanly" in n for n in rep.notes)
    assert not any("reporting the static screen instead" in n.lower() for n in rep.notes)


def test_a_vela_failure_note_survives_a_bench_match_without_its_false_tail(tmp_path, monkeypatch):
    """tan-cli#791 round-2 review item 4, site 4 of 5 (`_maybe_exact_ethos_u`'s
    generic-exception branch): the compile failure itself is a HOST fact and
    survives; "reporting the static screen instead" is a BASIS clause and
    must not."""
    _u55_tree(tmp_path)
    _write_perf_point(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)

    def _boom(self, source, *, accel_config, out_dir, opts=None,
              vela_memory_mode=None, vela_system_config=None,
              vela_vendor_system_config=None,
              vela_vendor_config_filename=None, soc_declares_dram=None):
        raise RuntimeError("vela failed for ethos-u55-256: bad shape")

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _boom)
    rep = _check(tmp_path, exact=True)
    assert rep.basis == "bench"
    assert any(n.startswith("--exact") and "vela failed" in n for n in rep.notes)
    assert not any("reporting the static screen instead" in n.lower() for n in rep.notes)


def test_a_placement_unreadable_note_survives_a_bench_match_without_its_false_tail(
        tmp_path, monkeypatch):
    """tan-cli#791 round-2 review item 4, site 5 of 5
    (`_vela_placement_unreadable`): the "could not be read" fact is a HOST
    fact and survives; "reporting the static screen instead" is a BASIS
    clause and must not."""
    _u55_tree(tmp_path)
    _write_perf_point(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vela" if name == "vela" else None)

    def _fake_compile(self, source, *, accel_config, out_dir, opts=None,
                      vela_memory_mode=None, vela_system_config=None,
                      vela_vendor_system_config=None,
                      vela_vendor_config_filename=None, soc_declares_dram=None):
        return Blob(format="vela_tflite", payload=b"x", arena_bytes=100,
                    compiler_version="vela 5.1.0", req_sram_kib=1)   # no cpu/npu_op_count

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _fake_compile)
    rep = _check(tmp_path, exact=True)
    assert rep.basis == "bench"
    assert any(n.startswith("--exact") and "placement" in n and "could not be read" in n
               for n in rep.notes)
    assert not any("reporting the static screen instead" in n.lower() for n in rep.notes)


# ---------------------------------------------------------------------------
# THE `fits` GUARD, extended to the second surface ever permitted to say it.
#
# `basis: "bench"` joins `basis: "compiled"`. The guard is bound to the LIVE
# rule -- `tan.model.perf.coverage_from_placement`, the ONE function in tan
# that may return the word -- in the same style `_refuse` above binds to the
# live refusal template, and for the same reason: a hand-copied expectation
# stayed byte-identical to the template it came from while a reworded template
# walked "fits" onto the customer surface with the whole suite green.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("npu,cpu", [(1, 0), (44, 0), (999, 0)])
def test_only_a_measured_hundred_percent_placement_earns_the_word_fits(npu, cpu):
    assert coverage_from_placement(npu, cpu) == "fits"


@pytest.mark.parametrize("npu,cpu", [
    (1, 1), (43, 1), (0, 44), (0, 1),          # a real placement, but not 100%
    (None, 0), (44, None), (None, None),        # no readable placement at all
    (0, 0),                                      # ... including "both zero"
])
def test_nothing_short_of_that_earns_it(npu, cpu):
    assert coverage_from_placement(npu, cpu) != "fits"


def test_the_static_screen_engine_cannot_produce_the_word_at_all():
    """`analyze._coverage_label` is the ONLY thing that labels a static
    screen's coverage, and no mixture of verdicts makes it say `fits` -- the
    static screen has no evidence that could support it (an eligible operator
    still carries unchecked quantization/shape/dtype constraints)."""
    eligible = OpVerdict(op="CONV_2D", status="npu-eligible", reason="constraint-unchecked")
    certain = OpVerdict(op="TOPK_V2", status="cpu-certain", reason="op-not-in-table")
    unknown = OpVerdict(op="X", status="unknown", reason="no-table-for-backend")
    for verdicts in ([eligible], [certain], [eligible, certain], [unknown],
                     [eligible, unknown], [eligible, certain, unknown]):
        assert analyze_mod._coverage_label(verdicts) != "fits"


def test_fits_may_appear_at_basis_bench_and_may_not_at_basis_static_screen(tmp_path):
    """The end-to-end pair, through the production path both times: the SAME
    tree, the SAME model, and the only thing that differs is whether a point
    for this exact identity exists."""
    _u55_tree(tmp_path)
    screened = _check(tmp_path)
    assert screened.basis == "static-screen"
    assert screened.npu_coverage != "fits"
    assert not any("fits" in n.lower() for n in screened.notes)

    _write_perf_point(tmp_path)
    benched = _check(tmp_path)
    assert benched.basis == "bench"
    assert benched.npu_coverage == "fits"           # permitted here, and only here
    assert benched.ops == []                         # nothing fell back; no verdict to add
    assert benched.uncosted_cpu_op_count == 0


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
    # `rep.ops` carries the per-op `format-not-accepted` verdict only when the
    # ops were extracted at all -- on the bare install shape (`ci.yml`'s
    # `gates` job, no `model-io` extra) `rep.ops` is `[]` and the reason
    # assertion below degrades to `assert ([])`. Same guard the sibling
    # real-.tflite cases in this file already carry.
    pytest.importorskip("tflite", reason=_MODEL_IO_SKIP_REASON)
    backends = resolve_check_backends(sku, metadata_root=_META)
    assert backends            # at least drpai; V2M also carries deepx_dxm1
    reports = check_model_backends(backends=backends, sku=sku, source=_FIXTURE,
                                    metadata_root=_META, exact=False)
    for rep in reports:
        assert rep.npu_coverage == "undetermined"
        assert rep.ops and rep.ops[0].reason == "format-not-accepted"
        assert rep.npu_coverage != "cpu-only"


@pytestmark_real_sdk
def test_real_imx93_topology_is_a55_cluster_and_m33_only():
    """tan-cli#791 round-2 review item 1 / item 6 NIT (b): item 1's own fix
    grounded against the REAL E1M-NX9101/imx93 metadata, not only the
    synthetic `_imx93_shaped_tree` fixture above. Binds to the LIVE parsed
    set, never a hand-copied literal (the standing mutation-proof bar this
    branch has already been bitten by twice) -- read straight off the real,
    committed `metadata/e1m_modules/E1M-NX9101.yaml` `topology:` block."""
    topology = perf_apply_mod._topology_core_ids("E1M-NX9101", metadata_root=_META)
    assert topology == {"a55_cluster", "m33"}
    assert "m55_hp" not in topology              # a real Ensemble core, not an imx93 one
    assert "cortex_potato" not in topology        # names no core anywhere


@pytestmark_real_sdk
def test_real_imx93_declares_no_paired_core_for_its_lone_ethos_u65():
    """The other half of the real-metadata proof: imx93.json's `ethos-u65`
    entry really does state no `paired_core` -- confirming the exact-match
    query has nothing to narrow `core` on for this SKU, which is what makes
    E1M-NX9101 the shape where LEVEL 1 (`_topology_core_ids`) is the ONLY
    thing standing between a bogus core and a `basis: "bench"` report."""
    target = _headline_ethos_u_target("E1M-NX9101", _META)
    assert target is not None
    assert target.paired_core is None


@pytestmark_real_sdk
def test_real_imx93_through_tan_model_check_refuses_a_core_it_does_not_have(tmp_path):
    """The reviewer's OWN verification method (tan model check, the
    production path, not a unit test), reproduced here against a tmp copy of
    the REAL committed E1M-NX9101/imx93 metadata plus a synthetic perf point
    -- alp-sdk publishes no `metadata/model_perf/` tree yet, so this is the
    only way to run the full `check_model_backends` path against real
    SoM/SoC content end to end. Before this fix, a point claiming `core:
    "m55_hp"` -- verified in the review's own words, "a core the i.MX 93
    does NOT physically have" -- was consumed outright at `basis: "bench",
    confidence: "certain"` with `arenaBytes 999999`/`reqSramKib 777`; this
    proves it no longer is, against the real files rather than a hand-typed
    stand-in for their shape.

    Deliberately does NOT copy a real `npu_ops/ethos_u/*.json` table -- that
    filename is SDK-version-sensitive (it has moved since `PINNED_SDK_
    COMMIT`) and this test's whole point is the tier-2 perf-point refusal,
    not the tier-1 static-screen table lookup; `analyze_backend` degrades
    to `_no_table_report` (still `basis: "static-screen"`) with no table at
    all, which is exactly the baseline this test needs."""
    (tmp_path / "e1m_modules").mkdir()
    shutil.copy(_META / "e1m_modules" / "E1M-NX9101.yaml",
                tmp_path / "e1m_modules" / "E1M-NX9101.yaml")
    (tmp_path / "socs" / "nxp" / "imx9").mkdir(parents=True)
    shutil.copy(_META / "socs" / "nxp" / "imx9" / "imx93.json",
                tmp_path / "socs" / "nxp" / "imx9" / "imx93.json")
    backends = resolve_check_backends("E1M-NX9101", metadata_root=tmp_path)
    assert backends == ["ethos_u"]

    def _screen():
        return check_model_backends(backends=backends, sku="E1M-NX9101", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=False, hw_rev="r1")[0]

    baseline = _screen()
    _write_perf_point(tmp_path, sku="E1M-NX9101", hw_rev="r1", core="m55_hp",
                      backend="ethos_u", accel_config="ethos-u65-256",
                      measured={"npu_ops": 1, "cpu_ops": 0, "arena_bytes": 999999,
                                "req_sram_kib": 777},
                      filename="tiny-int8-aaaa@vela-5.1.0+r1+m55_hp+aaaaaaaaaaaa.json")
    assert _screen() == baseline                  # refused, not the bogus 999999/777
