# SPDX-License-Identifier: Apache-2.0
"""Gate: every subprocess spawn under `python/tan/` builds its child's `env`
through `tan.core.subprocess_env.spawn_env` (or one of the two verified
wrappers below) -- never bare, never `None`. tan-cli#992.

## Why this exists

PyInstaller's Linux ONEDIR freeze points `LD_LIBRARY_PATH` at `tan`'s own
bundled `_internal/` lib dir; every child this process spawns inherits that
unless the spawn site restores the caller's original (`spawn_env`'s own
module docstring has the mechanism and the measured CI failure this closes).
26 files under `python/tan/` spawn a subprocess. One (`bootstrap_cmd.py`)
worked the rule out correctly in isolation (tan-cli#990); the other 25 did
not, until this same PR lifted the rule into `tan.core.subprocess_env` and
routed every site through it. This gate is what keeps a 27th site -- or a
regression in one of these 26 -- from reopening the leak silently: a spawn
call with no `env=` at all inherits this process's own (possibly bundle-
poisoned) environment exactly as thoroughly as one that builds a fresh,
unrestored dict by hand.

## What this checks, precisely

For every `subprocess.run` / `.Popen` / `.check_output` / `.call` call site
found under `python/tan/` (module aliasing and `from subprocess import ...`
bare names are both resolved -- see `_SubprocessBinding` below):

1. An `env=` keyword must be present at all. Its ABSENCE is a violation --
   the default is "inherit `os.environ` verbatim", the exact leak.
2. `env=None` literal is a violation for the same reason.
3. The keyword's value expression, walked as a tree, must contain at least
   one `Call` whose resolved name is [`_TRUSTED_CALL_NAMES`] -- `spawn_env`
   itself, or one of the two hand-verified wrapper functions
   (`_child_env`/`_resolution_env` in `flash_cmd.py`, `Runner._env` in
   `bootstrap_cmd.py`) whose OWN bodies this gate independently confirms
   still REACH `spawn_env`/`restore_ld_library_path` **on every path that
   produces the wrapper's return value** (see
   `test_the_verified_wrappers_still_call_the_primitive` below, tightened by
   tan-cli#999 and #1004) -- so an edit that hollows out a wrapper
   (tan-cli#992's own "helper wrapping a helper" concern) reds here even
   though no subprocess call site changed at all, and so does an edit that
   merely leaves the call PRESENT somewhere in the wrapper's body without it
   ever feeding the returned value (tan-cli#999's own "dead-code call
   satisfies the gate" defect -- a `Call` node existing ANYWHERE in the
   function's AST used to be enough; it no longer is, for EITHER shape this
   gate trusts -- #999 closed it for the direct-return shape, #1004 closed
   the identical gap left open for the in-place-mutation shape).

## How rule 3's wrapper-body check works (tan-cli#999)

`_call_reaches_every_return` walks a wrapper's own statements (not descending
into any `def`/`lambda` nested inside it -- a nested scope's own reachability
is a separate question, see "cross-function flow" below) and asks, for EVERY
`return` that isn't a bare `return None`: does `target_name(...)`'s result
plausibly flow into the value that `return` yields? A value flows if it is:

* the direct result of a `target_name(...)` call in the return's own value
  expression (`return spawn_env()`, `return _child_env(venv_bin)`);
* a local variable assigned from a flowing expression, later returned bare
  (`env = spawn_env(); return env` -- [`_child_env`]'s first branch);
* passed as an argument into ANOTHER call that IS (or is folded into) the
  return value (`return prepend_path(env, venv_bin)` -- [`_child_env`]'s
  second branch: the call's own return is trusted because a trusted argument
  was folded into it, not because `prepend_path` itself is on the trusted
  list);
* a local variable passed BY NAME into a bare `target_name(some_var)`
  statement -- the in-place-mutation shape [`restore_ld_library_path`] uses
  (it mutates its dict argument rather than returning a new one):
  `env = dict(os.environ); restore_ld_library_path(env); return env`
  ([`Runner._env`]'s real shape) trusts `env` because it was passed BY NAME
  into the trusted call, even though the assignment that PRODUCED `env` was
  itself untrusted -- but ONLY when that `target_name(some_var)` statement is
  itself a DIRECT top-level statement of the wrapper's own body, not nested
  inside an `if`/`for`/`while`/`try`/`except`/`with` (tan-cli#1004): without
  that restriction, `if False: restore_ld_library_path(env)` trusted `env`
  exactly as readily as the real unconditional call does, reopening
  tan-cli#999 probe 1 for this one shape (see
  `test_reachability_rejects_a_dead_code_guard_on_the_mutation_shape` below).

A function with no qualifying `return` at all fails closed (vacuous truth is
refused, not granted). This is a conservative STRUCTURAL walk, not a real
dataflow/control-flow analysis -- see the four blind-spot classes below for
exactly what shape of code it cannot see through.

## What this gate CANNOT catch

Four classes, following from what a static AST walk can and cannot prove --
not a list of function names, because enumerating names is exactly the
method that missed `check_call`/`getoutput`/`getstatusoutput` in an earlier
round of this same gate (tan-cli#992's own postmortem):

* **Reachability without execution.** This walk cannot run the code, so it
  cannot know whether a branch condition is ever true or false at runtime --
  `if False:` and `if today_is_a_leap_year():` are structurally identical to
  it. For a LOCAL ASSIGNMENT (`name = <expr>` / `name: T = <expr>`), it still
  collects trust LEXICALLY across every branch of a function (`if`/`for`/
  `try`/`except`) without proving a particular assignment DOMINATES a
  particular `return` -- a variable trusted-assigned only inside an `except`
  clause, then returned from a DIFFERENT `return` reachable only via the
  `try`'s success path, would still read as trusted. This is the same
  documented leniency `_locally_trusted_names` (rule 3's file-scan sibling)
  already accepts, and none of the three real wrappers relies on a
  conditionally-reached assignment, so it is left as-is. The bare
  `target_name(some_var)` IN-PLACE-MUTATION statement
  ([`restore_ld_library_path`]'s shape) is the one exception: it DOES require
  the statement to be a direct top-level statement of the function's own
  body (tan-cli#1004) -- a real dominance proof for the shape the one real
  mutation wrapper uses, closing what used to be this exact blind spot for
  it (`if False: restore_ld_library_path(env)` used to trust `env` just as
  readily as the unconditional call). One consequence: a mutation call
  confined to a block that a human can see always runs -- e.g. `try:
  restore_ld_library_path(env)\nexcept Exception: pass\nreturn env` -- is
  now rejected too, even though it is runtime-safe; this walk cannot tell
  "always runs" from "sometimes runs" for a nested block, so it refuses
  both rather than guess (see "Safe shapes this gate rejects" below).
* **Aliasing -- two different consequences depending which rule.** A trusted
  value stored into a container (`d["x"] = spawn_env()`), an attribute
  (`self._env = spawn_env()`), or captured by a closure and read back
  through a different name is invisible to `_locally_trusted_names` (rule
  3's file-scan sibling, used for a real `env=env` spawn-site match) -- it
  tracks bare local `Name` targets and bare `Name` call arguments only, so a
  spawn site built through one of these aliases is a genuine MISS (a real
  leak could hide behind it, undetected). For `_call_reaches_every_return`
  (the wrapper-body reachability check, above) the SAME shapes are instead
  OVER-strict: `d["env"] = spawn_env(); return d["env"]`, `self._env =
  spawn_env(); return self._env`, and `envs = [spawn_env()]; return
  envs[0]` are all measured REJECTED even though the value genuinely does
  flow to the return -- a false positive, not a miss, for this one check
  (tan-cli#1004; see "Safe shapes this gate rejects" below). None of the
  three real wrappers uses aliasing, so this has not needed fixing either
  direction.
* **Dynamic dispatch.** `getattr(subprocess, "run")(...)`, a call stored in
  and invoked through a dict/list, a spawn reached via `exec()`/`eval()`, or
  `target_name` resolved through anything other than a literal `Name` in the
  call's `.func` -- purely static resolution cannot see through these.
  Nothing under `python/tan/` does this today. (`_call_reaches_every_return`
  additionally refuses `<anything>.target_name(...)` outright, tan-cli#1004
  -- see `_direct_call_target`'s own docstring.)
* **Cross-function flow.** When a trusted local is passed as an argument
  into ANOTHER function's call (`prepend_path(env, venv_bin)`), this walk
  trusts that call's result because SOME argument was trusted -- it does not
  (cannot, without inlining the callee) verify the callee actually
  incorporates that argument into what it returns. A function that receives
  a trusted `env` and discards it, returning something unrelated, looks
  structurally identical to `prepend_path`, which does not; nothing under
  `python/tan/` does this today, and this is what makes rule 3's "any
  argument flows through" rule permissive rather than a real dataflow proof.

## Safe shapes this gate rejects (false positives, tan-cli#1004)

The four classes above are MISSES -- unsafe code that reads as trusted. The
opposite failure is just as real for a `safety` gate that a legitimate
wrapper might get worked around instead of fixed: a SAFE shape
`_call_reaches_every_return` rejects. Measured RED
(`_call_reaches_every_return(fn, "spawn_env")` -> `False`) on all of:

* `env: dict[str, str] = spawn_env(); return env` -- **fixed** by this same
  change (`ast.AnnAssign` is now collected alongside `ast.Assign`,
  `_OwnStatements.visit_AnnAssign`/`_locally_trusted_names`); kept in this
  list as the sharp example of why the class matters -- this repo annotates
  heavily, and it was one edit away from `_child_env`'s own current body.
* `return (env := spawn_env())` -- `ast.NamedExpr` (the walrus operator) is
  not a `Name`/`Call`/`BoolOp`/`IfExp`/`Starred`, so `_expr_flows_from` falls
  through to its opaque default. NOT handled -- documented here rather than
  chased, since nothing under `python/tan/` uses this style today.
* `env, extra = spawn_env(), None; return env` -- a tuple-unpacking target;
  `_assign_targets` yields the whole `ast.Tuple` as one target, which is
  never an `ast.Name`, so nothing is trusted. NOT handled.
* `return spawn_env() | {"X": "1"}` -- `ast.BinOp`. NOT handled.
* `return {**spawn_env(), "X": "1"}` -- `ast.Dict` (a `**`-unpack). NOT
  handled.
* `return _build_env()` where `_build_env` itself calls `spawn_env` -- a
  no-argument delegation to an UNVERIFIED helper; correctly conservative
  (rule 3 does not recursively verify arbitrary callees, see
  "cross-function flow" above, and could not tell this apart from a helper
  that does NOT call `spawn_env` without inlining it) but still a rejection
  of code that happens to be safe. NOT handled.
* the same delegation through a nested `def` defined INSIDE the wrapper --
  `_OwnStatements` deliberately does not descend into a nested scope (see
  its own docstring), so a helper `def` local to the wrapper is invisible
  to this walk entirely. NOT handled.
* the three aliasing shapes in the "Aliasing" bullet above (`d["env"] =
  spawn_env(); return d["env"]`, `self._env = spawn_env(); return
  self._env`, `envs = [spawn_env()]; return envs[0]`). NOT handled.
* a mutation call inside a block a human can see always dominates the
  return but this walk cannot prove it (`try: restore_ld_library_path(env)
  \nexcept Exception: pass\nreturn env`) -- introduced BY this change's own
  dominance fix, traded deliberately for closing the tan-cli#999-probe-1
  reopening described above. NOT handled.

None of the "NOT handled" shapes appears anywhere under `python/tan/` today
(the same standing this section's siblings above keep); a future author
hitting one of these should widen `_locally_flowed_names`/`_expr_flows_from`
rather than work around the gate.

Beyond rule 3's own wrapper-body check:

* **A wrapper NOT in the small hand-verified set.** A brand new
  `_my_own_child_env()` added at some future call site, itself never calling
  `spawn_env`, is simply UNTRUSTED by rule 3 above (its call sites fail
  outright) -- safe by default -- but if a future author instead ADDS its
  name to `_TRUSTED_CALL_NAMES` without the corresponding body-verification
  test also being added, the gate would trust it unconditionally. This is a
  hand-maintained allowlist, the same shape (and the same limitation)
  `test_shared_helpers_have_one_definition.py`'s `_SHARED_HELPERS` already
  accepts in this repo.
* **`os.spawnv`/`os.spawnve`/`os.posix_spawn`.** Not used anywhere under
  `python/tan/` today (this gate's own `test_no_os_level_spawn_bypasses_the_check`
  fails loudly if one appears, forcing it to be taught to this file rather
  than silently passing it through), but a genuinely new such call would need
  the `_SubprocessBinding` walk extended to match it.
"""
from __future__ import annotations

