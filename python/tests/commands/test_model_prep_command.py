# SPDX-License-Identifier: Apache-2.0
"""`tan model prep` (tan-cli#1287): coded refusals, and the real quantize path
on the committed tiny_cnn.onnx fixture when the `model` extra is installed."""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tan.commands import model_host_cmd
from tan.commands.model_cmd import model

app = typer.Typer(add_completion=False)
app.command("model")(model)
runner = CliRunner()
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "models" / "tiny_cnn.onnx"
HAVE_EXTRA = all(importlib.util.find_spec(m) for m in ("numpy", "onnxruntime", "onnx", "sympy"))


def invoke(*args):
    result = runner.invoke(app, ["--format", "json", *args], catch_exceptions=False)
    return result.exit_code, json.loads(result.stdout)


def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    shutil.copy(FIXTURE, proj / "m.onnx")
    return proj


def test_prep_needs_a_model(tmp_path):
    code, doc = invoke("prep", "--project", str(project(tmp_path)))
    assert code == 2 and doc["issues"][0]["code"] == "model.model-source-missing"
    code, doc = invoke("prep", "nope.onnx", "--project", str(project(tmp_path)))
    assert code == 2 and doc["issues"][0]["code"] == "model.model-source-missing"


def test_prep_refuses_tflite_input(tmp_path):
    proj = project(tmp_path)
    (proj / "m.tflite").write_bytes(b"x")
    code, doc = invoke("prep", "m.tflite", "--project", str(proj))
    assert code == 2 and doc["issues"][0]["code"] == "model.model-format-unsupported"
    assert "not available yet" in doc["issues"][0]["message"]


def test_prep_missing_extra_names_the_install(tmp_path, monkeypatch):
    monkeypatch.setattr(model_host_cmd, "missing_extra_modules", lambda verb: ["onnxruntime"])
    code, doc = invoke("prep", "m.onnx", "--calibration", "c", "--project", str(project(tmp_path)))
    assert code == 1
    issue = doc["issues"][0]
    assert issue["code"] == "model.model-extra-missing"
    assert 'pip install "tan-cli[model]"' in issue["message"]


@pytest.mark.skipif(not HAVE_EXTRA, reason="the optional `model` extra is not installed")
def test_prep_calibration_required_and_invalid(tmp_path):
    proj = project(tmp_path)
    code, doc = invoke("prep", "m.onnx", "--project", str(proj))
    assert code == 2 and doc["issues"][0]["code"] == "model.prep-calibration-invalid"
    (proj / "cal").mkdir()
    code, doc = invoke("prep", "m.onnx", "--calibration", "cal", "--project", str(proj))
    assert code == 2 and doc["issues"][0]["code"] == "model.prep-calibration-invalid"


@pytest.mark.skipif(not HAVE_EXTRA, reason="the optional `model` extra is not installed")
def test_prep_quantizes_and_reports(tmp_path):
    import numpy as np

    proj = project(tmp_path)
    (proj / "cal").mkdir()
    rng = np.random.default_rng(0)
    for i in range(8):
        np.save(proj / "cal" / f"s{i}.npy", rng.standard_normal((1, 3, 224, 224)).astype(np.float32))
    code, doc = invoke("prep", "m.onnx", "--calibration", "cal", "--project", str(proj))
    assert code == 0, doc
    assert (proj / "build" / "model-prep" / "m.int8.onnx").is_file()
    assert doc["data"]["calibration"]["samples"] == 8
    assert doc["data"]["accuracy"]["verdict"] in ("good", "degraded")
    assert doc["data"]["output"].endswith("m.int8.onnx")


# --- run / ab (host tier) ---------------------------------------------------


