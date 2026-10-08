# SPDX-License-Identifier: Apache-2.0
"""`tan model run --device` / `ab --device` without `--capture` (tan-cli#1287, live
tier): the Flow C run function is stubbed, so no probe, J-Link or board is touched."""
from __future__ import annotations

import json
from pathlib import Path

import typer
from typer.testing import CliRunner

from tan.commands import flash_cmd
from tan.commands.model_cmd import model
from tan.envelope import Issue
from tan.exit_codes import ExitCode

app = typer.Typer(add_completion=False)
app.command("model")(model)
runner = CliRunner()

CONSOLE = (
    'ENERGY-CFG {"rail": "+5V", "power_lsb_w": 0.001, "cycles_per_s": 1000000, '
    '"windows": 1, "npu_dispatched": true, "model": "kws"}\n'
    "ENERGY-S 0 active 0 3000\nENERGY-S 0 active 1000000 3000\n"
    "ENERGY-S 0 idle 0 1000\nENERGY-S 0 idle 1000000 1000\n"
    "ENERGY-W 0 active 2 1000000 1000.0 {n}\n"
    "ENERGY-W 0 idle 2 1000000 1000.0 0\n"
)

MANIFEST = (
    "schema_version: 1\nhw_info: {sku: S}\nslices:\n"
    "- {core_id: m55_he, os: zephyr, output_artefact: zephyr.elf, status: ok,\n"
    "   flash_method: ram_run_only}\nhelper_mcus: []\nboot_order: []\n"
)


def project(root: Path, name: str, manifest: str | None = MANIFEST) -> Path:
    p = root / name
    (p / "build").mkdir(parents=True)
    (p / "board.yaml").write_text("som: {sku: E1M-AEN801}\n", encoding="utf-8", newline="\n")
    if manifest is not None:
        (p / "build" / "system-manifest.yaml").write_text(manifest, encoding="utf-8", newline="\n")
    return p


def flow_result(text="x", status="ok", rc=0, issues=(), selected="ram"):
    console = {"selected": selected, "requested": True}
    if text is not None:
        console["text"] = text
    entry = {"status": status, "message": "stub", "ramConsole": console}
    return ExitCode(rc), {"entries": [entry]}, list(issues), [], None


class Stub:
    def __init__(self, monkeypatch, results):
        self.calls, self.results = [], list(results)
        monkeypatch.setattr(flash_cmd, "_run", self)

    def __call__(self, **kw):
        self.calls.append(kw)
        return self.results.pop(0)


def invoke(*args):
    result = runner.invoke(app, ["--format", "json", *args], catch_exceptions=False)
    return result.exit_code, json.loads(result.stdout)


def test_happy_path_ram_runs_and_parses_the_console(tmp_path, monkeypatch):
    p = project(tmp_path, "a")
    stub = Stub(monkeypatch, [flow_result(CONSOLE.replace("{n}", "10"))])
    code, doc = invoke(
        "run", "--device", "--project", str(p), "--confirm", "--wait", "2", "--core", "m55_he",
        "--probe-usb-path", "3-4.2", "--probe-serial", "603000869", "--jlink", "/x/JLinkExe",
    )
    assert code == 0, doc
    r = doc["data"]["result"]
    assert r["tier"] == "device" and r["source"] == "live" and r["latencyMs"] == 100.0
    assert r["model"] == "kws" and doc["issues"] == []
    (kw,) = stub.calls
    assert kw["ram"] is True and kw["ram_console"] is True and kw["confirm_flag"] is True
    assert kw["core"] == "m55_he" and kw["ram_wait"] == 2.0 and kw["dry_run"] is False
    assert kw["probe_usb_path"] == "3-4.2" and kw["probe_serial"] == "603000869"
    assert kw["jlink_path"] == "/x/JLinkExe" and Path(kw["app_path"]) == p
    assert "readback" not in kw and "recover" not in kw  # --ram only: no MRAM write option is ever forwarded


def test_capture_rows_do_not_gain_a_source(tmp_path):
    p = project(tmp_path, "a")
    (p / "cap.txt").write_text(CONSOLE.replace("{n}", "10"), encoding="utf-8", newline="\n")
    code, doc = invoke("run", "--device", "--capture", "cap.txt", "--project", str(p))
    assert code == 0 and "source" not in doc["data"]["result"]


def test_confirm_is_required(tmp_path, monkeypatch):
    monkeypatch.delenv("ALP_FLASH_FORCE", raising=False)
    p = project(tmp_path, "a")
    Stub(monkeypatch, [flow_result(None, status="planned")])
    code, doc = invoke("run", "--device", "--project", str(p))
    assert code == 2 and doc["issues"][0]["code"] == "model.device-confirm-required"


