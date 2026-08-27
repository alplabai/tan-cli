# SPDX-License-Identifier: Apache-2.0
"""An `except` clause that can never fire must not sit in the tree pretending
to be the contract (tan-cli#440).

`kconfig_cmd._resolve_core` carried two consecutive handlers for
`UnicodeDecodeError`:

    except (OSError, UnicodeDecodeError) as err:
        raise _CoreResolutionError("kconfig.board-yaml-missing", ...)

    except UnicodeDecodeError as err:
        raise _CoreResolutionError("kconfig.board-yaml-invalid", ...)

The first consumes every `UnicodeDecodeError`, so the second was dead. Not
harmless duplicated prose: the two blocks documented CONTRADICTORY contract
decisions, and the dead one carried the pre-#421 explanation from before the
oracle-parity correction that settled the code. Runtime followed the live
arm, so nothing was broken -- but a future edit could have modified or tested
the dead `board-yaml-invalid` branch believing it live while consumers kept
receiving `board-yaml-missing`.

## Why an AST gate and not ruff's B025

`B025` is exactly this rule, and #440 asks for "a focused Python lint gate
that includes B025". A ruff job would need ruff installed: CI's `gates` job
runs `pip install -e ./python` (no extras) plus `pip install pytest
pytest-xdist` -- still no `ruff` (`ci.yml`), so a gate that shells `ruff`
would SKIP on every run that matters -- the precise failure mode
`test_parity_freeze_completeness.py` was written to make impossible elsewhere
in this suite. This walks the same
structure with the stdlib `ast` the other gates in this directory already
use, inside a pytest run that is ALREADY a required context, so it enforces
from the next PR with no protection change and no new dependency.

It is deliberately narrower than B025 in one way and wider in another: it
compares handlers by exception NAME (so it cannot see a subclass relationship
`UnicodeDecodeError`/`ValueError` spelled with two different names), and it
covers every `try` in `tan/`, including `tan/planner/`. The narrow half is
the trade for having no import-time type resolution; the shape #440 reports
-- the same name twice in one `try` -- is caught exactly.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: `python/tan`, found from this file rather than from a cwd, so the gate is
#: identical however pytest was started (tan-cli#423's lesson).
_PACKAGE = Path(__file__).resolve().parents[2] / "tan"


def _handler_names(handler: ast.ExceptHandler) -> list[str]:
    """Every exception NAME one `except` clause names, flattened out of a
    tuple. A non-`Name` node (an attribute like `yaml.YAMLError`, a call) is
    rendered by `ast.unparse` so two spellings of the same thing still
    compare equal to each other."""
    node = handler.type
    if node is None:  # a bare `except:` names nothing
        return []
    parts = node.elts if isinstance(node, ast.Tuple) else [node]
    return [ast.unparse(part) for part in parts]


def _unreachable_handlers(tree: ast.AST) -> list[tuple[int, str]]:
    """`(lineno, name)` for every handler whose exception is ALREADY consumed
    by an earlier handler in the same `try`. Also flags a handler that sits
    after a bare `except:`, which consumes everything."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) and not isinstance(node, getattr(ast, "TryStar", ast.Try)):
            continue
        seen: set[str] = set()
        catch_all = False
        for handler in node.handlers:
            names = _handler_names(handler)
            if catch_all:
                out.append((handler.lineno, ", ".join(names) or "bare except"))
                continue
            if not names:  # bare `except:` -- consumes everything after it
                catch_all = True
                continue
            if all(name in seen for name in names):
                out.append((handler.lineno, ", ".join(names)))
            seen.update(names)
    return out


def test_no_except_clause_is_shadowed_by_an_earlier_one_in_the_same_try():
    dead: list[str] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as err:  # pragma: no cover -- fails elsewhere first
            pytest.fail(f"{path} does not parse: {err}")
        rel = path.relative_to(_PACKAGE.parent).as_posix()
        dead.extend(f"{rel}:{line} -- except {name}" for line, name in _unreachable_handlers(tree))

    assert dead == [], (
        "these `except` clauses can never fire -- an earlier handler in the "
        "same `try` already consumes the exception:\n  "
        + "\n  ".join(dead)
        + "\n\nDelete the dead arm. Do NOT reorder it into reachability "
        "without re-establishing which behaviour is correct: tan-cli#440's "
        "dead arm raised the WRONG issue code, so reordering would have made "
        "the wrong code live."
    )


def test_the_gate_sees_the_shape_tan_cli_440_reported():
    """The negative control. A gate that cannot fail is not a gate -- and
    this one would have passed silently if `_handler_names` mis-parsed the
    tuple form, which is exactly how #440's arm hid."""
    source = (
        "try:\n"
        "    pass\n"
        "except (OSError, UnicodeDecodeError):\n"
        "    pass\n"
        "except UnicodeDecodeError:\n"
        "    pass\n"
    )
    assert _unreachable_handlers(ast.parse(source)) == [(5, "UnicodeDecodeError")]


def test_a_legitimate_narrowing_order_is_not_flagged():
    """The other direction: a genuinely reachable second handler must stay
    green, or the gate gets disabled the first time someone writes ordinary
    code."""
    source = (
        "try:\n"
        "    pass\n"
        "except UnicodeDecodeError:\n"
        "    pass\n"
        "except OSError:\n"
        "    pass\n"
        "except (ValueError, TypeError):\n"
        "    pass\n"
    )
    assert _unreachable_handlers(ast.parse(source)) == []
