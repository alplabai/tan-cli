# SPDX-License-Identifier: Apache-2.0
"""tan-cli#815: a shared helper must not be re-implemented privately in a
command module, wherever it is homed.

`shapes.py` exists because `_is_sdk_root` had three copies and `_yaml_kind`
two, and the copies had drifted in TYPE -- "which is exactly how a 'same'
helper stops being the same one" (that module's own docstring). The dedup then
stopped half-done for four releases:

  * `rejected_sdk_root_message` and `SDK_MARKER` each had TWO live definitions,
    one in `shapes.py` and one in `sdk_cmd.py`, with a NOTE in the first asking
    for the collapse and a comment in the second claiming the marker was
    "Spelled once here". Nine commands split down the middle over which copy
    they imported.
  * `_is_file` had FOUR: three byte-identical `str` versions and a
    `bootstrap_cmd` one that had drifted in TYPE -- `Path` instead of `str`.
    (It also caught only `OSError`; that half was inert, because `pathlib`
    catches `ValueError` inside `Path.is_file()`.)

Every duplicate was byte-identical at the time, so nothing misbehaved and no
test could tell. That is the whole hazard: the cost is paid later, by the edit
that reaches one copy and not the other, on a refusal message nine commands
share and on the I-31 checkout marker.

## From "owned by shapes.py" to a `{name: home}` map (tan-cli#1091)

Through tan-cli#1091 this file could only express "owned by
`tan/core/shapes.py`" -- the per-helper assertion hard-coded that path, so a
helper deliberately homed anywhere ELSE could not join this gate at all. That
bit PR #1090 directly: it moved `validate`'s board.yaml resolver into a new
`tan/core/board_context.py` so `scaffold` could share it -- a real
one-definition move, verified by hand -- but nothing MECHANICAL held it there;
a second private `_resolve_board_path` the next day would have left every test
in this file green. `_SHARED_HELPERS` below generalises the check to
`{name: (home module, why one definition matters)}` for exactly that reason.
The six pre-existing entries still point at `tan/core/shapes.py`, unchanged in
behaviour; `_SHARED_HELPERS` now also carries `resolve_board_path`, homed at
`tan/core/board_context.py` -- the seed tan-cli#1091 was written for, added in
the same change that rebased onto PR #1090's merge. See the comment above the
dict for how the one genuinely different lookalike (`generate_cmd.py`'s
private copy) is carved out rather than folded in.

WHAT THIS ASSERTS, and why it is name-based, and why it is opt-in. For each
name in `_SHARED_HELPERS` below -- an explicit allow-list -- there is exactly
one module-level definition anywhere under `python/tan/`, and it is in the
module that entry declares as its home. `python/tests/**` is out of scope
here and always was; its narrow allow-list counterpart is
`tests/gates/test_shared_test_helpers_have_one_definition.py` (tan-cli#1081).
The check counts the underscore-prefixed spelling as the same helper -- a
re-introduced private copy is called `_is_file`, never `is_file`, so a
public-name-only check would miss the exact regression this file exists for.

WHAT THIS DOES NOT ASSERT, deliberately. It says nothing about any name not
in `_SHARED_HELPERS`. A helper duplicated across two `tan/**` modules under a
name nobody has seeded here is invisible to this gate and stays invisible
until somebody adds it -- there is no walk over `tan/**` NAMES at large (the
AST walk itself, `_module_level_definitions()`, covers every file under
`python/tan/**` on every run; what is narrow is which of its findings this
file acts on) and no heuristic that promotes a name into scope.

That narrowness is measured, not assumed. Run over `python/tan/**` -- this
file's own tree, with this file's own node-shape rules -- re-derived after
rebasing onto #1090's merge and adding the `resolve_board_path` seed below:
still 91 names have more than one module-level definition, 232 definitions in
total.
A blanket version of this check, asserting one definition for every name the
walk returns instead of the opt-in `_SHARED_HELPERS` list, is red on the day
it lands over THIS tree, not just some other one. (PR #1083's sibling gate
made the same case first, over the DIFFERENT tree `python/tests/**`: 173
names / 725 definitions at `dev` `8b4e3f43`, 176 names / 700 definitions at
this tip -- cited here as the sibling gate's own tree and commit, not this
file's, because both trees drift and a number belongs to the tree and commit
it was measured on.) A gate that is red the day it lands gets disabled, so
`_SHARED_HELPERS` stays a hand-written, opt-in dict for the same reason
#1083's `_SHARED_TEST_HELPERS` is.

ALIASING IS NOT A DEFINITION, in either spelling, and both must stay allowed:

    from tan.core.shapes import is_file as _is_file   # flash_cmd, size_cmd
    _is_sdk_root = is_sdk_root                        # flash_cmd

`flash_cmd._is_file` is passed as an injected predicate at four call sites and
imported by two tests, so the private module-level name has to survive the
dedup; `_is_sdk_root = is_sdk_root` is the same move tan-cli#408 already made
(`build_cmd.py` carried this same alias until tan-cli#408's ladder
extraction removed its last internal use, at which point the call site was
rewritten to the public `is_sdk_root` directly rather than keeping an alias
nothing else needed).
The first form binds a name without a statement the walk sees at all; the
second is an `Assign` whose value is a bare `Name`, which is why the walk
below skips that shape. A first draft of this gate did not, and reported three
definitions of `is_sdk_root` when there is one and two aliases.
"""
from __future__ import annotations

