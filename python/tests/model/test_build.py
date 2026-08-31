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
import json
import shutil
from pathlib import Path

import pytest

from tan.model.adapters import CompilerAdapter, Blob
from tan.model.adapters.cpu import CpuAdapter
from tan.model.adapters.ethos_u import VelaAdapter, VelaFootprintRefused
from tan.model.adapters.executorch import ExecutorchAdapter
from tan.model.package import read_manifest_file, read_package
from tan.model.targets import resolve_targets
from tan.planner_root import bind_sdk_root
from tests.conftest import needs_sdk_vela_profile, sdk_root

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


def _meta_without_a_vela_memory_mode(tmp_path: Path, sku: str) -> Path:
    """A copy of the BOUND metadata/ tree with `npu_toolchain.vela.memory_mode`
    deleted from every SoC spec -- i.e. the real presets and the real SoC JSON,
    minus the one key under test.

    Why this exists. Until alp-sdk #1470 every `ethos_u` compile went out
    flagless, vela fell back to its DRAM-backed built-in profile, and the
    three refusal guards below fired against the REAL shipped SKUs. They no
    longer do: `E1M-AEN401`/`E1M-AEN601`/`E1M-AEN801` now resolve
    `--memory-mode Sram_Only` and `E1M-NX9101` `--memory-mode Shared_Sram`
    straight out of `metadata/socs/**`'s `npu_toolchain.vela`, so nothing in
    the shipped catalogue reports 0 KiB SRAM on a real NPU placement any more
    (measured, `ethos-u-vela` 5.1.0 over both committed fixtures -- see
    `test_the_soms_memory_mode_makes_the_refused_target_ship_at_all`).

    That is the fix working, and it is NOT a licence to delete the guards:
    `tan.model.targets._vela_profile` resolves no memory mode for any SoC spec
    that declares none, which is exactly what a part whose profile is still TBD
    gets (the plan's own rule -- an unsourced profile is never invented, it is
    simply not passed). Deleting THAT ONE KEY reproduces that condition against
    the real presets, so these keep running the REAL vela process through the
    REAL `build_model` and keep proving what they were written to prove.

    Only `memory_mode` is dropped, not the whole `npu_toolchain` block, and the
    difference is load-bearing: the block also carries
    `vendor_config_filename`, which is where the refusal's vendor clause now
    comes from (alp-sdk #1470 Task 4), and `external_memory_interfaces` -- the
    source of its no-DRAM evidence -- is untouched here entirely. Stripping the
    block wholesale would silently delete both clauses from the very messages
    these tests read. A part with a declared vendor file and a TBD memory mode
    is also the realistic shape: which `.ini` a vendor ships is known long
    before anyone measures the part's memory model.

    The whole `socs/` tree is copied, not just the host SoC: `resolve_targets`
    globs it for on-module discrete accelerators."""
    root = tmp_path / "metadata-no-vela-profile"
    (root / "e1m_modules").mkdir(parents=True, exist_ok=True)
    shutil.copy(_META / "e1m_modules" / f"{sku}.yaml", root / "e1m_modules" / f"{sku}.yaml")
    for src_json in sorted((_META / "socs").glob("**/*.json")):
        dst = root / "socs" / src_json.relative_to(_META / "socs")
        dst.parent.mkdir(parents=True, exist_ok=True)
        soc = json.loads(src_json.read_text(encoding="utf-8"))
        vela = soc.get("npu_toolchain", {}).get("vela")
        if isinstance(vela, dict):
            vela.pop("memory_mode", None)
        dst.write_text(json.dumps(soc), encoding="utf-8")
    return root


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
        def compile(self, source, *, accel_config, out_dir, opts=None,
                    vela_memory_mode=None, vela_system_config=None,
                    vela_vendor_system_config=None,
                    vela_vendor_config_filename=None, soc_declares_dram=None):
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
        def compile(self, source, *, accel_config, out_dir, opts=None,
                    vela_memory_mode=None, vela_system_config=None,
                    vela_vendor_system_config=None,
                    vela_vendor_config_filename=None, soc_declares_dram=None):
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
    the zero footprint the refusal exists to stop.

    Driven through `_meta_without_a_vela_memory_mode` since alp-sdk #1470: the
    real `E1M-AEN801` now resolves `--memory-mode Sram_Only` and its u85
    target ships (`sram_memory_used = 0.03125`), so a SoC spec with no
    `npu_toolchain.vela` is where a real vela process still produces the zero
    this guard is about. See that helper for why the guard is kept rather than
    deleted."""
    src = tmp_path / _TINY_INT8.name
    shutil.copy(_TINY_INT8, src)
    out = build_model(sku="E1M-AEN801", name="tiny", source=src, out_dir=tmp_path,
                      # default registry: real vela + cpu
                      metadata_root=_meta_without_a_vela_memory_mode(tmp_path, "E1M-AEN801"))
    mft, blobs = read_package(out.read_bytes())

    # The siblings survived, with real footprints.
    survivors = {t.accel_config: t for t in mft.targets if t.backend == "ethos_u"}
    assert set(survivors) == {"ethos-u55-256", "ethos-u55-128"}
    for target in survivors.values():
        assert target.arena > 0 and target.requires["sram_kib"] > 0
        assert blobs[target.blob][4:8] == b"TFL3"
        # And, since no profile was resolvable at all here, the FULL-profile
        # caveat -- both flags defaulted, which is the shape
        # `tests/commands/test_model_command.py`'s `_CAVEAT` is copied from.
        # The half-defaulted shape tan now produces for a part WITH a
        # `npu_toolchain.vela` block is pinned by
        # `test_a_shipped_blobs_compiler_caveat_is_carried_into_the_package`.
        assert target.caveats == [
            "vela used its BUILT-IN default profile (system-config "
            "Ethos_U55_High_End_Embedded, memory-mode Shared_Sram), not one authored "
            "for this module -- vela's own warning for that is \"Compilation may be "
            "invalid or non-optimal\". The arena/SRAM figures and the compiled command "
            "stream describe that default memory model, not this module's."]
    assert any(t.backend == "cpu" for t in mft.targets)

    # The u85 target is absent from targets[] and RECORDED, with its reason.
    assert "ethos-u85-256" not in {t.accel_config for t in mft.targets}
    refused = [c for c in mft.coverage
               if c.backend == "ethos_u" and c.accel_config == "ethos-u85-256"]
    assert len(refused) == 1
    assert refused[0].status == "skipped"
    assert "reported 0 KiB SRAM" in refused[0].reason
    assert "Ethos_U85_SYS_DRAM_Mid" in refused[0].reason
    # ... and the EVIDENCE, end to end through a real vela process:
    # `metadata/socs/alif/ensemble/e8.json`'s `external_memory_interfaces`
    # lists only `HexSPI` and `SD/eMMC`, so the DRAM the run's working set
    # landed in is memory this part has no interface to (alp-sdk #1470 Task 4).
    # Nothing here is synthetic -- the interface list is the bound tree's own,
    # and the placement is vela's. Deliberately NOT `@needs_sdk_vela_profile`:
    # `external_memory_interfaces` long predates `npu_toolchain.vela` and is
    # present at the pinned SDK commit too, so this assertion RUNS there.
    assert "(no DRAM interface on this SoC)" in refused[0].reason
    # The vendor-file pointer is the half that DOES need the newer metadata, so
    # it is asserted next door under that capability mark rather than here.


@_needs_vela
@needs_sdk_vela_profile
def test_a_real_refusal_names_the_vendor_file_the_bound_metadata_declares(tmp_path):
    """The other half of the refusal above, split out because it is the one
    assertion that needs `npu_toolchain.vela` in the bound tree.

    `metadata/socs/alif/ensemble/e8.json` declares `vendor_config_filename:
    ensemble_vela.ini`, and that -- not a vendor prefix on the SoM preset's
    `silicon:` ref, and not a literal in tan -- is what puts the pointer in a
    real `tan model build`'s coverage reason (tan-cli#789 review (g),
    re-sourced). The counterpart for a part that declares none is pinned by
    `test_an_nxp_refusal_never_sends_the_reader_to_an_alif_file`.

    Runs the same real vela build as the test above rather than sharing it: a
    single test carrying both assertions would have to skip WHOLESALE at the
    pinned SDK commit, taking the per-target survival guard with it."""
    src = tmp_path / _TINY_INT8.name
    shutil.copy(_TINY_INT8, src)
    out = build_model(sku="E1M-AEN801", name="tiny", source=src, out_dir=tmp_path,
                      metadata_root=_meta_without_a_vela_memory_mode(tmp_path, "E1M-AEN801"))
    mft, _ = read_package(out.read_bytes())
    refused = [c for c in mft.coverage
               if c.backend == "ethos_u" and c.accel_config == "ethos-u85-256"]
    assert len(refused) == 1
    assert ("its System_Config lives in the proprietary ensemble_vela.ini "
            "alp-sdk does not redistribute") in refused[0].reason


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
    contract: one refused accelerator target must never empty the package.

    Driven through `_meta_without_a_vela_memory_mode` since alp-sdk #1470 -- the
    real `E1M-NX9101` now resolves `--memory-mode Shared_Sram` from
    `imx93.json` and its `ethos-u65-256` target ships. The SoM preset, the
    `silicon: nxp:imx9:imx93` ref the vendor clause is gated on, and the
    accel config are all still the bound tree's real ones, so the claim this
    makes about NXP customers is unchanged."""
    src = tmp_path / _TINY_INT8.name
    shutil.copy(_TINY_INT8, src)
    out = build_model(sku="E1M-NX9101", name="tiny", source=src, out_dir=tmp_path,
                      # default registry: real vela + cpu
                      metadata_root=_meta_without_a_vela_memory_mode(tmp_path, "E1M-NX9101"))
    mft, _ = read_package(out.read_bytes())
    assert [t.backend for t in mft.targets] == ["cpu"]

    refused = [c for c in mft.coverage if c.accel_config == "ethos-u65-256"]
    assert len(refused) == 1 and refused[0].status == "skipped"
    reason = refused[0].reason
    assert "Ethos_U65_Client_Server" in reason              # the profile it DID resolve
    assert "ensemble_vela.ini" not in reason               # ... and no Alif file after it
    assert "Alif" not in reason and "alif" not in reason
    # The part-independent remedy is intact for this SKU too.
    assert "No module vela profile was resolved for this part, so vela chose its own." in reason
    assert "`tan model build` skips this target and still builds the SKU's others." in reason


