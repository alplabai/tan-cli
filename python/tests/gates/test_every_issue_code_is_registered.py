# SPDX-License-Identifier: Apache-2.0
"""tan-cli#224: the Python emit-site gate the Rust one cannot stand in for.

`crates/tan-cli/tests/contract.rs` carries a PAIR of tests --
`every_emitted_issue_code_is_registered` (tan-cli#219: walks every literal
`code: "family.name"` in `crates/` and asserts it is in
`contract/issue-codes.json` at some status) and
`every_prefixed_issue_code_is_registered` (tan-cli#224 itself: a DECLARED
list of `PREFIXING_SITES`, because a code assembled as
`format!("bootstrap.{code}")` from a bare suffix never appears as one whole
literal and the first test structurally cannot see it). Both landed in
commit 78d8308.

`crates/` ships to NOBODY -- the release assets are PyInstaller freezes of
`python/tan` (tan-cli#271) -- so on the surface that actually reaches a
customer, NEITHER direction of this gate existed until this file. The
prefixing shape is not hypothetical on the Python side either:
`bootstrap_cmd.py`, `debug_config_cmd.py`, `doctor_cmd.py`, `sdk_cmd.py` and
`validate_cmd.py` all build a code the same way, and `deferred_cmd.py`'s
`cli.command-deferred` sat completely unregistered (assigned to a module
constant, never a whole literal at its `Issue(...)` call site) until the
audit this file's first run performed.

WHAT THIS COVERS -- WIDER than the Rust pair's own two shapes, by design, and
that widening is itself the product of a remediation: an earlier version of
this file claimed parity with the Rust pair's two shapes while actually
implementing a narrower one, which left ~40% of tan's emitted codes ungated
(caught in review, not by the gate -- the exact fail-open requirement 4 below
exists to prevent, applied to this file about itself):

  1. LITERAL sites -- `Issue("family.code", ...)`, `code="family.code"`
     anywhere, and `Issue(NAME, ...)` where `NAME` is a module-level constant
     assigned exactly that literal (`cli.command-deferred`'s actual shape).
  2. FULL-CODE-CARRYING CALL sites -- [`_FULL_CODE_CALLABLES`]: the port's
     DOMINANT emit idiom is not `Issue("family.code", ...)` directly but a
     per-command error TYPE (`BuildError`, `InitError`, `GenerateError`, ...)
     or a small local wrapper (`_issue`, `fail_sdk`, `_refuse`, `_error`, ...)
     constructed with the WHOLE literal code, later re-emitted through
     `Issue(err.code, ...)` several frames away. Shape (1) structurally
     cannot see the literal, because it never appears as `Issue(...)`'s own
     argument -- it appears at the CONSTRUCTOR call. Declared per `(file,
     callable name)` -> the positional index of the code argument, every call
     site scanned, exactly like shape (3) below scans a prefixing helper's
     call sites; a non-literal argument there is either a declared forward
     ([`_KNOWN_CODE_FORWARDS`], e.g. `except BuildError as err: ...
     Issue(err.code, ...)`, whose literal is captured at `err`'s OWN
     construction site) or reported UNRESOLVED, never silently dropped.
  3. PREFIXED / FAMILY sites -- an f-string whose ENTIRE value is one literal
     segment ending or starting with `.` plus exactly one substitution, in
     EITHER order: `f"bootstrap.{code}"` (fixed prefix, substituted suffix)
     or `f"{subcommand}.failed"` (substituted family, fixed suffix --
     `west_forward_cmd.py`'s mirrored shape). Auto-DISCOVERED across the
     whole `tan/` tree (unlike the Rust list, which is hand-declared per
     file) and then RESOLVED by scanning every call site of the one helper
     function/constructor the f-string's substitution comes from, mirroring
     `PREFIXING_SITES`'s "declared opener, scanned call sites, pinned count"
     shape one level more automatically.

Auto-discovery is the deliberate improvement over the Rust design for shape
(3): the Rust gate can only see a prefixing helper someone already added a
row for, so a FOURTH helper appearing elsewhere in `crates/` would escape
both of its own tests silently. Here, [`_prefix_templates`] finds every
"one fixed literal segment + one substitution" f-string in the tree by its
AST SHAPE, not by a hand-maintained file list, and
[`test_every_prefix_template_is_classified`] fails the moment one appears
that nothing below has classified -- so a fifth helper cannot hide the way a
fourth Rust one could. Shape (2) is declared rather than auto-discovered
(a bare `SomeClass("family.code", ...)` call has no AST feature that
distinguishes "this constructs a code-carrying error" from "this constructs
an unrelated value" the way a `f"prefix.{x}"` shape does), so it carries the
same non-vacuity discipline shape (3) does at the registration/test level
instead: [`test_every_emitted_issue_code_is_registered`]'s own count
(`len(literal) > 30`) would drop sharply if a `_FULL_CODE_CALLABLES` entry
silently stopped matching, the same tripwire `expected_calls` gives shape (3).

Classifying what auto-discovery FINDS still takes a human, in one of three
declared buckets, the same non-heuristic discipline
`crates/tan-cli/tests/contract.rs`'s own `PREFIXING_SITES` and
`DECLARED_FORWARDERS` comments insist on ("a scan that guessed which
functions prefix would either miss a new one silently or invent codes from
unrelated calls"):

  * `_RESOLVABLE_HELPERS`, keyed by `(file, lineno)` of the f-string itself --
    the substituted name is a plain parameter of the enclosing
    function/method (`kind="prefix"`, the fixed literal is a PREFIX) or,
    mirrored, a parameter of the function the f-string's SUBSTITUTED family
    comes from while the SUFFIX is fixed (`kind="family"`,
    `west_forward_cmd.py`'s `f"{subcommand}.failed"`) -- either way, every
    call site of that one function is scanned, and a literal argument there
    IS the missing half. Also covers a constructor whose call sites are
    scanned the same way even though the substitution is not literally the
    enclosing function's own parameter: `doctor_cmd.py`'s
    `f"doctor.{check.name}"` resolves by scanning every `Check(...)`
    construction's `name` (48 call sites, all literal -- MEASURED, not
    assumed, while closing tan-cli#224's own review). A call passing
    something else (a `Name`, an `Attribute`, a `Starred` unpack) is
    unresolved unless it also appears in `_FORWARDER_SUFFIXES`.
  * `_FORWARDER_SUFFIXES`, keyed by `(file, exact substituted expression)` --
    the substituted expression is not a plain parameter (`refusal.code`,
    `venv_refusal.code`, `result.outcome`, or a `*tuple` unpack) but its
    value space was read from the real source and is small and closed (a
    dataclass field fed by a handful of constructors, or an outcome derived
    from two module constants).
  * `_ACKNOWLEDGED_CEILINGS`, keyed by `(file, lineno)` -- stated rather than
    silently skipped, the same honesty the Rust gate's own "KNOWN CEILING"
    paragraph practises. Held EMPTY today: `doctor_cmd.py`'s
    `check.code or f"doctor.{check.name}"` ceiling this bucket used to carry
    was resolved into `_RESOLVABLE_HELPERS` above once its actual cost was
    measured (48 call sites, ALL literal, zero non-literal -- "a materially
    bigger audit" was the ceiling's original claim, and it did not survive
    contact with the real count). The bucket stays declared rather than
    deleted so a FUTURE genuinely-out-of-scope template has somewhere
    honest to go, per the same Rust "KNOWN CEILING" precedent -- an empty
    table is not evidence no ceiling will ever be needed again.

Shape (2) sites (`_FULL_CODE_CALLABLES`, see above) get the same declared, no
silent drop treatment through [`_KNOWN_CODE_FORWARDS`]: a code-position
argument (an `Issue(...)` first arg, any `code=` keyword, or a
`_FULL_CODE_CALLABLES` argument) that is neither a literal nor a resolved
module constant is either a declared forward -- its literal captured at the
ORIGIN this list points at, mirroring `crates/tan-cli/tests/contract.rs`'s
own `DECLARED_FORWARDERS` ("this list only says there is no literal HERE to
read, which is a fact about the call, not a licence to skip the code") -- or
reported UNRESOLVED by file:line, never silently dropped.

Every one of these buckets is a place a REAL escape can still happen if
mis-declared -- which is exactly why
[`test_gate_rejects_a_deliberately_unregistered_code`] exists: tan-cli#275 is
the standing lesson that an assertion nobody has ever seen fail is not proven
to fire. Confirmed by hand while writing this file: with the fabricated code
below removed from the injected set the assertion goes red; restored, green
-- see that test's own body for the same check run programmatically.
"""

