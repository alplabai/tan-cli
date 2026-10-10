# SPDX-License-Identifier: Apache-2.0
"""Gate: a private SDK-resolution wrapper must not collapse to `None`.

tan-cli#900, the sixth and seventh instance of a shape tan-cli#468 first fixed
in `presets_cmd.resolve_sdk`: `examples_cmd._resolve_sdk` and
`generate_cmd._resolve_sdk_root` each defined their OWN dataclass carrying
`broken_project_pin`/`foreign_global_default_for` -- the two facts
`resolve_sdk_root_wide`/`resolve_sdk_root_ladder` already answer with, even
when nothing resolved -- and then wrapped it `SomeDataclass | None`, `return
None`-ing the moment `.path` came back empty. That discarded the very facts
the dataclass exists to carry, on exactly the path they matter most: a
workspace whose `.alp/sdk-path` pin is broken and has nothing to fall through
to reported `<command>.sdk-root-unresolved` alone, with no
`sdk.project-pin-unresolved` alongside it -- the customer was told the SDK
could not be resolved but not that their own project pin was the broken
thing.

WHY THIS SHAPE AND NOT A REACHABILITY GATE. The obvious gate -- walk every
command, fail when one resolves an SDK and does not emit
`sdk.project-pin-unresolved` -- cannot be written soundly here, for the exact
reason `test_sdk_info_is_built_from_a_resolution.py` gives for its own
sibling defect (tan-cli#478): "resolves an SDK" flows through several entry
points, module-level presence of a call is trivially satisfied while an
early-return path stays silent, and a gate that cannot see control flow
passes on exactly the silence it was written to catch.

This gate is a LITERAL/CONSTRUCTOR check instead, the shape that family's own
docstring says works in this repo: any dataclass under `python/tan/` that
declares a `broken_project_pin` field is claiming to carry that fact. A
function returning `ThatDataclass | None` is where the claim gets broken --
`None` has no `.broken_project_pin` to read, so every caller's `is None`
branch loses the fact by construction, regardless of what the function body
does. Checking the SIGNATURE catches the defect the moment the type is
wrong, without having to prove anything about reachability.

`init_cmd._Sdk` is not exempted by name here -- it is excluded structurally,
which is the point. It deliberately does NOT declare `broken_project_pin`
(see its own docstring: `init` WRITES `.alp/sdk-path`, so a broken pin
already sitting in the parent workspace is superseded by this run's own
resolution, not something for `init` itself to disclose) and so never enters
`_resolution_class_names()` below -- a real design choice recorded once, in
the class that makes it, rather than a second time in an exemption list here
that could drift from it.

WHY TREE-WIDE, NOT `commands/*.py` (tan-cli#922 review). The original scan
was `PACKAGE.glob("*.py")` -- non-recursive (so `commands/build/execute.py`'s
whole subpackage went unscanned) and per-file (so a resolution-wrapper class
declared in one module and returned `| None` from ANOTHER -- e.g. a class in
`doctor_cmd.py` re-exported and wrapped in `size_cmd.py` -- was invisible,
since that file's own module body never declares the class). Both are fixed
by scanning `python/tan/` with `rglob` and collecting `resolution_classes` as
one set across every file BEFORE scanning any file's returns, rather than
per-file.

WHY MORE ANNOTATION SHAPES THAN `X | None` (tan-cli#922 review). The repo
runs no mypy and no ruff in CI (`python/pyproject.toml:215-229` says so in
so many words), so nothing forces an annotation to look any particular way,
or to exist at all. `_optional_of_name` below recognises `X | None`,
`None | X`, `Optional[X]`/`typing.Optional[X]`, `Union[X, None]`/
`typing.Union[X, None]`, a string forward-ref annotation (`"X | None"`), and
a module-level `TypeAlias` name that itself resolves to one of those shapes
(`_MaybeSdk = _ResolvedSdk | None`, then `-> _MaybeSdk` elsewhere). `async
def` is walked the same as `def`.

The realistic eighth instance is narrower than any of those, though: an
UNANNOTATED function is invisible to every one of the shape checks above,
annotation or no. Flagging every unannotated function tree-wide is too
broad to be a signature check -- 18 exist in this tree today, and 14 of them
have nothing to do with SDK resolution at all (a PTY reader callback, a CSV
splitter, a YAML loader hook, ...). So this checks the actual SHAPE of the
historical bug instead, on unannotated functions only: a return path that
constructs one of the resolution classes, and a *separate* return path that
is bare `None`, in the same function body -- the exact two branches
tan-cli#900's fix collapsed into one. Measured zero matches against this
tree today; the mutation tests below prove it fires on the shape the day it
reappears.
"""
from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]
PACKAGE = REPO / "python" / "tan"


def _class_field_names(node: ast.ClassDef) -> list[str]:
    return [
        target.id
        for target in (stmt.target for stmt in node.body if isinstance(stmt, ast.AnnAssign))
        if isinstance(target, ast.Name)
    ]


