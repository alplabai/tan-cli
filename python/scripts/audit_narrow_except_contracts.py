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

WHAT IT DOES BEYOND THE STATIC WALK, AND WHAT IT DOES NOT (review round 3
BLOCKER, corrected here after an earlier draft of this docstring overclaimed
it): for each candidate whose signature looks like `(path_like, ...) -> T`
-- a first positional parameter literally named `path`, or a `Path`-
annotated parameter -- it imports the module, substitutes a real on-disk
NON-UTF-8 file for that parameter, and calls the function with best-effort
defaults for the rest. That is ONE shape of the seven `tests/gates/
test_never_raises_contract_holds.py` itself drives (no directory, no
parent-is-a-file, no ELOOP, no `chmod 000`, no malformed document, no
absent path) -- every verdict this script prints is labelled `(non-UTF-8
only)` for exactly that reason, and a bare "OK" must never be read as
"driven clean" across all seven. Measured on this tree: **19 OK / 43
unexecuted / 3 ESCAPED**, and all three ESCAPED are curated exceptions a
contract-HOLDING function raised on purpose (a non-UTF-8 catalogue
correctly refused) -- this script's own execution pass finds ZERO defects
here. `_known_board_names` is the worked example of why that matters: this
script reports `OK (non-UTF-8 only)` for it, truthfully, and its real
defect (an `EACCES`-through-`is_dir()` pre-flight) needed the `chmod 000`
shape this script does not drive to surface at all.

So: **this script SCOPES the search, it does not perform it.** Every live
defect tan-cli#1116's triage found was found by a human reading the
candidate list this script narrows to a tractable size, then hand-driving
each one against the shapes that actually matter -- `chmod 000` chief among
them, since it is the one shape this script cannot reach at all. The
script's OWN role is upstream of that: it is what finds candidates worth a
human's time, not what finds the defects among them, and no report of this
issue's work should credit it with the latter.

A candidate this script cannot safely call (a multi-argument signature it
cannot guess, a function requiring state this script has no way to
construct) is reported as `unexecuted` rather than silently skipped -- the
same "a shape that silently no-ops is worse than an honest gap" reasoning
applies to the tool as a whole, not just one candidate: a ZERO-candidate
run (this script's own file moved somewhere its `TAN_ROOT` arithmetic no
longer resolves, or run against a tree that lost its `tan/` package) is
refused with a non-zero exit rather than printed as "0 candidate
function(s) found" and returning 0 -- the exact #1105 shape (a check that
can silently stop checking) this issue's own review exists to catch,
previously present in the one tool built to prevent it.

USAGE: `python scripts/audit_narrow_except_contracts.py [--planner]` from
`python/`. Prints a per-candidate verdict, then a tally line
(`N OK (non-UTF-8 only) / N unexecuted / N ESCAPED (non-UTF-8 only)`) on
every run -- read the tally before the per-line output, since most
candidates are `unexecuted` and a listing alone hides that ratio.
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
        # tan-cli#1116 review round 3 BLOCKER: this is NOT "driven clean" --
        # it is one shape of the seven the gate itself covers (non-UTF-8
        # only; no directory, no parent-is-a-file, no ELOOP, no chmod 000,
        # no malformed document, no absent path). A bare "OK" here reads as
        # a stronger claim than the run actually makes; label it so nobody
        # -- including a future run of this same script -- can mistake a
        # single-shape pass for exhaustive coverage the way `_known_board_
        # names` did (this exact call returned "OK" for it; the function's
        # real defect was an EACCES-through-`is_dir()` pre-flight this
        # script's one shape cannot reach at all).
        return "OK (non-UTF-8 only): returned without raising"
    except Exception as exc:  # noqa: BLE001 -- reporting every outcome
        tb = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return f"ESCAPED (non-UTF-8 only): {type(exc).__name__}: {tb}"
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

    # tan-cli#1116 review round 3 BLOCKER: a zero-candidate run used to print
    # "0 candidate function(s) found" and exit 0 -- indistinguishable from a
    # genuinely clean tree, and silently vacuous if this script is ever
    # invoked from anywhere its own walk root does not resolve correctly (a
    # stale checkout, a moved `tan/` package, a future refactor of this
    # file's own path arithmetic). This IS the #1105 shape -- a check that
    # can silently stop checking -- inside the one tool this issue's own
    # review built to prevent it. A tree with far fewer than this many
    # candidates is not "clean," it is "this script broke"; fail loud.
    _MIN_EXPECTED_CANDIDATES = 40  # measured 65 non-planner / 79 with
    # --planner on the tree this script was written against; well under
    # half of the smaller number is not a plausible real shrink from one
    # PR, only a broken walk.
    if len(candidates) < _MIN_EXPECTED_CANDIDATES:
        print(
            f"::error:: only {len(candidates)} candidate(s) found -- expected "
            f"at least {_MIN_EXPECTED_CANDIDATES}. This script's own walk root "
            f"is {TAN_ROOT}; if that path does not exist or the tree moved, "
            f"this is a broken/vacuous run, not a clean tree. Refusing to "
            f"report a tally that would misrepresent it.",
            file=sys.stderr,
        )
        return 1

    print(f"{len(candidates)} candidate function(s) found "
          f"({'including' if args.planner else 'excluding'} tan/planner/**).\n")

    tally: dict[str, int] = {"OK": 0, "ESCAPED": 0, "unexecuted": 0, "planner": 0}
    for dotted, qualname, lineno in candidates:
        if dotted.split(".")[1] == "planner":
            tally["planner"] += 1
            print(f"{dotted}::{qualname}:{lineno} -- planner (not executed here)")
            continue
        verdict = try_execute(dotted, qualname)
        bucket = verdict.split(" ", 1)[0].split(":", 1)[0]
        tally[bucket] = tally.get(bucket, 0) + 1
        print(f"{dotted}::{qualname}:{lineno} -- {verdict}")

    # tan-cli#1116 review round 3 BLOCKER: print the tally on EVERY run, not
    # just when someone greps for it by hand -- a reader who only sees
    # per-line verdicts has no cheap way to notice that most of them are
    # "unexecuted" (this script drives ONE shape of the seven the gate
    # itself covers, and only for the minority of candidates whose
    # signature it can safely guess at all). This tally is upstream
    # scoping, not a defect count: every "ESCAPED" verdict is a candidate
    # worth reading by hand, not a confirmed bug, and a bare "OK" only ever
    # means "this one shape didn't raise" -- see try_execute's own comment.
    print(
        f"\ntally: {tally.get('OK', 0)} OK (non-UTF-8 only) / "
        f"{tally.get('unexecuted', 0)} unexecuted / "
        f"{tally.get('ESCAPED', 0)} ESCAPED (non-UTF-8 only)"
        + (f" / {tally['planner']} planner (not executed)" if tally["planner"] else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
