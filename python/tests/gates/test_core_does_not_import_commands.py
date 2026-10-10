# SPDX-License-Identifier: Apache-2.0
"""The Dependency Rule, as a gate: inner layers must not import outward.

`tan/commands/**` is the outer ring (the CLI surface); `tan/core/**` and
`tan/envelope.py` sit inside it. Every file under `tan/commands/` that needs
shared logic imports it from `tan/core` -- the healthy direction. As of
tan-cli#408 (both the initial ladder extraction and its review follow-up),
none of them import back outward: `_KNOWN_INVERSIONS` below is empty, and
this docstring records how it got there rather than what still owes it.

A function-scoped import used to be how each of the three inversions this
gate has caught survived at runtime: it dodges the circular import the
inversion creates, and it is also what made the dependency invisible --
nothing at module level shows it, so `grep '^from tan.commands'` over the
inner tree finds nothing and the inversion accumulates unnoticed. This gate
looks at the parsed AST instead, so a local import counts exactly as much
as a top-level one -- which is what let it catch all three:

    tan/core/bootstrap.py:1703      ->  tan.commands.presets_cmd   (fixed)
    tan/envelope.py                 ->  tan.commands.build_cmd     (fixed, tan-cli#408 stage 1)
    tan/envelope.py                 ->  tan.commands.sdk_cmd       (fixed, tan-cli#408 stage 2)
    tan/core/sdk_discovery.py       ->  tan.commands.sdk_cmd       (fixed, tan-cli#408 stage 2)

`bootstrap.py`'s: `infer_runtime_for_core_id` had TWO definitions
(`tan/core/scaffold.py` and `tan/commands/presets_cmd.py`) and core reached
outward past its own copy to take the command's; both are now one function
in `tan/core/os_class.py`, the leaf that already answers "what OS class is
this core".

`envelope.py -> build_cmd`, stage 1: `resolve_sdk_root_ladder`,
`resolve_sdk_root_wide`, `sdk_ladder_divergence_issue` and `_planner_python`
moved verbatim into `tan/core/sdk_discovery.py`, so `envelope.py` could
import `sdk_ladder_divergence_issue` from there at module level -- the
concrete proof that inversion was gone, not just relocated.

That first move traded one inversion for a narrower one rather than closing
the loop: `resolve_sdk_root_ladder`/`resolve_sdk_root_wide` both called
`tan.commands.sdk_cmd.resolve_sdk_tiered`, the single narrow tiered
resolver, which at that point still lived in a command module, reached
through a function-scoped import inside each of the two ladder functions --
and `envelope.py -> sdk_cmd` stayed live too, for `sdk_resolution_issues`.
Review of that first pass (tan-cli#408 review) found the reasoning for
leaving `resolve_sdk_tiered` behind -- "a much larger, separately-scoped
change" -- did not survive inspection: `resolve_sdk_tiered` and the dozen
filesystem-primitive functions under it (`ActiveSdk`, the tier-pointer
reads, the registry lookup, the two positional-discovery walks) touched
none of `typer`, `tan.envelope`, `tan.output_format` or `tan.exit_codes` --
the same shape `sdk_discovery.py` already was. Stage 2 moved that whole
cluster, plus the two `Issue` builders `sdk_resolution_issues` needs
(`project_pin_issue`, `global_default_foreign_project_issue`), into
`tan/core/sdk_discovery.py` alongside the ladders -- which let both ladder
functions call `resolve_sdk_tiered` directly (no function-scoped import
left in either one) and let `envelope.py` import `sdk_resolution_issues` at
module level too. `_KNOWN_INVERSIONS` is empty as a result, not by
definition -- `test_no_new_inner_module_imports_a_command` below still
walks the real AST every run.

WHY THE ALLOWLIST STAYS, empty or not: the next inversion this gate catches
gets exactly this treatment -- listed, reasoned about, and either closed in
the same change or left as a disclosed, scoped debt with the reason recorded
here, never silently waved through. `test_no_known_inversion_has_been_
silently_fixed` below still runs, and still passes trivially, on an empty
list; that is the correct behaviour, not a gap -- a stale entry has nothing
to become stale from.
"""

from __future__ import annotations

import ast
from pathlib import Path

PYTHON_ROOT = Path(__file__).resolve().parents[2]
TAN = PYTHON_ROOT / "tan"

#: (module path relative to `python/`, imported command module). Every entry
#: is a Dependency Rule violation that is KNOWN and SCHEDULED, not accepted.
#: Delete the entry in the same change that removes the import -- a stale
#: entry fails `test_no_known_inversion_has_been_silently_fixed` below.
#:
#: Empty as of tan-cli#408 (both stages) -- see the module docstring for how
#: it got here. Stays a `set[tuple[str, str]]`, not deleted or replaced with
#: a comment, so the next inversion this gate catches has a working ratchet
#: to land in rather than a shape someone has to reconstruct first.
_KNOWN_INVERSIONS: set[tuple[str, str]] = set()


def _inner_modules() -> list[Path]:
    """`tan/core/**` plus `tan/envelope.py` -- the inner ring."""
    return sorted([*(TAN / "core").rglob("*.py"), TAN / "envelope.py"])


def _command_imports(path: Path) -> set[str]:
    """Every `tan.commands.*` this module imports, at ANY scope.

    Walks the AST rather than grepping line starts, because all three known
    inversions are function-scoped -- a line-anchored grep sees none of them.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "tan.commands" or node.module.startswith("tan.commands."):
                found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tan.commands" or alias.name.startswith("tan.commands."):
                    found.add(alias.name)
    return found


def _live_inversions() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for path in _inner_modules():
        rel = path.relative_to(PYTHON_ROOT).as_posix()
        for module in _command_imports(path):
            out.add((rel, module))
    return out


def test_no_new_inner_module_imports_a_command():
    """`tan/core/**` and `tan/envelope.py` may not import `tan.commands.*`."""
    new = _live_inversions() - _KNOWN_INVERSIONS
    assert not new, (
        "an inner module imports outward from `tan/commands/` -- the "
        "Dependency Rule inverted (tan-cli#408):\n"
        + "\n".join(f"  {mod} -> {imported}" for mod, imported in sorted(new))
        + "\n\nMove the shared code into `tan/core/` instead. A function-scoped "
        "import is not a fix: it hides the dependency from module-level "
        "inspection, which is how the three known ones accumulated."
    )


def test_no_known_inversion_has_been_silently_fixed():
    """A `_KNOWN_INVERSIONS` entry that is no longer real must be deleted.

    Without this the allowlist decays into a permanent exemption: an entry
    outlives the import it excuses, and the next genuine inversion at the same
    (module, target) pair is waved through by a line nobody re-checked.
    """
    stale = _KNOWN_INVERSIONS - _live_inversions()
    assert not stale, (
        "these `_KNOWN_INVERSIONS` entries no longer describe a real import "
        "-- delete them in the change that fixed them:\n"
        + "\n".join(f"  {mod} -> {imported}" for mod, imported in sorted(stale))
    )