def test_run_and_ab_refusals(tmp_path, monkeypatch):
    proj = project(tmp_path)
    code, doc = invoke("run", "--project", str(proj))
    assert code == 2 and doc["issues"][0]["code"] == "model.model-source-missing"
    code, doc = invoke("ab", "m.onnx", "--project", str(proj))
    assert code == 2 and doc["issues"][0]["code"] == "model.model-source-missing"
    (proj / "m.tflite").write_bytes(b"x")
    code, doc = invoke("run", "m.tflite", "--project", str(proj))
    assert code == 2 and doc["issues"][0]["code"] == "model.model-format-unsupported"
    monkeypatch.setattr(model_host_cmd, "missing_extra_modules", lambda verb: ["numpy"])
    code, doc = invoke("run", "m.onnx", "--project", str(proj))
    assert code == 1 and doc["issues"][0]["code"] == "model.model-extra-missing"
    code, doc = invoke("list", "m.onnx", "--project", str(proj))
    assert code == 2 and doc["issues"][-1]["code"] == "model.unexpected-argument"


@pytest.mark.skipif(not HAVE_EXTRA, reason="the optional `model` extra is not installed")
def test_run_host_reference(tmp_path):
    proj = project(tmp_path)
    code, doc = invoke("run", "m.onnx", "--runs", "3", "--project", str(proj))
    assert code == 0, doc
    r = doc["data"]["result"]
    assert r["backend"] == "cpu-host" and r["tier"] == "host" and r["runs"] == 3
    assert r["latencyMs"] > 0 and r["peakSramKib"] is None and r["powerMj"] is None
    assert isinstance(r["outputArgmax"], int)


@pytest.mark.skipif(not HAVE_EXTRA, reason="the optional `model` extra is not installed")
def test_run_with_a_bad_input_is_a_coded_failure(tmp_path):
    import numpy as np

    proj = project(tmp_path)
    np.save(proj / "bad.npy", np.zeros((1, 3, 8, 8), dtype=np.float32))
    code, doc = invoke("run", "m.onnx", "--input", "bad.npy", "--project", str(proj))
    assert code == 1 and doc["issues"][0]["code"] == "model.run-failed"


@pytest.mark.skipif(not HAVE_EXTRA, reason="the optional `model` extra is not installed")
def test_ab_compares_the_fp32_model_with_its_int8_prep(tmp_path):
    import numpy as np

    proj = project(tmp_path)
    (proj / "cal").mkdir()
    rng = np.random.default_rng(1)
    for i in range(8):
        np.save(proj / "cal" / f"s{i}.npy", rng.standard_normal((1, 3, 224, 224)).astype(np.float32))
    assert invoke("prep", "m.onnx", "--calibration", "cal", "--project", str(proj))[0] == 0
    code, doc = invoke(
        "ab", "m.onnx", "--against", "build/model-prep/m.int8.onnx", "--runs", "3", "--project", str(proj)
    )
    assert code == 0, doc
    c = doc["data"]["comparison"]
    assert c["faster"] in ("a", "b", "tie")
    assert c["sizeDeltaBytes"] == doc["data"]["b"]["sizeBytes"] - doc["data"]["a"]["sizeBytes"]


# --- hostile / broken inputs never crash (exit 5) ---------------------------


def _cal_dir(proj: Path, n: int = 8) -> Path:
    import numpy as np

    cal = proj / "cal"
    cal.mkdir(exist_ok=True)
    rng = np.random.default_rng(0)
    for i in range(n):
        np.save(cal / f"s{i}.npy", rng.standard_normal((1, 3, 224, 224)).astype(np.float32))
    return cal


@pytest.mark.skipif(not HAVE_EXTRA, reason="the optional `model` extra is not installed")
@pytest.mark.parametrize("kind", ["object", "npz", "string", "garbage"])
def test_bad_calibration_files_are_coded_not_crashes(tmp_path, kind):
    import numpy as np

    proj = project(tmp_path)
    cal = _cal_dir(proj, 7)
    bad = cal / "s7.npy"
    if kind == "object":
        np.save(bad, np.array([{"a": 1}], dtype=object), allow_pickle=True)
    elif kind == "npz":
        np.savez(cal / "tmp.npz", x=np.zeros(3))
        (cal / "tmp.npz").rename(bad)
    elif kind == "string":
        np.save(bad, np.array(["a", "b"]))
    else:
        bad.write_bytes(b"not an npy at all")
    code, doc = invoke("prep", "m.onnx", "--calibration", "cal", "--project", str(proj))
    assert code == 2 and doc["issues"][0]["code"] == "model.prep-calibration-invalid"
    assert not (proj / "build" / "model-prep").exists()


