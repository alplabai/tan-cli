# SPDX-License-Identifier: Apache-2.0
"""A hole in the freeze must never resolve to a SKIP (tan-cli#409).

`tests/parity/` keeps discriminating after tan-cli#269 deletes `crates/` only
for the cases whose rust side is a COMMITTED fixture (tan-cli#272). A case
that instead spawns the oracle directly, behind `skipif(RUST is None, ...)`,
turns into a passing skip at that moment: the run stays green and measures
nothing. Measured on this tree, same commit, same host, the only variable
being whether `target/{release,debug}/tan` exists:

    oracle binary present:  261 passed,  95 skipped, 4 xfailed
    no target/ at all:      244 passed, 112 skipped, 4 xfailed   (exit 0)

Seventeen tests moved from passed to skipped and the run still exited 0. That
is the shape this module makes impossible to add QUIETLY. It does not (and
cannot) force a case to be frozen -- some genuinely must not be, and those are
named below with the reason -- it forces the choice to be DECLARED, in source,
in a diff a reviewer sees.

Three registers, because "cannot be frozen" has three distinct reasons and
collapsing them would hide the only one that is temporary:

`_HARNESS_SELF_TESTS`
    The binary is the SUBJECT of the test, not a source of answers. A fixture
    cannot stand in for it and never will. Permanent, and NOT parity coverage.

`_PLATFORM_BOUND`
    A frozen answer would be wrong on every host but the capture host, so
    freezing as-is trades a skip for a false red. Resolvable, but only by
    capturing on `oracle_fixtures.CAPTURE_PLATFORM` and gating the replay on
    that platform -- i.e. by trading a binary-absence skip for a declared
    PLATFORM skip, which is a different and reviewable category.

`_UNFROZEN`
    Debt. Freezable today, not yet captured. Every entry is a case that goes
    silent the moment `crates/` is deleted, and after that it can never be
    captured at all -- so this register is the one that must reach empty
    BEFORE tan-cli#269 lands, not after.

Kept honest in both directions: an undeclared live-only case fails, and so
does a register entry that names a test which no longer exists or is no
longer live-only. A stale exemption is how a register stops describing the
tree it guards.

Location: tan-cli#409 asks for this under `python/tests/gates/`; it landed
here instead, because that directory belonged to another change in flight.
Nothing in the LOGIC depends on where the file sits -- the parity package is
located through the imported module, never through this file's own path -- so
relocating it is the move plus exactly one line: `from . import
oracle_fixtures` becomes `from tests.parity import oracle_fixtures`, the
spelling the existing gates use (`python/tests/` has no `__init__.py`, so a
relative import does not resolve from there).
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from . import oracle_fixtures

#: The parity package itself, found through the module rather than through
#: `__file__`, so this check runs identically from `tests/gates/`.
_PARITY_DIR = Path(oracle_fixtures.__file__).resolve().parent

#: How a fixture key spells its module: `oracle_fixtures._current_key` keys off
#: `PYTEST_CURRENT_TEST`, which is rootdir-relative (`tests/parity/<mod>.py::
#: <test>[<param>]#<n>`).
_KEY_PREFIX = "tests/parity"

#: The condition that makes a case live-only. Matched as SOURCE TEXT rather
#: than evaluated: the point is to find every gate spelled this way, including
#: the ones that reach a test through an intermediate decorator, and evaluating
#: it here would just answer "is a binary present on THIS host".
_LIVE_ONLY_CONDITION = "RUST is None"

#: Permanent exemptions -- the binary is the subject under test. Named
#: individually, per tan-cli#409's own acceptance criterion, so the exemption
#: is reviewable rather than implicit. Neither may be counted as parity
#: coverage: one asserts a typo'd oracle path RAISES instead of skipping, the
#: other exists to print which oracle a green run was measured against.
_HARNESS_SELF_TESTS: dict[str, frozenset[str]] = {
    "test_oracle_parity.py": frozenset(
        {
            "test_a_named_but_missing_rust_binary_is_an_error_not_a_skip",
            "test_rust_oracle_is_present_or_the_suite_says_so",
        }
    ),
}

#: Freezable only ON `oracle_fixtures.CAPTURE_PLATFORM`. Both compare
#: `doctor.checks` (names, positions, statuses) and the whole
#: `inspect.context`, and both are decided by the HOST as much as by the
#: binary: the check list is platform-shaped (`longPaths` exists on Windows
#: and nowhere else) and the statuses read this machine's tool inventory and,
#: on Windows, its registry. `empty_tool_inventory` pins the PATH for the
#: first and cannot pin the platform; the second deliberately runs on the real
#: PATH. Measured while writing this, on darwin, with the oracle's own bundle
#: under `empty_tool_inventory`: the frozen check list would carry
#: `zephyrSdkAvailableForHost pass -- The Zephyr SDK publishes a host build
#: for macos-aarch64`, plus `codeLLDBExtension`/`lldb` rows a Windows bundle
#: does not have at all. Nothing scrubs a host architecture out of a check's
#: `detail`. See `oracle_fixtures/PROVENANCE.txt` for why the capture-time
#: leak guard does NOT catch this and must not be relied on to.
_PLATFORM_BOUND: dict[str, frozenset[str]] = {
    "test_support_bundle_oracle_parity.py": frozenset(
        {
            "test_support_bundle_matches_the_oracle_on_a_failing_host",
            "test_support_bundle_matches_the_oracle_with_a_resolved_sdk",
        }
    ),
}

#: DEBT, not design -- and the `test_oracle_parity.py` half of it is PAID as
#: of 2026-08-04.
#:
#: That half held seven cases whose recorded blocker was file ownership, not
#: the cases themselves: the module was said to belong to tan-cli#408, so
#: converting them from a concurrent change would clobber it. The reason did
#: not survive reading #408, whose scope is the six `python/tan/` modules
#: over the 800-line cap -- it names the oversized test files as an
#: observation and delivers nothing in them. Nothing owned the file, so all
#: seven now replay from `oracle_fixtures/test_oracle_parity.json`: the four
#: whose assertions read a PATH through `test_oracle_parity._both_sides`, the
#: three that read only a `command`/exit/issue code through
#: `rust_run(..., scrub_roots=())`. `PROVENANCE.txt` records the capture run
#: and why darwin was safe for it.
#:
#: What is left here is genuinely a different shape, not the same debt
#: shrunk: the scaffold pair below compares a FILE TREE, which no fixture
#: store in this package can hold yet.
#: EMPTY as of the two freezes above landing together (tan-cli#409). Both
#: entries that lived here are gone for the same reason -- the recorded
#: blocker did not survive being measured:
#:
#: - `test_oracle_parity.py`'s seven were said to belong to tan-cli#408, so
#:   converting them concurrently would clobber it. #408's scope is the six
#:   `python/tan/` modules over the 800-line cap; it names the oversized test
#:   files as an observation and delivers nothing in them.
#: - the scaffold pair was said to need "a tree-shaped fixture store that does
#:   not exist yet". Measured, the oracle side is six templates, 40 files,
#:   every one valid UTF-8, 120 KiB of JSON -- frozen per template in
#:   `oracle_fixtures/scaffold_trees.json`.
#:
#: Kept as an empty dict rather than deleted: this is the register a future
#: live-only case declares itself in, and the gate below reads all three.
#: A new entry here is a debt someone chose to take, which is reviewable; a
#: case gated live with no entry is the silent hole this module exists to
#: refuse.
_UNFROZEN: dict[str, frozenset[str]] = {}

_REGISTERS = (
    ("harness self-test", _HARNESS_SELF_TESTS),
    ("platform-bound", _PLATFORM_BOUND),
    ("unfrozen debt", _UNFROZEN),
)


def _gate_names(tree: ast.Module) -> set[str]:
    """Module-level names that carry the live-only gate.

    Three spellings are in the tree today and all three must be found, because
    a detector that sees only the literal decorator would report the file with
    the MOST live-only cases as clean:

    * `_ORACLE_REQUIRED = pytest.mark.skipif(RUST is None, ...)` -- a mark
      bound to a name (`test_run_oracle_parity.py`);
    * `def _oracle_required(fn): ... pytest.mark.skipif(RUST is None, ...)`
      then `_ORACLE_REQUIRED = _oracle_required` -- a decorator FUNCTION, then
      an alias to it (`test_oracle_parity.py`);
    * the condition written inline on the test itself.

    Source text, not evaluation: `RUST` is `None` on a host with no oracle, so
    evaluating would make this check answer a question about the host.
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and _LIVE_ONLY_CONDITION in ast.unparse(node):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            text = ast.unparse(node.value)
            if _LIVE_ONLY_CONDITION in text or text in names:
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _root_name(node: ast.expr) -> str:
    """The leftmost identifier of a decorator expression -- `_ORACLE_REQUIRED`
    for both `@_ORACLE_REQUIRED` and `@_ORACLE_REQUIRED(...)`."""
    while isinstance(node, (ast.Call, ast.Attribute)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else ""


def _live_only_tests(path: Path) -> set[str]:
    """Every test in *path* that skips when no oracle binary is present."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    gates = _gate_names(tree)
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        decorated = any(
            _root_name(dec) in gates or _LIVE_ONLY_CONDITION in ast.unparse(dec)
            for dec in node.decorator_list
        )
        # `test_rust_oracle_is_present_or_the_suite_says_so` gates in its own
        # BODY (`if RUST is None: pytest.skip(...)`), not with a mark. Same
        # hole, so the same register has to cover it.
        if decorated or _LIVE_ONLY_CONDITION in ast.unparse(node):
            found.add(node.name)
    return found


def _frozen_test_names(module: str) -> set[str]:
    """Test names with at least one committed fixture key, from
    `oracle_fixtures/<module>.json`. Parametrisation and the per-call counter
    are stripped: one key for any of a test's calls proves it replays."""
    path = oracle_fixtures.FIXTURES_DIR / f"{module}.json"
    if not path.exists():
        return set()
    keys = json.loads(path.read_text(encoding="utf-8"))
    return {key.split("::", 1)[1].split("#")[0].split("[")[0] for key in keys if "::" in key}


#: THIS file, exempted explicitly rather than by writing the condition in a
#: way the detector happens to miss -- the same choice
#: `tests/gates/test_one_oracle_resolver.py` records for its own scan. It
#: quotes `RUST is None` twice on purpose: once as the pattern it searches
#: for, once inside the planted module below that proves the search works. An
#: exemption a reader can see beats an evasion they cannot.
_THE_CHECK_ITSELF = Path(__file__).resolve()


def _parity_modules() -> list[Path]:
    return sorted(p for p in _PARITY_DIR.glob("test_*.py") if p != _THE_CHECK_ITSELF)


def _declared(module_name: str) -> dict[str, str]:
    """Every declared exemption for *module_name*, as ``{test: register}``."""
    return {
        test: label
        for label, register in _REGISTERS
        for test in register.get(module_name, frozenset())
    }


@pytest.mark.parametrize("path", _parity_modules(), ids=lambda p: p.name)
def test_every_live_only_case_is_frozen_or_declared(path: Path):
    """The check that would have caught both of tan-cli#409's holes.

    A case gated on binary PRESENCE is a silent zero-coverage hole the day
    `crates/` goes away. Freeze it (route its rust side through
    `oracle.rust_run`/`oracle.compare` and capture), or name it in one of the
    three registers above with the reason.

    tan-cli#409 words the criterion as "gated on `RUST is None` AND has no
    committed fixture key". This checks the wider thing -- gated at all,
    fixture or not -- because the narrow form has a laundering path: a test
    that acquires a fixture key for ONE of its calls while keeping the
    presence gate still skips on a host with no binary, and would then be
    reported clean by the very check meant to find it. Nothing in the tree is
    in that state today (measured), which is the moment to close it. The
    message tells the two apart, since the fix differs: drop the gate, versus
    capture a fixture first.
    """
    live_only = _live_only_tests(path)
    frozen = _frozen_test_names(path.stem)
    declared = _declared(path.name)
    offenders = sorted(live_only - set(declared))
    detail = {
        name: "has a fixture already -- drop the gate" if name in frozen else "no fixture"
        for name in offenders
    }
    assert offenders == [], (
        f"{path.name}: {detail} skip when no oracle binary is present, so each becomes "
        "a passing SKIP the moment tan-cli#269 deletes crates/ -- a green run measuring "
        "nothing. Route the rust side through oracle.rust_run/compare and capture "
        "(TAN_PARITY_LIVE=1 TAN_PARITY_CAPTURE=1, recipe in "
        f"{oracle_fixtures.FIXTURES_DIR.name}/PROVENANCE.txt), or declare it in "
        "_HARNESS_SELF_TESTS/_PLATFORM_BOUND/_UNFROZEN with the reason (tan-cli#409)."
    )


@pytest.mark.parametrize("path", _parity_modules(), ids=lambda p: p.name)
def test_no_declared_exemption_has_gone_stale(path: Path):
    """A register entry naming a test that no longer exists, or that is no
    longer live-only, is an exemption still being granted for a case that no
    longer needs it -- and the next reader takes the list at its word. Freezing
    a case must therefore SHRINK its register, not leave a tombstone."""
    live_only = _live_only_tests(path)
    stale = sorted(
        f"{test} ({register})" for test, register in _declared(path.name).items()
        if test not in live_only
    )
    assert stale == [], (
        f"{path.name}: {stale} are declared live-only exemptions but are not gated on "
        f"`{_LIVE_ONLY_CONDITION}` any more (renamed, deleted, or frozen). Drop them "
        "from the register -- tan-cli#409's registers are only worth reading while "
        "they describe the tree."
    )


def test_the_detector_sees_a_gate_reached_through_a_decorator_function(tmp_path):
    """The detector's own load-bearing case, planted rather than assumed.

    `test_oracle_parity.py` -- the file with the most live-only cases -- does
    not spell the condition on any test. It builds a decorator FUNCTION that
    applies the mark, aliases it, and decorates with the alias. A detector
    matching only `@pytest.mark.skipif(RUST is None, ...)` reports that file
    as clean, which is the exact false-green this module exists to prevent.
    """
    module = tmp_path / "test_planted.py"
    module.write_text(
        "import pytest\n"
        "RUST = None\n"
        "def _oracle_required(fn):\n"
        "    return pytest.mark.skipif(RUST is None, reason='needs a built Rust tan')(fn)\n"
        "_ALIAS = _oracle_required\n"
        "@_ALIAS\n"
        "def test_reached_through_the_alias():\n"
        "    pass\n"
        "def test_not_gated_at_all():\n"
        "    pass\n",
        encoding="utf-8",
        newline="\n",
    )
    assert _live_only_tests(module) == {"test_reached_through_the_alias"}
