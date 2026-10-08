# SPDX-License-Identifier: Apache-2.0
"""Every operator-facing sentence of Flow A's whole-ATOC guard (tan-cli#1267).
Pure. The decisions live in `tan.core.atoc_guard`; these functions only word
them, so a message can be read (and pinned by a test) in one place.

Verdict-taking functions accept any object with the `GuardVerdict` fields
(`status`, `foreign`, `transcript`, `query_status`, `allowed`) -- typed
loosely so this module need not import the one that imports it.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

#: Spelled exactly as alp-sdk's runner `do_add_parser` registers it.
REPLACE_ATOC_FLAG = "--replace-atoc"

#: The Flow D acknowledgement -- named here only so messages can say it does
#: NOT answer this guard. `test_atoc_guard` pins it equal to
#: `flash_plan.ATOC_ACK_FLAG`.
FLOW_D_ACK_FLAG = "--atoc-unqueryable"

#: The runner's own name for a factory-provisioned module's MCUboot ATOC
#: entry (`_FACTORY_MCUBOOT_ATOC_NAME` in alp-sdk's
#: `scripts/west_commands/runners/alif_flash.py`, itself marked TBD there).
#: The runner steers AWAY from `--replace-atoc` when it is among the foreign
#: entries, because deleting it leaves the module unable to boot; tan's
#: message follows it rather than inviting the override.
FACTORY_MCUBOOT_ATOC_NAME = "MCUBOOT-"

#: alp-sdk's documented way to put BOTH M55 cores into one ATOC over the
#: SE-UART (`docs/aen-bench-bringup.md`, "Flow A -- Dual-core deferred-TOC
#: boot", at tan's pin): one burn carrying both entries, HP-master shape.
DUAL_CORE_PATH = (
    "alp-sdk's scripts/bench/aen/flash-run-dualcore.sh <hp-build-dir> <he-build-dir>, "
    "which burns one ATOC carrying both cores' entries (docs/aen-bench-bringup.md, "
    "\"Flow A -- Dual-core deferred-TOC boot\"; it emits the HP-master shape, and an "
    "HE-master ATOC is not scripted)"
)


def not_applicable_message(
    method: str, entry_id: str, runner: str | None, unknown_reason: str | None, alif: str
) -> str:
    head = f"{method}[{entry_id}]: {REPLACE_ATOC_FLAG} was not passed to this entry"
    if method == "alif_mram_jlink":
        why = (
            "it dispatches Flow D (alif_mram_jlink), which has no SE-UART query to "
            f"override. Flow D's whole-ATOC acknowledgement is {FLOW_D_ACK_FLAG}, a "
            f"different flag on purpose; {REPLACE_ATOC_FLAG} does not stand in for it"
        )
    elif method != "zephyr_west_flash":
        why = f"{REPLACE_ATOC_FLAG} applies only to the {alif} west runner"
    elif runner is None:
        why = f"tan could not learn which west runner it uses: {unknown_reason}"
    else:
        why = f"its west runner is {runner}, not {alif}, and only {alif} has the pre-burn ATOC guard"
    return f"{head}: {why}."


def unguarded_message(entry_id: str, source: str, origin: str, replace_atoc: bool) -> str:
    """An `alif_flash` slice whose runner has no guard. Said plainly, so a
    green run is never read as a checked one."""
    passed = (
        f" {REPLACE_ATOC_FLAG} was NOT passed: that runner does not accept it, "
        "and west would reject the whole command."
        if replace_atoc
        else ""
    )
    return (
        f"zephyr_west_flash[{entry_id}]: this write goes through the alif_flash west "
        f"runner, and the runner tan checked -- {source} ({origin}) -- has no pre-burn "
        "ATOC guard (alp-sdk#2262), or could not be read. The write REPLACES the whole "
        "ATOC: any resident entry it does not name (an A32 boot chain, the other core's "
        f"app, a factory MCUboot) is silently delisted, and nothing checks first.{passed} "
        "Build against an alp-sdk whose runner has the guard (tan-cli#1267)."
    )


def no_verdict_after_success(entry_id: str, reason: str, source: str) -> str:
    """`west flash` succeeded on an entry tan treated as guarded, but there is
    no usable verdict: the guard did not run for this write."""
    return (
        f"zephyr_west_flash[{entry_id}]: west flash succeeded, but the alif_flash "
        f"runner's ATOC guard verdict is unusable ({reason}). tan checked {source} and "
        "found the guard there, so west most likely loaded a different alif_flash "
        "runner, one without it. Treat this write as UNGUARDED: it replaced the whole "
        "ATOC, and any resident entry it did not name may now be delisted (tan-cli#1267)."
    )


def ambiguous_replace_message(entry_ids: list[str]) -> str:
    """`--replace-atoc` would reach more than one alif_flash write in one run."""
    names = ", ".join(entry_ids)
    return (
        f"flash: {REPLACE_ATOC_FLAG} would reach {len(entry_ids)} alif_flash writes in "
        f"this run ({names}). Each one REPLACES the whole ATOC with only its own entry, "
        "so the flag given to accept losing what is on the board now would also "
        "override the next write's guard into delisting what the previous write in "
        "this same run had just put there. Nothing was flashed. Narrow the run to one "
        f"entry with --core CORE_ID (or --helper NAME) and pass {REPLACE_ATOC_FLAG} only "
        "there. Flashing several cores this way leaves only the last one listed; to "
        f"put both M55 cores in the ATOC use {DUAL_CORE_PATH} (tan-cli#1267)."
    )


def refusal_message(entry_id: str, verdict: Any, earlier: Mapping[str, str]) -> str:
    """A refused verdict. `earlier` maps each ATOC section an EARLIER entry of
    this run wrote to that entry's id."""
    head = (
        f"zephyr_west_flash[{entry_id}]: the alif_flash runner's pre-burn ATOC guard "
        f"refused this write before anything was burned (status: {verdict.status}; "
        f"transcript: {verdict.transcript})."
    )
    if verdict.status == "refused-unverified":
        return f"{head} {_unverified_body()}"
    own = [name for name in verdict.foreign if name in earlier]
    if own:
        return f"{head} {_sequential_body(own, earlier)}"
    return f"{head} {_foreign_body(verdict.foreign)}"


