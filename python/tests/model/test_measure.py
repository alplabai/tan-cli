# SPDX-License-Identifier: Apache-2.0
"""`tan.model.measure` host tier (tan-cli#1287). Ported from alp-sdk
`tests/scripts/test_alp_model_measure.py` (host-tier parts only)."""
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
