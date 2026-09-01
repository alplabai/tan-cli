# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1081: a test helper deliberately promoted to a shared `tests/`
module must not be re-implemented privately in another test module.

WHAT THIS ASSERTS, and nothing wider. For each name in `_SHARED_TEST_HELPERS`
below -- an explicit, opt-in allow-list, currently two entries -- there is
exactly one module-level definition anywhere under `python/tests/**`, and it
is in the module this file declares as its home. "Module-level definition"
means, precisely: a top-level `def`/`async def`; a top-level `Assign` to a
bare name whose value is NOT itself a bare name or attribute (that shape is
an alias, and aliasing a shared helper is allowed on purpose); or a top-level
annotated assignment to a bare name that HAS a value. Leading underscores are
stripped before comparing, so `_x` and `x` are one name here. Nothing below
module level is looked at, and nothing outside `python/tests/**` is.

Those node-shape rules are `test_shared_helpers_have_one_definition.py`'s,
which runs the equivalent walk over `python/tan/**`. This is not literally
that file's walk: it strips leading underscores when INDEXING rather than
trying the two spellings at lookup, and it has no `_NOT_THE_SAME_HELPER`
carve-out map, because with a one-name allow-list there is nothing yet to
carve out.

WHAT THIS DOES NOT ASSERT, deliberately. It says nothing at all about any
name not in the allow-list. A test helper duplicated across two test modules
under a name nobody has seeded here is invisible to this gate and stays
invisible until somebody adds it. There is no walk over `tests/` at large and
no heuristic that promotes a name into scope; scope is exactly the dict below.

That narrowness is the whole design, and it was measured before it was chosen.
Dropping the allow-list -- asserting one definition for every name the walk
above returns -- is red the day it lands. Every figure here is `dev` at
`8b4e3f43` WITH THIS FILE EXCLUDED from the walk, i.e. the tree the gate
would have landed onto, measured by running `_module_level_definitions()`
itself: 173 names have more than one module-level definition, 725 definitions
in total, worst `SDK` 47, `runner` 27, `pytestmark` 24, `PACKAGE_ROOT` 23,
`REPO_ROOT` 21, `app` 17, `envelope` 16, `sdk_root` 15. Restricting the same
walk to `def`s only -- dropping the module-level constants, which are the
least interesting half -- still leaves 121 names and 406 definitions:
`envelope` 16, `sdk_root` 15, `write` 15, `bound_sdk` 14, `run_tan` 13, `run`
11, `project` 10.

Including this file moves those to 176/731 and 123/410: `_module_level_
definitions` and `test_the_walk_actually_finds_definitions` are names the
`python/tan/**` sibling gate already defines, so this file is itself a
three-name contributor to the count it cites. That is a fact about test-gate
boilerplate sharing a shape, not a helper anyone should import -- and it is
one more reason the allow-list is a hand-written dict rather than anything
derived from the walk.

Nearly every one of those is legitimate. A per-module `_envelope()` that
shells one command and parses its JSON is not a shared helper somebody forgot
to import, and `PACKAGE_ROOT` is not a helper at all. A gate that is red on
the day it lands is a gate that gets disabled, so this one can only ever be
red about a name a human put in the list on purpose.

WHY THIS GATE EXISTS AT ALL. The duplicate it is seeded against was real.
`bind_planner_sdk_root` exists because `tan/planner/paths.py` evaluates
`REPO = sdk_root()` (and `METADATA_ROOT` under it) at ITS OWN import time,
once per process, and `sys.modules` caches the result for the rest of the
session -- so the FIRST test in a session to import `tan.planner` decides what
`REPO` is for every later test that reads it. The binder's job is to make sure
that first import happens under the real bound checkout when there is one, not
under whatever throwaway stub a neighbouring module left bound. Before
tan-cli#1076 the logic lived in a private `_bound_sdk_root` fixture in
`test_baremetal_slice_post_commands_coverage.py`. An intermediate revision of
#1076 added the shared `bind_planner_sdk_root` to
`tests/planner/_baremetal_support.py` and LEFT that private copy in place,
while both the new module's docstring and the PR body asserted it now had
exactly one definition. Review caught it and #1076 merged at one definition,
which is the only reason this file can be seeded green -- nothing automated
could have caught it, because the sibling gate scopes its walk to
`python/tan/**`. The next one gets no reviewer who happens to look.