def test_no_project_manifest_or_itcm_slice(tmp_path, monkeypatch):
    stub = Stub(monkeypatch, [])
    code, doc = invoke("run", "--device", "--confirm", "--project", str(tmp_path / "none"))
    assert code == 2 and doc["issues"][0]["code"] == "model.device-no-project"
    p = project(tmp_path, "nomanifest", manifest=None)
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    assert code == 2 and doc["issues"][0]["code"] == "model.device-no-manifest"
    p = project(tmp_path, "mram", manifest=MANIFEST.replace("ram_run_only", "zephyr_west_flash"))
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    assert code == 2 and doc["issues"][0]["code"] == "model.device-no-itcm-slice"
    assert stub.calls == []  # nothing reaches the probe


def test_bad_probe_usb_path_is_refused_before_flashing(tmp_path, monkeypatch):
    p = project(tmp_path, "a")
    stub = Stub(monkeypatch, [])
    code, doc = invoke("run", "--device", "--confirm", "--probe-usb-path", "bogus", "--project", str(p))
    assert code == 2 and doc["issues"][0]["code"] == "model.device-probe-invalid" and stub.calls == []


def test_empty_console_is_refused(tmp_path, monkeypatch):
    p = project(tmp_path, "a")
    for text, selected in (("  \n", "ram"), (None, "uart")):
        Stub(monkeypatch, [flow_result(text, selected=selected)])
        code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
        assert code == 2 and doc["issues"][0]["code"] == "model.device-console-empty", selected


def test_flash_failure_is_passed_through_verbatim(tmp_path, monkeypatch):
    p = project(tmp_path, "a")
    boom = Issue("flash.ram-core-mismatch", "error", "HP evidence: refusing")
    Stub(monkeypatch, [flow_result(None, status="failed", rc=1, issues=[boom])])
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    assert code == 2
    assert doc["issues"] == [{"code": "flash.ram-core-mismatch", "severity": "error",
                              "message": "HP evidence: refusing"}]
    Stub(monkeypatch, [flow_result(None, status="failed", rc=1)])
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    assert code == 2 and doc["issues"][0]["code"] == "model.device-flash-failed"


def test_ab_runs_each_project_in_turn(tmp_path, monkeypatch):
    a, b = project(tmp_path, "a"), project(tmp_path, "b")
    stub = Stub(monkeypatch, [flow_result(CONSOLE.replace("{n}", "10")), flow_result(CONSOLE.replace("{n}", "20"))])
    code, doc = invoke("ab", "--device", "--confirm", "--project", str(a), "--against-project", str(b))
    assert code == 0, doc
    assert [Path(c["app_path"]) for c in stub.calls] == [a, b]
    assert doc["data"]["a"]["source"] == doc["data"]["b"]["source"] == "live"
    assert doc["data"]["comparison"]["faster"] == "b"


def test_ab_live_needs_a_second_project(tmp_path, monkeypatch):
    a = project(tmp_path, "a")
    stub = Stub(monkeypatch, [flow_result(CONSOLE.replace("{n}", "10"))])
    code, doc = invoke("ab", "--device", "--confirm", "--project", str(a))
    assert code == 2 and doc["issues"][0]["code"] == "model.device-live-needs-two-projects"
    assert stub.calls == []


def test_ab_stops_at_the_first_failing_project(tmp_path, monkeypatch):
    a, b = project(tmp_path, "a"), project(tmp_path, "b")
    boom = Issue("flash.no-probe", "error", "no J-Link")
    stub = Stub(monkeypatch, [flow_result(None, status="failed", rc=1, issues=[boom])])
    code, doc = invoke("ab", "--device", "--confirm", "--project", str(a), "--against-project", str(b))
    assert code == 2 and doc["issues"][0]["code"] == "flash.no-probe" and len(stub.calls) == 1


def test_live_flags_need_a_live_device_run(tmp_path):
    p = project(tmp_path, "a")
    (p / "cap.txt").write_text(CONSOLE.replace("{n}", "10"), encoding="utf-8", newline="\n")
    for args in (("run", "--confirm"), ("list", "--wait", "3"),
                 ("run", "--device", "--capture", "cap.txt", "--confirm")):
        code, doc = invoke(*args, "--project", str(p))
        assert code == 2 and doc["issues"][-1]["code"] == "model.unexpected-argument", args
