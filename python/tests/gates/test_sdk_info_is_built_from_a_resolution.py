# SPDX-License-Identifier: Apache-2.0
"""Gate: a new `SdkInfo` must be built from the resolution that produced it.

tan-cli#478. `SdkInfo(root, tier)` drops two facts the resolution already
answered with -- `foreign_global_default_for` and `broken_project_pin` -- and
dropping them is how 16 of ~32 commands disclosed that they had resolved
ANOTHER project's checkout while the rest stayed silent, at `ok: true` and
`issues: []`. `SdkInfo.from_resolution(root, resolution)` carries both, and
`Envelope.__init__` turns them into the warning for every command at once.

WHY A CONSTRUCTOR GATE AND NOT A "COMMAND RESOLVES WITHOUT EMITTING" GATE.
The obvious gate -- walk every registered command, fail when one resolves an
SDK and does not emit the pair -- cannot be written soundly here. "Resolves an
SDK" flows through at least five entry points (`resolve_sdk_tiered`,
`resolve_sdk_root_ladder`, `resolve_sdk_root_wide`,
`resolve_sdk_root_ladder_safe`, plus per-module wrappers), and MODULE-LEVEL
presence of a call is trivially satisfied while every early-return path stays
silent. That is not hypothetical: `size_cmd.py` and `image_cmd.py` both carry
comments describing guard paths that returned before "`_run`'s own
`sdk_resolution_issues` call further down", and `flash_cmd.py` documents a
path the call was removed from. A gate that cannot see control flow passes on
exactly the silence it was written to catch.

The gates that work in this repo -- `test_every_issue_code_is_registered.py`
and friends -- gate LITERALS and CONSTRUCTORS, not reachability. This is that
shape: it cannot be satisfied without going through the seam that carries the
facts, which is the property tan-cli#478 actually asks for.

RATCHET, NOT A BAN. The sites below still build `SdkInfo` directly because
their resolution object is not in scope at the construction point -- threading
it is per-command work, not a rename. They are listed so they can only SHRINK:
convert one, lower its number, and the gate holds the new floor. A site NOT on
this list is a new raw construction and fails.
"""
from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[3]
PACKAGE = REPO / "python" / "tan"

#: Modules whose raw `SdkInfo(...)` construction predates tan-cli#478's seam.
#: `<module path relative to python/>: <how many raw sites>`. Lower a number
#: when you convert one; never raise one.
#:
#: `tan/envelope.py` is skipped entirely -- `from_resolution`'s own `cls(...)`
#: is the seam, and this gate only looks at `SdkInfo(` by name. Matched by
#: PATH, not by `path.name`: a future `tan/<anything>/envelope.py` would
#: otherwise be exempt from the gate for the sake of its filename.
LEGACY_RAW_CONSTRUCTIONS: dict[str, int] = {
    "tan/commands/bootstrap_cmd.py": 4,
    "tan/commands/run_cmd.py": 1,
    "tan/commands/generate_cmd.py": 1,
    "tan/commands/build_cmd.py": 1,
}


def _raw_sdk_info_sites(path: pathlib.Path) -> list[int]:
    """Line numbers of every `SdkInfo(...)` CALL in `path`.

    Deliberately syntax, not text: a type annotation (`sdk: SdkInfo | None`)
    and an import are not constructions, and a substring scan counts both.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SdkInfo"
    ]


def _scan() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        if path == PACKAGE / "envelope.py":
            continue
        sites = _raw_sdk_info_sites(path)
        if sites:
            found[path.relative_to(REPO / "python").as_posix()] = sites
    return found


def test_no_new_raw_sdk_info_construction() -> None:
    """A module not in the ledger must build through `from_resolution`."""
    found = _scan()
    new = {mod: lines for mod, lines in found.items() if mod not in LEGACY_RAW_CONSTRUCTIONS}
    assert new == {}, (
        "these modules construct `SdkInfo(...)` directly, which drops the "
        "foreign-global-default and broken-pin facts the resolution already "
        "carried (tan-cli#478):\n  "
        + "\n  ".join(f"{mod}: line(s) {lines}" for mod, lines in sorted(new.items()))
        + "\n\nUse `SdkInfo.from_resolution(root, resolution)` -- the resolution "
        "object every ladder already answers with. `Envelope.__init__` then "
        "emits the pair for you, so no command has to remember."
    )


def test_the_legacy_ledger_only_shrinks() -> None:
    """Converting a site must lower its count; adding one to a legacy module
    must fail. Without this half, a module already on the list could grow new
    raw constructions forever."""
    found = _scan()
    grew = [
        f"{mod}: {len(found.get(mod, []))} raw site(s), ledger says {allowed}"
        for mod, allowed in LEGACY_RAW_CONSTRUCTIONS.items()
        if len(found.get(mod, [])) > allowed
    ]
    assert grew == [], "raw `SdkInfo(...)` construction grew:\n  " + "\n  ".join(grew)


def test_the_ledger_has_no_stale_entries() -> None:
    """A converted module must leave the ledger, or the gate quietly stops
    covering it -- the same staleness `test_module_size_budget` guards for
    sizes. Fails LOUDLY with the number to write, so the fix is mechanical."""
    found = _scan()
    stale = [
        f"{mod}: ledger says {allowed}, actual {len(found.get(mod, []))}"
        for mod, allowed in LEGACY_RAW_CONSTRUCTIONS.items()
        if len(found.get(mod, [])) < allowed
    ]
    assert stale == [], (
        "the ledger is stale -- lower or delete these entries:\n  " + "\n  ".join(stale)
    )
