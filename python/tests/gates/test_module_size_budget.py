# SPDX-License-Identifier: Apache-2.0
"""A 3000-line module and a 679-line function must not arrive unnoticed
(tan-cli#408).

The house guideline is 800 lines per module and 50 per function. Nothing
enforced either: there is no `[tool.ruff]`, flake8 or pylint section in
`python/pyproject.toml` and no Python lint job in `.github/workflows/`. The
only acknowledgement anywhere under `python/tan/` is one `# noqa: PLR0911,
PLR0912, PLR0915` on `bootstrap_cmd._run`, which no configured linter would
ever act on.

The measurement that makes this a gate rather than a preference: between
tan-cli#408 being filed and this file being written, with nobody objecting
and nothing failing, every one of the six modules it named GREW.

    doctor_cmd.py      3019 -> 3114
    bootstrap_cmd.py   2658 -> 2781
    core/flash_plan.py 1721 -> 1808
    flash_cmd.py       1652 -> 1783
    _run                630 -> 679 lines

So this is a RATCHET, not a cap. It records what is true today and fails on
growth. It deliberately does NOT fail the 23 modules and 199 functions that
are already over -- a gate that is red on the day it lands gets disabled, and
then it guards nothing at all.

## Why a pytest gate and not a ruff job

`python -- pytest across python/` is ALREADY a required context on `main` and
`dev`. A new CI job would have to be added to the required list to matter,
and adding a required context blocks every open PR until it has run on each
of them. This runs inside a gate that is already required, so it starts
enforcing on the next PR with no protection change. `pyproject.toml` gaining
a `[tool.ruff]` section is still worth doing for editor integration; it is
not what makes a rule enforced here.

## How to change these numbers

Lower them, freely, whenever a split lands -- that is the point. Raising one
means a module grew, and needs a reason in the diff that raises it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: `python/tan`, found from this file rather than from a cwd, so the gate is
#: identical however pytest was started (tan-cli#423's lesson).
_PACKAGE = Path(__file__).resolve().parents[2] / "tan"

#: The house guideline. Any module NOT in `_MODULE_BUDGET` must be under it --
#: that is what stops a 24th oversized module joining the list silently.
_MODULE_CAP = 800

#: The guideline for a function body, same role for the function ratchet.
_FUNCTION_CAP = 50

#: Every module over `_MODULE_CAP` as of 2026-08-04, with its measured line
#: count as its ceiling.
#:
#: Four entries were re-measured when this branch caught up with `dev`: the
#: ratchet was calibrated before tan-cli#426 merged, and that PR grew exactly
#: these four. Each still pins the file's EXACT current size, so the next
#: unnoticed growth still fails -- the baseline moved, the gate did not
#: loosen. `build/execute.py` 902 -> 941 (#419's Zephyr-SDK message),
#: `generate_cmd.py` 1101 -> 1147 (#420's refused-emit cleanup),
#: `build_cmd.py` 1555 -> 1559 (#407's ladder-divergence helper),
#: `validate_cmd.py` 1092 -> 1093 (a corrected #262 docstring). Recorded per file rather than as a single "worst
#: module" number so a split that shrinks one file cannot be spent widening
#: another.
_MODULE_BUDGET: dict[str, int] = {
    # 3127, not 3114: the 27-camelCase-issue-code fix added `kebab_check_name`
    # (+ its `_CAMEL_BOUNDARY` regex), the one shared place a `Check.name`
    # becomes a kebab issue-code suffix, used by both `checks_to_issues()` here
    # and `support_bundle_cmd._doctor_issues()`.
    "tan/commands/doctor_cmd.py": 3127,
    # 2833, not 2781, as of tan-cli#459: `--print-env` used to disagree with
    # `--dry-run` about which workspace a real run would build, on both the
    # workspace-parent-relocation branch AND a `$ZEPHYR_BASE` adoption branch
    # -- fixing both moved real logic into a new module-level
    # `_print_env_outcome` (extracted specifically to keep `_run` AT, not
    # over, its own 679-line ceiling below -- extraction cannot shrink the
    # MODULE total, only move lines off the worst function), plus the
    # one-line `target is not None` gate on the tan-cli#389 orphan refusal
    # (was refusing every in-place re-run of an already-bootstrapped
    # workspace, naming its own non-existent relocation target "None").
    "tan/commands/bootstrap_cmd.py": 2833,
    "tan/core/bootstrap.py": 1890,
    "tan/core/flash_plan.py": 1808,
    "tan/commands/flash_cmd.py": 1781,
    "tan/planner/kconfig.py": 1639,
    "tan/commands/build_cmd.py": 1559,
    "tan/commands/renode_cmd.py": 1440,
    "tan/commands/debug_config_cmd.py": 1296,
    "tan/planner/template.py": 1199,
    "tan/core/scaffold.py": 1106,
    # 1150, not 1147, as of tan-cli#457's review round: the overlay guard's
    # `--all` re-run fix had to become content-aware -- reading the existing
    # overlay and comparing it against the banner every tan-emitted one
    # carries -- to tell tan's own prior output from a real hand edit, which
    # the previous 1-line `destination.exists()` check could not. Raised
    # rather than extracted: `_overlay_would_overwrite` is five body lines
    # including its own read-error handling; there is nothing left to split.
    "tan/commands/generate_cmd.py": 1150,
    "tan/commands/sdk_cmd.py": 1096,
    "tan/commands/validate_cmd.py": 1093,
    "tan/commands/new_som_cmd.py": 1047,
    "tan/commands/clean_cmd.py": 1000,
    "tan/planner/loader.py": 996,
    "tan/commands/init_cmd.py": 974,
    # 1060, not 923, as of the tan-cli#456 review round: `_select_slice`'s
    # `os`-vocabulary map, its `native_sim` board discriminator, its manifest
    # slice reader, and the `--target-kind` inference decision itself
    # (`infer_target_kind`, its message-building split into four small
    # helpers to keep it under the FUNCTION ratchet too) all moved here from
    # `debug_config_cmd.py`, which was over ITS OWN budget after the same
    # review's bugfix -- "move the decision, don't just extract a helper" was
    # the review's own suggested fix, since `support_bundle_cmd.py` needs the
    # identical decision and both commands already import this module.
    # Raised rather than split further: the alternative was leaving the
    # shared decision duplicated per command, the exact drift this move
    # exists to prevent.
    "tan/core/debug_launch.py": 1060,
    "tan/commands/build/execute.py": 941,
    # 970, not 848, as of tan-cli#432: the alp-sdk#1069 port added the
    # disjoint per-core slot0 partition map (+168, matching alp-sdk's own
    # delta in scripts/gen_zephyr_board.py line for line). Raised rather
    # than extracted because this file mirrors an upstream generator --
    # splitting it here would make the next port a hand-merge instead of
    # a diff.
    "tan/planner/zephyr_board.py": 970,
    "tan/commands/support_bundle_cmd.py": 834,
    # 842, not 831, as of tan-cli#433: `_reorder_global_flags` now consults
    # `_every_declared_format()` -- the same single source `_format_callback`
    # reads -- instead of a second, driftable tuple, and the docstring
    # records why (the old rule silently DROPPED the subcommand for any
    # leading `--format` outside `text`/`json`). Raised rather than
    # extracted: the growth is the explanation of a shipped regression,
    # which is the last thing to move out of the file it explains.
    "tan/cli.py": 842,
}

#: Some of these are `tan/planner/**`, which is a hash-audited MIRROR of
#: alp-sdk's `scripts/alp_orchestrate/**` (`test_planner_relocation_
#: freshness.py`). Splitting one here would make the mirror diverge in SHAPE
#: from upstream and is the wrong repository for the fix -- tan-cli#408's
#: acceptance names `kconfig.py` and a `_library_alias_table` dedup across
#: `kconfig.py`/`libraries.py`/`loader.py`, and all of those are mirror
#: files. That part of #408 belongs upstream, not here.
_MIRRORED = ("tan/planner/",)

#: Functions over `_FUNCTION_CAP` as of 2026-08-04: 199 of them, which is far
#: too many to enumerate readably. Two numbers ratchet them instead -- the
#: COUNT (a new long function pushes it up) and the WORST (an existing one
#: growing pushes it up). Neither can move without this file moving.
# 200, not 199, for the same reason as the four module entries above: both
# functions that crossed 50 lines came from the tan-cli#407 fixes that landed
# after this ratchet was calibrated -- `tan/commands/sdk_cmd.py:_run_current`
# and `tan/envelope.py:_with_sdk_divergence`, each of which now emits the
# shared `sdk.discovery-divergent` warning. Measured: 198 over 50 lines at
# f3208e1, 200 now.
# 201 as of tan-cli#432: `tan/planner/zephyr_board.py:_aen_flash_partitions`
# crossed 50 lines carrying the alp-sdk#1069 disjoint-slot0 branch. It is a
# line-for-line port of alp-sdk's own function, which is the same size --
# extracting here would make the next port a hand-merge instead of a diff.
_FUNCTION_COUNT_BUDGET = 201
_FUNCTION_WORST_BUDGET = 679


def _modules() -> list[Path]:
    return sorted(_PACKAGE.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(_PACKAGE.parent).as_posix()


def _long_functions(tree: ast.AST) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            span = (node.end_lineno or node.lineno) - node.lineno + 1
            if span > _FUNCTION_CAP:
                out.append((span, node.name))
    return out


def test_no_module_grows_past_its_recorded_budget():
    """The ratchet. A budgeted module may shrink freely; growing past its
    recorded size fails and must be answered in the diff that causes it."""
    grew = []
    for path in _modules():
        rel = _rel(path)
        lines = len(path.read_text(encoding="utf-8").splitlines())
        ceiling = _MODULE_BUDGET.get(rel, _MODULE_CAP)
        if lines > ceiling:
            grew.append(f"{rel}: {lines} lines, budget {ceiling}")

    assert grew == [], (
        "these modules are over budget:\n  "
        + "\n  ".join(grew)
        + f"\n\nA module not in _MODULE_BUDGET is capped at {_MODULE_CAP}. Either "
        "extract from it, or raise its entry with a reason -- the entries "
        "record what was true on 2026-08-04, and every one of them grew "
        "silently before this gate existed."
    )


def test_the_module_budget_has_not_gone_stale():
    """The other direction: a budget entry for a file that has SHRUNK well
    under its ceiling is a ratchet that stopped ratcheting. Lower it, so the
    next growth is caught at the new level rather than at the old one.

    The slack allowed is deliberately generous (50 lines). This gate exists
    to catch a module doubling, not to make every ordinary edit renegotiate a
    number."""
    slack = []
    for rel, ceiling in sorted(_MODULE_BUDGET.items()):
        path = _PACKAGE.parent / rel
        if not path.exists():
            slack.append(f"{rel}: budgeted but no longer exists -- drop the entry")
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines <= _MODULE_CAP:
            slack.append(f"{rel}: {lines} lines, now under {_MODULE_CAP} -- drop the entry")
        elif ceiling - lines > 50:
            slack.append(f"{rel}: {lines} lines, budget {ceiling} -- lower it")

    assert slack == [], "the module budget no longer describes the tree:\n  " + "\n  ".join(slack)


def test_no_new_long_function_and_none_of_them_grows():
    """199 functions are already over 50 lines, so enumerating them would be
    a 199-line table nobody reads. The COUNT and the WORST are ratcheted
    instead: a new long function moves the count, and an existing one growing
    moves the worst. `bootstrap_cmd._run` is the worst at 679 lines, which is
    what tan-cli#408's `# noqa: PLR0911, PLR0912, PLR0915` is standing in
    front of."""
    found: list[tuple[int, str]] = []
    for path in _modules():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as err:  # pragma: no cover -- a broken tree fails elsewhere first
            pytest.fail(f"{_rel(path)} does not parse: {err}")
        found.extend((span, f"{_rel(path)}:{name}") for span, name in _long_functions(tree))

    worst = max(found, default=(0, "<none>"))
    assert len(found) <= _FUNCTION_COUNT_BUDGET, (
        f"{len(found)} functions are over {_FUNCTION_CAP} lines, budget "
        f"{_FUNCTION_COUNT_BUDGET}. Extract from the one you just grew, or "
        f"raise the budget with a reason. Longest: {worst[1]} at {worst[0]} lines."
    )
    assert worst[0] <= _FUNCTION_WORST_BUDGET, (
        f"{worst[1]} is {worst[0]} lines, past the recorded worst "
        f"({_FUNCTION_WORST_BUDGET}). The longest function in the package "
        f"getting longer is the exact drift tan-cli#408 reports."
    )


def test_the_mirrored_planner_is_named_as_out_of_scope():
    """`tan/planner/**` is a hash-audited relocation of alp-sdk's
    `scripts/alp_orchestrate/**`. Splitting a mirror file here would make it
    diverge in SHAPE from upstream, which
    `test_planner_relocation_freshness.py` exists to prevent -- so any
    oversized module under it is upstream's to fix, and this records that
    rather than leaving the next reader to rediscover it from a failing hash.

    Asserted, not commented, because the fact is load-bearing: tan-cli#408's
    acceptance asks for `kconfig.py` to be split and for
    `_library_alias_table` to be deduplicated across `kconfig.py`,
    `libraries.py` and `loader.py`. All of those are mirror files. That part
    of the issue cannot be done in this repository."""
    mirrored = [rel for rel in _MODULE_BUDGET if rel.startswith(_MIRRORED)]
    assert mirrored, "no mirrored planner module is budgeted -- has the mirror moved?"
    for rel in mirrored:
        assert (_PACKAGE.parent / rel).exists(), f"{rel} is budgeted but missing"
