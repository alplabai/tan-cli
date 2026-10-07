# SPDX-License-Identifier: Apache-2.0
"""User `-D NAME=VALUE` on a PLANNED build (tan-cli#1382).

A planned (`board.yaml`) build's Zephyr slice carries argv the planner emitted
(`west build -b <board> <app> -- -DPython3_EXECUTABLE=... -DEXTRA_CONF_FILE=...`).
alp-sdk examples document extra CMake definitions on top of that
(`-DSHIELD=...`, `-DCONFIG_...=...`), so `tan build -D` splices the user's
definitions into the plan BEFORE token substitution and dispatch:

* user `-D` goes AFTER the plan-emitted args, so CMake's last-wins lets it
  override anything the plan set -- except the keys tan owns for correctness;
* `EXTRA_CONF_FILE` / `EXTRA_DTC_OVERLAY_FILE` (and a sysbuild `<image>_`
  prefixed form) are lists tan already feeds (the per-core `alp.conf`), so the
  user's value is APPENDED `;`-joined to the plan's, never a replacement;
* `BOARD` / `Python3_EXECUTABLE` are refused -- tan derives both.

Pure data-shaping: nothing here resolves or spawns anything.
"""
from __future__ import annotations

import json
import re

#: Keys tan owns outright; a user value would desynchronise the build.
RESERVED_KEYS = frozenset({"BOARD", "Python3_EXECUTABLE"})
#: Keys whose user value joins tan's list (`;`, a CMake list).
_APPEND_KEY = re.compile(r"(?:[A-Za-z0-9_]+_)?EXTRA_(?:CONF|DTC_OVERLAY)_FILE")
_NAME = re.compile(r"-D([A-Za-z_][A-Za-z0-9_]*)(?::[A-Za-z]+)?(=.*)?", re.DOTALL)


class UserDefineError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _split(define: str) -> tuple[str, str | None]:
    m = _NAME.fullmatch(define)
    assert m is not None  # already validated by `normalise_defines`
    return m.group(1), (m.group(2)[1:] if m.group(2) is not None else None)


def user_defines_problem(defines: list[str]) -> tuple[str, str] | None:
    """`(code, message)` for a normalised `-D` list tan must refuse, else None."""
    for d in defines:
        name, value = _split(d)
        if name in RESERVED_KEYS:
            return (
                "build.define-reserved",
                f"`-D {name}` cannot be overridden: tan derives `{name}` itself "
                "(the board comes from the plan / `--board`, Python from the resolved host Python).",
            )
        if _APPEND_KEY.fullmatch(name) and not value:
            return (
                "build.invalid-argument",
                f"`-D {name}` needs a value: it is appended to tan's own `{name}` list.",
            )
    return None


def _apply_to_args(args: list[str], defines: list[str]) -> list[str]:
    out = list(args)
    if "--" not in out:
        out.append("--")
    for d in defines:
        name, value = _split(d)
        if _APPEND_KEY.fullmatch(name):
            idx = next(
                (
                    i
                    for i in range(len(out) - 1, out.index("--"), -1)
                    if (m := _NAME.fullmatch(out[i])) is not None
                    and m.group(1) == name
                    and m.group(2) is not None
                ),
                None,
            )
            if idx is not None:
                out[idx] = f"{out[idx]};{value}"
                continue
        out.append(d)
    return out


def apply_user_defines(
    plan_text: str, defines: list[str], cores: list[str] | None = None
) -> tuple[str, list[str]]:
    """The plan text with `defines` spliced into the targeted Zephyr slices'
    argv, plus the coreIds changed. Targets: every Zephyr slice that has a
    command, or only `cores` when given. Raises [`UserDefineError`] when `cores`
    names a slice that is not such a target, or nothing is targeted."""
    plan = json.loads(plan_text)
    targets = [
        s
        for s in plan.get("slices", [])
        if s.get("backend") == "zephyr" and isinstance(s.get("command"), dict)
    ]
    if cores:
        known = {s["coreId"] for s in targets}
        missing = [c for c in cores if c not in known]
        if missing:
            raise UserDefineError(
                "build.invalid-argument",
                f"`--core {', '.join(missing)}` is not a Zephyr slice of this plan "
                f"(Zephyr slices: {', '.join(sorted(known)) or 'none'}).",
            )
        targets = [s for s in targets if s["coreId"] in cores]
    if not targets:
        raise UserDefineError(
            "build.define-no-target",
            "`-D` has no Zephyr slice to apply to: this plan has none with a build command.",
        )
    for s in targets:
        s["command"]["args"] = _apply_to_args(s["command"]["args"], defines)
    return json.dumps(plan, indent=2), [s["coreId"] for s in targets]
