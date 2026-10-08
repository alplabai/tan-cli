# SPDX-License-Identifier: Apache-2.0
"""`tan probe <identify|read>` -- READ-ONLY J-Link probes (tan-cli#1406).

The AEN bench dogfood needed raw `JLinkExe` for two read-only questions; this is
that, going through the machinery `tan flash --ram` already uses: the trusted
J-Link binary (`resolve_jlink`), the probe-selection guard
(`--probe-usb-path` / `--probe-serial`, the `ShowEmuList` TOCTOU check that is
what the board-farm `JLINK_RUN_PLACE` shim hooks), `flash_cmd._execute` ->
`_spawn_jlink`, and the `--ram` attach check's own scripts. Nothing is halted,
written or reset: see `tan.core.probe_plan` for the script rules.

Flat verb command (like `tan sdk`): an unknown verb gets a real coded envelope,
not a parser-level usage error.
"""
from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import typer

from tan.commands import flash_cmd as fc
from tan.core import probe_plan as pp
from tan.core.dp_id import _dp_id_value
from tan.core.flash_plan import (
    _DEFAULT_JLINK_SPEED,
    FlashPlan,
    FlashPlanError,
    parse_system_manifest,
    fa_int_checked,
    fa_str,
    fa_str_checked,
    validate_identifier,
)
from tan.core.global_flags import accept_global_flags
from tan.core.jlink_binary import ENV_OVERRIDE as JLINK_ENV, resolve_jlink
from tan.core.ram_run import (
    RamRunError,
    ap_verdict,
    attached_core,
    check_session,
    core_check,
    parse_mem32,
)
from tan.envelope import Envelope, Issue
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat, resolve_format

COMMAND = "probe"
VERBS = ("identify", "read")
_SCHEMA_VERSION = fc._DATA_SCHEMA_VERSION  # the string "1", like every other command
_SCRIPT_PREFIX = "tan-probe-"
_GUARD_SCRIPT = (fc._DISABLE_FW_UPDATE + "ShowEmuList\nexit\n").splitlines()
_Result = tuple[ExitCode, dict[str, Any], list[Issue], list[str]]


@dataclass
class _Run:
    """Everything one verb needs, resolved once by `_prepare`."""

    verb: str
    core: str | None
    build_root: str
    exe: str
    pre: list[str]
    argv: tuple[str, ...]
    guard: Any
    echo: dict[str, Any] | None
    expected: str | None
    selected_id: str | None
    report: dict[str, Any]
    issues: list[Issue] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def _body(verb: str, report: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"schemaVersion": _SCHEMA_VERSION, "verb": verb, **(report or {})}


def _refuse(verb, issue, data=None, rc=ExitCode.RUNTIME_FAILURE) -> _Result:
    return rc, _body(verb, data), [issue], [f"probe {verb}: {issue.message}"]


def _arg_issue(err: pp.ProbeArgError) -> Issue:
    if err.code == pp.CODE_READ_TOO_LARGE:
        return Issue("probe.read-too-large", "error", str(err))
    return Issue("probe.bad-argument", "error", str(err))


def _load_slices(build_root: str) -> tuple[list[tuple[str, dict]], bool, Issue | None]:
    """`(slices, manifest present, warning)`. An existing but unusable manifest is a
    `probe.manifest-unusable` warning: the comparison it would have armed is skipped."""
    path = fc._abs_join(build_root, "system-manifest.yaml")
    if not fc._is_file(path):
        return [], False, None
    try:
        manifest = parse_system_manifest(fc._read(path))
    except Exception as err:  # noqa: BLE001 -- a broken manifest only loses the comparison
        return [], True, Issue(
            "probe.manifest-unusable", "warning",
            f"{path} exists but is not usable ({type(err).__name__}: {err}); the expect_dpidr "
            "comparison and the manifest's probe settings were skipped",
        )
    return [(sl.core_id, sl.flash_args) for sl in manifest.slices], True, None