import ast
import pathlib

PYTHON_ROOT = pathlib.Path(__file__).resolve().parents[2]
TAN_ROOT = PYTHON_ROOT / "tan"

_SPAWN_ATTRS = {"run", "Popen", "check_output", "call", "check_call"}

#: `subprocess.<attr>` calls that shell out with NO `env=` parameter at all --
#: the same unverifiable-by-construction shape `os.system`/`os.popen` are
#: banned for below, just spelled on the `subprocess` module instead of `os`.
#: `getoutput`/`getstatusoutput` cannot be routed through `spawn_env` no
#: matter how the call site is written, so they are refused outright rather
#: than added to `_SPAWN_ATTRS` (which assumes an `env=` keyword to inspect).
_BANNED_SUBPROCESS_ATTRS = {"getoutput", "getstatusoutput"}

#: Bare names trusted directly -- the ONE primitive itself, matched whether
#: called unqualified (`spawn_env(...)`) or through a module qualifier
#: (`subprocess_env.spawn_env(...)`, `tan.core.subprocess_env.spawn_env(...)`).
_TRUSTED_PRIMITIVE = "spawn_env"

#: Module-level functions verified (below) to call `spawn_env` themselves --
#: trusted when called BARE (`_child_env(...)`), never as `obj._child_env(...)`,
#: which would be a different object's unrelated method.
_TRUSTED_MODULE_WRAPPERS = {
    "_child_env": ("tan/commands/flash_cmd.py", "spawn_env"),
    "_resolution_env": ("tan/commands/flash_cmd.py", "_child_env"),
}

