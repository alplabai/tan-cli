# SPDX-License-Identifier: Apache-2.0
"""Shared measurement + storage for the module/function size ratchet (tan-cli#668).

Both `test_module_size_budget.py` (the CI gate) and
`scripts/regen_module_size_budget.py` (the tool that produces the files the
gate reads) import THIS module rather than duplicating the walk -- the
previous design's real defect was not the ratchet, it was that the ratchet's
numbers lived nowhere except a hand-maintained dict literal, so every PR that
moved one had to retype it and every merge that touched two had to reconcile
them by hand. See `module_size_budget.d/` for the data this produces (and
that directory's own `README.md`), and `MODULE_SIZE_BUDGET_LOG.d/` for why
any budgeted entry in it grew (tan-cli#907; `MODULE_SIZE_BUDGET_LOG.md` is
the frozen pre-migration history, see `LOG_PATH` below).

## One file per module, and two derived scalars (tan-cli#1057)

Through 2026-08-31 all of that data lived in ONE file,
`module_size_budget.generated.json`. tan-cli#907 removed the LEDGER's
conflict class structurally, by giving every entry its own file; it left the
generated JSON alone and filed tan-cli#1057 for it. This is that follow-up,
and it applies the same structural fix: one record file per measured module,
under `RECORD_DIR` below, so two branches that touch different modules are
never writing the same path and have nothing to conflict over -- no merge
driver, local or GitHub-side.

The issue proposed splitting per TOP-LEVEL PACKAGE and said outright that a
real proposal should measure a larger sample first. Measured across 93
value-changing commits to the old single file (4278 commit pairs), the
per-package split leaves 61.3% of pairs still colliding -- `tan/commands/`
alone is touched by 69% of them -- against 12.9% for the per-module split
with the two whole-tree scalars DERIVED. The scalars are the load-bearing
half of that: storing them costs 22.4% instead of 12.9%, because 34% of the
sampled commits moved one.

Deriving them changes nothing about what the ratchet MEANS. In
`measure_current` below, `function_count` was always exactly `len(found)`
where `found` accumulates per module, and `function_worst` exactly
`max(span)` over the same list -- a sum and a max over per-module facts. So
`MeasuredState` exposes them as computed PROPERTIES over `functions`, not as
stored fields: the gate still compares whole-tree totals, and there is no
longer a stored number two branches can both write.
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
#: So an `observed` record is a RECORD, never a ceiling. Nothing in the gate
#: compares one to a threshold; the only thing that can fail on it is going
#: STALE, which a plain `regen` fixes with no `--reason`. That is pinned by
#: `test_the_observed_test_tree_is_recorded_not_gated`. tan-cli#1057 split
#: these per file alongside the budgeted ones (they conflicted for the same
#: mechanical reason and are inside the measurement above), and kept the
#: distinction MACHINE-READABLE rather than positional: every record carries
#: an explicit `kind`, and `load_generated` / `load_observed_tests` each read
#: only their own kind.
TEST_ROOT = Path(__file__).resolve().parents[1]

#: tan-cli#1057: the per-module record tree that replaced the single
#: `module_size_budget.generated.json`. A record's PATH is its key: the
#: module's repo-relative path (as `rel()` spells it) plus a `.json` suffix,
#: e.g. `module_size_budget.d/tan/commands/build_cmd.py.json`. Mirroring the
#: source tree makes the name scheme collision-free by construction (the map
#: is a suffix append on an already-unique path, so it is injective) and
#: deterministic (no counter, no hash, nothing a second branch could compute
#: differently for the same module).
RECORD_DIR = Path(__file__).resolve().parent / "module_size_budget.d"

#: Every record file ends in this. It is also what tells a record apart from
#: the directory's two non-record files (`_caps.json`, `README.md`) without
#: an allow-list that would silently swallow a third, unexpected one --
#: `_load_records` rejects anything that is neither.
RECORD_SUFFIX = ".json"

#: The caps the records were measured against, kept in the record tree so it
#: stays self-describing when read without this module (the role the old
#: single file's `module_cap`/`function_cap` fields played). It is written by
#: every regen run and changes only when a cap changes, so two branches write
#: byte-identical content to it and git sees no change at all.
CAPS_PATH = RECORD_DIR / "_caps.json"

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

#: The house guideline. Any module with no `lines` ceiling recorded under
#: `RECORD_DIR` must be under this -- that is what stops a new oversized
#: module joining the tracked set silently.
MODULE_CAP = 800

#: The guideline for a function body, same role for the function ratchet.
FUNCTION_CAP = 50

#: `tan/planner/**` is a hash-audited relocation of alp-sdk's
#: `scripts/alp_orchestrate/**` (see `test_planner_relocation_freshness.py`).
#: An oversized module under it is upstream's to split, not this repo's.
MIRRORED_PREFIX = "tan/planner/"

#: The two record kinds. `budget` is the ratcheted `tan/**` side; `observed`
#: is the measured-never-gated `tests/**` side (tan-cli#817). Which one a
#: record must be is derivable from its path, and `_load_records` checks the
#: declared kind against it -- a record that disagrees with its own location
#: is exactly the shape that would let a `tests/**` measurement be read as a
#: `tan/**` ceiling, so it raises rather than being coerced either way.
KIND_BUDGET = "budget"
KIND_OBSERVED = "observed"


class ModuleFunctions(NamedTuple):
    """The per-module function facts the two whole-tree scalars are derived
    from: how many of this module's functions are over `FUNCTION_CAP`, and
    the longest one's span."""

    count: int
    worst: int


