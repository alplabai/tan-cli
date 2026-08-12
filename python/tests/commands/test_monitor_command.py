# SPDX-License-Identifier: Apache-2.0
"""`tan monitor` -- the port-resolution refusals and the miniterm exit-code
mapping. Ported from `scripts/alp_cli/monitor.py`'s own shape (no committed
Rust `monitor.rs` exists; the retired forwarder in
`crates/tan-cli/src/commands/sdk_cli.rs` is the oracle for the outer envelope
contract).

**What is NOT exercised here, and why**: the actual `serial.tools.miniterm`
session against a real board. An `autouse` fixture below monkeypatches
`monitor_cmd.subprocess.run` to a stub that always raises for every test in
this module, so a spawn refused to reach in-process is structural (a
guard-ordering regression that lets a test fall through to the real spawn
fails loudly, rather than incidentally happening to hit an absent COM port)
rather than a run never opening a real port or blocking on interactive I/O --
there is no hardware attached to this test run, and faking a passing hardware
test would be worse than not testing it. Individual tests override the stub
where they need to observe or control the spawn's outcome. The one path this
file cannot prove is that a REAL board's bytes make it to the terminal; that
needs a bench with a device on it.

**pyserial may or may not be genuinely installed, and this file does not need
to know which (tan-cli#255).** `ci.yml` installs `-e ./python` with NO extras
on purpose -- that is the shape a customer's `pip install alp-tan` gives, and
the only one in which `tests/gates/test_declared_dependencies.py` can catch an
extras-only import escaping to a top-level-module import -- while `python-binaries.yml` and
`parity.yml` install `[monitor]`. The six cases below that exercise
`_run_monitor`'s real refusal/spawn logic used to SKIP outright in the
extras-less shape (`@needs_pyserial`), which silently dropped exactly the
coverage they were written for on the one install shape `ci.yml` actually
runs. `_stub_pyserial_if_absent()` replaces that: it plants an empty `serial`
module in `sys.modules` when the real one is not importable, so
`_run_monitor`'s precheck (a bare, function-local `import serial`) succeeds
either way. That is safe, not a fake pass, because every test that calls it
also replaces `_available_ports` with a canned list before `_run_monitor` ever
reaches pyserial's actual API -- a placeholder module with no attributes is
indistinguishable from the real one to the code under test. Only the tests
that assert the pyserial-ABSENT behaviour still force the real `ImportError`
themselves (`_block_pyserial`), since producing that failure honestly is the
whole point of them. Installing the extra in `ci.yml` instead was considered
and rejected: it would blind `test_declared_dependencies.py` to the shape a
bare `pip install alp-tan` actually produces.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import types
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tan.commands import monitor_cmd
from tan.commands.monitor_cmd import monitor

app = typer.Typer(add_completion=False)
app.command("monitor")(monitor)

runner = CliRunner()


def _stub_pyserial_if_absent(monkeypatch) -> None:
    """Make a bare `import serial` succeed even when pyserial genuinely is not
    installed, so the test calling this exercises `_run_monitor`'s real logic
    in every environment `ci.yml` runs in -- not only the `[monitor]` extras
    shape. `_run_monitor`'s precheck imports `serial` in-process whenever the
    spawn would be THIS interpreter, and does nothing more with it (the actual
    port-listing call, `_available_ports`, is always monkeypatched away by the
    caller before this matters), so a bare placeholder module satisfies it."""
    if importlib.util.find_spec("serial") is None:
        monkeypatch.setitem(sys.modules, "serial", types.ModuleType("serial"))


@pytest.fixture(autouse=True)
def _no_real_spawn(monkeypatch):
    """Every test in this module gets a `subprocess.run` that refuses to run
    a real process, unless it overrides this with its own monkeypatch. A test
    relying only on a guard refusing before the spawn is proven, not assumed,
    to never reach a real port."""

    def _refuse(*args, **kwargs):
        raise AssertionError(
            f"a test reached the real subprocess.run spawn: args={args!r}"
        )

    monkeypatch.setattr(monitor_cmd.subprocess, "run", _refuse)


def envelope(result):
    assert result.stdout.count("\n") == 1, result.stdout
    return json.loads(result.stdout)


def test_no_port_given_lists_available_ports_and_refuses(monkeypatch):
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(
        monitor_cmd, "_available_ports", lambda: [("COM7", "USB Serial"), ("COM8", "")]
    )
    result = runner.invoke(app, ["--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["ok"] is False
    assert doc["exitCode"] == 1
    assert doc["issues"][0]["code"] == "monitor.no-port"
    assert "no --port given" in doc["issues"][0]["message"]
    assert doc["data"]["availablePorts"] == [
        {"device": "COM7", "description": "USB Serial"},
        {"device": "COM8", "description": ""},
    ]


def test_no_port_given_and_none_detected_says_so(monkeypatch):
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])
    result = runner.invoke(app, ["--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert "no serial ports detected" in doc["issues"][0]["message"]
    assert doc["data"]["availablePorts"] == []


def test_port_not_in_the_detected_list_refuses(monkeypatch):
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])
    result = runner.invoke(app, ["--port", "COM9", "--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "monitor.no-port"
    assert "'COM9' not found" in doc["issues"][0]["message"]


def test_a_present_port_spawns_miniterm_and_reports_success(monkeypatch):
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])

    captured = {}

    class _Completed:
        returncode = 0

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(monitor_cmd.subprocess, "run", fake_run)

    result = runner.invoke(
        app, ["--port", "COM7", "--baud", "9600", "--format", "json"]
    )
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["ok"] is True
    assert doc["data"] == {"schemaVersion": "1", "port": "COM7", "baud": 9600}
    assert captured["argv"][-2:] == ["COM7", "9600"]
    assert "serial.tools.miniterm" in captured["argv"]
    # The interpreter this test runs under is not frozen, so the spawn must
    # use `sys.executable` -- never a bare re-derivation, and never empty.
    assert captured["argv"][0] == sys.executable


def test_frozen_interpreter_spawns_a_path_python_not_sys_executable(monkeypatch):
    """Under PyInstaller `sys.executable` IS `tan` itself, so the spawn must
    use a PATH name (`_planner_python()`), never `sys.executable` -- else
    `tan monitor` under a frozen build re-enters this CLI instead of
    launching miniterm."""
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])
    monkeypatch.setattr(monitor_cmd.sys, "frozen", True, raising=False)

    captured = {}

    class _Completed:
        returncode = 0

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(monitor_cmd.subprocess, "run", fake_run)

    result = runner.invoke(
        app, ["--port", "COM7", "--baud", "9600", "--format", "json"]
    )
    assert result.exit_code == 0, result.stdout
    assert captured["argv"][0] == monitor_cmd._planner_python(str(Path.cwd()), None)
    assert captured["argv"][0] != sys.executable


def test_a_nonzero_miniterm_exit_maps_to_runtime_failure_not_the_raw_code(monkeypatch):
    """Mirrors the shipped Rust forwarder's `s.code().unwrap_or(1)` ->
    `ExitCode::RuntimeFailure` mapping -- NOT the oracle's literal
    `raise SystemExit(rc)`."""
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])

    captured = {}

    class _Completed:
        returncode = 42

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(monitor_cmd.subprocess, "run", fake_run)

    result = runner.invoke(app, ["--port", "COM7", "--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["exitCode"] == 1
    assert doc["issues"][0]["code"] == "monitor.failed"
    assert "code 42" in doc["issues"][0]["message"]
    assert captured["argv"][0] == sys.executable


def test_default_baud_is_the_sdk_wide_console_default(monkeypatch):
    """`--baud` omitted must fall back to `DEFAULT_BAUD` (115200), matching
    the oracle's `monitor.py::DEFAULT_BAUD` -- a silent drift here garbles
    every console session on the bench."""
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])

    captured = {}

    class _Completed:
        returncode = 0

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Completed()

    monkeypatch.setattr(monitor_cmd.subprocess, "run", fake_run)

    result = runner.invoke(app, ["--port", "COM7", "--format", "json"])
    assert result.exit_code == 0
    doc = envelope(result)
    assert doc["data"]["baud"] == 115200
    assert captured["argv"][-1] == "115200"


def test_pyserial_missing_is_a_coded_refusal(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "serial":
            raise ImportError("no module named serial")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = runner.invoke(app, ["--port", "COM7", "--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "monitor.pyserial-missing"


def test_text_mode_writes_nothing_to_stdout(monkeypatch):
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])
    result = runner.invoke(app, [])
    assert result.exit_code == 1
    assert result.stdout == ""


def test_a_bad_format_value_is_a_usage_error_not_a_traceback():
    result = runner.invoke(app, ["--format", "xml"])
    assert result.exit_code == 2
    assert "Traceback" not in (result.output or "")


@pytest.mark.parametrize(
    "flag",
    [
        ["--project", "."],
        ["--board-yaml", "board.yaml"],
        ["--sdk-root", "."],
        ["--target", "zephyr-conf"],
        ["--all"],
        ["--verbose"],
        ["--quiet"],
        ["--no-color"],
        ["--non-interactive"],
        ["--ci"],
    ],
)
def test_the_globals_the_oracle_ignores_are_accepted_not_rejected(flag, monkeypatch):
    """tan-cli#255: the oracle's clap `GlobalArgs` are declared on EVERY verb,
    `monitor` included, and never read by it (confirmed live against
    `tan.exe monitor`: each flag alone reaches the identical port-resolution
    failure a bare `tan.exe monitor --port COM7` does). Without them declared
    here, `tan monitor --sdk-root <path> --port COM7` was a Click "No such
    option" usage error at exit 2 where the oracle exits 0/1 -- so a caller
    forwarding the global set unconditionally (the extension, a saved script)
    could never open a console."""
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])

    class _Completed:
        returncode = 0

    monkeypatch.setattr(
        monitor_cmd.subprocess, "run", lambda argv, **kwargs: _Completed()
    )

    result = runner.invoke(app, ["--port", "COM7", *flag, "--format", "json"])
    assert result.exit_code == 0, result.output
    assert envelope(result)["command"] == "monitor"


def test_an_unknown_flag_is_still_a_usage_error():
    """The accepted-globals list above must not turn into "accept anything"."""
    assert runner.invoke(app, ["--not-a-real-flag"]).exit_code == 2


def _block_pyserial(monkeypatch):
    """Make `from serial.tools import list_ports` raise ImportError.

    Simulates a build without the `monitor` extra. Blocking at `sys.modules`
    with `None` is what makes the guarded import raise the same ImportError an
    absent distribution would, without uninstalling anything.
    """
    for name in ("serial", "serial.tools", "serial.tools.list_ports"):
        monkeypatch.setitem(sys.modules, name, None)


def test_frozen_build_without_the_extra_reports_pyserial_missing_not_a_tan_bug(monkeypatch):
    """The FROZEN path is the one the precheck deliberately skips.

    `_run_monitor` only validates pyserial when it is about to spawn THIS
    interpreter; under PyInstaller `sys.frozen` is set, that precheck is
    skipped, and `_available_ports()` becomes the first thing to touch
    pyserial -- in-process, before any child exists. Unguarded, that surfaced
    as `monitor.internal-failure` at exit 5, which tells a customer tan is
    broken when the honest answer is that an optional dependency was never
    installed. Regression pin for a real frozen no-extras build.
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _block_pyserial(monkeypatch)

    result = runner.invoke(app, ["--format", "json"])

    envelope = json.loads(result.stdout)
    codes = [issue["code"] for issue in envelope["issues"]]
    assert codes == ["monitor.pyserial-missing"], envelope
    assert "monitor.internal-failure" not in codes
    assert result.exit_code == 1, f"expected RUNTIME_FAILURE, got {result.exit_code}"
    assert 'alp-tan[monitor]' in envelope["issues"][0]["message"]


