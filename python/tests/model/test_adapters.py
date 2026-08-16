# SPDX-License-Identifier: Apache-2.0
"""Compiler adapters: interface + CPU passthrough."""
import os
import shutil
import pytest
from pathlib import Path
from tan.model.adapters import CompilerAdapter, Blob
from tan.model.adapters.cpu import CpuAdapter
from tan.model.adapters.drpai import DrpaiAdapter
from tan.model.adapters.deepx import DeepxAdapter
from tan.model.adapters.ethos_u import (VelaAdapter, VelaFootprintRefused, _footprint,
                                        _parse_vela_summary)
from tan.model.adapters.executorch import ExecutorchAdapter

_ROOT = Path(__file__).resolve().parents[2]


def test_onnx_is_an_accepted_blob_format():
    """The ORT CPU backend consumes raw .onnx -- neither vela_tflite nor dxnn
    nor drpai_dir."""
    from tan.model import manifest
    assert "onnx" in manifest.VALID_BLOB_FORMATS


def test_cpu_adapter_is_a_compiler_adapter():
    assert issubclass(CpuAdapter, CompilerAdapter)


def test_cpu_adapter_is_always_available_and_accepts_tflite():
    a = CpuAdapter()
    assert a.backend == "cpu"
    assert a.is_available() is True
    assert a.accepts("tflite") is True
    assert a.accepts("onnx") is False        # CPU/TFLM runs tflite only


def test_cpu_adapter_compile_passes_bytes_through(tmp_path):
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-DUMMY-MODEL")
    blob = CpuAdapter().compile(src, accel_config="", out_dir=tmp_path)
    assert isinstance(blob, Blob)
    assert blob.format == "tflite"
    assert blob.payload == b"TFL3-DUMMY-MODEL"
    assert blob.arena_bytes >= 0


def test_executorch_adapter_is_a_compiler_adapter():
    assert issubclass(ExecutorchAdapter, CompilerAdapter)


def test_executorch_adapter_is_always_available_and_accepts_pte():
    a = ExecutorchAdapter()
    assert a.backend == "cpu"
    assert a.is_available() is True
    assert a.accepts("pte") is True
    assert a.accepts("tflite") is False      # ExecuTorch ingests its own .pte, not .tflite
    assert a.accepts("onnx") is False


def test_executorch_adapter_compile_passes_bytes_through(tmp_path):
    src = tmp_path / "m.pte"
    src.write_bytes(b"PTE-DUMMY-PROGRAM")
    blob = ExecutorchAdapter().compile(src, accel_config="", out_dir=tmp_path)
    assert isinstance(blob, Blob)
    assert blob.format == "executorch"       # matches the device _fmt_enum case (#1260)
    assert blob.payload == b"PTE-DUMMY-PROGRAM"
    assert blob.arena_bytes >= 0


def test_executorch_adapter_does_not_require_compile_opts():
    assert ExecutorchAdapter().requires_compile_opts is False


def test_drpai_adapter_detect_and_skip(monkeypatch):
    monkeypatch.delenv("ALP_DRPAI_TVM_HOME", raising=False)
    # drpai.py probes the env var only (no shutil.which), so no which-patch needed.
    a = DrpaiAdapter()
    assert issubclass(DrpaiAdapter, CompilerAdapter)
    assert a.backend == "drpai"
    assert a.is_available() is False
    assert a.accepts("onnx") and not a.accepts("tflite") and not a.accepts("pt")
    # compile() is real now (Stage 2); with the toolchain absent it raises
    # RuntimeError naming ALP_DRPAI_TVM_HOME (detect-and-skip surfaces here).
    with pytest.raises(RuntimeError, match="ALP_DRPAI_TVM_HOME"):
        a.compile(Path("x.onnx"), accel_config="", out_dir=Path("."))


def test_deepx_adapter_detect_and_skip(monkeypatch):
    monkeypatch.delenv("ALP_DEEPX_SDK_HOME", raising=False)
    monkeypatch.setattr("tan.model.adapters.deepx.shutil.which", lambda n: None)
    a = DeepxAdapter()
    assert issubclass(DeepxAdapter, CompilerAdapter)
    assert a.backend == "deepx_dxm1"
    assert a.is_available() is False
    assert a.accepts("onnx") and not a.accepts("tflite") and not a.accepts("pt")
    # compile() is real now (Stage 2); with no per-model config it raises RuntimeError.
    with pytest.raises(RuntimeError, match="config"):
        a.compile(Path("x.onnx"), accel_config="", out_dir=Path("."))


def test_vela_adapter_backend_and_accepts():
    a = VelaAdapter()
    assert a.backend == "ethos_u"
    assert a.accepts("tflite") and not a.accepts("onnx")


def test_vela_adapter_is_available_follows_path(monkeypatch):
    monkeypatch.setattr("tan.model.adapters.ethos_u.shutil.which", lambda n: None)
    assert VelaAdapter().is_available() is False
    monkeypatch.setattr("tan.model.adapters.ethos_u.shutil.which", lambda n: "/usr/bin/vela")
    assert VelaAdapter().is_available() is True


def _out_dir_of(cmd: list[str]) -> Path:
    """Where the adapter told vela to write -- the real contract a fake
    `subprocess.run` has to honour. Read from the command line rather than
    assumed to be the caller's `out_dir`: each run gets its own
    per-accel-config subdirectory (`ethos_u._run_dir`), so two configs a SoM
    declares can never read each other's summary CSV."""
    return Path(cmd[cmd.index("--output-dir") + 1])


class _FakeProc:
    """A `subprocess.run` result stand-in. `stdout` matters: the adapter reads
    vela's resolved profile, its default-profile warnings AND its operator
    placement out of it, never out of the exit code."""

    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_vela_adapter_compile_invokes_cli_and_reads_output(tmp_path, monkeypatch):
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-INPUT")
    seen = {}

    def fake_run(cmd, capture_output, text, timeout):
        seen["cmd"] = cmd
        # emulate vela's output, into the directory the adapter asked for
        (_out_dir_of(cmd) / "m_vela.tflite").write_bytes(b"VELA-OUT")
        return _FakeProc()

    monkeypatch.setattr("tan.model.adapters.ethos_u.subprocess.run", fake_run)
    blob = VelaAdapter().compile(src, accel_config="ethos-u55-128", out_dir=tmp_path)
    assert seen["cmd"][:2] == ["vela", str(src)]
    assert "--accelerator-config" in seen["cmd"] and "ethos-u55-128" in seen["cmd"]
    assert _out_dir_of(seen["cmd"]) == tmp_path / "vela-ethos-u55-128"
    assert blob.format == "vela_tflite"
    assert blob.payload == b"VELA-OUT"
    assert blob.compiler_version.startswith("vela")
    assert blob.caveats == ()               # no default-profile warning in this stdout


