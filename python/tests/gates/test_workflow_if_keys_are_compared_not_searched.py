# SPDX-License-Identifier: Apache-2.0
"""A gate reading a GitHub Actions `if:` key must COMPARE it, never SEARCH it.

tan-cli#1145, first of the two mechanical shapes. The motivating instance is
tan-cli#1137's own review finding: the guard gate asserted

    assert RELEASE_OPT_OUT_INPUT in guard

where `guard` is the workflow string ``${{ !inputs.skip_ceiling_interpreter }}``.
A substring test, so deleting the `!` still passed -- measured `32 passed`
before and after the mutation. That one character would have deleted the
ceiling job from every PR *and* run it on the release path, and the gate
written to prevent exactly that was blind to it.

The rule is deliberately small: **a value read out of a workflow `if:` key must
be read WHOLE -- compared with `==`, `!=`, `is` or `is not` against one full
expected value, or tested for membership of a literal collection -- and never
searched for, or against, a substring.** `is` / `is not` are in that list
because `step.get("if") is None` is the whole-value test for an absent key and
is live at `test_getting_started_sdk_outage_probe.py:224`; what they may NOT be
written against is a `str` constant, for the reason below.

`_ALLOWED_OPS` is enforced, not decorative, so every ordering operator is
refused outright; `is` / `is not` survive it only when neither operand is a
`str` constant, and `in` / `not in` only when the guard is the ELEMENT and the
container is not a `str` or an f-string. Refused, therefore:

  * `<needle> in guard` / `not in` -- the guard as the CONTAINER, a substring
    search. Polarity, negation and whitespace all live inside the expression,
    so this frees every character that matters.
  * `guard in "<a str literal>"` / `guard in f"..."` -- the same substring
    search with the operands swapped. See below.
  * `guard is "<a str literal>"` / `is not` -- an identity comparison, which
    can be False for two equal strings. See below.
  * `guard < "z"` and the rest of the ordering operators, which are
    meaningless on a guard expression and which a constant that stopped being
    defined would still satisfy.

The guard on the ELEMENT side is refused only when the CONTAINER is a string.
`step.get("if") in {EXPR_A, EXPR_B}` and `guard in (EXPR_A, EXPR_B)` are
whole-value tests that move the entire expression with any rewrite, so
refusing those would tell the author to do the thing they already did. But
`guard in "<a str literal>"` and `guard in f"..."` are SUBSTRING searches with
the operands swapped, and a string container is decidable from the AST with no
name resolution. Measured against
`ALLOWED_GUARDS = "steps.sdk_list.outputs.outage != 'true'"`, Python answers
True to all four of `"steps.sdk_list.outputs.outage != 'true'"`,
`'steps.sdk_list.outputs.outage'`, `"outage != 'true'"` and `''` -- the last of
which means a step carrying no `if:` key at all passes it. An assertion on a
workflow guard that cannot fail is tan-cli#1145's own headline class, so a
string container is refused on whichever side the guard sits.

`is` and `is not` leave `_ALLOWED_OPS` on the same terms: `guard is "<str>"` is
an IDENTITY test that can be False for equal strings, and the compile-time
warning that says so is exactly what `_parse` swallows (see `_parse`).

An exact comparison forces a semantically-equivalent rewrite
(``${{ ! inputs.x }}``, ``${{ inputs.x != true }}``) to move the expected
constant with it, which is the point: a guard this consequential should not
slip through on "it means the same thing".

Two sites already conform and are what makes this a rule rather than a
heuristic -- `test_getting_started_sdk_outage_probe.py`'s
`step.get("if") == GATE_EXPRESSION`, and #1137's own fix, which converged
independently on `guard == RELEASE_GUARD_EXPRESSION`.

BINDING TRACKING IS LOAD-BEARING, not thoroughness. The defect shape is
`guard = <if-key read>` followed by `<needle> in guard` -- two statements. A
detector that only looked at direct `Compare` operands would miss the single
instance this gate exists for, which is the punchline tan-cli#1145 warns
about: a meta-gate that cannot catch its own motivating example.

Assignment alone is not enough either, and this is measured rather than
argued: rewritten as `wrong = {n: _step(n).get("if") for n in STEPS}` then
`for name, guard in wrong.items()`, that same defect -- with #1137's polarity
fully inverted in the workflow -- reported CLEAN against an assignment-only
walk, in the very file this docstring cites above as conforming. The read
floor did not save it (reads went 9 to 8, still above 6). So `_tainted_names`
covers the `for` target, the comprehension target, `with ... as` and the
walrus as well, treats an already-tainted name as a guard source, and
iterates to a fixpoint.

WHAT THIS DELIBERATELY DOES NOT DO. It does not judge substring assertions in
general. tan-cli#1145's audit found 70 such candidates and read 69 of them:
essentially all assert on driven subprocess stdout/stderr, where presence
genuinely IS the property. Only the `if:`-key surface is decidable, so only it
is gated.

It also does not follow a guard out of the module's own local names, does not
decide a `str` container that was BUILT rather than written, and does not look
past a comparison node at all. Every form below is MEASURED, not guessed at,
and each needs a rule this walk does not have. The first SEVEN are misses; the
last TWO are the over-approximating direction and are named for the same
reason, because `_tainted_names` promises exactly one of the two:

  * a guard passed as a function PARAMETER, or RETURNED / YIELDED out of one
    (`def f(): return step.get("if")` then `g = f()`) -- both need
    interprocedural flow.
  * a guard stored on an ATTRIBUTE or a SUBSCRIPT and read back through it
    (`self.guard = step.get("if")` then `NEEDLE in self.guard`;
    `d["k"] = ...` then `NEEDLE in d["k"]`), and the class-attribute form
    `C.guard`. Binding those would mean tainting the RECEIVER, which taints
    `self` and reds every unrelated `NEEDLE in self.stdout` in the module.
  * a guard IMPORTED from another module -- the scan is per-file.
  * a name in this module rebound from ANOTHER module
    (`import thisfile; thisfile.wrong = {...}`), or by an `exec` run elsewhere
    against this module's namespace. `_binds_names_dynamically` closes the
    in-file half -- one mention of `exec`, `eval`, `globals`, `locals`, `vars`
    or `from x import *` makes every name in the file unresolvable -- but a
    write performed from OUTSIDE the file is invisible to a per-file walk by
    construction, and no version of this scan sees it. It is the one
    fail-closed row this module cannot reach, so it is written down rather
    than papered over. 0 live sites: no file under `python/tests/` writes an
    attribute onto another file under `python/tests/`; the five `setattr`
    sites there all target a `tan` module or a dataclass instance, neither of
    which this scan reads.
  * a guard on the ELEMENT side of a bare `Name` CONTAINER
    (`guard in ALLOWED_GUARDS`). If that name holds a collection this is the
    whole-value test the gate asks for; if it holds a `str` it is a substring
    search, and `"" in ALLOWED_GUARDS` is True, so the assertion passes on a
    step carrying no `if:` key at all. Telling the two apart needs name
    resolution this per-file walk does not do, and refusing the shape outright
    would red the named-collection form -- `guard in ALLOWED_GUARDS` where
    `ALLOWED_GUARDS` is a `frozenset` is the whole-value test this gate asks
    for, and telling an author to inline it would be noise. What IS decidable,
    and IS refused, is exactly three spellings of an inline container: a bare
    `ast.Constant` holding a `str`, an `ast.JoinedStr` (an f-string), and
    implicit adjacent-literal concatenation, which the parser folds into a
    `Constant` before this walk ever sees it. Nothing else -- the next entry
    lists the six built containers that are NOT. Measured across every file
    this scan reads -- 267 on this branch, 270 on the merge with `dev`, so
    read it as "all of them" rather than as a number: 0 live sites put a guard
    on the element side of a `Name` container, so this is a disclosed gap, not
    a live miss.
  * a `str` CONTAINER that is BUILT rather than written as one literal.
    `_string_container` decides the three spellings above and no others.
    Measured, with `guard` tainted:

        guard in "a != b"                refused
        guard in f"{A} or {B}"           refused
        guard in "aaa" "bbb"             refused (the parser folds it)
        guard in "aaa" + "bbb"           MISSED
        guard in "a %s b" % X            MISSED
        guard in "a {} b".format(X)      MISSED
        guard in "".join([A, B])         MISSED
        guard in str(ALLOWED)            MISSED
        guard in "abc"[0:2]              MISSED

    Every missed row is a substring search that `"" in <container>` satisfies,
    so each is this gate's headline class. Deciding them means deciding the
    TYPE of an arbitrary expression, which is the same name resolution the
    `Name` container is disclosed for above. 0 live sites of any of the six.
  * any read of a guard that is not a COMPARISON. `_membership_violations`
    visits `ast.Compare` nodes and only those, so a method call or a regex
    performs the same substring search unwatched. Measured:

        guard.startswith(NEEDLE)   not refused
        re.search(NEEDLE, guard)   not refused
        guard.find(NEEDLE) >= 0    refused -- it IS a `Compare`

    `assert guard.startswith("${{ inputs.skip_ceiling_interpreter")` is
    tan-cli#1137's defect verbatim with the `!` deleted, which is the string
    `_FABRICATED_NON_COMPARE_SEARCH` uses, and this gate is silent on it.
    0 live sites. Closing it means an allow/deny list of `str` methods and of
    the `re` surface, which is a different rule from "read the whole value"
    and is not attempted here.
  * the OVER-approximating half of `_items_guard_side`. Which element of
    `for <k>, <v> in <d>.items()` carries the guard is DECIDED only when `<d>`
    is a bare `Name` whose EVERY binding in this module is visible and is a
    `Dict` literal or a `DictComp`, with an `if:` read on exactly one of its
    two sides. ONE binding by any other form refuses it: a `for` or
    comprehension target, a `with ... as`, an `AugAssign`, a tuple or
    `Starred` unpack, a function or lambda parameter, an `import ... as`, an
    `except ... as`, a `match` capture, a `global` / `nonlocal`, a `def` /
    `class` / `type` name, a PEP 695 type parameter. So does a `Dict` carrying
    a `**`, a `.items(...)` call that takes arguments, an unpack that is not
    exactly two elements, and a module that binds names dynamically. In every
    one of those BOTH names stay tainted -- including when the guard reached
    the dict through a name rather than through a read of its own
    (`g = step.get("if")` then `{n: g for n in STEPS}`). That is the
    false-positive direction, chosen on purpose; `_items_guard_side` records
    the measured miss that made the alternative unacceptable, and
    `_REBINDING_FORMS` pins one control per form, so a form dropped from the
    walk reds a test instead of quietly re-opening the miss.
  * a name bound to a COMPREHENSION whose guard read sits in the condition
    rather than the element. Measured:
    `off = {job for job, body in _publish_jobs().items() if
    _literal_false(body.get("if"))}` in
    `test_release_docs_match_the_workflow.py` taints `off`, which holds job
    NAMES. That is the over-approximation direction -- a false positive, not a
    miss -- and it is named here because `_tainted_names` promises exactly one
    of the two.

Within a module's local names the walk is a fixpoint (see `_tainted_names`),
so a chain of rebinds does not lose it. What is open is the module boundary,
the attribute/subscript boundary, the `Name` container, the built `str`
container and everything that is not an `ast.Compare` at all; closing any of
them costs more false positives, or more name resolution, or a list of `str`
methods, than tan-cli#1145's rule is worth. Every one of them has a control,
so the answer is pinned and a later narrowing reds a test rather than passing
quietly. #1145's criterion is that whatever these docstrings claim is enforced
is exactly true, and an honest list of what is NOT satisfies it; completeness
is not what it asks for.
"""
import ast
import pathlib
import warnings

