# SPDX-License-Identifier: Apache-2.0
"""`python -m tan.planner_cli` -- the planner's argv entry point.

RELOCATED from alp-sdk `scripts/alp_orchestrate/__main__.py`, and deliberately
NOT `tan/planner/__main__.py`: `python -m <pkg>` makes runpy import the PACKAGE
before it looks for `__main__`, so `tan/planner/__init__.py` -> `paths.py` ->
`sdk_root()` would run and raise before any code here could bind the root. A
sibling module is imported on its own and so gets to bind first. (The upstream
docstring's point still holds in its own way: nothing imports this module, so
nothing re-enters the package under runpy.)

`--sdk-root` wins over `ALP_SDK_ROOT`; with neither, the walk up from the CWD
covers the common case of running inside a checkout. The marker is
`scripts/alp_project.py`, the same one `tan`'s own SDK discovery uses. The scan
is hand-rolled rather than argparse because it has to run before the package
import, and therefore before `cli.py`'s parser exists.

This is a developer/parity entry, not the customer surface: `tan build` reaches
the same dispatch in-process via `tan.planner_root.emit`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from tan.planner_root import PlannerRootError, bind_sdk_root

_MARKER = ("scripts", "alp_project.py")


def _sdk_root_from_argv(argv: list[str]) -> str | None:
    for i, arg in enumerate(argv):
        if arg == "--sdk-root" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--sdk-root="):
            return arg.split("=", 1)[1]
    return None


def _strip_sdk_root(argv: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg == "--sdk-root":
            skip = True
            continue
        if arg.startswith("--sdk-root="):
            continue
        out.append(arg)
    return out


def _discover() -> Path | None:
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if candidate.joinpath(*_MARKER).is_file():
            return candidate
    return None


def _bootstrap(argv: list[str]) -> int:
    root = _sdk_root_from_argv(argv) or os.environ.get("ALP_SDK_ROOT")
    resolved = Path(root) if root else _discover()
    if resolved is None:
        print("tan-planner: no alp-sdk checkout found -- pass --sdk-root "
              "<PATH>, set ALP_SDK_ROOT, or run from inside a checkout.",
              file=sys.stderr)
        return 1
    try:
        bind_sdk_root(resolved)
    except PlannerRootError as e:
        print(f"tan-planner: {e}", file=sys.stderr)
        return 1
    from tan.planner.cli import main
    return main(_strip_sdk_root(argv))


if __name__ == "__main__":
    raise SystemExit(_bootstrap(sys.argv[1:]))
