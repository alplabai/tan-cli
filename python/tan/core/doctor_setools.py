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
from pathlib import Path

from tan.core.flash_plan import FLOW_D_METHOD, FlashTarget, parse_system_manifest, select_flash_method

#: The method `select_flash_method` leaves a Flow A entry on: no `FLOW_D_KEYS`,
#: so `west flash` picks the board's default runner (`alif_flash`, SE-UART).
FLOW_A_METHOD = "zephyr_west_flash"

__all__ = [
    "FLOW_A_METHOD",
    "FLOW_D_METHOD",
    "flash_methods_for_manifest_text",
    "project_flash_methods",
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
