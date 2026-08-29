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
   still call `spawn_env`/`restore_ld_library_path` (see
   `test_the_verified_wrappers_still_call_the_primitive` below) -- so an edit
   that hollows out a wrapper (tan-cli#992's own "helper wrapping a helper"
   concern) reds here even though no subprocess call site changed at all.

## What this gate CANNOT catch (see this PR's body for the full list)

* **A wrapper NOT in the small hand-verified set.** A brand new
  `_my_own_child_env()` added at some future call site, itself never calling
  `spawn_env`, is simply UNTRUSTED by rule 3 above (its call sites fail
  outright) -- safe by default -- but if a future author instead ADDS its
  name to `_TRUSTED_CALL_NAMES` without the corresponding body-verification
  test also being added, the gate would trust it unconditionally. This is a
  hand-maintained allowlist, the same shape (and the same limitation)
  `test_shared_helpers_have_one_definition.py`'s `_OWNED_BY_SHAPES` already
  accepts in this repo.
* **`os.spawnv`/`os.spawnve`/`os.posix_spawn`.** Not used anywhere under
  `python/tan/` today (this gate's own `test_no_os_level_spawn_bypasses_the_check`
  fails loudly if one appears, forcing it to be taught to this file rather
  than silently passing it through), but a genuinely new such call would need
  the `_SubprocessBinding` walk extended to match it.
* **A dynamically resolved call target** -- `getattr(subprocess, "run")(...)`,
  a call stored in and invoked through a dict/list, or a spawn reached via
  `exec()`/`eval()`. Purely static analysis cannot see through these; nothing
  under `python/tan/` does this today.
* **A structurally-present but semantically-empty reference** -- e.g.
  `env=(spawn_env(), dict(os.environ))[1]`, which contains a `Call` to
  `spawn_env` (so rule 3 above passes) but never actually uses its result.
  This is a textual/structural check, not a dataflow one; nothing under
  `python/tan/` is written this way, and code review is what catches a
  contortion like this, not this gate.
"""
from __future__ import annotations

import ast
import pathlib

PYTHON_ROOT = pathlib.Path(__file__).resolve().parents[2]
TAN_ROOT = PYTHON_ROOT / "tan"

_SPAWN_ATTRS = {"run", "Popen", "check_output", "call"}

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


def _trusted_call_names_present(expr: ast.expr) -> set[str]:
    """Every `Call`'s resolved name anywhere inside `expr`'s own subtree --
    catches `with_venv_on_path(spawn_env(), tool)` and
    `spawn_env(base=with_venv_on_path(...))` alike, not just a bare top-level
    call."""
    found: set[str] = set()
    for node in ast.walk(expr):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id == _TRUSTED_PRIMITIVE or func.id in _TRUSTED_MODULE_WRAPPERS:
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


def _locally_trusted_names(func: ast.AST) -> dict[str, set[str]]:
    """`{variable: trusted-call-names-found-in-its-assignment}` for every
    plain `name = <expr>` assignment anywhere in `func`'s body (nested blocks
    included -- an `if`/`try` does not hide the assignment from this walk,
    matching how straight-line code in this repo is actually written).
    Lenient by design: a variable assigned MULTIPLE times collects trust from
    every assignment, not just the last textually -- sufficient for tracing
    the "build env once, spawn twice" shape these call sites use; it is not a
    real dataflow/control-flow analysis."""
    out: dict[str, set[str]] = {}
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        trusted = _trusted_call_names_present(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                out.setdefault(target.id, set()).update(trusted)
    return out


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
        trusted = _trusted_call_names_present(env_kw.value)
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
                locals_cache[cache_key] = _locally_trusted_names(enclosing_func)
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
    `flash_cmd.py` alone has 5 real spawn sites."""
    found = 0
    for path in sorted(TAN_ROOT.rglob("*.py")):
        rel = path.relative_to(PYTHON_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _ModuleAliases()
        aliases.visit(tree)
        if not aliases.module_names and not aliases.bare_names:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _resolves_to_spawn(node.func, aliases):
                found += 1
        if rel == "tan/commands/flash_cmd.py":
            assert found >= 5, f"only found {found} spawn sites total by flash_cmd.py"
    assert found >= 30, f"only {found} subprocess spawn sites found under {TAN_ROOT}"


def test_the_verified_wrappers_still_call_the_primitive():
    """Anti-drift for `_TRUSTED_MODULE_WRAPPERS`/`_TRUSTED_SELF_METHODS`: each
    entry is trusted ONLY because this test independently confirms its body
    still calls the name it claims to. Hollow one out -- e.g. make
    `_child_env` return `dict(os.environ)` again without going through
    `spawn_env` -- and THIS test reds, even though no subprocess call site
    changed a single character. This is the "helper wrapping a helper" defence
    tan-cli#992 asked for."""
    problems: list[str] = []
    for name, (rel, must_call) in _TRUSTED_MODULE_WRAPPERS.items():
        path = PYTHON_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]
        if len(defs) != 1:
            problems.append(f"{rel}: expected exactly one `def {name}`, found {len(defs)}")
            continue
        calls = {
            c.func.id
            for c in ast.walk(defs[0])
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        }
        if must_call not in calls:
            problems.append(f"{rel}::{name} no longer calls `{must_call}()`")

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
        calls = {
            c.func.id
            for c in ast.walk(target)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        }
        if must_call not in calls:
            problems.append(f"{rel}::{cls}.{method} no longer calls `{must_call}()`")

    assert not problems, "\n".join(problems)


def test_no_os_level_spawn_bypasses_the_check():
    """`os.system`/`os.popen` cannot be checked by the rule above at all --
    `os.system` has no `env=` parameter (it always shells out through the
    CURRENT process's environment, unrestorable per-call), and `os.popen` is
    the same shell-out shape. Neither is used under `python/tan/` today; this
    fails loudly the day one is added, rather than silently letting a spawn
    class this gate cannot verify slip in unexamined."""
    banned = {"system", "popen", "spawnl", "spawnv", "spawnlp", "spawnvp"}
    hits: list[str] = []
    for path in sorted(TAN_ROOT.rglob("*.py")):
        rel = path.relative_to(PYTHON_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in banned
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
            ):
                hits.append(f"{rel}:{node.lineno}: os.{node.func.attr}(...)")
    assert not hits, (
        "these spawn calls cannot be verified by this gate at all -- route "
        "through subprocess + tan.core.subprocess_env.spawn_env instead:\n  "
        + "\n  ".join(hits)
    )