def test_vela_adapter_compile_raises_on_vela_error(tmp_path, monkeypatch):
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-INPUT")

    def fake_run(cmd, capture_output, text, timeout):
        return _FakeProc(returncode=1, stderr="Invalid model")

    monkeypatch.setattr("tan.model.adapters.ethos_u.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="vela failed"):
        VelaAdapter().compile(src, accel_config="ethos-u55-128", out_dir=tmp_path)


def test_vela_adapter_compile_raises_when_output_file_missing(tmp_path, monkeypatch):
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-INPUT")

    def fake_run(cmd, capture_output, text, timeout):
        return _FakeProc()                 # vela "succeeds" but writes no _vela.tflite

    monkeypatch.setattr("tan.model.adapters.ethos_u.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="produced no output"):
        VelaAdapter().compile(src, accel_config="ethos-u55-128", out_dir=tmp_path)


# A real vela summary CSV -- captured verbatim from an actual
# `vela --accelerator-config ethos-u85-256` run (ethos-u-vela via a venv's
# `bin/vela`) compiling the public alp-sdk fixture
# tests/fixtures/models/person_detect_int8.tflite -- not hand-typed, so this
# fixture cannot silently drift out of sync with vela's real column names or
# units the way a hand-authored CSV once did (the unit bug this test exists
# to catch: every `<mem_area>_memory_used` column vela writes is already in
# KiB, `ethosu/vela/stats_writer.py:123`, NOT bytes).
_REAL_VELA_SUMMARY_CSV = (
    "experiment,network,accelerator_configuration,system_config,memory_mode,"
    "core_clock,arena_cache_size,sram_bandwidth,dram_bandwidth,"
    "on_chip_flash_bandwidth,off_chip_flash_bandwidth,weights_storage_area,"
    "feature_map_storage_area,inferences_per_second,batch_size,inference_time,"
    "passes_before_fusing,passes_after_fusing,sram_memory_used,"
    "dram_memory_used,on_chip_flash_memory_used,off_chip_flash_memory_used,"
    "total_original_weights,total_npu_encoded_weights,"
    "sram_feature_map_read_bytes,sram_feature_map_write_bytes,"
    "sram_weight_read_bytes,sram_weight_write_bytes,sram_total_bytes,"
    "dram_feature_map_read_bytes,dram_feature_map_write_bytes,"
    "dram_weight_read_bytes,dram_weight_write_bytes,dram_total_bytes,"
    "on_chip_flash_feature_map_read_bytes,on_chip_flash_feature_map_write_bytes,"
    "on_chip_flash_weight_read_bytes,on_chip_flash_weight_write_bytes,"
    "on_chip_flash_total_bytes,off_chip_flash_feature_map_read_bytes,"
    "off_chip_flash_feature_map_write_bytes,off_chip_flash_weight_read_bytes,"
    "off_chip_flash_weight_write_bytes,off_chip_flash_total_bytes,nn_macs,"
    "nn_tops,cycles_npu,cycles_sram_access,cycles_dram_access,"
    "cycles_on_chip_flash_access,cycles_off_chip_flash_access,cycles_total\n"
    "default,person_detect_int8,Ethos_U85_256,Ethos_U85_SYS_DRAM_Mid,"
    "Dedicated_Sram_384KB,1000000000.0,384.0,29.802322387695312,"
    "11.175870895385742,14.901161193847656,14.901161193847656,DRAM,DRAM,"
    "5501.821102785022,1,0.000181758,44,44,72.734375,237.796875,0.0,0.0,"
    "207984,205472,415443,269082,62176,38192,815933,89920,0,220352,0,323072,"
    "0,0,0,0,0,0,0,0,0,0,7077252,0.077875548806655,136025,25669,150119,0,0,"
    "181758\n"
)

# vela names its summary `<stem>_summary_<system_config>.csv` -- the suffix is
# the system-config it RESOLVED, which is why two accel configs in one family
# (ethos-u55-256 and ethos-u55-128 both resolve to Ethos_U55_High_End_Embedded)
# collide on one filename, and why each run now gets its own subdirectory.
_REAL_VELA_SUMMARY_NAME = "person_detect_int8_summary_Ethos_U85_SYS_DRAM_Mid.csv"


def test_parse_vela_summary_converts_kib_columns_not_bytes(tmp_path):
    """`sram_memory_used`=72.734375 in this real CSV is genuinely *KiB*
    (vela divides by 1024.0 before writing it) -- _parse_vela_summary must
    convert it to real bytes for arena_bytes, round the KiB requirement UP
    (never truncate, or a model that doesn't fit could pass the device-side
    gate as if it did), and must never source either figure from
    `arena_cache_size`=384.0 (a build-time cache-capacity knob, not this
    model's requirement)."""
    (tmp_path / _REAL_VELA_SUMMARY_NAME).write_text(
        _REAL_VELA_SUMMARY_CSV, encoding="utf-8")
    used = _parse_vela_summary(tmp_path, "person_detect_int8",
                               system_config="Ethos_U85_SYS_DRAM_Mid")
    assert used["sram"] == 72.734375        # KiB, verbatim -- not re-scaled here
    assert "arena_cache_size" not in used   # a build-time knob, never a footprint
    arena, sram_kib = _footprint(used)
    assert arena == 74480          # round(72.734375 * 1024), NOT 384
    assert sram_kib == 73          # ceil(72.734375), NOT 0 and NOT floored to 72


def test_parse_vela_summary_reads_every_memory_area_not_just_sram(tmp_path):
    """The areas a compile did NOT land in are load-bearing too: a compile
    with 0 KiB SRAM and a non-zero DRAM figure is the exact shape
    `_refuse_zero_sram_footprint` exists to catch, and it can only name where
    the working set actually went if every `<mem_area>_memory_used` column is
    read, not just `sram_memory_used`."""
    (tmp_path / _REAL_VELA_SUMMARY_NAME).write_text(
        _REAL_VELA_SUMMARY_CSV, encoding="utf-8")
    used = _parse_vela_summary(tmp_path, "person_detect_int8",
                               system_config="Ethos_U85_SYS_DRAM_Mid")
    assert used == {"sram": 72.734375, "dram": 237.796875,
                    "on_chip_flash": 0.0, "off_chip_flash": 0.0}


def test_parse_vela_summary_picks_this_runs_csv_not_the_alphabetical_first(tmp_path):
    """Two summary CSVs in one directory (what `build_model`'s reused
    `out_dir` produced before each run got its own subdirectory) must resolve
    by the run's OWN system-config, never by sort order -- `Ethos_U55_High_
    End_Embedded` sorts before `Ethos_U85_SYS_DRAM_Mid`, so a u85 target read
    a u55 compile's arena purely by accident of the alphabet."""
    (tmp_path / _REAL_VELA_SUMMARY_NAME).write_text(
        _REAL_VELA_SUMMARY_CSV, encoding="utf-8")
    (tmp_path / "person_detect_int8_summary_Ethos_U55_High_End_Embedded.csv").write_text(
        _REAL_VELA_SUMMARY_CSV.replace("72.734375", "1.5"), encoding="utf-8")
    used = _parse_vela_summary(tmp_path, "person_detect_int8",
                               system_config="Ethos_U85_SYS_DRAM_Mid")
    assert used["sram"] == 72.734375
    # Ambiguous (no system-config to disambiguate) is NOT a guess: no figure
    # at all beats a figure belonging to a different compile.
    assert _parse_vela_summary(tmp_path, "person_detect_int8") == {}


def test_parse_vela_summary_absent_returns_no_areas(tmp_path):
    assert _parse_vela_summary(tmp_path, "missing") == {}
    assert _footprint({}) == (0, 0)


def test_vela_refuses_a_zero_sram_footprint_on_a_real_npu_placement(tmp_path, monkeypatch):
    """THE REGRESSION GUARD (tan-cli#789): a successful compile that placed
    operators on the NPU must never report `arena 0 / req_sram_kib 0`.

    Measured verbatim, real `ethos-u-vela` 5.1.0 on
    `keyword_scrambled_8bit.tflite` at `ethos-u85-256`: 6 of 15 operators on
    the NPU, exit 0, `sram_memory_used = 0.0`, `dram_memory_used = 5.359375`
    -- because vela's built-in default profile is DRAM-backed. Reporting 0
    there passes alp-sdk's on-device fit gate unconditionally
    (`e->arena_sram_kib == 0u || t->req_sram_kib <= e->arena_sram_kib`,
    src/backends/inference/alp_model_select.c) and hands the caller
    `arena_bytes = 0`."""
    src = tmp_path / "keyword_scrambled_8bit.tflite"
    src.write_bytes(b"TFL3-INPUT")
    stdout = ("System configuration             Ethos_U85_SYS_DRAM_Mid\n"
              "Memory mode                      Dedicated_Sram_384KB\n"
              "CPU operators = 9 (60.0%)\n"
              "NPU operators = 6 (40.0%)\n")

    def fake_run(cmd, capture_output, text, timeout):
        out = _out_dir_of(cmd)
        (out / "keyword_scrambled_8bit_vela.tflite").write_bytes(b"VELA-OUT")
        (out / "keyword_scrambled_8bit_summary_Ethos_U85_SYS_DRAM_Mid.csv").write_text(
            "sram_memory_used,dram_memory_used\n0.0,5.359375\n", encoding="utf-8")
        return _FakeProc(stdout=stdout)

    monkeypatch.setattr("tan.model.adapters.ethos_u.subprocess.run", fake_run)
    with pytest.raises(VelaFootprintRefused) as exc:
        VelaAdapter().compile(src, accel_config="ethos-u85-256", out_dir=tmp_path)
    msg = str(exc.value)
    assert "6/15 operators on the NPU" in msg      # the placement it DID achieve
    assert "dram 5.36 KiB" in msg                   # where the working set went
    assert "Ethos_U85_SYS_DRAM_Mid / Dedicated_Sram_384KB" in msg   # under which profile
    assert "\n" not in msg                          # one line: it lands in a report note


def test_the_refusal_prescribes_nothing_tan_cannot_actually_do(tmp_path, monkeypatch):
    """tan-cli#789 review BLOCKER 2: the refusal used to end "Compile against a
    --system-config/--memory-mode matching this module's memory model
    instead" -- an action tan cannot perform and the user cannot request.
    `VelaAdapter.compile()` takes `opts` and never reads it, nothing under
    `tan/` passes either flag, and alp-sdk's `board.schema.json` declares
    `models[].compile` as `additionalProperties: false` over
    `deepx_dxm1`/`drpai`, so there is no `ethos_u` key to route a profile
    through. Every clause must now be true and point at something real."""
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-INPUT")

    def fake_run(cmd, capture_output, text, timeout):
        out = _out_dir_of(cmd)
        (out / "m_vela.tflite").write_bytes(b"VELA-OUT")
        (out / "m_summary_Ethos_U85_SYS_DRAM_Mid.csv").write_text(
            "sram_memory_used,dram_memory_used\n0.0,5.359375\n", encoding="utf-8")
        return _FakeProc(stdout=(
            "Warning: No system configuration specified. Using a default of "
            "Ethos_U85_SYS_DRAM_Mid. Compilation may be invalid or non-optimal.\n"
            "Warning: No memory mode specified. Using a default of "
            "Dedicated_Sram_384KB. Compilation may be invalid or non-optimal.\n"
            "System configuration             Ethos_U85_SYS_DRAM_Mid\n"
            "Memory mode                      Dedicated_Sram_384KB\n"
            "CPU operators = 9 (60.0%)\n"
            "NPU operators = 6 (40.0%)\n"))

    monkeypatch.setattr("tan.model.adapters.ethos_u.subprocess.run", fake_run)
    with pytest.raises(VelaFootprintRefused) as exc:
        VelaAdapter().compile(src, accel_config="ethos-u85-256", out_dir=tmp_path)
    msg = str(exc.value)
    assert "--system-config" not in msg and "--memory-mode" not in msg
    # ... and instead, the three things that ARE true:
    assert "vela's BUILT-IN default profile" in msg          # why the figure is wrong
    assert "tan cannot pass one yet" in msg                   # why you cannot fix it here
    assert "ensemble_vela.ini" in msg                         # where the real profile lives
    assert "alp-sdk does not redistribute" in msg
    assert "`tan model build` skips this target" in msg       # what happens next
    assert "\n" not in msg


def test_the_refusal_names_the_profile_the_run_reported_not_a_hardcoded_alif_one(
        tmp_path, monkeypatch):
    """tan-cli#789 review MAJOR 3: `ethos-u65-256` on E1M-NX9101 (NXP i.MX 93)
    refuses identically -- measured, real vela 5.1.0 over the committed
    `tiny_int8.tflite`: "vela ... 1/1 operators on the NPU for ethos-u65-256
    ... 0 KiB SRAM ... dram 0.11 KiB ... Ethos_U65_Client_Server". An NXP user
    must never read an error blaming `Ethos_U85_SYS_DRAM_Mid`, so the profile
    is read from the run's own summary block, never hardcoded."""
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-INPUT")

    def fake_run(cmd, capture_output, text, timeout):
        out = _out_dir_of(cmd)
        (out / "m_vela.tflite").write_bytes(b"VELA-OUT")
        (out / "m_summary_Ethos_U65_Client_Server.csv").write_text(
            "sram_memory_used,dram_memory_used\n0.0,0.109375\n", encoding="utf-8")
        return _FakeProc(stdout=(
            "Warning: No system configuration specified. Using a default of "
            "Ethos_U65_Client_Server. Compilation may be invalid or non-optimal.\n"
            "System configuration             Ethos_U65_Client_Server\n"
            "Memory mode                      Dedicated_Sram_384KB\n"
            "CPU operators = 0 (0.0%)\n"
            "NPU operators = 1 (100.0%)\n"))

    monkeypatch.setattr("tan.model.adapters.ethos_u.subprocess.run", fake_run)
    with pytest.raises(VelaFootprintRefused) as exc:
        VelaAdapter().compile(src, accel_config="ethos-u65-256", out_dir=tmp_path)
    msg = str(exc.value)
    assert "ethos-u65-256" in msg
    assert "Ethos_U65_Client_Server" in msg
    assert "Ethos_U85" not in msg               # the SKU that refuses here is not Alif's


def test_two_runs_sharing_one_out_dir_never_read_each_others_summary(tmp_path, monkeypatch):
    """tan-cli#789 review MINOR 6: what `_run_dir`'s per-run subdirectory is
    actually FOR.

    Its only previous binding was a path-string assertion, so mutating
    `_run_dir` to `return out_dir` left the whole suite green but one line --
    nothing pinned the behaviour. This does: `build_model` reuses ONE `out_dir`
    across every accel config a SoM declares, and vela's summary filename
    carries the SYSTEM-CONFIG, a silicon-family property -- `ethos-u55-256` and
    `ethos-u55-128` both resolve to `Ethos_U55_High_End_Embedded` and so write
    the identical `<stem>_summary_Ethos_U55_High_End_Embedded.csv` (measured,
    vela 5.1.0).

    The killer case is the second run writing NO summary at all: in a shared
    directory it silently inherits the first run's file and reports a footprint
    that belongs to a different compile. Here run 2 is a full CPU fallback with
    a genuine 0 KiB footprint, so inheriting run 1's 8 KiB would be a wrong
    figure shipped into `requires.sram_kib` -- exactly the class this whole
    round exists to close."""
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-INPUT")
    shared_out = tmp_path / "out"
    shared_out.mkdir()
    summary_name = "m_summary_Ethos_U55_High_End_Embedded.csv"

    def run_with_summary(cmd, capture_output, text, timeout):
        out = _out_dir_of(cmd)
        (out / "m_vela.tflite").write_bytes(b"VELA-OUT-256")
        (out / summary_name).write_text(
            "sram_memory_used\n8.0\n", encoding="utf-8")
        return _FakeProc(stdout=(
            "System configuration             Ethos_U55_High_End_Embedded\n"
            "Memory mode                      Shared_Sram\n"
            "CPU operators = 0 (0.0%)\n"
            "NPU operators = 4 (100.0%)\n"))

    def run_without_summary(cmd, capture_output, text, timeout):
        out = _out_dir_of(cmd)
        (out / "m_vela.tflite").write_bytes(b"VELA-OUT-128")
        return _FakeProc(stdout=(                    # same system-config, same CSV name
            "System configuration             Ethos_U55_High_End_Embedded\n"
            "Memory mode                      Shared_Sram\n"
            "CPU operators = 4 (100.0%)\n"
            "NPU operators = 0 (0.0%)\n"))

    monkeypatch.setattr("tan.model.adapters.ethos_u.subprocess.run", run_with_summary)
    first = VelaAdapter().compile(src, accel_config="ethos-u55-256", out_dir=shared_out)
    assert (first.arena_bytes, first.req_sram_kib) == (8192, 8)

    monkeypatch.setattr("tan.model.adapters.ethos_u.subprocess.run", run_without_summary)
    second = VelaAdapter().compile(src, accel_config="ethos-u55-128", out_dir=shared_out)
    # Its OWN (absent) figure, not the previous run's 8 KiB.
    assert (second.arena_bytes, second.req_sram_kib) == (0, 0)
    assert second.payload == b"VELA-OUT-128"        # ... and its own artifact, not run 1's
    # The identical filename really was written once and would have collided.
    assert len(list(shared_out.glob(f"**/{summary_name}"))) == 1


def test_vela_reports_a_real_zero_footprint_for_a_full_cpu_fallback(tmp_path, monkeypatch):
    """0 NPU operators is a LEGITIMATE 0 KiB footprint -- measured:
    `float32_fc.tflite` at `ethos-u85-256` reports 0.0 for every memory area
    and prints "NPU operators = 0 (0.0%)". The refusal above must not fire
    here, or `tan model check --exact`'s cpu-only verdict becomes an error."""
    src = tmp_path / "float32_fc.tflite"
    src.write_bytes(b"TFL3-INPUT")

    def fake_run(cmd, capture_output, text, timeout):
        out = _out_dir_of(cmd)
        (out / "float32_fc_vela.tflite").write_bytes(b"VELA-OUT")
        (out / "float32_fc_summary_Ethos_U85_SYS_DRAM_Mid.csv").write_text(
            "sram_memory_used,dram_memory_used\n0.0,0.0\n", encoding="utf-8")
        return _FakeProc(stdout=(
            "System configuration             Ethos_U85_SYS_DRAM_Mid\n"
            "Memory mode                      Dedicated_Sram_384KB\n"
            "CPU operators = 1 (100.0%)\n"
            "NPU operators = 0 (0.0%)\n"))

    monkeypatch.setattr("tan.model.adapters.ethos_u.subprocess.run", fake_run)
    blob = VelaAdapter().compile(src, accel_config="ethos-u85-256", out_dir=tmp_path)
    assert (blob.arena_bytes, blob.req_sram_kib) == (0, 0)
    assert (blob.npu_op_count, blob.cpu_op_count) == (0, 1)


def test_vela_carries_its_own_default_profile_warning_as_a_caveat(tmp_path, monkeypatch):
    """No `--system-config`/`--memory-mode` is passed (none can be: the
    SoM-authoritative profile lives in a proprietary .ini alp-sdk does not
    redistribute), so vela falls back to a DRAM-backed default on a module
    that has no DRAM. Its own warning is carried out verbatim rather than
    swallowed -- a default-profile compile must not read as an authoritative
    one."""
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-INPUT")

    def fake_run(cmd, capture_output, text, timeout):
        out = _out_dir_of(cmd)
        (out / "m_vela.tflite").write_bytes(b"VELA-OUT")
        (out / "m_summary_Ethos_U85_SYS_DRAM_Mid.csv").write_text(
            "sram_memory_used\n8.0\n", encoding="utf-8")
        # Warning text verbatim from a real `ethos-u-vela` 5.1.0 run.
        return _FakeProc(stdout=(
            "Warning: No system configuration specified. Using a default of "
            "Ethos_U85_SYS_DRAM_Mid. Compilation may be invalid or non-optimal.\n"
            "Warning: No memory mode specified. Using a default of "
            "Dedicated_Sram_384KB. Compilation may be invalid or non-optimal.\n"
            "System configuration             Ethos_U85_SYS_DRAM_Mid\n"
            "Memory mode                      Dedicated_Sram_384KB\n"
            "CPU operators = 0 (0.0%)\n"
            "NPU operators = 4 (100.0%)\n"))

    monkeypatch.setattr("tan.model.adapters.ethos_u.subprocess.run", fake_run)
    blob = VelaAdapter().compile(src, accel_config="ethos-u85-256", out_dir=tmp_path)
    assert len(blob.caveats) == 1
    caveat = blob.caveats[0]
    assert "BUILT-IN default profile" in caveat
    assert "system-config Ethos_U85_SYS_DRAM_Mid" in caveat
    assert "memory-mode Dedicated_Sram_384KB" in caveat
    assert "Compilation may be invalid or non-optimal" in caveat     # vela's own words
    assert "site-packages" not in caveat        # never the host path from vela's third warning


@pytest.mark.skipif(shutil.which("vela") is None, reason="vela (ethos-u-vela) not installed")
def test_vela_real_compile_of_tiny_fixture(tmp_path):
    src = tmp_path / "tiny.tflite"
    shutil.copy(_ROOT / "tests/fixtures/models/tiny_int8.tflite", src)
    blob = VelaAdapter().compile(src, accel_config="ethos-u55-128", out_dir=tmp_path)
    assert blob.format == "vela_tflite"
    assert blob.payload[4:8] == b"TFL3"        # vela emits a .tflite flatbuffer
    assert blob.compiler_version.startswith("vela")


@pytest.mark.skipif(shutil.which("vela") is None, reason="vela (ethos-u-vela) not installed")
@pytest.mark.parametrize("accel_config", ["ethos-u55-256", "ethos-u55-128"])
def test_vela_real_compile_for_e8_accel_configs(tmp_path, accel_config):
    """Compile the committed fixture for the E1M-AEN801 (E8) U55 accel configs.

    Proves the shipped Vela accepts the metadata-derived config strings and
    emits a vela_tflite blob with a real footprint (i.e. op-support + arena
    sizing).  It does NOT prove the blob runs correctly on the NPU; that is
    silicon + Ethos-U HAL gated (alp_ethosu_aen_register() returns NOSUPPORT
    today).

    The E8-only ``ethos-u85-256`` is NOT parametrized here: vela accepts it
    and compiles cleanly, but its default profile reports no SRAM footprint
    for a fixture this small, which is a refusal, not a blob -- see the test
    below, which is where that config's real behaviour is pinned.
    """
    src = tmp_path / "tiny.tflite"
    shutil.copy(_ROOT / "tests/fixtures/models/tiny_int8.tflite", src)
    blob = VelaAdapter().compile(src, accel_config=accel_config, out_dir=tmp_path)
    assert blob.format == "vela_tflite"
    assert blob.payload[4:8] == b"TFL3"
    assert blob.compiler_version.startswith("vela")
    # Ethos_U55_High_End_Embedded / Shared_Sram: the arena IS in SRAM here, so
    # a real footprint comes back (measured, vela 5.1.0).
    assert blob.req_sram_kib > 0 and blob.arena_bytes > 0


@pytest.mark.skipif(shutil.which("vela") is None, reason="vela (ethos-u-vela) not installed")
@pytest.mark.parametrize("accel_config,profile", [
    # E1M-AEN401 (E4) / E1M-AEN601 (E6) / E1M-AEN801 (E8) -- Alif Ensemble.
    ("ethos-u85-256", "Ethos_U85_SYS_DRAM_Mid / Dedicated_Sram_384KB"),
    # E1M-NX9101 -- NXP i.MX 93. NOT an Alif part, and a DIFFERENT default
    # profile: hardcoding the U85 one would blame an Alif memory model for an
    # NXP refusal (tan-cli#789 review MAJOR 3).
    ("ethos-u65-256", "Ethos_U65_Client_Server / Dedicated_Sram_384KB"),
])
def test_vela_real_dram_default_profile_compile_refuses_a_zero_footprint(
        tmp_path, accel_config, profile):
    """The two accel configs whose BUILT-IN default profile is DRAM-backed,
    through a REAL vela process.

    Measured, `ethos-u-vela` 5.1.0 over the committed fixture: vela accepts
    each config string, compiles cleanly, places 1/1 operators on the NPU and
    exits 0 -- yet reports `sram_memory_used = 0.0`, because its default
    profile puts both feature maps and weights in DRAM (`ethos-u85-256` ->
    `Ethos_U85_SYS_DRAM_Mid`, `dram 0.27 KiB`; `ethos-u65-256` ->
    `Ethos_U65_Client_Server`, `dram 0.11 KiB`), and neither an Alif Ensemble
    module nor an i.MX 93 SoM exposes that memory to the NPU arena. There is
    no SoM-authoritative profile to compile against instead (Alif's
    `ensemble_vela.ini` is proprietary and not redistributed, and nothing
    plumbs one through `tan model build` anyway -- see
    `tan.model.adapters.ethos_u`'s module docstring), so the honest answer is
    a refusal naming exactly that, NOT `arena 0 / req_sram_kib 0`: alp-sdk's
    on-device selector reads a zero as "fits any envelope".

    Monkeypatching cannot prove this -- the whole point is what the REAL
    compiler reports for a REAL, successful, NPU-placing compile.
    """
    src = tmp_path / "tiny.tflite"
    shutil.copy(_ROOT / "tests/fixtures/models/tiny_int8.tflite", src)
    with pytest.raises(VelaFootprintRefused) as exc:
        VelaAdapter().compile(src, accel_config=accel_config, out_dir=tmp_path)
    msg = str(exc.value)
    assert f"vela compiled cleanly for {accel_config}" in msg   # the compile DID succeed
    assert "operators on the NPU" in msg
    assert "reported 0 KiB SRAM" in msg
    assert profile in msg                          # the profile THIS run resolved
    assert "\n" not in msg                         # one line: it lands in a note


def test_cpu_and_vela_do_not_require_compile_opts():
    assert CpuAdapter().requires_compile_opts is False
    assert VelaAdapter().requires_compile_opts is False


def test_drpai_and_deepx_require_compile_opts():
    assert DrpaiAdapter().requires_compile_opts is True
    assert DeepxAdapter().requires_compile_opts is True


def test_cpu_compile_accepts_opts_kwarg(tmp_path):
    src = tmp_path / "m.tflite"; src.write_bytes(b"TFL3-X")
    blob = CpuAdapter().compile(src, accel_config="", out_dir=tmp_path, opts=None)
    assert blob.payload == b"TFL3-X"


def test_vela_compile_accepts_opts_kwarg(tmp_path, monkeypatch):
    src = tmp_path / "m.tflite"; src.write_bytes(b"TFL3-X")
    def fake_run(cmd, capture_output, text, timeout):
        (_out_dir_of(cmd) / "m_vela.tflite").write_bytes(b"VELA-OUT")
        return _FakeProc()
    monkeypatch.setattr("tan.model.adapters.ethos_u.subprocess.run", fake_run)
    blob = VelaAdapter().compile(src, accel_config="ethos-u55-128",
                                 out_dir=tmp_path, opts={"ignored": True})
    assert blob.payload == b"VELA-OUT"


# --- DEEPX dxcom compile (Stage 2 step 2) ---------------------------------

def test_deepx_compile_rejects_missing_config(tmp_path):
    # opts present but no `config` key -> RuntimeError (dxcom needs -c).
    src = tmp_path / "m.onnx"; src.write_bytes(b"ONNX")
    with pytest.raises(RuntimeError, match="config"):
        DeepxAdapter().compile(src, accel_config="", out_dir=tmp_path,
                               opts={"calibration": "calib/"})


def test_deepx_compile_invokes_dxcom_and_returns_dxnn(tmp_path, monkeypatch):
    # A successful dxcom compile writes a single <stem>.dxnn into the -o dir; the
    # adapter returns its raw bytes (blob_format 'dxnn'), NOT a tar of the dir --
    # 'dxnn' is what the device _fmt_enum maps to ALP_INFERENCE_MODEL_DXNN.
    src = tmp_path / "m.onnx"; src.write_bytes(b"ONNX-IN")
    cfg = tmp_path / "m.deepx.json"; cfg.write_text("{}", encoding="utf-8")
    seen = {}

    def fake_run(cmd, capture_output, text, timeout):
        if "-v" in cmd:                              # _dxcom_version() probe
            class _V:
                returncode = 0
                stdout = "DX-COM (DEEPX Compiler) 2.3.0\nTarget Hardware: M1"
                stderr = ""
            return _V()
        seen["cmd"] = cmd                            # the compile invocation
        out = Path(cmd[cmd.index("-o") + 1])         # dxcom writes into the -o dir
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{src.stem}.dxnn").write_bytes(b"DXNN\x08\x00\x00\x00{}")   # canonical artifact
        (out / "compiler.log").write_text("ok", encoding="utf-8")          # stray log to ignore

        class _C:
            returncode = 0
            stdout = ""
            stderr = ""
        return _C()

    monkeypatch.setattr("tan.model.adapters.deepx.subprocess.run", fake_run)
    blob = DeepxAdapter().compile(src, accel_config="", out_dir=tmp_path,
                                  opts={"config": str(cfg)})
    assert seen["cmd"][:3] == ["dxcom", "-m", str(src)]
    assert "-c" in seen["cmd"] and str(cfg) in seen["cmd"]
    assert "-o" in seen["cmd"]
    assert blob.format == "dxnn"
    assert blob.payload.startswith(b"DXNN")          # raw .dxnn flatbuffer, not a tar
    assert blob.compiler_version == "DX-COM 2.3.0"


def test_deepx_compile_raises_when_no_dxnn_produced(tmp_path, monkeypatch):
    src = tmp_path / "m.onnx"; src.write_bytes(b"ONNX-IN")
    cfg = tmp_path / "m.deepx.json"; cfg.write_text("{}", encoding="utf-8")

    def fake_run(cmd, capture_output, text, timeout):
        if "-v" in cmd:
            class _V:
                returncode = 0
                stdout = "DX-COM (DEEPX Compiler) 2.3.0"
                stderr = ""
            return _V()
        Path(cmd[cmd.index("-o") + 1]).mkdir(parents=True, exist_ok=True)   # "succeeds", no .dxnn

        class _C:
            returncode = 0
            stdout = ""
            stderr = ""
        return _C()

    monkeypatch.setattr("tan.model.adapters.deepx.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="no .dxnn"):
        DeepxAdapter().compile(src, accel_config="", out_dir=tmp_path, opts={"config": str(cfg)})


@pytest.mark.skipif(shutil.which("dxcom") is None, reason="dxcom (dx-com wheel) not installed")
def test_deepx_real_dxcom_version_smoke():
    # Against the REAL installed dx-com wheel (e.g. a WSL venv): the adapter's
    # version probe reads dxcom's banner. The full real-compile e2e lives in
    # test_deepx_real_compile_of_tiny_fixture (public) + the alp-sdk-internal
    # yolo11n test.
    from tan.model.adapters.deepx import _dxcom_version
    v = _dxcom_version()
    assert v.startswith("DX-COM") and "2.3" in v


def _host_mem_avail_gib() -> float:
    """Available host RAM in GiB (Linux /proc/meminfo); 0.0 if unknown."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except OSError:
        pass
    return 0.0


# dx-com 2.3.0 aborts in PREPARE with a RamSizeError below ~15 GiB host RAM.
_DXCOM_MIN_RAM_GIB = 15.5


# --- DRP-AI TVM compile (Stage 2) -----------------------------------------

def _drpai_opts(images_dir):
    return {"input_shape": "1,3,224,224", "input_name": "input",
            "images": str(images_dir), "product": "V2N"}


def test_drpai_compile_rejects_non_224_images_early(tmp_path, monkeypatch):
    # Issue #1271: the vendor tutorial's --images path always resizes through
    # pre_process_imagenet_pytorch(), which hard-codes resize(256)+center_crop(224)
    # regardless of the model's real input_shape. A detector geometry (e.g.
    # YOLOX 1,3,640,640) must be rejected FAST and clearly here, not forwarded
    # into a multi-minute compile that then dies deep in the vendor tutorial
    # with `could not broadcast input array from shape (1,3,224,224) into
    # shape (1,3,640,640)`.
    home = tmp_path / "tvm"; (home / "tutorials").mkdir(parents=True)
    monkeypatch.setenv("ALP_DRPAI_TVM_HOME", str(home))
    src = tmp_path / "m.onnx"; src.write_bytes(b"ONNX-IN")
    calib = tmp_path / "calib"; calib.mkdir()
    (calib / "0.jpg").write_bytes(b"JPG")

    def fail_if_called(*a, **k):
        raise AssertionError(
            "compile_onnx_model_quant.py must not run for a shape mismatch")
    monkeypatch.setattr("tan.model.adapters.drpai.subprocess.run", fail_if_called)

    opts = {"input_shape": "1,3,640,640", "input_name": "images",
            "images": str(calib), "product": "V2N"}
    with pytest.raises(RuntimeError, match="pre_process_imagenet_pytorch"):
        DrpaiAdapter().compile(src, accel_config="", out_dir=tmp_path, opts=opts)


def test_drpai_compile_handles_non_str_input_shape_without_crashing(tmp_path, monkeypatch):
    # board.yaml can hand the adapter a YAML flow-sequence input_shape
    # ([1,3,640,640], parsed as a Python list) instead of a string; a
    # non-224 list shape must produce the same clean RuntimeError as its
    # string spelling, not an uncaught AttributeError from .split() on a
    # non-str.
    monkeypatch.setenv("ALP_DRPAI_TVM_HOME", str(tmp_path))
    src = tmp_path / "m.onnx"; src.write_bytes(b"ONNX")
    calib = tmp_path / "calib"; calib.mkdir()
    opts = {"input_shape": [1, 3, 640, 640], "input_name": "images",
            "images": str(calib), "product": "V2N"}
    with pytest.raises(RuntimeError, match="pre_process_imagenet_pytorch"):
        DrpaiAdapter().compile(src, accel_config="", out_dir=tmp_path, opts=opts)


def test_drpai_compile_accepts_224_shape_as_yaml_list(tmp_path, monkeypatch):
    # Round 4 (#1271): the adapter used to str()-normalize input_shape before
    # the 224x224 check, so str([1, 3, 224, 224]) == '[1, 3, 224, 224]' and
    # .split(",") on THAT tears into tokens ('[1', ' 224]', ...) that can't
    # parse as ints -- misdiagnosing a genuinely valid YAML-list 224x224
    # classifier shape as unsupported. A list-form 224x224 shape must compile
    # exactly like its string spelling, and the vendor CLI must still see a
    # comma-joined "-s 1,3,224,224", never Python's str() of the list.
    home = tmp_path / "tvm"; (home / "tutorials").mkdir(parents=True)
    monkeypatch.setenv("ALP_DRPAI_TVM_HOME", str(home))
    src = tmp_path / "m.onnx"; src.write_bytes(b"ONNX-IN")
    calib = tmp_path / "calib"; calib.mkdir()
    (calib / "0.png").write_bytes(b"PNG")
    seen = {}

    def fake_run(cmd, capture_output, text, timeout, env):
        seen["cmd"] = cmd
        out = Path(cmd[cmd.index("-o") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "drp_desc.bin").write_bytes(b"DESC")
        (out / "weight.bin").write_bytes(b"WEIGHT")
        (out / "addr_map.txt").write_text("0x0", encoding="utf-8")

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()
    monkeypatch.setattr("tan.model.adapters.drpai.subprocess.run", fake_run)

    opts = {"input_shape": [1, 3, 224, 224], "input_name": "input",
            "images": str(calib), "product": "V2N"}
    blob = DrpaiAdapter().compile(src, accel_config="", out_dir=tmp_path, opts=opts)
    assert blob.format == "drpai_dir"
    assert seen["cmd"][seen["cmd"].index("-s") + 1] == "1,3,224,224"


def test_drpai_is_224_imagenet_shape_accepts_nchw():
    from tan.model.adapters.drpai import _is_224_imagenet_shape
    assert _is_224_imagenet_shape("1,3,224,224") is True
    # Same geometry as a YAML flow-sequence list/tuple must match too --
    # compared structurally, not via a stringified spelling (#1271 round 4).
    assert _is_224_imagenet_shape([1, 3, 224, 224]) is True
    assert _is_224_imagenet_shape((1, 3, 224, 224)) is True


@pytest.mark.parametrize("input_shape", [
    "1,3,640,640",       # YOLOX detector geometry (the issue's repro)
    [1, 3, 640, 640],    # same geometry as a YAML flow-sequence list
    "1,3,300,300",       # SSD-style detector geometry
    "1,224,224,3",       # NHWC: not what accepts() ever hands this ONNX-only
                          # adapter (ONNX is NCHW by convention) -- unsourced,
                          # see #1271 review; must NOT be accepted
    [1, 224, 224, 3],    # same NHWC geometry as a list
    "1,224,224",         # wrong rank
    "not,a,shape,x",     # unparsable
])
def test_drpai_is_224_imagenet_shape_rejects_non_224(input_shape):
    from tan.model.adapters.drpai import _is_224_imagenet_shape
    assert _is_224_imagenet_shape(input_shape) is False


def test_drpai_compile_rejects_missing_opts(tmp_path, monkeypatch):
    # Toolchain present (faked) but the per-model compile config is incomplete
    # -> RuntimeError naming the required keys.
    monkeypatch.setenv("ALP_DRPAI_TVM_HOME", str(tmp_path))
    src = tmp_path / "m.onnx"; src.write_bytes(b"ONNX")
    with pytest.raises(RuntimeError, match="input_shape"):
        DrpaiAdapter().compile(src, accel_config="", out_dir=tmp_path,
                               opts={"input_name": "input"})


def test_drpai_compile_rejects_bad_product(tmp_path, monkeypatch):
    monkeypatch.setenv("ALP_DRPAI_TVM_HOME", str(tmp_path))
    src = tmp_path / "m.onnx"; src.write_bytes(b"ONNX")
    calib = tmp_path / "calib"; calib.mkdir()
    opts = _drpai_opts(calib); opts["product"] = "V2L"        # unsupported
    with pytest.raises(RuntimeError, match="product"):
        DrpaiAdapter().compile(src, accel_config="", out_dir=tmp_path, opts=opts)


def test_drpai_compile_invokes_tvm_and_returns_drpai_dir(tmp_path, monkeypatch):
    # A successful DRP-AI compile writes a multi-file object DIR; the adapter
    # tars it into one byte blob with blob_format 'drpai_dir' (what the device
    # _fmt_enum maps to ALP_INFERENCE_MODEL_DRPAI), passing PRODUCT in the env.
    import io
    import tarfile

    home = tmp_path / "tvm"; (home / "tutorials").mkdir(parents=True)
    monkeypatch.setenv("ALP_DRPAI_TVM_HOME", str(home))
    src = tmp_path / "m.onnx"; src.write_bytes(b"ONNX-IN")
    calib = tmp_path / "calib"; calib.mkdir()
    (calib / "0.png").write_bytes(b"PNG")
    seen = {}

    def fake_run(cmd, capture_output, text, timeout, env):
        seen["cmd"] = cmd
        seen["product"] = env.get("PRODUCT")
        out = Path(cmd[cmd.index("-o") + 1])             # the -o object dir
        out.mkdir(parents=True, exist_ok=True)
        (out / "drp_desc.bin").write_bytes(b"DESC")      # required artifacts
        (out / "weight.bin").write_bytes(b"WEIGHT")
        (out / "addr_map.txt").write_text("0x0", encoding="utf-8")
        (out / "deploy.json").write_text("{}", encoding="utf-8")

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr("tan.model.adapters.drpai.subprocess.run", fake_run)
    blob = DrpaiAdapter().compile(src, accel_config="V2N", out_dir=tmp_path,
                                  opts=_drpai_opts(calib))
    assert seen["cmd"][:3] == ["python3",
                               str(home / "tutorials" / "compile_onnx_model_quant.py"),
                               str(src)]
    assert "-o" in seen["cmd"] and "-s" in seen["cmd"] and "-i" in seen["cmd"]
    # Pin the input-geometry VALUES, not just the flag presence: a regression
    # that swapped shape/name or passed a stale literal must fail here.
    assert seen["cmd"][seen["cmd"].index("-s") + 1] == "1,3,224,224"
    assert seen["cmd"][seen["cmd"].index("-i") + 1] == "input"
    assert "--images" in seen["cmd"] and str(calib) in seen["cmd"]
    assert seen["product"] == "V2N"
    assert blob.format == "drpai_dir"
    assert blob.compiler_version.startswith("drp-ai_tvm")
    # The payload is a real tar carrying the object-dir files.
    with tarfile.open(fileobj=io.BytesIO(blob.payload), mode="r") as tar:
        names = set(tar.getnames())
    assert {"drp_desc.bin", "weight.bin", "addr_map.txt", "deploy.json"} <= names


def test_drpai_compile_raises_when_artifacts_missing(tmp_path, monkeypatch):
    home = tmp_path / "tvm"; (home / "tutorials").mkdir(parents=True)
    monkeypatch.setenv("ALP_DRPAI_TVM_HOME", str(home))
    src = tmp_path / "m.onnx"; src.write_bytes(b"ONNX-IN")
    calib = tmp_path / "calib"; calib.mkdir()

    def fake_run(cmd, capture_output, text, timeout, env):
        Path(cmd[cmd.index("-o") + 1]).mkdir(parents=True, exist_ok=True)   # "ok", no .bin

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr("tan.model.adapters.drpai.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="drp_desc.bin"):
        DrpaiAdapter().compile(src, accel_config="V2N", out_dir=tmp_path,
                               opts=_drpai_opts(calib))


def test_drpai_compile_raises_on_tool_error(tmp_path, monkeypatch):
    home = tmp_path / "tvm"; (home / "tutorials").mkdir(parents=True)
    monkeypatch.setenv("ALP_DRPAI_TVM_HOME", str(home))
    src = tmp_path / "m.onnx"; src.write_bytes(b"ONNX-IN")
    calib = tmp_path / "calib"; calib.mkdir()

    def fake_run(cmd, capture_output, text, timeout, env):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "translator error"
        return _R()

    monkeypatch.setattr("tan.model.adapters.drpai.subprocess.run", fake_run)
    with pytest.raises(RuntimeError, match="DRP-AI compile failed"):
        DrpaiAdapter().compile(src, accel_config="V2N", out_dir=tmp_path,
                               opts=_drpai_opts(calib))


@pytest.mark.skipif(os.environ.get("ALP_DRPAI_TVM_HOME") is None
                    or not Path(os.environ.get("ALP_DRPAI_TVM_HOME", "")).is_dir(),
                    reason="DRP-AI TVM toolchain absent (set ALP_DRPAI_TVM_HOME)")
def test_drpai_real_compile_of_tiny_fixture(tmp_path):
    """Compile the committed tiny ONNX with the REAL DRP-AI TVM toolchain.

    Runs only where a built rzv_drp-ai_tvm install is on ALP_DRPAI_TVM_HOME
    (a maintainer box / RZ/V SDK container); skips otherwise (always in cloud
    CI). Mirrors test_deepx_real_compile_of_tiny_fixture. The fixture is the
    tiny 2-conv classifier (input [1,3,224,224])."""
    import numpy as np
    from PIL import Image

    onnx = _ROOT / "tests/fixtures/models/tiny_cnn.onnx"
    calib = tmp_path / "calib"
    calib.mkdir()
    rng = np.random.default_rng(0)
    for i in range(4):
        Image.fromarray(rng.integers(0, 256, (224, 224, 3), dtype=np.uint8)).save(calib / f"{i}.png")

    blob = DrpaiAdapter().compile(onnx, accel_config="V2N", out_dir=tmp_path,
                                  opts={"input_shape": "1,3,224,224",
                                        "input_name": "input",
                                        "images": str(calib), "product": "V2N"})
    assert blob.format == "drpai_dir"
    assert blob.compiler_version.startswith("drp-ai_tvm")
    import io as _io, tarfile as _tf
    with _tf.open(fileobj=_io.BytesIO(blob.payload), mode="r") as tar:
        names = set(tar.getnames())
    assert {"drp_desc.bin", "weight.bin", "addr_map.txt"} <= names


@pytest.mark.skipif(shutil.which("dxcom") is None, reason="dxcom (dx-com wheel) not installed")
@pytest.mark.skipif(_host_mem_avail_gib() < _DXCOM_MIN_RAM_GIB,
                    reason="dxcom needs >15 GiB host RAM (raise WSL via .wslconfig)")
def test_deepx_real_compile_of_tiny_fixture(tmp_path):
    """Compile the committed tiny ONNX with the REAL dxcom -> a single .dxnn.

    Runs only where the licensed dx-com wheel is installed (e.g. a WSL py3.12
    venv: `~/dxcom-venv/bin/python -m pytest tests/model/test_adapters.py`, run
    from tan-cli's `python/`) AND host RAM clears dxcom's ~15 GiB floor; skips
    otherwise (always in cloud CI). Mirrors test_vela_real_compile_of_tiny_fixture.
    The real-yolo11n
    counterpart lives in tests/model/test_deepx_yolo_internal.py (gated on the
    alp-sdk-internal sibling)."""
    import json
    import numpy as np
    from PIL import Image          # Pillow ships as a dx-com wheel dependency

    onnx = _ROOT / "tests/fixtures/models/tiny_cnn.onnx"
    calib = tmp_path / "calib"
    calib.mkdir()
    rng = np.random.default_rng(0)
    for i in range(4):
        Image.fromarray(rng.integers(0, 256, (224, 224, 3), dtype=np.uint8)).save(calib / f"{i}.png")

    cfg = tmp_path / "tiny.json"
    cfg.write_text(json.dumps({
        "inputs": {"input": [1, 3, 224, 224]},
        "calibration_method": "minmax",
        "calibration_num": 4,
        "default_loader": {
            "dataset_path": str(calib),
            "file_extensions": ["png"],
            "preprocessings": [
                {"resize": {"width": 224, "height": 224}},
                {"normalize": {"mean": [0, 0, 0], "std": [255, 255, 255]}},
                {"transpose": {"axis": [2, 0, 1]}},      # HWC->CHW for the NCHW model
            ],
        },
    }), encoding="utf-8")

    blob = DeepxAdapter().compile(onnx, accel_config="", out_dir=tmp_path,
                                  opts={"config": str(cfg)})
    assert blob.format == "dxnn"
    assert blob.payload[:4] == b"DXNN"        # self-describing .dxnn flatbuffer magic
    assert blob.compiler_version.startswith("DX-COM")
