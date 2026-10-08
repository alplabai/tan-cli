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

import contextlib
import os
import re
import shutil
import tempfile
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
    commander_path,
    confirm_gate_note,
    dpidr_preflight_unarmed,
    fa_bool_checked,
    fa_int_checked,
    fa_str,
    fa_str_checked,
    validate_commander_path,
    validate_identifier,
)
from tan.core.jlink_binary import resolve_jlink
from tan.core.ram_run import (
    CODE_CONSOLE_SYMBOL_MISSING,
    CODE_CORE_MISMATCH,
    CODE_CORE_UNCONFIRMED,
    OVERRIDABLE_VERDICTS,
    trouble_markers,
    ap_verdict,
    core_check,
    core_check_script,
    apertures_for,
    attached_core,
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
    with contextlib.ExitStack() as stack:
        return _run_ram_entry(target, ctx, stack)


def _stage_image(stack: contextlib.ExitStack, data: bytes) -> str:
    """Write the image to a TAN-OWNED temp file with a fixed safe name and return its
    path -- the ONLY path that is ever interpolated into the Commander `loadbin`
    line, so a project-controlled artefact path (newline, quote, `;`) can never reach
    the script. Removed when the entry ends."""
    directory = tempfile.mkdtemp(prefix="tan-ram-")
    stack.callback(shutil.rmtree, directory, True)
    path = os.path.join(directory, "image.bin")
    with open(path, "wb") as fh:
        fh.write(data)
    validate_commander_path(path, "the staged RAM image path")
    return path


def _run_ram_entry(
    target: FlashTarget, ctx: Any, stack: contextlib.ExitStack
) -> tuple[int, Any, list[str]]:
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
        # Refused before anything is read or spawned: these strings appear in
        # messages and the transcript header, and a control character in a path is
        # never legitimate. (The script itself only ever sees the staged copy.)
        validate_commander_path(elf_path, "the ELF path")
        validate_commander_path(bin_path, "the binary path")
        validate_identifier(entry_id, "the flash target id")
    except FlashPlanError as err:
        return fail(str(err))
    try:
        elf_bytes, bin_bytes = _read(elf_path), _read(bin_path)
    except OSError as err:
        return fail(f"cannot read the built image ({err}); expected {elf_path} and {bin_path}")
    try:
        apertures = _load_apertures(ctx, entry_id)
        image = plan_ram_image(
            parse_elf(elf_bytes), bin_bytes, core_id=entry_id, apertures=apertures
        )
    except RamRunError as err:
        return fail(str(err), err.code)

    # ── J-Link: trusted binary, probe selection, parameters ──
    found = resolve_jlink(ctx.jlink_path, project_dir=ctx.project_dir)
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
        if not device.startswith("Cortex-"):
            # The part-number FLASH profile (`jlink_flash_device`) unlocks the MRAM
            # loader and will not re-halt a running core; the bench's JLINK_DEVICE_READ
            # contract is the generic core profile for every RAM-run attach.
            raise FlashPlanError(
                f"jlink_device '{device}' is a part-number profile, not a generic core "
                f"profile; a RAM-run attaches with '{_DEFAULT_ATTACH_DEVICE}' (the part "
                "profile unlocks the MRAM loader and cannot re-halt a live core)"
            )
        speed = fa_int_checked(flash_args, "jlink_speed") or _DEFAULT_JLINK_SPEED
        if serial is not None:
            validate_identifier(serial, "jlink_serial", destination=fc._JLINK_SERIAL_DESTINATION)
        fc.validate_flow_d_preflight_args(flash_args)
    except FlashPlanError as err:
        return fail(str(err))

    try:
        staged = _stage_image(stack, bin_bytes)
        pre = preamble(serial, speed, device)
    except (OSError, FlashPlanError, RamRunError) as err:
        return fail(f"cannot prepare the RAM-run ({err})")
    # `image.base`/`entry` and the console address/size are ints rendered with
    # `0x%X` by `ram_run`; the symbol NAME never reaches a script at all.
    load = load_script(pre, commander_path(staged), image)
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
        "elf": elf_path, "binary": bin_path, "stagedImage": staged, "loadAddress": f"0x{image.base:08X}",
        "size": image.size, "entry": f"0x{image.entry:08X}",
        "initialSp": f"0x{image.initial_sp:08X}",
        "spNote": "from the vector table; applied by loadbin's reset, not written by tan",
        "wait": ctx.ram_wait, "writesMram": False,
    }
    report["plan"] = {
        "argv": list(argv),
        "jlinkScript": (fc._DISABLE_FW_UPDATE + load).splitlines(),
        "coreCheckScript": (fc._DISABLE_FW_UPDATE + core_check_script(pre)).splitlines(),
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

    # ── the confirm gate (bench round 7) ──
    # `loadbin` resets the core through AIRCR.SYSRESETREQ, a full-device reset that also
    # resets the Secure Enclave, and the run REPLACES the running image: it is gated like
    # every other write. Without --confirm (or ALP_FLASH_FORCE=1 / flash_args.confirm) it
    # previews and exits non-zero, exactly as an unconfirmed flash does.
    try:
        confirmed = ctx.force_confirm or bool(fa_bool_checked(flash_args, "confirm"))
    except FlashPlanError as err:
        return fail(str(err))
    if not confirmed:
        msg = (
            f"{METHOD}[{entry_id}]: would run -- {summary} -- NOT run: "
            f"{confirm_gate_note('--confirm was not given')}. --ram resets the whole device "
            "(AIRCR.SYSRESETREQ, which resets the Secure Enclave) and replaces the running image."
        )
        lines.append(f"  {msg}")
        return 0, entry("planned", 0, msg, **warn_missing), lines

    # ── the wrong-board guard, exactly as for a Flow D write ──
    armed = not dpidr_preflight_unarmed(FLOW_D_METHOD, flash_args, None)
    if ctx.require_dpidr and not armed:
        return fail(
            "ALP_FLASH_REQUIRE_DPIDR=1 is set and flash_args.expect_dpidr / jlink_device are "
            "not both set -- refusing to run with no wrong-board guard"
        )
    if guard is not None and armed:
        # Verified once up front; the armed DPIDR preflight reuses it (as in Flow D) and
        # the load session re-verifies immediately before ITS spawn.
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
    report["jlink"]["attachedCore"] = None

    # ── which core did the generic attach land on? (tan-cli#1354) ──
    # DECISIVE: the AP that reports `Core found` (HE APAddr 0x00300000). CORROBORATION: the
    # local ITCM at 0x0 against the HE window 0x58000000 -- two read-only `mem32`s, no halt,
    # no write, and the HP window is never read. Only an HE access port proceeds; HP
    # evidence refuses and --assume-he can never override it.
    check = fc._execute(
        FlashPlan(argv=argv, ok_message="", jlink_script=core_check_script(pre)),
        True, ctx.venv_bin, ctx.workspace, guard, jlink_exe=exe,
    )
    if guard is not None and guard.tripped:
        return fail(guard.tripped, None, probe_refusal=guard.tripped_code)
    check_text = f"{check.stdout}\n{check.stderr}"
    bad = None if check.success else (fc._capture_tail(check) or "J-Link failed")
    bad = bad or check_session(check_text, loadbin=False)
    if bad:
        verdict, evidence = "unreadable", {}
    else:
        verdict, evidence = core_check(check_text)
    report["ram"]["coreCheck"] = {
        "verdict": verdict, **evidence, "assumeHe": bool(ctx.assume_he),
        "basis": "DECISIVE: the AP that reports `Core found` (HE APAddr 0x00300000, HP "
        "0x00200000); CORROBORATION: local ITCM 0x0 compared with the HE global window "
        "0x58000000 (the HP window is never read)",
        **({"sessionError": bad} if bad else {}),
    }
    report["jlink"]["attachedCore"] = evidence.get("ap")
    # Halt/reset trouble the CHECK session itself reported (it halts nothing, but a
    # core that is already unhaltable says so on connect): reported against the check.
    check_trouble = trouble_markers(check_text)
    report["ram"]["coreCheck"]["resetFailures"] = list(check_trouble)
    if verdict in ("hp", "conflict-hp"):
        return fail(
            "the probe is attached to the M55-HP core (J-Link's Core-found access port is "
            "the HP's 0x00200000"
            + (", and the local ITCM equals the HE window, which is contradictory" if verdict == "conflict-hp" else "")
            + ") -- refusing to load the HE image into it. Select the HE core's debug port. "
            "--assume-he never overrides HP evidence.", CODE_CORE_MISMATCH,
        )
    if verdict != "he":
        if not (ctx.assume_he and verdict in OVERRIDABLE_VERDICTS):
            return fail(
                f"cannot confirm the attached core is the M55-HE ({verdict}"
                + (f": {bad}" if bad else "")
                + "): "
                + (
                    "the access port says HE but the local ITCM does not equal the HE window "
                    "-- contradictory, and not overridable. "
                    if verdict == "conflict"
                    else "J-Link named no Core-found access port tan can place "
                    "(HE 0x00300000), or the check session could not be read. "
                )
                + "Refusing to load."
                + (
                    " --assume-he overrides this at your own risk (the bench has seen the "
                    "generic attach land on HE 6 of 6 times, which is not proof); it never "
                    "overrides HP evidence or a contradiction."
                    if verdict in OVERRIDABLE_VERDICTS
                    else ""
                ),
                CODE_CORE_UNCONFIRMED,
            )
        report["ram"]["coreCheck"]["assumed"] = True

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
    loaded_on = attached_core(transcript)
    checked_on = report["jlink"].get("attachedCore")
    if not (loaded_on and loaded_on.get("apAddr")):
        # Not silently skipped: say the load banner named no Core-found AP.
        report["jlink"]["attachedCoreAtLoad"] = None
        report["jlink"]["attachedCoreAtLoadNote"] = (
            "the load transcript named no Core-found access port, so the load session's "
            "core was not compared with the core check's"
        )
    if loaded_on and loaded_on.get("apAddr"):
        # The load is a SEPARATE J-Link session: it can attach to a different AP than the
        # check did (a probe that re-enumerated). After the fact, but loud -- the image may
        # now be in the wrong core. Both attaches are reported.
        report["jlink"]["attachedCoreAtLoad"] = loaded_on
        differs = bool(checked_on) and checked_on.get("apAddr") != loaded_on.get("apAddr")
        if differs or ap_verdict(loaded_on) == "hp":
            return fail(
                f"the load session attached to AP {loaded_on.get('apAddr')} "
                f"({ap_verdict(loaded_on)}) but the core check attached to AP "
                f"{(checked_on or {}).get('apAddr')}: the image may have been loaded into the "
                "WRONG core. Power-cycle the board and check both cores before running "
                "anything else.", CODE_CORE_MISMATCH,
            )
    elif checked_on is None and loaded_on:
        report["jlink"]["attachedCore"] = loaded_on
    if not facts:
        from tan.core.flow_d_report import dpidr_in

        report["jlink"].update({"dpidr": dpidr_in(transcript), "dpidrSource": "write-transcript"})
    unarmed = not armed
    # Halt/reset trouble is surfaced like Flow D's reset failures (bench round 8): a load
    # that only worked through J-Link's fallback chain is reported, never reported clean.
    trouble = trouble_markers(transcript)
    report["jlink"]["resetFailures"] = list(dict.fromkeys([*check_trouble, *trouble]))

    # ── the console ──
    message = f"{METHOD}[{entry_id}]: {summary}; running"
    if check_trouble and not trouble:
        message += (
            "; the core check reported halt/reset trouble (" + ", ".join(check_trouble) + ")"
        )
    if trouble:
        message += (
            "; the load only worked through a J-Link fallback (" + ", ".join(trouble) + ") -- "
            "the core did not halt/reset cleanly, so power-cycle before trusting this run"
        )
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
    return (
        0,
        entry("ok", 0, message, preflight_unarmed=unarmed,
              reset_unconfirmed=bool(trouble or check_trouble), **warn_missing),
        lines,
    )


def _load_apertures(ctx: Any, core_id: str) -> Any:
    """The core's code/data apertures from the SoC metadata of the SDK in use
    (`sram_banks_kb` of the manifest SKU's silicon variant). Refuses -- never guesses a
    size -- when the SDK metadata, the SoM preset or the variant's banks cannot be
    read."""
    from tan.commands.build_output import read_sdk_som_and_soc
    from tan.core.size import resolve_variant, sram_banks

    metadata_root = os.path.join(ctx.sdk_root, "metadata") if ctx.sdk_root else None
    why: list[str] = []
    walked = (
        read_sdk_som_and_soc(metadata_root, ctx.sku, skipped=why, explain_unsupported=True)
        if metadata_root and ctx.sku and os.path.isdir(metadata_root)
        else None
    )
    if walked is None:
        raise RamRunError(
            f"cannot bound the image: no readable SoM preset / SoC metadata for "
            f"'{ctx.sku}' under {metadata_root}"
            + (f" ({'; '.join(why)})" if why else "")
            + " -- refusing to guess the TCM sizes"
        )
    _silicon, silicon_variant, variants, _flash_mb, _cores = walked
    variant = resolve_variant(silicon_variant, ctx.sku, variants)
    apertures = apertures_for(core_id, sram_banks(variant)) if variant else None
    if apertures is None:
        raise RamRunError(
            f"cannot bound the image: the SoC variant for '{ctx.sku}' lists no "
            f"ITCM/DTCM bank for core '{core_id}' -- refusing to guess the TCM sizes"
        )
    return apertures


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