def test_unfrozen_build_without_pyserial_reports_the_same_code(monkeypatch):
    """The source-install path must not diverge from the frozen one: same code,
    same message, so a consumer matching on `monitor.pyserial-missing` sees one
    spelling regardless of how tan was delivered."""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    _block_pyserial(monkeypatch)

    result = runner.invoke(app, ["--format", "json"])

    envelope = json.loads(result.stdout)
    assert [i["code"] for i in envelope["issues"]] == ["monitor.pyserial-missing"], envelope
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# where miniterm's output goes (tan-cli#491 defect 6)
# ---------------------------------------------------------------------------


def _spawn_kwargs(monkeypatch, argv: list[str]) -> dict:
    """Run `tan monitor <argv>` with a stub spawn and hand back the `kwargs`
    the command passed to `subprocess.run`."""
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])
    captured: dict = {}

    class _Completed:
        returncode = 0

    def fake_run(_argv, **kwargs):
        captured.update(kwargs)
        return _Completed()

    monkeypatch.setattr(monitor_cmd.subprocess, "run", fake_run)
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.stdout
    return captured


def test_json_mode_keeps_the_boards_bytes_off_stdout(monkeypatch):
    """tan-cli#491 defect 6. The spawn passed no `stdout=`, so miniterm
    INHERITED tan's stdout and wrote every received serial byte there --
    reproduced end to end against a real pty: the board's banner preceded the
    envelope and a whole-stdout `JSON.parse` failed on an `ok: true`, exit-0
    run. Under `--format json` the child's stdout must therefore be something
    other than tan's own.

    Asserted as "not inherit, and not stdout" rather than against a specific
    stream object: pytest's own capture replaces the process streams, so
    pinning identity here would test the harness, not the command. What the
    stream IS is `_child_stdout`'s own case below."""
    kwargs = _spawn_kwargs(monkeypatch, ["--port", "COM7", "--format", "json"])
    assert "stdout" in kwargs, kwargs
    assert kwargs["stdout"] is not None, "inherited stdout -- the #491 d6 defect"
    assert kwargs["stdout"] is not sys.stdout


