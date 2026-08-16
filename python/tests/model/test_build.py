# SPDX-License-Identifier: Apache-2.0
"""build_model: resolve targets -> run available adapters -> write .alpmodel.

`build_model` (via `tan.model.targets.resolve_targets`) pulls
`resolve_soc_path` from the leaf module `tan.soc_ref` (ADR-0028 Task 2
follow-up decoupling: `tan.model` no longer imports `tan.planner`, see
`test_model_package_imports.py`) -- so `from tan.model.build import
build_model` imports fine with no SDK bound at all. What these tests need a
bound, real alp-sdk checkout for is the DATA, not the import: `_META` must
resolve real, committed SoM presets (`E1M-AEN801.yaml`, `E1M-V2M101.yaml`,
...) and SoC JSON under `metadata/socs/` -- a throwaway fixture tree has none
of that content. Bind + resolve happen together, at module scope, guarded by
the same `SDK is None` check `pytestmark` skips on."""
import shutil
from pathlib import Path

import pytest

from tan.model.adapters import CompilerAdapter, Blob
from tan.model.adapters.cpu import CpuAdapter
from tan.model.adapters.ethos_u import VelaAdapter, VelaFootprintRefused
from tan.model.adapters.executorch import ExecutorchAdapter
from tan.model.package import read_package
from tan.planner_root import bind_sdk_root
from tests.conftest import sdk_root

SDK = sdk_root()

#: `tan-cli`'s OWN committed fixtures, not the bound SDK's -- these are the
#: two whose REAL vela behaviour the tests below assert, and they must not
#: change under a test when `ALP_SDK_ROOT` is repointed.
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "models"
_TINY_INT8 = _FIXTURES / "tiny_int8.tflite"     # 1 op, 1/1 on the NPU at every config
_FLOAT32_FC = _FIXTURES / "float32_fc.tflite"   # float32: 0/1 on the NPU at every config

#: The tests below spawn the REAL compiler -- monkeypatching cannot prove what
#: vela actually reports for a real NPU-placing compile, which is the whole
#: claim. A host without `ethos-u-vela` skips rather than pretending.
_needs_vela = pytest.mark.skipif(shutil.which("vela") is None,
                                 reason="vela (ethos-u-vela) not installed")

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- build_model imports fine standalone, but these "
           "tests assert against real committed SoM presets and SoC JSON "
           "that only exist in a bound metadata/ tree.",
)

if SDK is not None:
    bind_sdk_root(SDK)
    from tan.model.build import build_model
    _META = SDK / "metadata"
else:
    build_model = None  # unreachable: every test in this module is skipped
    _META = None


def test_build_model_writes_alpmodel_with_cpu_blob_and_coverage(tmp_path):
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-DUMMY")
    # Inject only the CPU adapter so the result is independent of which compiler
    # toolchains happen to be installed on the build host.
    out = build_model(sku="E1M-AEN801", name="demo", source=src, out_dir=tmp_path,
                      metadata_root=_META, adapters=[CpuAdapter()])
    assert out == tmp_path / "demo.alpmodel"
    mft, blobs = read_package(out.read_bytes())
    assert mft.name == "demo"
    cpu = [t for t in mft.targets if t.backend == "cpu"]
    assert len(cpu) == 1
    assert blobs[cpu[0].blob] == b"TFL3-DUMMY"
    # Ethos-U has no injected adapter -> recorded as coverage skips (all E8 variants).
    ethos_u_skips = [c for c in mft.coverage if c.backend == "ethos_u" and c.status == "skipped"]
    assert len(ethos_u_skips) == 3
    assert {c.accel_config for c in ethos_u_skips} == {
        "ethos-u85-256", "ethos-u55-256", "ethos-u55-128"
    }


def test_build_model_errors_when_no_blob_compiled(tmp_path):
    # Unsupported source format: CpuAdapter rejects .pt, no other adapter -> no blob.
    src = tmp_path / "m.pt"
    src.write_bytes(b"PYTORCH")
    with pytest.raises(ValueError, match="no blob compiled"):
        build_model(sku="E1M-AEN801", name="demo", source=src, out_dir=tmp_path,
                    metadata_root=_META, adapters=[CpuAdapter()])