GATES_DIR = pathlib.Path(__file__).resolve().parent

#: The whole test tree, not just `gates/`. A non-recursive `test_*.py` glob
#: over `gates/` alone skipped `conftest.py` and the `_*_core.py` helpers in
#: that same directory, and everything outside it -- so a workflow-reading
#: helper one directory up would have been unwatched. Measured: 0 additional
#: `if:`-key reads and 0 additional violations at the widening, so the scan
#: costs nothing today and covers the whole surface tomorrow.
TESTS_DIR = GATES_DIR.parent

#: Every `if:`-key read the scan must still be finding. A floor, not a count:
#: the hazard tan-cli#1145 names for a meta-gate is that its scan silently
#: stops matching, reports zero candidates and goes green. Measured 9 on
#: `dd6ac839`; floored at 6 so ordinary churn does not red it but a scanner
#: that has stopped working does. Pattern borrowed from
#: `test_inert_option_markers.py`'s `assert len(ALL_OPTIONS) >= 400`.
MIN_IF_KEY_READS = 6

#: Comparison operators that READ the whole value. Everything else is refused
#: by `_membership_violations`: the ordering operators, which are meaningless
#: on a guard expression, and `In` / `NotIn` where either operand is a string
#: -- the guard as the CONTAINER (`<needle> in guard`), and the guard as the
#: ELEMENT of a `str` or f-string container, which is the same substring search
#: written round the other way. `Is` / `IsNot` are allowed only when neither
#: operand is a `str` constant; see `_suspect_operands`.
_ALLOWED_OPS = (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)


def _parse(source: str) -> ast.AST:
    """`ast.parse`, without republishing the parsed file's own SyntaxWarnings.

    Widening the scan to the whole test tree brought in modules whose string
    literals the compiler warns about at parse time (tan-cli#1167's invalid
    escape sequence). Those warnings belong to the file that owns them, not to
    this gate's summary line, and pytest's own collection still surfaces them.

    The filter is by category, so it also swallows `'"is" with 'str' literal.
    Did you mean "=="?'` -- a warning about a shape this gate has an opinion
    about. Rather than rent that opinion from the compiler, `_suspect_operands`
    refuses `Is` / `IsNot` against a `str` constant outright, which is an
    assertion rather than a warning and survives any filter.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(source)


def _is_if_key_read(node: ast.AST) -> bool:
    """True for `<expr>["if"]` and `<expr>.get("if", ...)`.

    Keyed on the literal `"if"` rather than on the receiver, because the
    receiver is a workflow dict under half a dozen different local names and
    pinning those would make this gate a list of the call sites it is supposed
    to discover.
    """
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and key.value == "if"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr != "get" or not node.args:
            return False
        first = node.args[0]
        return isinstance(first, ast.Constant) and first.value == "if"
    return False


def _contains_if_key_read(node: ast.AST) -> bool:
    return any(_is_if_key_read(child) for child in ast.walk(node))


def _mentions(node: ast.AST, names: set[str]) -> bool:
    return any(
        isinstance(sub, ast.Name) and sub.id in names for sub in ast.walk(node)
    )


def _touches_guard(node: ast.AST, tainted: set[str]) -> bool:
    return _contains_if_key_read(node) or _mentions(node, tainted)


def _bound_names(target: ast.AST) -> list[str]:
    """The local names a binding target binds.

    A bare `Name`, or a `Tuple`/`List`/`Starred` of them, and nothing else.
    An attribute or subscript target binds no local name at all -- walking it
    for every `Name` would taint the RECEIVER instead, so
    `self.guard = step.get("if")` would taint `self` and any later
    `assert "x" in self.stdout` in the same module would red, and
    `workflow["jobs"][j]["if"] = ...` would taint `workflow`.
    """
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        out: list[str] = []
        for element in target.elts:
            out.extend(_bound_names(element))
        return out
    return []


#: A binding whose VALUE this walk cannot name. Recorded rather than skipped,
#: so a name carrying one can never be mistaken for resolvable. Three
#: unrelated things produce it, and all three must fail closed:
#:
#:   * the form binds an ELEMENT of its source rather than the source itself
#:     -- a `for` or comprehension target, a `with ... as`, a tuple / list /
#:     `Starred` unpack -- so the source expression says nothing about what
#:     the name holds;
#:   * the form has no source expression in this tree at all -- a function or
#:     lambda PARAMETER, an `import ... as`, an `except ... as`, a `match`
#:     capture, a `global` / `nonlocal`, a bare `AnnAssign` annotation, a
#:     `def` / `class` / `type` name, a PEP 695 type parameter;
#:   * the form rebinds through an operator this walk does not model --
#:     `AugAssign`, where `d |= other` yields a value written somewhere else.
_OPAQUE = object()

#: Callables that can bind ANY name in this module from outside the syntax
#: tree. One mention is enough to make every name in the file unresolvable;
#: see `_binds_names_dynamically`.
_DYNAMIC_BINDERS = frozenset({"eval", "exec", "globals", "locals", "vars"})


def _binds_names_dynamically(tree: ast.AST) -> bool:
    """True if this module can bind a name outside the reach of a syntax walk.

    `exec("wrong = {}")`, `globals()["wrong"] = {}` and `from x import *` bind
    names that no enumeration of binding FORMS can see, so in a module using
    any of them no name has a provably complete binding set. `_name_sources`
    returns nothing at all for such a module and every receiver in it fails
    closed -- which is the only honest answer, since the alternative is to
    treat a binding set that is knowably incomplete as complete.

    This is the one entry on the fail-closed list that is not a form: it is
    the residue AFTER the forms, and it is here so that "any binding form this
    walk does not model" is true of the ones nobody can enumerate too.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "*" for alias in node.names
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _DYNAMIC_BINDERS
        ):
            return True
    return False


