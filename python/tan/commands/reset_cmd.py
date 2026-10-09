# SPDX-License-Identifier: Apache-2.0
"""`tan reset` -- ONE bare nRESET pulse through J-Link (tan-cli#1452).

Bench recovery from a non-waking STOP is a single clean pulse: `r0`, a pulse
width (default 100 ms), `r1`. This is not the pin reset `tan flash` runs
(`RSetType 2; r; g`, with retries): no connect, no halt, no connect-under-reset, no retry
storm, so the target is not woken out of STOP and the VBAT/BKRAM evidence
survives. See `tan.core.reset_plan` for the script rules.

Probe handling is `tan probe`'s: the trusted J-Link binary (`resolve_jlink`),
`--probe-serial` / `--probe-usb-path` and the `ShowEmuList` verification before
the spawn, through `flash_cmd._execute`. `JLINK_RUN_PLACE` is read from the
environment of THIS invocation, so the board-farm shim that wraps `JLinkExe`
picks the place per command, exactly as for `tan flash`; the envelope reports
the value in `data.place`.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Callable

import typer

from tan.commands import flash_cmd as fc
from tan.commands import reset_confirm as rc_mod
from tan.commands.monitor_cmd import MonitorError
from tan.core import reset_plan as rp
from tan.core.flash_plan import FlashPlan, FlashPlanError
from tan.core.global_flags import accept_global_flags
from tan.core.jlink_probe import HANDSHAKE_PREFIX, USB_PATH_ENV, parse_handshakes
from tan.core.jlink_binary import ENV_OVERRIDE as JLINK_ENV, resolve_jlink
from tan.envelope import Envelope, Issue
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat, resolve_format

COMMAND = "reset"
PLACE_ENV = "JLINK_RUN_PLACE"
_SCHEMA_VERSION = fc._DATA_SCHEMA_VERSION
_SCRIPT_PREFIX = "tan-reset-"
#: Commander output that means the pulse did not happen, even when the closing
#: "Script processing completed" banner is also printed.
_FAILURE = re.compile(r"FAILED|Cannot connect|Could not open")
_Result = tuple[ExitCode, dict[str, Any], list[Issue], list[str]]


def _fail(data: dict[str, Any], issue: Issue, rc=ExitCode.RUNTIME_FAILURE) -> _Result:
    return rc, data, [issue], [f"reset: {issue.message}"]


def _select(data, probe_serial, probe_usb_path, jlink_path, project_dir, enumerate_probes):
    """`(guard, echo, serial)` or a refusal `_Result`."""
    ctx = fc._Context(
        sku="", build_root="", sdk_root="", dry_run=False, skip_missing_tools=False,
        force_confirm=False, capture=True, probe_serial=probe_serial,
        probe_usb_path=probe_usb_path, project_dir=project_dir, jlink_path=jlink_path,
        **({"enumerate_probes": enumerate_probes} if enumerate_probes is not None else {}),
    )
    flash_args, selection, snapshot = fc._flow_d_probe_selection({}, ctx)
    echo = fc._probe_echo(selection, ctx)
    if echo:
        data["probe"] = echo
    if selection is not None and selection.refusal_code is not None:
        return _fail(data, Issue(f"flash.probe-{selection.refusal_code}", "error", selection.refusal or ""))
    guard = None
    if selection is not None and (selection.serial is not None or selection.visible is None):
        guard = fc._ProbeGuard(selection, snapshot, ctx.enumerate_probes, echo,
                               script_prefix=_SCRIPT_PREFIX)
    return guard, flash_args.get("jlink_serial")


def _spawn(guard, script, exe, usb_path, fast):
    """One J-Link Commander spawn. `fast` (a masking wrapper is in use, named by
    JLINK_RUN_PLACE, and --probe-usb-path was given): no ShowEmuList pass before
    the pulse -- the wrapper itself refuses a TAN_PROBE_USB_PATH that is not its
    place's port (exit 96) before opening any probe, and the pulse is judged
    afterwards by the TAN_PROBE_ISOLATED_USB_PATH handshake. Otherwise the
    pre-spawn probe guard of `tan flash` runs. The argv is the proven one
    (`-NoGui 1 -CommanderScript`, plus -ExitOnError 1): no -if, no -autoconnect,
    no -device; Commander opens the probe lazily on `r0`."""
    argv = ("JLinkExe", "-NoGui", "1", "-ExitOnError", "1", "-CommanderScript")
    if not fast:
        return fc._execute(
            FlashPlan(argv=argv, ok_message="", jlink_script=script),
            True, None, None, guard, jlink_exe=exe, script_prefix=_SCRIPT_PREFIX,
        )
    return fc._spawn_jlink(
        list(argv), script, True, fc._FLASH_TIMEOUT_S, None, None, exe,
        extra_env={USB_PATH_ENV: usb_path}, script_prefix=_SCRIPT_PREFIX,
    )


def _verdict(data, out, guard, fast, usb_path) -> Issue | None:
    """The refusal / failure for a finished spawn, else `None`."""
    if guard is not None and guard.tripped and not fast:
        return Issue(f"flash.probe-{guard.tripped_code}", "error", guard.tripped)
    text = f"{out.stdout}\n{out.stderr}"
    if not out.success or "Script processing completed" not in text or _FAILURE.search(text):
        tail = fc._capture_tail(out) or "J-Link did not complete the script"
        return Issue("reset.failed", "error", f"the nRESET pulse did not complete: {tail}")
    if fast:
        seen = parse_handshakes(text)
        if seen != [usb_path]:
            return Issue(
                "flash.probe-verify-failed", "error",
                f"the pulse ran, but the wrapper did not attest isolation to {usb_path} "
                f"({HANDSHAKE_PREFIX} lines: {', '.join(seen) or 'none'}); it may have reached "
                "the wrong board",
            )
        data.setdefault("probe", {})["isolation"] = f"wrapper-attested:{usb_path}"
    return None


def _finish_pulse(data, ser, confirm, pulse_ms) -> _Result:
    place = f" on place {data['place']}" if data["place"] else ""
    line = f"reset: nRESET pulsed for {pulse_ms} ms{place}"
    if ser is None:
        note = Issue("reset.boot-not-confirmed", "info",
                     "pulse sent; boot not confirmed (pass --confirm-console PORT --expect REGEX)")
        return ExitCode.SUCCESS, data, [note], [line + "; boot not confirmed"]
    data["console"] = rc_mod.observe(ser, confirm)
    data["resetObserved"] = bool(data["console"]["observed"])
    if not data["resetObserved"]:
        issue = Issue("reset.boot-not-observed", "error",
                      f"pulse sent, but no console line matched --expect within {confirm.timeout_s}s on "
                      f"{confirm.port}: the board did not visibly reboot")
        return _fail(data, issue)
    return ExitCode.SUCCESS, data, [], [line + "; boot observed"]


def _run(
    pulse_ms: int,
    probe_serial: str | None,
    probe_usb_path: str | None,
    jlink_path: str | None,
    project_dir: str,
    enumerate_probes: Callable[[], Any] | None = None,
    confirm: "rc_mod.ConfirmSpec | None" = None,
) -> _Result:
    data: dict[str, Any] = {
        "schemaVersion": _SCHEMA_VERSION, "pulseMs": pulse_ms,
        "place": os.environ.get(PLACE_ENV) or None, "writes": False,
        "resetObserved": "unknown",
    }
    try:
        rp.check_pulse_ms(pulse_ms)
    except rp.ResetArgError as err:
        return _fail(data, Issue("reset.bad-argument", "error", str(err)), ExitCode.VALIDATION_FAILURE)
    found = resolve_jlink(jlink_path, project_dir=project_dir)
    exe = found.path if found is not None else None
    data["jlink"] = {"binary": exe, "binarySource": found.source if found else None}
    if exe is None:
        return _fail(data, Issue("reset.failed", "error", fc._NO_TRUSTED_JLINK))
    fast = bool(data["place"]) and probe_usb_path is not None
    try:
        picked = _select(data, probe_serial, probe_usb_path, jlink_path, project_dir, enumerate_probes)
        if isinstance(picked, tuple) and len(picked) == 4:
            return picked
        guard, serial = picked
        script = rp.pulse_script(None if fast else serial, pulse_ms)
    except (FlashPlanError, rp.ResetArgError) as err:
        return _fail(data, Issue("reset.failed", "error", str(err)))
    data["script"] = fc._DISABLE_FW_UPDATE.splitlines() + script.splitlines()
    data["singleSpawn"] = fast
    ser = None
    if confirm is not None:
        try:
            ser = rc_mod.open_console(confirm)
        except MonitorError as err:
            return _fail(data, Issue("reset.console-open-failed", "error", err.message))
    out = _spawn(guard, script, exe, probe_usb_path, fast)
    problem = _verdict(data, out, guard, fast, probe_usb_path)
    if problem is not None:
        if ser is not None:
            ser.close()
        return _fail(data, problem)
    return _finish_pulse(data, ser, confirm, pulse_ms)


def reset(
    ctx: typer.Context,
    project: str = typer.Option(None, "--project", metavar="PATH", help="Project root (defaults to '.')."),
    pulse_ms: int = typer.Option(
        rp.DEFAULT_PULSE_MS, "--pulse-ms", metavar="MS", show_default=True,
        help=f"How long nRESET is held low ({rp.MIN_PULSE_MS}..{rp.MAX_PULSE_MS})."),
    probe_serial: str = typer.Option(None, "--probe-serial", metavar="SN", help="J-Link serial for this run."),
    probe_usb_path: str = typer.Option(
        None, "--probe-usb-path", metavar="BUS-PORT",
        help="Select the J-Link at this USB port path (e.g. 3-4.2); verified before the JLinkExe "
        "spawn exactly as `tan flash` does (TAN_PROBE_USB_PATH is exported for a masking wrapper)."),
    confirm_console: str = typer.Option(
        None, "--confirm-console", metavar="PORT",
        help="Serial port (or rfc2217:// URL) to watch for the reboot; opened before the pulse. "
        "Needs --expect. Without it the result is resetObserved: unknown."),
    expect: str = typer.Option(
        None, "--expect", metavar="REGEX",
        help="With --confirm-console: a line printed after reset; only bytes that arrive after "
        "J-Link exits count."),
    confirm_baud: int = typer.Option(None, "--confirm-baud", metavar="BAUD", help="Console baud (default 115200)."),
    confirm_timeout: float = typer.Option(
        None, "--confirm-timeout", metavar="SECONDS", help="How long to wait for --expect (default 30)."),
    jlink: str = typer.Option(
        None, "--jlink", metavar="PATH",
        help=f"The J-Link Commander binary (otherwise {JLINK_ENV}, PATH, then a SEGGER install root; "
        "never the project .venv)."),
    output_format: OutputFormat = typer.Option(None, "--format", help=FORMAT_HELP),
) -> None:
    """ONE bare nRESET pulse through J-Link: `r0`, wait --pulse-ms, `r1`. No connect, no halt, no
    connect-under-reset, no retry; it does not flash. Set JLINK_RUN_PLACE (with --probe-usb-path) in the
    environment to pick the board-farm place for this command.

    \b
    tan reset --probe-usb-path 3-4.2
    JLINK_RUN_PLACE=aen-evk-02 tan reset --probe-usb-path 3-4.2 --pulse-ms 200 --format json
    tan reset --probe-usb-path 3-4.2 --confirm-console rfc2217://gw:4001 --expect 'Zephyr'
    (the reset is only claimed with --confirm-console; otherwise resetObserved is "unknown")
    """
    json_mode = resolve_format(output_format, ctx.obj, choices=OutputFormat) == "json"
    probe_serial = probe_serial if isinstance(probe_serial, str) else None
    probe_usb_path = probe_usb_path if isinstance(probe_usb_path, str) else None
    if probe_usb_path is not None and not fc.is_valid_usb_path(probe_usb_path):
        raise typer.BadParameter(
            f"{probe_usb_path!r} is not a USB port path like 3-4.2 (<bus>-<port>[.<port>...])",
            param_hint="--probe-usb-path",
        )
    try:
        confirm = rc_mod.confirm_spec(confirm_console, expect, confirm_baud, confirm_timeout)
    except rc_mod.ConfirmError as err:
        raise typer.BadParameter(str(err)) from err
    cwd = fc.workspace_root(project)
    project_obj = fc._resolve_project(cwd, None)
    try:
        exit_code, data, issues, lines = _run(
            pulse_ms, probe_serial, probe_usb_path,
            jlink if isinstance(jlink, str) else None, cwd, confirm=confirm,
        )
    except Exception as err:  # noqa: BLE001 -- a tan bug is reported as one, with an envelope
        exit_code = ExitCode.INTERNAL_FAILURE
        data = {"schemaVersion": _SCHEMA_VERSION}
        issues = [Issue("reset.internal-failure", "error", f"{type(err).__name__}: {err}")]
        lines = ["reset: internal failure"]
    if json_mode:
        fc.emit(Envelope(COMMAND, project_obj, data, issues, exit_code))
    else:
        for line in lines:
            print(line, file=sys.stderr if exit_code else sys.stdout)
    raise typer.Exit(int(exit_code))


reset = accept_global_flags(reset)