def _unverified_body() -> str:
    return (
        "It could not verify what is resident in the ATOC over the SE-UART "
        "(maintenance getbanner/gettoc). The write REPLACES every app entry not "
        "in it, so writing blind could silently delist anything already on the "
        "board -- on a factory-provisioned Alp Lab module that includes its "
        f"MCUboot bootloader, which {REPLACE_ATOC_FLAG} would then delete without ever "
        "naming it. Check the SE-UART wiring and SE_UART, read the transcript, "
        f"confirm by hand what is resident, and re-run with {REPLACE_ATOC_FLAG} only "
        "once you know it is safe to lose. If the runner's own message says the "
        "read succeeded but its table format was not recognised, file the "
        f"transcript instead: {REPLACE_ATOC_FLAG} is not the answer there. "
        f"{FLOW_D_ACK_FLAG} does not answer this guard."
    )


def _sequential_body(own: list[str], earlier: Mapping[str, str]) -> str:
    written = ", ".join(f"{name} (written by {earlier[name]})" for name in own)
    return (
        f"The resident entry it would delist is {written}, written earlier in this same "
        "run. This manifest cannot be Flow A-flashed one core after another: every "
        "alif_flash write REPLACES the whole ATOC with only its own entry, so each core "
        f"delists the one flashed before it. {REPLACE_ATOC_FLAG} is NOT the fix here -- "
        "it would delist that entry again. To put both cores in the ATOC use "
        f"{DUAL_CORE_PATH}."
    )