import ast
import functools
import pathlib

import pytest

TAN_ROOT = pathlib.Path(__file__).resolve().parents[2] / "tan"

#: Shared helpers this gate protects, opt-in by name --
#: `{name: (home module, why one definition matters)}`. The first element of
#: the value is where a failure should say the real copy lives; the second is
#: what a failure should say WHY, because "duplicate definition" alone does
#: not tell the reader which copy is the real one.
#:
#: The first six entries below are owned by `tan/core/shapes.py`, unchanged
#: from before this file generalised past a single hard-coded home
#: (tan-cli#1091).
#:
#: `resolve_board_path` is the seed tan-cli#1091 was written for: PR #1090
#: moved `validate_cmd.py`'s private `_resolve_board_path` to the new
#: `tan/core/board_context.py` so `tan scaffold` could share it, rather than
#: growing a SEVENTH project/board resolver (`board_context.py`'s own
#: docstring names the other five and explains why they stay separate). Its
#: `validate_cmd.py` call site now reads
#: `from tan.core.board_context import resolve_board_path as _resolve_board_path`
#: -- an `ImportFrom`, not a definition, so this file's AST walk never sees it
#: at all (see ALIASING IS NOT A DEFINITION above). Verified against the real
#: merged tree, not assumed: `tan/commands/generate_cmd.py:938` is the ONLY
#: other module-level site either spelling resolves to, and it answers a
#: genuinely different question (see `_NOT_THE_SAME_HELPER` below) -- so this
#: seed needs exactly one carve-out, not the two it would have needed against
#: `validate_cmd.py`'s old private copy.
_SHARED_HELPERS: dict[str, tuple[str, str]] = {
    "SDK_MARKER": (
        "tan/core/shapes.py",
        "the I-31 checkout marker; relocating it must be a one-line change",
    ),
    "is_sdk_root": (
        "tan/core/shapes.py",
        "the loader-marker probe, raise-proof by contract",
    ),
    "is_file": (
        "tan/core/shapes.py",
        "raise-proof `os.path.isfile`, `Path | str`",
    ),
    "is_dir": (
        "tan/core/shapes.py",
        "raise-proof `os.path.isdir`, `Path | str`",
    ),
    "rejected_sdk_root_message": (
        "tan/core/shapes.py",
        "the `<command>.sdk-root-unresolved` text nine commands share",
    ),
    "yaml_kind": (
        "tan/core/shapes.py",
        "the short YAML-ish type name for an error message",
    ),
    "resolve_board_path": (
        "tan/core/board_context.py",
        "turns `--project`/`--board-yaml` into the `board.yaml` path "
        "`validate` and `scaffold` both read; a private re-implementation "
        "re-opens the drift #1090 closed (alp-sdk-vscode#601/#633 shipped "
        "modules that never compiled from exactly this kind of second copy)",
    ),
}