def test_text_mode_still_inherits_stdout(monkeypatch):
    """The other half, and the reason this is not an unconditional redirect:
    in text mode there is no envelope to protect, board traffic on stdout is
    the whole point of an interactive console, and redirecting it would break
    `tan monitor > board.log`."""
    kwargs = _spawn_kwargs(monkeypatch, ["--port", "COM7"])
    assert kwargs.get("stdout") is None, kwargs


def test_child_stdout_is_the_real_stderr_stream_when_it_is_healthy(monkeypatch):
    """The DEVNULL fallback above is only reachable when `sys.__stderr__` is
    absent or closed. On the ordinary path -- a real, open, fd-backed stream
    -- `_child_stdout(True)` must hand back THAT stream, not DEVNULL: a
    mutant that collapses the healthy branch straight to `return
    subprocess.DEVNULL` (i.e. `_child_stdout = lambda json_mode: None if not
    json_mode else subprocess.DEVNULL`) silently drops every board byte
    under `--format json` while still returning *something* falsy-adjacent
    for `stdout=`, and neither test above catches it: the None-stderr and
    closed-stderr cases both already expect DEVNULL, and the d6 stdout test
    only asserts `is not sys.stdout`, which DEVNULL also satisfies. Built and
    confirmed RED against exactly that mutant before this test was kept (see
    the PR receipt)."""
    fd = os.open(os.devnull, os.O_WRONLY)
    stream = os.fdopen(fd, "w")
    try:
        monkeypatch.setattr(monitor_cmd.sys, "__stderr__", stream, raising=False)
        assert monitor_cmd._child_stdout(True) is sys.__stderr__
        assert monitor_cmd._child_stdout(True) is stream
        assert monitor_cmd._child_stdout(True) is not monitor_cmd.subprocess.DEVNULL
    finally:
        stream.close()


