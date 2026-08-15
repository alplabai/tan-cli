# tests/scripts/test_alp_model_build.py
"""build_model: resolve targets -> run available adapters -> write .alpmodel.

`build_model` (via `tan.model.targets.resolve_targets`) pulls
`tan.planner.som_metadata.resolve_soc_path` -- the one external symbol the
relocated engine needs (ADR-0028 Task 2) -- which makes `tan.model.build` a
submodule of the `tan.planner` PACKAGE. `tan.planner`'s `__init__` reads real
`metadata/registries/*` content at IMPORT time (`tan/planner_root.py`), so
`from tan.model.build import build_model` cannot even be evaluated before a
REAL alp-sdk checkout is bound -- not just to resolve real SoM presets below
(`_META`), but to import at all. Bind + import happen together, at module
scope, guarded by the same `SDK is None` check `pytestmark` skips on, so an
unbound run never reaches the import line -- matching
`tests/planner/test_sdk_capability.py`'s documented reason for the same
shape."""
import pytest

from tan.model.adapters import CompilerAdapter, Blob
from tan.model.adapters.cpu import CpuAdapter
from tan.model.package import read_package
from tan.planner_root import bind_sdk_root
from tests.conftest import sdk_root

SDK = sdk_root()

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- build_model needs both a real metadata/ tree (SoM "
           "presets, SoC JSON) and a bound tan.planner to import at all.",
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


def test_build_model_default_registry_tflite_source_still_uses_cpu_adapter(tmp_path):
    # Guards the by_backend grouping: registering ExecutorchAdapter must not
    # steal the "cpu" backend key from CpuAdapter for an ordinary .tflite build
    # (a naive `{a.backend: a for a in registry}` dict would let the later
    # entry in _ADAPTERS silently win here).
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-DUMMY")
    out = build_model(sku="E1M-AEN801", name="demo", source=src, out_dir=tmp_path,
                      metadata_root=_META)   # default registry
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
        def compile(self, source, *, accel_config, out_dir, opts=None):
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
        def compile(self, source, *, accel_config, out_dir, opts=None):
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