A stale copy re-enables a measured failure, not a hypothetical one. Driving
the SHARED binder with its guard disabled reproduces what a drifted copy would
do:

    MS2: bind_planner_sdk_root's
         `if SDK is not None and "tan.planner" not in sys.modules:` -> `if False:`
         bound @ alp-sdk f1b1c9df, `test_flow_d_manifest_fields.py tests/planner`
         baseline 189 passed  ->  50 passed, 139 errors

That is tan-cli#1044 round 2's shape, arrived at from a stale copy rather than
from a code change.

WHY THE ALLOW-LIST IS SEEDED WITH THIS NAME AND NOT THE FIXTURE'S. The obvious
second seed is `bound_sdk_root`, the autouse fixture that calls the binder. It
is wrong, measured: `tests/conftest.py` defines `_bound_sdk_root`, an
unrelated helper that reports which of `ALP_SDK_PARITY_ROOT` / `ALP_SDK_ROOT`
bound the checkout and returns `tuple[str, Path] | None`. Under this gate's
underscore-folding spelling rule those two are one name with two definitions,
so seeding the fixture would red this gate on day one -- the exact failure
mode the wide walk above was rejected for. Both definitions are legitimate and
neither should move; the fixture simply is not a name this gate can carry.
`test_the_underscore_folding_is_live` below pins that pair, both because it
keeps the reason for the omission executable and because it is the only
standing proof that the folding rule -- the half that catches a copy spelled
`_bind_planner_sdk_root` -- is doing anything at all.