#: `self.<method>(...)` calls trusted ONLY on this exact (class, method) pair --
#: `_env` collides in name with unrelated module-level helpers elsewhere in
#: this tree (`core/build_plan.py::_env`, `bootstrap_cmd.py::_env`), so this is
#: deliberately the narrower "attribute call on `self`" shape, not a bare-name
#: trust for `_env` generally.
_TRUSTED_SELF_METHODS = {
    ("Runner", "_env"): ("tan/commands/bootstrap_cmd.py", "restore_ld_library_path"),
}


class _ModuleAliases(ast.NodeVisitor):
    """Resolves how THIS file imported `subprocess` -- the module name it is
    bound to (`subprocess`, or an `import ... as` alias) and any bare names
    imported directly (`from subprocess import run as _run`)."""

    def __init__(self) -> None:
        self.module_names: set[str] = set()
        self.bare_names: dict[str, str] = {}  # local name -> real subprocess attr

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "subprocess":
                self.module_names.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "subprocess":
            for alias in node.names:
                if alias.name in _SPAWN_ATTRS:
                    self.bare_names[alias.asname or alias.name] = alias.name
        self.generic_visit(node)


def _resolves_to_spawn(func: ast.expr, aliases: _ModuleAliases) -> str | None:
    """The real `subprocess.<attr>` name this call targets, or `None`."""
    if isinstance(func, ast.Attribute) and func.attr in _SPAWN_ATTRS:
        if isinstance(func.value, ast.Name) and func.value.id in aliases.module_names:
            return func.attr
    if isinstance(func, ast.Name) and func.id in aliases.bare_names:
        return aliases.bare_names[func.id]
    return None


def _trusted_call_names_present(expr: ast.expr, rel: str) -> set[str]:
    """Every `Call`'s resolved name anywhere inside `expr`'s own subtree --
    catches `with_venv_on_path(spawn_env(), tool)` and
    `spawn_env(base=with_venv_on_path(...))` alike, not just a bare top-level
    call.

    `rel` is the POSIX-relative path of the file this expression was parsed
    out of. `_TRUSTED_MODULE_WRAPPERS` is keyed by bare name, but trust is
    granted only when `rel` matches the wrapper's OWN recorded definition
    site -- a same-named function defined in some OTHER file (e.g. a second,
    unrelated `_child_env()` that never calls `spawn_env`) must not borrow the
    verified one's trust just because the name collides."""
    found: set[str] = set()
    for node in ast.walk(expr):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id == _TRUSTED_PRIMITIVE:
                found.add(func.id)
            elif func.id in _TRUSTED_MODULE_WRAPPERS and _TRUSTED_MODULE_WRAPPERS[func.id][0] == rel:
                found.add(func.id)
        elif isinstance(func, ast.Attribute):
            if func.attr == _TRUSTED_PRIMITIVE:
                found.add(func.attr)
            elif isinstance(func.value, ast.Name) and func.value.id == "self":
                found.add(f"self.{func.attr}")
    return found


class _EnclosingScopeTracker(ast.NodeVisitor):
    """Walks the whole module tracking, for every `Call` node, which
    `ast.ClassDef` (if any) and which `ast.FunctionDef`/`AsyncFunctionDef`
    directly enclose it -- so `self._env(...)` can be checked against the
    RIGHT class, and a bare `env=env` can be traced back to that SAME
    function's own local assignment rather than an unrelated one elsewhere in
    the file with the same variable name."""

    def __init__(self) -> None:
        self.class_of_call: dict[int, str | None] = {}  # id(call) -> class name
        self.func_of_call: dict[int, ast.AST | None] = {}  # id(call) -> FunctionDef node
        self._class_stack: list[str] = []
        self._func_stack: list[ast.AST] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def _visit_func(self, node: ast.AST) -> None:
        self._func_stack.append(node)
        self.generic_visit(node)
        self._func_stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_Call(self, node: ast.Call) -> None:
        self.class_of_call[id(node)] = self._class_stack[-1] if self._class_stack else None
        self.func_of_call[id(node)] = self._func_stack[-1] if self._func_stack else None
        self.generic_visit(node)