def _name_sources(tree: ast.AST) -> dict[str, list[object]]:
    """Every module-local name, and EVERY binding of it -- a value or `_OPAQUE`.

    Read only by `_key_value_pairs`, and only to answer one question about a
    `.items()` receiver: is this name's binding set COMPLETE, and is every
    binding in it a dict whose two sides this walk can tell apart?

    The contract is therefore COMPLETENESS, not coverage of the forms someone
    thought to enumerate. A name resolves to an expression under exactly three
    forms -- an `Assign`, an `AnnAssign` carrying a value, and a walrus, each
    to a bare `Name` target -- because those are the three that bind the name
    to the source expression ITSELF. Every other binder in the language
    records `_OPAQUE` against the name instead, which makes the caller fail
    closed; a name this module never binds is absent, which fails closed too;
    and a module that binds names dynamically resolves nothing at all.

    Recording only `Assign` was a MISS, and the round that shipped it was
    strictly worse than the two before it. Measured on
    `wrong = {_step(n).get("if"): n for n in STEPS}`, rebound later by
    `for wrong in _maps_keyed_by_name():` and read by
    `for name, guard in wrong.items(): assert GATE_EXPRESSION in guard`: the
    `for`-target rebind was invisible, the lone `Assign` was treated as the
    whole binding set, the KEY element was kept as the guard side -- and the
    substring test against the actual guard went unreported, where both
    earlier rounds reported it. `_REBINDING_FORMS` carries a control for every
    form below, so dropping one from this walk reds a test.
    """
    if _binds_names_dynamically(tree):
        return {}
    out: dict[str, list[object]] = {}

    def record(name: str, source: object) -> None:
        out.setdefault(name, []).append(source)

    def record_target(target: ast.AST, source: object) -> None:
        """`source` for a bare `Name` target; `_OPAQUE` for every other."""
        if isinstance(target, ast.Name):
            record(target.id, source)
            return
        for name in _bound_names(target):
            record(name, _OPAQUE)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                record_target(target, node.value)
        elif isinstance(node, ast.NamedExpr):
            record_target(node.target, node.value)
        elif isinstance(node, ast.AnnAssign):
            record_target(node.target, node.value or _OPAQUE)
        elif isinstance(node, ast.AugAssign):
            record_target(node.target, _OPAQUE)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            record_target(node.target, _OPAQUE)
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                record_target(node.optional_vars, _OPAQUE)
        elif isinstance(node, ast.arg):
            record(node.arg, _OPAQUE)
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            record(node.name, _OPAQUE)
        elif isinstance(node, ast.TypeAlias):
            record_target(node.name, _OPAQUE)
        elif isinstance(node, (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)):
            record(node.name, _OPAQUE)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                record(alias.asname or alias.name.split(".")[0], _OPAQUE)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                record(name, _OPAQUE)
        elif isinstance(node, ast.ExceptHandler):
            if node.name is not None:
                record(node.name, _OPAQUE)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            if node.name is not None:
                record(node.name, _OPAQUE)
        elif isinstance(node, ast.MatchMapping):
            if node.rest is not None:
                record(node.rest, _OPAQUE)
    return out


def _key_value_pairs(
    receiver: ast.AST, bound: dict[str, list[object]]
) -> list[tuple[ast.AST, ast.AST]] | None:
    """The `key: value` pairs of every dict a `.items()` receiver is bound to.

    `None` means UNDECIDABLE and the caller must fail closed. It is the answer
    whenever the receiver's binding set cannot be PROVEN complete AND
    dict-shaped, which is every case but one:

      * the receiver is not a bare `Name`;
      * nothing in this module binds that name -- it is imported, a built-in,
        or arrives through a star import;
      * the module binds names dynamically, so nothing in it resolves;
      * ANY of its bindings is `_OPAQUE` -- any binding form other than an
        `Assign` / `AnnAssign` / walrus to a bare `Name`: a `for` target, a
        comprehension target, a `with ... as`, an `AugAssign`, a tuple or
        `Starred` unpack, a function or lambda parameter, an `import ... as`,
        an `except ... as`, a `match` capture, a `global` / `nonlocal`, a
        `def` / `class` / `type` name, a PEP 695 type parameter;
      * any of its bindings is an expression that is neither a `Dict` literal
        nor a `DictComp`;
      * a `Dict` binding carries a `**` expansion, whose key is `None` and
        whose pairs are written somewhere else.

    Only when EVERY binding is a visible, `None`-key-free `Dict` literal or a
    `DictComp` are the pairs returned and `_items_guard_side` allowed to
    narrow. Fail-closed on any form this walk does not model -- not on the
    subset of forms someone thought to enumerate, which is the narrowing that
    shipped as a MISS in the round before this one (see `_name_sources`).
    """
    if not isinstance(receiver, ast.Name):
        return None
    sources = bound.get(receiver.id)
    if not sources:
        return None
    pairs: list[tuple[ast.AST, ast.AST]] = []
    for source in sources:
        if isinstance(source, ast.DictComp):
            pairs.append((source.key, source.value))
        elif isinstance(source, ast.Dict) and all(
            key is not None for key in source.keys
        ):
            pairs.extend(zip(source.keys, source.values))
        else:
            return None
    return pairs


def _items_guard_side(
    targets: list[ast.AST], source: ast.AST, bound: dict[str, list[object]]
) -> list[ast.AST] | None:
    """The ONE element of `for <k>, <v> in <d>.items()` that holds the guard.

    `None` means NO narrowing -- the caller keeps the whole tuple and taints
    both names. Both directions of that default are measured, not argued.

    Narrowing at all is needed because tainting the tuple spills the guard
    onto identifiers as generic as `name`: before any narrowing,
    `test_getting_started_sdk_outage_probe.py`'s taint set was
    `['found', 'name', 'wrong']`, so an unrelated `assert "..." in name`
    written later in that 232-line module would have red with a paragraph
    about workflow `if:` keys.

    Narrowing to the VALUE unconditionally, on the reasoning that a mapping
    key is a step name, is what the round before this one did, and it was a
    MISS: measured on `wrong = {_step(n).get("if"): n for n in STEPS}` then
    `for guard, name in wrong.items()`, the taint set was `['name', 'wrong']`
    and the only reported line was the unrelated `<needle> in name`. The
    substring test against the actual guard went unreported.

    So the side is DECIDED -- but only where `_key_value_pairs` can PROVE the
    receiver's binding set complete and dict-shaped: every binding of that
    name, by every binding form in the language, a visible `Dict` literal or
    `DictComp`. Then the guard sits on whichever side reads an `if:` key, and
    when exactly one side does, that side is kept. When the receiver does not
    resolve, when the `.items(...)` call takes arguments, when the unpack is
    not exactly two elements, or when BOTH sides read an `if:` key or NEITHER
    does, both elements stay tainted: over-approximating costs the module-wide
    NAME taint `_tainted_names` measures, but only the MISS costs the defect,
    so the tie goes to tainting both. Failing closed on any form the walk
    cannot name a value for -- rather than on a list of forms someone
    enumerated -- is the round-5 correction: enumerating only `Assign` MISSED
    the guard through a `for`-target rebind of the very same name.
    """
    if not (
        isinstance(source, ast.Call)
        and isinstance(source.func, ast.Attribute)
        and source.func.attr == "items"
        and not source.args
        and not source.keywords
    ):
        return None
    if len(targets) != 1 or not isinstance(targets[0], ast.Tuple):
        return None
    if len(targets[0].elts) != 2:
        return None
    pairs = _key_value_pairs(source.func.value, bound)
    if pairs is None:
        return None
    on_key = any(_contains_if_key_read(key) for key, _ in pairs)
    on_value = any(_contains_if_key_read(value) for _, value in pairs)
    if on_key == on_value:
        return None
    return [targets[0].elts[0 if on_key else 1]]


