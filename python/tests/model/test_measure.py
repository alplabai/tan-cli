# SPDX-License-Identifier: Apache-2.0
"""`tan.model.measure` host tier (tan-cli#1287). Ported from alp-sdk
`tests/scripts/test_alp_model_measure.py` (host-tier parts only)."""
import pytest
from tan.model.measure import RunResult, compare


def result(latency):
    return RunResult("cpu-host", latency, 0, None, None, 5)


def test_compare_faster_tie_and_ratio():
    assert compare(result(2.0), result(1.0)).faster == "b"
    assert compare(result(1.0), result(2.0)).latency_ratio == 2.0
    assert compare(result(1.0), result(1.0)).faster == "tie"


def test_compare_zero_latency_has_no_ratio_and_size_delta():
    c = compare(result(0.0), result(1.0), size_a=10, size_b=4)
    assert c.latency_ratio is None and c.size_delta_bytes == -6
    assert compare(result(1.0), result(2.0)).size_delta_bytes is None


# tan-cli#1486: host run honours each input's dtype and feeds every input.
class _Inp:
    def __init__(self, name, shape, type_):
        self.name, self.shape, self.type = name, shape, type_


def _fake_ort(monkeypatch, inputs, seen):
    import sys
    import types

    class Sess:
        def __init__(self, *a, **k):
            pass

        def get_inputs(self):
            return inputs

        def run(self, _outs, feed):
            seen.append(feed)
            import numpy as np
            return [np.array([[0.0, 1.0]])]

    mod = types.ModuleType("onnxruntime")
    mod.InferenceSession = Sess
    monkeypatch.setitem(sys.modules, "onnxruntime", mod)


def test_run_host_keeps_integer_dtype_and_feeds_every_input(monkeypatch):
    np = pytest.importorskip("numpy")
    from tan.model.measure import run_host
    seen = []
    _fake_ort(monkeypatch, [_Inp("ids", [1, 4], "tensor(int64)"),
                            _Inp("mask", [1, 4], "tensor(uint8)")], seen)
    run_host("m.onnx", np.ones((1, 4), dtype=np.int64), runs=1)
    feed = seen[0]
    assert feed["ids"].dtype == np.int64 and feed["mask"].dtype == np.uint8
    assert set(feed) == {"ids", "mask"}


def test_default_input_follows_the_input_dtype(monkeypatch):
    np = pytest.importorskip("numpy")
    from tan.model.measure import default_input
    _fake_ort(monkeypatch, [_Inp("x", ["batch", 3], "tensor(uint8)")], [])
    x = default_input("m.onnx")
    assert x.dtype == np.uint8 and x.shape == (1, 3)


# tan-cli#1497: a user sample cast to the model input dtype is reported.
def test_run_host_reports_a_float_to_int_cast(monkeypatch):
    np = pytest.importorskip("numpy")
    from tan.model.measure import run_host
    _fake_ort(monkeypatch, [_Inp("ids", [1, 4], "tensor(int64)")], [])
    res = run_host("m.onnx", np.full((1, 4), 1.5, dtype=np.float32), runs=1)
    assert res.input_cast is not None and "truncated" in res.input_cast


def test_run_host_reports_nothing_when_the_dtype_already_matches(monkeypatch):
    np = pytest.importorskip("numpy")
    from tan.model.measure import run_host
    _fake_ort(monkeypatch, [_Inp("ids", [1, 4], "tensor(int64)")], [])
    assert run_host("m.onnx", np.ones((1, 4), dtype=np.int64), runs=1).input_cast is None


def test_describe_input_cast_flags_narrowing_and_ignores_identity():
    pytest.importorskip("numpy")
    from tan.model.measure import describe_input_cast
    assert describe_input_cast("float64", "float32") is not None
    assert describe_input_cast("float32", "float32") is None