def test_child_stdout_falls_back_to_devnull_when_there_is_no_real_stderr(monkeypatch):
    """`_child_stdout` hands the child `sys.__stderr__` -- NOT `sys.stderr`,
    which `cli.main` binds to a `_TeeStderr` under `--format json`, an object
    with no `fileno()` for `subprocess` to hand over. `sys.__stderr__` can
    still be `None` (pythonw, an embedded interpreter) or closed, and the
    answer then is DEVNULL: dropping the board's bytes is bad, putting them on
    stdout is the defect."""
    monkeypatch.setattr(monitor_cmd.sys, "__stderr__", None, raising=False)
    assert monitor_cmd._child_stdout(True) is monitor_cmd.subprocess.DEVNULL

    class _Closed:
        def fileno(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(monitor_cmd.sys, "__stderr__", _Closed(), raising=False)
    assert monitor_cmd._child_stdout(True) is monitor_cmd.subprocess.DEVNULL
    # Text mode never consults the stream at all.
    assert monitor_cmd._child_stdout(False) is None


# ---------------------------------------------------------------------------
# the port gate is OPENABILITY, not enumeration (tan-cli#569)
# ---------------------------------------------------------------------------


def _spawned_argv(monkeypatch, argv: list[str]) -> list[str]:
    """Run `tan monitor <argv>` with a stub spawn and hand back the argv the
    command handed `subprocess.run` -- i.e. proof the port was ACCEPTED and
    miniterm was launched on it, not refused."""
    captured: dict = {}

    class _Completed:
        returncode = 0

    def fake_run(spawn_argv, **kwargs):
        captured["argv"] = spawn_argv
        return _Completed()

    monkeypatch.setattr(monitor_cmd.subprocess, "run", fake_run)
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.stdout
    return captured["argv"]


def _pyserial_with_url_support(monkeypatch) -> None:
    """Make `serial.serial_for_url` available to the URL rule in EVERY install
    shape this suite runs in.

    When pyserial is genuinely installed (the `[monitor]` extras shape that
    `python-binaries.yml` and `parity.yml` use) this does nothing at all and the
    tests below run against the REAL `serial.serial_for_url`, which is the
    authority the fix delegates to. In the extras-less shape `ci.yml` runs, it
    plants a stub that reproduces pyserial 3.5's contract for the two things
    this rule reads: a known scheme returns an unopened `Serial`, and an unknown
    one raises `ValueError("invalid URL, protocol 'x' not known")` (verbatim,
    from `serial/__init__.py::serial_for_url`). The scheme list is pyserial
    3.5's `serial/urlhandler/` plus the built-in `rfc2217`. This is the same
    bargain `_stub_pyserial_if_absent` strikes and for the same reason: without
    it, the coverage written for this rule would silently vanish on the one
    install shape `ci.yml` actually runs.
    """
    if importlib.util.find_spec("serial") is not None:
        return

    module = types.ModuleType("serial")
    known = {"alt", "cp2110", "hwgrep", "loop", "rfc2217", "socket", "spy"}

    class _Serial:
        is_open = False

    def serial_for_url(url, *args, **kwargs):
        scheme = url.split("://", 1)[0] if "://" in url else ""
        if scheme and scheme not in known:
            raise ValueError(f"invalid URL, protocol {scheme!r} not known")
        return _Serial()

    module.serial_for_url = serial_for_url
    monkeypatch.setitem(sys.modules, "serial", module)


@pytest.mark.skipif(
    os.name != "posix",
    reason="the character-device rule is POSIX-only by construction: on Windows "
    "a COM name is not a stat-able node, so this rule can never fire there and "
    "an absent COM9 keeps refusing exactly as before (see monitor_cmd's own "
    "docstring). Nothing to assert on a non-POSIX host.",
)
def test_a_by_id_symlink_to_a_real_chardev_is_accepted_and_spawned(monkeypatch, tmp_path):
    """tan-cli#569. `comports()` reports raw nodes (`/dev/ttyACM0`) and NEVER
    the `/dev/serial/by-id/...` symlinks, so a set-membership gate refuses the
    only STABLE name a bench with two identical adapters has -- a port
    `serial.Serial()`, and therefore the miniterm tan is about to spawn, opens
    fine (miniterm resolves its port through `serial.serial_for_url`, not
    through `comports()`).

    A `pty` slave stands in for the adapter: it is a real character device, so
    `os.stat(...).st_mode` answers exactly what it answers for `/dev/ttyACM0`,
    and no hardware is touched -- deliberate, because accepting a real bench
    port asserts DTR/RTS on the node and resets some boards.
    """
    import pty  # noqa: PLC0415 -- POSIX-only, and this test is skipped elsewhere

    master, slave = pty.openpty()
    try:
        by_id = tmp_path / "usb-Artery_AT32_Virtual_Com_Port_10A2617F4486-if00"
        by_id.symlink_to(os.ttyname(slave))

        _stub_pyserial_if_absent(monkeypatch)
        # The node the symlink points AT is what pyserial enumerates; the
        # symlink itself never appears in `comports()`, which is the whole bug.
        monkeypatch.setattr(
            monitor_cmd, "_available_ports", lambda: [(os.ttyname(slave), "AT32 Virtual Com Port")]
        )

        argv = _spawned_argv(monkeypatch, ["--port", str(by_id), "--format", "json"])
        assert argv[-2:] == [str(by_id), "115200"]
        assert "serial.tools.miniterm" in argv
    finally:
        os.close(master)
        os.close(slave)


def test_a_pyserial_url_is_accepted_and_spawned(monkeypatch):
    """tan-cli#569. miniterm's URL handlers (`socket://`, `rfc2217://`,
    `loop://`) can never appear in `comports()`, so the enumeration gate refused
    every one of them with no workaround at all -- while
    `serial.serial_for_url()` opens them."""
    _pyserial_with_url_support(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("/dev/ttyUSB0", "")])

    argv = _spawned_argv(
        monkeypatch, ["--port", "socket://127.0.0.1:9999", "--baud", "9600", "--format", "json"]
    )
    assert argv[-2:] == ["socket://127.0.0.1:9999", "9600"]


