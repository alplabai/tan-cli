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
carrying any of THREE shapes. `--planner` widens the walk to include
`tan/planner/**` too, reported separately, since import order makes those
undriveable from this script directly -- read the function, do not execute
it, the same manual step tan-cli#1116's review round 2 took for `_docs_ref`
and `render_to_envelope`.

THE THREE SHAPES IN ONE LINE EACH, since "which shapes does this detect"
is the question this tool is most often asked and most easily wrong about:

  1. narrow-except -- there IS a `try`, and its `except` is narrower than
     the I/O it wraps.
  2. lazy-escape   -- there IS a `try`, and the filesystem work happens on
     ITERATION, outside it, so no handler can fire.
  3. absent-try    -- there is NO `try`, and the function declares a
     contract the read can breach.

And the shapes it does NOT detect, stated with the same weight (each is
expanded in its own section below): a read whose only handler is in a
CALLER too narrow to absorb it, when the reading function itself declares
nothing (shape 3 does not walk callers); a swallow, where the primitive
returns a benign value instead of raising at all, so no exception ever
exists to catch (the tan-cli#1127 shape -- `Path.glob` on a denied
ancestor, `Path.is_file()` on 3.14.7); and a handler that is correctly
broad but WRONG about what it then does. None of the three shapes below is
a substitute for reading the function.

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
    whatever `x` is.
  * An alias or a forcing spelling this walk does not model errs in BOTH
    directions, not only toward silence (measured on synthetic input):
    a lazy call reached through a helper (`m = _lazy(d)`) is a false
    NEGATIVE, invisible; a WALRUS binding (`if (m := d.glob(...)):` then
    `list(m)` inside the same `try`) is a false POSITIVE, because
    `_forced_within` reconciles names only against `ast.Assign` and
    `ast.With` bindings; and so is forcing spelled as a method rather than
    one of `FORCING_CALLS` (`",".join(d.glob(...))`). Both false-positive
    shapes report as escapes they are not. Pinned either way in
    `tests/scripts/test_audit_narrow_except_contracts.py`, so the gap is a
    recorded limit rather than a surprise.
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