def test_build_model_records_unavailable_tool_as_skip(tmp_path):
    # An adapter exists for ethos_u but its tool is "not installed" -> coverage skip,
    # and its compile() must never be called.
    class _Unavail(CompilerAdapter):
        backend = "ethos_u"

        def is_available(self):
            return False

        def accepts(self, src_format):
            return src_format == "tflite"

        def compile(self, source, *, accel_config, out_dir):
            raise AssertionError("compile() must not run for an unavailable adapter")

    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-DUMMY")
    out = build_model(sku="E1M-AEN801", name="demo", source=src, out_dir=tmp_path,
                      metadata_root=_META, adapters=[CpuAdapter(), _Unavail()])
    mft, _ = read_package(out.read_bytes())
    ethos_u_skips = [c for c in mft.coverage
                     if c.backend == "ethos_u" and c.status == "skipped"]
    assert len(ethos_u_skips) == 3                      # u85 plus both u55 accel-config variants
    assert all("not installed" in c.reason for c in ethos_u_skips)


def test_build_model_default_registry_reaches_executorch_adapter_for_pte_source(tmp_path):
    # #1260 reachability: `alp model build` (scripts/alp_cli/model.py) calls
    # build_model() with NO adapters= kwarg -- the default registry. Before this
    # fix, ExecutorchAdapter existed but was never in _ADAPTERS, and even adding
    # it there naively would have collided with CpuAdapter on the "cpu" backend
    # key. Exercise the real default path, not an injected adapter list.
    src = tmp_path / "m.pte"
    src.write_bytes(b"PTE-DUMMY-PROGRAM")
    out = build_model(sku="E1M-AEN801", name="demo", source=src, out_dir=tmp_path,
                      metadata_root=_META)   # default registry
    mft, blobs = read_package(out.read_bytes())
    cpu = [t for t in mft.targets if t.backend == "cpu"]
    assert len(cpu) == 1
    assert cpu[0].blob_format == "executorch"
    assert blobs[cpu[0].blob] == b"PTE-DUMMY-PROGRAM"
    # ethos_u (VelaAdapter) still gets a coverage entry, not silently absorbed
    # as "no adapter registered" (its reason is host-dependent: "not installed"
    # if vela isn't on PATH here, else "does not accept .pte").
    ethos_u = [c for c in mft.coverage if c.backend == "ethos_u"]
    assert ethos_u and all(c.status in ("skipped", "incompatible") for c in ethos_u)


def test_build_model_cpu_backend_adapters_tflite_source_uses_cpu_adapter(tmp_path):
    # Guards the by_backend grouping: registering ExecutorchAdapter must not
    # steal the "cpu" backend key from CpuAdapter for an ordinary .tflite build
    # (a naive `{a.backend: a for a in registry}` dict would let the later
    # entry in _ADAPTERS silently win here).
    #
    # Pinned to just the two "cpu"-tier adapters -- NOT adapters=None (the
    # real default registry, as the sibling .pte test above deliberately
    # uses). The default registry also carries VelaAdapter, which DOES
    # accept .tflite and IS invoked for real whenever `vela` happens to be on
    # PATH; the dummy `TFL3-DUMMY` bytes below are not a parseable
    # flatbuffer, so a present vela genuinely fails to compile them and
    # build_model() (which does not catch adapter.compile() exceptions --
    # a real compile failure is meant to fail the build loudly, not vanish
    # into a coverage skip) raises -- flipping this test red only on hosts
    # that happen to have vela installed (tan-cli#784). This test's
    # only claim is the cpu-vs-cpu backend-key collision, so its registry is
    # pinned to reproduce exactly that collision, deterministically, on every
    # host; default-registry reachability is the sibling test's job above,
    # and ethos_u's own real-tool behaviour belongs to test_adapters.py /
    # test_vela_yolo_internal.py.
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-DUMMY")
    out = build_model(sku="E1M-AEN801", name="demo", source=src, out_dir=tmp_path,
                      metadata_root=_META, adapters=[CpuAdapter(), ExecutorchAdapter()])
    mft, blobs = read_package(out.read_bytes())
    cpu = [t for t in mft.targets if t.backend == "cpu"]
    assert len(cpu) == 1
    assert cpu[0].blob_format == "tflite"
    assert blobs[cpu[0].blob] == b"TFL3-DUMMY"


