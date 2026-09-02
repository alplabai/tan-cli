#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1116 review round 2, MINOR: the shape-sweep count behind
`tests/gates/test_never_raises_contract_holds.py` was not reproducible --
its docstring cited a number from a script that was run once and discarded.
This is that script, committed so the count is re-derivable by anyone, not
just remembered.

A ONE-SHOT AUDIT TOOL, NOT A GATE. It prints a report; nothing here is
asserted, nothing here runs in CI. The gate itself stays the small, curated,
opt-in `_SEEDED_CONTRACTS` allow-list -- this script is upstream of that
list, not a replacement for it (the seed list is what enforces; this script
is what finds candidates to seed).

WHAT IT WALKS: every `python/tan/**` function (excluding `tan/planner/**`,
whose reads are import-order-gated behind `bind_sdk_root` and cannot be
driven by a bare `python3` invocation the way every other module here can)
whose `try` wraps a `.read_text(`/`.read_bytes(`/`open(`/`yaml.safe_load(`/
`json.load(`/`json.loads(` call, with a broad `except Exception`/
`BaseException` excluded as safe-by-construction. `--planner` widens the
walk to include `tan/planner/**` too, reported separately, since import
order makes those undriveable from this script directly -- read the
function, do not execute it, the same manual step tan-cli#1116's review
round 2 took for `_docs_ref` and `render_to_envelope`.

WHAT IT DOES BEYOND THE STATIC WALK: for each candidate whose signature
looks like `(path_like, ...) -> T` -- a first positional parameter literally
named `path`, or a `Path`-annotated parameter -- it imports the module,
substitutes a real on-disk non-UTF-8 file for that parameter, and calls the
function with best-effort defaults for the rest. This is EXACTLY the
"actual execution against broken files rather than reading alone" tan-cli#1116
review round 2 asked for after the 18-of-79 hand-read triage missed two
confirmed live defects (`som_buildability._safe_load_mapping`,
`template.py::_docs_ref`) in the 61 it left unread. A candidate this script
cannot safely call (a multi-argument signature it cannot guess, a function
requiring state this script has no way to construct) is reported as
`unexecuted` rather than silently skipped -- the same "a shape that
silently no-ops is worse than an honest gap" the review's own MINOR finding
made about `drpai._compiler_version`'s dead `test_parent_is_a_file` shape.

USAGE: `python scripts/audit_narrow_except_contracts.py [--planner]` from
`python/`.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import sys
import tempfile
import traceback
from pathlib import Path

TAN_ROOT = Path(__file__).resolve().parents[1] / "tan"

RISKY_ATTRS = {"read_text", "read_bytes", "safe_load", "loads", "load"}
BROAD = {"Exception", "BaseException"}


def _name(n: ast.expr) -> str:
    if isinstance(n, ast.Attribute):
        return n.attr
    if isinstance(n, ast.Name):
        return n.id
    return ast.dump(n)


def _except_names(handler: ast.ExceptHandler) -> set[str]:
    t = handler.type
    if t is None:
        return {"BaseException"}
    if isinstance(t, ast.Tuple):
        return {_name(e) for e in t.elts}
    return {_name(t)}


def _calls_risky_io(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute) and f.attr in RISKY_ATTRS:
                return True
            if isinstance(f, ast.Name) and f.id == "open":
                return True
    return False


def find_candidates(include_planner: bool) -> list[tuple[str, str, int]]:
    """`[(module_dotted_path, qualname, lineno), ...]` for every function
    whose `try` wraps risky I/O with a non-broad `except`."""
    found: list[tuple[str, str, int]] = []
    for path in sorted(TAN_ROOT.rglob("*.py")):
        rel = path.relative_to(TAN_ROOT.parent)
        parts = rel.with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not include_planner and len(parts) > 1 and parts[1] == "planner":
            continue
        dotted = ".".join(parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk_func(node: ast.AST, qual_prefix: str = "") -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qual = f"{qual_prefix}{child.name}"
                    for sub in ast.walk(child):
                        if isinstance(sub, ast.Try) and _calls_risky_io(sub):
                            names: set[str] = set()
                            for h in sub.handlers:
                                names |= _except_names(h)
                            if not (names & BROAD):
                                found.append((dotted, qual, child.lineno))
                                break
                    walk_func(child, qual_prefix=f"{qual}.")
                elif isinstance(child, ast.ClassDef):
                    walk_func(child, qual_prefix=f"{qual_prefix}{child.name}.")

        walk_func(tree)
    return found


def _non_utf8_file() -> Path:
    fd = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
    fd.write(b"\xff\xfe\x00not-utf8-bytes")
    fd.close()
    return Path(fd.name)


def try_execute(dotted: str, qualname: str) -> str:
    """Best-effort: import @dotted, resolve @qualname (dotted through
    classes), find a Path-like first parameter, call it with a real
    non-UTF-8 file substituted there and defaults elsewhere. Returns a
    one-line verdict string."""
    try:
        module = importlib.import_module(dotted)
    except Exception as exc:  # noqa: BLE001 -- reporting, not asserting
        return f"import failed: {exc!r}"

    obj: object = module
    for part in qualname.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return "unexecuted: could not resolve qualname"
    if not callable(obj) or inspect.isclass(obj):
        return "unexecuted: not a plain function"

    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return "unexecuted: no introspectable signature"

    params = list(sig.parameters.values())
    if not params:
        return "unexecuted: no parameters to substitute a broken path into"

    first = params[0]
    looks_path_like = "path" in first.name.lower() or (
        first.annotation is not inspect.Parameter.empty
        and "Path" in str(first.annotation)
    )
    if not looks_path_like:
        return "unexecuted: first parameter is not obviously path-shaped"

    for p in params[1:]:
        if p.default is inspect.Parameter.empty and p.kind not in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            return "unexecuted: a later required parameter has no default to guess"

    broken = _non_utf8_file()
    try:
        obj(broken)  # type: ignore[misc]
        return "OK: returned without raising"
    except Exception as exc:  # noqa: BLE001 -- reporting every outcome
        tb = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return f"ESCAPED: {type(exc).__name__}: {tb}"
    finally:
        broken.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--planner", action="store_true",
        help="include tan/planner/** in the static walk (reported, not executed "
             "-- import order needs bind_sdk_root first)")
    args = parser.parse_args()

    sys.path.insert(0, str(TAN_ROOT.parent))
    candidates = find_candidates(include_planner=args.planner)
    print(f"{len(candidates)} candidate function(s) found "
          f"({'including' if args.planner else 'excluding'} tan/planner/**).\n")

    for dotted, qualname, lineno in candidates:
        if dotted.split(".")[1] == "planner":
            print(f"{dotted}::{qualname}:{lineno} -- planner (not executed here)")
            continue
        verdict = try_execute(dotted, qualname)
        print(f"{dotted}::{qualname}:{lineno} -- {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
