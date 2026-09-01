# SPDX-License-Identifier: Apache-2.0
"""tan-cli#815: a helper `tan/core/shapes.py` owns must not be re-implemented
privately in a command module.

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

WHAT THIS ASSERTS, and why it is name-based. Each shared helper may have
exactly one module-level definition under `python/tan/` -- that scope, and no
other. `python/tests/**` is out of scope here and always was; its narrow
allow-list counterpart is
`tests/gates/test_shared_test_helpers_have_one_definition.py` (tan-cli#1081).
The check counts the
underscore-prefixed spelling as the same helper -- a re-introduced private copy
is called `_is_file`, never `is_file`, so a public-name-only check would miss
the exact regression this file exists for.

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

#: Helpers `tan/core/shapes.py` owns. The value is what a failure should tell
#: the reader to do, because "duplicate definition" alone does not say which
#: copy is the real one.
_OWNED_BY_SHAPES: dict[str, str] = {
    "SDK_MARKER": "the I-31 checkout marker; relocating it must be a one-line change",
    "is_sdk_root": "the loader-marker probe, raise-proof by contract",
    "is_file": "raise-proof `os.path.isfile`, `Path | str`",
    "is_dir": "raise-proof `os.path.isdir`, `Path | str`",
    "rejected_sdk_root_message": "the `<command>.sdk-root-unresolved` text nine commands share",
    "yaml_kind": "the short YAML-ish type name for an error message",
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


@pytest.mark.parametrize("helper", sorted(_OWNED_BY_SHAPES), ids=lambda h: h)
def test_a_shapes_helper_is_defined_exactly_once(helper):
    defs = _module_level_definitions()
    sites = [
        (rel, spelling)
        for spelling in (helper, f"_{helper}")
        for rel in defs.get(spelling, [])
        if (rel, spelling) not in _NOT_THE_SAME_HELPER
    ]

    assert sites, (
        f"`{helper}` is defined nowhere under python/tan/. It is supposed to "
        f"live in tan/core/shapes.py -- {_OWNED_BY_SHAPES[helper]}. If it was "
        f"renamed or retired, update _OWNED_BY_SHAPES in the same change."
    )
    assert len(sites) == 1, (
        f"`{helper}` has {len(sites)} module-level definitions:\n  "
        + "\n  ".join(f"{rel}: {spelling}" for rel, spelling in sites)
        + f"\n\nIt is owned by tan/core/shapes.py -- {_OWNED_BY_SHAPES[helper]}. "
        "Import it from there instead of re-implementing it; if the private "
        "name is load-bearing (an injected predicate, a test import), alias it "
        "with `from tan.core.shapes import x as _x` rather than redefining. "
        "A second copy is byte-identical on the day it lands and drifts on the "
        "day somebody edits one of them (tan-cli#815). If the new one genuinely "
        "answers a DIFFERENT question, declare it in _NOT_THE_SAME_HELPER with "
        "the reason."
    )
    rel, _spelling = sites[0]
    assert rel == "tan/core/shapes.py", (
        f"`{helper}` is defined once, but in {rel} rather than "
        f"tan/core/shapes.py. tan.core imports no command module, so that is "
        f"the only direction that cannot cycle."
    )


def test_the_ownership_list_covers_everything_shapes_actually_owns():
    """Anti-rot for `_OWNED_BY_SHAPES` itself -- the one input this file had
    with neither anti-rot nor anti-vacuity.

    A hand-maintained list protects today's six helpers and nothing added
    tomorrow, which is the tan-cli#275 pattern this file's own docstring
    cites. Demonstrated in review: adding `def is_symlink` to `shapes.py` AND
    a private `def _is_symlink` to `size_cmd.py` left all eight tests green,
    because the new helper was in neither list. Reading the ownership from
    `shapes.py` closes that: a new helper there reds until it is declared.
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

    undeclared = sorted(owned - set(_OWNED_BY_SHAPES))
    assert not undeclared, (
        "tan/core/shapes.py defines these and _OWNED_BY_SHAPES does not claim "
        f"them, so nothing stops a private copy appearing beside a caller:\n  "
        + "\n  ".join(undeclared)
        + "\n\nAdd each with a one-line note saying what it is for -- the note "
        "is what a failure quotes back, and 'duplicate definition' alone does "
        "not tell the reader which copy is the real one."
    )

    retired = sorted(set(_OWNED_BY_SHAPES) - owned)
    assert not retired, (
        "_OWNED_BY_SHAPES claims these and tan/core/shapes.py no longer "
        f"defines them -- drop them in the same change:\n  " + "\n  ".join(retired)
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
    assert defs["SDK_MARKER"] == ("tan/core/shapes.py",), defs["SDK_MARKER"]