@_needs_vela
def test_every_target_refusing_is_an_error_not_an_empty_package(tmp_path):
    """The deliberate decision for "what if they ALL refuse".

    A `.alpmodel` with no runnable blob is worse than an error: nothing fails
    until the device tries to load it. So the existing zero-blob guard stands
    -- per-target skipping is about not losing the targets that WORKED, never
    about shipping a package with none.

    `E1M-NX9101` declares exactly one NPU (`ethos-u65-256`, which refuses on
    this fixture when no memory mode is resolvable) so restricting the registry
    to vela alone leaves nothing at all, and the refusal's own text must
    survive into the failure so the reader learns WHY rather than just "no blob
    compiled". `_meta_without_a_vela_memory_mode` for the same reason as its two
    siblings above."""
    src = tmp_path / _TINY_INT8.name
    shutil.copy(_TINY_INT8, src)
    with pytest.raises(ValueError) as exc:
        build_model(sku="E1M-NX9101", name="tiny", source=src, out_dir=tmp_path,
                    metadata_root=_meta_without_a_vela_memory_mode(tmp_path, "E1M-NX9101"),
                    adapters=[VelaAdapter()])
    msg = str(exc.value)
    assert "no blob compiled" in msg
    assert "ethos-u65-256" in msg and "Ethos_U65_Client_Server" in msg
    assert not list(tmp_path.glob("*.alpmodel"))         # nothing half-written


