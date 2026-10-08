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

import sys
from typing import Any, Callable

import typer

from tan.commands import flash_cmd as fc
from tan.core import probe_plan as pp
from tan.core.dp_id import _dp_id_matches, _dp_id_value
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
from tan.core.ram_run import RamRunError, attached_core, check_session, core_check, parse_mem32
from tan.envelope import Envelope, Issue
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat, resolve_format

COMMAND = "probe"
VERBS = ("identify", "read")
_SCHEMA_VERSION = 1


def _refuse(verb, issue, data=None, rc=ExitCode.RUNTIME_FAILURE):
    body = {"schemaVersion": _SCHEMA_VERSION, "verb": verb, **(data or {})}
    return rc, body, [issue], [f"probe {verb}: {issue.message}"]


def _arg_issue(err: pp.ProbeArgError) -> Issue:
    if err.code == pp.CODE_READ_TOO_LARGE:
        return Issue("probe.read-too-large", "error", str(err))
    return Issue("probe.bad-argument", "error", str(err))


def _slice_flash_args(build_root: str, core: str | None) -> tuple[dict[str, Any], str | None]:
    """The manifest slice's `flash_args` (`{}` with no manifest) and a note on why."""
    path = fc._abs_join(build_root, "system-manifest.yaml")
    if not fc._is_file(path):
        return {}, None
    try:
        manifest = parse_system_manifest(fc._read(path))
        targets = [t for t in fc.plan_flash_targets(manifest, core, None).targets if t.kind == "slice"]
    except Exception as err:  # noqa: BLE001 -- a broken manifest only loses the comparison
        return {}, f"{path} not usable ({type(err).__name__}); no expected DPIDR to compare"
    armed = [t for t in targets if isinstance(t.flash_args, dict) and t.flash_args.get("expect_dpidr")]
    if len(armed) == 1:
        return dict(armed[0].flash_args), None
    if len(armed) > 1:
        return {}, "several slices declare expect_dpidr; pass --core to pick one"
    return {}, None


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
) -> tuple[ExitCode, dict[str, Any], list[Issue], list[str]]:
    if verb not in VERBS:
        return _refuse(
            verb,
            Issue("probe.unknown-verb", "error",
                  f"unknown verb {verb!r}; use `tan probe identify` or `tan probe read <addr> [words]`"),
            rc=ExitCode.VALIDATION_FAILURE,
        )
    bad = ExitCode.VALIDATION_FAILURE
    try:
        core = pp.normalise_core(core)
        words = addr = None
        if verb == "read":
            if address is None:
                raise pp.ProbeArgError(pp.CODE_BAD_ARGUMENT, "`tan probe read` needs an address")
            addr, words = pp.parse_read(address, count)
        elif address is not None or count is not None:
            raise pp.ProbeArgError(pp.CODE_BAD_ARGUMENT, "`tan probe identify` takes no address")
    except pp.ProbeArgError as err:
        return _refuse(verb, _arg_issue(err), rc=bad)
    window = verb == "read" and pp.touches_unsafe_window(addr, words)
    if window and core == "m55_he":
        return _refuse(
            verb,
            Issue("probe.read-unsafe-region", "error",
                  pp.unsafe_region_message(addr, words, "--core m55_he selects the HE core.")),
            rc=bad,
        )

    flash_args, note = _slice_flash_args(build_root, core)
    report: dict[str, Any] = {"core": core, "buildRoot": build_root, "writes": False}
    if note:
        report["manifestNote"] = note
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
            return _refuse(
                verb, Issue(f"flash.probe-{selection.refusal_code}", "error", selection.refusal or ""), data
            )
        guard = None
        if selection is not None and (selection.serial is not None or selection.visible is None):
            guard = fc._ProbeGuard(selection, snapshot, ctx.enumerate_probes, echo)
        serial = fa_str_checked(flash_args, "jlink_serial", False)
        device = fa_str(flash_args, "jlink_device") or pp.DEFAULT_DEVICE
        validate_identifier(device, "jlink_device", destination="a J-Link Commander script line")
        if not device.startswith("Cortex-"):
            device = pp.DEFAULT_DEVICE  # the part profile is a flash loader, not a read attach
        speed = fa_int_checked(flash_args, "jlink_speed") or _DEFAULT_JLINK_SPEED
        if serial is not None:
            validate_identifier(serial, "jlink_serial", destination=fc._JLINK_SERIAL_DESTINATION)
        pre = pp.make_preamble(serial, speed, device)
        expected = fa_str(flash_args, "expect_dpidr")
    except (FlashPlanError, RamRunError) as err:
        return _refuse(verb, Issue("probe.failed", "error", str(err)), report)
    if echo:
        report["probe"] = echo

    argv = ("JLinkExe", "-device", device, "-if", "SWD", "-speed", str(speed), "-NoGui", "1",
            "-CommanderScript")

    def session(script: str):
        out = fc._execute(
            FlashPlan(argv=argv, ok_message="", jlink_script=script),
            True, None, None, guard, jlink_exe=exe,
        )
        text = f"{out.stdout}\n{out.stderr}"
        if guard is not None and guard.tripped:
            return text, guard.tripped, guard.tripped_code
        problem = None if out.success else (fc._capture_tail(out) or "J-Link failed")
        return text, problem or check_session(text, loadbin=False), None

    def failed(message, read=False):
        if guard is not None and guard.tripped:
            return _refuse(
                verb, Issue(f"flash.probe-{guard.tripped_code}", "error", guard.tripped), report
            )
        code = Issue("probe.read-failed", "error", message) if read else Issue("probe.failed", "error", message)
        return _refuse(verb, code, report)

    if verb == "identify":
        report["scripts"] = {"dpidr": pp.identity_script(pre).splitlines(),
                             "coreCheck": pp.core_script(pre).splitlines()}
        text, problem, _ = session(pp.identity_script(pre))
        if problem:
            return failed(f"the DPIDR read failed: {problem}")
        dpidr = _dp_id_value(text)
        text2, problem, _ = session(pp.core_script(pre))
        if problem:
            return failed(f"the core check failed: {problem}")
        verdict, evidence = core_check(text2)
        att = evidence.get("ap") or {}
        match = None if not expected or dpidr is None else _dp_id_matches(expected, text)
        report["identity"] = {
            "dpidr": dpidr, "expectedDpidr": expected, "dpidrMatch": match,
            "apAddr": att.get("apAddr"), "cpuid": att.get("cpuid"),
            "core": verdict, "itcmVerdict": evidence["itcmVerdict"],
            "apVerdict": evidence["apVerdict"], "attached": att or None,
            "isolation": (echo or {}).get("isolation"),
        }
        issues: list[Issue] = []
        if dpidr is None:
            issues.append(Issue("probe.dpidr-unread", "error",
                                "the connect banner reported no SW-DP ID"))
        elif match is False:
            issues.append(Issue("probe.dpidr-mismatch", "error",
                                f"expected SW-DP IDR {expected} (manifest expect_dpidr) but the probe "
                                f"reported {dpidr}: a different board is attached"))
        rc = ExitCode.RUNTIME_FAILURE if issues else ExitCode.SUCCESS
        line = f"probe identify: dpidr={dpidr} core={verdict} ap={att.get('apAddr')} cpuid={att.get('cpuid')}"
        return rc, {"schemaVersion": _SCHEMA_VERSION, "verb": verb, **report}, issues, [line]

    # ── read ──
    if window:
        text, problem, _ = session(pp.core_script(pre))
        if problem:
            return failed(f"cannot establish which core is attached ({problem})")
        verdict, evidence = core_check(text)
        report["attachedCore"] = {"verdict": verdict, "ap": evidence.get("ap")}
        if evidence["apVerdict"] != "hp" or core not in (None, "m55_hp"):
            return _refuse(
                verb,
                Issue("probe.read-unsafe-region", "error", pp.unsafe_region_message(
                    addr, words,
                    f"The attached core is not confirmed to be the M55-HP (access-port verdict "
                    f"{evidence['apVerdict']}).",
                )), report, rc=bad,
            )
    script = pp.read_script(pre, addr, words)
    report["script"] = script.splitlines()
    text, problem, _ = session(script)
    if problem:
        return failed(f"the read failed: {problem}", read=True)
    got = parse_mem32(text, addr, words)
    if got is None:
        return failed(f"J-Link returned no complete dump for {words} words at 0x{addr:08X}", read=True)
    report["read"] = {
        "address": f"0x{addr:08X}", "words": words, "bytes": 4 * words,
        "data": [f"0x{w:08X}" for w in got],
        "attached": attached_core(text),
    }
    lines = [f"probe read: 0x{addr + 4 * i:08X} = 0x{w:08X}" for i, w in enumerate(got)]
    return ExitCode.SUCCESS, {"schemaVersion": _SCHEMA_VERSION, "verb": verb, **report}, [], lines


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
        help="The core you mean to probe; also picks the manifest slice. read refuses "
        "0x50000000-0x5FFFFFFF (probe.read-unsafe-region) for m55_he, and for any attach it cannot "
        "confirm is the M55-HP: an HE session must not touch that window."),
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