SHAPE 3, "there is no `try` at all" (tan-cli#1133): a function that
DECLARES a contract and performs a risky read that no handled `try` in that
same function covers. This is the shape the first two are structurally
blind to -- a site with no `try` is not a narrow `try` and has no dead
handler -- and it is why two live sites in `tan/planner/template.py`
(`_load_som_doc`, `_board_route_entries`) survived a sweep, a gate and
three reviews: each ran `is_file()` pre-flight -> bare `read_text` -> bare
`yaml.safe_load`, so a non-UTF-8 byte, a malformed document or a `chmod
000` file escaped raw out of `emit_scaffold`, whose caller catches
`TemplateError` and nothing else.

    som_path = metadata_root / "e1m_modules" / f"{sku}.yaml"
    if not som_path.is_file():
        raise TemplateError(...)          # a curated contract, declared
    return _require_mapping_doc(
        yaml.safe_load(som_path.read_text(encoding="utf-8")) or {},  # bare
        path=som_path, what="SoM preset")

"Declares a contract" is `declared_contract` below, and it is read off the
FUNCTION, never inferred: either the function raises a NON-BUILTIN
exception class somewhere in its own body (a curated contract by
construction -- it chose to speak in a project exception type, and a raw
`OSError` beside that choice breaches it), or its docstring carries one of
`CONTRACT_PHRASES` (a quiet-return contract). "No `try` covers it" means
not lexically inside the BODY of a `try` that has at least one handler --
a `try`/`finally` is not cover, since it re-raises exactly what SHAPE 3 is
hunting.

WHAT SHAPE 3 DOES NOT CLAIM, spelled out to the same standard as SHAPE 2:

  * It is NAME-based like the other two: `x.read_text(...)`/`x.safe_load
    (...)`/`open(...)` count whatever `x` is, and a read reached through a
    helper this walk does not model is invisible.
  * It uses the function's OWN declared contract as the evidence and does
    NOT walk callers, so the OTHER half of the issue's definition -- "or
    whose caller's only handler cannot absorb what the read raises" -- is
    a deliberate FALSE NEGATIVE here. `analyze._resolve_table`
    (tan-cli#1132's second site) is the worked example: it declared its
    contract only in prose this walk's phrase list does not match and
    raised nothing curated, so shape 3 would not have reported it either.
    Caller-side contract inference needs a call graph this script does not
    build; that gap is recorded, not closed.
  * A read that is genuinely SAFE where it stands -- because the caller
    two frames up wraps the whole call in a broad handler on purpose -- is
    a FALSE POSITIVE, for the same reason. A SHAPE 3 line is a candidate
    to read, never a confirmed defect, exactly like SHAPE 1's `ESCAPED`
    and SHAPE 2's `lazy-escape`.
  * The docstring-phrase half is a literal substring match over
    `CONTRACT_PHRASES`, not English parsing. A function that promises
    quiet-return in words this list does not contain is missed.
  * It says nothing about whether the read can actually FAIL. A
    `read_text` on a path this same function just wrote is reported like
    any other. `commands/build_cmd::_build` is the live example on this
    tree: PR #1160's review checked it and it is a FALSE POSITIVE --
    `_acquire_plan` returns `text, parse_build_plan(text)`
    (`build_cmd.py:546`), so the later `json.loads(text)` on that same
    already-parsed string cannot fail.
  * It is FUNCTION-SUBTREE based, not function-body based:
    `declared_contract` and `unguarded_risky_reads` both `ast.walk` the
    whole subtree, so a nested `def`'s curated raise counts as the
    ENCLOSING function's contract evidence, and a nested `def`'s bare read
    is attributed to the enclosing function too -- and DOUBLE-counted,
    since `find_candidates` visits nested defs separately and may report
    the same read twice under two qualnames. Shape 1 has always had the
    same imprecision (its `try` scan walks subtrees too); it is recorded
    here rather than fixed because no function in this tree currently
    carries the shape, so a fix would be untested by construction.

What IS pinned in `tests/scripts/test_audit_narrow_except_contracts.py`:
the positive half (the pre-#1133 `_load_som_doc` body, transcribed, reports
both of its bare calls), the selectivity half (a bare read with no declared
contract, a `raise ValueError`, and a read already inside a handled `try`
are each NOT reported), that a `try`/`finally` is not cover, and the
caller-side FALSE NEGATIVE above, as a measurement rather than an approval.
The false-POSITIVE direction is argued, not pinned: it needs a real site in
this tree where a broad caller genuinely makes a bare read safe, and there
is none to point at today. Stated here rather than left implied.

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

RE-MEASURED at the tan-cli#1133 fix, and the tally MOVED. Every number here
was produced by running this script; the DECOMPOSITION matters as much as
the totals, because "the count went up" has two unrelated causes and an
earlier draft of this paragraph collapsed them into one and got it wrong:

    tree        script      candidates            absent-try
    ---------------------------------------------------------------
    #1132       #1132       65  /  79 --planner   (shape did not exist)
    dev         dev         66  /  --             (shape did not exist)
    dev         this        67  /  87 --planner   1  /  9
    #1133 fix   this        69  /  85 --planner   1  /  5

Read down the `candidates` column: **65 -> 66 is tree growth** since #1132,
nothing to do with this change. **66 -> 67 is this script's new SHAPE 3**,
which selects exactly one function the other two shapes never saw
(`commands/build_cmd::_build`). **67 -> 69 is the #1133 fix's own new
code**: `document_guards.require_readable_bytes` and
`require_yaml_mapping_doc` are themselves narrow-`except` reads, so they
join as SHAPE 1 candidates -- which is correct, and the honest cost of
guarding anything.

The `--planner` column moves the other way, 87 -> 85, and that is the fix
landing: `template::_load_som_doc`, `_board_route_entries`,
`_rendered_bytes` and `render_to_envelope` all DROP OUT of the walk
entirely, because their bare reads are gone. Shape 3 falls 9 -> 5 for the
same reason.

Shape 3's detector is not vacuous at that 5, and this is the measurement
that proves it: run over the pre-fix tree it reports **9 absent-try** and
names every site the fix touched -- `_load_som_doc` (`som_path.read_text`
and `yaml.safe_load`, both line 653), `_board_route_entries` (both line
826), `render_to_envelope` (`yaml.safe_load`, line 1974) and
`_rendered_bytes` (`read_bytes`, line 509).

THE FIVE THAT REMAIN, and what is known about each -- a list, not a
number, because "five candidates" invites exactly the deferral that let
`_rendered_bytes` sit unread through a whole PR:

  * `commands/build_cmd::_build:1234` -- a confirmed FALSE POSITIVE.
    `_acquire_plan` returns `text, parse_build_plan(text)`
    (`build_cmd.py:546`), so the later `json.loads(text)` on that same
    string cannot fail. Checked, not assumed.
  * `planner/libraries::load_manifest:232` and `planner/zephyr_board::
    _load_soc_spec:144` -- the same defect shape as the fixed four, but on
    the `tan build` path, where `build_cmd.py:505`'s broad `except
    Exception` absorbs them into a coded `build.plan-unavailable`
    envelope. A poor message rather than a traceback: real, filed, not
    urgent.
  * `planner/kconfig_symbols::_load_board_symbols:389` and `planner/
    topology::_core_os_choices:62` -- not yet driven.

All four open ones are tracked individually in tan-cli#1162, deliberately
as a LIST rather than as the count "five candidates": #1133's own PR filed
`template::_rendered_bytes` inside an undifferentiated six-candidate list
and it turned out to be the busiest live defect of the set.

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
selected it, then THREE tally lines -- the execution one (`N OK (non-UTF-8
only) / N unexecuted / N ESCAPED (non-UTF-8 only)`), the static
`lazy-escape (static, shape 2)` one, and the static `absent-try (static,
shape 3)` one -- on every run. Read the tallies before the per-line output,
since most candidates are `unexecuted` and a listing alone hides that
ratio. The three are deliberately NOT summed: they count different things
over different populations (one function can appear in more than one), and
one number covering them would mean none of the three.
"""
from __future__ import annotations

import argparse
import ast
import builtins
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

#: SHAPE 3's docstring evidence for a QUIET-RETURN contract. Lower-cased
#: substring match against the function's own docstring. Deliberately short
#: and literal: these are the phrases this tree's quiet-return functions
#: actually use (`example_catalog.unsupported_som`, `perf.read_perf_point`,
#: `som_buildability.hw_rev_not_buildable`, `analyze._resolve_table`), not a
#: general attempt to parse English. See SHAPE 3's limits in the module
#: docstring.
CONTRACT_PHRASES = (
    "never raises",
    "does not raise",
    "returns none on",
    "none on every failure",
    "returns the empty",
)


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


def _risky_call_label(node: ast.Call) -> str | None:
    """`"som_path.read_text"` for a call SHAPE 1 would call risky I/O, else
    `None` -- the same `RISKY_ATTRS`/`open` test `_calls_risky_io` makes,
    reported per-call instead of per-`try`."""
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr in RISKY_ATTRS:
        return ast.unparse(f)
    if isinstance(f, ast.Name) and f.id == "open":
        return "open"
    return None


def _covered_call_ids(func: ast.AST) -> set[int]:
    """Ids of every call lexically inside the BODY of a `try` that has at
    least one handler, anywhere in @func.

    `try`/`finally` with no `except` does not count as cover: it re-raises
    whatever the body raised, which is exactly the escape SHAPE 3 is looking
    for. The `else:` clause does not count either -- it runs outside the
    handlers, the same reasoning `escaping_lazy_calls` gives for SHAPE 2.
    """
    covered: set[int] = set()
    for sub in ast.walk(func):
        if isinstance(sub, ast.Try) and sub.handlers:
            for stmt in sub.body:
                for inner in ast.walk(stmt):
                    if isinstance(inner, ast.Call):
                        covered.add(id(inner))
    return covered


def declared_contract(func: ast.AST) -> str | None:
    """WHY this function's own outcome on failure is a promise -- the
    evidence SHAPE 3 needs before an unguarded read is worth reporting, or
    `None` when the function makes no such promise.

    Two kinds, both read off the function itself and neither inferred from
    its callers (see SHAPE 3's limits in the module docstring):

    * a CURATED RAISE -- the function raises a non-builtin exception class
      somewhere in its own body. That is a declared contract by
      construction: the function has chosen to speak in a project exception
      type, and a bare read beside that choice escapes as a raw stdlib class
      instead. Builtin classes are excluded deliberately -- a function
      raising `ValueError` promises nothing a raw `OSError` would breach.
    * a QUIET-RETURN DOCSTRING -- one of `CONTRACT_PHRASES` above.
    """
    for sub in ast.walk(func):
        if isinstance(sub, ast.Raise) and sub.exc is not None:
            exc = sub.exc
            name = _name(exc.func) if isinstance(exc, ast.Call) else _name(exc)
            if name.endswith("Error") and not hasattr(builtins, name):
                return f"raises {name}"
    doc = ast.get_docstring(func) if isinstance(
        func, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
    for phrase in CONTRACT_PHRASES:
        if phrase in (doc or "").lower():
            return f"docstring says {phrase!r}"
    return None


def unguarded_risky_reads(func: ast.AST) -> list[str]:
    """Labels of the risky-I/O calls in @func that NO `try` in @func covers
    -- the tan-cli#1133 shape, a read with no handler at all rather than one
    whose handler is too narrow.

    Returns `[]` for a function whose reads are all inside a handled `try`,
    whatever that handler catches: judging the handler's BREADTH is SHAPE
    1's job, and a call cannot be both shapes at once.
    """
    covered = _covered_call_ids(func)
    out: list[str] = []
    for sub in ast.walk(func):
        if isinstance(sub, ast.Call) and id(sub) not in covered:
            label = _risky_call_label(sub)
            if label is not None:
                out.append(f"{label}(...) at line {sub.lineno}")
    return out


class Candidate(NamedTuple):
    """One function the walk selected, and WHY. `shapes` carries
    `"narrow-except"`, `"lazy-escape"`, `"absent-try"`, or any combination
    -- a function can be picked for more than one reason, and reporting
    which matters because each needs different follow-up (SHAPE 1 wants the
    handler widened; SHAPE 2 wants the iteration moved inside the `try`, or
    the primitive swapped; SHAPE 3 wants a handler that does not exist yet,
    or the read routed through a primitive that already has one)."""

    dotted: str
    qualname: str
    lineno: int
    shapes: frozenset[str]
    lazy_detail: tuple[str, ...]
    absent_detail: tuple[str, ...]


def find_candidates(include_planner: bool) -> list[Candidate]:
    """Every function carrying SHAPE 1 (a `try` wrapping risky I/O with a
    non-broad `except`), SHAPE 2 (a `try` whose lazy-iterator call is
    iterated outside it) or SHAPE 3 (a declared contract plus a risky read
    no `try` covers) -- see the module docstring for all three."""
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
                    absent_detail: list[str] = []
                    # SHAPE 3 is per-FUNCTION, not per-`try`: the whole
                    # point is that there may be no `try` to iterate over.
                    contract = declared_contract(child)
                    if contract is not None:
                        unguarded = unguarded_risky_reads(child)
                        if unguarded:
                            shapes.add("absent-try")
                            absent_detail.extend(
                                f"{detail} [{contract}]" for detail in unguarded)
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
                            frozenset(shapes), tuple(lazy_detail),
                            tuple(absent_detail)))
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
    # --planner on the tree this script was written against, and 69 / 85 at
    # the tan-cli#1133 fix; well under half of the smaller number is not a
    # plausible real shrink from one PR, only a broken walk. The floor
    # deliberately does NOT track the measurement upward: it is a
    # vacuous-run tripwire, not a ratchet, and raising it every time the
    # tree grows would turn an honest refactor that deletes candidates into
    # a red run.
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
    absent_tries = 0
    for candidate in candidates:
        dotted, qualname, lineno = candidate.dotted, candidate.qualname, candidate.lineno
        shape = "+".join(sorted(candidate.shapes))
        if "lazy-escape" in candidate.shapes:
            lazy_escapes += 1
        if "absent-try" in candidate.shapes:
            absent_tries += 1
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
        for detail in candidate.absent_detail:
            # Also a STATIC finding: this function declares a contract (the
            # bracketed evidence) and performs this read with no handler
            # anywhere in its own body. Whether the CALLER absorbs what the
            # read raises is not decided here -- read the function.
            print(f"    absent-try: {detail}")

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
    # tan-cli#1133: a THIRD tally line, not folded into either above for the
    # same reason those two are not folded into each other -- it counts a
    # purely STATIC finding over a population selected by a DIFFERENT rule
    # (a declared contract, not a `try`). A candidate can appear in more than
    # one of the three lines; that is why none of them sums with another.
    print(f"absent-try (static, shape 3): {absent_tries} of {len(candidates)} "
          f"candidate(s) declare a contract and perform a risky read no "
          f"`try` in the same function covers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