def _is_none_constant(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _dotted_name(node: ast.expr) -> str | None:
    """`Optional`/`Union` (bare import) or `typing.Optional`/`typing.Union`
    (qualified) -> the bare name they end in. Anything else -> `None`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _plain_name(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """A bare `ast.Name` -> its id, resolved one level through `aliases` if
    it names a `TypeAlias`. Anything else (a subscript, a binop, ...) ->
    `None` -- this codebase's resolution wrappers are always a single class
    name, never a further-nested generic."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    return None


def _optional_of_name(annotation: ast.expr, aliases: dict[str, str] | None = None) -> str | None:
    """`X | None` (either order), `Optional[X]`/`typing.Optional[X]`,
    `Union[X, None]`/`typing.Union[X, None]`, a string forward-ref
    (`"X | None"`), or a `TypeAlias` name that itself resolves to one of
    those (via `aliases`) -> `"X"`. Anything else (a bare `X`, `X | Y` with
    no `None` side, an unrecognised subscript) -> `None`."""
    aliases = aliases if aliases is not None else {}

    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            parsed = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return None
        return _optional_of_name(parsed, aliases)

    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        sides = (annotation.left, annotation.right)
        if any(_is_none_constant(side) for side in sides):
            other = next(side for side in sides if not _is_none_constant(side))
            return _plain_name(other, aliases)
        return None

    if isinstance(annotation, ast.Subscript):
        base = _dotted_name(annotation.value)
        sl = annotation.slice
        elts = sl.elts if isinstance(sl, ast.Tuple) else [sl]
        if base == "Optional" and len(elts) == 1:
            return _plain_name(elts[0], aliases)
        if base == "Union":
            non_none = [e for e in elts if not _is_none_constant(e)]
            if len(non_none) == 1 and len(non_none) < len(elts):
                return _plain_name(non_none[0], aliases)
        return None

    if isinstance(annotation, ast.Name):
        return aliases.get(annotation.id)

    return None


def _module_optional_aliases(tree: ast.Module) -> dict[str, str]:
    """`_MaybeSdk = _ResolvedSdk | None` (a `TypeAlias`, annotated or not)
    at module scope -> `{"_MaybeSdk": "_ResolvedSdk"}`, so a function later
    declared `-> _MaybeSdk` is still caught by `_optional_of_name`."""
    aliases: dict[str, str] = {}
    for node in tree.body:
        target: str | None = None
        value: ast.expr | None = None
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0].id, node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            target, value = node.target.id, node.value
        if target is None or value is None:
            continue
        wrapped = _optional_of_name(value, {})
        if wrapped is not None:
            aliases[target] = wrapped
    return aliases


def _constructs_resolution_class(value: ast.expr, resolution_classes: set[str]) -> bool:
    for sub in ast.walk(value):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            if sub.func.id in resolution_classes:
                return True
    return False


def _returns_bare_none_and_a_resolution_class(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, resolution_classes: set[str]
) -> bool:
    """The realistic eighth instance (tan-cli#922 review): the same shape
    tan-cli#900 fixed, minus a return annotation entirely -- see the module
    docstring's "WHY MORE ANNOTATION SHAPES" section for why this is a
    narrower, shape-specific check rather than "any unannotated function"."""
    has_bare_none = False
    has_class_construction = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return):
            continue
        if node.value is None or _is_none_constant(node.value):
            has_bare_none = True
        elif _constructs_resolution_class(node.value, resolution_classes):
            has_class_construction = True
    return has_bare_none and has_class_construction


def _resolution_class_names(paths: list[pathlib.Path]) -> set[str]:
    """Tree-wide, not per-file (tan-cli#922 review): a resolution class
    declared in one module and returned `| None` from another must still be
    caught, so the set of known class NAMES has to be built across every
    scanned file before any file's returns are checked."""
    names: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and "broken_project_pin" in _class_field_names(node):
                names.add(node.name)
    return names


