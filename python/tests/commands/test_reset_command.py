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
from tan.commands import reset_confirm as rc_mod
from tan.core import reset_plan as rp
from tan.core.jlink_probe import JLinkProbe

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX executables / filenames")

SERIAL = "000999000001"
PROBE = JLinkProbe("3-4.2", SERIAL)


class FakeJlink:
    def __init__(self, monkeypatch, *, fail=False, banner="TAN_PROBE_ISOLATED_USB_PATH=3-4.2\nScript processing completed.\n"):
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


def codes(issues):
    return [i.code for i in issues]


def test_one_pulse_script_is_r0_sleep_r1_and_nothing_else(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    rc, data, issues, lines = _run(env)
    assert rc == 0 and codes(issues) == ["reset.boot-not-confirmed"]
    assert data["resetObserved"] == "unknown"
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


@pytest.mark.parametrize("noise", ["FAILED to open", "Cannot connect to target", "Could not open"])
def test_failure_text_beats_the_completion_banner(env, monkeypatch, noise):
    FakeJlink(monkeypatch, banner=f"{noise}\nScript processing completed.\n")
    rc, _, issues, _ = _run(env)
    assert rc == 1 and issues[0].code == "reset.failed"


def test_exit_on_error_is_passed(env, monkeypatch):
    seen = {}
    FakeJlink(monkeypatch)
    real = flash_cmd._execute

    def spy(plan, *a, **k):
        seen["argv"] = plan.argv
        return real(plan, *a, **k)

    monkeypatch.setattr(flash_cmd, "_execute", spy)
    _run(env)
    assert seen["argv"][seen["argv"].index("-ExitOnError") + 1] == "1"


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
    assert body["data"]["pulseMs"] == 150 and body["data"]["resetObserved"] == "unknown"
    assert [i["code"] for i in body["issues"]] == ["reset.boot-not-confirmed"]


def _spy_execute(monkeypatch):
    seen = {}
    real = flash_cmd._execute

    def spy(plan, *a, **k):
        seen["argv"] = plan.argv
        return real(plan, *a, **k)

    monkeypatch.setattr(flash_cmd, "_execute", spy)
    return seen


def test_jlink_argv_is_the_proven_one(env, monkeypatch):
    seen = _spy_execute(monkeypatch)
    FakeJlink(monkeypatch)
    assert _run(env)[0] == 0
    assert seen["argv"] == ("JLinkExe", "-NoGui", "1", "-ExitOnError", "1", "-CommanderScript")
    assert "connect" in rp.FORBIDDEN_VERBS


def test_with_a_place_there_is_one_spawn_and_no_serial_line(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    monkeypatch.setenv("JLINK_RUN_PLACE", "aen-evk-02")
    rc, data, issues, _ = _run(env)
    assert rc == 0 and data["singleSpawn"] is True
    assert len(jl.scripts) == 1 and "ShowEmuList" not in jl.scripts[0]
    assert "SelectEmuBySN" not in jl.scripts[0]
    assert jl.envs[0] == {"TAN_PROBE_USB_PATH": "3-4.2"}
    assert data["probe"]["isolation"] == "wrapper-attested:3-4.2"


def test_without_the_wrapper_handshake_the_fast_path_fails(env, monkeypatch):
    FakeJlink(monkeypatch, banner="Script processing completed.\n")
    monkeypatch.setenv("JLINK_RUN_PLACE", "aen-evk-02")
    rc, _, issues, _ = _run(env)
    assert rc == 1 and issues[0].code == "flash.probe-verify-failed"


def test_a_handshake_for_another_path_fails(env, monkeypatch):
    FakeJlink(monkeypatch, banner="TAN_PROBE_ISOLATED_USB_PATH=3-9\nScript processing completed.\n")
    monkeypatch.setenv("JLINK_RUN_PLACE", "aen-evk-02")
    rc, _, issues, _ = _run(env)
    assert rc == 1 and issues[0].code == "flash.probe-verify-failed"


class _Console:
    def __init__(self, chunks):
        self.chunks, self.closed, self.flushed = list(chunks), False, False

    def reset_input_buffer(self):
        self.flushed = True

    def read(self, n=1):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


def _confirm(monkeypatch, chunks):
    ser = _Console(chunks)
    monkeypatch.setattr(rc_mod, "open_console", lambda spec: ser)
    return ser, rc_mod.confirm_spec("rfc2217://gw:4001", "Zephyr", None, 0.5)


def test_a_matching_console_line_confirms_the_reset(env, monkeypatch):
    FakeJlink(monkeypatch)
    ser, spec = _confirm(monkeypatch, [b"*** Booting Zephyr OS ***\n"])
    rc, data, issues, _ = _run(env, confirm=spec)
    assert rc == 0 and data["resetObserved"] is True and issues == []
    assert data["console"]["matchedLine"].endswith("Zephyr OS ***")
    assert ser.flushed and ser.closed


def test_no_console_line_is_not_a_reset(env, monkeypatch):
    FakeJlink(monkeypatch)
    ser, spec = _confirm(monkeypatch, [b"still in STOP\n"])
    rc, data, issues, _ = _run(env, confirm=spec)
    assert rc == 1 and data["resetObserved"] is False
    assert issues[0].code == "reset.boot-not-observed" and ser.closed


def test_confirm_options_must_come_together():
    with pytest.raises(rc_mod.ConfirmError):
        rc_mod.confirm_spec("p", None, None, None)
    with pytest.raises(rc_mod.ConfirmError):
        rc_mod.confirm_spec("p", "(", None, None)


def test_cli_rejects_a_malformed_usb_path(env):
    r = CliRunner().invoke(app, ["reset", "--probe-usb-path", "nope", "--project", str(env)])
    assert r.exit_code == 2


class _LateConsole(_Console):
    """Says nothing for `delay` seconds, then the banner."""

    def __init__(self, delay):
        super().__init__([])
        import time as _t
        self._t, self._due = _t, _t.monotonic() + delay

    def read(self, n=1):
        if self._t.monotonic() >= self._due and not self.chunks:
            self.chunks = [b"RTC alarm wake: Zephyr\n"]
        self._t.sleep(0.02)
        return self.chunks.pop(0) if self.chunks else b""


def test_a_match_after_the_window_is_not_a_reset(env, monkeypatch):
    FakeJlink(monkeypatch)
    ser = _LateConsole(0.4)
    monkeypatch.setattr(rc_mod, "open_console", lambda spec: ser)
    spec = rc_mod.confirm_spec("rfc2217://gw:4001", "Zephyr", None, 2.0, 0.2)
    rc, data, issues, _ = _run(env, confirm=spec)
    assert rc == 1 and data["resetObserved"] is False
    assert issues[0].code == "reset.boot-not-observed"
    assert data["console"]["lateMatchAtSeconds"] >= 0.4
    assert data["console"]["matchLatencySeconds"] is None and data["console"]["matchedLine"] is None


def test_a_match_inside_the_window_records_its_latency(env, monkeypatch):
    FakeJlink(monkeypatch)
    _, spec = _confirm(monkeypatch, [b"Zephyr\n"])
    rc, data, _, _ = _run(env, confirm=spec)
    assert rc == 0 and data["console"]["matchLatencySeconds"] <= spec.window_s
    assert "lateMatchAtSeconds" not in data["console"]


def test_timing_is_recorded(env, monkeypatch):
    FakeJlink(monkeypatch)
    _, data, _, _ = _run(env)
    assert data["timing"]["jlinkSpawnSeconds"] >= 0 and "prepSeconds" in data["timing"]


def test_a_malformed_confirm_console_url_is_a_bad_port_envelope(env):
    r = CliRunner().invoke(
        app, ["reset", "--probe-usb-path", "3-4.2", "--project", str(env), "--format", "json",
              "--confirm-console", "rfc2217://gw:", "--expect", "x"])
    assert r.exit_code == 2
    assert json.loads(r.stdout)["issues"][0]["code"] == "reset.bad-port"


@pytest.mark.parametrize("url", ["socket://host", "rfc2217://:1", "ftp://x:1", "rfc2217://h:99999"])
def test_confirm_spec_refuses_bad_urls(url):
    with pytest.raises(rc_mod.BadPort):
        rc_mod.confirm_spec(url, "x", None, None)