def _bindings(tree: ast.AST) -> list[tuple[list[str], ast.AST]]:
    """`(bound names, source expression)` for every binding form that can
    carry a guard from one name to another.

    Assignment alone is not enough: the guard reaches a name through a `for`
    target, a comprehension target, a `with ... as`, and a walrus just as
    readily, and the shape measured to slip past an assignment-only walk is a
    dict comprehension followed by `for name, guard in wrong.items()`.
    """
    bound = _name_sources(tree)
    pairs: list[tuple[list[str], ast.AST]] = []
    for node in ast.walk(tree):
        targets: list[ast.AST]
        source: ast.AST | None
        if isinstance(node, ast.Assign):
            targets, source = list(node.targets), node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets, source = [node.target], node.value
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets, source = [node.target], node.iter
        elif isinstance(node, ast.comprehension):
            targets, source = [node.target], node.iter
        elif isinstance(node, ast.withitem):
            if node.optional_vars is None:
                continue
            targets, source = [node.optional_vars], node.context_expr
        elif isinstance(node, ast.NamedExpr):
            targets, source = [node.target], node.value
        else:
            continue
        if source is None:
            continue
        targets = _items_guard_side(targets, source, bound) or targets
        names: list[str] = []
        for target in targets:
            names.extend(_bound_names(target))
        if names:
            pairs.append((names, source))
    return pairs


def _tainted_names(tree: ast.AST) -> set[str]:
    """Local names bound to an expression that reads an `if:` key.

    Flow-insensitive and module-wide on purpose: a name that EVER holds a
    guard expression is treated as holding one everywhere. Missing the
    two-statement shape costs the defect this gate exists for, so the walk
    over-approximates -- but "a false positive a reviewer resolves in one
    line" understates what that costs, and this is what it actually is: the
    taint lands on a NAME, module-wide, and the name can be generic. Measured
    on the shipped tree, the three files with any taint at all are

        gates/test_getting_started_sdk_outage_probe.py: ['found', 'wrong']
        gates/test_interpreter_policy.py:               ['guard']
        gates/test_release_docs_match_the_workflow.py:  ['off']

    `off` holds job NAMES, not guards -- it is bound to a comprehension whose
    only `if:` read sits in the CONDITION -- so any later `<needle> in off` in
    that module reds on a paragraph about workflow guards. That is the shape of
    the cost: not one line of review, but an unrelated author in an unrelated
    module getting a red about `if:` keys. `name` used to be in the first set
    too, which is why `_items_guard_side` exists.

    Iterated to a FIXPOINT, because one rebind is enough to lose the trail: a
    single pass sees `a = step.get("if")` but not the `b = a` behind it, and
    the measured miss was exactly that -- `wrong = {... .get("if") ...}` then
    `for name, guard in wrong.items()`, where `guard` is two hops from the
    read. A name already in the set is itself a guard source for a later
    binding, so the walk repeats until nothing new is added.
    """
    bindings = _bindings(tree)
    names: set[str] = set()
    while True:
        grew = False
        for bound, source in bindings:
            if not _touches_guard(source, names):
                continue
            for name in bound:
                if name not in names:
                    names.add(name)
                    grew = True
        if not grew:
            return names


def _str_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _string_container(node: ast.AST) -> bool:
    """True for a container operand that makes `in` a SUBSTRING search.

    A `str` literal or an f-string, both decidable from the AST with no name
    resolution. `x in "<str>"` is a substring test however whole-valued `x`
    looks, and `"" in "<str>"` is True -- so a guard read from a step that
    carries no `if:` key at all satisfies it.

    Two node types, and no third. Implicit adjacent-literal concatenation
    (`"aaa" "bbb"`) arrives already folded into one `Constant` by the parser,
    so it is covered for free; a container BUILT at runtime is not. Those six
    forms are tabulated in the module docstring's does-NOT-do list and pinned
    by `test_a_built_str_container_is_a_measured_gap`.
    """
    return _str_constant(node) or isinstance(node, ast.JoinedStr)


def _suspect_operands(
    op: ast.cmpop, left: ast.AST, right: ast.AST
) -> tuple[ast.AST, ...] | None:
    """The operands a REFUSED comparison makes suspect, or `None` if allowed.

    * `in` / `not in`: the CONTAINER is always suspect (`<needle> in guard`,
      the tan-cli#1137 shape). The ELEMENT is suspect too when the container is
      a `str` or an f-string, because that is the same substring search with
      the operands swapped. A container that is a literal collection, or a bare
      `Name`, leaves the element alone -- the first is a genuine whole-value
      test, the second is the disclosed blind spot in the module docstring.
    * `is` / `is not` against a `str` constant: an identity test that can be
      False for equal strings.
    * anything else outside `_ALLOWED_OPS`: the ordering operators.
    """
    if isinstance(op, (ast.In, ast.NotIn)):
        return (left, right) if _string_container(right) else (right,)
    if isinstance(op, (ast.Is, ast.IsNot)) and (
        _str_constant(left) or _str_constant(right)
    ):
        return (left, right)
    if isinstance(op, _ALLOWED_OPS):
        return None
    return (left, right)