@pytest.mark.parametrize("url", ["rfc2217://localhost:4000", "loop://"])
def test_the_other_url_handlers_are_accepted_too(monkeypatch, url):
    """No hardcoded scheme list: pyserial IS the authority, consulted through
    `serial_for_url(..., do_not_open=True)`, so every handler it ships works."""
    _pyserial_with_url_support(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])
    assert _spawned_argv(monkeypatch, ["--port", url, "--format", "json"])[-2] == url


def test_an_unknown_url_scheme_is_still_refused(monkeypatch):
    """The URL rule delegates the verdict to pyserial rather than pattern-
    matching `://`, so a scheme pyserial does not know stays refused -- same
    `monitor.no-port` code, same message, same port listing as before."""
    _pyserial_with_url_support(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "USB Serial")])

    result = runner.invoke(app, ["--port", "bogus://x", "--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "monitor.no-port"
    assert "'bogus://x' not found" in doc["issues"][0]["message"]
    assert doc["data"]["availablePorts"] == [{"device": "COM7", "description": "USB Serial"}]


@pytest.mark.skipif(
    importlib.util.find_spec("serial") is None,
    reason="asserts real pyserial's spy:// side effect (protocol_spy.py's `file=` "
    "option opens the target INSIDE the port setter); a stub cannot fake this "
    "without silently hiding the exact bug this test exists to catch.",
)
def test_a_pre_existing_file_survives_the_gate_on_every_path(monkeypatch, tmp_path):
    """Review finding 1 (major, data loss): the gate's probe used to truncate
    or create whatever path a `spy://...?file=<path>` names, because
    `serial_for_url` runs the port SETTER (which pyserial's `spy://` handler
    uses to open `file=`) before `do_not_open` is ever consulted -- and it did
    so even on an input the gate went on to REFUSE, because the query is
    parsed left to right and `file=` opened the target before a later unknown
    option raised. Reproduced pre-fix with these exact two calls against real
    pyserial 3.5: the first TRUNCATED a pre-existing file it went on to
    ACCEPT; the second CREATED an absent file it went on to REFUSE.

    `_pyserial_accepts_url` now probes with the query stripped, so the file
    target is never opened by the probe at all -- proven here directly,
    rather than through the exit code, by reading the file back.
    """
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])

    survivor = tmp_path / "notes.log"
    survivor.write_text("PRE-EXISTING CONTENT", encoding="utf-8")
    accepted = monitor_cmd._port_is_openable(f"spy://loop://?file={survivor}")
    assert accepted is True
    assert survivor.read_text(encoding="utf-8") == "PRE-EXISTING CONTENT", (
        "the gate touched a file on the path it ACCEPTED"
    )

    absent = tmp_path / "should-not-be-created.log"
    # Stripping the query also means this no longer differs from the case
    # above at the scheme/netloc pyserial actually validates, so the gate now
    # accepts it too -- a real, intentional narrowing of what the gate can
    # refuse in exchange for never touching a file either way (see
    # `_pyserial_accepts_url`'s docstring). What must hold regardless of that
    # verdict is that the file is never created.
    monitor_cmd._port_is_openable(f"spy://loop://?file={absent}&nosuchoption=1")
    assert not absent.exists(), "the gate created a file it was never asked to open"