class MeasuredState(NamedTuple):
    """What `measure_current()` and the committed records both hold in common.

    `function_count` / `function_worst` are PROPERTIES, not fields, and that
    is the whole of tan-cli#1057's second half: they are a sum and a max over
    `functions`, exactly as `measure_current` always computed them, so no
    branch can write them and no merge can reconcile two committed opinions
    about them. The gate reads the same two names it always did and still
    compares WHOLE-TREE totals -- the ratchet's meaning is unchanged."""

    modules: dict[str, int]
    functions: dict[str, ModuleFunctions]

    @property
    def function_count(self) -> int:
        return sum(facts.count for facts in self.functions.values())

    @property
    def function_worst(self) -> int:
        return max((facts.worst for facts in self.functions.values()), default=0)


def empty_state() -> MeasuredState:
    return MeasuredState(modules={}, functions={})


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
    way the budgeted modules are (both go through `rel()`, which relativises
    to `PACKAGE.parent`, so `tan/...` and `tests/...` are siblings both in
    the record tree and in these maps).

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


def record_path(key: str) -> Path:
    """The one place the module key -> record path map is spelled. Injective
    by construction: a fixed suffix appended to a path that is already unique
    within the tree."""
    return RECORD_DIR / (key + RECORD_SUFFIX)


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
    previously committed records to derive new ones; both re-derive from
    source, which is what makes a padded or stale value structurally
    impossible (tan-cli#668's constraint: a merge resolution must re-measure,
    not interpolate between two committed numbers). Splitting the storage
    per module (tan-cli#1057) does not touch that: a resolution is still
    "delete either side and re-run the script against the merged tree"."""
    module_lines: dict[str, int] = {}
    functions: dict[str, ModuleFunctions] = {}
    for path in modules():
        text = path.read_text(encoding="utf-8")
        key = rel(path)
        lines = len(text.splitlines())
        if lines > MODULE_CAP:
            module_lines[key] = lines
        found = long_functions(ast.parse(text))
        if found:
            functions[key] = ModuleFunctions(
                count=len(found), worst=max(span for span, _ in found)
            )
    return MeasuredState(modules=module_lines, functions=functions)


def _load_json(path: Path) -> dict:
    """Parse one record. Raises on a duplicate key: Python's (and JSON's) own
    "last write wins" dict-literal collapse is exactly the silent-drop shape
    tan-cli#586 found in the original hand-maintained dict, and a generated
    file is not immune to a bad hand-edit landing anyway.

    tan-cli#1057 kept this per FILE and added the cross-file half the split
    newly needs -- see `_load_records`, where a record whose declared
    `module` disagrees with its own path raises. Together those are the same
    guarantee the single file's one duplicate-key check gave: no measured
    module can be spelled twice with one spelling silently winning."""
    raw = path.read_text(encoding="utf-8")
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
            f"{path.relative_to(RECORD_DIR.parent).as_posix()} declares these "
            f"keys more than once: {sorted(set(duplicates))} (tan-cli#586 class "
            "-- the last spelling silently wins and any other is dead weight)"
        )
    return data


def _expected_kind(key: str) -> str:
    return KIND_BUDGET if key.startswith("tan/") else KIND_OBSERVED


def _load_records() -> dict[str, dict]:
    """Every record under `RECORD_DIR`, keyed by module path.

    Three refusals, all of them the silent-drop shape a split newly exposes
    and none of which the single file needed:

    * a file that is neither a record nor one of the two known non-record
      files is an ERROR, not something to skip -- a typo'd or half-renamed
      record that is quietly ignored is a budget entry that has silently
      stopped existing;
    * a record whose `module` field disagrees with its own path is an ERROR
      -- that is the only way two files could claim the same module key, so
      it is the cross-file half of `_load_json`'s duplicate-key guard;
    * a record whose `kind` disagrees with the tree its path sits in is an
      ERROR -- that is the only way a `tests/**` measurement could be read as
      a `tan/**` ceiling, which is tan-cli#817's decision inverted.
    """
    if not RECORD_DIR.exists():
        return {}
    out: dict[str, dict] = {}
    unexpected: list[str] = []
    for path in sorted(RECORD_DIR.rglob("*")):
        if path.is_dir():
            continue
        name = path.relative_to(RECORD_DIR).as_posix()
        if name in ("_caps.json", "README.md"):
            continue
        if not name.endswith(".py" + RECORD_SUFFIX):
            unexpected.append(name)
            continue
        key = name[: -len(RECORD_SUFFIX)]
        data = _load_json(path)
        declared = data.get("module")
        if declared != key:
            raise ValueError(
                f"module_size_budget.d/{name} says it records {declared!r} but "
                f"its path says {key!r} -- a record's path IS its key, so a "
                "disagreement is two records able to claim one module (the "
                "cross-file form of tan-cli#586's silent last-write-wins)"
            )
        kind = data.get("kind")
        if kind != _expected_kind(key):
            raise ValueError(
                f"module_size_budget.d/{name} declares kind {kind!r}, but a "
                f"record for {key!r} must be {_expected_kind(key)!r} -- "
                "`budget` is the ratcheted tan/** side and `observed` is the "
                "measured-never-gated tests/** side (tan-cli#817)"
            )
        out[key] = data
    if unexpected:
        raise ValueError(
            "module_size_budget.d/ holds files that are neither records nor "
            f"its `_caps.json` / `README.md`: {sorted(unexpected)} -- a record "
            "that is silently skipped is a budget entry that has stopped "
            "existing without anything going red"
        )
    return out


def load_generated() -> MeasuredState:
    """The GATED half of the record tree (`kind: budget`)."""
    module_map: dict[str, int] = {}
    functions: dict[str, ModuleFunctions] = {}
    for key, data in _load_records().items():
        if data.get("kind") != KIND_BUDGET:
            continue
        lines = data.get("lines")
        if lines is not None:
            module_map[key] = int(lines)
        count = int(data.get("long_functions", 0))
        worst = int(data.get("worst_function", 0))
        if (count == 0) != (worst == 0):
            raise ValueError(
                f"module_size_budget.d/{key}{RECORD_SUFFIX} records "
                f"long_functions={count} with worst_function={worst} -- a "
                "module has a longest over-cap function exactly when it has "
                "any, so one of the two is a hand-edit"
            )
        if count:
            functions[key] = ModuleFunctions(count=count, worst=worst)
    return MeasuredState(modules=module_map, functions=functions)


def load_observed_tests() -> dict[str, int]:
    """The OBSERVED half (`kind: observed`). A missing record tree reads as
    empty rather than raising, so the first regen after tan-cli#1057 seeds it
    instead of failing on a checkout that predates it."""
    return {
        key: int(data["lines"])
        for key, data in _load_records().items()
        if data.get("kind") == KIND_OBSERVED
    }


def load_caps() -> dict[str, int]:
    """The caps the committed records were measured against."""
    data = _load_json(CAPS_PATH)
    return {"module_cap": int(data["module_cap"]), "function_cap": int(data["function_cap"])}


def _dump(payload: dict) -> str:
    """Canonical text form -- 2-space indent, one trailing newline, key order
    fixed by the caller. Deterministic so two independent regen runs against
    the same tree byte-for-byte agree, and so a diff shows only the numbers
    that actually moved."""
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def dump_budget_record(key: str, lines: int | None, facts: ModuleFunctions) -> str:
    return _dump(
        {
            "$schema": "One module's entry in the tan-cli#668 size ratchet, "
            "produced by `python scripts/regen_module_size_budget.py` -- see "
            "module_size_budget.d/README.md. Do not hand-edit; a value that "
            "does not match a real measurement fails "
            "`regen_module_size_budget.py --check` and the gate.",
            "module": key,
            "kind": KIND_BUDGET,
            "lines": lines,
            "long_functions": facts.count,
            "worst_function": facts.worst,
        }
    )


def dump_observed_record(key: str, lines: int) -> str:
    return _dump(
        {
            "$schema": "One python/tests/** MEASUREMENT, never a ceiling "
            "(tan-cli#817). Nothing compares it to a threshold and its growth "
            "needs no --reason; the only failure it can cause is going stale, "
            "which a plain `python scripts/regen_module_size_budget.py` fixes. "
            "See `TEST_ROOT` in _module_size_budget_core.py for why this tree "
            "is observed rather than gated.",
            "module": key,
            "kind": KIND_OBSERVED,
            "lines": lines,
        }
    )


def dump_caps() -> str:
    return _dump(
        {
            "$schema": "The house guidelines module_size_budget.d/'s records "
            "were measured against, kept here so the record tree is "
            "self-describing. Produced by `python "
            "scripts/regen_module_size_budget.py`; must agree with MODULE_CAP "
            "/ FUNCTION_CAP in _module_size_budget_core.py.",
            "module_cap": MODULE_CAP,
            "function_cap": FUNCTION_CAP,
        }
    )


def render_records(state: MeasuredState, observed_tests: dict[str, int]) -> dict[str, str]:
    """The whole record tree as {relative path: text}, so the writer below and
    any test can compare against it without either re-deriving the layout."""
    out: dict[str, str] = {"_caps.json": dump_caps()}
    for key in sorted(set(state.modules) | set(state.functions)):
        facts = state.functions.get(key, ModuleFunctions(count=0, worst=0))
        out[key + RECORD_SUFFIX] = dump_budget_record(key, state.modules.get(key), facts)
    for key, lines in sorted(observed_tests.items()):
        out[key + RECORD_SUFFIX] = dump_observed_record(key, lines)
    return out


def write_records(state: MeasuredState, observed_tests: dict[str, int]) -> None:
    """Write the record tree, and DELETE any record the measurement no longer
    produces. The delete half matters: a module that dropped under the cap and
    lost its long functions leaves a record describing a module that is no
    longer measured, which is the stale-ceiling shape
    `test_the_module_budget_has_not_gone_stale` exists to catch."""
    wanted = render_records(state, observed_tests)
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in wanted.items():
        path = RECORD_DIR / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
    for path in sorted(RECORD_DIR.rglob("*"), reverse=True):
        if path.is_dir():
            if not any(path.iterdir()):
                path.rmdir()
            continue
        name = path.relative_to(RECORD_DIR).as_posix()
        if name == "README.md" or name in wanted:
            continue
        path.unlink()