#: Definitions that LOOK like a second copy and are not. Each entry names a
#: different question, not a duplicate answer -- kept here rather than silently
#: narrowing the check, because an unexplained exemption is how the previous
#: dedup stalled.
_NOT_THE_SAME_HELPER: dict[tuple[str, str], str] = {
    ("tan/commands/presets_cmd.py", "_is_dir"): (
        "takes an `os.DirEntry`, not a path, and FOLLOWS symlinks to match the "
        "oracle's `Path::is_dir()`. A different predicate with its own "
        "documented divergence"
    ),
    ("tan/commands/generate_cmd.py", "_resolve_board_path"): (
        "answers `(board_yaml, workspace_root) -> Path` via pathlib, always "
        "returning a path that EXISTS-or-not against `workspace_root` -- a "
        "different question from `board_context.resolve_board_path`'s "
        "`(project, board_yaml) -> (str, str)`, which joins as strings on "
        "purpose so the leading `./` the conformance fixtures pin survives. "
        "`tan/commands/validate_cmd.py`'s former copy of this same private "
        "name is not a THIRD site: PR #1090 replaced it with `from "
        "tan.core.board_context import resolve_board_path as "
        "_resolve_board_path`, an `ImportFrom` this file's AST walk never "
        "sees as a definition"
    ),
}


@functools.cache
def _module_level_definitions() -> dict[str, tuple[str, ...]]:
    """`{name: [file, ...]}` for every module-level `def` and assignment under
    `python/tan/`. Module level only: a nested helper inside one function is
    not a competing definition of anything."""
    found: dict[str, list[str]] = {}
    # Cached: six parametrised cases plus three whole-file tests would
    # otherwise re-parse all of python/tan/ nine times over.
    for path in sorted(TAN_ROOT.rglob("*.py")):
        rel = path.relative_to(TAN_ROOT.parent).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            names: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                # `_x = x` binds a second NAME for one function; it is the
                # sanctioned way to keep a private name load-bearing after a
                # dedup, not a second implementation. Anything whose value is
                # a bare name or attribute is that shape.
                if isinstance(node.value, (ast.Name, ast.Attribute)):
                    continue
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                if isinstance(node.target, ast.Name):
                    names = [node.target.id]
            for name in names:
                found.setdefault(name, []).append(rel)
    return {name: tuple(rels) for name, rels in found.items()}


@pytest.mark.parametrize("helper", sorted(_SHARED_HELPERS), ids=lambda h: h)
def test_a_shared_helper_is_defined_exactly_once(helper):
    home, why = _SHARED_HELPERS[helper]
    defs = _module_level_definitions()
    sites = [
        (rel, spelling)
        for spelling in (helper, f"_{helper}")
        for rel in defs.get(spelling, [])
        if (rel, spelling) not in _NOT_THE_SAME_HELPER
    ]

    assert sites, (
        f"`{helper}` is defined nowhere under python/tan/. It is supposed to "
        f"live in {home} -- {why}. If it was renamed or retired, update "
        f"_SHARED_HELPERS in the same change."
    )
    assert len(sites) == 1, (
        f"`{helper}` has {len(sites)} module-level definitions:\n  "
        + "\n  ".join(f"{rel}: {spelling}" for rel, spelling in sites)
        + f"\n\nIt is owned by {home} -- {why}. "
        "Import it from there instead of re-implementing it; if the private "
        "name is load-bearing (an injected predicate, a test import), alias it "
        f"with `from {home.removesuffix('.py').replace('/', '.')} import x as "
        "_x` rather than redefining. A second copy is byte-identical on the day "
        "it lands and drifts on the day somebody edits one of them "
        "(tan-cli#815). If the new one genuinely answers a DIFFERENT "
        "question, declare it in _NOT_THE_SAME_HELPER with the reason."
    )
    rel, _spelling = sites[0]
    assert rel == home, (
        f"`{helper}` is defined once, but in {rel} rather than {home}. "
        f"_SHARED_HELPERS names {home} as its home -- move the definition "
        "back or update the entry. A legal home must not itself import from "
        "`tan/commands/`, the constraint `tan/core/shapes.py` satisfies today "
        "-- otherwise a command module importing the shared helper can cycle "
        "back through its own package."
    )


