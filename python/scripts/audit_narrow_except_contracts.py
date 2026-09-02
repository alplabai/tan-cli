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
carrying either of TWO shapes. `--planner` widens the walk to include
`tan/planner/**` too, reported separately, since import order makes those
undriveable from this script directly -- read the function, do not execute
it, the same manual step tan-cli#1116's review round 2 took for `_docs_ref`
and `render_to_envelope`.

SHAPE 1, "the `except` is narrower than the I/O" (the original walk): a
`try` wrapping a `.read_text(`/`.read_bytes(`/`open(`/`yaml.safe_load(`/
`json.load(`/`json.loads(` call, with a broad `except Exception`/
`BaseException` excluded as safe-by-construction.

SHAPE 2, "the `except` cannot fire at all" (tan-cli#1132): a `try` whose
BODY builds a lazy iterator -- a call named `glob`, `rglob`, `iterdir`,
`scandir` or `walk` -- that nothing in that same body forces. `Path.glob`
returns a generator and does the filesystem work on ITERATION, so a `try`
that only wraps the call guards nothing:

    try:
        matches = app_dir.glob(pattern)   # builds a generator; cannot fail
    except OSError:
        continue
    for path in matches:                  # the filesystem work, uncovered

Forcing is detected structurally, not by convention: consumption by
`list`/`sorted`/`set`/`frozenset`/`tuple`/`dict`/`next`/`any`/`all`/`min`/
`max`/`sum`/`len`, or a `for` loop or comprehension driving it, counts --
directly on the call (`sorted(d.glob(...))`), or through a name bound by an
assignment or a `with ... as` (`with os.scandir(root) as entries:` then a
comprehension over `entries`). Handler BREADTH is not filtered on for this
shape: an `except Exception` around a lazy call it never covers is exactly
as dead as an `except OSError`.

WHAT SHAPE 2 DOES NOT CLAIM, spelled out because an overclaiming docstring
here is the same defect class the tool reports:

  * It is NAME-based, like `RISKY_ATTRS` above -- `x.glob(...)` matches
    whatever `x` is, and a lazy call reached through an alias this walk does
    not model (a walrus binding, a helper that returns the iterator) is
    invisible.
  * Membership in that name set is not a claim that all five are lazy on
    every interpreter, and MEASUREMENT SAYS THEY ARE NOT. Non-root, on
    3.12.3 / 3.13.15 / 3.14.7 side by side, against a `chmod 000` directory:
    `Path.glob` returns without raising on all three (lazy everywhere);
    `Path.iterdir` is lazy on 3.12.3 but raises AT THE CALL on 3.13.15 and
    3.14.7; `os.scandir` raises at the call on all three, because it opens
    the directory eagerly -- only its per-entry `readdir` failures escape a
    `try` left behind. So a SHAPE 2 report is a candidate to read, not a
    confirmed dead handler, exactly like SHAPE 1's `ESCAPED`.
  * It only sees a lazy call that HAS a `try` around it. `model/analyze.
    _resolve_table`, tan-cli#1132's second live site, wrote `sorted(table_
    dir.glob("*.json"))` with no `try` at all, and this walk cannot report
    it -- there is no handler to call dead. A missing guard is a different
    audit from a guard that cannot fire, and this script does not perform
    it.
  * `try_execute`'s single non-UTF-8 shape says NOTHING about a SHAPE 2
    finding either way, which is why each is printed as its own indented
    `lazy-escape:` line under whatever execution verdict the candidate got.

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
"driven clean" across all seven. Measured on this tree at the tan-cli#1132
fix: **65 candidates, 19 OK / 43 unexecuted / 3 ESCAPED**, plus **0
lazy-escape (shape 2)**, and all three ESCAPED are curated exceptions a
contract-HOLDING function raised on purpose (a non-UTF-8 catalogue
correctly refused) -- this script's own execution pass finds ZERO defects
here. Shape 2's detector is not vacuous at that zero: re-run over the same
tree with `commands/build/configure_inputs.py` reverted to its pre-#1132
form it reports **66 candidates / 1 lazy-escape**, naming
`discover_configure_inputs`'s `app_dir.glob(...)` exactly (measured, both
with and without `--planner`; `tan/planner/**` carries none of this
shape). `_known_board_names` is the worked example of why that matters: this
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
`python/`. Prints a per-candidate verdict tagged with the shape(s) that
selected it, then TWO tally lines -- the execution one (`N OK (non-UTF-8
only) / N unexecuted / N ESCAPED (non-UTF-8 only)`) and the static
`lazy-escape (static, shape 2)` one -- on every run. Read the tallies
before the per-line output, since most candidates are `unexecuted` and a
listing alone hides that ratio. The two are deliberately NOT summed: they
count different things over different populations, and one number covering
both would mean neither.
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
from typing import NamedTuple

TAN_ROOT = Path(__file__).resolve().parents[1] / "tan"

RISKY_ATTRS = {"read_text", "read_bytes", "safe_load", "loads", "load"}
BROAD = {"Exception", "BaseException"}

#: Calls whose RESULT is (or may be) a lazy iterator: the filesystem work
#: happens on iteration, not at the call. Matched by attribute/function NAME
#: only -- `x.glob(...)` counts whatever `x` is, the same name-based
#: approximation `RISKY_ATTRS` above already makes. See SHAPE 2 in the module
#: docstring for what is and is not measured about each of these.
LAZY_ITER_CALLS = {"glob", "rglob", "iterdir", "scandir", "walk"}

#: Calls that FORCE a lazy iterator: if one of these consumes the lazy call
#: inside the same `try` body, the filesystem work happens inside the `try`
#: and the handler is live. A `for` loop and a comprehension force it too
#: and are detected structurally, not by name.
FORCING_CALLS = {
    "list", "sorted", "set", "frozenset", "tuple", "dict",
    "next", "any", "all", "min", "max", "sum", "len",
}


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


def _lazy_call_label(node: ast.Call) -> str | None:
    """`"app_dir.glob"` for a call whose callee name is in
    `LAZY_ITER_CALLS`, else `None`."""
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr in LAZY_ITER_CALLS:
        return ast.unparse(f)
    if isinstance(f, ast.Name) and f.id in LAZY_ITER_CALLS:
        return f.id
    return None


def _forced_within(body: list[ast.stmt]) -> tuple[set[int], set[str]]:
    """`(call node ids, local names)` whose iteration is forced somewhere in
    @body -- consumed by a `FORCING_CALLS` builtin, or driven by a `for`
    loop or comprehension. Both halves are needed: `sorted(p.glob(...))`
    forces the CALL directly, while `it = p.glob(...)` / `for x in it:`
    forces it through a NAME."""
    call_ids: set[int] = set()
    names: set[str] = set()

    def _consume(node: ast.expr) -> None:
        if isinstance(node, ast.Call):
            call_ids.add(id(node))
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Starred):
            _consume(node.value)

    for stmt in body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                    and sub.func.id in FORCING_CALLS:
                for arg in sub.args:
                    _consume(arg)
            elif isinstance(sub, (ast.For, ast.AsyncFor, ast.comprehension)):
                _consume(sub.iter)
    return call_ids, names


def escaping_lazy_calls(try_node: ast.Try) -> list[str]:
    """Labels of the lazy-iterator calls in @try_node's BODY whose iteration
    is NOT forced anywhere in that same body -- the tan-cli#1132 shape, a
    `try` that cannot catch because the filesystem work happens after it.

    Only `try_node.body` is inspected. A lazy call forced in the `else:` or
    `finally:` clause, or after the whole statement, is still an escape:
    those run outside the handlers' cover, which is the entire point."""
    forced_ids, forced_names = _forced_within(try_node.body)
    aliased: dict[str, int] = {}
    for stmt in try_node.body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Call) \
                    and _lazy_call_label(sub.value):
                for target in sub.targets:
                    if isinstance(target, ast.Name):
                        aliased[target.id] = id(sub.value)
            # `with os.scandir(root) as entries:` binds the same way an
            # assignment does, and BOTH real call sites in this tree spell it
            # that way -- an earlier draft of this detector handled only
            # `ast.Assign` and reported `examples_cmd._subdirectories` and
            # `presets_cmd._entries` as escapes when each forces its iterator
            # (a comprehension, `list(it)`) one line down. Read by hand, not
            # assumed clean: that is the correction.
            elif isinstance(sub, (ast.With, ast.AsyncWith)):
                for item in sub.items:
                    if isinstance(item.context_expr, ast.Call) \
                            and _lazy_call_label(item.context_expr) \
                            and isinstance(item.optional_vars, ast.Name):
                        aliased[item.optional_vars.id] = id(item.context_expr)
    for name in forced_names:
        if name in aliased:
            forced_ids.add(aliased[name])

    escaping: list[str] = []
    for stmt in try_node.body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Call) and id(sub) not in forced_ids:
                label = _lazy_call_label(sub)
                if label is not None:
                    escaping.append(f"{label}(...) at line {sub.lineno}")
    return escaping


class Candidate(NamedTuple):
    """One function the walk selected, and WHY. `shapes` carries
    `"narrow-except"`, `"lazy-escape"`, or both -- a function can be picked
    for either reason, and reporting which matters because the two need
    different follow-up (SHAPE 1 wants the handler widened; SHAPE 2 wants
    the iteration moved inside the `try`, or the primitive swapped)."""

    dotted: str
    qualname: str
    lineno: int
    shapes: frozenset[str]
    lazy_detail: tuple[str, ...]


def find_candidates(include_planner: bool) -> list[Candidate]:
    """Every function carrying SHAPE 1 (a `try` wrapping risky I/O with a
    non-broad `except`) or SHAPE 2 (a `try` whose lazy-iterator call is
    iterated outside it) -- see the module docstring for both."""
    found: list[Candidate] = []
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
                    shapes: set[str] = set()
                    lazy_detail: list[str] = []
                    for sub in ast.walk(child):
                        if not isinstance(sub, ast.Try):
                            continue
                        names: set[str] = set()
                        for h in sub.handlers:
                            names |= _except_names(h)
                        # SHAPE 1's selection is unchanged from the original
                        # walk, including its treatment of a handler-less
                        # `try`/`finally` (empty `names`, so not BROAD).
                        if _calls_risky_io(sub) and not (names & BROAD):
                            shapes.add("narrow-except")
                        # SHAPE 2 needs an `except` to call dead -- a
                        # `try`/`finally` has no handler to be unreachable --
                        # but does NOT filter on handler BREADTH: an `except
                        # Exception` around a lazy call it never covers is
                        # exactly as dead as an `except OSError`.
                        escaping = escaping_lazy_calls(sub) if sub.handlers else []
                        if escaping:
                            shapes.add("lazy-escape")
                            lazy_detail.extend(escaping)
                    if shapes:
                        found.append(Candidate(
                            dotted, qual, child.lineno,
                            frozenset(shapes), tuple(lazy_detail)))
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
    lazy_escapes = 0
    for candidate in candidates:
        dotted, qualname, lineno = candidate.dotted, candidate.qualname, candidate.lineno
        shape = "+".join(sorted(candidate.shapes))
        if "lazy-escape" in candidate.shapes:
            lazy_escapes += 1
        if dotted.split(".")[1] == "planner":
            tally["planner"] += 1
            print(f"{dotted}::{qualname}:{lineno} [{shape}] -- planner (not executed here)")
        else:
            verdict = try_execute(dotted, qualname)
            bucket = verdict.split(" ", 1)[0].split(":", 1)[0]
            tally[bucket] = tally.get(bucket, 0) + 1
            print(f"{dotted}::{qualname}:{lineno} [{shape}] -- {verdict}")
        for detail in candidate.lazy_detail:
            # A STATIC finding, not an execution verdict: the `try` body
            # built this iterator and nothing in that body forced it. Read
            # the function; `try_execute`'s one non-UTF-8 shape says nothing
            # about it either way.
            print(f"    lazy-escape: {detail}")

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
    # tan-cli#1132: a SECOND tally line, deliberately not folded into the
    # first. The line above buckets by what EXECUTION found (one shape, on
    # the minority of candidates whose signature this script can guess);
    # this one counts a purely STATIC finding over every candidate, executed
    # or not. Summing them would invent a number that means neither thing.
    print(f"lazy-escape (static, shape 2): {lazy_escapes} of {len(candidates)} "
          f"candidate(s) build a lazy iterator inside a `try` and force it "
          f"outside")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