def test_build_model_v2m101_records_drpai_and_deepx_skips(tmp_path, monkeypatch):
    # With the default registry, V2M101 has drpai (host) + deepx_dxm1 (on-module) targets;
    # both require compile opts which aren't provided -> "no compile config" skips;
    # cpu still compiles.
    monkeypatch.delenv("ALP_DRPAI_TVM_HOME", raising=False)
    monkeypatch.delenv("ALP_DEEPX_SDK_HOME", raising=False)
    monkeypatch.setattr("tan.model.adapters.deepx.shutil.which", lambda n: None)
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-DUMMY")
    out = build_model(sku="E1M-V2M101", name="demo", source=src,
                      out_dir=tmp_path, metadata_root=_META)   # default registry
    mft, _ = read_package(out.read_bytes())
    skipped = {c.backend for c in mft.coverage if c.status == "skipped"}
    assert "drpai" in skipped
    assert "deepx_dxm1" in skipped              # resolver folded it in (Task 1)
    assert any(t.backend == "cpu" for t in mft.targets)


def test_build_model_skips_backend_missing_compile_config(tmp_path):
    # An adapter that requires compile opts + none provided -> coverage skip, compile() never called.
    # Use V2M101 (has drpai target); tflite source so CpuAdapter produces a blob.
    class _NeedsOpts(CompilerAdapter):
        backend = "drpai"
        requires_compile_opts = True
        def is_available(self): return True
        def accepts(self, src_format): return src_format == "tflite"
        def compile(self, source, *, accel_config, out_dir, opts=None, silicon_ref=None):
            raise AssertionError("must not compile without opts")
    src = tmp_path / "m.tflite"; src.write_bytes(b"TFL3-X")
    out = build_model(sku="E1M-V2M101", name="demo", source=src, out_dir=tmp_path,
                      metadata_root=_META, adapters=[CpuAdapter(), _NeedsOpts()])
    mft, _ = read_package(out.read_bytes())
    skips = [c for c in mft.coverage if c.backend == "drpai"]
    assert skips and all(c.status == "skipped" and "no compile config" in c.reason for c in skips)


def test_build_model_passes_compile_opts_to_adapter(tmp_path):
    # Use V2M101 (has drpai target); tflite source so CpuAdapter also produces a blob.
    seen = {}
    class _Capture(CompilerAdapter):
        backend = "drpai"
        requires_compile_opts = True
        def is_available(self): return True
        def accepts(self, src_format): return src_format == "tflite"
        def compile(self, source, *, accel_config, out_dir, opts=None, silicon_ref=None):
            seen["opts"] = opts
            return Blob(format="drpai_dir", payload=b"RT", arena_bytes=0)
    src = tmp_path / "m.tflite"; src.write_bytes(b"TFL3-X")
    build_model(sku="E1M-V2M101", name="demo", source=src, out_dir=tmp_path,
                metadata_root=_META, adapters=[CpuAdapter(), _Capture()],
                compile_opts={"drpai": {"spec": "/abs/p.yaml"}})
    assert seen["opts"] == {"spec": "/abs/p.yaml"}


# --------------------------------------------------------------------------
# #1125: `name` reaches `out_dir / f"{name}.alpmodel"` directly -- build_model
# is called by non-CLI callers too (this file), so the guard lives here, not
# only behind the CLI's board.schema.json validation.
# --------------------------------------------------------------------------