@pytest.mark.skipif(not HAVE_EXTRA, reason="the optional `model` extra is not installed")
def test_onnx_with_escaping_external_data_is_a_coded_refusal(tmp_path):
    import onnx
    from onnx import TensorProto, helper

    proj = project(tmp_path)
    _cal_dir(proj)
    tensor = helper.make_tensor("w", TensorProto.FLOAT, [4], vals=[0.0] * 4)
    tensor.ClearField("float_data")
    tensor.data_location = TensorProto.EXTERNAL
    for k, v in (("location", "../escape.bin"), ("offset", "0"), ("length", "16")):
        e = tensor.external_data.add()
        e.key, e.value = k, v
    graph = helper.make_graph(
        [helper.make_node("Add", ["input", "w"], ["out"])],
        "g",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 4])],
        [helper.make_tensor_value_info("out", TensorProto.FLOAT, [1, 4])],
        initializer=[tensor],
    )
    onnx.save(helper.make_model(graph), proj / "evil.onnx")
    (tmp_path / "escape.bin").write_bytes(b"\0" * 16)
    code, doc = invoke("prep", "evil.onnx", "--calibration", "cal", "--min-samples", "1", "--project", str(proj))
    assert doc["issues"][0]["code"] in ("model.prep-calibration-invalid", "model.prep-failed"), doc
    assert code in (1, 2)
    code, doc = invoke("run", "evil.onnx", "--project", str(proj))
    assert doc["issues"][0]["code"] == "model.run-failed" and code == 1


@pytest.mark.skipif(not HAVE_EXTRA, reason="the optional `model` extra is not installed")
def test_a_broken_extra_import_is_the_extra_missing_refusal(tmp_path, monkeypatch):
    import builtins

    proj = project(tmp_path)
    _cal_dir(proj)
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("onnxruntime"):
            raise ImportError("libonnxruntime.so: cannot open shared object file")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    code, doc = invoke("prep", "m.onnx", "--calibration", "cal", "--project", str(proj))
    assert code == 1 and doc["issues"][0]["code"] == "model.model-extra-missing"
    code, doc = invoke("run", "m.onnx", "--project", str(proj))
    assert code == 1 and doc["issues"][0]["code"] == "model.model-extra-missing"


@pytest.mark.skipif(not HAVE_EXTRA, reason="the optional `model` extra is not installed")
def test_pickled_input_sample_is_refused(tmp_path):
    import numpy as np

    proj = project(tmp_path)
    np.save(proj / "obj.npy", np.array([{"a": 1}], dtype=object), allow_pickle=True)
    code, doc = invoke("run", "m.onnx", "--input", "obj.npy", "--project", str(proj))
    assert code == 1 and doc["issues"][0]["code"] == "model.run-failed"


@pytest.mark.skipif(not HAVE_EXTRA, reason="the optional `model` extra is not installed")
def test_prep_never_clobbers_and_defaults_to_its_own_dir(tmp_path):
    proj = project(tmp_path)
    _cal_dir(proj)
    code, doc = invoke("prep", "m.onnx", "--calibration", "cal", "--project", str(proj))
    assert code == 0, doc
    out = proj / "build" / "model-prep" / "m.int8.onnx"
    assert out.is_file() and not (proj / "build" / "models").exists()
    assert [p.name for p in out.parent.iterdir()] == ["m.int8.onnx"]  # no staging left
    before = out.read_bytes()
    code, doc = invoke("prep", "m.onnx", "--calibration", "cal", "--project", str(proj))
    assert code == 2 and doc["issues"][0]["code"] == "model.prep-output-exists"
    assert out.read_bytes() == before