# --------------------------------------------------------------------------
# alp-sdk #1470: the SoM's own vela memory profile, end to end.
# --------------------------------------------------------------------------

@_needs_vela
@needs_sdk_vela_profile
@pytest.mark.parametrize("sku,accel_config,memory_mode", [
    ("E1M-AEN401", "ethos-u85-256", "Sram_Only"),
    ("E1M-AEN601", "ethos-u85-256", "Sram_Only"),
    ("E1M-AEN801", "ethos-u85-256", "Sram_Only"),
    ("E1M-NX9101", "ethos-u65-256", "Shared_Sram"),
])
def test_the_soms_memory_mode_makes_the_refused_target_ship_at_all(
        tmp_path, sku, accel_config, memory_mode):
    """THE ACCEPTANCE for the whole slice, through a REAL vela process.

    These are precisely the four SKUs tan-cli#789 had to refuse, and the
    reason was never the model: vela was compiling them against its own
    DRAM-backed built-in profile on parts that have no DRAM, so the SRAM
    figure alp-sdk sizes an arena from came back zero and the target became a
    `skipped` coverage row. With `--memory-mode` sourced from
    `metadata/socs/**`'s `npu_toolchain.vela` the placement lands in memory
    the module actually has and the target ships.

    Measured, `ethos-u-vela` 5.1.0 over the committed `tiny_int8.tflite`, this
    exact code path, before -> after:

      | SKU / accel config           | before      | after                     |
      |---|---|---|
      | AEN401/601/801 ethos-u85-256 | skipped     | arena 32, req_sram_kib 1  |
      | NX9101         ethos-u65-256 | skipped     | arena 32, req_sram_kib 1  |

    with vela's own columns moving `sram 0.0 / dram 0.265625` ->
    `sram 0.03125 / dram 0.0 / on_chip_flash 0.234375` on the Alif u85, and
    `sram 0.0 / dram 0.109375` -> `sram 0.03125 / dram 0.078125` on the NXP
    u65. Parametrized over all four rather than spot-checked on one: three
    resolve `Sram_Only` from an Alif Ensemble spec and one resolves
    `Shared_Sram` from `imx93.json`, and a regression that hardcoded either
    would pass a single-SKU test."""
    src = tmp_path / _TINY_INT8.name
    shutil.copy(_TINY_INT8, src)
    # The profile really did come from metadata, and the vendor-gated system
    # config really was withheld -- assert it here so a silently-empty profile
    # cannot pass this test by way of some other change making the compile work.
    spec = [s for s in resolve_targets(sku, metadata_root=_META)
            if s.accel_config == accel_config]
    assert len(spec) == 1
    assert spec[0].vela_memory_mode == memory_mode
    assert spec[0].vela_system_config is None

    out = build_model(sku=sku, name="tiny", source=src, out_dir=tmp_path,
                      metadata_root=_META)          # default registry: real vela + cpu
    mft, _ = read_package(out.read_bytes())

    shipped = {t.accel_config: t for t in mft.targets if t.backend == "ethos_u"}
    assert accel_config in shipped, (
        f"{accel_config} must ship as a target now, not a coverage row; "
        f"coverage: {[(c.accel_config, c.status, c.reason) for c in mft.coverage]}")
    target = shipped[accel_config]
    # NOT a pinned figure. `req_sram_kib` is measured above and named in the
    # docstring for the record, but what this asserts is the property the
    # device-side fit gate needs -- a nonzero requirement
    # (`t->req_sram_kib <= e->arena_sram_kib` reads 0 as fitting ANY arena,
    # src/backends/inference/alp_model_select.c).
    #
    # THE CONTRACT DECISION (tan-cli#1011): under `Sram_Only` this figure is
    # the ARENA ONLY, and that is correct -- not a gap. `e->arena_sram_kib`
    # itself is documented as an arena-only budget
    # (`metadata/schemas/soc-spec-v1.schema.json`'s `inference_arena_sram_kib`:
    # "Usable SRAM budget (KiB) for an NPU tensor arena on this SoC"), and the
    # const/weights region vela reports separately is carried in the blob
    # payload (`model_data`/`model_size`, `src/common/alp_model_loader.c`),
    # sized by the integrator, never by `req_sram_kib`. vela files that region
    # under `on_chip_flash` for `Sram_Only` parts as a pure bookkeeping rename,
    # not a real placement -- `ethosu/vela/architecture_features.py` overrides
    # `const_mem_area` from `Sram` to `OnChipFlash` with every characteristic
    # copied ("This will use the same characteristics as Sram."), because
    # vela's own validity check forbids naming `Sram` as a const area while
    # `Sram_Only` puts all three areas on the same port. On an AEN module that
    # region really is SRAM-resident too (alp-sdk's
    # `examples/aen/aen-npu-inference-alp/src/main.c` memcpy's the model into
    # `model_sram[] __attribute__((section("SRAM0")))` with the arena in SRAM0
    # beside it) -- real information, just not carried through this field.
    # Measured on the real 44-op `person_detect_int8.tflite` at
    # `ethos-u85-256`: `sram_memory_used = 72.0` (the arena; `req_sram_kib`)
    # and `on_chip_flash_memory_used = 235.265625` (the const/weights region;
    # the blob payload's own concern). Summing the columns into
    # `req_sram_kib` would be WRONG, not merely unnecessary: it would inflate
    # `arena_bytes` by the same amount (both come from one `sram_kib` value),
    # oversizing the scratch buffer, and for an integration that XIPs weights
    # from flash instead it would OVER-report and refuse a board that fits.
    # See `ethos_u._footprint`'s own docstring for the full contract.
    assert target.requires["sram_kib"] > 0
    assert target.arena > 0
    assert any(t.backend == "cpu" for t in mft.targets)     # siblings untouched
    assert accel_config not in {c.accel_config for c in mft.coverage}


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
        def compile(self, source, *, accel_config, out_dir, opts=None,
                    vela_memory_mode=None, vela_system_config=None,
                    vela_vendor_system_config=None,
                    vela_vendor_config_filename=None, soc_declares_dram=None):
            raise VelaFootprintRefused(f"refused {accel_config}")

    class _Crashes(_Refuses):
        def compile(self, source, *, accel_config, out_dir, opts=None,
                    vela_memory_mode=None, vela_system_config=None,
                    vela_vendor_system_config=None,
                    vela_vendor_config_filename=None, soc_declares_dram=None):
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
        def compile(self, source, *, accel_config, out_dir, opts=None,
                    vela_memory_mode=None, vela_system_config=None,
                    vela_vendor_system_config=None,
                    vela_vendor_config_filename=None, soc_declares_dram=None):
            return Blob(format="drpai_dir", payload=b"RT", arena_bytes=4096,
                        req_sram_kib=4)             # npu_op_count stays None

    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-X")
    out = build_model(sku="E1M-V2M101", name="demo", source=src, out_dir=tmp_path,
                      metadata_root=_META, adapters=[CpuAdapter(), _Silent()])
    mft, _ = read_package(out.read_bytes())
    assert [t.backend for t in mft.targets if t.backend == "drpai"] == ["drpai"]