def _scan_tree(tree: ast.Module, resolution_classes: set[str]) -> list[str]:
    """`["<name> -> <Class> | None (line N)", ...]` for every (possibly
    `async`) function in `tree` whose return annotation resolves (through
    `_optional_of_name`) to a resolution class, plus every unannotated
    function that constructs one and also has a bare `return None`."""
    aliases = _module_optional_aliases(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.returns is None:
            if _returns_bare_none_and_a_resolution_class(node, resolution_classes):
                found.append(
                    f"{node.name} -> (unannotated; constructs a resolution class on one "
                    f"path, bare `None` on another) (line {node.lineno})"
                )
            continue
        wrapped = _optional_of_name(node.returns, aliases)
        if wrapped in resolution_classes:
            found.append(f"{node.name} -> {wrapped} | None (line {node.lineno})")
    return found


def _optional_resolution_wrapper_returns(
    path: pathlib.Path, resolution_classes: set[str]
) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _scan_tree(tree, resolution_classes)


def test_no_sdk_resolution_wrapper_is_returned_as_optional() -> None:
    paths = sorted(PACKAGE.rglob("*.py"))
    resolution_classes = _resolution_class_names(paths)

    offenders: dict[str, list[str]] = {}
    for path in paths:
        sites = _optional_resolution_wrapper_returns(path, resolution_classes)
        if sites:
            offenders[path.relative_to(REPO / "python").as_posix()] = sites

    assert offenders == {}, (
        "these functions return an SDK-resolution dataclass as `X | None` (or an "
        "unannotated equivalent), which discards `.broken_project_pin`/"
        "`.foreign_global_default_for` on exactly the path -- nothing resolved -- "
        "where they matter most (tan-cli#900, tan-cli#468, tan-cli#922):\n  "
        + "\n  ".join(f"{mod}: {site}" for mod, sites in sorted(offenders.items()) for site in sites)
        + "\n\nMake `.path` (or the equivalent field) the `X | None` instead, "
        "and always return the dataclass -- the shape `examples_cmd._resolve_sdk`"
        " / `generate_cmd._resolve_sdk_root` / `flash_cmd._resolve_sdk` all use "
        "post-tan-cli#900. If this dataclass genuinely must not carry "
        "`broken_project_pin` through an unresolved case (the way "
        "`init_cmd._Sdk` deliberately does not declare the field at all "
        "because `init` WRITES the pin rather than disclosing a stale one), "
        "drop the field instead of leaving it to be silently discarded."
    )


def test_the_scan_actually_finds_resolution_wrapper_classes() -> None:
    """Anti-vacuity: prove the walk still recognises a resolution-wrapper
    dataclass at all, so a refactor that renames `broken_project_pin`
    everywhere (and updates this file to match) cannot also silently blind
    the check above without a failure here to say so."""
    paths = sorted(PACKAGE.rglob("*.py"))
    assert _resolution_class_names(paths), (
        f"no dataclass under {PACKAGE} declares a `broken_project_pin` field -- "
        "either the field was renamed (update this gate to match) or the walk "
        "itself broke."
    )


# Anti-vacuity, detection half (tan-cli#922 review). The test above proves
# the CLASS walk still works; it says nothing about `_optional_of_name` /
# `_scan_tree` themselves -- replacing `_optional_of_name`'s body with a bare
# `return None` leaves both prior tests green, because there would then be
# nothing left in the tree to find either way. Each fixture below is a
# literal source string containing exactly one of the annotation shapes this
# gate claims to support; parsed and scanned directly (no file, no real
# `resolution_classes` walk) so a regression in the shape logic itself shows
# up here, independent of anything under `python/tan/`.
_POSITIVE_FIXTURES: dict[str, str] = {
    "x_or_none": """
class _ResolvedSdk:
    broken_project_pin: str | None = None

def f() -> _ResolvedSdk | None:
    return None
""",
    "none_or_x": """
class _ResolvedSdk:
    broken_project_pin: str | None = None

def f() -> None | _ResolvedSdk:
    return None
""",
    "typing_optional": """
import typing

class _ResolvedSdk:
    broken_project_pin: str | None = None

def f() -> typing.Optional[_ResolvedSdk]:
    return None
""",
    "bare_optional": """
from typing import Optional

class _ResolvedSdk:
    broken_project_pin: str | None = None

def f() -> Optional[_ResolvedSdk]:
    return None
""",
    "typing_union": """
import typing

class _ResolvedSdk:
    broken_project_pin: str | None = None

def f() -> typing.Union[_ResolvedSdk, None]:
    return None
""",
    "bare_union": """
from typing import Union

class _ResolvedSdk:
    broken_project_pin: str | None = None

def f() -> Union[_ResolvedSdk, None]:
    return None
""",
    "string_forward_ref": """
class _ResolvedSdk:
    broken_project_pin: str | None = None

def f() -> "_ResolvedSdk | None":
    return None
""",
    "type_alias": """
class _ResolvedSdk:
    broken_project_pin: str | None = None

_MaybeSdk = _ResolvedSdk | None

def f() -> _MaybeSdk:
    return None
""",
    "async_def": """
class _ResolvedSdk:
    broken_project_pin: str | None = None

async def f() -> _ResolvedSdk | None:
    return None
""",
    "unannotated_construct_and_none": """
class _ResolvedSdk:
    broken_project_pin: str | None = None

def f(sdk_root):
    if sdk_root is None:
        return None
    return _ResolvedSdk()
""",
}


def test_the_scan_actually_recognises_every_supported_optional_shape() -> None:
    """Anti-vacuity, detection half. One site per fixture, every time --
    proves `_optional_of_name`/`_scan_tree` still do the recognising, not
    just the class walk above."""
    for shape, source in _POSITIVE_FIXTURES.items():
        tree = ast.parse(source, filename=f"<fixture:{shape}>")
        found = _scan_tree(tree, {"_ResolvedSdk"})
        assert found, f"fixture {shape!r} should have been flagged and was not: {source}"
        assert len(found) == 1, f"fixture {shape!r} flagged {len(found)} sites, expected exactly 1: {found}"