def _attach_params(flash_args: dict[str, Any], jlink_report: dict[str, Any]):
    """`(preamble, speed, expect_dpidr)` from the selected slice's flash_args; echoes a
    `jlink_device` substitution into the report."""
    serial = fa_str_checked(flash_args, "jlink_serial", False)
    wanted = fa_str(flash_args, "jlink_device") or pp.DEFAULT_DEVICE
    validate_identifier(wanted, "jlink_device", destination="a J-Link Commander script line")
    device = wanted if wanted.startswith("Cortex-") else pp.DEFAULT_DEVICE
    jlink_report["device"] = device
    if device != wanted:  # the part profile is a flash loader, not a read attach
        jlink_report["deviceSubstitutedFrom"] = wanted
    speed = fa_int_checked(flash_args, "jlink_speed") or _DEFAULT_JLINK_SPEED
    if serial is not None:
        validate_identifier(serial, "jlink_serial", destination=fc._JLINK_SERIAL_DESTINATION)
    return pp.make_preamble(serial, speed, device), speed, fa_str(flash_args, "expect_dpidr")


def _prepare(
    verb, core, build_root, probe_serial, probe_usb_path, jlink_path, project_dir, enumerate_probes
) -> _Run | _Result:
    slices, present, warning = _load_slices(build_root)
    flash_args, selected_id, ambiguity, note = pp.select_slice(slices, core)
    report: dict[str, Any] = {
        "core": core, "buildRoot": build_root, "writes": False, "manifestPresent": present,
    }
    warn = [warning] if warning else []
    if note:
        warn.append(Issue("probe.no-manifest", "info", note))
    if ambiguity:
        return _refuse(verb, Issue("probe.failed", "error", ambiguity), report)
    found = resolve_jlink(jlink_path, project_dir=project_dir)
    exe = found.path if found is not None else None
    report["jlink"] = {"binary": exe, "binarySource": found.source if found else None}
    if exe is None:
        return _refuse(verb, Issue("probe.failed", "error", fc._NO_TRUSTED_JLINK), report)
    ctx = fc._Context(
        sku="", build_root=build_root, sdk_root="", dry_run=False, skip_missing_tools=False,
        force_confirm=False, capture=True, probe_serial=probe_serial,
        probe_usb_path=probe_usb_path, project_dir=project_dir, jlink_path=jlink_path,
        **({"enumerate_probes": enumerate_probes} if enumerate_probes is not None else {}),
    )
    try:
        flash_args, selection, snapshot = fc._flow_d_probe_selection(flash_args, ctx)
        echo = fc._probe_echo(selection, ctx)
        if selection is not None and selection.refusal_code is not None:
            data = {**report, **({"probe": echo} if echo else {})}
            issue = Issue(f"flash.probe-{selection.refusal_code}", "error", selection.refusal or "")
            return _refuse(verb, issue, data)
        guard = None
        if selection is not None and (selection.serial is not None or selection.visible is None):
            guard = fc._ProbeGuard(selection, snapshot, ctx.enumerate_probes, echo,
                                   script_prefix=_SCRIPT_PREFIX)
        pre, speed, expected = _attach_params(flash_args, report["jlink"])
    except (FlashPlanError, RamRunError) as err:
        return _refuse(verb, Issue("probe.failed", "error", str(err)), report)
    if echo:
        report["probe"] = echo
    report["guard"] = {"script": _GUARD_SCRIPT, "runsBeforeEachSession": guard is not None,
                       "note": "the probe-pin verification, spawned before each session below"}
    argv = ("JLinkExe", "-device", report["jlink"]["device"], "-if", "SWD", "-speed", str(speed),
            "-NoGui", "1",
            "-CommanderScript")
    run = _Run(verb, core, build_root, exe, pre, argv, guard, echo, expected, selected_id, report)
    run.issues.extend(warn)
    return run