# --------------------------------------------------------------------------
# The compiler's own caveats travel WITH the blob into the package.
# --------------------------------------------------------------------------

@_needs_vela
@needs_sdk_vela_profile
def test_a_shipped_blobs_compiler_caveat_is_carried_into_the_package(tmp_path):
    """THE (f) GUARD, through a REAL vela process, read back out of the file.

    `tan model check --exact` already surfaced `Blob.caveats` into its report
    and JSON envelope -- but `check` ships nothing. `build` is the path that
    puts bytes on a board, and it dropped them: a package could ship a blob
    compiled for a memory model the module does not have with NOTHING in the
    package saying so, while the `arena`/`requires.sram_kib` pair sitting
    beside that blob -- figures describing vela's DEFAULT memory model, not
    the module's -- are exactly what alp-sdk's on-device selector consumes
    (`return e->arena_sram_kib == 0u || t->req_sram_kib <= e->arena_sram_kib;`,
    `src/backends/inference/alp_model_select.c`).

    Measured, `ethos-u-vela` 5.1.0 over the committed `tiny_int8.tflite` for
    `E1M-AEN801`, ALL THREE `ethos_u` targets (`ethos-u85-256`,
    `ethos-u55-256`, `ethos-u55-128` -- the u85 one no longer refused, since
    alp-sdk #1470 supplies its memory mode): each is compiled with
    `--memory-mode Sram_Only` out of `e8.json`'s `npu_toolchain.vela` and with
    NO `--system-config`, because the Alif names live only in the proprietary
    `ensemble_vela.ini`. vela therefore defaults exactly one of the two flags
    and says so once per run -- verbatim, "Warning: No system configuration
    specified. Using a default of Ethos_U85_SYS_DRAM_Mid." (and
    `Ethos_U55_High_End_Embedded` on the two u55 configs). The caveat that
    reaches the manifest must name THAT, per run, and must not attribute the
    memory mode tan supplied to vela: the hard "figures describe that default
    memory model" verdict is false now that the placement is the module's, and
    a customer reading it would discount a figure that is correct.

    Read back with `read_manifest_file`, i.e. out of the WRITTEN ARTIFACT: a
    caveat that never reached the file must not be assertable here.
    Monkeypatching cannot prove any of this -- the claim is about what the
    real compiler reported for a real, successful, NPU-placing compile."""
    src = tmp_path / _TINY_INT8.name
    shutil.copy(_TINY_INT8, src)
    out = build_model(sku="E1M-AEN801", name="tiny", source=src, out_dir=tmp_path,
                      metadata_root=_META)          # default registry: real vela + cpu
    mft = read_manifest_file(out)

    ethos_u = {t.accel_config: t for t in mft.targets if t.backend == "ethos_u"}
    assert set(ethos_u) == {"ethos-u85-256", "ethos-u55-256", "ethos-u55-128"}
    defaulted_system_config = {"ethos-u85-256": "Ethos_U85_SYS_DRAM_Mid",
                               "ethos-u55-256": "Ethos_U55_High_End_Embedded",
                               "ethos-u55-128": "Ethos_U55_High_End_Embedded"}
    for accel_config, target in ethos_u.items():
        assert len(target.caveats) == 1, f"{accel_config}: {target.caveats}"
        caveat = target.caveats[0]
        # The system config THIS run resolved, named -- never a hardcoded one,
        # and never the U85 name on a u55 config.
        assert f"BUILT-IN default system-config {defaulted_system_config[accel_config]}" in caveat
        # ... while the memory mode is credited to the module, not to vela.
        assert "--memory-mode Sram_Only" in caveat
        assert "came from this module's SoC metadata" in caveat
        assert "describe that default memory model" not in caveat
        # Never the host path out of vela's third ("No configuration file
        # specified") warning -- that one interpolates site-packages.
        assert "site-packages" not in caveat
        # The caveat sits beside the very figures it qualifies.
        assert target.arena > 0 and target.requires["sram_kib"] > 0

    # A passthrough CPU blob has no compiler with anything to say about it,
    # and none is invented.
    cpu = [t for t in mft.targets if t.backend == "cpu"]
    assert len(cpu) == 1 and cpu[0].caveats == []


@_needs_vela
def test_a_package_with_nothing_to_caveat_carries_no_caveat_key_at_all(tmp_path):
    """The negative control for the wire-level compatibility property: the
    `cpu` passthrough target ships no `caveats` key, so a package built out of
    uncaveated blobs is byte-for-byte what this writer produced before the
    field existed. Asserted on the RAW CBOR, not the dataclass, because the
    dataclass defaults an absent key to `[]` and would hide the difference."""
    import struct

    import cbor2

    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-DUMMY")
    out = build_model(sku="E1M-AEN801", name="demo", source=src, out_dir=tmp_path,
                      metadata_root=_META, adapters=[CpuAdapter()])
    raw = out.read_bytes()
    _, _, _, mft_off, mft_len, _, _ = struct.Struct("<4sHHIIII").unpack_from(raw, 0)
    wire = cbor2.loads(raw[mft_off:mft_off + mft_len])
    assert wire["targets"], "expected the cpu target"
    for target in wire["targets"]:
        assert "caveats" not in target, target