def test_the_ownership_list_covers_everything_shapes_actually_owns():
    """Anti-rot for the `tan/core/shapes.py`-homed slice of `_SHARED_HELPERS`
    -- the one input this file had with neither anti-rot nor anti-vacuity.

    A hand-maintained list protects today's six `shapes.py` helpers and
    nothing added tomorrow, which is the tan-cli#275 pattern this file's own
    docstring cites. Demonstrated in review: adding `def is_symlink` to
    `shapes.py` AND a private `def _is_symlink` to `size_cmd.py` left all
    eight tests green, because the new helper was in neither list. Reading
    the ownership from `shapes.py` -- a real source, not a literal copied by
    hand -- closes that: a new helper there reds until it is declared.

    Scoped to `shapes.py` on purpose: `_SHARED_HELPERS` entries homed
    elsewhere (once any exist) point at a module that is not "everything
    shared lives here" the way `shapes.py` is, so there is no equivalent
    exhaustiveness claim to make about them here.
    """
    shapes = ast.parse(
        (TAN_ROOT / "core" / "shapes.py").read_text(encoding="utf-8")
    )
    owned = set()
    for node in shapes.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owned.add(node.name)
        elif isinstance(node, ast.Assign):
            if isinstance(node.value, (ast.Name, ast.Attribute)):
                continue
            owned.update(t.id for t in node.targets if isinstance(t, ast.Name))
    # Module-private helpers of `shapes.py` itself are not shared surface.
    owned = {name for name in owned if not name.startswith("_")}

    declared_for_shapes = {
        name
        for name, (home, _why) in _SHARED_HELPERS.items()
        if home == "tan/core/shapes.py"
    }

    undeclared = sorted(owned - declared_for_shapes)
    assert not undeclared, (
        "tan/core/shapes.py defines these and _SHARED_HELPERS does not claim "
        f"them, so nothing stops a private copy appearing beside a caller:\n  "
        + "\n  ".join(undeclared)
        + "\n\nAdd each with a one-line note saying what it is for -- the note "
        "is what a failure quotes back, and 'duplicate definition' alone does "
        "not tell the reader which copy is the real one."
    )

    retired = sorted(declared_for_shapes - owned)
    assert not retired, (
        "_SHARED_HELPERS claims these are owned by tan/core/shapes.py and "
        f"tan/core/shapes.py no longer defines them -- drop them in the same "
        "change:\n  " + "\n  ".join(retired)
    )


def test_the_declared_lookalikes_still_exist():
    """Anti-rot for `_NOT_THE_SAME_HELPER`. An exemption for a definition that
    was since deleted or renamed reads as a live carve-out and quietly widens
    what the check above tolerates."""
    defs = _module_level_definitions()
    stale = sorted(
        f"{rel}: {name}"
        for (rel, name), _reason in _NOT_THE_SAME_HELPER.items()
        if rel not in defs.get(name, [])
    )
    assert not stale, (
        "these declared lookalikes no longer exist -- drop them from "
        "_NOT_THE_SAME_HELPER in the same change that removed them:\n  "
        + "\n  ".join(stale)
    )


def test_the_walk_actually_finds_definitions():
    """Anti-vacuity. Every assertion above counts what the AST walk returned,
    so a walk that silently found nothing -- a moved package root, a glob that
    stopped matching -- would report six passes having measured an empty dict."""
    defs = _module_level_definitions()
    assert len(defs) > 500, f"only {len(defs)} module-level names found under {TAN_ROOT}"
    assert "SDK_MARKER" in defs, sorted(defs)[:20]
    # Read the expected home from _SHARED_HELPERS itself, not a literal --
    # relocating SDK_MARKER and updating the map correctly should never make
    # THIS assertion the one that reds; it still would, with a message about
    # the walk rather than the map, if the walk itself broke.
    assert defs["SDK_MARKER"] == (_SHARED_HELPERS["SDK_MARKER"][0],), defs["SDK_MARKER"]