def _session(run: _Run, label: str, script: str) -> tuple[str, str | None]:
    """Spawn one read-only session. `(transcript, problem)`; the text sent and the
    output go to the run's transcript."""
    sent = fc._DISABLE_FW_UPDATE + script
    out = fc._execute(
        FlashPlan(argv=run.argv, ok_message="", jlink_script=script),
        True, None, None, run.guard, jlink_exe=run.exe, script_prefix=_SCRIPT_PREFIX,
    )
    text = f"{out.stdout}\n{out.stderr}"
    run.log.append(f"## {label} script (exactly as sent)\n{sent}\n## stdout\n{out.stdout}\n"
                   f"## stderr\n{out.stderr}\n")
    run.report.setdefault("scripts", {})[label] = sent.splitlines()
    if run.guard is not None and run.guard.tripped:
        return text, run.guard.tripped
    problem = None if out.success else (fc._capture_tail(out) or "J-Link failed")
    return text, problem or check_session(text, loadbin=False)


def _cache_log_dir() -> str:
    """One stable directory for transcripts when there is no build root, so nothing
    accumulates in fresh temp dirs: `$XDG_CACHE_HOME/tan/probe-logs`, else
    `~/.cache/tan/probe-logs` (`_write_transcript` prunes the oldest)."""
    root = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(root, "tan", "probe-logs")


def _save_log(run: _Run) -> None:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = re.sub(r"[^A-Za-z0-9._-]", "_", f"probe-{run.verb}-{stamp}.log")
    base = os.path.join(run.build_root, "flash-logs") if os.path.isdir(run.build_root) else None
    try:
        directory = base or _cache_log_dir()
        run.report["transcriptPath"] = fc._write_transcript(
            os.path.join(directory, name), f"# tan probe {run.verb}\n" + "".join(run.log)
        )
    except OSError as err:
        run.report["transcriptPath"] = None
        run.report["transcriptError"] = str(err)


def _finish(run: _Run, rc: ExitCode, lines: list[str], *more: Issue) -> _Result:
    if run.echo is not None and "identity" in run.report:
        run.report["identity"]["isolation"] = run.echo.get("isolation")
    run.issues.extend(more)
    _save_log(run)
    return rc, _body(run.verb, run.report), run.issues, lines


def _fail(run: _Run, message: str, *, read: bool = False) -> _Result:
    if run.guard is not None and run.guard.tripped:
        issue = Issue(f"flash.probe-{run.guard.tripped_code}", "error", run.guard.tripped)
    else:
        issue = (Issue("probe.read-failed", "error", message) if read
                 else Issue("probe.failed", "error", message))
    return _finish(run, ExitCode.RUNTIME_FAILURE, [f"probe {run.verb}: {issue.message}"], issue)


def _dpidr_issue(state: str, expected: str | None, dpidr: str | None) -> Issue:
    if state == "unread":
        return Issue("probe.dpidr-unread", "error", "the connect banner reported no SW-DP ID")
    return Issue(
        "probe.dpidr-mismatch", "error",
        f"expected SW-DP IDR {expected} (manifest expect_dpidr) but the probe reported "
        f"{dpidr}: a different board is attached",
    )


def _core_issue(message: str) -> Issue:
    return Issue("probe.core-mismatch", "error", message)


