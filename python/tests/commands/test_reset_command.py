# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1452: `tan reset` -- one bare nRESET pulse. No hardware: `_spawn_jlink`
is a stub J-Link that records the scripts it is handed."""
from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

from tan.cli import app
from tan.commands import flash_cmd, flash_raw, reset_cmd
from tan.commands import reset_confirm as rc_mod
from tan.core import reset_plan as rp
from tan.core.jlink_probe import JLinkProbe

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX executables / filenames")

_REAL_RESERVATION_REFUSAL = flash_raw._reservation_refusal
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
    # The session-lease gate has its own tests below; the rest exercise the pulse itself.
    monkeypatch.setattr(flash_raw, "_reservation_refusal", lambda place, exe, usb: (None, exe))
    return tmp_path


def _run(tmp_path, pulse=100, usb="3-4.2", **kw):
    return reset_cmd._run(pulse, None, usb, None, str(tmp_path),
                          enumerate_probes=lambda: [PROBE], **kw)


def as_wrapper(env, monkeypatch):
    """Make the PATH-resolved stub JLinkExe the configured, trusted wrapper."""
    monkeypatch.setenv("TAN_JLINK_WRAPPER", str(env / "tools" / "JLinkExe"))


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
    as_wrapper(env, monkeypatch)
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
    as_wrapper(env, monkeypatch)
    jl = FakeJlink(monkeypatch)
    monkeypatch.setenv("JLINK_RUN_PLACE", "aen-evk-02")
    rc, data, issues, _ = _run(env)
    assert rc == 0 and data["singleSpawn"] is True
    assert len(jl.scripts) == 1 and "ShowEmuList" not in jl.scripts[0]
    assert "SelectEmuBySN" not in jl.scripts[0]
    assert jl.envs[0] == {"TAN_PROBE_USB_PATH": "3-4.2"}
    assert data["probe"]["isolation"] == "wrapper-attested:3-4.2"


def test_without_the_wrapper_handshake_the_fast_path_fails(env, monkeypatch):
    as_wrapper(env, monkeypatch)
    FakeJlink(monkeypatch, banner="Script processing completed.\n")
    monkeypatch.setenv("JLINK_RUN_PLACE", "aen-evk-02")
    rc, _, issues, _ = _run(env)
    assert rc == 1 and issues[0].code == "flash.probe-verify-failed"


def test_a_handshake_for_another_path_fails(env, monkeypatch):
    as_wrapper(env, monkeypatch)
    FakeJlink(monkeypatch, banner="TAN_PROBE_ISOLATED_USB_PATH=3-9\nScript processing completed.\n")
    monkeypatch.setenv("JLINK_RUN_PLACE", "aen-evk-02")
    rc, _, issues, _ = _run(env)
    assert rc == 1 and issues[0].code == "flash.probe-verify-failed"


class _NoDrain:
    def __init__(self, ser):
        pass

    def stop(self, exit_ts=None, timeout=2.0):
        return []


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
    monkeypatch.setattr(rc_mod, "Drain", _NoDrain)  # the canned chunks are post-pulse data
    return ser, rc_mod.confirm_spec("rfc2217://gw:4001", "Zephyr", None, 0.5)


def test_a_matching_console_line_confirms_the_reset(env, monkeypatch):
    FakeJlink(monkeypatch)
    ser, spec = _confirm(monkeypatch, [b"*** Booting Zephyr OS ***\n"])
    rc, data, issues, _ = _run(env, confirm=spec)
    assert rc == 0 and data["resetObserved"] is True and issues == []
    assert data["console"]["matchedLine"].endswith("Zephyr OS ***")
    assert ser.closed


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


class _FakeClock:
    t = 100.0

    def __call__(self):
        return self.t


class _LateConsole(_Console):
    """Each read takes `delay` fake seconds; the banner comes with the first one."""

    def __init__(self, clock, delay):
        super().__init__([])
        self.clock, self.delay = clock, delay

    def read(self, n=1):
        self.clock.t += self.delay
        if self.delay is not None and not getattr(self, "_said", False):
            self._said = True
            return b"RTC alarm wake: Zephyr\n"
        return b""


def test_a_match_after_the_window_is_not_a_reset(env, monkeypatch):
    FakeJlink(monkeypatch)
    clock = _FakeClock()
    ser = _LateConsole(clock, 0.4)
    monkeypatch.setattr(rc_mod, "open_console", lambda spec: ser)
    monkeypatch.setattr(rc_mod, "Drain", _NoDrain)
    real = rc_mod.observe
    monkeypatch.setattr(
        rc_mod, "observe",
        lambda ser, spec, initial=b"", exit_ts=None: real(ser, spec, initial, clock(), clock=clock),
    )
    spec = rc_mod.confirm_spec("rfc2217://gw:4001", "Zephyr", None, 2.0, 0.2)
    rc, data, issues, _ = _run(env, confirm=spec)
    assert rc == 1 and data["resetObserved"] is False
    assert issues[0].code == "reset.boot-not-observed"
    assert data["console"]["lateMatchAtSeconds"] == 0.4
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


def _no_exec(monkeypatch):
    calls = []
    monkeypatch.setattr(flash_cmd, "_spawn_jlink", lambda *a, **k: calls.append(a) or pytest.fail("spawned"))
    return calls


def test_a_place_without_a_wrapper_is_refused_before_any_spawn(env, monkeypatch):
    _no_exec(monkeypatch)
    monkeypatch.setenv("JLINK_RUN_PLACE", "aen-evk-02")  # PATH has a raw JLinkExe, no TAN_JLINK_WRAPPER
    rc, data, issues, _ = _run(env)
    assert rc == 1 and issues[0].code == "reset.wrapper-required"
    assert data["wrapper"] == {"trusted": False, "reason": "wrapper-env-unset"}


def test_a_trusted_wrapper_beats_path_when_no_jlink_flag_is_given(env, monkeypatch, tmp_path):
    jl = FakeJlink(monkeypatch)
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "JLinkExe"
    shim.write_text("#!/bin/sh\nexit 1\n")
    os.chmod(shim, 0o755)
    monkeypatch.setenv("TAN_JLINK_WRAPPER", str(shim))
    monkeypatch.setenv("JLINK_RUN_PLACE", "aen-evk-02")
    rc, data, _, _ = _run(env)  # PATH still holds the raw stub; the wrapper must win
    assert rc == 0 and data["jlink"]["binary"] == str(shim) and data["singleSpawn"] is True
    assert data["wrapper"]["trusted"] is True and jl.reset_scripts()
    # tan-cli#1467: the program came from TAN_JLINK_WRAPPER, not from a --jlink flag.
    assert data["jlink"]["binarySource"] == "TAN_JLINK_WRAPPER"


def test_an_explicit_jlink_that_is_not_the_wrapper_is_refused_with_a_place(env, monkeypatch, tmp_path):
    _no_exec(monkeypatch)
    other = tmp_path / "other"
    other.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(other, 0o755)
    as_wrapper(env, monkeypatch)
    monkeypatch.setenv("JLINK_RUN_PLACE", "aen-evk-02")
    rc, data, issues, _ = reset_cmd._run(
        100, None, "3-4.2", str(other), str(env), enumerate_probes=lambda: [PROBE])
    assert rc == 1 and issues[0].code == "reset.wrapper-required"
    assert data["wrapper"]["reason"] == "wrapper-not-resolved-binary"


@pytest.mark.parametrize("mode", ["unsafe", "relative"])
def test_an_unsafe_wrapper_with_a_place_is_refused(env, monkeypatch, mode):
    _no_exec(monkeypatch)
    if mode == "unsafe":
        os.chmod(env / "tools" / "JLinkExe", 0o777)
        as_wrapper(env, monkeypatch)
    else:
        monkeypatch.setenv("TAN_JLINK_WRAPPER", "JLinkExe")
    monkeypatch.setenv("JLINK_RUN_PLACE", "aen-evk-02")
    rc, data, issues, _ = _run(env)
    assert rc == 1 and issues[0].code == "reset.wrapper-required"
    assert data["wrapper"]["reason"] == "wrapper-path-unsafe"


def test_without_a_place_a_real_jlink_takes_the_guarded_two_pass_path(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    rc, data, _, _ = _run(env)
    assert rc == 0 and data["singleSpawn"] is False and data["wrapper"]["trusted"] is False
    assert any("ShowEmuList" in x for x in jl.scripts)
    assert f"SelectEmuBySN {SERIAL}" in jl.reset_scripts()[0]


class _DeadConsole(_Console):
    def read(self, n=1):
        raise OSError("link dropped")


def test_a_console_read_error_is_not_reported_as_no_reboot(env, monkeypatch):
    FakeJlink(monkeypatch)
    ser = _DeadConsole([])
    monkeypatch.setattr(rc_mod, "open_console", lambda spec: ser)
    monkeypatch.setattr(rc_mod, "Drain", _NoDrain)
    spec = rc_mod.confirm_spec("rfc2217://gw:4001", "Zephyr", None, 0.5)
    rc, data, issues, _ = _run(env, confirm=spec)
    assert rc == 1 and issues[0].code == "reset.console-read-failed"
    assert data["resetObserved"] == "unknown" and "link dropped" in issues[0].message


def test_the_console_is_closed_when_the_spawn_raises(env, monkeypatch):
    FakeJlink(monkeypatch)
    ser, spec = _confirm(monkeypatch, [])

    def boom(*a, **k):
        raise RuntimeError("spawn blew up")

    monkeypatch.setattr(reset_cmd, "_spawn", boom)
    with pytest.raises(RuntimeError):
        _run(env, confirm=spec)
    assert ser.closed


class _GatedPort:
    """First read returns `first` at once; every later read blocks on `release` and then returns
    `then`. `entered` is set when a blocking read has started, so a test never has to guess."""

    def __init__(self, first=b"", then=b"banner\n"):
        import threading

        self.first, self.then = first, then
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = 0

    def read(self, n=1):
        self.calls += 1
        if self.calls == 1:
            return self.first
        self.entered.set()
        if self.release.wait(10.0):
            self.release.clear()
            data, self.then = self.then, b""
            return data
        return b""


def _drain_over(port, clock):
    return rc_mod.Drain(port, clock=clock)


def test_drain_keeps_a_banner_that_lands_while_it_is_mid_read():
    import threading

    clock = _FakeClock()
    port = _GatedPort(first=b"stale line\n")
    drain = _drain_over(port, clock)
    assert port.entered.wait(10.0)  # the reader is now parked inside read(), stale line stamped 100.0
    clock.t = 105.0
    exit_ts = clock.t  # J-Link exits
    result = []
    stopper = threading.Thread(target=lambda: result.append(drain.stop(exit_ts)))
    stopper.start()
    assert drain._halt.wait(10.0)  # stop() has been called while the read is still blocked
    clock.t = 105.05  # the banner lands 50 ms after exit
    port.release.set()
    stopper.join(10.0)
    assert result == [[b"banner\n"]]  # kept; the pre-exit stale line is not handed on


def test_drain_that_cannot_stop_is_reported_not_raced():
    port = _GatedPort()
    drain = _drain_over(port, _FakeClock())
    assert port.entered.wait(10.0)
    try:
        with pytest.raises(rc_mod.DrainStuck):
            drain.stop(0.0, timeout=0.01)
    finally:
        port.release.set()
        drain.stop()


def test_a_stuck_drain_means_the_console_is_not_read(env, monkeypatch):
    FakeJlink(monkeypatch)
    ser, spec = _confirm(monkeypatch, [b"Zephyr\n"])

    class Stuck(_NoDrain):
        def stop(self, exit_ts=None, timeout=2.0):
            raise rc_mod.DrainStuck("stuck")

    monkeypatch.setattr(rc_mod, "Drain", Stuck)
    rc, data, issues, _ = _run(env, confirm=spec)
    assert rc == 1 and issues[0].code == "reset.console-read-failed"
    assert ser.chunks == [b"Zephyr\n"]  # nothing was read beside the stuck reader


def test_an_explicit_jlink_flag_keeps_its_own_binary_source_label(env, monkeypatch, tmp_path):
    """tan-cli#1467: only a TAN_JLINK_WRAPPER default is relabelled; an explicit --jlink
    naming the same wrapper still reports "the --jlink flag"."""
    FakeJlink(monkeypatch)
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "JLinkExe"
    shim.write_text("#!/bin/sh\nexit 1\n")
    os.chmod(shim, 0o755)
    monkeypatch.setenv("TAN_JLINK_WRAPPER", str(shim))
    monkeypatch.setenv("JLINK_RUN_PLACE", "aen-evk-02")
    rc, data, _, _ = reset_cmd._run(100, None, "3-4.2", str(shim), str(env),
                                    enumerate_probes=lambda: [PROBE])
    assert rc == 0 and data["jlink"]["binarySource"] == "the --jlink flag"


# -- tan-cli#1485: a named place needs this session's lease, as `tan flash --raw` does --------

_NONCE = "ab" * 24
_CHANGED = "2026-10-09 17:12:47.796610"


def _holder():
    import socket
    return f"{socket.gethostname()}/{flash_raw._current_user()}"


def _show(path="3-4.2", holder=None, changed=_CHANGED):
    block = f"Acquired resource 'swd' (e/p/NetworkUSBDebugger/swd):\n  {{'path': '{path}'}}\n"
    return (f"Place 'p':\n  matches:\n    e/NetworkUSBDebugger/swd\n  acquired: {holder or _holder()}\n"
            f"  changed: {changed}\n{block}")


def _real_gate(env, monkeypatch, *, lease=True, nonce=_NONCE, show=None):
    """The REAL flash_raw gate, with a lease dir and a canned `labgrid-client show`."""
    as_wrapper(env, monkeypatch)
    monkeypatch.setattr(flash_raw, "_reservation_refusal", _REAL_RESERVATION_REFUSAL)
    monkeypatch.setenv("JLINK_RUN_PLACE", "aen-evk-02")
    leases = env / "leases"
    leases.mkdir(mode=0o700)
    os.chmod(leases, 0o700)
    if lease:
        f = leases / "aen-evk-02.lease"
        f.write_text(f"place=aen-evk-02\nnonce={_NONCE}\nchanged={_CHANGED}\n", encoding="utf-8")
        os.chmod(f, 0o600)
    monkeypatch.setattr(flash_raw, "LEASE_DIR", str(leases))
    if nonce is None:
        monkeypatch.delenv(flash_raw.NONCE_ENV, raising=False)
    else:
        monkeypatch.setenv(flash_raw.NONCE_ENV, nonce)
    monkeypatch.setattr(flash_raw, "_labgrid_show", lambda place: (show or _show(), ""))


@pytest.mark.parametrize(
    "kw, text",
    [
        ({"nonce": None}, "TAN_LEASE_NONCE"),
        ({"nonce": "cd" * 24}, "does not match the lease"),
        ({"lease": False}, "no readable regular lease file"),
        ({"show": _show(holder="other-host/someone")}, "do not hold the labgrid place"),
        ({"show": _show(path="9-9")}, "not the leased place's swd port"),
        ({"show": _show(changed="2026-10-10 01:00:00.000001")}, "earlier acquisition"),
    ],
)
def test_a_place_without_this_sessions_lease_is_refused_before_any_spawn(env, monkeypatch, kw, text):
    calls = _no_exec(monkeypatch)
    _real_gate(env, monkeypatch, **kw)
    rc, data, issues, _ = _run(env)
    assert rc == 1 and codes(issues) == ["reset.reservation-required"]
    assert text in issues[0].message and calls == []


def test_a_place_without_a_probe_usb_path_cannot_match_the_lease(env, monkeypatch):
    calls = _no_exec(monkeypatch)
    _real_gate(env, monkeypatch)
    rc, _, issues, _ = _run(env, usb=None)
    assert rc == 1 and codes(issues) == ["reset.reservation-required"] and calls == []


def test_a_place_with_this_sessions_lease_pulses(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    _real_gate(env, monkeypatch)
    rc, data, issues, _ = _run(env)
    assert rc == 0, issues
    assert data["singleSpawn"] is True and jl.reset_scripts()


def test_no_place_needs_no_lease(env, monkeypatch):
    jl = FakeJlink(monkeypatch)
    monkeypatch.setattr(flash_raw, "_reservation_refusal",
                        lambda *a: pytest.fail("the lease gate must only run for a named place"))
    rc, _, _, _ = _run(env)
    assert rc == 0 and jl.reset_scripts()


def _help(*args):
    out = CliRunner().invoke(app, [*args, "--help"], env={"COLUMNS": "400", "TERM": "dumb"})
    assert out.exit_code == 0
    return " ".join(out.output.split())


def test_reset_help_states_the_place_mode_rules(env):
    """tan-cli#1485: the help named neither the wrapper env nor the post-pulse handshake, and its
    example banner was one the docs say may be missed."""
    text = _help("reset")
    assert "TAN_JLINK_WRAPPER" in text and "TAN_LEASE_NONCE" in text
    assert "AFTER the pulse" in text and "reset.reservation-required" in text
    assert "--expect 'Zephyr'" not in text


def test_flash_help_names_every_raw_reservation_requirement_and_the_setools_trust_rule(env):
    text = _help("flash")
    for needle in ("TAN_LEASE_NONCE", "TAN_JLINK_WRAPPER", "swd port", "flash.setools-untrusted-source"):
        assert needle in text, needle
