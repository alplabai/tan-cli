# SPDX-License-Identifier: Apache-2.0
"""`tan flash --ram` -- AEN Flow C: J-Link ITCM RAM-run + RAM-console read
(tan-cli#1313).

**Why `flash --ram` and not `run --ram`.** `tan run` is a thin orchestrator that
builds and then delegates to this command's engine; everything Flow C needs --
the probe-selection guard (tan-cli#1312), the DPIDR preflight, the trusted J-Link
binary (tan-cli#1336), `--dry-run`, `--jlink`, the per-run envelope -- already
lives behind `tan flash`. A `run --ram` would re-expose a third copy of it.

**What it does** (the proven raw recipe is alp-sdk `scripts/bench/aen/ram-run.sh`):

1. read the slice's ELF and its sibling `zephyr.bin`; the load address is DERIVED
   from the ELF's LOAD segments and the image is refused unless it is linked for
   ITCM/SRAM (`tan.core.ram_run.plan_ram_image`) -- an MRAM-linked image would just
   re-enter the resident MRAM image;
2. verify the probe (the same `_ProbeGuard` as a Flow D write: ShowEmuList, TOCTOU
   re-check, shared-serial handshake, `exec DisableAutoUpdateFW`), run the read-only
   DPIDR preflight when `expect_dpidr`/`jlink_device` arm it, then ONE J-Link session:
   `connect; halt; loadbin zephyr.bin <base>; setpc <reset handler>; go`. `loadbin`
   resets the core, which loads SP from the vector table; the PC is set from the
   vector table's reset handler (refused if it disagrees with the ELF entry).
   `WReg` is not accepted by J-Link Commander here (bench notes), so SP is not
   written by hand -- the vector-table SP is reported, not applied;
3. with `--ram-console`, wait `--wait` seconds and read `ram_console_buf`
   (address and size from the ELF symbol, never hardcoded) in a second session
   through `mem8`, decode it and report it.

**Nothing writes MRAM**: the only addresses in the script are the ITCM/SRAM load
base and the console buffer, and an MRAM-linked image is refused.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

from tan.commands import flash_cmd as fc
from tan.core.flash_plan import (
    FLOW_D_METHOD,
    FlashInputs,
    FlashPlan,
    FlashPlanError,
    FlashTarget,
    _DEFAULT_JLINK_SPEED,
    dpidr_preflight_unarmed,
    fa_int_checked,
    fa_str,
    fa_str_checked,
    validate_identifier,
)
from tan.core.jlink_binary import resolve_jlink
from tan.core.ram_run import (
    CODE_CONSOLE_SYMBOL_MISSING,
    CODE_FAILED,
    CONSOLE_SYMBOL,
    RamRunError,
    check_session,
    decode_console,
    load_script,
    parse_elf,
    plan_ram_image,
    preamble,
    read_back,
    read_script,
)

#: The `method` this entry reports.
METHOD = "ram_run"
#: Default `--wait` (seconds the app runs before the console is read) -- the raw
#: script's `sleep_ms` default of 1500.
DEFAULT_WAIT_S = 1.5
#: J-Link's own attach profile for a live core (the script's `JLINK_DEVICE_READ`).
_DEFAULT_ATTACH_DEVICE = "Cortex-M55"


def _artefact_pair(path: str) -> tuple[str, str]:
    """`(elf, bin)` for a manifest artefact: `zephyr.elf` + `zephyr.bin`."""
    stem, ext = os.path.splitext(path)
    elf = path if ext.lower() == ".elf" else stem + ".elf"
    return elf, os.path.splitext(elf)[0] + ".bin"


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def run_ram_entry(target: FlashTarget, ctx: Any) -> tuple[int, Any, list[str]]:
    """RAM-run one slice. Returns `(rc, entry, text-lines)` like `_flash_entry`."""
    kind, entry_id = target.kind, target.id
    lines: list[str] = [f"flash: {kind} '{entry_id}' -> {METHOD}"]
    report: dict[str, Any] = {}
    probe_echo: dict[str, Any] | None = None

    def entry(status: str, rc: int, message: str, **kw: Any) -> Any:
        return fc._Entry(
            kind=kind, id=entry_id, method=METHOD, status=status, rc=rc, message=message,
            probe=probe_echo, extra=report, **kw,
        )

    def fail(message: str, code: str | None = CODE_FAILED, **kw: Any) -> tuple[int, Any, list[str]]:
        text = f"{METHOD}[{entry_id}]: {message}"
        lines.append(f"  FAIL: {text}")
        return 1, entry("failed", 1, text, issue_code=code, **kw), lines

    if kind != "slice":
        return fail("only a slice can be RAM-run, not a helper MCU")
    if not isinstance(target.flash_args, dict) and target.flash_args is not None:
        return fail("flash_args is not a mapping")
    flash_args: Any = dict(target.flash_args or {})

    # ── the image ──
    artefact = target.output_artefact
    if not artefact or fc.is_pending(artefact):
        return fail("the slice has no built output_artefact; run `tan build` first")
    elf_path, bin_path = _artefact_pair(
        fc.resolve_artefact_path(artefact, ctx.build_root, ctx.sdk_root, fc._is_file)
    )
    try:
        elf_bytes, bin_bytes = _read(elf_path), _read(bin_path)
    except OSError as err:
        return fail(f"cannot read the built image ({err}); expected {elf_path} and {bin_path}")
    try:
        image = plan_ram_image(parse_elf(elf_bytes), bin_bytes)
    except RamRunError as err:
        return fail(str(err), err.code)

    # ── J-Link: trusted binary, probe selection, parameters ──
    found = resolve_jlink(ctx.jlink_path)
    exe = found.path if found is not None else None
    report["jlink"] = {"binary": exe, "binarySource": found.source if found else None}
    if exe is None and not ctx.dry_run:
        return fail(fc._NO_TRUSTED_JLINK)
    try:
        flash_args, selection, snapshot = fc._flow_d_probe_selection(flash_args, ctx)
        probe_echo = fc._probe_echo(selection, ctx)
        if selection is not None and selection.refusal_code is not None:
            lines.append(f"  FAIL: {selection.refusal}")
            return (
                1,
                entry("failed", 1, selection.refusal or "", probe_refusal=selection.refusal_code),
                lines,
            )
        guard = None
        if selection is not None and (selection.serial is not None or selection.visible is None):
            guard = fc._ProbeGuard(selection, snapshot, ctx.enumerate_probes, probe_echo)
        serial = fa_str_checked(flash_args, "jlink_serial", False)
        device = fa_str(flash_args, "jlink_device") or _DEFAULT_ATTACH_DEVICE
        validate_identifier(device, "jlink_device", destination="a J-Link Commander script line")
        speed = fa_int_checked(flash_args, "jlink_speed") or _DEFAULT_JLINK_SPEED
        if serial is not None:
            validate_identifier(serial, "jlink_serial", destination=fc._JLINK_SERIAL_DESTINATION)
        fc.validate_flow_d_preflight_args(flash_args)
    except FlashPlanError as err:
        return fail(str(err))

    pre = preamble(serial, speed, device)
    load = load_script(pre, bin_path, image)
    console = image.console
    argv = (
        "JLinkExe", "-device", device, "-if", "SWD", "-speed", str(speed),
        "-ExitOnError", "1", "-NoGui", "1", "-CommanderScript",
    )
    selected = "ram" if console is not None else "uart"
    ram_console: dict[str, Any] = {
        "selected": selected,
        "symbol": CONSOLE_SYMBOL if console else None,
        "address": f"0x{console[0]:08X}" if console else None,
        "size": console[1] if console else None,
        "requested": bool(ctx.ram_console),
    }
    report["ramConsole"] = ram_console
    report["ram"] = {
        "elf": elf_path, "binary": bin_path, "loadAddress": f"0x{image.base:08X}",
        "size": image.size, "entry": f"0x{image.entry:08X}",
        "initialSp": f"0x{image.initial_sp:08X}",
        "spNote": "from the vector table; applied by loadbin's reset, not written by tan",
        "wait": ctx.ram_wait, "writesMram": False,
    }
    report["plan"] = {
        "argv": list(argv),
        "jlinkScript": (fc._DISABLE_FW_UPDATE + load).splitlines(),
        "consoleReadScript": (
            (fc._DISABLE_FW_UPDATE + read_script(pre, *console)).splitlines()
            if console and ctx.ram_console else None
        ),
    }
    missing_symbol = bool(ctx.ram_console and console is None)
    warn_missing = {"ram_console_missing": True} if missing_symbol else {}
    summary = (
        f"loaded {image.size} B at 0x{image.base:X}, PC=0x{image.entry:X}, "
        f"SP=0x{image.initial_sp:X} (vector table)"
    )

    if ctx.dry_run:
        msg = f"{METHOD}[{entry_id}]: would run -- {summary}; no MRAM write; nothing spawned"
        lines.append(f"  {msg}")
        return 0, entry("ok", 0, msg, **warn_missing), lines

    # ── the wrong-board guard, exactly as for a Flow D write ──
    armed = not dpidr_preflight_unarmed(FLOW_D_METHOD, flash_args, None)
    if ctx.require_dpidr and not armed:
        return fail(
            "ALP_FLASH_REQUIRE_DPIDR=1 is set and flash_args.expect_dpidr / jlink_device are "
            "not both set -- refusing to run with no wrong-board guard"
        )
    if guard is not None:
        refusal = fc._probe_guard_refusal(guard, exe, None, ctx.workspace)
        if refusal is not None:
            return fail(refusal, None, probe_refusal=guard.tripped_code)
        guard.fresh = True
    facts: dict[str, Any] = {}
    refusal = fc._flow_d_preflight(
        FlashInputs(
            artefact=elf_path, flash_args=flash_args, core_id=entry_id, sku=ctx.sku,
            dry_run=False, force_confirm=ctx.force_confirm,
        ),
        ctx.venv_bin, ctx.workspace, probe_guard=guard, facts=facts, jlink_exe=exe,
    )
    if refusal is not None:
        tripped = guard is not None and guard.tripped == refusal
        return fail(refusal, None, probe_refusal=guard.tripped_code if tripped else None)
    report["jlink"].update({"dpidr": facts.get("dpidr"), "dpidrSource": "preflight" if facts else "none"})

    # ── load + go ──
    plan = FlashPlan(argv=argv, ok_message="", jlink_script=load)
    outcome = fc._execute(plan, ctx.capture, ctx.venv_bin, ctx.workspace, guard, jlink_exe=exe)
    transcript = f"{outcome.stdout}\n{outcome.stderr}"
    _save_transcript(ctx, entry_id, report, load, outcome)
    if guard is not None and guard.tripped:
        return fail(guard.tripped, None, probe_refusal=guard.tripped_code)
    problem = None if outcome.success else (fc._capture_tail(outcome) or "J-Link failed")
    problem = problem or check_session(transcript, loadbin=True)
    if problem:
        return fail(f"the load session failed: {problem}")
    if not facts:
        from tan.core.flow_d_report import dpidr_in

        report["jlink"].update({"dpidr": dpidr_in(transcript), "dpidrSource": "write-transcript"})
    unarmed = not armed

    # ── the console ──
    message = f"{METHOD}[{entry_id}]: {summary}; running"
    if ctx.ram_console:
        if console is None:
            message += (
                f"; no {CONSOLE_SYMBOL} symbol -- this build selected the UART console "
                "(read it on the console, e.g. `tan monitor`); nothing to read over SWD"
            )
        else:
            time.sleep(max(ctx.ram_wait, 0.0))
            read = fc._execute(
                FlashPlan(argv=argv, ok_message="", jlink_script=read_script(pre, *console)),
                True, ctx.venv_bin, ctx.workspace, guard, jlink_exe=exe,
            )
            if guard is not None and guard.tripped:
                return fail(guard.tripped, None, probe_refusal=guard.tripped_code)
            bad = None if read.success else (fc._capture_tail(read) or "J-Link failed")
            bad = bad or check_session(f"{read.stdout}\n{read.stderr}", loadbin=False)
            if bad:
                return fail(f"the image is running but the RAM console read failed: {bad}")
            try:
                data = read_back(f"{read.stdout}\n{read.stderr}", console[0], console[1])
            except RamRunError as err:
                return fail(f"the image is running but {err}")
            text = decode_console(data)
            ram_console.update(
                {"bytesRead": len(data), "textBytes": len(text.encode()), "text": text}
            )
            message += f"; read {len(data)} B of {CONSOLE_SYMBOL} ({len(text)} chars)"
    lines.append(f"  ok: {message}")
    return 0, entry("ok", 0, message, preflight_unarmed=unarmed, **warn_missing), lines


def _save_transcript(ctx: Any, entry_id: str, report: dict[str, Any], script: str, outcome: Any) -> None:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", entry_id) or "entry"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = os.path.join(ctx.build_root, "flash-logs", f"{METHOD}-{safe}-{stamp}.log")
    try:
        report["jlink"]["transcriptPath"] = fc._write_transcript(
            path,
            f"# tan flash --ram [{entry_id}] rc={outcome.returncode}\n## script\n"
            f"{fc._DISABLE_FW_UPDATE}{script}\n## stdout\n{outcome.stdout}\n## stderr\n"
            f"{outcome.stderr}\n",
        )
    except OSError as err:
        report["jlink"]["transcriptPath"] = None
        report["jlink"]["transcriptError"] = str(err)
    report["jlink"]["transcriptTail"] = fc.transcript_tail(f"{outcome.stdout}\n{outcome.stderr}")
