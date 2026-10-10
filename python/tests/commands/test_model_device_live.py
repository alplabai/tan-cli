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


def flow_result(text="x", status="ok", rc=0, issues=(), selected="ram", build_root=None):
    console = {"selected": selected, "requested": True}
    if text is not None:
        console["text"] = text
    entry = {"status": status, "message": "stub", "ramConsole": console,
             "jlink": {"transcriptPath": "/t/ram_run-m55_he-1.log", "attachedCore": {"apAddr": "0x00300000"},
                       "dpidr": "0x4C013477"}}
    data = {"entries": [entry]}
    if build_root is not None:
        data["buildRoot"] = str(build_root)
    return ExitCode(rc), data, list(issues), [], None


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
    console_path = r["flash"].pop("consolePath")
    assert Path(console_path).parent == p / "build" / "flash-logs"
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
    # tan-cli#1486: the registry's VALIDATION_FAILURE.
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
    assert code == 2 and doc["issues"][0]["code"] == "model.device-confirm-required", doc
    assert not any(jl.kind(s) in ("load", "read") for s in jl.scripts)  # no load, no console read
    jl.scripts.clear()
    monkeypatch.setenv("ALP_FLASH_FORCE", "1")
    code, doc = invoke(*args)
    # armed: the load and the console read happen (the stub console is not a benchmark's, so
    # the parser refuses it -- what matters here is that Flow C ran)
    assert [jl.kind(s) for s in jl.scripts][-2:] == ["load", "read"], (doc, jl.scripts)
    assert code == 2 and doc["issues"][-1]["code"] == "model.device-capture-invalid"


def test_confirm_required_is_first_and_does_not_say_to_actually_flash(tmp_path, monkeypatch):
    monkeypatch.delenv("ALP_FLASH_FORCE", raising=False)
    p = project(tmp_path, "a")
    nothing = Issue("flash.nothing-flashed", "warning", "no image written")
    Stub(monkeypatch, [flow_result(None, status="planned", issues=[nothing])])
    code, doc = invoke("run", "--device", "--project", str(p))
    codes = [i["code"] for i in doc["issues"]]
    assert code == 2 and codes == ["model.device-confirm-required", "flash.nothing-flashed"], codes
    msg = doc["issues"][0]["message"]
    assert "actually flash" not in msg and "to run on the device" in msg and "--confirm" in msg


def test_live_row_carries_the_console_and_saves_it_next_to_the_transcript(tmp_path, monkeypatch):
    p = project(tmp_path, "a")
    text = CONSOLE.replace("{n}", "10")
    Stub(monkeypatch, [flow_result(text)])
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    assert code == 0, doc
    row = doc["data"]["result"]
    assert row["consoleText"] == text and row["consoleTruncated"] is False
    saved = Path(row["flash"]["consolePath"])
    assert saved.parent == p / "build" / "flash-logs" and saved.name.startswith("model-console-")
    assert saved.name.endswith("Z.txt")
    assert saved.read_bytes() == text.encode("utf-8")  # newline="\n": byte-exact on every OS
    assert doc["issues"] == []


def test_console_text_is_capped_to_its_tail_but_the_saved_file_is_whole(tmp_path, monkeypatch):
    from tan.commands import model_device_cmd

    monkeypatch.setattr(model_device_cmd, "MAX_CONSOLE_TEXT_CHARS", 50)
    p = project(tmp_path, "a")
    text = CONSOLE.replace("{n}", "10")
    Stub(monkeypatch, [flow_result(text)])
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    row = doc["data"]["result"]
    assert code == 0 and row["consoleTruncated"] is True and row["consoleText"] == text[-50:]
    assert Path(row["flash"]["consolePath"]).read_bytes() == text.encode("utf-8")


def test_unsavable_console_is_a_warning_not_a_failure(tmp_path, monkeypatch):
    p = project(tmp_path, "a")
    (p / "build" / "flash-logs").write_text("a file, not a directory", encoding="utf-8", newline="\n")
    Stub(monkeypatch, [flow_result(CONSOLE.replace("{n}", "10"))])
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    assert code == 0 and [i["code"] for i in doc["issues"]] == ["model.device-console-unsaved"]
    assert "consolePath" not in doc["data"]["result"]["flash"]


def test_refusals_after_a_successful_load_keep_the_flash_provenance(tmp_path, monkeypatch):
    p = project(tmp_path, "a")
    Stub(monkeypatch, [flow_result("  \n")])
    code, doc = invoke("run", "--device", "--confirm", "--wait", "3", "--project", str(p))
    assert code == 2 and doc["issues"][-1]["code"] == "model.device-console-empty"
    flash = doc["data"]["flash"]
    assert flash["core"] == "m55_he" and flash["wait"] == 3.0
    assert flash["transcriptPath"] == "/t/ram_run-m55_he-1.log" and "consolePath" not in flash  # a blank console is not saved
    assert doc["data"]["result"] is None
    Stub(monkeypatch, [flow_result("not a benchmark console\n")])
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    assert code == 2 and doc["issues"][-1]["code"] == "model.device-capture-invalid"
    assert doc["data"]["flash"]["dpidr"] == "0x4C013477"
    assert Path(doc["data"]["flash"]["consolePath"]).read_bytes() == b"not a benchmark console\n"
    # nothing reached the board: no provenance claimed
    Stub(monkeypatch, [flow_result(None, status="failed", rc=1)])
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    assert "flash" not in doc["data"]


