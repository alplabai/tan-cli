# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1452: `tan reset` -- one bare nRESET pulse. No hardware: `_spawn_jlink`
is a stub J-Link that records the scripts it is handed."""
from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

from tan.cli import app
from tan.commands import flash_cmd, reset_cmd
from tan.core import reset_plan as rp
from tan.core.jlink_probe import JLinkProbe

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX executables / filenames")

SERIAL = "000999000001"
PROBE = JLinkProbe("3-4.2", SERIAL)


class FakeJlink:
    def __init__(self, monkeypatch, *, fail=False, banner="Script processing completed.\n"):
        self.scripts: list[str] = []
        self.envs: list[dict | None] = []
        self.fail, self.banner = fail, banner
        monkeypatch.setattr(flash_cmd, "_spawn_jlink", self._spawn)

    def _spawn(self, argv, script, capture, timeout, venv_bin=None, workspace=None,
               executable=None, extra_env=None, **kw):
        self.scripts.append(script)
        self.envs.append(extra_env)
        if "ShowEmuList" in script:
            out = f"J-Link[0]: Connection: USB, Serial number: {SERIAL}, ProductName: J-Link\n"
            return flash_cmd._Outcome(success=True, stdout=out, returncode=0)
        if self.fail:
            return flash_cmd._Outcome(success=False, stderr="Cannot connect to J-Link", returncode=1)
        return flash_cmd._Outcome(success=True, stdout=self.banner, returncode=0)

    def reset_scripts(self):
        return [s for s in self.scripts if "ShowEmuList" not in s]


@pytest.fixture
def env(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    stub = tools / "JLinkExe"
    stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    os.chmod(stub, 0o755)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.delenv("TAN_JLINK", raising=False)
    monkeypatch.delenv(reset_cmd.PLACE_ENV, raising=False)
    monkeypatch.setattr(flash_cmd, "enumerate_jlinks", lambda: [PROBE])
    return tmp_path


def _run(tmp_path, pulse=100, usb="3-4.2", **kw):
    return reset_cmd._run(pulse, None, usb, None, str(tmp_path),
                          enumerate_probes=lambda: [PROBE], **kw)


def test_one_pulse_script_is_r0_sleep_r1_and_nothing_else(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    rc, data, issues, lines = _run(env)
    assert rc == 0 and issues == []
    (script,) = jl.reset_scripts()
    body = [ln for ln in script.splitlines() if ln]
    assert body[-4:] == ["r0", "sleep 100", "r1", "q"] and "connect" not in body
    assert script.count("r0") == 1 and script.count("r1") == 1
    assert not set(rp.script_verbs(script)) & set(rp.FORBIDDEN_VERBS)
    assert data["pulseMs"] == 100 and data["writes"] is False
    assert data["probe"]["serial"] == SERIAL and "place" in data


def test_pulse_width_is_configurable(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    rc, data, _, _ = _run(env, pulse=250)
    assert rc == 0 and "sleep 250\n" in jl.reset_scripts()[0] and data["pulseMs"] == 250


@pytest.mark.parametrize("bad", [0, -5, 10001])
def test_out_of_range_pulse_is_refused_before_any_spawn(env, monkeypatch, bad):
    jl = FakeJlink(monkeypatch)
    rc, _, issues, _ = _run(env, pulse=bad)
    assert rc == 2 and issues[0].code == "reset.bad-argument" and jl.scripts == []


def test_jlink_run_place_is_reported_and_left_in_the_spawn_environment(env, monkeypatch):
    FakeJlink(monkeypatch)
    monkeypatch.setenv("JLINK_RUN_PLACE", "aen-evk-02")
    rc, data, _, lines = _run(env)
    assert rc == 0 and data["place"] == "aen-evk-02" and "aen-evk-02" in lines[0]
    assert flash_cmd.spawn_env()["JLINK_RUN_PLACE"] == "aen-evk-02"


def test_usb_path_guard_runs_and_exports_the_path(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    rc, _, _, _ = _run(env)
    assert rc == 0 and any("ShowEmuList" in s for s in jl.scripts)
    assert {"TAN_PROBE_USB_PATH": "3-4.2"} in jl.envs


def test_an_unknown_usb_path_is_a_probe_refusal(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    rc, _, issues, _ = _run(env, usb="9-9")
    assert rc == 1 and issues[0].code.startswith("flash.probe-") and jl.reset_scripts() == []


def test_a_failing_jlink_is_reset_failed(env, monkeypatch):
    FakeJlink(monkeypatch, fail=True)
    rc, _, issues, _ = _run(env)
    assert rc == 1 and issues[0].code == "reset.failed"


def test_missing_completion_banner_is_not_a_success(env, monkeypatch):
    FakeJlink(monkeypatch, banner="")
    rc, _, issues, _ = _run(env)
    assert rc == 1 and issues[0].code == "reset.failed"


def test_cli_envelope_shape(env, monkeypatch):
    FakeJlink(monkeypatch)
    r = CliRunner().invoke(
        app, ["reset", "--probe-usb-path", "3-4.2", "--pulse-ms", "150", "--project", str(env),
              "--format", "json"])
    assert r.exit_code == 0, r.output
    body = json.loads(r.stdout)
    assert body["command"] == "reset" and body["ok"] is True
    assert body["data"]["pulseMs"] == 150 and body["issues"] == []


def test_jlink_is_not_asked_to_connect(env, monkeypatch):
    seen = {}
    FakeJlink(monkeypatch)
    real = flash_cmd._execute

    def spy(plan, *a, **k):
        seen["argv"] = plan.argv
        return real(plan, *a, **k)

    monkeypatch.setattr(flash_cmd, "_execute", spy)
    assert _run(env)[0] == 0
    assert "-device" not in seen["argv"] and "-speed" not in seen["argv"]
    assert seen["argv"][seen["argv"].index("-autoconnect") + 1] == "0"
    assert "connect" in rp.FORBIDDEN_VERBS


def test_cli_rejects_a_malformed_usb_path(env):
    r = CliRunner().invoke(app, ["reset", "--probe-usb-path", "nope", "--project", str(env)])
    assert r.exit_code == 2