def _membership_violations(source: str, label: str) -> list[str]:
    """`<label>:<line>` for every REFUSED comparison against an `if:`-key value.

    `_suspect_operands` decides which operands a given operator makes suspect;
    this walk only supplies the `Compare` nodes and the tainted-name set.
    """
    tree = _parse(source)
    tainted = _tainted_names(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for index, op in enumerate(node.ops):
            suspects = _suspect_operands(op, operands[index], operands[index + 1])
            if suspects is None:
                continue
            if any(_touches_guard(operand, tainted) for operand in suspects):
                out.append(f"{label}:{node.lineno}")
                break
    return out


def _if_key_read_count(source: str) -> int:
    return sum(1 for node in ast.walk(_parse(source)) if _is_if_key_read(node))


def _gate_sources() -> list[tuple[str, str]]:
    return [
        (path.relative_to(TESTS_DIR).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(TESTS_DIR.rglob("*.py"))
    ]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def test_no_gate_searches_a_workflow_if_key_for_a_substring():
    violations: list[str] = []
    for label, source in _gate_sources():
        violations.extend(_membership_violations(source, label))
    assert not violations, (
        "A workflow `if:` value is SEARCHED here (`<needle> in guard`) or "
        "compared with an operator that does not read the whole value, which "
        "leaves every character that matters free -- polarity above all. "
        "tan-cli#1137 shipped `RELEASE_OPT_OUT_INPUT in guard`, and deleting "
        "the `!` from `${{ !inputs.skip_ceiling_interpreter }}` still "
        "passed.\n"
        "Compare the whole expression with `==` / `!=` / `is` / `is not` "
        "against a named constant instead, the way "
        "test_getting_started_sdk_outage_probe.py and "
        "test_interpreter_policy.py already do. (Membership with the guard on "
        "the ELEMENT side of a LITERAL COLLECTION -- "
        "`guard in {ALLOWED_A, ALLOWED_B}` -- is a whole-value test and is "
        "not reported here. Membership in a `str` or f-string container IS "
        "reported however it is written round: `guard in \"a != b\"` is a "
        "substring search, and `\"\" in \"a != b\"` is True, so it passes on a "
        "step with no `if:` key at all.):\n  "
        + "\n  ".join(violations)
    )


def test_the_scan_still_finds_the_if_key_reads_it_is_scanning():
    """The candidate floor. A meta-gate's own failure mode is a scan that
    stopped matching: it then reports zero violations and goes green forever."""
    total = sum(_if_key_read_count(source) for _, source in _gate_sources())
    assert total >= MIN_IF_KEY_READS, (
        f"the `if:`-key scan found {total} reads across python/tests/, "
        f"below the floor of {MIN_IF_KEY_READS}. Either the workflow-reading "
        f"gates were deleted, or -- far likelier -- `_is_if_key_read` stopped "
        f"matching the shape they use and this gate is now checking nothing."
    )


def test_the_scan_reaches_past_the_gates_test_files():
    """The widening has no violation to catch today, so nothing else reds if
    it is narrowed back. This is what makes it a checked property: the scan
    must reach the non-`test_*.py` modules in `gates/` and at least one file
    outside `gates/` entirely, or a workflow-reading helper there would be
    unwatched."""
    scanned = {label for label, _ in _gate_sources()}
    for expected in (
        "gates/conftest.py",
        "gates/_module_size_budget_core.py",
        "gates/_planner_oracle_ref_matching_core.py",
        "conftest.py",
    ):
        assert expected in scanned, (
            f"{expected} is outside the `if:`-key scan. A non-recursive "
            f"`gates/test_*.py` glob skipped every one of these."
        )


# ---------------------------------------------------------------------------
# Negative controls -- fabricated input, so the detector is proven to fire
# ---------------------------------------------------------------------------
_FABRICATED_DIRECT = '''
def test_x():
    assert NEEDLE in step.get("if")
'''

_FABRICATED_ASSIGNED = '''
def test_x():
    guard = str(ci["jobs"][JOB].get("if", "")).strip()
    assert NEEDLE in guard
'''

_FABRICATED_SUBSCRIPT = '''
def test_x():
    assert "!" not in job["if"]
'''

_FABRICATED_CONFORMING = '''
def test_x():
    guard = ci["jobs"][JOB].get("if", "")
    assert guard == RELEASE_GUARD_EXPRESSION
    assert step.get("if") is None
    assert "needle" in some_unrelated_run_output
'''

#: The shape that slipped past an assignment-only walk. Two hops from the read
#: -- a dict comprehension, then a `.items()` unpack -- and it is #1137's
#: defect with its polarity inverted, reintroduced in the file this gate's
#: docstring cites as conforming. Measured on the pre-fix walk: reported
#: clean, and the read floor did not save it (reads went 9 -> 8, still >= 6).
_FABRICATED_ITEMS_UNPACK = '''
def test_x():
    wrong = {name: _step(name).get("if") for name in SDK_DEPENDENT_STEPS}
    for name, guard in wrong.items():
        assert GATE_EXPRESSION in guard
'''

#: One rebind is all it took: an assignment-only walk taints `a` and never
#: reaches the `b` behind it, because `b = a.strip()` reads no `if:` key of its
#: own -- it only mentions a name that already holds one.
_FABRICATED_REBIND = '''
def test_x():
    a = step.get("if")
    b = a.strip()
    assert NEEDLE in b
'''

#: `_ALLOWED_OPS` refuses the ordering operators too -- they are meaningless on
#: a guard expression, and a constant that stopped being defined would still
#: satisfy one.
_FABRICATED_ORDERING = '''
def test_x():
    assert step.get("if") < "z"
'''

#: Whole-value membership: the guard is the ELEMENT and the container is a
#: LITERAL COLLECTION. Each of these moves the entire expression with any
#: rewrite, which is exactly what this gate asks for, so refusing them would
#: tell the author to do the thing they already did.
_FABRICATED_WHOLE_VALUE = '''
def test_x():
    assert step.get("if") in {EXPR_A, EXPR_B}
    guard = step.get("if")
    assert guard in (EXPR_A, EXPR_B)
    assert guard in [EXPR_A, EXPR_B]
    assert guard in frozenset((EXPR_A, EXPR_B))
'''

#: NOT a blessing -- the disclosed BLIND SPOT, kept as a control so the gap is
#: measured rather than assumed, and so a future narrowing of it reds here
#: instead of arguing with a green test. Whether `ALLOWED_GUARDS` holds a
#: collection (a whole-value test) or a `str` (a substring search that the
#: empty string satisfies) needs name resolution this per-file walk does not
#: do. Measured across the files this scan reads: 0 live sites of this shape,
#: so allowing it costs nothing today.
_FABRICATED_NAME_CONTAINER = '''
def test_x():
    guard = step.get("if")
    assert guard in ALLOWED_GUARDS
'''

#: The guard is on the ELEMENT side, but the CONTAINER is a `str` literal, so
#: `in` is a substring search with the operands swapped. Measured against
#: `ALLOWED_GUARDS = "steps.sdk_list.outputs.outage != 'true'"`, Python answers
#: True to the whole expression, to `'steps.sdk_list.outputs.outage'`, to
#: `"outage != 'true'"` AND to `''` -- the last meaning a step carrying no
#: `if:` key at all passes, which is an assertion that cannot fail.
_FABRICATED_STR_CONSTANT_CONTAINER = """
def test_x():
    assert step.get("if") in "steps.sdk_list.outputs.outage != 'true'"
"""

#: The same defect assembled at runtime. An f-string is a `JoinedStr`, equally
#: decidable from the AST with no name resolution.
_FABRICATED_FSTRING_CONTAINER = '''
def test_x():
    guard = step.get("if")
    assert guard in f"{EXPR_A} or {EXPR_B}"
'''

#: `is` against a `str` literal is an IDENTITY test that can be False for two
#: equal strings. CPython warns about it at compile time -- `"is" with 'str'
#: literal. Did you mean "=="?` -- and that warning is exactly what `_parse`
#: swallows, so the gate holds the opinion itself instead of renting it.
_FABRICATED_IS_STR_LITERAL = """
def test_x():
    assert step.get("if") is "${{ github.event_name == 'push' }}"
"""

#: Orientation A, `{name: guard}`: the KEY is a step name. Before any
#: narrowing the whole tuple was tainted and this reported `fab:4` -- an
#: unrelated substring assertion on `name`, red with a paragraph about
#: workflow `if:` keys. The guard half must stay tainted, which the
#: `== GATE_EXPRESSION` line does not prove; `_FABRICATED_ITEMS_UNPACK` does.
_FABRICATED_ITEMS_KEY = '''
def test_x():
    wrong = {name: _step(name).get("if") for name in STEPS}
    for name, guard in wrong.items():
        assert "tan-cli#840" in name
        assert guard == GATE_EXPRESSION
'''

#: Orientation B, `{guard: name}` -- the same dict keyed the other way round,
#: and the shape a narrowing to the VALUE element gets exactly backwards.
#: Measured on that narrowing: taint `['name', 'wrong']`, and the ONLY line
#: reported was `fab:6`, the unrelated assertion on `name`. The substring test
#: against the actual guard on `fab:5` -- tan-cli#1137's defect verbatim --
#: went unreported, which is strictly worse than tainting both.
_FABRICATED_ITEMS_KEY_IS_THE_GUARD = '''
def test_x():
    wrong = {_step(n).get("if"): n for n in SDK_DEPENDENT_STEPS}
    for guard, name in wrong.items():
        assert GATE_EXPRESSION in guard
        assert "tan-cli#840" in name
'''

#: The FAIL-CLOSED case. `wrong` is tainted -- the call that builds it reads an
#: `if:` key -- but a call is not a `Dict` literal or a `DictComp`, so which
#: side of the pair carries the guard is undecidable and BOTH names stay
#: tainted. Two reported lines, one of which is a false positive on `name`.
#: That is the deliberate direction -- it is the same over-approximation cost
#: `_tainted_names` records, and the miss `_FABRICATED_ITEMS_KEY_IS_THE_GUARD`
#: measures is the one that costs the defect.
_FABRICATED_ITEMS_UNRESOLVED_RECEIVER = '''
def test_x():
    wrong = _guards_by_name(_step(NAME).get("if"))
    for name, guard in wrong.items():
        assert GATE_EXPRESSION in guard
        assert "tan-cli#840" in name
'''

#: NOT a blessing -- the six built `str` containers the module docstring
#: enumerates, kept as a control so the gap stays measured. Each is a
#: substring search that `"" in <container>` satisfies; none is decidable
#: without deciding the TYPE of an arbitrary expression.
_FABRICATED_BUILT_STR_CONTAINER = '''
def test_x():
    guard = step.get("if")
    assert guard in "aaa" + "bbb"
    assert guard in "a %s b" % X
    assert guard in "a {} b".format(X)
    assert guard in "".join([A, B])
    assert guard in str(ALLOWED)
    assert guard in "abc"[0:2]
'''

#: NOT a blessing either -- the walk visits `ast.Compare` nodes and only
#: those. The first two lines are substring searches on the guard that no
#: `Compare` node covers; the third does the identical search and IS reported,
#: purely because `>= 0` makes it a comparison. `startswith` here is
#: tan-cli#1137's defect verbatim with the `!` deleted.
_FABRICATED_NON_COMPARE_SEARCH = '''
def test_x():
    guard = step.get("if")
    assert guard.startswith("${{ inputs.skip_ceiling_interpreter")
    assert re.search(NEEDLE, guard)
    assert guard.find(NEEDLE) >= 0
'''

#: The binding whose SOURCE is tainted only later in the walk. `ast.walk` is
#: breadth-first, so `guard = raw` (function-body depth) is visited BEFORE the
#: `raw = step.get("if")` nested inside the loop above it -- which is why the
#: walk has to iterate rather than sweep once. Measured: a single sweep taints
#: `raw` alone and reports this clean.
_FABRICATED_NESTED_PRODUCER = '''
def test_x():
    for step in _steps():
        raw = step.get("if")
    guard = raw
    assert NEEDLE in guard
'''

#: An attribute or subscript target binds no local NAME. Walking it for every
#: `Name` tainted the RECEIVER instead, so this module's unrelated
#: `"tan build" in self.stdout` red on a guard it never touched.
_FABRICATED_RECEIVER_COLLATERAL = '''
def test_x(self):
    self.guard = step.get("if")
    workflow["jobs"][JOB]["if"] = GATE_EXPRESSION
    assert "tan build" in self.stdout
    assert "jobs" in workflow
'''


def test_the_detector_fires_on_a_direct_membership_test():
    assert _membership_violations(_FABRICATED_DIRECT, "fab") == ["fab:3"]


def test_the_detector_fires_on_the_two_statement_shape_that_shipped():
    """The tan-cli#1137 shape exactly: read into a local, then search it.

    This is the control that matters. A detector looking only at `Compare`
    operands passes `_FABRICATED_DIRECT` and misses this one -- and this one
    is the instance that actually shipped.
    """
    assert _membership_violations(_FABRICATED_ASSIGNED, "fab") == ["fab:4"]


def test_the_detector_fires_on_subscript_reads_and_on_not_in():
    assert _membership_violations(_FABRICATED_SUBSCRIPT, "fab") == ["fab:3"]


def test_the_detector_is_silent_on_conforming_code():
    """Including a membership test on something that is NOT an `if:` value --
    the false-positive direction, which is what would make this gate noise."""
    assert _membership_violations(_FABRICATED_CONFORMING, "fab") == []


def test_the_detector_fires_through_a_dict_items_unpack():
    """Two hops from the read, and the pre-fix walk reported it clean.

    Needs BOTH halves of the rewrite: a `for` target has to be a binding form
    at all, and an already-tainted name (`wrong`) has to count as a guard
    SOURCE for the binding behind it.
    """
    assert _membership_violations(_FABRICATED_ITEMS_UNPACK, "fab") == ["fab:5"]


def test_the_walk_iterates_rather_than_sweeping_once():
    """The control for the loop itself, not just for the tainted-name source.

    `ast.walk` is breadth-first, so a binding can be visited before the
    binding that taints its source. Measured: one sweep over the same
    bindings taints `raw` and not `guard`, and reports this clean.
    """
    assert _membership_violations(_FABRICATED_NESTED_PRODUCER, "fab") == ["fab:6"]


def test_the_detector_fires_through_a_rebind():
    assert _membership_violations(_FABRICATED_REBIND, "fab") == ["fab:5"]


def test_the_detector_fires_on_an_ordering_operator():
    """`_ALLOWED_OPS` is enforced, not decorative -- the constant named the
    ordering operators as refused while `_membership_violations` hardcoded
    `In`/`NotIn`, so `step.get("if") < "z"` went through (tan-cli#1145's own
    criterion: whatever the docstrings claim is enforced is exactly true)."""
    assert _membership_violations(_FABRICATED_ORDERING, "fab") == ["fab:3"]


def test_the_detector_is_silent_on_whole_value_membership():
    """The guard as the ELEMENT of a set/tuple/list/frozenset LITERAL is a
    whole-value test, which is what this gate asks for."""
    assert _membership_violations(_FABRICATED_WHOLE_VALUE, "fab") == []


def test_the_name_container_blind_spot_is_measured_not_assumed():
    """`guard in ALLOWED_GUARDS` is ALLOWED, and that is a gap, not a
    blessing. If `ALLOWED_GUARDS` is a `str` this is a substring search that
    even an absent `if:` key satisfies; the walk cannot tell. The module
    docstring enumerates it, and this control pins the current answer so
    closing the gap reds here rather than passing silently."""
    assert _membership_violations(_FABRICATED_NAME_CONTAINER, "fab") == []


def test_the_detector_fires_on_a_str_literal_container():
    """The element side is suspect too once the container is a string.

    `"" in "<any str>"` is True, so this shape passes on a step carrying no
    `if:` key at all -- tan-cli#1145's own headline class, an assertion on a
    workflow guard that cannot fail.
    """
    assert _membership_violations(_FABRICATED_STR_CONSTANT_CONTAINER, "fab") == [
        "fab:3"
    ]


def test_the_detector_fires_on_an_fstring_container():
    """Same search, assembled at runtime. A `JoinedStr` container is as
    decidable from the AST as a `Constant` one."""
    assert _membership_violations(_FABRICATED_FSTRING_CONTAINER, "fab") == ["fab:4"]


def test_the_detector_fires_on_an_identity_test_against_a_str_literal():
    """`guard is "<str>"` can be False for equal strings, and the compile-time
    warning that says so is the one `_parse` filters out -- so the refusal
    lives here, as an assertion, not as a warning that a filter can drop."""
    assert _membership_violations(_FABRICATED_IS_STR_LITERAL, "fab") == ["fab:3"]


def test_the_key_of_an_items_unpack_is_not_tainted():
    """Orientation A. Taint must not escape onto `name`. Before any narrowing
    this reported `fab:4`."""
    assert _membership_violations(_FABRICATED_ITEMS_KEY, "fab") == []


def test_the_guard_is_found_when_the_items_key_is_the_one_holding_it():
    """Orientation B, and the reason the side is decided rather than assumed.

    `fab:5` is the substring test against the guard; `fab:6` is unrelated and
    must stay silent. A narrowing to the VALUE element reported `['fab:6']`
    only -- it named the unrelated line and missed the defect, which is worse
    than tainting both. `_items_guard_side` reads the `DictComp` and keeps the
    KEY here because that is the side with the `if:` read.
    """
    assert _membership_violations(
        _FABRICATED_ITEMS_KEY_IS_THE_GUARD, "fab"
    ) == ["fab:5"]


def test_an_unresolvable_items_receiver_taints_both_elements():
    """The fail-closed default: a receiver that is not a `Dict` literal or a
    `DictComp` in this module makes the pair undecidable, so both names stay
    tainted and `fab:6` is a disclosed false positive."""
    assert _membership_violations(
        _FABRICATED_ITEMS_UNRESOLVED_RECEIVER, "fab"
    ) == ["fab:5", "fab:6"]


def test_a_built_str_container_is_a_measured_gap():
    """Six substring searches, all silent. `_string_container` decides an
    `ast.Constant` str and an `ast.JoinedStr`; a container assembled by `+`,
    `%`, `.format()`, `join`, `str()` or a slice needs the TYPE of an
    arbitrary expression. Enumerated in the module docstring and pinned here,
    so closing the gap reds this rather than passing quietly."""
    assert _membership_violations(_FABRICATED_BUILT_STR_CONTAINER, "fab") == []


def test_the_rule_stops_at_comparison_nodes():
    """The walk visits `ast.Compare` and nothing else, which no other
    does-NOT-do entry covers -- every one of those is a DATAFLOW limit.

    `fab:4` is tan-cli#1137's defect with the `!` deleted and is silent;
    `fab:5` is the same search through `re`; `fab:6` does the identical thing
    and IS reported, only because `>= 0` makes it a comparison."""
    assert _membership_violations(_FABRICATED_NON_COMPARE_SEARCH, "fab") == [
        "fab:6"
    ]


def test_the_detector_does_not_taint_an_attribute_or_subscript_receiver():
    """`self.guard = step.get("if")` must not taint `self`."""
    assert _membership_violations(_FABRICATED_RECEIVER_COLLATERAL, "fab") == []


# ---------------------------------------------------------------------------
# The fail-closed controls -- one per binding form `_name_sources` does NOT
# resolve to a value, plus one per predicate `_key_value_pairs` and
# `_items_guard_side` rely on. Round 4 shipped four of the predicates below
# with NO control at all: each of the four mutations named in
# `test_every_resolution_predicate_has_a_control`'s docstring measured a fully
# green `22 passed`.
# ---------------------------------------------------------------------------
#: `wrong` bound ONCE, by a `DictComp` whose VALUE side reads the `if:` key.
#: The side is decidable, so only the guard line is reported and the unrelated
#: assertion on `name` stays silent. Every fail-closed control below is this
#: same source with ONE rebinding form spliced in, so the difference between
#: `['fab:5']` here and two reported lines there is the fail-closed branch and
#: nothing else.
_RESOLVED_ITEMS_RECEIVER = '''
def test_x():
    wrong = {n: _step(n).get("if") for n in STEPS}
    for name, guard in wrong.items():
        assert GATE_EXPRESSION in guard
        assert "tan-cli#840" in name
'''

#: EVERY binding form in the language that binds a name to something other
#: than a source expression this walk can name -- the completeness obligation
#: `_name_sources` carries, written out rather than left to whoever next
#: enumerates a subset. Each value is spliced into `_RESOLVED_ITEMS_RECEIVER`
#: between the `DictComp` and the `.items()` unpack, so `wrong` is bound twice:
#: once decidably, once by the form under test. The answer must be the
#: FAIL-CLOSED one -- BOTH names tainted, BOTH assertion lines reported --
#: because a binding set with one unresolvable member is not a binding set.
#:
#: `del wrong` is absent on purpose: it UNBINDS a name rather than binding it,
#: so it cannot make a resolved receiver hold something else.
_REBINDING_FORMS = {
    "for target": "for wrong in _maps():\n    pass",
    "async for target": (
        "async def _inner():\n    async for wrong in _amaps():\n        pass"
    ),
    "comprehension target": "_ = [wrong for wrong in _maps()]",
    "with ... as": "with _open() as wrong:\n    pass",
    "async with ... as": (
        "async def _inner():\n    async with _open() as wrong:\n        pass"
    ),
    "augmented assignment": "wrong |= _more()",
    "annotation with no value": "wrong: dict",
    "tuple unpack from a call": "wrong, other = _pair()",
    "starred unpack": "wrong, *rest = _pair()",
    "function parameter": "def _inner(wrong):\n    return wrong",
    "keyword-only parameter": "def _inner(*, wrong=None):\n    return wrong",
    "lambda parameter": "_ = lambda wrong: wrong",
    "import ... as": "import json as wrong",
    "from ... import ... as": "from json import loads as wrong",
    "except ... as": "try:\n    pass\nexcept Exception as wrong:\n    pass",
    "match capture": "match _thing():\n    case wrong:\n        pass",
    "match mapping rest": (
        "match _thing():\n    case {'k': _, **wrong}:\n        pass"
    ),
    "global declaration": "global wrong",
    "nonlocal declaration": "nonlocal wrong",
    "def name": "def wrong():\n    return None",
    "class name": "class wrong:\n    pass",
    "type alias": "type wrong = dict",
    "PEP 695 type parameter": "def _inner[wrong](x):\n    return x",
    "exec": 'exec("wrong = {}")',
    "globals() subscript": 'globals()["wrong"] = {}',
    "eval": '_ = eval("wrong")',
    "locals()": "_ = locals()",
    "vars()": "_ = vars()",
}


def _with_rebinding(rebind: str) -> tuple[str, list[str]]:
    """`_RESOLVED_ITEMS_RECEIVER` with `rebind` spliced in, and its answer.

    Returns the fabricated source and the two `fab:<line>` labels that a
    fail-closed walk must report: the substring test against the guard, and
    the unrelated assertion on `name` that a DECIDED side would have kept
    silent. Reporting both is the point -- the false positive on `name` is the
    disclosed price of never missing the guard.
    """
    body = "\n".join(f"    {line}" if line else "" for line in rebind.splitlines())
    source = _RESOLVED_ITEMS_RECEIVER.replace(
        "    for name, guard in wrong.items():",
        f"{body}\n    for name, guard in wrong.items():",
        1,
    )
    offset = len(rebind.splitlines())
    return source, [f"fab:{5 + offset}", f"fab:{6 + offset}"]


#: A star import cannot sit inside a function, so it is the one dynamic binder
#: that needs its own module. `from x import *` can bind ANY name, `wrong`
#: included, from a file this per-file walk never opens.
_FABRICATED_STAR_IMPORT = '''
from _elsewhere import *


def test_x():
    wrong = {n: _step(n).get("if") for n in STEPS}
    for name, guard in wrong.items():
        assert GATE_EXPRESSION in guard
        assert "tan-cli#840" in name
'''

#: The `Dict` LITERAL resolution path, which round 4 documented in two
#: docstrings and exercised in no control: both `.items()` controls it shipped
#: build their receiver from a `DictComp`, so deleting the `ast.Dict` branch of
#: `_key_value_pairs` measured a fully green 22.
_FABRICATED_DICT_LITERAL_RECEIVER = '''
def test_x():
    wrong = {"install": _step("install").get("if"), "build": _step("b").get("if")}
    for name, guard in wrong.items():
        assert GATE_EXPRESSION in guard
        assert "tan-cli#840" in name
'''

#: A `**` expansion gives the `Dict` a `None` key, and the pairs it
#: contributes are written somewhere else entirely -- so the dict is NOT
#: complete and the side is undecidable. Round 4 guarded this and had no
#: control for it: dropping the `None`-key guard measured a green 22, because
#: nothing in the file ever built a `Dict` with a `**` in it.
_FABRICATED_DICT_STAR_EXPANSION = '''
def test_x():
    wrong = {"install": _step("install").get("if"), **_more_guards()}
    for name, guard in wrong.items():
        assert GATE_EXPRESSION in guard
        assert "tan-cli#840" in name
'''

#: `dict.items()` takes no arguments, so a `.items(...)` that does is not the
#: method this narrowing models and the receiver's shape says nothing about
#: the unpack. Round 4 refused both spellings and had a control for neither.
_FABRICATED_ITEMS_WITH_ARGUMENTS = '''
def test_x():
    wrong = {n: _step(n).get("if") for n in STEPS}
    for name, guard in wrong.items(SENTINEL):
        assert GATE_EXPRESSION in guard
        assert "tan-cli#840" in name
    for name, guard in wrong.items(strict=True):
        assert GATE_EXPRESSION in guard
        assert "tan-cli#840" in name
'''

#: `dict.items()` yields 2-tuples, so a 3-element unpack is not unpacking the
#: pair this narrowing resolved and neither element can be ruled out. Round 4
#: refused it and had no control: dropping the length check measured a green
#: 22, and kept element `[1]` out of a tuple whose shape it had not checked.
_FABRICATED_THREE_ELEMENT_UNPACK = '''
def test_x():
    wrong = {n: _step(n).get("if") for n in STEPS}
    for name, guard, extra in wrong.items():
        assert GATE_EXPRESSION in guard
        assert "tan-cli#840" in name
        assert "tan-cli#840" in extra
'''

#: The BLOCKER round 4 shipped, in both orientations. `wrong` is bound once by
#: an `Assign` to a decidable `DictComp` and once by a `for` target that
#: `_assigned_sources` did not read -- so the `Assign` alone was treated as the
#: complete binding set, a side was picked from a dict that is not the one at
#: the `.items()` call, and the guard went untainted. Measured on the two
#: earlier revisions of this file and on round 4's:
#:
#:     8344c8f (round 2)  A ['fab:9']  B ['fab:9']
#:     6cd93a7 (round 3)  A ['fab:9']  B []
#:     58eee85 (round 4)  A []         B []          <- worse than both
#:
#: It reproduces on the live tree too: appending
#: `for wrong in _guard_maps_by_guard(): / for guard, name in wrong.items(): /
#: assert GATE_EXPRESSION in guard` to
#: `test_getting_started_sdk_outage_probe.py` measured `[]` on round 4, with
#: `guard` absent from a taint set of `['found', 'name', 'wrong']`.
_FABRICATED_REBOUND_KEYED_BY_GUARD = '''
def test_x():
    wrong = {_step(n).get("if"): n for n in STEPS}
    assert wrong


    for wrong in _maps_keyed_by_name():
        for name, guard in wrong.items():
            assert GATE_EXPRESSION in guard
'''

_FABRICATED_REBOUND_KEYED_BY_NAME = '''
def test_x():
    wrong = {n: _step(n).get("if") for n in STEPS}
    assert wrong


    for wrong in _maps_keyed_by_guard():
        for guard, name in wrong.items():
            assert GATE_EXPRESSION in guard
'''


def test_a_resolved_items_receiver_still_decides_the_guard_side():
    """The baseline the fail-closed controls below are measured against.

    One binding, a `DictComp`, `if:` read on the VALUE side: the side is
    decided, `guard` is tainted and `name` is not, so only `fab:5` is
    reported. Every row of `_REBINDING_FORMS` is this same source with one
    extra binding of `wrong` spliced in, and every one of them must report
    BOTH lines instead."""
    assert _membership_violations(_RESOLVED_ITEMS_RECEIVER, "fab") == ["fab:5"]


def test_every_binding_form_that_is_not_resolved_fails_closed():
    """The round-5 rule: fail closed on ANY binding form the walk does not
    model, not on the ones someone thought to enumerate.

    Round 4's `_assigned_sources` read `ast.Assign` and nothing else while
    `_bindings` enumerated seven forms, so a `for`-target rebind, a walrus, a
    `with ... as`, an `AnnAssign`, an `AugAssign` or a plain function
    parameter shadowing the name was invisible -- and `_key_value_pairs`
    treated a non-empty `Assign` list as a COMPLETE binding set. It then
    picked a side out of a dict that is not the one at the `.items()` call.

    Twenty-eight forms, every one of them a second binding of an otherwise
    decidable receiver, and every one must report both lines."""
    for label, rebind in _REBINDING_FORMS.items():
        source, expected = _with_rebinding(rebind)
        assert _membership_violations(source, "fab") == expected, label


def test_a_star_import_makes_every_name_in_the_module_unresolvable():
    """`from x import *` binds names from a file this walk never opens, so no
    name in the module has a provably complete binding set. It cannot live
    inside a function, which is why it is not a `_REBINDING_FORMS` row."""
    assert _membership_violations(_FABRICATED_STAR_IMPORT, "fab") == [
        "fab:8",
        "fab:9",
    ]


def test_the_blocker_is_caught_in_both_orientations():
    """A decidable `Assign`, then a `for`-target rebind of the same name.

    Round 4 reported `[]` on both -- strictly worse than round 2 (`['fab:9']`
    on both) and round 3 (`['fab:9']` on the first). The guard must be
    reported however the `Assign`-bound dict happens to be keyed, because the
    dict at the `.items()` call is not that dict at all."""
    assert _membership_violations(
        _FABRICATED_REBOUND_KEYED_BY_GUARD, "fab"
    ) == ["fab:9"]
    assert _membership_violations(
        _FABRICATED_REBOUND_KEYED_BY_NAME, "fab"
    ) == ["fab:9"]


def test_a_dict_literal_receiver_resolves_like_a_dict_comprehension():
    """`_key_value_pairs`' `ast.Dict` branch, which had no control at all.

    Both `.items()` controls round 4 shipped build the receiver from a
    `DictComp`, so deleting the `Dict` branch outright measured `22 passed`
    while two docstrings went on documenting it as a supported resolution
    path. Deleted, this reports `['fab:4', 'fab:5']` instead."""
    assert _membership_violations(
        _FABRICATED_DICT_LITERAL_RECEIVER, "fab"
    ) == ["fab:5"]


def test_a_dict_with_a_star_expansion_is_undecidable():
    """The `None`-key guard, which had no control either.

    `{**other}` gives `ast.Dict` a `None` key and the pairs come from
    somewhere this walk cannot read, so the dict is incomplete and both names
    stay tainted."""
    assert _membership_violations(
        _FABRICATED_DICT_STAR_EXPANSION, "fab"
    ) == ["fab:5", "fab:6"]


def test_an_items_call_that_takes_arguments_is_not_the_items_being_modelled():
    """`not source.args and not source.keywords`, which had no control.

    `dict.items()` takes none of either, so a `.items(...)` that does is some
    other method on some other object and the receiver's dict shape proves
    nothing about the unpack."""
    assert _membership_violations(
        _FABRICATED_ITEMS_WITH_ARGUMENTS, "fab"
    ) == ["fab:5", "fab:6", "fab:8", "fab:9"]


def test_an_unpack_that_is_not_a_pair_is_undecidable():
    """`len(targets[0].elts) != 2`, which had no control.

    `dict.items()` yields 2-tuples. A 3-element unpack is not unpacking the
    pair that was resolved, so no element can be ruled out; dropping the check
    kept element `[1]` out of a tuple whose shape had never been checked."""
    assert _membership_violations(
        _FABRICATED_THREE_ELEMENT_UNPACK, "fab"
    ) == ["fab:5", "fab:6", "fab:7"]


#: The one place a non-`Name` binding target differs from an `_OPAQUE` one.
#: Unpacking a dict yields its KEYS, so `wrong` here holds a guard STRING, not
#: the mapping -- but the source expression IS a `DictComp`, so a
#: `record_target` that handed every unpacked name the source it was unpacked
#: FROM would resolve `wrong` to that comprehension, keep the KEY side, leave
#: `guard` untainted and miss `fab:5`. Every other unpack fails closed by
#: accident (a `Call`, a `Tuple` and a `List` are none of them dicts); this one
#: fails closed only because the walk refuses to name a value for a target it
#: did not bind whole.
_FABRICATED_UNPACK_FROM_A_DICT = '''
def test_x():
    wrong, other = {_step(n).get("if"): n for n in STEPS}
    for name, guard in wrong.items():
        assert GATE_EXPRESSION in guard
        assert "tan-cli#840" in name
'''


def test_unpacking_a_dict_does_not_resolve_the_names_to_that_dict():
    """A tuple target binds an ELEMENT of its source, never the source.

    Measured: handing the unpacked names the `DictComp` they came out of
    reports `['fab:5', 'fab:6']` here today and `['fab:6']` with that change
    -- the unrelated line kept, the guard lost."""
    assert _membership_violations(
        _FABRICATED_UNPACK_FROM_A_DICT, "fab"
    ) == ["fab:5", "fab:6"]
