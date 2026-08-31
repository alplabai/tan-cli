# SPDX-License-Identifier: Apache-2.0
"""Shared measurement + storage for the module/function size ratchet (tan-cli#668).

Both `test_module_size_budget.py` (the CI gate) and
`scripts/regen_module_size_budget.py` (the tool that produces the file the
gate reads) import THIS module rather than duplicating the walk -- the
previous design's real defect was not the ratchet, it was that the ratchet's
numbers lived nowhere except a hand-maintained dict literal, so every PR that
moved one had to retype it and every merge that touched two had to reconcile
them by hand. See `module_size_budget.generated.json` for the data this
produces and `MODULE_SIZE_BUDGET_LOG.d/` for why any entry in it grew
(tan-cli#907; `MODULE_SIZE_BUDGET_LOG.md` is the frozen pre-migration
history, see `LOG_PATH` below).
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import NamedTuple

#: `python/tan`, found from this file rather than from a cwd, so the gate is
#: identical however pytest (or the regen script) was started.
PACKAGE = Path(__file__).resolve().parents[2] / "tan"

#: `python/tests`, the sibling tree -- MEASURED but never gated (tan-cli#817).
#: Found the same cwd-independent way as `PACKAGE` above.
#:
#: The scope decision this encodes, made deliberately and recorded here so it
#: is not rediscovered from a failing hash: the ratchet gates `tan/**` and
#: only OBSERVES `tests/**`. tan-cli#817's actual complaint was not that test
#: files grow, it was that they grow INVISIBLY -- "there is no number for a
#: PR to move, so the growth is not visible in review or in a diff of the
#: generated file". Measuring closes that; gating would not have been free.
#: Measured on 60 consecutive `dev` commits, 36% of them GREW a `tests/**`
#: file already over `MODULE_CAP`, so a per-file ratchet here would have made
#: better than one PR in three write a `--reason` ledger entry, and the ledger
#: would fill with "added test cases" -- taxing exactly the behaviour this
#: repo wants and burying the `tan/**` reasons that are the log's whole point.
#:
#: So `observed_tests` in the generated file is a RECORD, never a ceiling.
#: Nothing in the gate compares it to a threshold; the only thing that can
#: fail on it is going STALE, which a plain `regen` fixes with no `--reason`.
#: That is pinned by `test_the_observed_test_tree_is_recorded_not_gated`.
TEST_ROOT = Path(__file__).resolve().parents[1]

GENERATED_PATH = Path(__file__).resolve().parent / "module_size_budget.generated.json"

#: FROZEN as of tan-cli#907 -- no future write path targets this file any
#: more (`_append_log` in `scripts/regen_module_size_budget.py` writes into
#: `LOG_DIR` below instead). Kept, and still enforced append-only by
#: `test_module_size_budget_log_append_only.py`, purely as the historical
#: record up to the freeze; see its own closing note for why.
LOG_PATH = Path(__file__).resolve().parent / "MODULE_SIZE_BUDGET_LOG.md"

#: tan-cli#907: the live ledger. One file per regen-written entry, mirroring
#: `changelog.d/` (`changelog.d/README.md`'s own reasoning applies verbatim:
#: "disjoint files cannot conflict"). Unlike `LOG_PATH`'s old single-file
#: shape, two branches that each add an entry here need no merge driver and
#: no conflict resolution at all -- git (and, unlike `.gitattributes`
#: `merge=union`, GitHub's own PR-mergeability computation, which does not
#: apply custom merge drivers -- measured on PR #971, tan-cli#907 comment,
#: 2026-08-28: a clean local `git merge origin/dev` at that head, GitHub
#: polled three times over eight minutes to `CONFLICTING` every time) both
#: treat two new, differently-named files as trivially compatible.
LOG_DIR = Path(__file__).resolve().parent / "MODULE_SIZE_BUDGET_LOG.d"

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


def test_tree_modules() -> list[Path]:
    """The observed tree (tan-cli#817). Deliberately a SEPARATE enumeration
    from `modules()` rather than a second root folded into it: everything
    downstream of `modules()` is gated, and a walk that yielded both would
    put `tests/**` inside the ratchet by accident -- the exact outcome the
    scope decision above rejects."""
    return sorted(TEST_ROOT.rglob("*.py"))


def measure_observed_tests() -> dict[str, int]:
    """Line counts for the `tests/**` files over `MODULE_CAP`, keyed the same
    way `modules` is (both go through `rel()`, which relativises to
    `PACKAGE.parent`, so `tan/...` and `tests/...` are siblings in the file).

    Same threshold as the gated side on purpose: the number answers "which
    files are over the house guideline", and the guideline does not change
    because a file holds tests. What changes is the CONSEQUENCE -- see
    `TEST_ROOT`."""
    out: dict[str, int] = {}
    for path in test_tree_modules():
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > MODULE_CAP:
            out[rel(path)] = lines
    return out


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


def _load_json() -> dict:
    """Parse the committed file once. Raises on a duplicate key: Python's
    (and JSON's) own "last write wins" dict-literal collapse is exactly the
    silent-drop shape tan-cli#586 found in the previous hand-maintained dict,
    and a generated file is not immune to a bad hand-edit landing anyway.

    Split out of `load_generated` by tan-cli#817 so the observed section can
    be read through the SAME duplicate-key check rather than a second, laxer
    `json.loads` that would not have it."""
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
    return data


def load_generated() -> MeasuredState:
    """The GATED half of the committed file."""
    data = _load_json()
    module_map = {str(k): int(v) for k, v in data["modules"].items()}
    return MeasuredState(
        modules=module_map,
        function_count=int(data["function_count_budget"]),
        function_worst=int(data["function_worst_budget"]),
    )


def load_observed_tests() -> dict[str, int]:
    """The OBSERVED half. Absent key reads as empty rather than raising, so
    the first regen after tan-cli#817 seeds the section instead of failing on
    a file that predates it."""
    return {str(k): int(v) for k, v in _load_json().get("observed_tests", {}).items()}


def dump_generated(state: MeasuredState, observed_tests: dict[str, int]) -> str:
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
        "observed_tests_are_not_a_budget": "Every key under `observed_tests` "
        "is a MEASUREMENT of python/tests/**, never a ceiling (tan-cli#817). "
        "Nothing compares these to a threshold and no growth here needs a "
        "--reason; the only failure they can cause is going stale, which a "
        "plain `python scripts/regen_module_size_budget.py` fixes. See "
        "`TEST_ROOT` in _module_size_budget_core.py for why this tree is "
        "observed rather than gated.",
        "observed_tests": dict(sorted(observed_tests.items())),
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"
