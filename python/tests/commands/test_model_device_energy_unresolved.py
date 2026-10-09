# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1387: an energy delta below the rail's noise is not reported as a
number. The bench capture printed `RESULT FAIL: delta not resolvable` and tan
still returned `ok:true` with `energy.valueMjPerInference` 2.446e-06 and a
spread three times larger."""
from __future__ import annotations

from tests.commands.test_model_device_command import _via_cli, capture

_CFG = (
    'ENERGY-CFG {"rail": "+3V3", "power_lsb_w": 0.001, "cycles_per_s": 1000000, '
    '"windows": 2, "npu_dispatched": true}\n'
)


def _pair(window: int, active_raw: int) -> str:
    """One (active, idle) pair over 1 s, 10 inferences, idle at 1000 counts."""
    return (
        f"ENERGY-S {window} active 0 {active_raw}\nENERGY-S {window} active 1000000 {active_raw}\n"
        f"ENERGY-S {window} idle 0 1000\nENERGY-S {window} idle 1000000 1000\n"
        f"ENERGY-W {window} active 2 1000000 1000.0 10\n"
        f"ENERGY-W {window} idle 2 1000000 1000.0 0\n"
    )


def _noisy(extra: str = "") -> str:
    # Per-pair deltas +0.5 and -0.4 mJ/inference: mean 0.05, spread ~0.64.
    return _CFG + _pair(0, 1005) + _pair(1, 996) + extra


_DEVICE_FAIL = (
    "RESULT FAIL: delta not resolvable -- mean=0.000002 spread=0.000008 all_windows_ok=1 "
    "rail=+3V3 (inference load below this rail's noise, or wrong rail)\n"
)


def _unresolved_issue(doc: dict) -> dict:
    found = [i for i in doc["issues"] if i["code"] == "model.device-energy-unresolved"]
    assert len(found) == 1, doc["issues"]
    return found[0]


def test_a_mean_inside_its_own_noise_is_not_reported_as_energy(tmp_path):
    code, doc = _via_cli(tmp_path, _noisy())
    r = doc["data"]["result"]
    assert code == 0, doc
    assert r["energy"] is None
    assert r["latencyMs"] == 100.0  # latency is a separate measurement and stays
    kept = r["diagnostics"]["unresolvedEnergy"]
    assert kept["rail"] == "+3V3" and kept["pairs"] == 2 and kept["verdict"] == "host"
    assert kept["spreadMj"] > kept["valueMjPerInference"] > 0
    issue = _unresolved_issue(doc)
    assert issue["severity"] == "warning"
    assert "`+3V3`" in issue["message"]


def test_the_device_fail_verdict_wins_and_is_surfaced(tmp_path):
    """A single pair has no spread for the host to test, so only the device's
    own `RESULT FAIL` line can say the delta is noise -- the bench shape."""
    code, doc = _via_cli(tmp_path, capture(extra=_DEVICE_FAIL))
    r = doc["data"]["result"]
    assert code == 0, doc
    assert r["energy"] is None
    assert r["diagnostics"]["unresolvedEnergy"]["verdict"] == "device"
    assert _DEVICE_FAIL.strip() in r["diagnostics"]["warnLines"]
    assert "RESULT FAIL: delta not resolvable" in _unresolved_issue(doc)["message"]


def test_a_device_pass_verdict_keeps_the_energy(tmp_path):
    passed = "RESULT PASS: 0.050000 mJ/inference (+/-0.636396) rail=+3V3 n=10 pairs=2/2 all_pairs_ok=1\n"
    code, doc = _via_cli(tmp_path, _noisy(passed))
    assert code == 0, doc
    assert doc["data"]["result"]["energy"] is not None
    assert not [i for i in doc["issues"] if i["code"] == "model.device-energy-unresolved"]


def test_a_resolvable_capture_is_unchanged(tmp_path):
    code, doc = _via_cli(tmp_path, capture())
    assert code == 0 and doc["issues"] == []
    assert doc["data"]["result"]["energy"]["valueMjPerInference"] > 0
    assert doc["data"]["result"]["diagnostics"]["unresolvedEnergy"] is None