def test_ab_refusal_after_a_load_names_the_side_that_was_loaded(tmp_path, monkeypatch):
    a, b = project(tmp_path, "a"), project(tmp_path, "b")
    Stub(monkeypatch, [flow_result("garbage\n")])
    code, doc = invoke("ab", "--device", "--confirm", "--project", str(a), "--against-project", str(b))
    assert code == 2 and set(doc["data"]["flash"]) == {"a"}


def test_capture_invalid_suggests_raising_wait(tmp_path, monkeypatch):
    p = project(tmp_path, "a")
    Stub(monkeypatch, [flow_result("not a benchmark console\n")])
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    assert code == 2 and "raise --wait" in doc["issues"][-1]["message"]


def test_top_level_model_matches_the_row(tmp_path, monkeypatch):
    p = project(tmp_path, "a")
    Stub(monkeypatch, [flow_result(CONSOLE.replace("{n}", "10"))])
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    assert code == 0 and doc["data"]["model"] == doc["data"]["result"]["model"] == "kws"
    (p / "m.onnx").write_bytes(b"x")
    Stub(monkeypatch, [flow_result(CONSOLE.replace("{n}", "10"))])
    code, doc = invoke("run", "m.onnx", "--device", "--confirm", "--project", str(p))
    assert doc["data"]["model"] == doc["data"]["result"]["model"] and doc["data"]["model"].endswith("m.onnx")


def test_ab_refusal_on_b_after_a_loaded_keeps_a_provenance(tmp_path, monkeypatch):
    a, b = project(tmp_path, "a"), project(tmp_path, "b")
    Stub(monkeypatch, [flow_result(CONSOLE.replace("{n}", "10")), flow_result("garbage\n")])
    code, doc = invoke("ab", "--device", "--confirm", "--project", str(a), "--against-project", str(b))
    assert code == 2 and doc["issues"][-1]["code"] == "model.device-capture-invalid"
    flash = doc["data"]["flash"]
    assert set(flash) == {"a", "b"}
    assert Path(flash["a"]["consolePath"]).parent == a / "build" / "flash-logs"
    assert Path(flash["a"]["consolePath"]).is_file() and Path(flash["b"]["consolePath"]).is_file()


def test_ab_b_failing_before_its_load_still_reports_a(tmp_path, monkeypatch):
    a, b = project(tmp_path, "a"), project(tmp_path, "b")
    Stub(monkeypatch, [flow_result(CONSOLE.replace("{n}", "10")),
                       flow_result(None, status="failed", rc=1)])
    code, doc = invoke("ab", "--device", "--confirm", "--project", str(a), "--against-project", str(b))
    assert code == 2 and doc["issues"][-1]["code"] == "model.device-flash-failed"
    assert set(doc["data"]["flash"]) == {"a"}
    assert Path(doc["data"]["flash"]["a"]["consolePath"]).is_file()


def test_console_is_saved_under_the_build_root_flow_c_used(tmp_path, monkeypatch):
    p = project(tmp_path, "a")
    nested = tmp_path / "elsewhere" / "build" / "m55_he"
    Stub(monkeypatch, [flow_result(CONSOLE.replace("{n}", "10"), build_root=nested)])
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    saved = Path(doc["data"]["result"]["flash"]["consolePath"])
    assert code == 0 and saved.parent == nested / "flash-logs" and saved.is_file()
    assert not (p / "build" / "flash-logs").exists()


def test_blank_console_is_not_saved_or_reported(tmp_path, monkeypatch):
    p = project(tmp_path, "a")
    Stub(monkeypatch, [flow_result("  \n")])
    code, doc = invoke("run", "--device", "--confirm", "--project", str(p))
    assert code == 2 and "consolePath" not in doc["data"]["flash"]
    assert [i["code"] for i in doc["issues"]] == ["model.device-console-empty"]
    assert not (p / "build" / "flash-logs").exists()


def test_ab_missing_b_model_refuses_before_a_loads_the_board(tmp_path, monkeypatch):
    a, b = project(tmp_path, "a"), project(tmp_path, "b")
    (a / "a.onnx").write_bytes(b"x")
    stub = Stub(monkeypatch, [flow_result(CONSOLE.replace("{n}", "10"))])
    code, doc = invoke("ab", "a.onnx", "--against", "missing.onnx", "--device", "--confirm",
                       "--project", str(a), "--against-project", str(b))
    assert code == 2 and [i["code"] for i in doc["issues"]] == ["model.model-source-missing"]
    assert stub.calls == []  # A never loaded the board
    assert "flash" not in doc["data"]