def test_build_model_rejects_traversal_in_name(tmp_path):
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-DUMMY")
    out_dir = tmp_path / "out"
    with pytest.raises(ValueError, match="invalid model name"):
        build_model(sku="E1M-AEN801", name="../../../../tmp/evil", source=src,
                    out_dir=out_dir, metadata_root=_META, adapters=[CpuAdapter()])
    assert not out_dir.exists()


def test_build_model_rejects_absolute_name(tmp_path):
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-DUMMY")
    out_dir = tmp_path / "out"
    with pytest.raises(ValueError, match="invalid model name"):
        build_model(sku="E1M-AEN801", name="/etc/passwd", source=src,
                    out_dir=out_dir, metadata_root=_META, adapters=[CpuAdapter()])


def test_build_model_valid_name_still_writes_in_out_dir(tmp_path):
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-DUMMY")
    out_dir = tmp_path / "out"
    out = build_model(sku="E1M-AEN801", name="demo-model_1", source=src,
                      out_dir=out_dir, metadata_root=_META, adapters=[CpuAdapter()])
    assert out == out_dir / "demo-model_1.alpmodel"
    assert out.is_file()


# --------------------------------------------------------------------------
# tan-cli#789 review: a refused target costs its SIBLINGS nothing.
# --------------------------------------------------------------------------

@_needs_vela
def test_a_refused_target_does_not_take_the_rest_of_the_package_with_it(tmp_path):
    """THE BLOCKER-1 GUARD, through a REAL vela process.

    Measured, `ethos-u-vela` 5.1.0 over the committed `tiny_int8.tflite`:
    `E1M-AEN801` (Alif Ensemble E8) resolves to `ethos-u85-256`,
    `ethos-u55-256`, `ethos-u55-128` and `cpu`. The u85 compile places 1/1
    operators on the NPU but reports its working set in DRAM under vela's
    built-in `Ethos_U85_SYS_DRAM_Mid`, which tan refuses -- and with no guard
    in `build_model`, that ONE refusal propagated out of the loop and aborted
    the whole build: `BUILD FAILED RuntimeError`, **no package written at
    all**, with the other three targets compiling perfectly (`arena 32, SRAM
    1 KiB` on both u55 configs). The same held for `E1M-AEN401`,
    `E1M-AEN601`, and for `E1M-NX9101` at `ethos-u65-256`.

    Both halves matter: the survivors must be PRESENT, and the refused target
    must be legibly ABSENT with its reason -- never silently present carrying
    the zero footprint the refusal exists to stop."""
    src = tmp_path / _TINY_INT8.name
    shutil.copy(_TINY_INT8, src)
    out = build_model(sku="E1M-AEN801", name="tiny", source=src, out_dir=tmp_path,
                      metadata_root=_META)          # default registry: real vela + cpu
    mft, blobs = read_package(out.read_bytes())

    # The siblings survived, with real footprints.
    survivors = {t.accel_config: t for t in mft.targets if t.backend == "ethos_u"}
    assert set(survivors) == {"ethos-u55-256", "ethos-u55-128"}
    for target in survivors.values():
        assert target.arena > 0 and target.requires["sram_kib"] > 0
        assert blobs[target.blob][4:8] == b"TFL3"
    assert any(t.backend == "cpu" for t in mft.targets)

    # The u85 target is absent from targets[] and RECORDED, with its reason.
    assert "ethos-u85-256" not in {t.accel_config for t in mft.targets}
    refused = [c for c in mft.coverage
               if c.backend == "ethos_u" and c.accel_config == "ethos-u85-256"]
    assert len(refused) == 1
    assert refused[0].status == "skipped"
    assert "reported 0 KiB SRAM" in refused[0].reason
    assert "Ethos_U85_SYS_DRAM_Mid" in refused[0].reason
    # ... and what to do about it. E1M-AEN801's preset is `silicon:
    # alif:ensemble:e8`, so this SKU is exactly where the proprietary-profile
    # pointer belongs (tan-cli#789 review (g); the NXP counterpart is pinned
    # by `test_an_nxp_refusal_never_sends_the_reader_to_an_alif_file`).
    assert "ensemble_vela.ini" in refused[0].reason


