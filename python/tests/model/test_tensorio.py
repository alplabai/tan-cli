# SPDX-License-Identifier: Apache-2.0
"""TFLite tensor-I/O + operator-walk extraction (Stage-1a Tensor records for
the manifest; OpDesc/MAC records for tan.model.analyze's static NPU screen)."""
import sys
from pathlib import Path

import pytest

from tan.model.tensorio import extract_io, extract_ops
from tan.model.manifest import Tensor

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _ROOT / "tests/fixtures/models/tiny_int8.tflite"


def test_extract_io_non_tflite_returns_empty(tmp_path):
    src = tmp_path / "m.onnx"
    src.write_bytes(b"ONNX-BYTES")
    assert extract_io(src) == ([], [])


def test_extract_io_malformed_tflite_returns_empty(tmp_path):
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-NOT-REALLY")
    # Contract: an unparseable .tflite always yields no I/O metadata and never raises
    # (parse-exception path when tflite is installed; ImportError path when it isn't).
    assert extract_io(src) == ([], [])


def test_extract_io_parses_tiny_fixture():
    pytest.importorskip("tflite")
    ins, outs = extract_io(_FIXTURE)
    # scale is stored as float32 in the flatbuffer; compare with approx to tolerate
    # the float32 -> float64 precision artifact (e.g. 0.004 -> 0.004000000189989805).
    assert len(ins) == 1
    assert ins[0].dtype == "int8" and ins[0].rank == 2 and ins[0].shape == [1, 4]
    assert ins[0].zp == -1
    assert ins[0].scale == pytest.approx(0.0078125, rel=1e-5)
    assert len(outs) == 1
    assert outs[0].dtype == "int8" and outs[0].rank == 2 and outs[0].shape == [1, 2]
    assert outs[0].zp == 2
    assert outs[0].scale == pytest.approx(0.004, rel=1e-5)


def test_extract_io_honors_raw_bytes_without_reading_file(tmp_path):
    # raw= lets build_model pass already-read bytes (read source once). The .tflite
    # path here does NOT exist on disk, so a non-empty result proves raw was used.
    pytest.importorskip("tflite")
    raw = _FIXTURE.read_bytes()
    ins, outs = extract_io(tmp_path / "does-not-exist.tflite", raw=raw)
    assert len(ins) == 1 and ins[0].shape == [1, 4]
    assert len(outs) == 1 and outs[0].shape == [1, 2]


# ---------------------------------------------------------------------------
# extract_ops: the operator walk + MAC weighting (tan.model.analyze's input)
# ---------------------------------------------------------------------------

def test_extract_ops_non_tflite_returns_empty(tmp_path):
    src = tmp_path / "m.onnx"
    src.write_bytes(b"ONNX-BYTES")
    # ONNX operator extraction is a follow-on; a .onnx source always yields []
    # here, never raises, so the ONNX-ingesting backends report `undetermined`
    # rather than a guess (tan.model.analyze).
    assert extract_ops(src) == []


def test_extract_ops_malformed_tflite_returns_empty(tmp_path):
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-NOT-REALLY-GARBAGE-BYTES")
    assert extract_ops(src) == []


def test_extract_ops_missing_tflite_reader_returns_empty(tmp_path, monkeypatch):
    # Force the ImportError branch even on a machine where `tflite` (the
    # `model-io` extra) IS installed: `sys.modules[name] = None` makes a bare
    # `import tflite` raise ImportError, same as it not being installed at
    # all -- this is the "missing tflite reader" case, distinct from the
    # malformed-bytes case above.
    monkeypatch.setitem(sys.modules, "tflite", None)
    assert extract_ops(_FIXTURE) == []


def test_extract_ops_parses_tiny_fixture_with_macs():
    pytest.importorskip("tflite")
    ops = extract_ops(_FIXTURE)
    assert len(ops) == 1
    op = ops[0]
    assert op.op == "FULLY_CONNECTED"
    # weights [2, 4] (const), bias [2] (const), input [1, 4], output [1, 2] --
    # MACs = out_elems(1*2) * in_features(4) = 8.
    assert op.macs == 8
    assert len(op.inputs) == 3 and len(op.outputs) == 1
    weight = next(t for t in op.inputs if t.is_const and len(t.shape) == 2)
    assert weight.shape == [2, 4]
    assert weight.dtype == "int8"
    activation = next(t for t in op.inputs if not t.is_const)
    assert activation.shape == [1, 4]
    assert op.outputs[0].shape == [1, 2] and not op.outputs[0].is_const


def test_extract_ops_honors_raw_bytes_without_reading_file(tmp_path):
    pytest.importorskip("tflite")
    raw = _FIXTURE.read_bytes()
    ops = extract_ops(tmp_path / "does-not-exist.tflite", raw=raw)
    assert len(ops) == 1 and ops[0].op == "FULLY_CONNECTED" and ops[0].macs == 8
