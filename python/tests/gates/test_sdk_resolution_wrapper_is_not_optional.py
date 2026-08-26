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
docstring says works in this repo: any dataclass under `python/tan/commands/`
that declares a `broken_project_pin` field is claiming to carry that fact.
A function returning `ThatDataclass | None` is where the claim gets broken --
`None` has no `.broken_project_pin` to read, so every caller's `is None`
branch loses the fact by construction, regardless of what the function body
does. Checking the SIGNATURE catches the defect the moment the type is wrong,
without having to prove anything about reachability.

`init_cmd._Sdk` is not exempted by name here -- it is excluded structurally,
which is the point. It deliberately does NOT declare `broken_project_pin`
(see its own docstring: `init` WRITES `.alp/sdk-path`, so a broken pin
already sitting in the parent workspace is superseded by this run's own
resolution, not something for `init` itself to disclose) and so never enters
`_resolution_wrapper_classes()` below -- a real design choice recorded once,
in the class that makes it, rather than a second time in an exemption list
here that could drift from it.
"""
from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]
PACKAGE = REPO / "python" / "tan" / "commands"


def _class_field_names(node: ast.ClassDef) -> list[str]:
    return [
        target.id
        for target in (stmt.target for stmt in node.body if isinstance(stmt, ast.AnnAssign))
        if isinstance(target, ast.Name)
    ]


def _optional_of_name(annotation: ast.expr) -> str | None:
    """`X | None` -> `"X"`; anything else (`Optional[X]`, a bare `X`, `X |
    Y`) -> `None`. Deliberately narrow: `X | None` is the one shape every
    ladder wrapper in this codebase actually uses (`_ResolvedSdk`,
    `_ResolvedSdkRoot`, `_SdkResolution`, `_Sdk`) -- widening this to catch
    `Optional[X]` too is a straightforward follow-up the day one appears, not
    a gap worth guessing at today."""
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        for name_side, none_side in ((annotation.left, annotation.right), (annotation.right, annotation.left)):
            if (
                isinstance(name_side, ast.Name)
                and isinstance(none_side, ast.Constant)
                and none_side.value is None
            ):
                return name_side.id
    return None


def _optional_resolution_wrapper_returns(path: pathlib.Path) -> list[str]:
    """`["def <name> -> <Class> | None (line N)", ...]` for every function in
    `path` whose return annotation is `X | None` where `X` is a
    module-level dataclass declaring a `broken_project_pin` field."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    resolution_classes = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef) and "broken_project_pin" in _class_field_names(node)
    }
    if not resolution_classes:
        return []
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.returns is None:
            continue
        wrapped = _optional_of_name(node.returns)
        if wrapped in resolution_classes:
            found.append(f"{node.name} -> {wrapped} | None (line {node.lineno})")
    return found


def test_no_sdk_resolution_wrapper_is_returned_as_optional() -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        sites = _optional_resolution_wrapper_returns(path)
        if sites:
            offenders[path.relative_to(REPO / "python").as_posix()] = sites

    assert offenders == {}, (
        "these functions return an SDK-resolution dataclass as `X | None`, "
        "which discards `.broken_project_pin`/`.foreign_global_default_for` "
        "on exactly the path -- nothing resolved -- where they matter most "
        "(tan-cli#900, tan-cli#468):\n  "
        + "\n  ".join(f"{mod}: {site}" for mod, sites in sorted(offenders.items()) for site in sites)
        + "\n\nMake `.path` (or the equivalent field) the `X | None` instead, "
        "and always return the dataclass -- the shape `examples_cmd._resolve_sdk`"
        " / `generate_cmd._resolve_sdk_root` / `flash_cmd._SdkResolution` all use "
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
    found_any = False
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and "broken_project_pin" in _class_field_names(node):
                found_any = True
    assert found_any, (
        f"no dataclass under {PACKAGE} declares a `broken_project_pin` field -- "
        "either the field was renamed (update this gate to match) or the walk "
        "itself broke."
    )