def _identify(run: _Run) -> _Result:
    text, problem = _session(run, "dpidr", pp.identity_script(run.pre))
    if problem:
        return _fail(run, f"the DPIDR read failed: {problem}")
    dpidr, att = _dp_id_value(text), attached_core(text)
    verdict = ap_verdict(att)
    state = pp.dpidr_state(run.expected, dpidr) or ("unread" if dpidr is None else None)
    ident = {
        "dpidr": dpidr, "expectedDpidr": run.expected,
        "dpidrMatch": None if not run.expected or dpidr is None else state is None,
        "apAddr": (att or {}).get("apAddr"), "cpuid": (att or {}).get("cpuid"),
        "core": verdict, "itcmVerdict": "not-checked", "apVerdict": verdict,
        "attached": att, "isolation": None,
    }
    run.report["identity"] = ident
    if state:  # C: nothing else runs against a board that is not the expected one
        issue = _dpidr_issue(state, run.expected, dpidr)
        return _finish(run, ExitCode.RUNTIME_FAILURE, [f"probe identify: {issue.message}"], issue)
    if pp.itcm_check_allowed(pp.is_he_target(run.core, run.selected_id), verdict):
        text2, problem = _session(run, "coreCheck", pp.core_script(run.pre))
        if problem:
            return _fail(run, f"the core check failed: {problem}")
        verdict, evidence = core_check(text2)
        ident.update(core=verdict, itcmVerdict=evidence["itcmVerdict"],
                     apVerdict=evidence["apVerdict"], attached=evidence.get("ap") or att)
        ident["apAddr"] = (evidence.get("ap") or att or {}).get("apAddr")
        ident["cpuid"] = (evidence.get("ap") or att or {}).get("cpuid")
    extra: list[Issue] = []
    if ident["itcmVerdict"] == "not-checked":
        extra.append(Issue("probe.itcm-not-checked", "info",
                           "ITCM corroboration skipped: "
                           + pp.itcm_skip_reason(run.core, run.selected_id, verdict)))
    clash = pp.core_contradiction(pp.claimed_core(run.core, run.selected_id), verdict)
    if clash:
        extra.append(_core_issue(clash))
    if not run.expected and not any(i.code == "probe.no-manifest" for i in run.issues):
        why = "no system-manifest.yaml" if not run.report["manifestPresent"] else "no expect_dpidr armed"
        extra.append(Issue("probe.no-manifest", "info",
                           f"{why} for the selected core; the DPIDR match was skipped"))
    rc = ExitCode.RUNTIME_FAILURE if clash else ExitCode.SUCCESS
    line = (f"probe identify: dpidr={dpidr} core={verdict} ap={ident['apAddr']} "
            f"cpuid={ident['cpuid']}")
    return _finish(run, rc, [line], *extra)


def _read(run: _Run, addr: int, words: int) -> _Result:
    text, problem = _session(run, "read", pp.read_script(run.pre, addr, words))
    if problem:
        return _fail(run, f"the read failed: {problem}", read=True)
    dpidr, att = _dp_id_value(text), attached_core(text)
    run.report["attached"] = {"dpidr": dpidr, "ap": att}
    state = pp.dpidr_state(run.expected, dpidr)
    clash = pp.core_contradiction(pp.claimed_core(run.core, run.selected_id), ap_verdict(att))
    bad = _dpidr_issue(state, run.expected, dpidr) if state else (_core_issue(clash) if clash else None)
    if bad:  # the words may belong to another board or core: not returned
        return _finish(run, ExitCode.RUNTIME_FAILURE, [f"probe read: {bad.message}"], bad)
    got = parse_mem32(text, addr, words)
    if got is None:
        return _fail(run, f"J-Link returned no complete dump for {words} words at 0x{addr:08X}",
                     read=True)
    run.report["read"] = {
        "address": f"0x{addr:08X}", "words": words, "bytes": 4 * words,
        "data": [f"0x{w:08X}" for w in got], "attached": att,
    }
    lines = [f"probe read: 0x{addr + 4 * i:08X} = 0x{w:08X}" for i, w in enumerate(got)]
    return _finish(run, ExitCode.SUCCESS, lines)


def _run(
    verb: str,
    address: str | None,
    count: str | None,
    core: str | None,
    build_root: str,
    probe_serial: str | None,
    probe_usb_path: str | None,
    jlink_path: str | None,
    project_dir: str,
    enumerate_probes: Callable[[], Any] | None = None,
) -> _Result:
    bad = ExitCode.VALIDATION_FAILURE
    if verb not in VERBS:
        text = f"unknown verb {verb!r}; use `tan probe identify` or `tan probe read <addr> [words]`"
        return _refuse(verb, Issue("probe.unknown-verb", "error", text), rc=bad)
    addr = words = None
    try:
        core = pp.normalise_core(core)
        if verb == "read":
            if address is None:
                raise pp.ProbeArgError(pp.CODE_BAD_ARGUMENT, "`tan probe read` needs an address")
            addr, words = pp.parse_read(address, count)
        elif address is not None or count is not None:
            raise pp.ProbeArgError(pp.CODE_BAD_ARGUMENT, "`tan probe identify` takes no address")
    except pp.ProbeArgError as err:
        return _refuse(verb, _arg_issue(err), rc=bad)
    message = pp.window_refusal(addr, words) if verb == "read" else None
    if message:  # every core, no J-Link spawned
        return _refuse(verb, Issue("probe.read-unsafe-region", "error", message), rc=bad)
    run = _prepare(verb, core, build_root, probe_serial, probe_usb_path, jlink_path,
                   project_dir, enumerate_probes)
    if not isinstance(run, _Run):
        return run
    return _identify(run) if verb == "identify" else _read(run, addr, words)


