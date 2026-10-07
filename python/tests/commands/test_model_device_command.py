# SPDX-License-Identifier: Apache-2.0
"""`tan model run --device` / `ab --device` (tan-cli#1287): console captures in,
`tier: device` envelope out. No hardware, no onnxruntime."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from typer.testing import CliRunner

from tan.commands import model_device_cmd
from tan.commands.build_output import resolve_project_context
from tan.commands.model_cmd import model
from tan.envelope import Issue

app = typer.Typer(add_completion=False)
app.command("model")(model)
runner = CliRunner()


def capture(active_ms=1000.0, inferences=10, extra=""):
    return (
        'ENERGY-CFG {"rail": "+5V", "power_lsb_w": 0.001, "cycles_per_s": 1000000, '
        '"windows": 1, "npu_dispatched": true}\n'
        "ENERGY-S 0 active 0 3000\nENERGY-S 0 active 1000000 3000\n"
        "ENERGY-S 0 idle 0 1000\nENERGY-S 0 idle 1000000 1000\n"
        f"ENERGY-W 0 active 2 1000000 {active_ms} {inferences}\n"
        "ENERGY-W 0 idle 2 1000000 1000.0 0\n" + extra
    )


def invoke(*args):
    result = runner.invoke(app, ["--format", "json", *args], catch_exceptions=False)
    return result.exit_code, json.loads(result.stdout)


def proj(tmp_path: Path, **files: str) -> Path:
    p = tmp_path / "proj"
    p.mkdir(exist_ok=True)
    for name, text in files.items():
        (p / name).write_text(text, encoding="utf-8")
    return p


def test_run_device_reports_tier_device(tmp_path):
    p = proj(tmp_path, **{"cap.txt": capture()})
    code, doc = invoke("run", "--device", "--capture", "cap.txt", "--project", str(p))
    assert code == 0, doc
    r = doc["data"]["result"]
    assert r["tier"] == "device" and r["backend"] == "ethos-u"
    assert r["latencyMs"] == 100.0 and r["runs"] == 10
    assert r["peakSramKib"] is None and r["outputArgmax"] is None
    assert r["energy"]["scope"] == "carrier-rail-delta" and r["powerMj"] is None
    assert r["latencyScope"].startswith("window-span-per-inference")
    assert r["diagnostics"]["npuDispatched"] is True
    assert doc["issues"] == []


def test_ab_device_compares_two_captures(tmp_path):
    p = proj(tmp_path, **{"a.txt": capture(1000.0, 10), "b.txt": capture(1000.0, 20)})
    code, doc = invoke(
        "ab", "--device", "--capture", "a.txt", "--against-capture", "b.txt", "--project", str(p)
    )
    assert code == 0, doc
    c = doc["data"]["comparison"]
    assert c["faster"] == "b" and c["latencyRatio"] == 0.5 and c["sizeDeltaBytes"] is None
    assert doc["data"]["a"]["tier"] == doc["data"]["b"]["tier"] == "device"


def test_without_a_capture_or_live_flow_is_a_coded_refusal(tmp_path):
    code, doc = invoke("run", "--device", "--project", str(proj(tmp_path)))
    assert code == 2 and doc["issues"][0]["code"] == "model.device-flow-unavailable"
    assert doc["data"]["result"] is None


def test_missing_and_garbage_captures_are_coded(tmp_path):
    p = proj(tmp_path, **{"bad.txt": "hello\n", "partial.txt": capture(extra="ENERGY-WPART 0 active\n")})
    code, doc = invoke("run", "--device", "--capture", "nope.txt", "--project", str(p))
    assert code == 2 and doc["issues"][0]["code"] == "model.device-capture-missing"
    for name in ("bad.txt", "partial.txt"):
        code, doc = invoke("run", "--device", "--capture", name, "--project", str(p))
        assert code == 2 and doc["issues"][0]["code"] == "model.device-capture-invalid", name


def test_degraded_capture_is_a_warning_with_the_result_kept(tmp_path):
    p = proj(tmp_path, **{"cap.txt": capture(extra="ENERGY-WERR 0 i2c nack\n")})
    code, doc = invoke("run", "--device", "--capture", "cap.txt", "--project", str(p))
    assert code == 0 and doc["issues"][0]["code"] == "model.device-capture-degraded"
    assert doc["data"]["result"]["latencyMs"] == 100.0


def test_capture_flags_need_device_and_a_device_verb(tmp_path):
    p = proj(tmp_path, **{"cap.txt": capture()})
    code, doc = invoke("run", "--capture", "cap.txt", "--project", str(p))
    assert code == 2 and doc["issues"][-1]["code"] == "model.unexpected-argument"
    code, doc = invoke("list", "--device", "--project", str(p))
    assert code == 2 and doc["issues"][-1]["code"] == "model.unexpected-argument"


def test_a_live_flow_is_used_when_no_capture_is_given(tmp_path, monkeypatch):
    p = proj(tmp_path)
    seen = []

    def flow(context, label):
        seen.append(label)
        return capture()

    monkeypatch.setattr(model_device_cmd, "LIVE_FLOW", flow)
    code, doc = invoke("run", "--device", "--project", str(p))
    assert code == 0 and seen == [None] and doc["data"]["result"]["tier"] == "device"


def test_capture_via_returns_the_first_failure_verbatim():
    boom = Issue("flash.no-probe", "error", "no J-Link attached")
    reads = []

    assert model_device_cmd.capture_via(lambda: boom, lambda: reads.append(1) or "x") is boom
    assert reads == []  # a failed deploy never reads the console
    assert model_device_cmd.capture_via(lambda: None, lambda: "console") == "console"
    refused = Issue("monitor.no-port", "error", "no port")
    assert model_device_cmd.capture_via(lambda: None, lambda: refused) is refused


# --- adversarial captures (reviewer fixtures, tests/fixtures/device_captures) ---

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "device_captures"


def _via_cli(tmp_path, text):
    p = proj(tmp_path, **{"cap.txt": text})
    return invoke("run", "--device", "--capture", "cap.txt", "--project", str(p))


import pytest  # noqa: E402


@pytest.mark.parametrize(
    "name",
    ["cfg_list", "cfg_num", "result_list", "cfg_nan_lsb", "cfg_zero_cps", "huge_int",
     "inf_span", "nan_span", "neg_span", "zero_span", "zero_inf"],
)
def test_adversarial_fixtures_are_capture_invalid_never_a_crash(tmp_path, name):
    code, doc = _via_cli(tmp_path, (FIX / f"{name}.txt").read_text(encoding="utf-8"))
    assert code == 2, (name, doc)
    assert doc["issues"][0]["code"] == "model.device-capture-invalid", (name, doc["issues"])
    assert doc["data"]["result"] is None


_NESTED = {"brackets": "[" * 100_000, "braces": "{" * 100_000, "keys": '{"a":' * 50_000}


# Short ids on purpose: a 100k-character parametrize id lands in
# PYTEST_CURRENT_TEST, and Windows refuses environment values over 32767 chars.
@pytest.mark.parametrize("kind", sorted(_NESTED))
def test_deeply_nested_json_is_refused_not_a_recursion_crash(tmp_path, kind):
    payload = _NESTED[kind]
    for line in ("ENERGY-CFG ", "ENERGY-RESULT "):
        code, doc = _via_cli(tmp_path, f"{line}{payload}\n" + capture())
        assert code == 2 and doc["issues"][0]["code"] == "model.device-capture-invalid"


def test_good_fixture_is_clean(tmp_path):
    code, doc = _via_cli(tmp_path, (FIX / "good.txt").read_text(encoding="utf-8"))
    assert code == 0, doc
    assert doc["data"]["result"]["latencyMs"] > 0


def test_latency_only_capture_is_valid_with_a_degraded_note(tmp_path):
    code, doc = _via_cli(tmp_path, (FIX / "no_idle.txt").read_text(encoding="utf-8"))
    r = doc["data"]["result"]
    assert code == 0 and r["energy"] is None and r["latencyMs"] > 0
    assert doc["issues"][0]["code"] == "model.device-capture-degraded"
    assert "energy not derived" in doc["issues"][0]["message"]


def test_a_capture_with_only_energy_w_lines_is_latency_only(tmp_path):
    text = capture().split("ENERGY-S")[0] + "ENERGY-W 0 active 2 1000000 1000.0 10\n"
    code, doc = _via_cli(tmp_path, text)
    assert code == 0 and doc["data"]["result"]["energy"] is None
    assert doc["data"]["result"]["latencyMs"] == 100.0


def test_skipped_pairs_are_excluded_from_energy_and_reported(tmp_path):
    code, doc = _via_cli(tmp_path, (FIX / "pair_skipped.txt").read_text(encoding="utf-8"))
    r = doc["data"]["result"]
    assert code == 0 and r["energy"] is None  # the only pair was SKIPPED
    assert r["diagnostics"]["skippedPairs"] and r["diagnostics"]["energyWindows"] == []
    assert "SKIPPED" in doc["issues"][0]["message"]


def test_a_skipped_pair_does_not_pollute_the_energy_of_good_pairs(tmp_path):
    good = capture()
    # window 1: wildly different power, but the app marked its pair SKIPPED
    bad = (
        "ENERGY-S 1 active 0 90000\nENERGY-S 1 active 1000000 90000\n"
        "ENERGY-S 1 idle 0 1000\nENERGY-S 1 idle 1000000 1000\n"
        "ENERGY-W 1 active 2 1000000 1000.0 10\nENERGY-W 1 idle 2 1000000 1000.0 0\n"
        "ENERGY-PAIR 1 SKIPPED: n=10 spans 1000/1000 ms match=1 timed_out=1/0\n"
    )
    _, with_skip = _via_cli(tmp_path, good + bad)
    _, alone = _via_cli(tmp_path, good)
    assert with_skip["data"]["result"]["energy"]["valueMjPerInference"] == alone["data"]["result"]["energy"]["valueMjPerInference"]


def test_timed_out_window_is_excluded_from_latency(tmp_path):
    slow = (
        "ENERGY-W 1 active 2 1000000 9000.0 10\nENERGY-W 1 idle 2 1000000 1000.0 0\n"
        "ENERGY-WERR 1 active timed_out=1 i2c_errors=0 last_rc=0 got=2/250\n"
    )
    code, doc = _via_cli(tmp_path, capture() + slow)
    r = doc["data"]["result"]
    assert code == 0 and r["latencyMs"] == 100.0 and r["runs"] == 10
    assert doc["issues"][0]["code"] == "model.device-capture-degraded"


def test_duplicate_energy_w_is_refused(tmp_path):
    code, doc = _via_cli(tmp_path, capture() + "ENERGY-W 0 active 2 1000000 1000.0 10\n")
    assert code == 2 and doc["issues"][0]["code"] == "model.device-capture-invalid"


@pytest.mark.skipif(sys.platform == "win32", reason="/dev/zero and os.mkfifo are POSIX-only")
def test_non_regular_capture_files_are_refused_without_reading(tmp_path):
    import os

    p = proj(tmp_path)
    code, doc = invoke("run", "--device", "--capture", "/dev/zero", "--project", str(p))
    assert code == 2 and doc["issues"][0]["code"] == "model.device-capture-invalid"
    fifo = p / "fifo"
    os.mkfifo(fifo)
    code, doc = invoke("run", "--device", "--capture", "fifo", "--project", str(p))
    assert code == 2 and doc["issues"][0]["code"] == "model.device-capture-invalid"


def test_oversized_capture_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(model_device_cmd, "MAX_CAPTURE_BYTES", 64)
    code, doc = _via_cli(tmp_path, capture())
    assert code == 2 and "larger than" in doc["issues"][0]["message"]


def test_non_utf8_capture_is_decoded_with_replacement(tmp_path):
    p = proj(tmp_path)
    (p / "cap.txt").write_bytes(b"\xff\xfe noise\n" + capture().encode())
    code, doc = invoke("run", "--device", "--capture", "cap.txt", "--project", str(p))
    assert code == 0, doc


def test_argument_combinations_are_refused(tmp_path):
    p = proj(tmp_path, **{"cap.txt": capture()})
    cases = [
        ("run", "--device", "--capture", "cap.txt", "--against-capture", "cap.txt"),
        ("run", "--device", "--capture", "cap.txt", "--runs", "5"),
        ("ab", "--device", "--capture", "cap.txt", "--input", "x.npy"),
    ]
    for args in cases:
        code, doc = invoke(*args, "--project", str(p))
        assert code == 2 and doc["issues"][-1]["code"] == "model.unexpected-argument", args


def test_ab_device_without_the_b_capture_names_it(tmp_path):
    p = proj(tmp_path, **{"cap.txt": capture()})
    code, doc = invoke("ab", "--device", "--capture", "cap.txt", "--project", str(p))
    assert code == 2 and "--against-capture" in doc["issues"][0]["message"]


def test_model_size_is_statted_when_the_model_file_exists(tmp_path):
    p = proj(tmp_path, **{"cap.txt": capture(), "m.onnx": "12345"})
    code, doc = invoke("run", "m.onnx", "--device", "--capture", "cap.txt", "--project", str(p))
    assert code == 0 and doc["data"]["result"]["sizeBytes"] == 5


def test_k_cycle_get_32_latency_capture_uses_the_configured_clock_and_surfaces_warnings(tmp_path):
    # Spans share cycles_per_s's own (SysTick) clock; span_ms is an integer tick
    # (always 50 here), so cycles/ms would read ~158 MHz -- an artefact.
    code, doc = _via_cli(tmp_path, (FIX / "latency_k_cycle.txt").read_text(encoding="utf-8"))
    assert code == 0, doc
    row = doc["data"]["result"]
    assert row["latencyMs"] == pytest.approx(0.033019, abs=2e-5)  # median per window; the app's own figure is the pooled mean
    diag = row["diagnostics"]
    assert diag["cyclesPerSUsed"] == 160_000_000.0
    assert any(line.startswith("LATENCY-WARN window 0") for line in diag["warnLines"])
    assert any(line.startswith("WARN: DWT CYCCNT detail dropped") for line in diag["warnLines"])
    degraded = [i for i in doc["issues"] if i["code"] == "model.device-capture-degraded"]
    assert degraded and degraded[0]["severity"] == "warning"