def _locally_trusted_names(func: ast.AST, rel: str) -> dict[str, set[str]]:
    """`{variable: trusted-call-names-found-in-its-assignment}` for every
    plain `name = <expr>` OR annotated `name: T = <expr>` assignment anywhere
    in `func`'s body (nested blocks included -- an `if`/`try` does not hide
    the assignment from this walk, matching how straight-line code in this
    repo is actually written). Lenient by design: a variable assigned
    MULTIPLE times collects trust from every assignment, not just the last
    textually -- sufficient for tracing the "build env once, spawn twice"
    shape these call sites use; it is not a real dataflow/control-flow
    analysis.

    `ast.AnnAssign` (tan-cli#1004) is handled alongside plain `ast.Assign`
    because it is the exact same binding, just annotated -- `env: dict[str,
    str] = spawn_env()` was a false-positive VIOLATION before this: a
    legitimate `subprocess.run(..., env=env)` call site would have read as
    unrouted purely because its building assignment carried a type
    annotation. A bare annotation with no `=` (`node.value is None`) binds
    nothing and is skipped."""
    out: dict[str, set[str]] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        trusted = _trusted_call_names_present(value, rel)
        for target in targets:
            if isinstance(target, ast.Name):
                out.setdefault(target.id, set()).update(trusted)
    return out


def _direct_call_target(call: ast.Call, target_name: str) -> bool:
    """Whether `call` is syntactically a bare `target_name(...)` -- a literal
    unqualified `ast.Name`, and ONLY that (tan-cli#1004). Deliberately
    narrower than `_trusted_call_names_present`'s Attribute-qualified
    matching (which exists to recognise `subprocess_env.spawn_env(...)`
    reached through a module alias at a real SPAWN call site): every real
    caller of this function is instead checking a hand-verified wrapper's OWN
    body for a call to a name that body imports and calls bare -- `spawn_env`,
    `_child_env`, and `restore_ld_library_path` are all bare imports/module
    functions in the three wrapper files this gate trusts, never accessed
    through an attribute. Accepting `<anything>.target_name(...)` here (the
    prior behaviour) let `return evil.spawn_env()` inside a wrapper's body
    borrow the real primitive's trust from an unrelated object's same-named
    method -- measured green before this fix, via
    `test_reachability_rejects_a_call_through_an_unrelated_attribute` below."""
    return isinstance(call.func, ast.Name) and call.func.id == target_name


def _expr_flows_from(expr: ast.expr, target_name: str, trusted_locals: set[str]) -> bool:
    """Whether `target_name(...)`'s result plausibly reaches the value `expr`
    evaluates to. A conservative STRUCTURAL walk, not dataflow analysis --
    see "What this gate CANNOT catch" in the module docstring for exactly
    what this cannot see through.

    * a direct `target_name(...)` call -- true immediately.
    * any OTHER call -- true if any of its positional/keyword arguments flow
      true (the "cross-function flow" class: trusts the CALL because an
      argument was trusted, without verifying the callee actually uses it).
    * a bare `Name` -- true iff it is in `trusted_locals`.
    * `and`/`or`/ternary -- true if EITHER operand/branch flows true; this
      walk cannot execute the condition to know which one runs.
    * `ast.Subscript` -- ALWAYS opaque, deliberately: refuses to descend
      into `.value` or `.slice` at all. `(target_name(), other)[1]` visibly
      CONTAINS a call to `target_name` but the index throws its result away,
      and there is no static way to tell a safe index from a discarding one
      -- this is what makes `return (spawn_env(), dict(os.environ))[1]` red
      (tan-cli#999 probe 3).
    * anything else (constants, comprehensions, attribute access on an
      untracked base, ...) -- opaque, false.
    """
    if isinstance(expr, ast.Call):
        if _direct_call_target(expr, target_name):
            return True
        if any(_expr_flows_from(a, target_name, trusted_locals) for a in expr.args):
            return True
        return any(_expr_flows_from(kw.value, target_name, trusted_locals) for kw in expr.keywords)
    if isinstance(expr, ast.Name):
        return expr.id in trusted_locals
    if isinstance(expr, ast.BoolOp):
        return any(_expr_flows_from(v, target_name, trusted_locals) for v in expr.values)
    if isinstance(expr, ast.IfExp):
        return _expr_flows_from(expr.body, target_name, trusted_locals) or _expr_flows_from(
            expr.orelse, target_name, trusted_locals
        )
    if isinstance(expr, ast.Starred):
        return _expr_flows_from(expr.value, target_name, trusted_locals)
    return False


def _is_bare_none(value: ast.expr | None) -> bool:
    return value is None or (isinstance(value, ast.Constant) and value.value is None)