from __future__ import annotations

import ast
import json
import pathlib

#: `contract/` lives at the repo root, one level above `python/` -- the same
#: resolution `test_frozen_issue_codes.py` uses, reused rather than
#: reinvented so there is exactly one place that path is computed.
REGISTRY = pathlib.Path(__file__).resolve().parents[3] / "contract" / "issue-codes.json"
TAN = pathlib.Path(__file__).resolve().parents[2] / "tan"


def _registered_codes() -> set[str]:
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    codes = {e["code"] for e in data["issueCodes"]}
    # Non-vacuity: an empty (or unreadable-as-expected) registry would make
    # every assertion below pass by finding nothing to fail against --
    # exactly the tan-cli#275 shape this whole file exists to avoid.
    assert codes, f"{REGISTRY} has no issue codes -- this gate would be vacuous"
    return codes


def _is_code_literal(s: str) -> bool:
    """Shaped like a whole `family.name` issue code: at least one dot,
    otherwise lowercase/digits/dash/dot only. Deliberately narrow, the same
    reason `crates/tan-cli/tests/contract.rs::emitted_code_literals` is
    narrow -- so an unrelated `code="UTF-8"`-shaped kwarg or a prose string
    can never be mistaken for a real code."""
    return bool(s) and "." in s and all(c.islower() or c.isdigit() or c in "-." for c in s)


def _is_code_suffix(s: str) -> bool:
    """Shaped like the bare SUFFIX a prefixing helper takes: no dot at all
    (a dot there would mean the caller already passed a whole code, which is
    a literal-emit site, not a prefixed one). Two shapes accepted: the
    kebab-case convention every HAND-WRITTEN suffix in this tree uses
    (`board-yaml-missing`), or a bare camelCase identifier for the one
    MECHANICALLY-resolved exception -- `doctor_cmd.py`'s `Check(...)` `name`s
    mirror the Rust oracle's own `doctor.<checkName>` convention verbatim
    (`checks_to_issues`'s own docstring says so), so `boardYaml`/`zephyrSdk`/
    ... are real suffixes this scan must accept, not reject as malformed."""
    if not s or "." in s:
        return False
    if all(c.islower() or c.isdigit() or c == "-" for c in s):
        return True
    return s[0].islower() and all(c.isalnum() for c in s)


def _is_family_prefix(s: str) -> bool:
    """Shaped like `"bootstrap."` -- lowercase/dash, exactly one trailing
    dot and no other. Guards [`_prefix_templates`] against an unrelated
    f-string (a path, a URL) that happens to end a literal segment in `.`."""
    return s.endswith(".") and s.count(".") == 1 and len(s) > 1 and all(c.islower() or c == "-" for c in s[:-1])


def _is_family_suffix(s: str) -> bool:
    """Shaped like `".failed"` -- the MIRROR of [`_is_family_prefix`]: one
    leading dot, then lowercase/dash, no other dot. Guards the "substituted
    FAMILY, fixed SUFFIX" template shape (`f"{subcommand}.failed"`,
    `west_forward_cmd.py`) against an unrelated f-string ending a literal
    segment in `.something` that is not a code suffix."""
    return s.startswith(".") and s.count(".") == 1 and len(s) > 1 and all(c.islower() or c == "-" for c in s[1:])


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))


