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
from pathlib import Path

import pytest

from tan.model import check as check_mod
from tan.model.adapters import Blob
from tan.model.check import (
    _headline_ethos_u_accel_config,
    check_model_backends,
    resolve_check_backends,
)
from tests.conftest import sdk_root

SDK = sdk_root()

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "models" / "tiny_int8.tflite"


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


def test_check_model_backends_reports_format_not_accepted_never_cpu_only(tmp_path):
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
    _write_som(tmp_path, "E1M-FAKE", "fake:soc:u55", ethos_u_variant="u55")
    _write_soc(tmp_path, "fake:soc:u55", [{"type": "ethos-u55", "subtype": "x", "mac_per_cycle": 256}])
    with pytest.raises(OSError):
        check_model_backends(backends=["ethos_u"], sku="E1M-FAKE",
                              source=tmp_path / "missing.tflite", metadata_root=tmp_path, exact=False)


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
                    compiler_version="vela 3.9.0", req_sram_kib=4)

    monkeypatch.setattr(check_mod.VelaAdapter, "compile", _fake_compile)
    reports = check_model_backends(backends=["ethos_u"], sku="E1M-FAKE", source=_FIXTURE,
                                    metadata_root=tmp_path, exact=True)
    rep = reports[0]
    assert rep.basis == "compiled"
    assert rep.confidence == "certain"
    assert rep.npu_coverage == "fits"          # the ONLY basis allowed to say this
    assert rep.ops == []
    assert "vela 3.9.0" in rep.notes[0]
    assert "123" in rep.notes[0] and "4" in rep.notes[0]


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