THE tan-cli#1081 SWEEP OF THE OTHER `bound_sdk*` NAMES. The issue asked for a
census of every `bound_sdk*`-shaped binder under `python/tests/**`, not just
`bind_planner_sdk_root`. Folding leading underscores and matching on
substring `bound_sdk` in the name (the same folding this gate already does),
`dev` carried six such folded names:

  * `bound_sdk` -- FIFTEEN module-level definitions, all spelled `_bound_sdk`,
    all byte-identical (`bind_sdk_root(SDK); yield` under
    `@pytest.fixture(autouse=True)`, one with an extra docstring). A genuine
    duplicate by this file's own bar -- same signature, same body, no
    context-specific variation -- so it was consolidated into
    `tests/planner/_bound_sdk_fixture.py` and every consumer now imports it
    for its fixture-registration side effect instead of redefining it. Seeded
    above as `bound_sdk` (the folded spelling; every actual definition is
    written `_bound_sdk`).
  * `bound_sdk_root` -- two definitions (the fixture + `tests/conftest.py`'s
    `_bound_sdk_root`). Already covered above: legitimately distinct, and the
    reason it cannot be seeded is the paragraph just before this one.
  * `needs_bound_sdk`, `test_a_bound_sdk_root_still_ships_the_planner_oracle`,
    `test_jlink_aen_device_fallback_matches_the_bound_sdk_metadata`,
    `warn_when_the_bound_sdk_disagrees_with_the_pins` -- one module-level
    definition each. Nothing to consolidate; each merely happens to contain
    the substring `bound_sdk` (a `pytest.mark.skipif` object, two test
    function names, and conftest's disagreement-warning fixture, in order).
    Recorded here so a future sweep does not re-run this same census from
    scratch only to find the same four singletons again.
"""
from __future__ import annotations

import ast
import functools
import pathlib
import warnings

import pytest

TESTS_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: `{helper: (home module, why one definition matters)}`. Opt-in: a name is in
#: scope for this gate if and only if it is a key here. The reason string is
#: what a failure quotes back, because "duplicate definition" alone does not
#: tell the reader which copy is the real one or what breaks if they drift.
_SHARED_TEST_HELPERS: dict[str, tuple[str, str]] = {
    "bind_planner_sdk_root": (
        "tests/planner/_baremetal_support.py",
        "binds the planner's SDK root BEFORE the first `tan.planner` import in "
        "the session freezes `paths.REPO`/`METADATA_ROOT` for the whole "
        "process; a copy that drifts from this one re-enables tan-cli#1044's "
        "round-2 failure (139 errors under a real bound checkout)",
    ),
    "bound_sdk": (
        "tests/planner/_bound_sdk_fixture.py",
        "is the plain `bind_sdk_root(SDK); yield` autouse fixture that "
        "fifteen real-SDK-gated modules each defined byte-identically "
        "before tan-cli#1081's sweep consolidated them; the risk is "
        "smaller than `bind_planner_sdk_root`'s (there is no fallback "
        "dance to drift), but a lone straggler that stops importing this "
        "module and starts redefining the fixture is exactly the shape "
        "this gate exists to catch, not a hypothetical it is seeded "
        "against speculatively",
    ),
}


@functools.cache
def _module_level_definitions() -> dict[str, tuple[tuple[str, str], ...]]:
    """`{folded name: ((file, spelling), ...)}` for every module-level `def`
    and assignment under `python/tests/**`.

    Folded name = the name with leading underscores stripped, so a private
    re-implementation called `_bind_planner_sdk_root` lands on the same key as
    the shared `bind_planner_sdk_root` it copies. Module level only: a nested
    helper defined inside one test function is not a competing definition of
    anything. Cached because the parametrised cases and the whole-file tests
    would otherwise re-parse the entire test tree once each.
    """
    found: dict[str, list[tuple[str, str]]] = {}
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        rel = path.relative_to(TESTS_ROOT.parent).as_posix()
        with warnings.catch_warnings():
            # A handful of test modules carry a non-raw `"\\S"` in a regex
            # literal. That `SyntaxWarning` belongs to the compilation of
            # those files, which Python does on its own; re-parsing them here
            # would re-raise it with a `<unknown>:25` location and attribute a
            # pre-existing lint in someone else's module to this gate.
            warnings.simplefilter("ignore", SyntaxWarning)
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:
                # `python/tests/**` is not all test modules: it already holds
                # `tests/oracle_captures.py` and the `tests/fixtures/models/
                # gen_*.py` generators, and a malformed fixture added later
                # would otherwise fail this gate with a bare `SyntaxError` and
                # no hint that the file is unrelated to helper duplication.
                raise AssertionError(
                    f"{rel} does not parse, so this gate cannot walk "
                    f"python/tests/** at all: {exc}. Fix the file, or move it "
                    "out of the test tree if it is not meant to be Python."
                ) from exc
        for node in tree.body:
            names: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                # `_x = x` binds a second NAME for one helper; it is the
                # sanctioned way to keep a private name load-bearing, not a
                # second implementation. Anything whose value is a bare name
                # or attribute is that shape. (Same rule as the `tan/**`
                # sibling gate, whose first draft omitted it and reported
                # three definitions of `is_sdk_root` when there is one.)
                if isinstance(node.value, (ast.Name, ast.Attribute)):
                    continue
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                if isinstance(node.target, ast.Name):
                    names = [node.target.id]
            for name in names:
                found.setdefault(name.lstrip("_"), []).append((rel, name))
    return {name: tuple(sites) for name, sites in found.items()}


@pytest.mark.parametrize("helper", sorted(_SHARED_TEST_HELPERS), ids=lambda h: h)
def test_a_shared_test_helper_is_defined_exactly_once(helper):
    home, why = _SHARED_TEST_HELPERS[helper]
    sites = _module_level_definitions().get(helper, ())

    assert sites, (
        f"`{helper}` is defined nowhere under python/tests/. It is supposed to "
        f"live in {home} -- it {why}. If it was renamed or retired, update "
        f"_SHARED_TEST_HELPERS in the same change; an allow-list entry for a "
        f"name that no longer exists enforces nothing and reads as if it does."
    )
    assert len(sites) == 1, (
        f"`{helper}` has {len(sites)} module-level definitions under "
        "python/tests/:\n  "
        + "\n  ".join(f"{rel}: {spelling}" for rel, spelling in sites)
        + f"\n\nIt is owned by {home} -- it {why}. Import it from there instead "
        "of re-implementing it; if a private module-level name is load-bearing, "
        f"alias it (`from tests.planner._baremetal_support import {helper} as "
        f"_{helper}`) rather than redefining, which this walk allows on "
        "purpose. A second copy is byte-identical on the day it lands and "
        "drifts on the day somebody edits one of them -- that is tan-cli#1081, "
        "and the drift it re-enables is tan-cli#1044 round 2."
    )
    rel, _spelling = sites[0]
    assert rel == home, (
        f"`{helper}` is defined once, but in {rel} rather than {home}. "
        "_SHARED_TEST_HELPERS names the home module so a failure can say where "
        "to import from; move the definition back or update the entry."
    )


def test_the_underscore_folding_is_live():
    """Proof that the spelling rule above is load-bearing, and the executable
    record of why `bound_sdk_root` is not in the allow-list.

    A private re-implementation is spelled `_helper`, not `helper` -- so a
    public-name-only walk would miss the whole regression class this file
    exists for. Nothing else here demonstrates the folding actually happens:
    the one seeded helper has a single definition under either spelling, so all
    the parametrised case proves is that a count of one is a count of one.

    `bound_sdk_root` is the standing example of the folding firing, on the real
    tree, with no mutant to maintain: the autouse fixture in
    `tests/planner/_baremetal_support.py` and the unrelated env-reading
    `_bound_sdk_root` in `tests/conftest.py` fold to one name with two
    definitions. Both are legitimate -- the second returns
    `tuple[str, Path] | None` and answers "which variable bound the checkout",
    a different question entirely -- which is precisely why that name cannot be
    seeded, and why this gate is an allow-list rather than a walk.
    """
    sites = _module_level_definitions().get("bound_sdk_root", ())
    files = sorted({rel for rel, _spelling in sites})
    assert files == ["tests/conftest.py", "tests/planner/_baremetal_support.py"], (
        "the `bound_sdk_root` / `_bound_sdk_root` pair this test pins has "
        f"moved -- found {sites}. If one of them was renamed or deleted, the "
        "folding rule needs a different live example (or, if they genuinely "
        "collapsed into one, `bound_sdk_root` can now be seeded into "
        "_SHARED_TEST_HELPERS and this test replaced by that entry)."
    )
    spellings = sorted(spelling for _rel, spelling in sites)
    assert spellings == ["_bound_sdk_root", "bound_sdk_root"], (
        "both halves of the pair are spelled the same way now, so this pins "
        f"nothing about underscore folding -- found {spellings}."
    )


def test_the_allow_list_is_not_empty():
    """Anti-vacuity for the LIST, which is a separate hole from anti-vacuity
    for the walk below.

    Every enforcing assertion in this file is a `parametrize` over
    `_SHARED_TEST_HELPERS`. Emptying that dict does not red anything: pytest
    turns an empty parameter set into a SKIP, `-q` swallows the
    `got empty parameter set` note, and the suite is `2 passed, 1 skipped`,
    rc 0. `empty_parameter_set_mark` is not configured in
    `python/pyproject.toml`, so nothing upgrades that skip to a failure. A
    gate one line from a silent no-op is the shape this repo has been bitten
    by repeatedly (tan-cli#275 most explicitly), so the list gets its own
    non-parametrised assertion.
    """
    assert _SHARED_TEST_HELPERS, (
        "_SHARED_TEST_HELPERS is empty, so the parametrised check above has "
        "no cases and this whole file enforces nothing while still reporting "
        "green. Removing the last entry has to be deliberate: if the seeded "
        "helper was retired, seed the next shared test helper in the same "
        "change or delete this file outright."
    )


def test_the_walk_actually_finds_definitions():
    """Anti-vacuity. Every assertion above counts what the AST walk returned,
    so a walk that silently found nothing -- a moved test root, a glob that
    stopped matching -- would report a pass having measured an empty dict."""
    defs = _module_level_definitions()
    # 5879 folded names on this branch (5732 on `dev` at `8b4e3f43` without
    # this file; the tree has grown and tan-cli#1081's `_bound_sdk`
    # consolidation removed fourteen of those definitions along the way).
    # The floor is deliberately far below either figure, since it is
    # guarding against a walk that collapsed to one subdirectory or to
    # nothing, not ratcheting the tree's size.
    assert len(defs) > 2000, f"only {len(defs)} module-level names found under {TESTS_ROOT}"
    assert "bind_planner_sdk_root" in defs, sorted(defs)[:20]
    assert defs["bind_planner_sdk_root"] == (
        ("tests/planner/_baremetal_support.py", "bind_planner_sdk_root"),
    ), defs["bind_planner_sdk_root"]
