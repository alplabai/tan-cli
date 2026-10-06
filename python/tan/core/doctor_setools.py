# SPDX-License-Identifier: Apache-2.0
"""Method-aware inputs for `tan doctor`'s `setools` check (tan-cli#1323).

The check used to assert, for every host, that "AEN MRAM flashing (`west
flash`, the alif_flash runner) will fail" without `$SE_UART`. That is Flow A
only. A planner-emitted AEN manifest dispatches Flow D (`alif_mram_jlink`):
J-Link over SWD, SETOOLS needed solely to sign the ATOC (`app-gen-toc`), no
SE-UART at all. So the check must know which method `tan flash` would pick.

This module answers that question by calling `tan.core.flash_plan.
select_flash_method` -- the SAME resolver `tan flash` dispatches with -- on
each slice of the project's `build/system-manifest.yaml`, never by
re-deriving the Flow D rule. Pure/IO split: `flash_methods_for_manifest_text`
is pure; `project_flash_methods` does the one read. Neither raises.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tan.core.setools import find_app_gen_toc, resolve_setools_dir

from tan.core.flash_plan import FLOW_D_METHOD, FlashTarget, parse_system_manifest, select_flash_method

#: The method `select_flash_method` leaves a Flow A entry on: no `FLOW_D_KEYS`,
#: so `west flash` picks the board's default runner (`alif_flash`, SE-UART).
FLOW_A_METHOD = "zephyr_west_flash"

__all__ = [
    "FLOW_A_METHOD",
    "FLOW_D_METHOD",
    "flash_methods_for_manifest_text",
    "ProjectFlash",
    "project_flash",
    "project_flash_methods",
    "verdict",
    "signing_problems",
]


def flash_methods_for_manifest_text(text: str) -> frozenset[str] | None:
    """The set of `flash_method`s `tan flash` would dispatch for the slices of
    this `system-manifest.yaml`, or `None` when the text cannot be parsed.

    Slices only: a helper MCU's method is never a SETOOLS path."""
    try:
        manifest = parse_system_manifest(text)
    except Exception:  # noqa: BLE001 -- a doctor probe must not raise
        return None
    methods = set()
    for s in manifest.slices:
        method = select_flash_method(
            FlashTarget(
                kind="slice", id=s.core_id, flash_method=s.flash_method, flash_args=s.flash_args
            )
        )
        if method:
            methods.add(method)
    return frozenset(methods)


def project_flash_methods(board_yaml: str | None) -> frozenset[str] | None:
    """Methods for the project owning `board_yaml`, read from its
    `build/system-manifest.yaml`. `None` when there is no project, no built
    manifest yet, or it is unreadable/invalid -- the caller then phrases the
    verdict per method instead of guessing one."""
    if board_yaml is None:
        return None
    try:
        text = (Path(board_yaml).parent / "build" / "system-manifest.yaml").read_text(
            encoding="utf-8"
        )
    except (OSError, ValueError):
        return None
    return flash_methods_for_manifest_text(text)


def signing_problems(
    setools_dir: str | None,
    executables: tuple[str, ...],
    find_app_gen_toc,
    require_exec: bool = False,
) -> list[str]:
    """What stops SETOOLS from signing/writing: `$SETOOLS_DIR` unset, or a
    directory lacking any of `executables`. `app-gen-toc` is located through
    `find_app_gen_toc` (the `.exe`-aware lookup `tan flash` uses); with
    `require_exec` it must also be executable on POSIX (Flow D spawns it)."""
    if not setools_dir:
        return [
            "$SETOOLS_DIR is unset (the Alif Security Toolkit is license-gated and "
            "NOT redistributed by alp-sdk)"
        ]
    absent: list[str] = []
    for exe in executables:
        try:
            if exe == "app-gen-toc":
                found = find_app_gen_toc(setools_dir)
                ok = found is not None and (
                    not require_exec or os.name == "nt" or os.access(found, os.X_OK)
                )
            else:
                ok = (Path(setools_dir) / exe).is_file()
        except OSError:
            ok = False
        if not ok:
            absent.append(exe)
    if absent:
        return [
            f"$SETOOLS_DIR=`{setools_dir}` does not look like an "
            f"app-release-exec-linux directory (no {', '.join(absent)}{' executable' if require_exec else ''})"
        ]
    return []


#: Said wherever doctor reports a SETOOLS verdict: doctor cannot see per-run
#: arguments, so a flag given only to `tan flash` is outside its view.
FLAG_NOTE = (
    "doctor reads $SETOOLS_DIR and the built manifest's flash_args.setools_dir; "
    "a --setools-dir flag given to `tan flash` is not visible to it"
)