def _rel(path: pathlib.Path) -> str:
    """`tan/commands/foo.py`, the same spelling used throughout this file's
    declared tables -- so a table entry can be found by grepping this file
    for the exact string that would appear in a failure message.

    Falls back to `path` unchanged for a path outside `tan/`: a pytest
    `tmp_path` self-test scratch file (`test_prefix_template_scan_finds_a_fresh_synthetic_site`)
    is scanned by `_literal_codes_in_file`, which now calls `_rel` to key its
    `_FULL_CODE_CALLABLES`/`_KNOWN_CODE_FORWARDS` lookups -- no declared table
    entry can ever match a path outside `tan/`, so the exact spelling does not
    matter there, but crashing on `relative_to` does."""
    try:
        return str(path.relative_to(TAN.parent)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal.with.a.dot"` assignments -- the
    `cli.command-deferred` shape (`DEFERRED_ISSUE_CODE` in
    `deferred_cmd.py`), where the whole code is named once and referenced by
    identifier at the `Issue(...)` call site rather than spelled inline.
    Deliberately shallow: only a direct top-level `Assign` to a `Name`
    counts, so a value reassigned or computed elsewhere is correctly left
    unresolved rather than guessed at."""
    consts: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and _is_code_literal(node.value.value)
        ):
            consts[node.targets[0].id] = node.value.value
    return consts


#: `(file, callable name)` -> the positional index of the argument that
#: carries an ALREADY-WHOLE issue code, for constructors/local helpers whose
#: declared purpose is exactly that (not a bare suffix needing a prefix --
#: `_RESOLVABLE_HELPERS` below covers that shape). See the module docstring's
#: shape (2): the port's dominant emit idiom is a per-command error TYPE
#: (`BuildError`, `InitError`, ...) or a small local wrapper (`_issue`,
#: `fail_sdk`, `_refuse`, `_error`, `_error_outcome`, `_Notice`) constructed
#: with the whole code, later re-emitted through `Issue(err.code, ...)` --
#: several call frames from the eventual `Issue(...)` shape (1) alone can see.
#: Every entry here was read from source (a `grep` for the class/function
#: name, then its `__init__`/signature, then every call site), the same
#: discipline `_RESOLVABLE_HELPERS`/`_FORWARDER_SUFFIXES` already apply.
_FULL_CODE_CALLABLES: dict[tuple[str, str], int] = {
    ("tan/core/build_plan.py", "PlanParseError"): 0,
    ("tan/commands/monitor_cmd.py", "MonitorError"): 0,
    ("tan/commands/explain_cmd.py", "ExplainError"): 0,
    ("tan/commands/generate_cmd.py", "GenerateError"): 0,
    ("tan/commands/build/token_substitution.py", "TokenSubstitutionError"): 0,
    ("tan/commands/build_cmd.py", "BuildError"): 0,
    ("tan/commands/build_cmd.py", "_refuse"): 0,
    ("tan/commands/build/materialise.py", "MaterialiseError"): 0,
    ("tan/commands/model_cmd.py", "ModelError"): 0,
    ("tan/commands/kconfig_cmd.py", "_CoreResolutionError"): 0,
    ("tan/commands/init_cmd.py", "InitError"): 0,
    ("tan/commands/renode_cmd.py", "_issue"): 0,
    ("tan/commands/renode_cmd.py", "fail"): 0,
    ("tan/commands/renode_cmd.py", "fail_sdk"): 0,
    ("tan/commands/flash_cmd.py", "_error"): 1,
    ("tan/commands/image_cmd.py", "_error_outcome"): 3,
    ("tan/commands/image_cmd.py", "_Notice"): 0,
    ("tan/commands/size_cmd.py", "_error_outcome"): 2,
}

#: `(file, exact unparsed expression)` -> declared as a KNOWN forward, never
#: a silent skip -- mirrors `crates/tan-cli/tests/contract.rs`'s own
#: `DECLARED_FORWARDERS` ("this list only says there is no literal HERE to
#: read, which is a fact about the call, not a licence to skip the code").
#: Applies at every code-position argument this file inspects: `Issue(...)`'s
#: first arg, any `code=` keyword, and every `_FULL_CODE_CALLABLES` argument.
#: Every entry's literal IS captured elsewhere in this same scan: an
#: `except <X>Error as err:` block re-emitting `err.code` (`<X>Error` is
#: itself in `_FULL_CODE_CALLABLES`, so its OWN construction sites carry the
#: literal), or a module constant imported from another file (`deferred_cmd
#: .py`'s `DEFERRED_ISSUE_CODE`, resolved by `_module_string_constants` only
#: at ITS OWN definition site -- deliberately shallow, per that function's own
#: docstring -- so the cross-module import here needs its own declared entry).
_KNOWN_CODE_FORWARDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("tan/commands/build_cmd.py", "err.code"),  # BuildError <- PlanParseError/TokenSubstitutionError
        ("tan/commands/build_cmd.py", "DEFERRED_ISSUE_CODE"),  # imported from deferred_cmd.py
        ("tan/commands/build_cmd.py", "code"),  # `Issue(code, ...)` inside `_refuse`'s OWN body,
        # forwarding ITS OWN `code` parameter -- `_refuse` is itself in
        # `_FULL_CODE_CALLABLES`, so its call sites carry the literal.
        ("tan/commands/monitor_cmd.py", "err.code"),  # <- MonitorError
        ("tan/commands/init_cmd.py", "err.code"),  # <- InitError
        ("tan/commands/model_cmd.py", "err.code"),  # <- ModelError
        ("tan/commands/explain_cmd.py", "err.code"),  # <- ExplainError
        ("tan/commands/run_cmd.py", "err.code"),  # <- BuildError (run retags build's own refusal)
        ("tan/commands/generate_cmd.py", "err.code"),  # <- GenerateError
        ("tan/commands/kconfig_cmd.py", "err.code"),  # <- _CoreResolutionError (via `code=err.code`)
        ("tan/commands/kconfig_cmd.py", "code"),  # `Issue(code, ...)` inside `_fail`'s OWN body --
        # `_fail`'s literal `code=` call sites are already caught by the plain
        # `code=` keyword scan above; this is only its internal forward.
        ("tan/commands/flash_cmd.py", "code"),  # `Issue(code, ...)` inside `_error`'s OWN body --
        # `_error` is itself in `_FULL_CODE_CALLABLES`.
        ("tan/commands/image_cmd.py", "n.code"),  # <- _Notice, one per bundle-assembly gap
        ("tan/commands/image_cmd.py", "code"),  # `Issue(code, ...)` inside `_error_outcome`'s OWN
        # body -- `_error_outcome` is itself in `_FULL_CODE_CALLABLES`.
        ("tan/commands/size_cmd.py", "code"),  # same shape, `size_cmd.py`'s own `_error_outcome`.
        ("tan/commands/renode_cmd.py", "code"),  # `_issue(code, ...)` inside `fail`/`fail_sdk`'s OWN
        # bodies, forwarding THEIR OWN `code` parameter -- `fail`/`fail_sdk`
        # are themselves in `_FULL_CODE_CALLABLES`, so their call sites carry it.
    }
)


def _resolve_code_value(
    rel: str, value: ast.expr | None, consts: dict[str, str]
) -> tuple[str, str | None]:
    """Classify one code-position argument. Returns `(status, payload)`:

    * `("literal", code)` -- `value` is a resolvable code literal (a
      `Constant` shaped like a whole `family.name` code, or a known
      module-string-constant `Name`).
    * `("ignored", None)` -- `value` is owned by a DIFFERENT, already-asserted
      mechanism, so reporting it here would duplicate that mechanism's own
      check rather than add coverage: a `Constant` string that is NOT
      code-shaped (no dot -- a bare SUFFIX literal, e.g. `_fail(code="not-
      ported", ...)`, which `_RESOLVABLE_HELPERS`/`_resolve_helper` scans
      these exact call sites for separately), or an `ast.JoinedStr`/
      `ast.BoolOp` (an f-string or `x or f"..."` -- the family/prefix-
      template shape `_prefix_templates`/`_classify_and_resolve` owns, with
      its own unresolved/unclassified assertions).
    * `("forward", None)` -- `(rel, unparsed value)` is declared in
      `_KNOWN_CODE_FORWARDS` -- deliberately skipped, the code is captured at
      its own origin.
    * `("unresolved", unparsed value)` -- none of the above: a real escape,
      reported by file:line, never silently dropped.
    """
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        if _is_code_literal(value.value):
            return "literal", value.value
        return "ignored", None
    if isinstance(value, ast.Name) and value.id in consts:
        return "literal", consts[value.id]
    if isinstance(value, (ast.JoinedStr, ast.BoolOp)):
        return "ignored", None
    expr = ast.unparse(value) if value is not None else "<missing argument>"
    if (rel, expr) in _KNOWN_CODE_FORWARDS:
        return "forward", None
    return "unresolved", expr


def _literal_codes_in_file(path: pathlib.Path) -> tuple[set[str], list[str]]:
    """Every LITERAL whole-code emit site in one file, plus every
    code-position argument that could NOT be resolved (never silently
    dropped -- tan-cli#224's own review finding). Three shapes:
    `Issue("family.code", ...)` (first positional arg, resolving a module
    constant when the arg is a bare `Name`), `code="family.code"` (a keyword
    named `code`, wherever it appears -- deliberately not scoped to any one
    callee, the same breadth `contract.rs`'s `code: "..."` text scan has),
    and a call to any `_FULL_CODE_CALLABLES` entry declared for THIS file."""
    rel = _rel(path)
    tree = _parse(path)
    consts = _module_string_constants(tree)
    callables_here = {name: idx for (f, name), idx in _FULL_CODE_CALLABLES.items() if f == rel}
    found: set[str] = set()
    unresolved: list[str] = []

    def _record(lineno: int, site: str, value: ast.expr | None) -> None:
        status, payload = _resolve_code_value(rel, value, consts)
        if status == "literal":
            assert payload is not None
            found.add(payload)
        elif status == "unresolved":
            unresolved.append(f"{rel}:{lineno} -- {site} argument is not a resolvable code literal ({payload})")
        # "forward" / "ignored": declared safe or owned elsewhere -- nothing to record.

    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "code":
            _record(node.lineno, "a `code=` keyword", node.value)
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "Issue" and node.args:
                _record(node.lineno, "`Issue(...)`'s first", node.args[0])
                continue
            if node.func.id in callables_here:
                idx = callables_here[node.func.id]
                arg = node.args[idx] if len(node.args) > idx else None
                _record(node.lineno, f"`{node.func.id}(...)`'s (declared in _FULL_CODE_CALLABLES)", arg)
                continue
    return found, unresolved


def _prefix_templates(path: pathlib.Path) -> list[tuple[int, str, str, str]]:
    """Every f-string used AT A CODE POSITION in `path` -- `Issue(...)`'s
    first argument, or a `code=` keyword's value, the SAME two positions
    [`_literal_codes_in_file`] inspects -- shaped EXACTLY `[one fixed literal
    segment ending/starting with `.`, ONE substitution]`, in either order --
    e.g. `f"bootstrap.{code}"` (`kind="prefix"`) or `f"{subcommand}.failed"`
    (`kind="family"`). Returns `(lineno, literal segment, unparsed
    substituted expression, kind)`.

    Scoping to code positions (rather than "any f-string in the file") is
    load-bearing, not cosmetic: this tree has MANY unrelated f-strings
    sharing the bare dot-suffix SHAPE -- `f"{sku}.yaml"`, `f"{tool}.exe"`,
    `f"{field}.path"` -- that `_is_family_suffix` alone cannot distinguish
    from a real code-assembling template (unlike `_is_family_prefix`'s
    multi-char prefixes, which happen not to collide with anything else in
    this tree today). `_is_family_prefix`/`_is_family_suffix` narrow the
    SHAPE; scoping to code positions narrows WHERE that shape is even
    looked for -- caught by measurement while closing tan-cli#224's own
    review (28 false positives from an unscoped scan, none of them a real
    issue-code template).

    Walks the WHOLE subtree of each code-position expression (not just its
    top level), so a JoinedStr nested one level in -- `doctor_cmd.py`'s
    `check.code or f"doctor.{check.name}"`, a `BoolOp` -- is still found.
    """
    tree = _parse(path)
    out: list[tuple[int, str, str, str]] = []

    def _scan(value: ast.expr | None) -> None:
        if value is None:
            return
        for node in ast.walk(value):
            if not isinstance(node, ast.JoinedStr) or len(node.values) != 2:
                continue
            head, tail = node.values
            if (
                isinstance(head, ast.FormattedValue)
                and isinstance(tail, ast.Constant)
                and isinstance(tail.value, str)
                and _is_family_suffix(tail.value)
            ):
                out.append((node.lineno, tail.value, ast.unparse(head.value), "family"))
                continue
            if (
                isinstance(head, ast.Constant)
                and isinstance(head.value, str)
                and _is_family_prefix(head.value)
                and isinstance(tail, ast.FormattedValue)
            ):
                out.append((node.lineno, head.value, ast.unparse(tail.value), "prefix"))

    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "code":
            _scan(node.value)
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Issue" and node.args:
            _scan(node.args[0])
    return out


# ---------------------------------------------------------------------------
# The declared classification of every prefix template this tree contains
# today (tan-cli#224). See the module docstring for what each bucket means
# and why a fourth is deliberately not offered.
# ---------------------------------------------------------------------------

#: The exact number of `_prefix_templates` matches across the whole `tan/`
#: tree, TODAY. Pinned exactly, the same reason
#: `crates/tan-cli/tests/contract.rs`'s `PREFIXED_CODE_COUNT` and per-file
#: `expected_sites` are pinned rather than floored: a template silently
#: disappearing (a rename that stops covering an emit) is exactly as real a
#: defect as a new one silently appearing uncovered, and only an EXACT count
#: notices the first case.
EXPECTED_TEMPLATE_COUNT = 11

#: `(file, lineno-of-the-f-string)` -> how to recover the missing half, for
#: every template resolvable by scanning one declared callable's call sites.
#: Keyed on the f-string's own line (not `(file, prefix, expr)`) because
#: `bootstrap_cmd.py` has TWO distinct such templates that both happen to
#: substitute a parameter named `code` -- (file, prefix, expr) alone cannot
#: tell them apart. `kind` (default `"prefix"`) picks which half is fixed:
#: `"prefix"` -- `prefix` is the fixed literal, `expr`'s call-site argument is
#: the SUFFIX (`f"bootstrap.{code}"`, `bootstrap.` + scanned code); `"family"`
#: -- `suffix` is the fixed literal, `expr`'s call-site argument is the FAMILY
#: (`f"{subcommand}.failed"`, scanned subcommand + `.failed`). Either way the
#: named callable's call sites are scanned the same way (`name`/`attr`,
#: `arg_index`/`arg_keyword`) -- a constructor whose call sites carry the
#: missing half works exactly like a helper whose OWN parameter does
#: (`doctor_cmd.py`'s `Check(name=...)` below is a constructor, not the
#: f-string's enclosing function; `_resolve_helper` does not care which).
_RESOLVABLE_HELPERS: dict[tuple[str, int], dict] = {
    ("tan/commands/bootstrap_cmd.py", 271): dict(
        # `Log.warn(self, code, message)`, drained by `take_issues` into
        # this exact f-string -- every call site is `<log>.warn(...)`.
        prefix="bootstrap.",
        expr="code",
        attr="warn",
        arg_index=0,
        expected_calls=16,
    ),
    ("tan/commands/bootstrap_cmd.py", 1522): dict(
        prefix="bootstrap.",
        expr="code",
        name="_refusal",
        arg_index=1,
        expected_calls=8,
    ),
    ("tan/commands/debug_config_cmd.py", 749): dict(
        prefix="debug-config.",
        expr="code",
        name="_failure",
        arg_keyword="code",
        expected_calls=2,
    ),
    ("tan/commands/sdk_cmd.py", 779): dict(
        prefix="sdk.",
        expr="code",
        name="_fail",
        arg_keyword="code",
        expected_calls=5,
    ),
    ("tan/commands/validate_cmd.py", 485): dict(
        prefix="validate.",
        expr="code",
        name="fail",
        arg_index=0,
        expected_calls=5,
    ),
    ("tan/commands/doctor_cmd.py", 1706): dict(
        # `check.code or f"doctor.{check.name}"` in `checks_to_issues()` --
        # the ceiling this bucket used to acknowledge instead of resolving
        # (tan-cli#224 review): MEASURED at 48 `Check(...)` constructions,
        # every one passing its `name` positionally and literally, zero
        # non-literal, 20 distinct camelCase names (`_is_code_suffix` admits
        # camelCase for exactly this reason -- see its own docstring).
        prefix="doctor.",
        expr="check.name",
        name="Check",
        arg_index=0,
        expected_calls=48,
    ),
    ("tan/commands/west_forward_cmd.py", 124): dict(
        # `Issue(f"{subcommand}.failed", ...)` -- the MIRRORED shape
        # (tan-cli#224 review): `subcommand` is `_run_forward`'s own
        # parameter, closed to the three literal strings its three Typer
        # callers (`migrate`/`lock`/`quality`) pass.
        kind="family",
        suffix=".failed",
        expr="subcommand",
        name="_run_forward",
        arg_index=0,
        expected_calls=3,
    ),
    ("tan/commands/west_forward_cmd.py", 133): dict(
        kind="family",
        suffix=".failed",
        expr="subcommand",
        name="_run_forward",
        arg_index=0,
        expected_calls=3,
    ),
}

#: `(file, exact substituted expression text)` -> the closed, source-verified
#: set of suffixes a FORWARDED expression can carry. Every entry here was
#: read from the origin, not guessed -- see the module docstring's bucket
#: description. `warn(*skew)`/`warn(*ceiling)` are keyed by the call shape
#: rather than the bare unparsed name, because a `Starred` argument is not a
#: template substitution at all -- it is resolved per CALL SITE inside
#: `_resolve_helper`, not per f-string.
_FORWARDER_SUFFIXES: dict[tuple[str, str], frozenset[str]] = {
    # `Issue(f"bootstrap.{refusal.code}", ...)` at bootstrap_cmd.py:2072
    # forwards `check_prerequisites()`'s `PrereqFailure.code`
    # (`tan/core/bootstrap.py`), which is exactly one of these four literals
    # depending on which refusal branch it returned.
    ("tan/commands/bootstrap_cmd.py", "refusal.code"): frozenset(
        {"prerequisites-missing", "python-not-runnable", "python-too-old", "venv-unusable"}
    ),
    # `code=f"bootstrap.{venv_refusal.code}"` at doctor_cmd.py:662 forwards
    # the SAME `PrereqFailure`, but `venv_refusal` there is only ever set
    # from `posix_venv_unusable()` (doctor_cmd.py:2342) -- a strictly
    # narrower value space than the bootstrap_cmd.py forward above.
    ("tan/commands/doctor_cmd.py", "venv_refusal.code"): frozenset({"venv-unusable"}),
    # `Issue(f"validate.{result.outcome}", ...)` at validate_cmd.py:546 only
    # ever fires inside `for message in result.messages`, and
    # `outcome = OUTCOME_CLEAN if not messages else OUTCOME_SCHEMA_VIOLATION`
    # (validate_cmd.py:272) means a non-empty `messages` implies
    # `outcome == OUTCOME_SCHEMA_VIOLATION == "schema-violation"` always.
    ("tan/commands/validate_cmd.py", "result.outcome"): frozenset({"schema-violation"}),
    # `log.warn(*skew)` / `log.warn(*ceiling)` at bootstrap_cmd.py:2061/:2326
    # unpack the `(suffix, message)` pairs `python_floor_skew_warning()` and
    # `python_ceiling_warning()` (`tan/core/bootstrap.py`) return.
    ("tan/commands/bootstrap_cmd.py", "warn(*skew)"): frozenset({"python-floor-skew"}),
    ("tan/commands/bootstrap_cmd.py", "warn(*ceiling)"): frozenset({"python-newer-than-verified"}),
}

#: `(file, lineno)` -> why this template is deliberately NOT resolved here,
#: stated rather than silently absent (matching `contract.rs`'s own "KNOWN
#: CEILING" paragraph). See the module docstring's third bucket. Held EMPTY
#: today: `doctor_cmd.py`'s `check.code or f"doctor.{check.name}"` was the
#: sole entry until tan-cli#224's own review measured its actual cost (48
#: `Check(...)` call sites, ALL literal) and found it resolvable, not a real
#: ceiling -- it moved to `_RESOLVABLE_HELPERS` above. The bucket stays
#: declared, not deleted, so a genuinely out-of-scope FUTURE template has
#: somewhere honest to go rather than forcing a false resolution.
_ACKNOWLEDGED_CEILINGS: dict[tuple[str, int], str] = {}


def _calls_matching(tree: ast.Module, *, attr: str | None = None, name: str | None = None) -> list[ast.Call]:
    """Every `ast.Call` whose callee is `<anything>.attr(` (when `attr` is
    given) or a bare `name(` (when `name` is given)."""
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if attr is not None and isinstance(func, ast.Attribute) and func.attr == attr:
            out.append(node)
        elif name is not None and isinstance(func, ast.Name) and func.id == name:
            out.append(node)
    return out


def _resolve_helper(
    path: pathlib.Path,
    *,
    kind: str = "prefix",
    prefix: str | None = None,
    suffix: str | None = None,
    attr: str | None = None,
    name: str | None = None,
    arg_index: int | None = None,
    arg_keyword: str | None = None,
    expected_calls: int,
) -> tuple[set[str], list[str]]:
    """Scan every call to the declared helper in `path`, read the code
    argument at `arg_index` (positional) or `arg_keyword`, and return
    `(reconstructed codes, unresolved-descriptions)`. `kind="prefix"`
    (default) reconstructs `prefix + <scanned suffix>`; `kind="family"`
    reconstructs `<scanned family> + suffix` (`west_forward_cmd.py`'s
    mirrored shape).

    `expected_calls` is asserted EXACTLY, mirroring `PREFIXING_SITES`'s own
    pinned per-file counts (`contract.rs:663-682`) for the identical reason:
    a floor lets a call site disappear unnoticed as long as enough others
    remain to clear it.

    A `Starred` argument (`log.warn(*skew)`, unpacking a 2-tuple rather than
    passing the code positionally) is looked up in `_FORWARDER_SUFFIXES` by
    `f"{opener}(*{expr})"`; anything else non-literal is reported UNRESOLVED
    -- never a silent skip.
    """
    opener = attr or name
    rel = _rel(path)
    tree = _parse(path)
    calls = _calls_matching(tree, attr=attr, name=name)
    assert len(calls) == expected_calls, (
        f"{rel}: expected {expected_calls} call(s) to {opener}(...), found "
        f"{len(calls)} -- a prefixing call site was ADDED (register its code, "
        f"then bump this count) or REMOVED (a rename silently stopped this gate "
        f"covering an emit). See _resolve_helper's docstring."
    )
    parts: set[str] = set()
    unresolved: list[str] = []
    for call in calls:
        arg: ast.expr | None
        if arg_keyword is not None:
            arg = next((kw.value for kw in call.keywords if kw.arg == arg_keyword), None)
        elif arg_index is not None and len(call.args) > arg_index:
            arg = call.args[arg_index]
        else:
            arg = None

        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and _is_code_suffix(arg.value):
            parts.add(arg.value)
            continue
        if isinstance(arg, ast.Starred):
            key = (rel, f"{opener}(*{ast.unparse(arg.value)})")
            declared = _FORWARDER_SUFFIXES.get(key)
            if declared is None:
                unresolved.append(
                    f"{rel}:{call.lineno} -- `{opener}(*{ast.unparse(arg.value)})` unpacks a "
                    f"tuple this scan cannot read a suffix from directly. Add {key!r} to "
                    f"_FORWARDER_SUFFIXES with the known suffix set, read from source."
                )
            else:
                parts |= declared
            continue
        got = ast.unparse(arg) if arg is not None else "no matching argument"
        unresolved.append(
            f"{rel}:{call.lineno} -- `{opener}(...)`'s code argument is not a literal "
            f"({got}). Either pass a literal suffix, or if it forwards a code from "
            f"elsewhere, resolve the value space by hand and add it to "
            f"_FORWARDER_SUFFIXES."
        )
    if kind == "prefix":
        assert prefix is not None
        return {prefix + s for s in parts}, unresolved
    assert suffix is not None
    return {s + suffix for s in parts}, unresolved


def _classify_and_resolve(
    templates: dict[str, list[tuple[int, str, str, str]]],
) -> tuple[set[str], list[str], list[str]]:
    """Walk every discovered template, classify it into one of the three
    declared buckets, and resolve the ones that are classified. Returns
    `(reconstructed codes, unresolved call sites, unclassified templates)`.
    """
    codes: set[str] = set()
    unresolved: list[str] = []
    unclassified: list[str] = []
    seen_helper_keys: set[tuple[str, int]] = set()

    for rel, sites in templates.items():
        for lineno, literal, expr, kind in sites:
            key = (rel, lineno)
            if key in _ACKNOWLEDGED_CEILINGS:
                continue
            if key in _RESOLVABLE_HELPERS:
                spec = _RESOLVABLE_HELPERS[key]
                spec_kind = spec.get("kind", "prefix")
                assert spec_kind == kind, (
                    f"{rel}:{lineno} -- _RESOLVABLE_HELPERS declared kind={spec_kind!r} but "
                    f"the template now reads kind={kind!r}; the f-string changed shape -- "
                    f"update the declaration."
                )
                declared_literal = spec["prefix"] if kind == "prefix" else spec["suffix"]
                assert declared_literal == literal and spec["expr"] == expr, (
                    f"{rel}:{lineno} -- _RESOLVABLE_HELPERS declared literal={declared_literal!r} "
                    f"expr={spec['expr']!r} but the template now reads literal={literal!r} "
                    f"expr={expr!r}; the f-string changed shape -- update the declaration."
                )
                seen_helper_keys.add(key)
                continue
            fwd = _FORWARDER_SUFFIXES.get((rel, expr))
            if fwd is not None:
                codes |= {literal + s for s in fwd} if kind == "prefix" else {s + literal for s in fwd}
                continue
            shape = f'f"{literal}{{{expr}}}"' if kind == "prefix" else f'f"{{{expr}}}{literal}"'
            unclassified.append(
                f"{rel}:{lineno} -- new prefix template {shape} is not in "
                f"_RESOLVABLE_HELPERS, _FORWARDER_SUFFIXES or _ACKNOWLEDGED_CEILINGS. "
                f"Classify it in one of the three (see this file's module docstring)."
            )

    missing_helpers = sorted(set(_RESOLVABLE_HELPERS) - seen_helper_keys)
    if missing_helpers:
        unclassified.append(
            f"_RESOLVABLE_HELPERS declares template(s) _prefix_templates no longer finds: "
            f"{missing_helpers} -- the f-string moved, was rewritten, or was removed; "
            f"update the declaration (and EXPECTED_TEMPLATE_COUNT)."
        )

    for key, spec in _RESOLVABLE_HELPERS.items():
        rel = key[0]
        site_codes, site_unresolved = _resolve_helper(
            TAN.parent / rel,
            kind=spec.get("kind", "prefix"),
            prefix=spec.get("prefix"),
            suffix=spec.get("suffix"),
            attr=spec.get("attr"),
            name=spec.get("name"),
            arg_index=spec.get("arg_index"),
            arg_keyword=spec.get("arg_keyword"),
            expected_calls=spec["expected_calls"],
        )
        codes |= site_codes
        unresolved.extend(site_unresolved)

    return codes, unresolved, unclassified


def _all_prefix_templates() -> dict[str, list[tuple[int, str, str, str]]]:
    return {_rel(path): _prefix_templates(path) for path in sorted(TAN.rglob("*.py"))}


def _all_literal_codes() -> tuple[dict[str, list[str]], list[str]]:
    """`(code -> the files it was found in, every unresolved code-position
    site across the whole tree)` -- never silently dropped, see
    `_literal_codes_in_file`."""
    found: dict[str, list[str]] = {}
    unresolved: list[str] = []
    for path in sorted(TAN.rglob("*.py")):
        codes, site_unresolved = _literal_codes_in_file(path)
        for code in codes:
            found.setdefault(code, []).append(_rel(path))
        unresolved.extend(site_unresolved)
    return found, unresolved


def _missing(emitted: set[str], registered: set[str]) -> list[str]:
    """The pure diff both the real gate and its self-test below share, so the
    self-test exercises the SAME comparison the real assertion makes rather
    than a reimplementation of it."""
    return sorted(emitted - registered)


def test_every_prefix_template_is_classified():
    """Non-vacuity + drift pin for auto-discovery itself (tan-cli#224): the
    template COUNT is exact, and every template found must resolve to one of
    the three declared buckets. A template appearing with none of the three
    is the exact hole this file exists to close for a FUTURE prefixing
    helper, the same way #224 (and its own review remediation) closed it for
    the ones that already existed.
    """
    templates = _all_prefix_templates()
    total = sum(len(sites) for sites in templates.values())
    found_lines = "\n".join(
        (f'  {rel}:{lineno} f"{literal}{{{expr}}}"' if kind == "prefix" else f'  {rel}:{lineno} f"{{{expr}}}{literal}"')
        for rel, sites in templates.items()
        for lineno, literal, expr, kind in sites
    )
    assert total == EXPECTED_TEMPLATE_COUNT, (
        f'found {total} f-string prefix templates (`f"family.{{code}}"` shape) '
        f"across tan/, expected {EXPECTED_TEMPLATE_COUNT}. Fewer means one was "
        f"rewritten (update the count AND check whether a _RESOLVABLE_HELPERS / "
        f"_FORWARDER_SUFFIXES / _ACKNOWLEDGED_CEILINGS entry is now stale); more "
        f"means a NEW prefixing helper appeared -- classify it in one of the "
        f"three buckets (see this file's module docstring), then bump this "
        f"count. Found:\n{found_lines}"
    )
    _, _, unclassified = _classify_and_resolve(templates)
    assert not unclassified, "Unclassified prefix template(s):\n  " + "\n  ".join(unclassified)


def test_every_emitted_issue_code_is_registered():
    """The pair `crates/tan-cli/tests/contract.rs::every_emitted_issue_code_is_registered`
    + `::every_prefixed_issue_code_is_registered` ported to the surface that
    actually ships (tan-cli#224) -- see the module docstring for the full
    design. LITERAL codes and PREFIXED codes are both required to appear in
    `contract/issue-codes.json` at some status.
    """
    registered = _registered_codes()

    literal, literal_unresolved = _all_literal_codes()
    # Never a silent drop (tan-cli#224's own review finding): a code-position
    # argument this scan cannot resolve to a literal, and that is not a
    # declared forward, is reported by file:line -- not quietly absent from
    # `literal` with no trace.
    assert not literal_unresolved, (
        f"{len(literal_unresolved)} code-position argument(s) could not be resolved to a "
        "literal and are not declared in _KNOWN_CODE_FORWARDS or _FULL_CODE_CALLABLES:\n  "
        + "\n  ".join(literal_unresolved)
    )
    # Non-vacuity: a scanner that silently matched nothing would pass this
    # gate while checking nothing at all -- the tan-cli#219 failure mode.
    assert len(literal) > 30, (
        f"found only {len(literal)} literal issue codes across tan/ -- the scan is "
        f"broken, and a broken scan makes this gate vacuous"
    )

    templates = _all_prefix_templates()
    prefixed, unresolved, unclassified = _classify_and_resolve(templates)
    assert not unclassified, (
        "Unclassified prefix template(s) -- see test_every_prefix_template_is_classified:\n  "
        + "\n  ".join(unclassified)
    )
    assert not unresolved, (
        f"{len(unresolved)} prefixed emit site(s) could not be resolved:\n  " + "\n  ".join(unresolved)
    )

    emitted = set(literal) | prefixed
    missing = _missing(emitted, registered)
    assert not missing, (
        f"{len(missing)} issue code(s) are emitted by python/tan but appear in "
        "contract/issue-codes.json at NO status:\n"
        + "\n".join(
            f"  {c}  (sites: {', '.join(literal.get(c, ['assembled by a prefixing helper']))})" for c in missing
        )
        + "\n\nAn unregistered code is ungated on both sides of the seam at once: this "
        "repo's registry-driven checks never see it, and the published "
        "envelope-contract.json is built from that same registry, so alp-sdk-vscode "
        'cannot see it either. Add each one with "status": "reserved" and '
        '"consumer": "none" -- that costs nothing, since a reserved code may '
        'still be renamed freely. Use "frozen" ONLY once a consumer actually binds '
        "to it."
    )


def test_gate_rejects_a_deliberately_unregistered_code():
    """tan-cli#275's own lesson, applied to this gate specifically: an
    assertion nobody has ever watched fail is not proven to fire. This test
    is that watch -- it exercises the SAME `_missing` comparison
    `test_every_emitted_issue_code_is_registered` makes, against the REAL
    registry, with one fabricated code injected into the emitted set.

    Manually verified both ways while writing this file (not just asserted
    here): with the fabricated code removed from the injected set the
    assertion below fails (`AssertionError`), and restored it passes -- so
    this genuinely exercises the failure path, not a tautology that can
    never go red.
    """
    registered = _registered_codes()
    real_emitted = set(_all_literal_codes()[0])
    # Not a real code -- guaranteed absent from the registry both by
    # construction (this spelling names itself as one) and because a real
    # code always has a plausible family; "zzz-tan-cli-224-self-test" is
    # neither.
    fabricated = "zzz-tan-cli-224-self-test.never-registered"
    assert fabricated not in registered, "the fabricated self-test code collided with a real one -- pick another"

    injected = real_emitted | {fabricated}
    offenders = _missing(injected, registered)
    assert fabricated in offenders, (
        "the gate's own diff did not flag a deliberately unregistered code -- "
        "this gate cannot fail, which per tan-cli#275 means it is not a gate"
    )

    # And the negative: with nothing injected, that same fabricated spelling
    # must NOT appear -- proving the assertion is sensitive to the input,
    # not unconditionally red.
    offenders_clean = _missing(real_emitted, registered)
    assert fabricated not in offenders_clean


def test_prefix_template_scan_finds_a_fresh_synthetic_site(tmp_path: pathlib.Path):
    """A second self-test, at the AST-mechanics level rather than the
    registry-diff level: [`_prefix_templates`] and [`_literal_codes_in_file`]
    are exercised here against SYNTHETIC source containing codes neither has
    ever seen. Proves the SCANNER notices a new site, not just that the diff
    logic notices a missing registration.

    Written under pytest's own `tmp_path`, NOT under `TAN` (tan-cli#224
    review): `_literal_codes_in_file`/`_prefix_templates` take an arbitrary
    path (`_rel` falls back to the path unchanged outside `tan/`, see its own
    docstring), so nothing here needs to live inside the real package -- and a
    scratch file that DID would be one interrupted run away from surviving
    into `python/tan/` itself (a stray file in the very tree every other test
    in this file globs with `TAN.rglob("*.py")`), the production-tree escape
    this fix closes.
    """
    synthetic = tmp_path / "selftest_scratch.py"
    synthetic.write_text(
        "from __future__ import annotations\n"
        "\n"
        'SELFTEST_CONST = "selftest.const-code"\n'
        "\n"
        "\n"
        "def emit_one(reg):\n"
        '    return Issue(SELFTEST_CONST, "error", "x")\n'
        "\n"
        "\n"
        "def emit_two(code):\n"
        '    return Issue(f"selftestfamily.{code}", "error", "x")\n'
        "\n"
        "\n"
        "def emit_three():\n"
        '    return Check("n", "fail", "d", code="selftest.kwarg-code")\n',
        encoding="utf-8",
    )
    literal, unresolved = _literal_codes_in_file(synthetic)
    assert literal == {"selftest.const-code", "selftest.kwarg-code"}, literal
    assert unresolved == [], unresolved

    templates = _prefix_templates(synthetic)
    assert templates == [(11, "selftestfamily.", "code", "prefix")], templates
