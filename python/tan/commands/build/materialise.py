# SPDX-License-Identifier: Apache-2.0
"""Write every artefact the plan carries under `build_root` -- BEFORE any
slice runs. That ordering is the stated precondition of the plan's
slice-independence invariant: `sharedArtefacts` and every slice's
`configArtefacts` must all exist on disk before dispatch starts, or
concurrent slice execution (planned later) would race a slice reading a
config file its own materialise step hasn't written yet.

Mirrors `tan-cli/src/commands/build/materialise.rs`'s `MaterialiseError`
(one type, two failure shapes -- an unsafe path, or an I/O error) rather than
letting either escape as a bare exception."""
from pathlib import Path
from typing import Any

from tan.core.build_plan import BuildPlan


class MaterialiseError(ValueError):
    """A structured, coded materialise failure. Subclasses `ValueError` (not
    a plain `Exception`) so `except ValueError` -- the shape a caller reaches
    for around "a plan-supplied path was bad" -- still catches it; `.code`/
    `.message` are there for a caller that wants to fold it into the
    `(code, message)` envelope contract instead (mirrors `PlanParseError`,
    `TokenSubstitutionError`)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def confine_to_build_root(build_root: Path, rel: str) -> Path:
    """Resolve `rel` under `build_root`, refusing anything that escapes it.

    Plans are trusted input, but a plan-supplied relative path (an
    artefact's `path`, or -- reused by `execute.py` -- a slice command's
    `cwd`) must never let `build_root / rel` land outside `build_root`.
    `Path.is_absolute()` alone is not enough to guard against this on
    Windows: a drive-relative `C:foo` or a rooted `\\foo` is NOT
    `is_absolute()`, yet joining either onto `build_root` discards part or
    all of it (`PurePath.__truediv__` replaces the base outright once the
    right-hand side supplies its own root/drive) -- exactly the shape that
    let a path escape silently. Resolving both sides and requiring
    containment catches every one of these shapes (verified empirically:
    `..`, a bare posix `/x`, `C:x`, and `\\x` all resolve outside
    `build_root.resolve()`), not just the lexical `..` case.
    """
    root = build_root.resolve()
    target = (build_root / rel).resolve()
    if target != root and root not in target.parents:
        raise MaterialiseError(
            "build.path-escape",
            f"path `{rel}` would escape the build root `{root}`",
        )
    return target


def _write(build_root: Path, artefact: dict[str, Any]) -> Path:
    rel = artefact["path"]
    target = confine_to_build_root(build_root, rel)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(artefact.get("contents", ""), encoding="utf-8", newline="")
    except OSError as err:
        # Permission denied, a Windows path over MAX_PATH, a parent that's
        # actually a file, target itself being a directory (an empty/`.`
        # artefact path) -- every write-time OSError, never a bare traceback.
        raise MaterialiseError(
            "build.artefact-write-failed", f"failed to write `{rel}`: {err}"
        ) from err
    return target


def materialise_plan(plan: BuildPlan, build_root: Path) -> list[Path]:
    """Write ALL `sharedArtefacts`, then every slice's `configArtefacts`, and
    only then return. Callers must not start any slice before this returns --
    that ordering is the whole point (see module docstring)."""
    written = [_write(build_root, a) for a in plan.shared_artefacts]
    for sl in plan.slices:
        written.extend(_write(build_root, a) for a in sl.config_artefacts)
    return written
