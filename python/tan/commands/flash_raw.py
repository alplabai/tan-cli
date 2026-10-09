# SPDX-License-Identifier: Apache-2.0
"""`tan flash --raw <file>@<addr>` -- a byte-exact MRAM sector write for bench
backup/restore (tan-cli#1446).

A bench backs MRAM up with J-Link `savebin` and must put it back byte-exact:
`he_slot0` at 0x80010000, the ATOC sector at 0x8057C000. Flow D cannot (it writes a
built image plus a signed ATOC), so this is the same J-Link part-profile path with
`loadbin` + `verifybin` and nothing else -- no signing, no SETOOLS, no reset, no `go`.

**Guards, in order:** every `--raw` is parsed and its range validated BEFORE anything is
spawned (`tan.core.raw_write`: explicit hex address, 16 KiB sector alignment, whole
sectors, inside the SKU's MRAM, no overlap), under `--dry-run` too. A real write then
needs `--confirm` and a held bench reservation (`JLINK_RUN_PLACE` set), and goes through
the Flow D probe-selection guard and DPIDR preflight. tan derives no address from a name
or a map, so an ATOC/STOC is only ever written where the user said.
"""
from __future__ import annotations

import os
from typing import Any

from tan.commands import flash_cmd as fc
from tan.core.flash_plan import (
    FLOW_D_METHOD,
    FlashInputs,
    FlashPlan,
    FlashPlanError,
    FlashTarget,
    _DEFAULT_JLINK_SPEED,
    confirm_gate_note,
    dpidr_preflight_unarmed,
    fa_bool_checked,
    fa_int_checked,
    fa_str_checked,
    validate_identifier,
)
from tan.core.flow_d_report import (
    VERIFICATION_READBACK,
    dpidr_in,
    reset_failures,
    sha256_of,
)
from tan.core.jlink_binary import resolve_jlink
from tan.core.raw_write import (
    CODE_FAILED,
    CODE_INVALID,
    CODE_RESERVATION,
    RawError,
    RawSpec,
    parse_raw,
    planned,
    raw_script,
    validate_ranges,
)

METHOD = "mram_raw"
#: The bench's reservation marker: the J-Link wrapper on a shared bench only serves a
#: caller that holds the named labgrid place. tan cannot verify the lease itself, so it
#: refuses a real write when the marker is absent and reports the place it ran under.
RESERVATION_ENV = "JLINK_RUN_PLACE"


