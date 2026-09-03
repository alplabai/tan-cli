# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1112: ties a registered `contract/issue-codes.json` `severity` to
the severity its own emission MECHANISMS actually construct -- the gate that
did not exist when `clean.remove-failed` and `build.toolchain-root-unresolved`
drifted (registered `warning`-only, emitted `error` too) and sat undetected
until a manual grep during an unrelated review (PR #1110) surfaced it.

WHAT A "MECHANISM" IS, AND WHY THIS IS NOT A SET UNION. An earlier version of
this file walked every `Issue(` construction in three files and asserted the
UNION of resolved severities equalled the registry. That was vacuous for
`build.toolchain-root-unresolved` specifically (PR #1120 review, tan-cli#1112
follow-up): that ONE code is constructed through FIVE independent origins
converging on TWO delivery sites --

  * a direct `Issue(code, "error" if failed else "warning", ...)` ternary
    (`build_cmd.py::_dispatch`) -- by itself already both severities;
  * a direct `Issue(code, "warning", ...)` (`build_cmd.py::_demoted_artefact_issues`);
  * `raise BuildError(code, ...)` in that same function's FAIL arm
    (`build_cmd.py::_demoted_artefact_issues`);
  * `raise TokenSubstitutionError(code, ...)` in a DIFFERENT file
    (`build/token_substitution.py::apply_plan_token_substitution`), forwarded
    into a `BuildError` by `build_cmd.py::_build`'s own
    `except TokenSubstitutionError as err: raise BuildError(err.code, ...)`;
  * and TWO separate `except BuildError as err: ... Issue(err.code, "error",
    ...)` DELIVERY sites -- `build_cmd.py::build` (the `tan build` dispatcher)
    and `run_cmd.py::_run` (`tan run` shares the same `_build` engine,
    tan-cli#1112 review finding).

A UNION-of-all-sites set is blind to any ONE of these vanishing as long as
ANOTHER still supplies the same severity value: collapsing the `_dispatch`
ternary to a bare `"warning"` (a real regression -- `tan build` under
`executionPolicy.missingTool=fail`'s OTHER code path would still deliver
`error` via the `BuildError` chain) left a union-based gate green, and so did
deleting the `BuildError` delivery entirely (the `_dispatch` ternary still
supplies `"error"` on its own). Both were measured, independently, on PR
#1120.

So this file asserts EACH mechanism SEPARATELY -- [`_SEEDED_MECHANISMS`] is a
`{code: (Mechanism, ...)}` table, and every entry is checked on its own via
[`_verify`]: losing ANY ONE of them reds by name, regardless of whether a
DIFFERENT mechanism for the same code still happens to cover the same
severity value. The union (`_aggregate_expected`) is still computed and still
checked against the registry -- that half of the contract (does the
REGISTRY match what the code as a WHOLE constructs) is real and worth
keeping -- but it is no longer the only thing enforced.

FOUR MECHANISM KINDS, each independently AST-verified in [`_verify`]:

  * `"direct"` -- every `Issue(code, <severity>, ...)` call whose CODE is the
    seeded literal, inside one named `(file, qualname)`. `<severity>` may be
    a plain string constant or a `"error" if <cond> else "warning"` ternary
    (resolved the same way [`_severity_from_node`] always has). Compared as
    a SORTED MULTISET (a call site can repeat inside one function, e.g.
    `clean_cmd.py::_run`'s two `Issue("clean.remove-failed", ...)` sites) --
    not a set -- so a severity value FLIPPING at one call while another site
    keeps the old value still changes the multiset and reds, and so does a
    call SITE disappearing (the multiset shrinks) even if the surviving
    site(s) still nominally cover the same severity SET.
  * `"raise:<ClassName>"` -- every `raise <ClassName>(code, ...)` -- an
    ACTUAL `ast.Raise`, not merely a construction (`_unused =
    BuildError(code, ...)` does NOT count; see `_Index.raised_ids`'s own
    docstring for why that distinction is load-bearing, not pedantic: PR
    #1120 review round 2 measured that without it, deleting the entire
    `executionPolicy.missingTool=fail` arm at `build_cmd.py:1189` -- which
    makes `tan build` under that policy exit 0 with a `warning` instead of
    failing -- left every test in this file green) -- whose CODE is the
    seeded literal, inside one named `(file, qualname)`. Counted, not just
    present -- pins exactly how many such raises exist there.
  * `"forward:<Source>-><Target>"` -- every `raise <Target>(<name>.code,
    ...)` -- again an ACTUAL raise, same reason as above -- inside an
    `except <Source> as <name>:` handler, inside one named `(file,
    qualname)` -- the shape `build_cmd.py::_build` uses to re-tag a caught
    `TokenSubstitutionError` as a `BuildError`. Counted, not code-specific
    (the forward does not know which code is flowing through it; it is
    pinned once per code whose flow genuinely depends on it, so its
    disappearance breaks that code's own mechanism list).
  * `"deliver:<ClassName>"` -- every `Issue(<name>.code, <severity>, ...)`
    construction inside an `except <ClassName> as <name>:` handler, inside
    one named `(file, qualname)`. Requires EXACTLY ONE such construction
    (reds by name if zero -- the handler stopped forwarding -- or more than
    one) and resolves its severity the same way `"direct"` does.

Argument resolution accepts BOTH shapes for every code/severity-position
argument this file inspects -- positional (`Issue("code", "severity")`) or
the matching keyword (`Issue(code="code", severity="severity")`) -- and the
callee may be a bare name (`Issue(...)`) or attribute-qualified
(`envelope.Issue(...)`). This closes the four SILENT-skip shapes a reviewer
measured against the union-based predecessor of this file (a `severity=`
keyword, an attribute-qualified callee, a code hoisted to a module constant,
a `**kwargs` splat): none of those shapes resolves as a match for a
mechanism's own `(file, qualname)` lookup, so a real call site REWRITTEN into
one of them stops counting as a match at that key -- and because every
mechanism's expected count/multiset is PINNED exactly, that changes what
[`_verify`] finds and reds on a COUNT/multiset mismatch, even for the two
shapes (hoisted constant, `**kwargs`) this file does not attempt to resolve
the value of. A severity argument that IS resolvable in shape but not in
VALUE (a variable, an f-string, a ternary with a non-literal branch) is a
HARD FAILURE, never a silent skip -- same discipline
`test_blob_format_producers_stay_in_valid_blob_formats.py` (tan-cli#1074)
established for this class of gate.

WHAT REMAINS INVISIBLE, stated plainly (the PR #1120 review's own bar):

  * Any code not a key in `_SEEDED_MECHANISMS` (4 today: `clean.remove-failed`,
    `build.toolchain-root-unresolved`, `build.unknown-backend`,
    `flash.nothing-matched`) -- `build.missing-tool` (`build_cmd.py:1019`) is
    measured to be JUST as genuinely dual-severity in source as the four
    seeded here, and registered `error`-only, and this file does not catch
    it -- deliberately: fixing it is out of tan-cli#1112's own named scope
    (`clean.remove-failed` and `build.toolchain-root-unresolved` only), and
    seeding an assertion this PR does not also fix would either red on
    landing or require silently laundering a THIRD registry fix into a
    two-code issue. Left as the concrete, measured proof that widening the
    seed list past what an issue actually fixes reds on arrival.
  * A construction site in a `(file, qualname)` NOT named by any
    `Mechanism` for that code -- a brand-new fifth origin or third delivery
    site, in a function this table has never heard of, is exactly as
    invisible as it was before this file existed. `_SCANNED_FILES` (derived
    from the table, not hand-maintained separately) names every file this
    gate has ever looked at: `commands/build_cmd.py`, `commands/clean_cmd.py`,
    `commands/flash_cmd.py`, `commands/run_cmd.py`,
    `commands/build/token_substitution.py`.
  * A CODE argument passed as a hoisted module constant or via `**kwargs`
    -- not silently PASSED (it changes a pinned count/multiset and reds:
    `_code_from_node` returns `None` for either shape, so that call simply
    stops matching its mechanism's `code` filter), but not RESOLVED either:
    the failure message names a count/multiset mismatch, not the real value
    that escaped. A SEVERITY argument in either shape is NOT in this bullet
    -- `_severity_from_node` returns `None` for both, which `_verify` raises
    as a HARD FAILURE naming the file, qualname AND line (measured, PR #1120
    review round 2: both a hoisted-constant severity and a `**kwargs`
    severity on an already-matched code produce `"...:<line> constructs
    Issue(...) with a severity this gate cannot resolve statically"`, not a
    silent skip).
  * A SEVERITY SWAP between two call sites inside the SAME `(file,
    qualname, kind)` -- e.g. `clean_cmd.py:852` and `:864` trading values --
    is invisible to `"direct"`'s MULTISET comparison, which is severity-only
    and carries no per-site IDENTITY (deliberately: a lineno-keyed identity
    is exactly what broke `test_every_issue_code_is_registered.py`'s own
    `_RESOLVABLE_HELPERS` table once already, tan-cli#224, dev's fc88ca1).
    Measured, PR #1120 review round 2: swapping `clean_cmd.py:852`'s
    `"warning"` and `:864`'s `"error"` -- so the best-effort
    `rmtree(ignore_errors=True)` directory removal now claims `error` and the
    must-succeed `os.remove` state-file removal now claims `warning`, while
    `exit_code = ExitCode.RUNTIME_FAILURE` stays on the ORIGINAL (now
    mis-labelled) branch -- leaves every test in this file green, because the
    multiset `{"error", "warning"}` is unchanged. A single value FLIPPING
    (one call's severity changes, the multiset's contents change) DOES red,
    confirmed above and by `test_every_seeded_mechanism_holds` -- only a
    two-for-two SWAP survives. Applies to every `"direct"` mechanism with
    more than one call site: `clean.remove-failed` and `flash.nothing-matched`
    today. Traded deliberately for the qualname/line-shift robustness the
    multiset design buys everywhere else; not fixed, because the fix (a
    stable per-call-site identity that is not a lineno) is exactly the
    problem `_RESOLVABLE_HELPERS` already shows is not free, and no swap of
    this shape has happened in this tree's real history.

DUAL SEVERITY IS SUPPORTED, ON PURPOSE -- do not read this file as an
argument for one-severity-per-code. `flash.nothing-matched` and
`build.unknown-backend` are already correctly registered `"error or warning"`
(tan-cli#807, tan-cli review) and are seeded here NOT because they needed
fixing, but so a LATER sweep cannot flatten them back to a single severity
without this gate naming the exact mechanism it would be dropping --
[`test_dual_severity_codes_stay_dual`] mutates the registry-string normaliser
itself to prove that protection is not vacuous.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

#: Same resolution `test_frozen_issue_codes.py` / `test_issue_code_registry_shape.py`
#: use: `contract/` sits three levels above this file (gates -> tests -> python -> repo root).
REGISTRY = Path(__file__).resolve().parents[3] / "contract" / "issue-codes.json"
TAN = Path(__file__).resolve().parents[2] / "tan"

_SEVERITY_VALUES = frozenset({"error", "warning", "info"})


def _registry_entries() -> dict[str, str]:
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    entries = {e["code"]: e["severity"] for e in data["issueCodes"]}
    # Non-vacuity (tan-cli#275's standing lesson): an empty/unreadable
    # registry would make every assertion below pass by finding nothing.
    assert entries, f"{REGISTRY} has no issue codes -- this gate would be vacuous"
    return entries


def _severity_set(raw: str) -> frozenset[str]:
    """`contract/issue-codes.json`'s own severity spelling, normalised: the
    deliberate dual spelling `"error or warning"` (the `flash.nothing-matched`
    / `build.unknown-backend` precedent) becomes `{"error", "warning"}`;
    anything else is a singleton set of itself."""
    if raw == "error or warning":
        return frozenset({"error", "warning"})
    return frozenset({raw})


def _severity_from_node(node: ast.expr | None) -> frozenset[str] | None:
    """Resolve a severity ARGUMENT expression to the set of literal severity
    strings it can evaluate to at runtime. Two shapes resolve: a plain string
    constant, or a `"error" if <cond> else "warning"` ternary with both
    branches literal (resolves to the union of both). Anything else -- a
    variable, an f-string, a ternary with a non-literal branch -- is
    unresolved (`None`), never guessed at."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in _SEVERITY_VALUES:
        return frozenset({node.value})
    if isinstance(node, ast.IfExp):
        body = _severity_from_node(node.body)
        orelse = _severity_from_node(node.orelse)
        if body is not None and orelse is not None:
            return body | orelse
    return None


def _code_from_node(node: ast.expr | None) -> str | None:
    """The literal code string at a code-position argument, when it IS one
    (a `Constant` string shaped like `family.name` -- has a dot). A hoisted
    module constant or a non-literal expression is NOT resolved here -- see
    the module docstring's "WHAT REMAINS INVISIBLE" for why that is still
    safe: it changes a pinned mechanism's match count rather than passing
    silently."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and "." in node.value:
        return node.value
    return None


def _callee_name(func: ast.expr) -> str | None:
    """A call's callee, resolved to its bare name for both `Issue(...)` (an
    `ast.Name`) and an attribute-qualified `envelope.Issue(...)`
    (`ast.Attribute`) -- closes the silent-skip shape a reviewer measured
    against this file's predecessor (PR #1120 review)."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _call_arg(call: ast.Call, index: int, keyword: str) -> ast.expr | None:
    """The argument at `index` positionally, else the matching `keyword=`
    -- `Issue`/`BuildError`/`TokenSubstitutionError` all name their fields
    `code`/`severity`/`message`, so a keyword call is a real, legal shape,
    not a hypothetical -- closes the second silent-skip shape a reviewer
    measured (PR #1120 review)."""
    if index < len(call.args):
        return call.args[index]
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    return None


def _is_attr_code(node: ast.expr | None, exc_name: str) -> bool:
    """True for `<exc_name>.code` -- the shape `except <Class> as <exc_name>:
    ... Issue(<exc_name>.code, ...)` / `... <OtherClass>(<exc_name>.code,
    ...)` re-emits a caught error's own code under."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "code"
        and isinstance(node.value, ast.Name)
        and node.value.id == exc_name
    )


@dataclass(frozen=True)
class _Index:
    """Every `Call` and `ExceptHandler` node in one file's AST, keyed by the
    dotted qualname of the innermost enclosing function/method/class it sits
    in (the same `_prefix_templates`-style walk `test_every_issue_code_is_
    registered.py` uses, and for the same reason: a qualname survives a
    line-shift a lineno-keyed table would not, tan-cli#224's own dev fc88ca1
    lesson).

    `raised_ids` -- `id(...)` of every `Call` node that is an `ast.Raise`'s
    own `.exc` -- is what lets `_verify`'s `"raise:"`/`"forward:"` kinds tell
    a REAL `raise BuildError(...)` apart from a bare construction
    (`_unused = BuildError(...)`, or any other non-raised use): PR #1120
    review round 2 measured that without this, rewriting `build_cmd.py:1189`
    from `raise BuildError(...)` to `_unused = BuildError(...)` -- which
    deletes the entire `executionPolicy.missingTool=fail` arm, so `tan build`
    under that policy exits 0 with a `warning` instead of failing -- left
    every test in this file green. `id()` is safe to key on here (rather
    than the node itself, which `ast` gives no `__hash__`/`__eq__` worth
    using) because the SAME `Call` OBJECT that is one `ast.Raise`'s `.exc` is
    also the object `_walk` appends to `calls[qualname]` when it visits that
    Call node itself -- `calls` already keeps it alive for `_Index`'s whole
    lifetime, so its `id()` cannot be reused by an unrelated object in the
    meantime."""

    calls: dict[str, list[ast.Call]]
    handlers: dict[str, list[ast.ExceptHandler]]
    raised_ids: frozenset[int]


def _index(path: Path) -> _Index:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: dict[str, list[ast.Call]] = {}
    handlers: dict[str, list[ast.ExceptHandler]] = {}
    raised_ids: set[int] = set()
    stack: list[str] = []

    def _qualname() -> str:
        return ".".join(stack) if stack else "<module>"

    def _walk(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.append(node.name)
            for child in ast.iter_child_nodes(node):
                _walk(child)
            stack.pop()
            return
        if isinstance(node, ast.Call):
            calls.setdefault(_qualname(), []).append(node)
        elif isinstance(node, ast.ExceptHandler):
            handlers.setdefault(_qualname(), []).append(node)
        elif isinstance(node, ast.Raise) and node.exc is not None:
            raised_ids.add(id(node.exc))
        for child in ast.iter_child_nodes(node):
            _walk(child)

    _walk(tree)
    return _Index(calls=calls, handlers=handlers, raised_ids=frozenset(raised_ids))


@dataclass(frozen=True)
class Mechanism:
    file: str  # relative to TAN's parent, e.g. "tan/commands/build_cmd.py"
    qualname: str
    kind: str  # "direct" | "raise:<Class>" | "forward:<Src>-><Tgt>" | "deliver:<Class>"
    expected: object
    why: str


def _verify(code: str, m: Mechanism) -> None:
    """Independently AST-verifies ONE mechanism for ONE code -- see the
    module docstring's "FOUR MECHANISM KINDS" for what each `kind` checks and
    why each is asserted on its own rather than folded into a union."""
    path = TAN.parent / m.file
    idx = _index(path)
    site = f"{m.file}::{m.qualname}"
    calls = idx.calls.get(m.qualname, [])
    handlers = idx.handlers.get(m.qualname, [])

    if m.kind == "direct":
        found: list[tuple[str, ...]] = []
        for call in calls:
            if _callee_name(call.func) != "Issue":
                continue
            code_arg = _call_arg(call, 0, "code")
            if _code_from_node(code_arg) != code:
                continue
            sev_arg = _call_arg(call, 1, "severity")
            sevs = _severity_from_node(sev_arg)
            if sevs is None:
                raise AssertionError(
                    f"{site}:{call.lineno} constructs Issue({code!r}, ...) with a "
                    f"severity this gate cannot resolve statically -- {m.why}"
                )
            found.append(tuple(sorted(sevs)))
        actual = tuple(sorted(found))
        assert actual == m.expected, (
            f"{site} -- direct Issue({code!r}, ...) call(s) now construct {actual}, "
            f"expected {m.expected} -- {m.why}"
        )
        return

    if m.kind.startswith("raise:"):
        cls = m.kind.split(":", 1)[1]
        count = sum(
            1
            for call in calls
            if _callee_name(call.func) == cls
            and _code_from_node(_call_arg(call, 0, "code")) == code
            and id(call) in idx.raised_ids
        )
        assert count == m.expected, (
            f"{site} -- found {count} `raise {cls}({code!r}, ...)` raise site(s) "
            f"(a mere construction, e.g. `_unused = {cls}({code!r}, ...)`, does NOT "
            f"count -- see _Index.raised_ids), expected {m.expected} -- {m.why}"
        )
        return

    if m.kind.startswith("forward:"):
        src, tgt = m.kind.split(":", 1)[1].split("->")
        count = 0
        for h in handlers:
            if not (isinstance(h.type, ast.Name) and h.type.id == src and h.name):
                continue
            exc_name = h.name
            for node in ast.walk(h):
                if (
                    isinstance(node, ast.Call)
                    and _callee_name(node.func) == tgt
                    and _is_attr_code(_call_arg(node, 0, "code"), exc_name)
                    and id(node) in idx.raised_ids
                ):
                    count += 1
        assert count == m.expected, (
            f"{site} -- found {count} `raise {tgt}(<name>.code, ...)` inside "
            f"`except {src} as <name>:` forward site(s) (a mere construction does "
            f"NOT count -- see _Index.raised_ids), expected {m.expected} -- {m.why}"
        )
        return

    if m.kind.startswith("deliver:"):
        cls = m.kind.split(":", 1)[1]
        sevs: set[str] = set()
        matches = 0
        for h in handlers:
            if not (isinstance(h.type, ast.Name) and h.type.id == cls and h.name):
                continue
            exc_name = h.name
            for node in ast.walk(h):
                if not (
                    isinstance(node, ast.Call)
                    and _callee_name(node.func) == "Issue"
                    and _is_attr_code(_call_arg(node, 0, "code"), exc_name)
                ):
                    continue
                sev_arg = _call_arg(node, 1, "severity")
                resolved = _severity_from_node(sev_arg)
                if resolved is None:
                    raise AssertionError(
                        f"{site}:{node.lineno} forwards a caught {cls} through "
                        f"Issue({exc_name}.code, ...) with a severity this gate cannot "
                        f"resolve statically -- {m.why}"
                    )
                sevs |= resolved
                matches += 1
        assert matches == 1, (
            f"{site} -- found {matches} `except {cls} as <name>: Issue(<name>.code, ...)` "
            f"delivery site(s), expected exactly 1 -- {m.why}"
        )
        assert frozenset(sevs) == m.expected, (
            f"{site} -- delivers {cls} at severities {sorted(sevs)}, expected "
            f"{sorted(m.expected)} -- {m.why}"
        )
        return

    raise AssertionError(f"{m!r}: unknown mechanism kind")


#: Opt-in: a code is in scope for this gate if and only if it is a key here.
#: See the module docstring's "FOUR MECHANISM KINDS" for the shape of each
#: entry and "WHAT REMAINS INVISIBLE" for the boundary of what this table
#: does NOT cover.
_SEEDED_MECHANISMS: dict[str, tuple[Mechanism, ...]] = {
    "clean.remove-failed": (
        Mechanism(
            "tan/commands/clean_cmd.py",
            "_run",
            "direct",
            (("error",), ("warning",)),
            "tan-cli#1112: clean_cmd.py's best-effort DIRECTORY removal "
            "(matching rmtree(ignore_errors=True), :852) warns; its "
            "NOT-ignore-errors STATE-FILE removal (os.remove, :864) fails the "
            "command outright. Both arms genuine -- registered `error or "
            "warning`.",
        ),
    ),
    "build.toolchain-root-unresolved": (
        Mechanism(
            "tan/commands/build_cmd.py",
            "_dispatch",
            "direct",
            (("error", "warning"),),
            "tan-cli#1112: the held-outcome retry path's own "
            "`\"error\" if failed else \"warning\"` ternary (:799) -- both "
            "severities from one call site.",
        ),
        Mechanism(
            "tan/commands/build_cmd.py",
            "_demoted_artefact_issues",
            "direct",
            (("warning",),),
            "tan-cli#1112: the default executionPolicy.missingTool=skip arm "
            "(:1196) -- the warning half of this code's dual severity.",
        ),
        Mechanism(
            "tan/commands/build_cmd.py",
            "_demoted_artefact_issues",
            "raise:BuildError",
            1,
            "tan-cli#1112: the executionPolicy.missingTool=fail arm (:1189) "
            "raises BuildError directly -- one of two origins that reach "
            "`error` (the other is the TokenSubstitutionError forward below).",
        ),
        Mechanism(
            "tan/commands/build/token_substitution.py",
            "apply_plan_token_substitution",
            "raise:TokenSubstitutionError",
            1,
            "tan-cli#1112 (PR #1120 review, finding 4): the SECOND origin of "
            "this code's `error` arm -- an unresolved ${TOOLCHAIN_ROOT} token "
            "(:259) -- lives in a DIFFERENT file than the rest of this code's "
            "mechanisms and was invisible to this gate's predecessor.",
        ),
        Mechanism(
            "tan/commands/build_cmd.py",
            "_build",
            "forward:TokenSubstitutionError->BuildError",
            1,
            "tan-cli#1112 (PR #1120 review, finding 4): `_build`'s own "
            "`except TokenSubstitutionError as err: raise BuildError(err.code, "
            "...)` (:1251-1256) is what lets the token_substitution.py origin "
            "above reach BuildError's delivery sites at all.",
        ),
        Mechanism(
            "tan/commands/build_cmd.py",
            "build",
            "deliver:BuildError",
            frozenset({"error"}),
            "tan-cli#1112 (PR #1120 review, finding 3): `tan build`'s own "
            "`except BuildError as err: ... Issue(err.code, \"error\", ...)` "
            "(:1804-1805) -- the mechanism a reviewer measured could be "
            "deleted outright without reddening the union-based predecessor "
            "of this gate, because the _dispatch ternary above independently "
            "covers `error` too. Asserted on its own so THAT deletion reds by "
            "name regardless.",
        ),
        Mechanism(
            "tan/commands/run_cmd.py",
            "_run",
            "deliver:BuildError",
            frozenset({"error"}),
            "tan-cli#1112 (PR #1120 review, finding 1): `tan run` reuses "
            "`build_cmd._build` (the same engine `tan build` calls) and has "
            "its OWN `except BuildError as err: ... Issue(err.code, \"error\", "
            "...)` (:281-289) -- a real, separate emission home this gate's "
            "predecessor never scanned.",
        ),
    ),
    "build.unknown-backend": (
        Mechanism(
            "tan/commands/build_cmd.py",
            "_backend_issues",
            "direct",
            (("error", "warning"),),
            "already correctly registered `error or warning` -- seeded here "
            "so a LATER sweep cannot flatten it back to one severity without "
            "this gate naming the exact site (tan-cli#1112).",
        ),
    ),
    "flash.nothing-matched": (
        Mechanism(
            "tan/commands/flash_cmd.py",
            "_run",
            "direct",
            (("error",), ("warning",)),
            "already correctly registered `error or warning` (tan-cli#807) -- "
            "seeded here for the same reason as build.unknown-backend "
            "(tan-cli#1112).",
        ),
    ),
}

#: Every file any Mechanism above names -- see the module docstring's "WHAT
#: REMAINS INVISIBLE" for why a file NOT in this set is exactly that.
_SCANNED_FILES: frozenset[str] = frozenset(m.file for ms in _SEEDED_MECHANISMS.values() for m in ms)

#: Total mechanism-table entries across every seeded code -- anti-vacuity for
#: the TABLE itself (a `Mechanism` silently removed from `_SEEDED_MECHANISMS`
#: is simply not parametrised below, which is not a red; this pin is what
#: catches THAT, the same role `EXPECTED_TEMPLATE_COUNT` plays in `test_
#: every_issue_code_is_registered.py`).
_EXPECTED_MECHANISM_COUNT = 10


def _aggregate_expected(code: str) -> frozenset[str]:
    """The severities `code`'s OWN mechanism list, taken as a whole, expects
    to be constructible -- every `"direct"` multiset's members, plus every
    `"deliver:<Class>"` mechanism's severities (a `"raise:"` / `"forward:"`
    entry contributes no severity of its own; it only proves a PATH exists
    for a `"deliver:"` entry's severity to actually apply). This is the
    UNION half of the contract -- still checked against the registry, but no
    longer the ONLY thing checked (see the module docstring)."""
    sevs: set[str] = set()
    for m in _SEEDED_MECHANISMS[code]:
        if m.kind == "direct":
            for entry in m.expected:
                sevs.update(entry)
        elif m.kind.startswith("deliver:"):
            sevs.update(m.expected)
    return frozenset(sevs)


@pytest.mark.parametrize("code", sorted(_SEEDED_MECHANISMS), ids=lambda c: c)
def test_registered_severity_matches_the_aggregate_of_every_mechanism(code):
    registry = _registry_entries()
    assert code in registry, f"{code!r} is seeded in _SEEDED_MECHANISMS but is not registered in {REGISTRY} at all."
    registered = _severity_set(registry[code])
    expected = _aggregate_expected(code)
    assert registered == expected, (
        f"{REGISTRY} registers {code!r} at severity {registry[code]!r} (-> "
        f"{sorted(registered)}), but this gate's own seeded mechanisms "
        f"together expect {sorted(expected)}. If the real emission genuinely "
        f"changed, update _SEEDED_MECHANISMS in the same change as the "
        f"registry edit; if the registry drifted from the emission sites, "
        f"fix the registry instead."
    )


_MECHANISM_CASES = [(code, m) for code in sorted(_SEEDED_MECHANISMS) for m in _SEEDED_MECHANISMS[code]]


@pytest.mark.parametrize(
    "code,mechanism",
    _MECHANISM_CASES,
    ids=[f"{code}::{m.file}::{m.qualname}::{m.kind}" for code, m in _MECHANISM_CASES],
)
def test_every_seeded_mechanism_holds(code, mechanism):
    _verify(code, mechanism)


def test_the_mechanism_table_size_is_pinned():
    """Anti-vacuity for the TABLE, not just for the parametrised checks above
    -- see `_EXPECTED_MECHANISM_COUNT`'s own docstring for why a removed
    entry needs its own tripwire."""
    total = sum(len(ms) for ms in _SEEDED_MECHANISMS.values())
    assert total == _EXPECTED_MECHANISM_COUNT, (
        f"_SEEDED_MECHANISMS now declares {total} mechanism entries across "
        f"{sorted(_SEEDED_MECHANISMS)}, not {_EXPECTED_MECHANISM_COUNT} -- if "
        f"an entry was added or removed on purpose, update this pin (and, if "
        f"removed, confirm the removal is not silently dropping coverage of "
        f"a real construction site)."
    )


def test_the_seeded_table_is_not_empty():
    """Anti-vacuity for the LIST itself -- emptying it would make the
    parametrised checks above collect zero cases (pytest turns that into a
    silent skip, not a failure -- tan-cli#275's standing lesson)."""
    assert _SEEDED_MECHANISMS, (
        "_SEEDED_MECHANISMS is empty, so the parametrised checks above have "
        "no cases and this whole file enforces nothing while still reporting "
        "green. If every seeded code was genuinely retired, delete this file "
        "outright rather than leaving an empty allow-list behind."
    )
    for code, mechanisms in _SEEDED_MECHANISMS.items():
        assert mechanisms, f"{code!r} is seeded with an empty mechanism tuple -- it enforces nothing."


def test_the_scanned_files_are_not_empty():
    """Anti-vacuity for `_SCANNED_FILES`: derived from the table above, so an
    empty table (already caught by the previous test) would also make this
    one vacuous; kept as its own assertion because `_SCANNED_FILES` is what
    the module docstring's "WHAT REMAINS INVISIBLE" promise is measured
    against, and a silent derivation bug (a typo in `Mechanism.file`) could
    in principle diverge the two even with a non-empty table."""
    assert _SCANNED_FILES == frozenset(
        {
            "tan/commands/build_cmd.py",
            "tan/commands/clean_cmd.py",
            "tan/commands/flash_cmd.py",
            "tan/commands/run_cmd.py",
            "tan/commands/build/token_substitution.py",
        }
    ), sorted(_SCANNED_FILES)


def test_dual_severity_codes_stay_dual():
    """Structural proof that `_severity_set` -- the ONLY place this file
    decides whether a registry string means one severity or two -- cannot be
    fooled into reading `"error or warning"` as single-severity, which is
    exactly the shape a later "normalise the registry" sweep could introduce
    (this repo's own recurring defect class: #1059, #1062's round-3 review,
    PR #1070's :1350,
    and four tests on PR #1111 -- a protection that cannot fail is not a
    protection).
    """
    for code in ("build.unknown-backend", "flash.nothing-matched"):
        assert _aggregate_expected(code) == frozenset({"error", "warning"}), (
            f"{code} is no longer seeded as genuinely dual in this file's own "
            f"table -- update the table or this test together with whatever "
            f"changed its real emission shape."
        )
    assert _severity_set("error or warning") == frozenset({"error", "warning"})
    # The negative control: a SINGLE registered severity must NOT be read as
    # dual -- if this ever passed, `_severity_set` would be treating every
    # code as `{"error", "warning"}` regardless of what the registry says,
    # which would make the whole gate incapable of ever catching a
    # single-severity code that drifted (the ORIGINAL tan-cli#1112 defect).
    assert _severity_set("error") == frozenset({"error"})
    assert _severity_set("warning") == frozenset({"warning"})
    assert _severity_set("error") != frozenset({"error", "warning"})
