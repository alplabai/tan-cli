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
    assert (proj / "build" / "models" / "m.int8.onnx").is_file()
    assert doc["data"]["calibration"]["samples"] == 8
    assert doc["data"]["accuracy"]["verdict"] in ("good", "degraded")
    assert doc["data"]["output"].endswith("m.int8.onnx")