@_needs_vela
def test_an_nxp_refusal_never_sends_the_reader_to_an_alif_file(tmp_path):
    """THE (g) GUARD, end to end through a REAL vela process.

    `E1M-NX9101` is an NXP i.MX 93. Measured, `ethos-u-vela` 5.1.0 over the
    committed `tiny_int8.tflite`, its single `ethos-u65-256` target refuses
    with the profile it really resolved (`Ethos_U65_Client_Server /
    Dedicated_Sram_384KB`, `dram 0.11 KiB`) -- and then, until this fix, the
    remedy sentence told that customer the profile "lives in the proprietary
    ensemble_vela.ini", an Alif Ensemble file that has nothing to do with
    their silicon (alp-sdk's own i.MX 93 vela invocation involves no
    proprietary `.ini` at all -- `vendors/nxp-imx93/README.md`). Naming the
    profile per run (MAJOR 3) fixed only the first half of the sentence.

    The `cpu` target still ships, which is the other half of the per-target
    contract: one refused accelerator target must never empty the package."""
    src = tmp_path / _TINY_INT8.name
    shutil.copy(_TINY_INT8, src)
    out = build_model(sku="E1M-NX9101", name="tiny", source=src, out_dir=tmp_path,
                      metadata_root=_META)          # default registry: real vela + cpu
    mft, _ = read_package(out.read_bytes())
    assert [t.backend for t in mft.targets] == ["cpu"]

    refused = [c for c in mft.coverage if c.accel_config == "ethos-u65-256"]
    assert len(refused) == 1 and refused[0].status == "skipped"
    reason = refused[0].reason
    assert "Ethos_U65_Client_Server" in reason              # the profile it DID resolve
    assert "ensemble_vela.ini" not in reason               # ... and no Alif file after it
    assert "Alif" not in reason and "alif" not in reason
    # The part-independent remedy is intact for this SKU too.
    assert "No module vela profile was supplied and tan cannot pass one yet." in reason
    assert "`tan model build` skips this target and still builds the SKU's others." in reason


@_needs_vela
def test_every_target_refusing_is_an_error_not_an_empty_package(tmp_path):
    """The deliberate decision for "what if they ALL refuse".

    A `.alpmodel` with no runnable blob is worse than an error: nothing fails
    until the device tries to load it. So the existing zero-blob guard stands
    -- per-target skipping is about not losing the targets that WORKED, never
    about shipping a package with none.

    `E1M-NX9101` declares exactly one NPU (`ethos-u65-256`, which refuses on
    this fixture) so restricting the registry to vela alone leaves nothing at
    all, and the refusal's own text must survive into the failure so the
    reader learns WHY rather than just "no blob compiled"."""
    src = tmp_path / _TINY_INT8.name
    shutil.copy(_TINY_INT8, src)
    with pytest.raises(ValueError) as exc:
        build_model(sku="E1M-NX9101", name="tiny", source=src, out_dir=tmp_path,
                    metadata_root=_META, adapters=[VelaAdapter()])
    msg = str(exc.value)
    assert "no blob compiled" in msg
    assert "ethos-u65-256" in msg and "Ethos_U65_Client_Server" in msg
    assert not list(tmp_path.glob("*.alpmodel"))         # nothing half-written


