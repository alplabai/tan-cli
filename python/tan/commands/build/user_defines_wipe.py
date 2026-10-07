# SPDX-License-Identifier: Apache-2.0
"""Per-slice reaction to a changed user `-D` set (tan-cli#1382).

Zephyr caches SHIELD / SNIPPET and friends (`zephyr_check_cache(... WATCH)`
restores them from `CACHED_*` and refuses a value change short of a pristine
build; sysbuild images keep their own caches), and `-U` fixes none of that. So a
changed `-D` set on an already-configured slice wipes that slice's build dir --
behind the same two structural guards as tan's other wipe (no `-d`/`--build-dir`
override, cwd under `build/`).
"""
from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path

from tan.commands.build.configure_inputs import read_user_defines_stamp, write_user_defines_stamp
from tan.commands.build.manifest import cmake_cache_configured
from tan.core.user_defines import changed_defines
from tan.envelope import Issue

#: A non-empty cached SHIELD / SNIPPET: set by SOME earlier configure.
_CACHED_SELECTION = re.compile(r"^CACHED_(?:SHIELD|SNIPPET)(?::\w+)?=.+$", re.MULTILINE)


def _cache_holds_selection(cwd: Path) -> bool:
    try:
        text = (cwd / "build" / "CMakeCache.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _CACHED_SELECTION.search(text) is not None


def _unsafe_wipe_reason(cwd: Path, build_root: Path) -> str | None:
    """Why `cwd/build` must not be removed, or None when it is safe: the slice
    dir must resolve strictly inside `<build_root>/build`, and neither it nor
    its `build` may be a symlink (rmtree would follow or refuse)."""
    try:
        root = (build_root / "build").resolve()
        resolved = cwd.resolve()
    except OSError as err:
        return f"could not resolve the build dir ({err.strerror or type(err).__name__})"
    if resolved == root or not resolved.is_relative_to(root):
        return f"build dir resolves outside {build_root / 'build'}"
    if cwd.is_symlink() or (cwd / "build").is_symlink():
        return "the build dir is a symlink; not wiping"
    return None


def reconcile_user_defines(
    core_id: str,
    cwd: Path,
    now: list[str],
    *,
    build_root: Path,
    guards_ok: bool,
    on_output: Callable[[str], None],
) -> tuple[list[Issue], bool]:
    """Wipe `cwd/build` when the user `-D` set differs from the stamp, and
    return `(issues, stampable)`. `stampable` is False only when a change could
    not be applied (a guard suppressed the wipe, or the wipe failed): the stamp
    is then left alone so the next build still sees the change.

    An UNSTAMPED configured dir (built before this stamp existed) is unknown,
    not "no -D": it is wiped once only if its cache holds a SHIELD/SNIPPET or
    the build now passes `-D`."""
    if not cmake_cache_configured(cwd):
        return [], True
    before = read_user_defines_stamp(cwd)
    changed = changed_defines(before or [], now)
    if before is None and not changed and _cache_holds_selection(cwd):
        changed = ["SHIELD/SNIPPET (cached by an earlier build)"]
    if not changed:
        return [], True
    names = ", ".join(changed)
    if not guards_ok:
        message = (
            f"{core_id}: user -D changed ({names}) but this slice's build dir is "
            "redirected (`-d`/`--build-dir`, or a cwd outside `build/`), so tan did "
            "not wipe it: the cache may still hold stale values -- use a fresh build dir"
        )
        on_output(f"note: {message}")
        return [Issue("build.configure-cache-stale", "warning", message)], False
    refusal = _unsafe_wipe_reason(cwd, build_root)
    if refusal is not None:
        message = (
            f"{core_id}: user -D changed ({names}) but tan did not wipe the build dir: "
            f"{refusal}. The cache may still hold stale values -- use a fresh build dir"
        )
        on_output(f"note: {message}")
        return [Issue("build.configure-cache-stale", "warning", message)], False
    try:
        shutil.rmtree(cwd / "build")
    except OSError as err:
        detail = err.strerror or str(err.args[0] if err.args else type(err).__name__)
        message = (
            f"{core_id}: user -D changed ({names}) but the build dir could not be wiped: {detail}"
        )
        on_output(f"note: {message}")
        return [Issue("build.configure-cache-stale", "warning", message)], False
    message = (
        f"{core_id}: user -D changed ({names}) -- wiped this slice's build dir so Zephyr "
        "re-configures from scratch instead of keeping the cached value (tan-cli#1382)"
    )
    on_output(f"note: {message}")
    return [Issue("build.configure-cache-reset", "info", message)], True


def stamp_user_defines(cwd: Path, now: list[str]) -> None:
    """Record what the configure about to run is given (best-effort)."""
    try:
        write_user_defines_stamp(cwd, now)
    except OSError:
        pass
