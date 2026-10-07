# SPDX-License-Identifier: Apache-2.0
"""`tan model run --device` / `ab --device` (tan-cli#1287): console captures in,
`tier: device` envelope out. No hardware, no onnxruntime."""
from __future__ import annotations

import json
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
    assert r["energy"]["scope"] == "carrier-rail-delta" and r["powerMj"] == r["energy"]["valueMjPerInference"]
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