@dataclass(frozen=True)
class ProjectFlash:
    """What the project's built manifest says about flashing: the dispatched
    `methods` and the first `flash_args.setools_dir` a slice carries."""

    methods: frozenset[str]
    setools_dir: str | None = None


def project_flash(board_yaml: str | None) -> ProjectFlash | None:
    """[`project_flash_methods`] plus the manifest's `setools_dir`, resolved
    with `tan.core.setools.resolve_setools_dir` (empty env, no flag) so it
    reads the key exactly as `tan flash` does.

    **Assumption:** the manifest lives at `<board.yaml dir>/build/
    system-manifest.yaml`, the default `tan flash`/`tan build` use. A custom
    `--build-root` is invisible here, and doctor then falls back to per-method
    wording. `None` when no project or no readable manifest."""
    methods = project_flash_methods(board_yaml)
    if methods is None or board_yaml is None:
        return None
    setools_dir = None
    try:
        text = (Path(board_yaml).parent / "build" / "system-manifest.yaml").read_text(
            encoding="utf-8"
        )
        for s in parse_system_manifest(text).slices:
            found = resolve_setools_dir(s.flash_args, {}, None)
            if found is not None:
                setools_dir = found.path
                break
    except Exception:  # noqa: BLE001 -- a doctor probe must not raise
        setools_dir = None
    return ProjectFlash(methods, setools_dir)


def verdict(
    setools_dir: str | None,
    setools_source: str,
    se_uart: str | None,
    flash_methods: frozenset[str] | None,
    jlink_found: bool | None,
    bundle: str,
    executables: tuple[str, ...],
) -> tuple[str, str, str | None]:
    """`(status, detail, fix)` for the `setools` check, per flash method."""
    flow_d = flash_methods is not None and FLOW_D_METHOD in flash_methods
    flow_a = flash_methods is not None and FLOW_A_METHOD in flash_methods
    only_gen_toc = not flow_a
    signing = signing_problems(
        setools_dir,
        ("app-gen-toc",) if only_gen_toc else executables,
        find_app_gen_toc,
        require_exec=flow_d,
    )
    d_problems = list(signing)
    if flow_d and jlink_found is False:
        d_problems.append("no J-Link tool (JLinkExe/JLink) on PATH or in the workspace venv")
    a_problems = list(signing)
    if flow_a and not se_uart:
        a_problems.append(
            "$SE_UART is unset (the SE-UART device: Linux /dev/ttyUSB*, macOS "
            "/dev/cu.usbserial-*, a passed-through COM under WSL)"
        )
    if flash_methods is None:
        problems = signing
        lead = (
            "AEN MRAM flashing (Flow D `alif_mram_jlink`, the planner default, and "
            "Flow A `west flash`) cannot sign an ATOC: "
        )
        joined = "; ".join(problems)
    else:
        problems = []
        if flow_d and d_problems:
            problems.append("Flow D (`alif_mram_jlink`): " + "; ".join(d_problems))
        if flow_a and a_problems:
            problems.append("Flow A (`west flash`, the alif_flash runner): " + "; ".join(a_problems))
        lead = "AEN MRAM flashing will fail: "
        joined = " | ".join(problems)
    if not problems:
        tools = "app-gen-toc" if only_gen_toc else "/".join(executables)
        ready = f"SETOOLS ready: SETOOLS dir `{setools_dir}` (from {setools_source}) has {tools}"
        if flash_methods is None:
            ready += (
                ". Flow D (`alif_mram_jlink`) needs only this plus a J-Link (see the "
                "`jlink` check); Flow A (`west flash`) also needs $SE_UART "
                + (f"(`{se_uart}`, set)." if se_uart else "(currently unset).")
            )
        elif flow_a:
            ready += f", $SE_UART=`{se_uart}`."
        else:
            ready += " and a J-Link is available (Flow D needs no SE-UART)."
        return "pass", ready + " (" + FLAG_NOTE + ".)", None
    fix = (
        f"Download the Alif Security Toolkit (`{bundle}`) from the Alif "
        "developer portal -- it is license-gated and alp-sdk does not "
        "redistribute it -- then `export SETOOLS_DIR=<...>/app-release-exec-linux`. "
        "Flow A (`west flash`) additionally needs `export SE_UART=/dev/ttyUSB0` "
        "(your SE-UART device); Flow D does not. See docs/aen-bench-bringup.md. "
        + FLAG_NOTE[0].upper()
        + FLAG_NOTE[1:]
        + "."
    )
    return "warn", lead + joined + ".", fix
