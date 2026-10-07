# SPDX-License-Identifier: Apache-2.0
"""The `${PYTHON}` a `tan build` bakes into its slices (tan-cli#1317).

Split out of `build_cmd._build`. The resolver itself is `tan.core.host_python`,
shared with `tan doctor`'s `hostPython`; the FLOOR is doctor's own EFFECTIVE
floor (`tan.core.python_floor.effective_python_floor`), never a second constant.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tan.core import host_python
from tan.core.plan_tokens import TOKEN_PYTHON
from tan.core.python_floor import effective_python_floor, read_manifest_floors
from tan.core.sdk_discovery import _planner_python_resolution
from tan.core.venv import west_workspace_dir


@dataclass(frozen=True)
class BuildPython:
    python: str
    #: Why no interpreter qualified (`None` when one did / none was needed).
    refusal: str | None
    #: Cores whose pre-substitution command/env uses `${PYTHON}`.
    users: frozenset[str]


def _effective_floor(build_root: str, sdk_root: str | None) -> tuple[int, int]:
    """Doctor's EFFECTIVE floor, via the shared pure `tan.core.python_floor`
    (same inputs `tan doctor` feeds it: the resolved workspace's `zephyr/`,
    else `$ZEPHYR_BASE`; the SDK manifest's two floors)."""
    ws = west_workspace_dir(build_root, Path(sdk_root) if sdk_root else None)
    zephyr_base = str(ws / "zephyr") if ws is not None else os.environ.get("ZEPHYR_BASE")
    manifest_floor, zephyr_manifest_floor = read_manifest_floors(sdk_root)
    return effective_python_floor(manifest_floor, zephyr_base, zephyr_manifest_floor)[0]


def resolve_build_python(plan, plan_text: str, build_root: str, sdk_root: str | None) -> BuildPython:
    fallback, used_venv = _planner_python_resolution(build_root, sdk_root)
    users = frozenset(
        sl.core_id
        for sl in plan.slices
        if TOKEN_PYTHON in f"{sl.command!r}{sl.env!r}{sl.env_append_path!r}"
    )
    needs = TOKEN_PYTHON in plan_text and not used_venv
    floor = _effective_floor(build_root, sdk_root) if needs else (0, 0)
    python, refusal = host_python.build_interpreter(
        fallback if used_venv else None, fallback, needs, floor
    )
    return BuildPython(python, refusal, users)


def refusal_message(refusal: str) -> str:
    return (
        refusal + " Install it and put it first on PATH, or run `tan bootstrap` "
        "to create a workspace venv; `tan doctor` reports the same interpreter."
    )
