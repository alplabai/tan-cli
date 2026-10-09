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
import sys
from typing import Any, Callable

import typer

from tan.commands import flash_cmd as fc
from tan.core import reset_plan as rp
from tan.core.flash_plan import FlashPlan, FlashPlanError
from tan.core.global_flags import accept_global_flags
from tan.core.jlink_binary import ENV_OVERRIDE as JLINK_ENV, resolve_jlink
from tan.envelope import Envelope, Issue
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat, resolve_format

COMMAND = "reset"
PLACE_ENV = "JLINK_RUN_PLACE"
_SCHEMA_VERSION = fc._DATA_SCHEMA_VERSION
_SCRIPT_PREFIX = "tan-reset-"
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


def _run(
    pulse_ms: int,
    probe_serial: str | None,
    probe_usb_path: str | None,
    jlink_path: str | None,
    project_dir: str,
    enumerate_probes: Callable[[], Any] | None = None,
) -> _Result:
    data: dict[str, Any] = {
        "schemaVersion": _SCHEMA_VERSION, "pulseMs": pulse_ms,
        "place": os.environ.get(PLACE_ENV) or None, "writes": False,
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
    try:
        picked = _select(data, probe_serial, probe_usb_path, jlink_path, project_dir, enumerate_probes)
        if isinstance(picked, tuple) and len(picked) == 4:
            return picked
        guard, serial = picked
        script = rp.pulse_script(serial, pulse_ms)
    except (FlashPlanError, rp.ResetArgError) as err:
        return _fail(data, Issue("reset.failed", "error", str(err)))
    data["script"] = fc._DISABLE_FW_UPDATE.splitlines() + script.splitlines()
    # No -device/-speed/-autoconnect 1: Commander opens the probe and never attaches.
    argv = ("JLinkExe", "-if", "SWD", "-autoconnect", "0", "-NoGui", "1", "-CommanderScript")
    out = fc._execute(
        FlashPlan(argv=argv, ok_message="", jlink_script=script),
        True, None, None, guard, jlink_exe=exe, script_prefix=_SCRIPT_PREFIX,
    )
    if guard is not None and guard.tripped:
        return _fail(data, Issue(f"flash.probe-{guard.tripped_code}", "error", guard.tripped))
    if not out.success or "Script processing completed" not in f"{out.stdout}\n{out.stderr}":
        tail = fc._capture_tail(out) or "J-Link did not complete the script"
        return _fail(data, Issue("reset.failed", "error", f"the nRESET pulse did not complete: {tail}"))
    place = f" on place {data['place']}" if data["place"] else ""
    return ExitCode.SUCCESS, data, [], [f"reset: nRESET pulsed for {pulse_ms} ms{place}"]


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
    jlink: str = typer.Option(
        None, "--jlink", metavar="PATH",
        help=f"The J-Link Commander binary (otherwise {JLINK_ENV}, PATH, then a SEGGER install root; "
        "never the project .venv)."),
    output_format: OutputFormat = typer.Option(None, "--format", help=FORMAT_HELP),
) -> None:
    """ONE bare nRESET pulse through J-Link: `r0`, wait --pulse-ms, `r1`. No connect, no halt, no
    connect-under-reset, no retry; it does not flash. Set JLINK_RUN_PLACE in the
    environment to pick the board-farm place for this command.

    \b
    tan reset --probe-usb-path 3-4.2
    JLINK_RUN_PLACE=aen-evk-02 tan reset --pulse-ms 200 --format json
    """
    json_mode = resolve_format(output_format, ctx.obj, choices=OutputFormat) == "json"
    probe_serial = probe_serial if isinstance(probe_serial, str) else None
    probe_usb_path = probe_usb_path if isinstance(probe_usb_path, str) else None
    if probe_usb_path is not None and not fc.is_valid_usb_path(probe_usb_path):
        raise typer.BadParameter(
            f"{probe_usb_path!r} is not a USB port path like 3-4.2 (<bus>-<port>[.<port>...])",
            param_hint="--probe-usb-path",
        )
    cwd = fc.workspace_root(project)
    project_obj = fc._resolve_project(cwd, None)
    try:
        exit_code, data, issues, lines = _run(
            pulse_ms, probe_serial, probe_usb_path,
            jlink if isinstance(jlink, str) else None, cwd,
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
