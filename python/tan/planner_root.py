# SPDX-License-Identifier: Apache-2.0
"""Where the relocated planner finds the SDK -- bound once, before import.

`tan.planner` is alp-sdk's `scripts/alp_orchestrate/` relocated verbatim. In
alp-sdk its `paths.py` derived the repo root by walking up from its own
`__file__`; inside `tan` that walk lands on the `tan` package and every
`metadata/**` path would be wrong. So the root becomes an explicit binding.

**Binding must happen before `tan.planner` is imported.** `paths.py` evaluates
`REPO = sdk_root()` at module scope, and half the package takes
`metadata_root: Path = METADATA_ROOT` as a *default argument* -- both freeze at
import time. An unbound import therefore raises `PlannerRootError` loudly
rather than silently resolving `metadata/**` against the wrong tree; that is
the same refuse-don't-degrade posture the executor's token substitution takes
(`tan/commands/build/token_substitution.py`).

`metadata/**` stays in alp-sdk (ADR-0017): what relocated is the generators,
never the facts. The fact READERS stay too -- `alp_project`,
`alp_project_loader`, `alp_project_emit`, `alp_registries` and
`alp_cli.validator` remain alp-sdk modules (`scripts/alp_project.py` is also
tan's canonical SDK-root marker and cannot move), so binding additionally puts
`<sdk_root>/scripts` on `sys.path` -- the in-process equivalent of the
`PYTHONPATH=<sdk>/scripts` the subprocess path exported.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["PlannerRootError", "bind_sdk_root", "sdk_root", "emit"]


class PlannerRootError(RuntimeError):
    """The planner's SDK root is unbound, or a rebind came too late."""


_BOUND: Path | None = None


def bind_sdk_root(root: Path | str) -> Path:
    """Point the relocated planner at an alp-sdk checkout. Idempotent.

    Rebinding to a *different* root once `tan.planner` is imported is an error,
    not a no-op: the already-frozen module constants and default arguments would
    keep reading the first root's `metadata/**` while callers believed the
    second -- a silent wrong-SDK build, which is precisely the split-brain the
    plan's `sdkCommit` guard exists to catch.
    """
    global _BOUND
    resolved = Path(root).resolve()
    if _BOUND is not None and _BOUND != resolved:
        if "tan.planner" in sys.modules:
            raise PlannerRootError(
                f"the planner is already bound to `{_BOUND}` and imported; "
                f"cannot rebind to `{resolved}` in the same process.")
        _drop_from_path(_BOUND / "scripts")
    _BOUND = resolved
    scripts = str(resolved / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return resolved


def sdk_root() -> Path:
    """The bound alp-sdk checkout root. Raises if unbound."""
    if _BOUND is None:
        raise PlannerRootError(
            "tan.planner was imported before `bind_sdk_root(<alp-sdk root>)`; "
            "its metadata paths freeze at import time, so an unbound import "
            "cannot be repaired -- bind first, then import.")
    return _BOUND


def _drop_from_path(scripts: Path) -> None:
    text = str(scripts)
    while text in sys.path:
        sys.path.remove(text)


def emit(mode: str, *, root: Path | str, board_yaml: Path,
         build_root: Path = Path("build"), core: str | None = None) -> str:
    """Bind, load `board_yaml`, and return one emit artefact as text.

    The in-process twin of `python -m tan.planner --emit <mode>`; both route
    through the same `tan.planner.cli.emit_artefact` dispatch so a mode can
    never render differently depending on how it was reached.
    """
    bind_sdk_root(root)
    from tan.planner import load_board_yaml
    from tan.planner.cli import emit_artefact
    project = load_board_yaml(Path(board_yaml))
    return emit_artefact(project, mode, board_yaml=Path(board_yaml),
                         build_root=Path(build_root), core=core)
