# SPDX-License-Identifier: Apache-2.0
"""`rollback_relocation_after` must be the last thing a path does before it returns.

`_run` builds `ws = Workspace(...)` from `paths` immediately after workspace
selection, snapshotting the three path values. `rollback_relocation_after`
rebinds `paths` back to the pre-relocation snapshot -- but it cannot reach
inside `ws`, which goes on naming the VACATED paths.

Nothing goes wrong today only because both call sites `return` on the very
next statement, so no step ever reads the stale `ws`. That was an invariant
held by luck: it is stated in no type, enforced by no check, and freezing
`RunPaths` (tan-cli#991) does not fix it -- an immutable value rebound in the
enclosing scope leaves an already-constructed `Workspace` exactly as stale as
a mutated one would.

Rather than leave that as a comment, this gate makes it structural: a rollback
call that is NOT immediately followed by `return` fails here. It is the same
"guard one line, leave the unguarded sibling" shape this repo keeps hitting
(#900, #926, #937, #943), caught before it has a sibling.
"""

from __future__ import annotations

import ast
from pathlib import Path

MODULE = Path(__file__).resolve().parents[2] / "tan" / "commands" / "bootstrap_cmd.py"
_ROLLBACK = "rollback_relocation_after"


def _is_rollback_call(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Name)
        and stmt.value.func.id == _ROLLBACK
    )


def _call_sites() -> list[tuple[int, ast.stmt | None]]:
    """Every `rollback_relocation_after(...)` statement, with the statement
    that follows it in the SAME block (`None` when it is the block's last)."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"), filename=str(MODULE))
    found: list[tuple[int, ast.stmt | None]] = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block):
                if _is_rollback_call(stmt):
                    nxt = block[i + 1] if i + 1 < len(block) else None
                    found.append((stmt.lineno, nxt))
    return found


def test_the_rollback_call_sites_are_still_there():
    """Guard against this gate quietly measuring nothing.

    If the function is renamed or the calls are restructured away, this gate
    must be updated or dropped deliberately -- not left passing over an empty
    set, which is how a gate becomes decoration.
    """
    assert _call_sites(), (
        f"no `{_ROLLBACK}(...)` call statements found in {MODULE.name} -- either "
        "the shape changed (update this gate) or the rollback was removed (drop "
        "it), but it must not silently pass having found nothing"
    )


def test_every_rollback_is_immediately_followed_by_return():
    offenders = [
        lineno for lineno, nxt in _call_sites() if not isinstance(nxt, ast.Return)
    ]
    assert not offenders, (
        f"`{_ROLLBACK}()` is called at "
        + ", ".join(f"{MODULE.name}:{n}" for n in offenders)
        + " without an immediate `return`.\n\n"
        "`ws` (the `Workspace` built from `paths` before the relocation) still "
        "names the VACATED paths after a rollback -- the rebinding reaches "
        "`_run`'s `paths`, never the snapshot inside `ws`. Any step that runs "
        "after a rollback therefore works against a directory this run just "
        "moved away from. Return immediately, or rebuild `ws` from the restored "
        "`paths` before continuing (tan-cli#991)."
    )