def run_raw_entry(target: FlashTarget, ctx: Any) -> tuple[int, Any, list[str]]:
    """Raw-write one slice's MRAM. Returns `(rc, entry, text-lines)` like `_flash_entry`."""
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
        return fail("only a slice's MRAM can be raw-written, not a helper MCU", CODE_INVALID)
    if not isinstance(target.flash_args, dict) and target.flash_args is not None:
        return fail("flash_args is not a mapping", CODE_INVALID)
    flash_args: Any = dict(target.flash_args or {})

    # ── the blobs: parsed, read, hashed and range-checked before anything is spawned ──
    try:
        specs = [parse_raw(raw) for raw in ctx.raw]
        sized: list[RawSpec] = []
        digests: list[str] = []
        for spec in specs:
            try:
                size, digest = os.path.getsize(spec.path), sha256_of(spec.path)
            except OSError as err:
                raise RawError(f"cannot read {spec.path} ({err})") from err
            sized.append(RawSpec(spec.path, spec.address, size))
            digests.append(digest)
        validate_ranges(sized, _mram_bytes(ctx))
        device = fa_str_checked(flash_args, "jlink_flash_device", False)
        if device is None:
            raise RawError(
                "flash_args.jlink_flash_device is required -- only the part-number J-Link device "
                "profile unlocks the MRAM loader; tan does not guess one"
            )
        validate_identifier(device, "jlink_flash_device")
    except (RawError, FlashPlanError) as err:
        return fail(str(err), CODE_INVALID)
    report["raw"] = {"writes": planned(sized, digests), "resets": False, "signs": False}

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
        speed = fa_int_checked(flash_args, "jlink_speed") or _DEFAULT_JLINK_SPEED
        if serial is not None:
            validate_identifier(serial, "jlink_serial", destination=fc._JLINK_SERIAL_DESTINATION)
        fc.validate_flow_d_preflight_args(flash_args)
    except FlashPlanError as err:
        return fail(str(err), CODE_INVALID)

    preamble = "".join(
        [f"SelectEmuBySN {serial}\n" if serial is not None else "",
         f"si SWD\nspeed {speed}\ndevice {device}\nconnect\n"]
    )
    script = raw_script(preamble, sized)
    argv = (
        "JLinkExe", "-device", device, "-if", "SWD", "-speed", str(speed),
        "-ExitOnError", "1", "-NoGui", "1", "-CommanderScript",
    )
    report["plan"] = {"argv": list(argv), "jlinkScript": (fc._DISABLE_FW_UPDATE + script).splitlines()}
    summary = ", ".join(
        f"{w['size']} B at {w['address']} ({w['sectorSpan']['count']} sector(s))" for w in report["raw"]["writes"]
    )

    if ctx.dry_run:
        msg = f"{METHOD}[{entry_id}]: would write {summary}; no reset; nothing spawned"
        lines.append(f"  {msg}")
        return 0, entry("ok", 0, msg), lines

    try:
        confirmed = ctx.force_confirm or bool(fa_bool_checked(flash_args, "confirm"))
    except FlashPlanError as err:
        return fail(str(err), CODE_INVALID)
    if not confirmed:
        msg = (
            f"{METHOD}[{entry_id}]: would write {summary} -- NOT written: "
            f"{confirm_gate_note('--confirm was not given')}"
        )
        lines.append(f"  {msg}")
        return 0, entry("planned", 0, msg), lines

    place = os.environ.get(RESERVATION_ENV, "").strip()
    if not place:
        return fail(
            f"refusing to overwrite MRAM without a held bench reservation: {RESERVATION_ENV} is "
            "not set. Acquire the labgrid place first and run under it so the J-Link wrapper "
            "serves this probe.", CODE_RESERVATION,
        )
    report["jlink"]["reservation"] = {"env": RESERVATION_ENV, "place": place}

    # ── the wrong-board guard, exactly as for a Flow D write ──
    armed = not dpidr_preflight_unarmed(FLOW_D_METHOD, flash_args, None)
    if ctx.require_dpidr and not armed:
        return fail(
            "ALP_FLASH_REQUIRE_DPIDR=1 is set and flash_args.expect_dpidr / jlink_device are "
            "not both set -- refusing to write with no wrong-board guard", CODE_INVALID,
        )
    if guard is not None and armed:
        refusal = fc._probe_guard_refusal(guard, exe, None, ctx.workspace)
        if refusal is not None:
            return fail(refusal, None, probe_refusal=guard.tripped_code)
        guard.fresh = True
    facts: dict[str, Any] = {}
    refusal = fc._flow_d_preflight(
        FlashInputs(
            artefact="", flash_args=flash_args, core_id=entry_id, sku=ctx.sku,
            dry_run=False, force_confirm=ctx.force_confirm,
        ),
        ctx.venv_bin, ctx.workspace, probe_guard=guard, facts=facts, jlink_exe=exe,
    )
    if refusal is not None:
        tripped = guard is not None and guard.tripped == refusal
        return fail(refusal, None, probe_refusal=guard.tripped_code if tripped else None)

    # ── write + verify (J-Link's flash cache), no reset ──
    plan = FlashPlan(argv=argv, ok_message="", jlink_script=script)
    outcome = fc._execute(plan, ctx.capture, ctx.venv_bin, ctx.workspace, guard, jlink_exe=exe)
    transcript = f"{outcome.stdout}\n{outcome.stderr}"
    report["jlink"]["dpidr"] = facts.get("dpidr") or dpidr_in(transcript)
    report["jlink"]["verification"] = fc.VERIFICATION_CACHE
    report["jlink"]["verificationNote"] = fc.VERIFICATION_NOTE
    report["jlink"]["transcriptTail"] = fc.transcript_tail(transcript)
    if guard is not None and guard.tripped:
        return fail(guard.tripped, None, probe_refusal=guard.tripped_code)
    if not outcome.success:
        return fail(f"the write session failed: {fc._capture_tail(outcome) or 'J-Link failed'}")
    if reset_failures(transcript):
        return fail(
            "J-Link reported a halt/reset failure during a write that must not reset: "
            + ", ".join(reset_failures(transcript))
        )

    message = f"{METHOD}[{entry_id}]: wrote {summary}; cache-verified; not reset"
    if ctx.readback:
        writes = [
            {"address": w["address"], "size": w["size"], "path": w["path"]}
            for w in report["raw"]["writes"]
        ]
        failure = fc._flow_d_readback(
            plan, outcome, ctx, writes, report, guard, exe, reset_after=False
        )
        if failure is not None:
            code, text = failure
            return fail(text, code if code.startswith("flash.readback") else None,
                        probe_refusal=code[len("flash.probe-"):] if code.startswith("flash.probe-") else None)
        if report["jlink"].get("verification") == VERIFICATION_READBACK:
            message += "; read back in a fresh J-Link session (sha256 match)"
    message += (
        " -- the board still runs what it booted with; power-cycle for the Secure Enclave to "
        "boot the restored contents"
    )
    lines.append(f"  ok: {message}")
    return 0, entry("ok", 0, message, preflight_unarmed=not armed), lines


def _mram_bytes(ctx: Any) -> int:
    """The SKU's MRAM length from the SoC metadata of the SDK in use (`variants[].mram_mb`,
    else the family default `soc_flash_mb`). Refuses -- never guesses -- when unreadable."""
    from tan.commands.build_output import read_sdk_som_and_soc
    from tan.core.size import resolve_variant

    metadata_root = os.path.join(ctx.sdk_root, "metadata") if ctx.sdk_root else None
    walked = (
        read_sdk_som_and_soc(metadata_root, ctx.sku)
        if metadata_root and ctx.sku and os.path.isdir(metadata_root)
        else None
    )
    if walked is None:
        raise RawError(
            f"cannot bound the write: no readable SoM preset / SoC metadata for '{ctx.sku}' "
            f"under {metadata_root} -- refusing to guess the MRAM size"
        )
    _silicon, silicon_variant, variants, soc_flash_mb, _cores = walked
    variant = resolve_variant(silicon_variant, ctx.sku, variants) or {}
    mb = variant.get("mram_mb")
    if not isinstance(mb, (int, float)) or isinstance(mb, bool) or mb <= 0:
        mb = soc_flash_mb
    if not isinstance(mb, (int, float)) or isinstance(mb, bool) or mb <= 0:
        raise RawError(
            f"cannot bound the write: the SoC metadata for '{ctx.sku}' states no MRAM size "
            "(variants[].mram_mb / soc_flash_mb) -- refusing to guess it"
        )
    return int(mb * 1024 * 1024)
