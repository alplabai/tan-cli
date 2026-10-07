# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1350: switching board.yaml `diagnostics.link: itcm` back to `auto`
in an already-configured build dir must not keep linking at the ITCM.

Zephyr resolves `EXTRA_DTC_OVERLAY_FILE` with `zephyr_get(... CACHE ...)`
(`cmake/modules/configuration_files.cmake`): a `-DEXTRA_DTC_OVERLAY_FILE=...`
from an earlier configure is stored in `CMakeCache.txt` and read back on every
later configure that does not pass the variable itself. With the knob off the
plan carries no `-DEXTRA_DTC_OVERLAY_FILE`, so the cached
`<build>/alp-link-itcm.overlay` is still applied -- and the overlay file itself
is still on disk from the earlier materialise -- and the image silently links
at 0x0 with `ok: true`. (`EXTRA_CONF_FILE` is not affected: every plan re-passes
it.)

[`stale_itcm_overlay_reset`] is the fix, run before each Zephyr configure:
when the slice's command does NOT carry the ITCM overlay, delete any leftover
`alp-link-itcm.*` artefact and, if the cache still names the overlay, return
`-UEXTRA_DTC_OVERLAY_FILE`. The WHOLE `EXTRA_DTC_OVERLAY_FILE` cache entry is
unset when our overlay appears anywhere in it -- harmless, because tan never
passes any other `EXTRA_DTC_OVERLAY_FILE`; an entry that never names our
overlay is left alone.
"""

from __future__ import annotations

from pathlib import Path

from tan.envelope import Issue

_OVERLAY = "alp-link-itcm.overlay"
_ARTEFACT_GLOB = "alp-link-itcm.*"


def stale_itcm_overlay_reset(cwd: Path, args: list[str]) -> tuple[list[str], list[Issue]]:
    """`(extra_cmake_args, issues)` -- empty when nothing is stale or when the
    slice IS itcm-targeted (its command names the overlay itself)."""
    if any(_OVERLAY in a for a in args):
        return [], []
    removed: list[str] = []
    for stale in sorted(Path(cwd).glob(_ARTEFACT_GLOB)):
        try:
            stale.unlink()
            removed.append(stale.name)
        except OSError:
            pass  # best-effort; the -U below is what protects the link
    try:
        cache = (Path(cwd) / "build" / "CMakeCache.txt").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        cache = ""
    cached = any(
        line.startswith("EXTRA_DTC_OVERLAY_FILE") and _OVERLAY in line
        for line in cache.splitlines()
    )
    if not cached:
        return [], []
    note = (
        "this slice is no longer linked for the ITCM (`diagnostics.link` is "
        f"not `itcm`) but its cached configure still names {_OVERLAY}"
        + (f"; removed stale {', '.join(removed)}" if removed else "")
        + " -- unsetting EXTRA_DTC_OVERLAY_FILE so it links for MRAM again"
    )
    return ["-UEXTRA_DTC_OVERLAY_FILE"], [Issue("build.configure-cache-reset", "info", note)]


def insert_after_separator(args: list[str], extra: list[str]) -> list[str]:
    """`args` with `extra` placed immediately after the first `--` (before any
    `-D`), or appended when there is no `--`. A `-U` placed before a `-D` of the
    same cache key is applied first, so the later `-D` always survives."""
    if not extra:
        return list(args)
    out = list(args)
    try:
        at = out.index("--") + 1
    except ValueError:
        at = len(out)
    out[at:at] = extra
    return out
