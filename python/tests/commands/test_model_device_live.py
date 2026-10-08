# SPDX-License-Identifier: Apache-2.0
"""`tan model run --device` / `ab --device` without `--capture` (tan-cli#1287, live
tier): the Flow C run function is stubbed, so no probe, J-Link or board is touched."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
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
    entry = {"status": status, "message": "stub", "ramConsole": console,
             "jlink": {"transcriptPath": "/t/ram_run-m55_he-1.log", "attachedCore": {"apAddr": "0x00300000"},
                       "dpidr": "0x4C013477"}}
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
    assert Path(r["project"]) == p
    assert r["flash"] == {
        "core": "m55_he", "wait": 2.0, "transcriptPath": "/t/ram_run-m55_he-1.log",
        "attachedCore": {"apAddr": "0x00300000"}, "dpidr": "0x4C013477",
    }
    assert r["model"] == "kws" and doc["issues"] == []
    (kw,) = stub.calls
    assert kw["ram"] is True and kw["ram_console"] is True and kw["confirm_flag"] is True
    assert kw["core"] == "m55_he" and kw["ram_wait"] == 2.0 and kw["dry_run"] is False
    assert kw["probe_usb_path"] == "3-4.2" and kw["probe_serial"] == "603000869"
    assert kw["jlink_path"] == "/x/JLinkExe" and Path(kw["app_path"]) == p
    assert "readback" not in kw and "recover" not in kw  # --ram only: no MRAM write option is ever forwarded


def test_capture_rows_are_marked_source_capture(tmp_path):
    p = project(tmp_path, "a")
    (p / "cap.txt").write_text(CONSOLE.replace("{n}", "10"), encoding="utf-8", newline="\n")
    code, doc = invoke("run", "--device", "--capture", "cap.txt", "--project", str(p))
    assert code == 0 and doc["data"]["result"]["source"] == "capture"
    assert "flash" not in doc["data"]["result"]


def test_confirm_is_required(tmp_path, monkeypatch):
    monkeypatch.delenv("ALP_FLASH_FORCE", raising=False)
    p = project(tmp_path, "a")
    stub = Stub(monkeypatch, [flow_result(None, status="planned")])
    code, doc = invoke("run", "--device", "--project", str(p))
    assert code == 2 and doc["issues"][0]["code"] == "model.device-confirm-required"
    assert stub.calls[0]["confirm_flag"] is False
    msg = doc["issues"][0]["message"]
    assert "--confirm" in msg and "ALP_FLASH_FORCE=1" in msg and "flash_args.confirm" in msg


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
    assert code == int(ExitCode.RUNTIME_FAILURE)  # Flow C's own exit code, not remapped
    assert doc["issues"] == [{"code": "flash.ram-core-mismatch", "severity": "error",
                              "message": "HP evidence: refusing"}]
    Stub(monkeypatch, [flow_result(None, status="failed", rc=1)])
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    assert code == 1 and doc["issues"][0]["code"] == "model.device-flash-failed"


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
    assert code == 1 and doc["issues"][0]["code"] == "flash.no-probe" and len(stub.calls) == 1


def test_live_flags_need_a_live_device_run(tmp_path):
    p = project(tmp_path, "a")
    (p / "cap.txt").write_text(CONSOLE.replace("{n}", "10"), encoding="utf-8", newline="\n")
    for args in (("run", "--confirm"), ("list", "--wait", "3"),
                 ("run", "--device", "--capture", "cap.txt", "--confirm")):
        code, doc = invoke(*args, "--project", str(p))
        assert code == 2 and doc["issues"][-1]["code"] == "model.unexpected-argument", args


def test_flow_c_warnings_survive_success_and_every_issue_survives_failure(tmp_path, monkeypatch):
    p = project(tmp_path, "a")
    warn = [
        Issue("flash.dpidr-preflight-unarmed", "warning", "no wrong-board guard"),
        Issue("flash.probe-unverified", "warning", "probe not verified"),
        Issue("flash.ram-console-symbol-missing", "warning", "no ram_console_buf"),
    ]
    Stub(monkeypatch, [flow_result(CONSOLE.replace("{n}", "10"), issues=warn)])
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    assert code == 0 and [i["code"] for i in doc["issues"]] == [i.code for i in warn]
    err = Issue("flash.ram-core-unconfirmed", "error", "cannot confirm HE")
    Stub(monkeypatch, [flow_result(None, status="failed", rc=1, issues=[warn[0], err])])
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    assert code == 1
    assert [i["code"] for i in doc["issues"]] == ["flash.dpidr-preflight-unarmed", "flash.ram-core-unconfirmed"]
    Stub(monkeypatch, [flow_result("  ", issues=[warn[2]])])  # the empty console keeps the explanation
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    assert [i["code"] for i in doc["issues"]] == ["flash.ram-console-symbol-missing", "model.device-console-empty"]


def test_ab_merges_both_sides_issues_and_records_each_project(tmp_path, monkeypatch):
    a, b = project(tmp_path, "a"), project(tmp_path, "b")
    wa = Issue("flash.probe-unverified", "warning", "A")
    wb = Issue("flash.jlink-reset-unconfirmed", "warning", "B")
    Stub(monkeypatch, [flow_result(CONSOLE.replace("{n}", "10"), issues=[wa]),
                       flow_result(CONSOLE.replace("{n}", "20"), issues=[wb])])
    code, doc = invoke("ab", "--device", "--confirm", "--project", str(a), "--against-project", str(b))
    assert code == 0 and [i["code"] for i in doc["issues"]] == [wa.code, wb.code]
    assert Path(doc["data"]["a"]["project"]) == a and Path(doc["data"]["b"]["project"]) == b


def test_ab_mixed_live_and_capture_is_refused_before_anything_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("ALP_FLASH_FORCE", "1")  # armed: only the refusal can stop the board
    a, b = project(tmp_path, "a"), project(tmp_path, "b")
    (a / "cap.txt").write_text(CONSOLE.replace("{n}", "10"), encoding="utf-8", newline="\n")
    stub = Stub(monkeypatch, [flow_result(CONSOLE.replace("{n}", "10")), flow_result(CONSOLE.replace("{n}", "20"))])
    for args in (
        ("--capture", "cap.txt"),                                  # A capture, B live
        ("--against-capture", "cap.txt", "--against-project", str(b)),  # A live, B capture
    ):
        code, doc = invoke("ab", "--device", "--project", str(a), *args)
        assert code == 2 and doc["issues"][-1]["code"] in (
            "model.device-ab-mixed-sources", "model.unexpected-argument"), args
    code, doc = invoke("ab", "--device", "--project", str(a), "--against-capture", "cap.txt")
    assert code == 2 and doc["issues"][0]["code"] == "model.device-ab-mixed-sources"
    assert stub.calls == []  # nothing reached the probe


def test_board_yaml_is_forwarded_to_flow_c(tmp_path, monkeypatch):
    p = project(tmp_path, "a")
    (p / "alt.yaml").write_text("som: {sku: E1M-AEN801}\n", encoding="utf-8", newline="\n")
    stub = Stub(monkeypatch, [flow_result(CONSOLE.replace("{n}", "10"))])
    code, _ = invoke("run", "--device", "--confirm", "--project", str(p), "--board-yaml", "alt.yaml")
    assert code == 0 and stub.calls[0]["board_yaml"] == "alt.yaml"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executables (the Flow C stub J-Link)")
def test_real_flow_c_spawns_nothing_without_confirm_and_is_armed_by_the_env(tmp_path, monkeypatch):
    from .test_flash_ram import FakeJlink, _manifest, _setup

    monkeypatch.delenv("ALP_FLASH_FORCE", raising=False)
    _setup(tmp_path, monkeypatch, manifest=_manifest().replace("zephyr_west_flash", "ram_run_only"))
    (tmp_path / "board.yaml").write_text("som: {sku: E1M-AEN801}\n", encoding="utf-8", newline="\n")
    jl = FakeJlink(monkeypatch)
    args = ("run", "--device", "--project", str(tmp_path), "--sdk-root", str(tmp_path / "sdk"))
    code, doc = invoke(*args)
    assert code == 2 and doc["issues"][-1]["code"] == "model.device-confirm-required", doc
    assert not any(jl.kind(s) in ("load", "read") for s in jl.scripts)  # no load, no console read
    jl.scripts.clear()
    monkeypatch.setenv("ALP_FLASH_FORCE", "1")
    code, doc = invoke(*args)
    # armed: the load and the console read happen (the stub console is not a benchmark's, so
    # the parser refuses it -- what matters here is that Flow C ran)
    assert [jl.kind(s) for s in jl.scripts][-2:] == ["load", "read"], (doc, jl.scripts)
    assert code == 2 and doc["issues"][-1]["code"] == "model.device-capture-invalid"
