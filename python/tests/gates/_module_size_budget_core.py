# SPDX-License-Identifier: Apache-2.0
"""Shared measurement + storage for the module/function size ratchet (tan-cli#668).

Both `test_module_size_budget.py` (the CI gate) and
`scripts/regen_module_size_budget.py` (the tool that produces the file the
gate reads) import THIS module rather than duplicating the walk -- the
previous design's real defect was not the ratchet, it was that the ratchet's
numbers lived nowhere except a hand-maintained dict literal, so every PR that
moved one had to retype it and every merge that touched two had to reconcile
them by hand. See `module_size_budget.generated.json` for the data this
produces and `MODULE_SIZE_BUDGET_LOG.md` for why any entry in it grew.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import NamedTuple

#: `python/tan`, found from this file rather than from a cwd, so the gate is
#: identical however pytest (or the regen script) was started.
PACKAGE = Path(__file__).resolve().parents[2] / "tan"

GENERATED_PATH = Path(__file__).resolve().parent / "module_size_budget.generated.json"
LOG_PATH = Path(__file__).resolve().parent / "MODULE_SIZE_BUDGET_LOG.md"

#: The house guideline. Any module NOT recorded in the generated file's
#: `modules` map must be under this -- that is what stops a new oversized
#: module joining the tracked set silently.
MODULE_CAP = 800

#: The guideline for a function body, same role for the function ratchet.
FUNCTION_CAP = 50

#: `tan/planner/**` is a hash-audited relocation of alp-sdk's
#: `scripts/alp_orchestrate/**` (see `test_planner_relocation_freshness.py`).
#: An oversized module under it is upstream's to split, not this repo's.
MIRRORED_PREFIX = "tan/planner/"


class MeasuredState(NamedTuple):
    """What `measure_current()` and the generated file both hold in common."""

    modules: dict[str, int]
    function_count: int
    function_worst: int


def modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def rel(path: Path) -> str:
    return path.relative_to(PACKAGE.parent).as_posix()


def long_functions(tree: ast.AST) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            span = (node.end_lineno or node.lineno) - node.lineno + 1
            if span > FUNCTION_CAP:
                out.append((span, node.name))
    return out


def measure_current() -> MeasuredState:
    """Walk the real tree. This is the ONLY function either the gate or the
    regen tool trusts for "what is true right now" -- neither ever reads the
    previous generated file to derive a new one; both re-derive from source,
    which is what makes a padded or stale value structurally impossible
    (tan-cli#668's constraint: a merge resolution must re-measure, not
    interpolate between two committed numbers)."""
    module_lines: dict[str, int] = {}
    found: list[tuple[int, str]] = []
    for path in modules():
        text = path.read_text(encoding="utf-8")
        lines = len(text.splitlines())
        if lines > MODULE_CAP:
            module_lines[rel(path)] = lines
        tree = ast.parse(text)
        found.extend(long_functions(tree))
    worst = max((span for span, _ in found), default=0)
    return MeasuredState(modules=module_lines, function_count=len(found), function_worst=worst)


def load_generated() -> MeasuredState:
    """Parse the committed file. Raises on a duplicate `modules` key: Python's
    (and JSON's) own "last write wins" dict-literal collapse is exactly the
    silent-drop shape tan-cli#586 found in the previous hand-maintained dict,
    and a generated file is not immune to a bad hand-edit landing anyway."""
    raw = GENERATED_PATH.read_text(encoding="utf-8")
    seen: dict[str, int] = {}
    duplicates: list[str] = []

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, value in pairs:
            if key in out:
                duplicates.append(key)
            out[key] = value
        return out

    data = json.loads(raw, object_pairs_hook=reject_duplicates)
    if duplicates:
        raise ValueError(
            f"{GENERATED_PATH.name} declares these keys more than once: "
            f"{sorted(set(duplicates))} (tan-cli#586 class -- the last spelling "
            "silently wins and any other is dead weight)"
        )
    module_map = {str(k): int(v) for k, v in data["modules"].items()}
    return MeasuredState(
        modules=module_map,
        function_count=int(data["function_count_budget"]),
        function_worst=int(data["function_worst_budget"]),
    )


def dump_generated(state: MeasuredState) -> str:
    """Canonical text form -- sorted keys, 2-space indent, one trailing
    newline. Deterministic so two independent regen runs against the same
    tree byte-for-byte agree, and so a diff shows only the numbers that
    actually moved."""
    payload = {
        "$schema": "module_size_budget.generated.json is produced by "
        "`python scripts/regen_module_size_budget.py` -- see tan-cli#668. "
        "Do not hand-edit; a hand-edited value that does not match a real "
        "measurement fails test_the_generated_budget_is_in_sync.",
        "module_cap": MODULE_CAP,
        "function_cap": FUNCTION_CAP,
        "modules": dict(sorted(state.modules.items())),
        "function_count_budget": state.function_count,
        "function_worst_budget": state.function_worst,
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"