@_needs_vela
def test_a_zero_npu_placement_target_is_skipped_not_shipped_as_a_zero(tmp_path):
    """THE MINOR-7 GUARD, through a REAL vela process.

    `float32_fc.tflite` is float32, so vela places 0/1 operators on the NPU at
    every accel config and exits 0 with a genuine 0 KiB footprint -- correct,
    and deliberately NOT a refusal (a full CPU fallback really does need no
    arena; `tan model check --exact` reports it as `cpu-only`). But writing it
    into the package as an `ethos_u` target is the same defect from the other
    side: measured, `E1M-AEN801` shipped THREE targets at `arena=0
    requires={'sram_kib': 0}`, the exact "fits any envelope" shape alp-sdk's
    selector waves through (`e->arena_sram_kib == 0u || t->req_sram_kib <=
    e->arena_sram_kib`, `src/backends/inference/alp_model_select.c`), so a
    board could select an NPU blob that runs every operator on the CPU anyway.

    An accelerator target with no accelerator placement is dropped to a
    coverage skip; the `cpu` target -- which is what actually runs this model
    -- stays."""
    src = tmp_path / _FLOAT32_FC.name
    shutil.copy(_FLOAT32_FC, src)
    out = build_model(sku="E1M-AEN801", name="fc", source=src, out_dir=tmp_path,
                      metadata_root=_META)          # default registry: real vela + cpu
    mft, _ = read_package(out.read_bytes())

    assert [t.backend for t in mft.targets] == ["cpu"]
    assert not [t for t in mft.targets if t.requires["sram_kib"] == 0
                and t.backend != "cpu"]
    dropped = {c.accel_config: c for c in mft.coverage if c.backend == "ethos_u"}
    assert set(dropped) == {"ethos-u85-256", "ethos-u55-256", "ethos-u55-128"}
    for cov in dropped.values():
        assert cov.status == "skipped"
        assert "placed 0 of 1 operators" in cov.reason
        assert "The cpu target runs this model." in cov.reason


def test_only_the_footprint_refusal_is_absorbed_a_real_compile_failure_still_fails(tmp_path):
    """The scope of the per-target guard, stated from the other side.

    `build_model` catches `VelaFootprintRefused` and NOTHING else: a toolchain
    that crashed, timed out or produced no artifact is a broken build, not a
    coverage line. Widening the `except` to `Exception` would turn every one
    of those into a silent skip, which is how a customer gets a package that
    is quietly missing the target they bought the module for."""
    class _Refuses(CompilerAdapter):
        backend = "ethos_u"
        def is_available(self): return True
        def accepts(self, src_format): return src_format == "tflite"
        def compile(self, source, *, accel_config, out_dir, opts=None, silicon_ref=None):
            raise VelaFootprintRefused(f"refused {accel_config}")

    class _Crashes(_Refuses):
        def compile(self, source, *, accel_config, out_dir, opts=None, silicon_ref=None):
            raise RuntimeError(f"vela failed for {accel_config}: segmentation fault")

    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-DUMMY")
    out = build_model(sku="E1M-AEN801", name="ok", source=src, out_dir=tmp_path,
                      metadata_root=_META, adapters=[CpuAdapter(), _Refuses()])
    mft, _ = read_package(out.read_bytes())
    assert {c.reason for c in mft.coverage if c.backend == "ethos_u"} == {
        "refused ethos-u85-256", "refused ethos-u55-256", "refused ethos-u55-128"}

    with pytest.raises(RuntimeError, match="segmentation fault"):
        build_model(sku="E1M-AEN801", name="boom", source=src, out_dir=tmp_path,
                    metadata_root=_META, adapters=[CpuAdapter(), _Crashes()])


def test_an_unknown_accelerator_placement_is_not_treated_as_zero(tmp_path):
    """`npu_op_count is None` means "this compiler did not report a
    placement", which is not the same fact as "it placed nothing" -- every
    adapter but vela leaves it None today, and vela itself leaves it None when
    its own summary could not be parsed (`_parse_vela_placement`). Dropping
    those targets would silently empty the package for DRP-AI and DEEPX, whose
    adapters have never reported a placement at all."""
    class _Silent(CompilerAdapter):
        backend = "drpai"
        def is_available(self): return True
        def accepts(self, src_format): return src_format == "tflite"
        def compile(self, source, *, accel_config, out_dir, opts=None, silicon_ref=None):
            return Blob(format="drpai_dir", payload=b"RT", arena_bytes=4096,
                        req_sram_kib=4)             # npu_op_count stays None

    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-X")
    out = build_model(sku="E1M-V2M101", name="demo", source=src, out_dir=tmp_path,
                      metadata_root=_META, adapters=[CpuAdapter(), _Silent()])
    mft, _ = read_package(out.read_bytes())
    assert [t.backend for t in mft.targets if t.backend == "drpai"] == ["drpai"]