@pytest.mark.skipif(not HAVE_EXTRA, reason="the optional `model` extra is not installed")
def test_a_users_pre_onnx_next_to_the_output_is_untouched(tmp_path):
    proj = project(tmp_path)
    _cal_dir(proj)
    mine = proj / "out"
    mine.mkdir()
    (mine / "m.int8.pre.onnx").write_bytes(b"mine")
    code, doc = invoke("prep", "m.onnx", "--calibration", "cal", "--out", "out", "--project", str(proj))
    assert code == 0, doc
    assert (mine / "m.int8.pre.onnx").read_bytes() == b"mine"


def test_counts_must_be_positive(tmp_path):
    proj = project(tmp_path)
    for args in (["run", "m.onnx", "--runs", "0"], ["ab", "m.onnx", "--against", "m.onnx", "--runs", "-1"],
                 ["prep", "m.onnx", "--calibration", "c", "--min-samples", "0"]):
        code, doc = invoke(*args, "--project", str(proj))
        assert code == 2 and doc["issues"][0]["code"] == "model.unexpected-argument", args


@pytest.mark.skipif(not HAVE_EXTRA, reason="the optional `model` extra is not installed")
def test_a_model_that_will_not_load_is_prep_failed_not_a_calibration_error(tmp_path):
    proj = project(tmp_path)
    _cal_dir(proj)
    (proj / "broken.onnx").write_bytes(b"not an onnx model")
    code, doc = invoke("prep", "broken.onnx", "--calibration", "cal", "--project", str(proj))
    assert code == 1 and doc["issues"][0]["code"] == "model.prep-failed"


@pytest.mark.skipif(not HAVE_EXTRA, reason="the optional `model` extra is not installed")
def test_failure_removes_every_directory_the_run_created(tmp_path):
    proj = project(tmp_path)
    cal = _cal_dir(proj, 2)  # too few samples -> refused after validation... before mkdir
    code, doc = invoke("prep", "m.onnx", "--calibration", "cal", "--out", "build/deep/out", "--project", str(proj))
    assert code == 2 and not (proj / "build").exists()
    # a failure AFTER the directories exist (quantize error) also cleans them up
    import tan.model.prep as prep

    _cal_dir(proj, 8)
    original = prep.quantize

    def boom(*a, **k):
        raise prep.PrepError("quantization failed: forced")

    prep.quantize = boom
    try:
        code, doc = invoke("prep", "m.onnx", "--calibration", "cal", "--out", "build/deep/out", "--project", str(proj))
    finally:
        prep.quantize = original
    assert code == 1 and doc["issues"][0]["code"] == "model.prep-failed"
    assert not (proj / "build").exists()


@pytest.mark.skipif(not HAVE_EXTRA, reason="the optional `model` extra is not installed")
def test_prep_publishes_through_the_no_hardlink_fallback(tmp_path, monkeypatch):
    proj = project(tmp_path)
    _cal_dir(proj)

    def nope(*a, **k):
        raise OSError("hard links unsupported")

    monkeypatch.setattr("tan.core.publish.os.link", nope)
    code, doc = invoke("prep", "m.onnx", "--calibration", "cal", "--project", str(proj))
    assert code == 0, doc
    assert (proj / "build" / "model-prep" / "m.int8.onnx").is_file()


def test_broken_extra_message_quotes_the_error_separately():
    from tan.core.model_host import broken_extra_message

    msg = broken_extra_message("prep", "libfoo.so missing")
    assert "libfoo.so missing" in msg and 'pip install "tan-cli[model]"' in msg


def test_a_stray_min_samples_is_refused_even_at_its_default_value(tmp_path):
    """tan-cli#1497: `--min-samples 8` typed on a non-prep subcommand used to
    equal the default and be silently dropped."""
    proj = project(tmp_path)
    code, doc = invoke("run", "m.onnx", "--min-samples", "8", "--project", str(proj))
    assert code == 2 and doc["issues"][0]["code"] == "model.unexpected-argument"
    assert "--min-samples" in doc["issues"][0]["message"]