def probe(
    ctx: typer.Context,
    verb: str = typer.Argument(..., metavar="VERB", help="`identify` or `read`."),
    address: str = typer.Argument(
        None, metavar="[ADDR]", help="read: first word address, plain hex (0x...) or decimal, "
        "4-byte aligned."),
    count: str = typer.Argument(
        None, metavar="[WORDS]", help="read: 32-bit words to read (default 4, at most 256; "
        "more is probe.read-too-large)."),
    project: str = typer.Option(None, "--project", metavar="PATH", help="Project root (defaults to '.')."),
    build_root: str = typer.Option(
        None, "--build-root", metavar="PATH",
        help="Build root holding system-manifest.yaml (default <project>/build). When present, its "
        "slice's flash_args supply expect_dpidr (compared by identify), jlink_serial/jlink_speed."),
    core: str = typer.Option(
        None, "--core", metavar="m55_he|m55_hp",
        help="The core you mean to probe; also picks the manifest slice. identify checks the "
        "attach against it (probe.core-mismatch) and reads the ITCM windows only for m55_he. "
        "read refuses 0x50000000-0x5FFFFFFF on EVERY core (probe.read-unsafe-region)."),
    probe_serial: str = typer.Option(None, "--probe-serial", metavar="SN", help="J-Link serial for this run."),
    probe_usb_path: str = typer.Option(
        None, "--probe-usb-path", metavar="BUS-PORT",
        help="Select the J-Link at this USB port path (e.g. 3-4.2); verified before every JLinkExe "
        "spawn exactly as `tan flash` does (TAN_PROBE_USB_PATH is exported for a masking wrapper)."),
    jlink: str = typer.Option(
        None, "--jlink", metavar="PATH",
        help=f"The J-Link Commander binary (otherwise {JLINK_ENV}, PATH, then a SEGGER install root; "
        "never the project .venv)."),
    output_format: OutputFormat = typer.Option(None, "--format", help=FORMAT_HELP),
) -> None:
    """READ-ONLY J-Link probe: `identify` (SW-DP ID, access port, CPUID, core verdict) and
    `read ADDR [WORDS]` (a bounded `mem32`). Never writes, erases, halts, resets or runs.

    \b
    tan probe identify --probe-usb-path 3-4.2 --core m55_he --format json
    tan probe read 0x80010000 4 --probe-usb-path 3-4.2
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
    root = build_root if isinstance(build_root, str) else fc._abs_join(cwd, "build")
    if isinstance(build_root, str) and not root.startswith("/"):
        root = fc._abs_join(cwd, root)
    try:
        exit_code, data, issues, lines = _run(
            verb, address, count, core if isinstance(core, str) else None, root,
            probe_serial, probe_usb_path, jlink if isinstance(jlink, str) else None, cwd,
        )
    except Exception as err:  # noqa: BLE001 -- a tan bug is reported as one, with an envelope
        exit_code = ExitCode.INTERNAL_FAILURE
        data = {"schemaVersion": _SCHEMA_VERSION, "verb": verb}
        issues = [Issue("probe.internal-failure", "error", f"{type(err).__name__}: {err}")]
        lines = ["probe: internal failure"]
    if json_mode:
        fc.emit(Envelope(COMMAND, project_obj, data, issues, exit_code))
    else:
        for line in lines:
            print(line, file=sys.stderr if exit_code else sys.stdout)
    raise typer.Exit(int(exit_code))


probe = accept_global_flags(probe)
