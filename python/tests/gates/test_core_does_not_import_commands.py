# SPDX-License-Identifier: Apache-2.0
"""The Dependency Rule, as a gate: inner layers must not import outward.

`tan/commands/**` is the outer ring (the CLI surface); `tan/core/**` and
`tan/envelope.py` sit inside it. 32 files under `tan/commands/` import from
`tan/core` -- the healthy direction. Three imports ran the other way
(tan-cli#408), each one function-scoped to dodge the circular import the
inversion creates:

    tan/core/bootstrap.py:1703  ->  tan.commands.presets_cmd
    tan/envelope.py:380         ->  tan.commands.sdk_cmd
    tan/envelope.py:439         ->  tan.commands.build_cmd

A function-scoped import is what makes this survivable at runtime, and it is
also what makes it invisible: nothing at module level shows the dependency,
so `grep '^from tan.commands'` over the inner tree finds nothing and the
inversion accumulates. This gate looks at the parsed AST instead, so a local
import counts exactly as much as a top-level one.

The `bootstrap.py` one is fixed -- `infer_runtime_for_core_id` had TWO
definitions (`tan/core/scaffold.py` and `tan/commands/presets_cmd.py`) and
core reached outward past its own copy to take the command's; both are now
one function in `tan/core/os_class.py`, the leaf that already answers "what
OS class is this core".

The two in `envelope.py` need `sdk_resolution_issues` and
`sdk_ladder_divergence_issue` extracted to a shared core module, which
touches ~18 call sites and is its own change. They are listed in
`_KNOWN_INVERSIONS` so this gate holds the line at "no NEW ones" today
rather than waiting for that work -- the ratchet shape this repo already
uses for the module-size budget. Removing an entry from that list is the
acceptance criterion for the extraction; the test below fails if an entry
is listed but no longer real, so the list cannot rot into a permanent
exemption.
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
_KNOWN_INVERSIONS = {
    ("tan/envelope.py", "tan.commands.sdk_cmd"),
    ("tan/envelope.py", "tan.commands.build_cmd"),
}


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