def test_a_pre_existing_file_survives_a_genuinely_refused_scheme(monkeypatch, tmp_path):
    """The other half of review finding 1: an input the gate actually REFUSES
    (an unknown scheme, still refused after the fix -- see
    `test_an_unknown_url_scheme_is_still_refused`) must not touch a file
    named anywhere in its query, on any pyserial install shape."""
    _pyserial_with_url_support(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])

    absent = tmp_path / "should-not-be-created.log"
    accepted = monitor_cmd._port_is_openable(f"bogus://x?file={absent}")
    assert accepted is False
    assert not absent.exists()


@pytest.mark.skipif(
    importlib.util.find_spec("serial") is None,
    reason="asserts the exception type pyserial ITSELF raises for a well-formed "
    "URL of a KNOWN scheme that resolves to nothing; a stub proving its own "
    "behaviour would prove nothing.",
)
def test_a_known_scheme_that_pyserial_rejects_is_a_refusal_not_an_internal_failure(monkeypatch):
    """`hwgrep://` with a regexp matching no adapter raises
    `serial.serialutil.SerialException: no ports found matching regexp 'ZZZZ'`
    -- NOT the `ValueError` an unknown scheme raises. Left to escape, that
    reaches `monitor`'s catch-all and surfaces as `monitor.internal-failure` at
    exit 5 ("tan has a bug") for what is simply a port that is not there."""
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])

    result = runner.invoke(app, ["--port", "hwgrep://ZZZZ-no-such-adapter", "--format", "json"])
    assert result.exit_code == 1, result.stdout
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "monitor.no-port"
    assert "monitor.internal-failure" not in result.stdout


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_a_dangling_by_id_symlink_is_still_refused(monkeypatch, tmp_path):
    """The unplugged-adapter case: the `by-id` name survives in a stale
    listing but points at nothing. `os.stat` FOLLOWS the symlink, so it raises
    `FileNotFoundError` and the refusal fires exactly as it did before."""
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("/dev/ttyACM0", "")])

    dangling = tmp_path / "usb-FTDI_FT232R_USB_UART_AA5BY4GV-if00-port0"
    dangling.symlink_to("/dev/nope-not-here")

    result = runner.invoke(app, ["--port", str(dangling), "--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "monitor.no-port"
    assert f"'{dangling}' not found" in doc["issues"][0]["message"]


def test_a_regular_file_is_not_a_port(monkeypatch, tmp_path):
    """The character-device rule must not decay into "any path that exists".
    A regular file stats fine and is not openable as a serial port."""
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("/dev/ttyACM0", "")])

    ordinary = tmp_path / "README.md"
    ordinary.write_text("not a serial port\n", encoding="utf-8")

    result = runner.invoke(app, ["--port", str(ordinary), "--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "monitor.no-port"
    assert "not found" in doc["issues"][0]["message"]


def test_an_absent_bare_com_name_still_refuses(monkeypatch):
    """A genuinely absent `COM9` keeps refusing exactly as it did pre-#569:
    on POSIX it is simply absent (`os.stat` raises); on Windows `_BARE_COM_NAME_RE`
    skips rule 3 for it unconditionally, so absence never mattered there
    either. (Review finding 3: the old docstring claimed rule 3 "can never
    fire" for a COM name and offered only this absent case as proof -- true
    but insufficient, since a *present* COM name unenumerated by `comports()`
    would, per CPython's documented Windows `stat()` behaviour, satisfy
    `S_ISCHR` and let rule 3 fire there. `_BARE_COM_NAME_RE` below makes the
    "never fires" guarantee hold by construction instead of resting on that
    unverified case; see the `test_a_com_shaped_name_skips_rule_3_...`
    test below.)
    """
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [("COM7", "")])

    result = runner.invoke(app, ["--port", "COM9", "--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "monitor.no-port"
    assert "'COM9' not found" in doc["issues"][0]["message"]


def test_a_com_shaped_name_skips_rule_3_even_if_it_would_stat_as_a_character_device(
    monkeypatch, tmp_path
):
    """Review finding 3, proven directly rather than argued: stub `os.stat` so
    that stat-ing `COM3` would answer "character device" (what a real, present
    Windows COM handle reports per CPython's `stat()` docs) and confirm a
    COM-shaped name is STILL refused -- `_BARE_COM_NAME_RE` short-circuits
    before `os.stat` is even consulted for it, proven by asserting the stub is
    never called for that path -- while an equally "character device" non-COM
    path is accepted. This is the scenario this host cannot reproduce with a
    real Windows COM handle, so the stub stands in for the one fact `os.stat`
    behaviour that could not be bench-verified here. The stub delegates every
    OTHER path to the real `os.stat` so it cannot disturb anything outside
    this test (pytest's own machinery calls `os.stat` too, on the same,
    process-global `os` module)."""
    _stub_pyserial_if_absent(monkeypatch)
    monkeypatch.setattr(monitor_cmd, "_available_ports", lambda: [])

    class _CharDeviceStat:
        st_mode = stat.S_IFCHR | 0o666

    also_chardev = str(tmp_path / "not-a-com-name")
    faked_as_chardev = {"COM3", also_chardev}
    real_stat = os.stat
    stat_calls: list[str] = []

    def _fake_stat(path, *args, **kwargs):
        stat_calls.append(str(path))
        if str(path) in faked_as_chardev:
            return _CharDeviceStat()
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(monitor_cmd.os, "stat", _fake_stat)

    result = runner.invoke(app, ["--port", "COM3", "--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert doc["issues"][0]["code"] == "monitor.no-port"
    assert "'COM3' not found" in doc["issues"][0]["message"]
    assert "COM3" not in stat_calls, "rule 3 stat-ed a bare COM name instead of skipping it"

    # Sanity: the stub really does make its target look like a character
    # device, so the COM3 refusal above is the regex, not a stub that never
    # says True. A non-COM path hitting the same stub is accepted.
    assert monitor_cmd._port_is_openable(also_chardev) is True


def test_the_url_rules_import_guard_is_reachable_directly(monkeypatch):
    """`_pyserial_accepts_url`'s own `ImportError` guard, pinned honestly.

    Review finding 2: the guard is NOT reachable through the CLI today --
    `_port_is_openable` always tries rule 1 (`_available_ports()`) first, and
    `_available_ports` raises `monitor.pyserial-missing` itself the moment
    pyserial is not importable, so `_pyserial_accepts_url` is only ever
    called once `import serial` has already succeeded in this process. The
    superseded version of this test manufactured that impossible state by
    monkeypatching `_available_ports` to `lambda: []` while pyserial stayed
    blocked -- a state the real code can never be in. This calls the guarded
    function directly instead, which is a real, reachable way to exercise it.
    """
    _block_pyserial(monkeypatch)
    with pytest.raises(monitor_cmd.MonitorError) as excinfo:
        monitor_cmd._pyserial_accepts_url("socket://127.0.0.1:9999")
    assert excinfo.value.code == "monitor.pyserial-missing"


def test_pyserial_missing_refuses_before_the_url_rule_is_ever_reached(monkeypatch):
    """The production-reachable half of review finding 2: with pyserial
    genuinely unimportable, `_available_ports()` (rule 1) raises before rule 2
    (`_pyserial_accepts_url`) is ever entered -- proven by spying on the real
    `_pyserial_accepts_url` and confirming it is never called, not merely by
    observing the right issue code."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _block_pyserial(monkeypatch)

    calls = []
    real = monitor_cmd._pyserial_accepts_url
    monkeypatch.setattr(
        monitor_cmd, "_pyserial_accepts_url", lambda port: calls.append(port) or real(port)
    )

    result = runner.invoke(app, ["--port", "socket://127.0.0.1:9999", "--format", "json"])
    assert result.exit_code == 1
    doc = envelope(result)
    assert [i["code"] for i in doc["issues"]] == ["monitor.pyserial-missing"], doc
    assert calls == [], "the URL rule was reached despite rule 1 owning the refusal"