def _foreign_body(foreign: tuple[str, ...]) -> str:
    names = ", ".join(foreign)
    lead = (
        "The write REPLACES the whole ATOC -- it does not merge -- and the board "
        f"also carries: {names}."
    )
    if FACTORY_MCUBOOT_ATOC_NAME in foreign:
        return (
            f"{lead} That includes the factory-provisioned "
            f"{FACTORY_MCUBOOT_ATOC_NAME!r} MCUboot bootloader: burning would delist "
            "it and leave the module unable to boot until MCUboot is "
            "reprovisioned. Load your app without disturbing it instead -- a "
            "J-Link loadbin of your imgtool-signed image straight to slot0 "
            "(alp-sdk docs/aen-provisioning.md section 0.5, Option B). "
            f"{REPLACE_ATOC_FLAG} still overrides this refusal, but it deletes "
            f"{FACTORY_MCUBOOT_ATOC_NAME!r}."
        )
    return (
        f"{lead} Burning would have silently delisted {names}. Capture or restore "
        f"{names} first (alp-sdk docs/aen-provisioning.md), then re-run with "
        f"{REPLACE_ATOC_FLAG} -- or pass it now only if losing them is intended. "
        f"{FLOW_D_ACK_FLAG} does not answer this guard."
    )


def unreadable_note(reason: str) -> str:
    """Appended to a failure (or success) on a guarded runner whose verdict
    tan could not use. Says what is not known, invents nothing."""
    return (
        f" The alif_flash ATOC guard verdict could not be read ({reason}), so tan "
        "cannot say what the guard decided for this write; see west's own output."
    )


def passed_note(verdict: Any, *, failed: bool = False) -> str:
    """The guard's outcome for a verdict that did NOT refuse. On a failed
    write, an override is reported as an override, not as a done deletion."""
    if verdict.status != "replaced":
        return f"ATOC guard: {verdict.status}"
    if verdict.query_status == "unverified":
        return f"ATOC guard: {REPLACE_ATOC_FLAG} overrode an UNVERIFIED read of the resident ATOC"
    names = ", ".join(verdict.foreign)
    if failed:
        return (
            f"ATOC guard: {REPLACE_ATOC_FLAG} overrode {names}; the write then failed, so "
            f"whether {names} is still listed is unknown"
        )
    return f"ATOC guard: {REPLACE_ATOC_FLAG} overrode resident entries, now delisted: {names}"


#: Two host-setup notices west prints whatever the write's outcome
#: (tan-cli#1426), each matched by its WORDING, never by its logger: zephyr's
#: runner loader failing to import a runner module (`runners/__init__.py`,
#: `_import_runner_module`), and a runner saying an optional Python package is
#: missing (alif_flash's `fdt` hint for app-gen-toc, logged from `do_run`).
#: Keying on the `WARNING: runners.<name>:` prefix alone would also take a
#: warning the runner's own ATOC guard might log, and label it unrelated.
_RUNNER_SETUP_NOISE = (
    re.compile(r'^(?:WARNING: )?The module for runner "[^"]+" could not be imported\b'),
    re.compile(r"^WARNING: runners\.[\w.]+: the '[^']+' Python package\b[^.]*\bwas not found\b"),
)


def split_runner_setup_noise(lines: Iterable[str]) -> tuple[list[str], list[str]]:
    """`(kept, noise)`: `lines` with west's runner-loading warnings moved out,
    order preserved on both sides."""
    kept: list[str] = []
    noise: list[str] = []
    for line in lines:
        is_noise = any(p.match(line.strip()) for p in _RUNNER_SETUP_NOISE)
        (noise if is_noise else kept).append(line)
    return kept, noise


def runner_setup_note(entry_id: str, noise: Iterable[str]) -> str:
    """The warning that carries what a refusal's message no longer does."""
    return (
        f"zephyr_west_flash[{entry_id}]: west also printed runner setup warnings, "
        "unrelated to the ATOC guard's refusal (a west runner or a Python package it "
        f"imports is missing from this environment): {' | '.join(n.strip() for n in noise)}"
    )