class _OwnStatements(ast.NodeVisitor):
    """Collects the `Assign` / bare-`Call`-`Expr` / `Return` statements that
    belong DIRECTLY to `root` -- descending into `if`/`for`/`while`/`try`/
    `except`/`with` bodies (no control-flow modelling -- see "reachability
    without execution" in the module docstring) but NOT into any `def`/
    `async def`/`lambda` nested inside `root`. A nested scope's own
    reachability is a separate question this walk does not answer; a value
    crossing INTO one through a closure is the "aliasing" blind spot, not
    something this class claims to see through."""

    def __init__(self, root: ast.AST) -> None:
        self.root = root
        self.assigns: list[ast.Assign | ast.AnnAssign] = []
        self.bare_calls: list[ast.Expr] = []
        self.returns: list[ast.Return] = []

    def visit_FunctionDef(self, node: ast.AST) -> None:
        if node is self.root:
            self.generic_visit(node)
        # else: a nested `def` -- its statements are not `root`'s own.

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return  # never relevant to a single function's own body

    def visit_Assign(self, node: ast.Assign) -> None:
        self.assigns.append(node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        # tan-cli#1004 false-positive class: `env: dict[str, str] =
        # spawn_env()` is annotation-syntax for the exact same binding
        # `visit_Assign` above already collects for plain `ast.Assign` -- one
        # edit away from `_child_env`'s current body. A bare annotation with
        # no `=` (`node.value is None`) binds nothing and is skipped.
        if node.value is not None:
            self.assigns.append(node)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        self.returns.append(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        if isinstance(node.value, ast.Call):
            self.bare_calls.append(node)


def _assign_targets(node: ast.Assign | ast.AnnAssign) -> list[ast.expr]:
    """The LHS target(s) of an assignment, normalised across `ast.Assign`
    (`.targets`, a list -- possibly chained, possibly a tuple-unpack) and
    `ast.AnnAssign` (a single `.target`, tan-cli#1004)."""
    if isinstance(node, ast.AnnAssign):
        return [node.target]
    return list(node.targets)


def _locally_flowed_names(own: _OwnStatements, target_name: str) -> set[str]:
    """Fixed-point closure of every local name `target_name(...)`'s result
    reaches, over `own`'s collected assigns/bare-calls:

    * `name = <expr>` (or annotated `name: T = <expr>`, tan-cli#1004) where
      `<expr>` flows true adds `name` (`env = spawn_env()` adds `env`).
      Lexical across every branch of the function, same documented leniency
      `_locally_trusted_names` (rule 3's file-scan sibling) already accepts
      for this shape -- see "reachability without execution" in the module
      docstring; none of the three real wrappers relies on a conditionally-
      reached assignment, so this branch is not further restricted here.
    * a bare `target_name(some_var)` statement -- the in-place-mutation shape
      [`restore_ld_library_path`] uses -- adds `some_var`
      (`restore_ld_library_path(env)` adds `env`, even though `env` was
      itself assigned from an UNTRUSTED expression beforehand) -- BUT ONLY
      when that statement is a DIRECT top-level statement of the function's
      own body (`own.root.body`), not nested inside any `if`/`for`/`while`/
      `try`/`except`/`with` (tan-cli#1004). Before this restriction, `if
      False: restore_ld_library_path(env)` added `env` to `trusted` exactly
      as readily as an unconditional top-level call, because this branch
      only checked THAT the statement existed in `own.bare_calls`, never
      WHERE it lived -- reopening tan-cli#999 probe 1 (and its discarded-
      target, thrown-away, and untaken-except analogues) for `Runner._env`,
      the one real wrapper using this shape. `Runner._env`'s actual
      `restore_ld_library_path(env)` call is itself a direct top-level
      statement, so this is a real (if conservative) dominance proof for
      the shape the real wrapper uses, not merely a stricter lexical filter.

    Iterates to a fixed point since a later assignment can build on an
    earlier one's trust (`a = spawn_env(); b = prepend_path(a, x)` trusts
    `b` too)."""
    trusted: set[str] = set()
    top_level_ids = {id(stmt) for stmt in own.root.body}  # type: ignore[attr-defined]
    changed = True
    while changed:
        changed = False
        for assign in own.assigns:
            if _expr_flows_from(assign.value, target_name, trusted):  # type: ignore[arg-type]
                for tgt in _assign_targets(assign):
                    if isinstance(tgt, ast.Name) and tgt.id not in trusted:
                        trusted.add(tgt.id)
                        changed = True
        for expr_stmt in own.bare_calls:
            if id(expr_stmt) not in top_level_ids:
                continue
            call = expr_stmt.value
            assert isinstance(call, ast.Call)
            if not _direct_call_target(call, target_name):
                continue
            for arg in call.args:
                if isinstance(arg, ast.Name) and arg.id not in trusted:
                    trusted.add(arg.id)
                    changed = True
            for kw in call.keywords:
                if isinstance(kw.value, ast.Name) and kw.value.id not in trusted:
                    trusted.add(kw.value.id)
                    changed = True
    return trusted


def _call_reaches_every_return(func: ast.AST, target_name: str) -> bool:
    """Whether `target_name(...)`'s result is reachable on the path that
    produces EVERY value `func` can return (tan-cli#999). A bare `return
    None` is exempted -- there is no dict there to leak. Fails CLOSED: a
    function with no qualifying return at all trusts nothing, rather than
    vacuously passing an empty `all()`."""
    own = _OwnStatements(func)
    own.visit(func)
    trusted_locals = _locally_flowed_names(own, target_name)
    qualifying = [r for r in own.returns if not _is_bare_none(r.value)]
    if not qualifying:
        return False
    return all(_expr_flows_from(r.value, target_name, trusted_locals) for r in qualifying)  # type: ignore[arg-type]


def _violations_in_file(path: pathlib.Path) -> list[str]:
    rel = path.relative_to(PYTHON_ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = _ModuleAliases()
    aliases.visit(tree)
    if not aliases.module_names and not aliases.bare_names:
        return []  # this file never imports subprocess at all

    scope_tracker = _EnclosingScopeTracker()
    scope_tracker.visit(tree)
    # Memoised per enclosing function node -- `_locally_trusted_names` walks
    # the whole function body, and a function with several spawn sites (e.g.
    # `flash_cmd._spawn`) would otherwise redo that walk once per site.
    locals_cache: dict[int, dict[str, set[str]]] = {}

    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        spawn_attr = _resolves_to_spawn(node.func, aliases)
        if spawn_attr is None:
            continue
        env_kw = next((kw for kw in node.keywords if kw.arg == "env"), None)
        if env_kw is None:
            out.append(f"{rel}:{node.lineno}: subprocess.{spawn_attr}(...) has no `env=` at all")
            continue
        if isinstance(env_kw.value, ast.Constant) and env_kw.value.value is None:
            out.append(f"{rel}:{node.lineno}: subprocess.{spawn_attr}(..., env=None)")
            continue
        trusted = _trusted_call_names_present(env_kw.value, rel)
        enclosing_class = scope_tracker.class_of_call.get(id(node))
        enclosing_func = scope_tracker.func_of_call.get(id(node))
        ok = bool(trusted & ({_TRUSTED_PRIMITIVE} | set(_TRUSTED_MODULE_WRAPPERS)))
        if not ok:
            for (cls, method) in _TRUSTED_SELF_METHODS:
                if f"self.{method}" in trusted and enclosing_class == cls:
                    ok = True
                    break
        if not ok and isinstance(env_kw.value, ast.Name) and enclosing_func is not None:
            # `env=env` (or any other bare variable): trace back to that
            # SAME function's own local assignment(s) rather than trusting
            # a same-named variable elsewhere in the file.
            cache_key = id(enclosing_func)
            if cache_key not in locals_cache:
                locals_cache[cache_key] = _locally_trusted_names(enclosing_func, rel)
            local_trust = locals_cache[cache_key].get(env_kw.value.id, set())
            ok = bool(local_trust & ({_TRUSTED_PRIMITIVE} | set(_TRUSTED_MODULE_WRAPPERS)))
        if not ok:
            out.append(
                f"{rel}:{node.lineno}: subprocess.{spawn_attr}(..., env=<{ast.dump(env_kw.value)[:80]}>) "
                "does not route through tan.core.subprocess_env.spawn_env (or a verified wrapper)"
            )
    return out


def _all_violations() -> list[str]:
    out: list[str] = []
    for path in sorted(TAN_ROOT.rglob("*.py")):
        out.extend(_violations_in_file(path))
    return out


def test_every_subprocess_spawn_routes_through_spawn_env():
    violations = _all_violations()
    assert not violations, (
        "these spawn sites build a child environment without going through "
        "`tan.core.subprocess_env.spawn_env` (tan-cli#992) -- a frozen `tan`'s "
        "bundled LD_LIBRARY_PATH leaks into the child unless it does:\n  "
        + "\n  ".join(violations)
    )


def test_the_walk_actually_finds_the_real_call_sites():
    """Anti-vacuity: a broken import-alias resolver or a moved package root
    would make `_all_violations` silently return `[]` having looked at
    nothing, and the test above would report a false green forever.
    `flash_cmd.py` alone has 5 real spawn sites -- counted as THAT file's own
    contribution (`found - before`), not the running total across every file
    sorted ahead of it, which is already well past 5 by the time the walk
    reaches `flash_cmd.py` and so could never catch all 5 of its sites going
    silently missing."""
    found = 0
    for path in sorted(TAN_ROOT.rglob("*.py")):
        rel = path.relative_to(PYTHON_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _ModuleAliases()
        aliases.visit(tree)
        if not aliases.module_names and not aliases.bare_names:
            continue
        before = found
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _resolves_to_spawn(node.func, aliases):
                found += 1
        if rel == "tan/commands/flash_cmd.py":
            assert found - before >= 5, f"only found {found - before} spawn sites in flash_cmd.py"
    assert found >= 30, f"only {found} subprocess spawn sites found under {TAN_ROOT}"


def test_the_verified_wrappers_still_call_the_primitive():
    """Anti-drift for `_TRUSTED_MODULE_WRAPPERS`/`_TRUSTED_SELF_METHODS`: each
    entry is trusted ONLY because this test independently confirms its body
    still REACHES the name it claims to on every path that produces its
    return value (`_call_reaches_every_return`, tan-cli#999/#1004). Hollow
    one out -- e.g. make `_child_env` return `dict(os.environ)` again without
    going through `spawn_env` -- and THIS test reds, even though no
    subprocess call site changed a single character (the "helper wrapping a
    helper" defence tan-cli#992 asked for). A call merely PRESENT somewhere
    in the body -- behind `if False:`, discarded via `(x, y)[1]`, or confined
    to a branch the return doesn't take -- is no longer enough to pass FOR
    EITHER shape this gate trusts: the direct-return shape
    (`_child_env`/`_resolution_env`, closed by tan-cli#999) AND the in-place-
    mutation shape (`Runner._env`, closed by tan-cli#1004 -- #999 only
    verified the dead-code guard was refused for the direct-return shape,
    leaving the exact same `if False:` bypass open for the one wrapper that
    mutates rather than returns; see the module docstring's "reachability
    without execution" bullet). See `_call_reaches_every_return`'s own
    docstring and the eight `test_reachability_rejects_*` probes below (four
    per shape)."""
    problems: list[str] = []
    for name, (rel, must_call) in _TRUSTED_MODULE_WRAPPERS.items():
        path = PYTHON_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]
        if len(defs) != 1:
            problems.append(f"{rel}: expected exactly one `def {name}`, found {len(defs)}")
            continue
        if not _call_reaches_every_return(defs[0], must_call):
            problems.append(
                f"{rel}::{name} no longer reaches `{must_call}()` on every return path"
            )

    for (cls, method), (rel, must_call) in _TRUSTED_SELF_METHODS.items():
        path = PYTHON_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == cls:
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == method:
                        target = item
        if target is None:
            problems.append(f"{rel}: no `class {cls}` method `{method}` found")
            continue
        if not _call_reaches_every_return(target, must_call):
            problems.append(
                f"{rel}::{cls}.{method} no longer reaches `{must_call}()` on every return path"
            )

    assert not problems, "\n".join(problems)


def test_reachability_rejects_a_dead_code_guard():
    """tan-cli#999 probe 1: `if False: spawn_env()` reads as "the wrapper
    calls spawn_env" to a bare presence check, but the branch never runs and
    the returned value never passes through it."""
    fn = ast.parse(
        "def _child_env():\n"
        "    if False:\n"
        "        spawn_env()\n"
        "    return dict(os.environ)\n"
    ).body[0]
    assert not _call_reaches_every_return(fn, "spawn_env")


def test_reachability_rejects_a_discarded_call_result():
    """tan-cli#999 probe 2: `spawn_env()` is called, but its RESULT is never
    used -- the returned dict is a fresh, unrestored `dict(os.environ)`."""
    fn = ast.parse("def _child_env():\n    spawn_env()\n    return dict(os.environ)\n").body[0]
    assert not _call_reaches_every_return(fn, "spawn_env")


def test_reachability_rejects_a_call_thrown_away_through_a_subscript():
    """tan-cli#999 probe 3: `spawn_env()` is textually present INSIDE the
    `return` statement's own value expression -- closer to a real use than
    probes 1/2 -- but `[1]` selects the OTHER tuple element, discarding it.
    A walk that trusts "any Call anywhere in the return expression's
    subtree" is fooled by this; `_expr_flows_from` refuses to look inside a
    `Subscript` at all rather than guess which index is safe."""
    fn = ast.parse("def _child_env():\n    return (spawn_env(), dict(os.environ))[1]\n").body[0]
    assert not _call_reaches_every_return(fn, "spawn_env")


def test_reachability_rejects_a_call_confined_to_an_untaken_except_branch():
    """tan-cli#999 probe 4: `spawn_env()` is only ever called from an
    exception handler; the normal-path return builds its dict independently
    and never sees it -- and even the except branch's own return doesn't use
    the call's result, so this reds regardless of which branch runs."""
    fn = ast.parse(
        "def _child_env():\n"
        "    try:\n"
        "        return dict(os.environ)\n"
        "    except OSError:\n"
        "        spawn_env()\n"
        "        return dict(os.environ)\n"
    ).body[0]
    assert not _call_reaches_every_return(fn, "spawn_env")


def test_reachability_rejects_a_dead_code_guard_on_the_mutation_shape():
    """tan-cli#1004 probe 1, mutation shape: the exact analogue of probe 1
    above (`if False: spawn_env()`), but for `Runner._env`
    (`bootstrap_cmd.py`) -- the one real wrapper trusted via the in-place-
    mutation shape (`restore_ld_library_path(env)` then `return env`), not
    the direct-return shape #999's four probes exercised. Before the
    top-level-only dominance restriction on `_locally_flowed_names`'s
    bare-call branch, this shape measured GREEN against the real
    `bootstrap_cmd.py::Runner._env` -- reopening #999's own probe 1 for the
    one wrapper it was never actually run against."""
    fn = ast.parse(
        "def _env(self):\n"
        "    env = dict(os.environ)\n"
        "    if False:\n"
        "        restore_ld_library_path(env)\n"
        "    return env\n"
    ).body[0]
    assert not _call_reaches_every_return(fn, "restore_ld_library_path")
    # The same bug, viewed through a copy rather than a bare return -- also
    # measured GREEN before this fix (`env` was trusted lexically, so
    # `dict(env)` "flowed" from it via the cross-function-flow rule too).
    copied = ast.parse(
        "def _env(self):\n"
        "    env = dict(os.environ)\n"
        "    if False:\n"
        "        restore_ld_library_path(env)\n"
        "    return dict(env)\n"
    ).body[0]
    assert not _call_reaches_every_return(copied, "restore_ld_library_path")


def test_reachability_rejects_a_mutation_applied_to_the_wrong_target():
    """tan-cli#1004 probe 2, mutation shape: `restore_ld_library_path` IS
    called, unconditionally, at the top level -- but on a throwaway `other`
    dict, never on the `env` the function actually returns. The direct-
    return analogue of this (probe 2 above) is "call and discard the
    result"; the mutation shape has no return value to discard, so its
    equivalent failure is "mutate the wrong object"."""
    fn = ast.parse(
        "def _env(self):\n"
        "    env = dict(os.environ)\n"
        "    other = dict(os.environ)\n"
        "    restore_ld_library_path(other)\n"
        "    return env\n"
    ).body[0]
    assert not _call_reaches_every_return(fn, "restore_ld_library_path")


def test_reachability_rejects_a_mutation_call_thrown_away_in_a_container():
    """tan-cli#1004 probe 3, mutation shape: `restore_ld_library_path(env)`
    is textually present, but only as an element of a list literal that is
    itself never returned -- closer to probe 3's subscript-throwaway above
    than probe 2's plain discard, since the call is syntactically INSIDE an
    expression, just not one this walk's bare-call-statement trust applies
    to (only a literal `Expr`-statement call is a "bare call" at all)."""
    fn = ast.parse(
        "def _env(self):\n"
        "    env = dict(os.environ)\n"
        "    calls = [restore_ld_library_path(env)]\n"
        "    return env\n"
    ).body[0]
    assert not _call_reaches_every_return(fn, "restore_ld_library_path")


def test_reachability_rejects_a_mutation_confined_to_an_untaken_except_branch():
    """tan-cli#1004 probe 4, mutation shape: `restore_ld_library_path(env)`
    is only ever called from an exception handler; the normal-path return
    sees `env` before any restore ran. Direct analogue of probe 4 above,
    but for the mutation wrapper -- the nested `except` body is not a
    top-level statement of the function, so the new dominance restriction
    refuses it regardless of which branch actually runs."""
    fn = ast.parse(
        "def _env(self):\n"
        "    env = dict(os.environ)\n"
        "    try:\n"
        "        return env\n"
        "    except OSError:\n"
        "        restore_ld_library_path(env)\n"
        "        return env\n"
    ).body[0]
    assert not _call_reaches_every_return(fn, "restore_ld_library_path")


def test_reachability_accepts_an_annotated_assignment():
    """tan-cli#1004 false-positive fix: `env: dict[str, str] = spawn_env()`
    used to measure RED (`ast.AnnAssign` was invisible to `_OwnStatements`)
    even though the value plainly flows to the return -- one edit away from
    `_child_env`'s own current body. Now accepted, matching plain
    `ast.Assign`."""
    fn = ast.parse(
        "def _child_env():\n"
        "    env: dict[str, str] = spawn_env()\n"
        "    return env\n"
    ).body[0]
    assert _call_reaches_every_return(fn, "spawn_env")


def test_reachability_rejects_a_call_through_an_unrelated_attribute():
    """tan-cli#1004 nit: `_direct_call_target` used to match
    `<anything>.target_name(...)`, so `return evil.spawn_env()` inside a
    wrapper's body satisfied the walk by borrowing an unrelated object's
    same-named method's trust -- measured GREEN before this fix. Now
    refused: only a literal unqualified `Name` call counts."""
    fn = ast.parse("def _child_env():\n    return evil.spawn_env()\n").body[0]
    assert not _call_reaches_every_return(fn, "spawn_env")


def test_reachability_documents_the_still_rejected_safe_shapes():
    """Locks in the "NOT handled" entries from the module docstring's "Safe
    shapes this gate rejects" section (tan-cli#1004) as a REGRESSION test in
    the permissive direction: if a future change to `_expr_flows_from`/
    `_locally_flowed_names` accidentally starts ACCEPTING one of these, this
    test catches the drift so the module docstring's claim can be corrected
    deliberately rather than silently going stale in the safe direction."""
    walrus = ast.parse("def _child_env():\n    return (env := spawn_env())\n").body[0]
    assert not _call_reaches_every_return(walrus, "spawn_env")

    tuple_unpack = ast.parse(
        "def _child_env():\n    env, extra = spawn_env(), None\n    return env\n"
    ).body[0]
    assert not _call_reaches_every_return(tuple_unpack, "spawn_env")

    binop_merge = ast.parse(
        'def _child_env():\n    return spawn_env() | {"X": "1"}\n'
    ).body[0]
    assert not _call_reaches_every_return(binop_merge, "spawn_env")

    dict_unpack = ast.parse(
        'def _child_env():\n    return {**spawn_env(), "X": "1"}\n'
    ).body[0]
    assert not _call_reaches_every_return(dict_unpack, "spawn_env")

    delegation = ast.parse(
        "def _child_env():\n    return _build_env()\n"
    ).body[0]
    assert not _call_reaches_every_return(delegation, "spawn_env")

    nested_def = ast.parse(
        "def _child_env():\n"
        "    def _build():\n"
        "        return spawn_env()\n"
        "    return _build()\n"
    ).body[0]
    assert not _call_reaches_every_return(nested_def, "spawn_env")

    dict_alias = ast.parse(
        'def _child_env():\n    d = {}\n    d["env"] = spawn_env()\n    return d["env"]\n'
    ).body[0]
    assert not _call_reaches_every_return(dict_alias, "spawn_env")

    attr_alias = ast.parse(
        "def _env(self):\n    self._env = spawn_env()\n    return self._env\n"
    ).body[0]
    assert not _call_reaches_every_return(attr_alias, "spawn_env")

    list_index_alias = ast.parse(
        "def _child_env():\n    envs = [spawn_env()]\n    return envs[0]\n"
    ).body[0]
    assert not _call_reaches_every_return(list_index_alias, "spawn_env")


def test_locally_trusted_names_recognises_an_annotated_assignment():
    """tan-cli#1004 false-positive class: `_locally_trusted_names` (rule 3's
    file-scan sibling, used to trace a bare `env=env` spawn-site keyword back
    to its building assignment) used to collect trust only from
    `ast.Assign`, missing `ast.AnnAssign` the same way `_OwnStatements` did --
    a real `env: dict[str, str] = spawn_env()` followed by
    `subprocess.run(..., env=env)` would have been a false-positive
    VIOLATION (a legitimate spawn site reported as unrouted)."""
    fn = ast.parse(
        "def f():\n"
        "    env: dict[str, str] = spawn_env()\n"
        "    subprocess.run(['true'], env=env)\n"
    ).body[0]
    trusted = _locally_trusted_names(fn, "some/module.py")
    assert "spawn_env" in trusted.get("env", set())


def test_reachability_accepts_the_real_wrapper_shapes():
    """Positive control for the four probes above: the exact shapes the real
    hand-verified wrappers use must still pass. A trusted value built once
    and returned on one branch, folded into another call's argument on
    another branch ([`_child_env`]); a bare pass-through to another trusted
    wrapper ([`_resolution_env`]); and an in-place mutation of a variable
    that was assigned from an UNTRUSTED expression before the trusted call
    ran ([`Runner._env`])."""
    direct_and_folded = ast.parse(
        "def _child_env(venv_bin):\n"
        "    env = spawn_env()\n"
        "    if venv_bin is None:\n"
        "        return env\n"
        "    return prepend_path(env, venv_bin)\n"
    ).body[0]
    assert _call_reaches_every_return(direct_and_folded, "spawn_env")

    pass_through = ast.parse(
        "def _resolution_env(venv_bin):\n    return _child_env(venv_bin)\n"
    ).body[0]
    assert _call_reaches_every_return(pass_through, "_child_env")

    mutation_of_a_prior_untrusted_assignment = ast.parse(
        "def _env(self, extra_env=None):\n"
        "    ld = os.environ.get('LD_LIBRARY_PATH_ORIG')\n"
        "    if not extra_env and ld is None:\n"
        "        return None\n"
        "    env = dict(os.environ)\n"
        "    restore_ld_library_path(env)\n"
        "    return env\n"
    ).body[0]
    assert _call_reaches_every_return(
        mutation_of_a_prior_untrusted_assignment, "restore_ld_library_path"
    )


def test_no_os_level_spawn_bypasses_the_check():
    """`os.system`/`os.popen` cannot be checked by the rule above at all --
    `os.system` has no `env=` parameter (it always shells out through the
    CURRENT process's environment, unrestorable per-call), and `os.popen` is
    the same shell-out shape. `subprocess.getoutput`/`getstatusoutput` are the
    identical unverifiable shape spelled on the OTHER module -- no `env=`
    parameter exists on either, so `_SPAWN_ATTRS`'s env-inspection machinery
    can never apply to them (`_BANNED_SUBPROCESS_ATTRS`, above). None of these
    six names is used under `python/tan/` today; this fails loudly the day
    one is added, rather than silently letting a spawn class this gate cannot
    verify slip in unexamined."""
    banned = {"system", "popen", "spawnl", "spawnv", "spawnlp", "spawnvp"}
    hits: list[str] = []
    for path in sorted(TAN_ROOT.rglob("*.py")):
        rel = path.relative_to(PYTHON_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _ModuleAliases()
        aliases.visit(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if not isinstance(node.func.value, ast.Name):
                continue
            if node.func.value.id == "os" and node.func.attr in banned:
                hits.append(f"{rel}:{node.lineno}: os.{node.func.attr}(...)")
            elif (
                node.func.value.id in aliases.module_names
                and node.func.attr in _BANNED_SUBPROCESS_ATTRS
            ):
                hits.append(f"{rel}:{node.lineno}: subprocess.{node.func.attr}(...)")
    assert not hits, (
        "these spawn calls cannot be verified by this gate at all -- route "
        "through subprocess + tan.core.subprocess_env.spawn_env instead:\n  "
        + "\n  ".join(hits)
    )
