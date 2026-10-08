# SPDX-License-Identifier: Apache-2.0
"""`tan.model.prep` -- license-free quantize + accuracy report (tan-cli#1287).
Ported from alp-sdk `tests/scripts/test_alp_model_prep.py` (alp-sdk#933)."""
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("onnx")
pytest.importorskip("sympy")

from tan.model.prep import (  # noqa: E402
    PrepError,
    accuracy_delta,
    load_calibration,
    model_input,
    quantize,
    validate_calibration,
)

_ONNX = Path(__file__).resolve().parents[1] / "fixtures" / "models" / "tiny_cnn.onnx"


def make_calib(dirpath: Path, n: int, shape=(1, 3, 224, 224)) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(0)
    for i in range(n):
        np.save(dirpath / f"s{i}.npy", rng.standard_normal(shape).astype(np.float32))


def test_model_input_shape():
    name, shape = model_input(_ONNX)
    assert name == "input"
    assert shape[-2:] == [224, 224]


def test_validate_calibration_ok_and_too_few(tmp_path):
    make_calib(tmp_path / "cal", 8)
    info = validate_calibration(tmp_path / "cal", _ONNX, min_samples=8)
    assert info.samples == 8 and info.input_name == "input"
    make_calib(tmp_path / "cal2", 2)
    with pytest.raises(PrepError):
        validate_calibration(tmp_path / "cal2", _ONNX, min_samples=8)


def test_load_calibration_shape_mismatch_and_empty(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    with pytest.raises(PrepError, match="no .npy"):
        load_calibration(d, [1, 3, 224, 224])
    np.save(d / "wrong.npy", np.zeros((1, 3, 64, 64), dtype=np.float32))
    with pytest.raises(PrepError):
        load_calibration(d, [1, 3, 224, 224])


def test_quantize_and_accuracy_report(tmp_path):
    cal = tmp_path / "cal"
    make_calib(cal, 8)
    q = quantize(_ONNX, tmp_path / "tiny.int8.onnx", cal)
    assert q.is_file() and q.stat().st_size > 0
    import onnxruntime as ort

    ort.InferenceSession(str(q), providers=["CPUExecutionProvider"])
    rep = accuracy_delta(_ONNX, q, cal)
    assert rep.samples == 8
    assert 0.0 <= rep.top1_agreement_pct <= 100.0
    assert -1.0 <= rep.mean_cosine <= 1.0001
    assert rep.verdict in ("good", "degraded")
    assert (rep.guidance is None) == (rep.verdict == "good")


def test_quantize_refuses_non_onnx(tmp_path):
    with pytest.raises(PrepError, match="supports .onnx"):
        quantize(tmp_path / "m.tflite", tmp_path / "o.onnx", tmp_path)
