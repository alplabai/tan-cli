# SPDX-License-Identifier: Apache-2.0
"""tan-cli#224: the Python emit-site gate the Rust one could not stand in for
-- and, since tan-cli#269 deleted `crates/`, the only emit-site gate there is.

`crates/tan-cli/tests/contract.rs` carried a PAIR of tests --
`every_emitted_issue_code_is_registered` (tan-cli#219: walks every literal
`code: "family.name"` in `crates/` and asserts it is in
`contract/issue-codes.json` at some status) and
`every_prefixed_issue_code_is_registered` (tan-cli#224 itself: a DECLARED
list of `PREFIXING_SITES`, because a code assembled as
`format!("bootstrap.{code}")` from a bare suffix never appears as one whole
literal and the first test structurally cannot see it). Both landed in
commit 78d8308 and both are gone with the crate.

`crates/` shipped to NOBODY -- the release assets are PyInstaller freezes of
`python/tan` (tan-cli#271) -- so on the surface that actually reaches a
customer, NEITHER direction of this gate existed until this file. The
descriptions of the Rust pair kept below are the RECORD of what this file was
written against, not a claim that a second gate still runs. The
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

  * `_RESOLVABLE_HELPERS`, keyed by `(file, enclosing qualname)` -- the
    dotted name of the function/method the f-string ITSELF sits inside
    (`Log.take_issues`, `_refusal`, `validate.fail`), tracked by
    [`_prefix_templates`] while it walks. DELIBERATELY NOT the f-string's
    line number, which an earlier version of this table used and which broke
    for real, not hypothetically: dev's fc88ca1 shifted `_refusal`'s own
    template from :1522 to :1532 by adding lines earlier in the same file --
    changing nothing about `_refusal` itself -- and reddened this gate on an
    unrelated, already-merged PR. A qualname is immune to that: it only
    changes when the SITE itself is renamed, moved to a different function,
    or rewritten, all of which genuinely warrant updating this table. It
    still disambiguates every case a bare `(file, prefix, expr)` key could
    not: `bootstrap_cmd.py` has TWO distinct templates that both substitute a
    parameter literally named `code`, but one sits in `Log.take_issues` and
    the other in `_refusal`, so the qualname alone tells them apart. The
    substituted name is a plain parameter of the enclosing function/method
    (`kind="prefix"`, the fixed literal is a PREFIX) or, mirrored, a
    parameter of the function the f-string's SUBSTITUTED family comes from
    while the SUFFIX is fixed (`kind="family"`, `west_forward_cmd.py`'s
    `f"{subcommand}.failed"`) -- either way, every call site of that one
    function is scanned, and a literal argument there IS the missing half.
    Also covers a constructor whose call sites are scanned the same way even
    though the substitution is not literally the enclosing function's own
    parameter: `doctor_cmd.py`'s `f"doctor.{check.name}"` resolves by
    scanning every `Check(...)` construction's `name` (48 call sites, all
    literal -- MEASURED, not assumed, while closing tan-cli#224's own
    review). A call passing something else (a `Name`, an `Attribute`, a
    `Starred` unpack) is unresolved unless it also appears in
    `_FORWARDER_SUFFIXES`. Two templates that share one qualname
    (`west_forward_cmd.py`'s `_run_forward`, whose success and `OSError` arms
    both build the identical `f"{subcommand}.failed"`) collapse to ONE
    declared entry, not two -- they are the same resolvable site scanned
    once, not two coincidentally-identical declarations to keep in sync by
    hand. That collapse is exactly what a REVIEWER later showed was a hole,
    not just an economy: because the key is `(file, qualname)`, a THIRD,
    UNREGISTERED template landing inside an already-declared function is
    indistinguishable from the two legitimate ones by key alone, and the old
    code recorded only whether the key had been seen at all
    (`seen_helper_keys: set`), not how many times. Concretely: adding a
    second, shadowed-variable `f"bootstrap.{code}"` inside `_refusal` (a
    comprehension `for code in (...)` shadows the parameter `_refusal`
    already takes, so `ast.unparse` still reads the substituted expression as
    plain `code`, and the declared literal/`kind` still match) passed every
    existing assertion silently -- the only thing that caught it was
    `EXPECTED_TEMPLATE_COUNT`'s own failure message, which says "bump the
    count", not "this code is unregistered". Each `_RESOLVABLE_HELPERS`
    entry therefore also declares `sites`: the exact number of
    `_prefix_templates` matches expected at that key (`_run_forward`'s entry
    reads `sites=2`, recording the "two, not one, not three" fact the old
    code left implicit). [`_classify_and_resolve`] records every matched
    lineno per key and calls [`_check_site_counts`], which fails, naming the
    qualname, the declared vs. actual count, and every matched file:line, the
    moment they diverge -- so the shadowed-comprehension shape above is now a
    loud, specific failure ("_RESOLVABLE_HELPERS[...] declares sites=1 but
    this run found 2") instead of a nudge to bump an unrelated scalar.
  * `_FORWARDER_SUFFIXES`, keyed by `(file, exact substituted expression)` --
    the substituted expression is not a plain parameter (`refusal.code`,
    `venv_refusal.code`, `result.outcome`, or a `*tuple` unpack) but its
    value space was read from the real source and is small and closed (a
    dataclass field fed by a handful of constructors, or an outcome derived
    from two module constants). Carries the SAME `sites`-pinning discipline
    the other two buckets do, for the SAME many-to-one reason, closing a
    THIRD hole a reviewer found in this table specifically: the key is
    `(file, expr)` -- no qualname, no call-site scope -- so a wholly
    UNRELATED function whose own f-string happens to substitute an
    identically-spelled expression collapses onto the same declared entry
    and gets waved through by whatever suffix set that entry already
    carries, without a single new call site ever being named. Measured:
    adding `def _sneaky(refusal): return Issue(f"bootstrap.{refusal.code}",
    "error", "smuggled")` to `bootstrap_cmd.py` and bumping
    `EXPECTED_TEMPLATE_COUNT` 11 -> 12 gave "4 passed" -- every existing
    assertion, silently. `sites` is the exact count of matches expected at
    that key -- from [`_prefix_templates`]'s f-string scan for the three
    plain-expression entries, or from [`_resolve_helper`]'s own
    Starred-argument scan for the two `*tuple` entries -- asserted by the
    SAME [`_check_site_counts`] the other two buckets now share.
  * `_ACKNOWLEDGED_CEILINGS`, keyed by `(file, enclosing qualname)` -- the
    same stable identity `_RESOLVABLE_HELPERS` keys on, for the same reason
    (see its bullet above), mapping to `dict(reason=..., sites=...)` -- the
    same `sites`-pinning discipline `_RESOLVABLE_HELPERS` carries, for the
    same reason: an acknowledged ceiling is exactly as many-to-one a key as a
    resolved helper, so a SECOND, unregistered template landing at an
    already-acknowledged qualname deserves the same loud count mismatch, not
    a silent "well, it's acknowledged" pass. `reason` is stated rather than
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

ACKNOWLEDGED CEILING -- a limit of static resolution, documented rather than
chased closed (a reviewer's second finding, tan-cli#224 review). Every bucket
above resolves a template's substituted expression by reading its TEXT
(`ast.unparse`) and matching that text against the enclosing callable's own
parameter name or a declared forward -- it does not, and structurally cannot
without reimplementing Python's own name resolution, check that the text it
read is actually BOUND the way the enclosing signature implies. A construct
that locally REBINDS the substituted name defeats that match while leaving
every surface signal this file checks unchanged. Measured, not hypothetical:
`_refusal`'s legitimate site is

    [Issue(f"bootstrap.{code}", "error", " ".join(lines))]

where `code` is `_refusal`'s own parameter. Replacing it with

    [Issue(f"bootstrap.{code}", "error", " ".join(lines)) for code in ("sneaky-unregistered",)]

keeps `sites=1` (still exactly one `f"bootstrap.{code}"` AST node),
keeps `kind`/literal/`expr` all matching `_RESOLVABLE_HELPERS[("tan/commands
/bootstrap_cmd.py", "_refusal")]` (`ast.unparse` reads the substituted name
as the bare identifier `code` either way -- it has no notion of WHICH `code`
a comprehension's own scope binds, only that the token spells the same),
keeps `EXPECTED_TEMPLATE_COUNT` at 11 -- and the gate stays green while
`bootstrap.sneaky-unregistered` is emitted at runtime, unregistered.

WHAT THIS GATE DOES CATCH: every literal code, every `_FULL_CODE_CALLABLES`
site, and every prefix/family template whose substituted expression is
textually the identifier the matching declared spec says it is -- which is
every real site in this tree today (measured while writing this file: zero
unclassified, zero unresolved). WHAT IT STRUCTURALLY CANNOT: tell "this
occurrence of `code` is the enclosing function's OWN parameter" apart from
"this occurrence of `code` is a comprehension/`with`/`except`/nested-`def`
target that merely happens to share that spelling" -- that is a lexical-
scoping question, not an AST-shape one, and answering it for real means
building the same name-resolution pass CPython's own compiler does. Do NOT
add a heuristic that tries to detect local rebinding of a substituted name
-- shadowing a name is common, legitimate Python (this file's own `_scan`/
`_walk` helpers do it), so a detector for it would either miss a subtler
shadow just as easily or flag ordinary, harmless code as suspect; either
way it is an arms race against a static-analysis limit, not a fix for one.

THE ACTUAL BACKSTOP: this gate is a completeness REMINDER at review time,
not the last line of defence against an unregistered code reaching a
customer. The authority for what a consumer may rely on is
`contract/issue-codes.json` itself, and the release workflow's own "Bundle
the envelope contract" step (`.github/workflows/release.yml`) builds the
published `envelope-contract.json` directly FROM that registry file -- by
reading it, never by re-scanning `python/tan/` source -- so a code that
escapes this static scan is exactly as absent from the published contract as
a code nobody ever tried to register in the first place; this scan's ceiling
does not create a NEW way for that to happen, it just fails to add coverage
for one narrow shape of it. And on the wire, `alp-sdk-vscode`'s own `===`
match against a `frozen` code FAILS OPEN on anything it does not recognise
(`test_frozen_issue_codes.py`'s own docstring: "an unrecognised code is
indistinguishable from 'no problem' on the consumer side") -- so the
practical cost of this ceiling is a real problem going unsurfaced to a user
who hit it, not a crash, and the actual defence against that is a human
registering every new code in the one place (`contract/issue-codes.json`)
this gate, `test_frozen_issue_codes.py`, and the release step all read from.
"""

from __future__ import annotations

import ast
import json
import pathlib

from tan.commands.doctor_cmd import kebab_check_name

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
    # tan-cli#616: `faultdecode_cmd._refuse(code, message, *, envelope_mode)`
    # -- same shape and same 0th position as `build_cmd`'s namesake. Its two
    # call sites carry the literals (`faultdecode.no-registers`,
    # `faultdecode.invalid-register-value`), which is what this index resolves.
    ("tan/commands/faultdecode_cmd.py", "_refuse"): 0,
    ("tan/commands/build/materialise.py", "MaterialiseError"): 0,
    ("tan/commands/model_cmd.py", "ModelError"): 0,
    ("tan/commands/kconfig_cmd.py", "_CoreResolutionError"): 0,
    ("tan/commands/init_cmd.py", "InitError"): 0,
    ("tan/commands/flash_cmd.py", "_error"): 1,
    ("tan/commands/image_cmd.py", "_error_outcome"): 3,
    ("tan/commands/image_cmd.py", "_Notice"): 0,
    ("tan/commands/size_cmd.py", "_error_outcome"): 2,
    ("tan/commands/scaffold_cmd.py", "ScaffoldError"): 0,
    # tan-cli#454: `_refuse_required(subcommand, code, message, context,
    # output_format)` -- `code` is its 2nd positional (index 1). Its two call
    # sites (`quality`'s `quality.profile-required`, `migrate`'s
    # `migrate.mode-required`) are what this index resolves.
    ("tan/commands/west_forward_cmd.py", "_refuse_required"): 1,
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
        ("tan/commands/doctor_cmd.py", "SDK_DISCOVERY_DIVERGENT"),  # imported from build_cmd.py
        # (tan-cli#407). Same shape as the line above and covered the same way:
        # `_module_string_constants` only reads the file it is given, so a
        # constant DEFINED in `build_cmd.py` (where its literal
        # `"sdk.discovery-divergent"` IS resolved and checked against
        # `contract/issue-codes.json`) cannot resolve from `doctor_cmd.py`'s
        # own tree. Declared, not silently dropped.
        ("tan/commands/build_cmd.py", "code"),  # `Issue(code, ...)` inside `_refuse`'s OWN body,
        # forwarding ITS OWN `code` parameter -- `_refuse` is itself in
        # `_FULL_CODE_CALLABLES`, so its call sites carry the literal.
        ("tan/commands/faultdecode_cmd.py", "code"),  # `Issue(code, ...)` inside `_refuse`'s OWN
        # body (tan-cli#616) -- `_refuse` is itself in `_FULL_CODE_CALLABLES`,
        # so both of its call sites carry the literal.
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
        ("tan/commands/west_forward_cmd.py", "code"),  # `Issue(code, ...)` inside `_refuse_required`'s
        # OWN body (tan-cli#454) -- `_refuse_required` is itself in
        # `_FULL_CODE_CALLABLES`, so its two call sites carry the literal.
        ("tan/commands/scaffold_cmd.py", "err.code"),  # <- ScaffoldError
        ("tan/commands/diff_cmd.py", "failure.code"),  # <- ParseFailure. NOT a whole code (it is a
        # bare suffix, e.g. "schema-violation" -- ParseFailure is deliberately
        # NOT in _FULL_CODE_CALLABLES, since _is_code_literal requires a dot
        # and a bare suffix has none). This entry only silences the generic
        # `code=` keyword scan below; the actual literal is captured by the
        # PREFIX-TEMPLATE mechanism instead (`_FORWARDER_SUFFIXES[("tan/commands
        # /diff_cmd.py", "failure.code")]`, consulted from inside
        # `_resolve_helper` while scanning `_emit_failure(...)`'s own call
        # sites) -- "captured elsewhere in this same scan" still holds, just
        # in the sibling scan this file also runs.
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


def _prefix_templates(path: pathlib.Path) -> list[tuple[int, str, str, str, str]]:
    """Every f-string used AT A CODE POSITION in `path` -- `Issue(...)`'s
    first argument, or a `code=` keyword's value, the SAME two positions
    [`_literal_codes_in_file`] inspects -- shaped EXACTLY `[one fixed literal
    segment ending/starting with `.`, ONE substitution]`, in either order --
    e.g. `f"bootstrap.{code}"` (`kind="prefix"`) or `f"{subcommand}.failed"`
    (`kind="family"`). Returns `(lineno, literal segment, unparsed
    substituted expression, kind, enclosing qualname)`.

    The enclosing qualname (`Log.take_issues`, `_refusal`, `validate.fail`)
    is the dotted name of the innermost function/method/class the f-string
    sits inside, built while walking (the same idea Python's own
    `__qualname__` encodes, minus the `<locals>` marker CPython inserts for a
    nested function -- unneeded here, since nothing downstream reconstructs a
    real `__qualname__`, it only looks a site up BY this identity). This is
    what [`_RESOLVABLE_HELPERS`]/[`_ACKNOWLEDGED_CEILINGS`] key on INSTEAD of
    `lineno`: `lineno` is still returned (and still belongs in every error
    message, so a human can jump straight to the site), but it is not part of
    any declared, hand-maintained key any more -- see those tables' own
    comments for the concrete break (tan-cli#224, dev's fc88ca1) that made
    keying on it a defect rather than a convenience.

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
    out: list[tuple[int, str, str, str, str]] = []
    stack: list[str] = []

    def _qualname() -> str:
        return ".".join(stack) if stack else "<module>"

    def _scan(value: ast.expr | None) -> None:
        if value is None:
            return
        qualname = _qualname()
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
                out.append((node.lineno, tail.value, ast.unparse(head.value), "family", qualname))
                continue
            if (
                isinstance(head, ast.Constant)
                and isinstance(head.value, str)
                and _is_family_prefix(head.value)
                and isinstance(tail, ast.FormattedValue)
            ):
                out.append((node.lineno, head.value, ast.unparse(tail.value), "prefix", qualname))

    def _walk(node: ast.AST) -> None:
        # Tracks the enclosing def/class stack (for the qualname `_scan`
        # reads) while still visiting EVERY node in the tree, the same
        # completeness the old flat `ast.walk(tree)` had -- a nested
        # function/class pushes its name, recurses into its own body, then
        # pops, so a template two scopes deep still gets the full dotted
        # name.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.append(node.name)
            for child in ast.iter_child_nodes(node):
                _walk(child)
            stack.pop()
            return
        if isinstance(node, ast.keyword) and node.arg == "code":
            _scan(node.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Issue" and node.args:
            _scan(node.args[0])
        for child in ast.iter_child_nodes(node):
            _walk(child)

    _walk(tree)
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
#: notices the first case. Deliberately kept as a scalar count, not replaced:
#: it is ALREADY immune to the line-shift defect this file's re-keying fixes
#: (tan-cli#224) -- it counts template OCCURRENCES across the tree, never a
#: line number, so an unrelated edit that merely moves existing sites cannot
#: change it. It still needs a hand bump on a genuine content change (a
#: template added or removed), which is the correct, narrow cost a drift
#: detector for "did the template COUNT change" should have -- the defect
#: fixed here was the SEPARATE `_RESOLVABLE_HELPERS`/`_ACKNOWLEDGED_CEILINGS`
#: keys pinning WHERE each one lives, not this total.
#:
#: KEPT alongside the per-key `sites` field ALL THREE declared tables now
#: carry (`_RESOLVABLE_HELPERS`/`_ACKNOWLEDGED_CEILINGS` from the first
#: review round, `_FORWARDER_SUFFIXES` from the second, tan-cli#224 review
#: remediation), deliberately not replaced by either: the two are NOT
#: redundant, because they cover different ground. `sites` only exists at
#: keys someone has DECLARED in one of the three tables -- of today's 11
#: templates, all 11 now sit behind a `sites` pin, the last 3
#: (`bootstrap_cmd.py`'s `refusal.code` forward, `doctor_cmd.py`'s
#: `venv_refusal.code` forward, `validate_cmd.py`'s `result.outcome` forward)
#: via `_FORWARDER_SUFFIXES`'s OWN `sites` field once a reviewer showed the
#: identical many-to-one collapse reaches a `(file, expr)` key exactly as
#: easily as a `(file, qualname)` one -- but a BRAND NEW expression or
#: qualname nobody has declared ANYWHERE still only shows up as a total
#: mismatch here, never as a per-key one (there is no key yet for it to
#: collapse onto). Conversely, `sites` catches something this scalar cannot
#: localize on its own: which SPECIFIC key absorbed an extra template, by
#: name, with every offending file:line -- this total only says "11 became
#: 12" and, on its own, invites exactly the "bump the number and move on"
#: response the reviewer's exploit relied on. One coarse, whole-tree tripwire
#: plus precise per-key tripwires is not the same overlap as two detectors
#: both hand-bumped for the SAME fact; dropping either narrows real coverage.
EXPECTED_TEMPLATE_COUNT = 14

#: `(file, enclosing qualname)` -> how to recover the missing half, for every
#: template resolvable by scanning one declared callable's call sites.
#: `enclosing qualname` is the dotted name of the function/method the
#: f-string ITSELF sits inside (`Log.take_issues`, `_refusal`,
#: `validate.fail`), tracked by [`_prefix_templates`] while it walks --
#: DELIBERATELY NOT the f-string's line number, which an earlier version of
#: this table used and which broke for real (tan-cli#224): dev's fc88ca1
#: shifted `_refusal`'s own template from :1522 to :1532 by adding lines
#: earlier in the same file, changing nothing about `_refusal` itself, and
#: reddened this gate on an unrelated, already-merged PR. A qualname is
#: immune to that -- it only changes if the SITE itself is renamed, moved to
#: a different function, or rewritten, all of which genuinely warrant
#: updating this table -- and it still disambiguates every case a bare
#: `(file, prefix, expr)` key could not: `bootstrap_cmd.py` has TWO distinct
#: templates that both substitute a parameter literally named `code`, but
#: one sits in `Log.take_issues` and the other in `_refusal`. `kind`
#: (default `"prefix"`) picks which half is fixed: `"prefix"` -- `prefix` is
#: the fixed literal, `expr`'s call-site argument is the SUFFIX
#: (`f"bootstrap.{code}"`, `bootstrap.` + scanned code); `"family"` --
#: `suffix` is the fixed literal, `expr`'s call-site argument is the FAMILY
#: (`f"{subcommand}.failed"`, scanned subcommand + `.failed`). Either way the
#: named callable's call sites are scanned the same way (`name`/`attr`,
#: `arg_index`/`arg_keyword`) -- a constructor whose call sites carry the
#: missing half works exactly like a helper whose OWN parameter does
#: (`doctor_cmd.py`'s `Check(name=...)` below is a constructor, not the
#: f-string's enclosing function; `_resolve_helper` does not care which).
#: Two templates that share one qualname (`west_forward_cmd.py`'s
#: `_run_forward`, whose success and `OSError` arms both build the identical
#: `f"{subcommand}.failed"`) collapse to ONE entry below, not two -- they are
#: the same resolvable site scanned once, never two coincidentally-identical
#: declarations to keep in sync by hand.
#: Every entry's `sites` field is the exact number of `_prefix_templates`
#: MATCHES (f-string occurrences) expected at that `(file, qualname)` key --
#: a SEPARATE axis from `expected_calls` (the number of calls to the scanned
#: helper/constructor itself, e.g. `Log.warn(...)` call sites). This is the
#: closing of the hole a reviewer found in the qualname re-keying (tan-cli
#: #224): because the key is `(file, qualname)`, not `(file, lineno)`, a
#: SECOND, unregistered template appearing inside an already-declared
#: function/method collapses onto the SAME key -- the old code only recorded
#: key MEMBERSHIP (`seen_helper_keys: set`), which a second occurrence at an
#: already-seen key does not change, so it passed silently as long as its
#: `kind`/literal/`expr` happened to match the declared spec (exactly the
#: shape a shadowed comprehension variable produces). `sites` makes the COUNT
#: itself a declared, asserted fact -- `_classify_and_resolve` records every
#: lineno matched per key and hands it to [`_check_site_counts`], which fails,
#: naming the qualname, both counts, and every matched file:line, when the
#: real count differs from `sites`. Bump
#: `sites` ONLY after confirming each newly-listed line is a legitimate,
#: already-registered emit -- the same discipline `expected_calls`'s own
#: docstring insists on for `_resolve_helper`'s call-site count.
_RESOLVABLE_HELPERS: dict[tuple[str, str], dict] = {
    ("tan/commands/bootstrap_cmd.py", "Log.take_issues"): dict(
        # `Log.warn(self, code, message)`, drained by `take_issues` into
        # this exact f-string -- every call site is `<log>.warn(...)`.
        prefix="bootstrap.",
        expr="code",
        attr="warn",
        arg_index=0,
        # 17, not 16, since dev's 518ac8c (tan-cli#334) split the single
        # `zephyr-base-incompatible` warn into a found/else pair so the message
        # can name the evidence. Two call sites, ONE code, already registered
        # (`bootstrap.zephyr-base-incompatible`) -- checked before bumping,
        # which is the whole point of the count: it forced the look.
        #
        # 20, not 17, since tan-cli#390 split `ensure_venv`'s existing-venv
        # branch three ways. Each is a NEW code and all three are registered:
        # `bootstrap.adopted-venv-unusable` (an adopted tree's venv is refused,
        # never deleted), `bootstrap.venv-probe-inconclusive` (the pip probe
        # never answered, so the venv is reused rather than removed), and
        # `bootstrap.venv-recreated` (the one surviving delete, promoted from a
        # `log.line` so it reaches `issues[]`). Checked before bumping.
        #
        # 31, not 20, since issue #474 (ADR 0021 Lane 1 P1) added
        # `toolchain_phase`/`_acquire_toolchain`/`_finish_toolchain_install`,
        # which together carry ELEVEN new `log.warn("toolchain-install", ...)`
        # call sites -- one code, many sites, all of them the phase's own
        # non-fatal failure modes (bad manifest, missing 7-Zip, insufficient
        # disk, a `west sdk install` failure, a post-install version
        # mismatch, a compiler that will not run, a failed stamp write, an
        # adopted-root refusal). Registered as `bootstrap.toolchain-install`
        # in contract/issue-codes.json before bumping.
        expected_calls=31,
        sites=1,
    ),
    ("tan/commands/bootstrap_cmd.py", "_refusal"): dict(
        prefix="bootstrap.",
        expr="code",
        name="_refusal",
        arg_index=1,
        # 10, not 9, since tan-cli#964 review added the
        # `metadata-schema-invalid` refusal: `bootstrap` reads a SoM preset to
        # build its topology, so a schema-invalid preset must REFUSE rather
        # than silently degrade the way `tan presets`'s WARN half is allowed
        # to. Code registered before bumping.
        expected_calls=10,
        sites=1,
    ),
    ("tan/commands/debug_config_cmd.py", "_failure"): dict(
        prefix="debug-config.",
        expr="code",
        name="_failure",
        arg_keyword="code",
        # 6, not 2, as of tan-cli#462: `_build_manifest_missing_failure` and
        # `_core_unknown_failure` each add their own literal `code=` call to
        # `_failure`, splitting their two refusals off `_internal_failure`'s
        # blanket `internal-failure` at `VALIDATION_FAILURE` (2) (4). Review
        # round: `_target_kind_ambiguous_failure` and
        # `_no_debuggable_target_class_failure` do the same for
        # `infer_target_kind`'s other two refusal shapes -- the SAME defect,
        # not a distinct one -- bringing this to 6. All four split-off codes
        # checked against the registry before each bump.
        # 9, not 7 or 8: THREE independent changes each added a `_failure(`
        # call site to this module, and two separate two-way merges each
        # wrote 8 on their own branch before the third landed.
        #   tan-cli#489  `_explicit_core_unknown_failure` -- reuses the
        #     already-registered `core-unknown` literal for the
        #     `--target-kind`-explicit path `infer_target_kind`'s guard
        #     never reaches.
        #   tan-cli#477  `_invalid_argument_failure` -- splits every
        #     bad-flag-VALUE refusal off `_internal_failure`'s blanket 5.
        #   tan-cli#476  `_project_not_found_failure` refuses a `--project`
        #     that names a directory which does not exist, instead of
        #     creating it and writing a launch.json into it at exit 0.
        # 6 on dev + 1 + 1 + 1. Resolved deliberately at the merge, not
        # discovered from a red gate.
        #   tan-cli#476 half (b)  `_target_kind_unresolved_failure` -- an
        #     omitted `--target-kind` on a project offering NO signal to
        #     infer one from used to fall through to `parse_target_kind(
        #     None)`'s `native-host` default and write an `Alp: Native Sim
        #     Debug` entry into whatever directory `--project` named. Code
        #     `debug-config.target-kind-unresolved` registered before this
        #     bump. tan-cli#477 major 2 added no call site of its own: its
        #     refusal reuses `_explicit_core_unknown_failure` (already
        #     counted) with a second, SDK-published authority for the same
        #     `core-unknown` code.
        # 9 -> 10.
        expected_calls=10,
        sites=1,
    ),
    ("tan/commands/sdk_cmd.py", "_fail"): dict(
        prefix="sdk.",
        expr="code",
        name="_fail",
        arg_keyword="code",
        # tan-cli#351: was 5. `sdk list` without `--online` moved off `_fail`
        # (which hardcodes exit_code=RUNTIME_FAILURE and severity "error") to a
        # direct `_emit(...)` call with its own `warning`-severity Issue and
        # exit_code=SUCCESS -- a normal state, not a failure. Its code,
        # `sdk.network-required`, is still a LITERAL `Issue("sdk.network-
        # required", ...)` first-arg site, so it is still covered, just by the
        # plain-literal scan (shape 1) instead of this prefixing scan (shape 3).
        expected_calls=4,
        sites=1,
    ),
    ("tan/commands/validate_cmd.py", "validate.fail"): dict(
        prefix="validate.",
        expr="code",
        name="fail",
        arg_index=0,
        # 6, not 5, since tan-cli#376 ported the spawn path: it DROPPED the
        # `spawn-not-implemented` refusal and added the two guards that refusal
        # stood in front of -- `sdk-root-unresolved` (the oracle's own guard 2)
        # and `spawn-failed` (the subprocess could not be started at all). Each
        # checked against the registry before the bump, which is the point of
        # the count.
        # 7 as of the #376 follow-up, which ported the oracle's THIRD pre-spawn
        # guard (`crates/tan-cli/src/commands/validate.rs:124-129`):
        # `python-too-old`, the one #376 left out while its own module docstring
        # claimed three guards were implemented.
        expected_calls=7,
        sites=1,
    ),
    ("tan/commands/doctor_cmd.py", "checks_to_issues"): dict(
        # `check.code or f"doctor.{kebab_check_name(check.name)}"` in
        # `checks_to_issues()` -- the ceiling this bucket used to acknowledge
        # instead of resolving (tan-cli#224 review): MEASURED at 48
        # `Check(...)` constructions, every one passing its `name` positionally
        # and literally, zero non-literal, 20 distinct camelCase names
        # (`_is_code_suffix` admits camelCase for exactly this reason -- see
        # its own docstring). The camelCase `name` is still what's scanned off
        # each `Check(...)` call (`_resolve_helper`'s own literal scan, below,
        # is unchanged); `kebab=True` tells it to run `kebab_check_name` over
        # each scanned literal before combining with `prefix` -- the 27
        # camelCase issue codes this shipped in v0.5.0, since `check.name` is
        # ALSO a JSON data key/display string and stays camelCase there
        # deliberately (see `Check.as_dict` / `Check`'s own docstring).
        # 54, not 48, as of tan-cli#91's `--fix` consent-gate work: 9 of the
        # 54 also passed an explicit `code=` override (the frozen
        # `bootstrap.*` spellings this class's own docstring documents, plus
        # five NEW `doctor.fix-*` checks whose `name` is a dynamic
        # `f"fix:{tool}"`, never a code-shaped literal at all) --
        # `skip_if_keyword="code"` excludes exactly those from this scan,
        # since `check.code or f"doctor.{kebab_check_name(check.name)}"` never
        # evaluates their `name` for the code position at runtime; their real
        # codes are
        # captured separately by the plain `code=` keyword scan, the same
        # mechanism every other literal `code=` site in this file already
        # goes through.
        # 55, and 10 skipped, as of tan-cli#360: `fix_installer_not_found_check`
        # is the sixth `doctor.fix-*` Check, named `f"fix:{installer}"` (one
        # per ABSENT INSTALLER, not per tool) and carrying an explicit
        # `code="doctor.fix-installer-not-found"`.
        #
        # 56 as of tan-cli#407: `sdk_check` grew a second return, the `warn`
        # arm for a workspace where the narrow and wide ladders resolve
        # different checkouts. It carries `code=SDK_DISCOVERY_DIVERGENT`, so
        # it is SKIPPED by `skip_if_keyword` and its code is covered by the
        # `_KNOWN_CODE_FORWARDS` entry above rather than by this count -- the
        # count moves anyway, because what it pins is how many `Check(...)`
        # sites exist, not how many of them this spec classifies.
        #
        # 57 as of tan-cli#488 defect 1: `west_resolved_check` grew a new
        # `not ran` return -- a `west` that resolves but cannot be executed.
        # It passes no `code=` override, so it is NOT skipped -- but its
        # literal name is still `"westResolved"`, the SAME name the existing
        # `found is None` arm already uses, so `doctor.west-resolved` needs no
        # new registry entry. The count moves because one more `Check(...)`
        # call site exists now (3 -> 4 in that function), not because a new
        # code exists.
        #
        # 58 as of tan-cli#488 ROUND 2, defect 3: `west_check` grew a new
        # `resolved is not None and not resolved_ran` return -- the sibling of
        # the `west_resolved_check` case above, for a `west` that resolves
        # through the workspace venv but cannot actually be spawned. It passes
        # no `code=` override either, and its literal name is still `"west"`,
        # the SAME name every other arm of this function already uses, so
        # `doctor.west` needs no new registry entry. The count moves because
        # one more `Check(...)` call site exists now, not because a new code
        # exists.
        #
        # 62 as of the alp-sdk `_check_libraries` port: `libraries_check` is
        # four `Check(...)` sites -- one per `LibraryReport` outcome -- and
        # all four are literally named `"libraries"`, so they contribute ONE
        # code, `doctor.libraries`, newly registered in
        # `contract/issue-codes.json`. Unlike the two entries above, this one
        # IS a new code, not just a new call site.
        #
        # 63 as of tan-cli#727: `sdk_check` grew a `dangling_flag_root` arm
        # for a `--sdk-root` the loader-marker check rejected. It is named
        # `"sdk"` -- the same literal every other arm of that function uses --
        # and passes no `code=` override, so `doctor.sdk` needs no new
        # registry entry. One more call site, no new code.
        #
        # 69 as of issue #474 (ADR 0021 Lane 1 P1): `toolchain_check` is SIX
        # `Check(...)` sites (no-sdk-root, missing/malformed manifest, pass,
        # version-skew fail, no-toolchain fail), all literally named
        # `"toolchain"`, so they contribute ONE new code, `doctor.toolchain`,
        # newly registered in `contract/issue-codes.json` -- the same shape
        # as the `libraries_check` bump above.
        #
        # 70 as of tan-cli#990 review (the BLOCKER fix): `toolchain_check`
        # grew a SEVENTH `Check(...)` site -- the host-toolchain-matches-the-
        # pin adoption path (`_host_toolchain_matching_pin`), a `pass`
        # alongside the existing stamp-verified `pass`. Still literally named
        # `"toolchain"`, so still `doctor.toolchain`; no new code registered.
        prefix="doctor.",
        expr="kebab_check_name(check.name)",
        name="Check",
        arg_index=0,
        skip_if_keyword="code",
        kebab=True,
        expected_calls=70,
        sites=1,
    ),
    ("tan/commands/west_forward_cmd.py", "_run_forward"): dict(
        # `Issue(f"{subcommand}.failed", ...)` -- the MIRRORED shape
        # (tan-cli#224 review): `subcommand` is `_run_forward`'s own
        # parameter, closed to the three literal strings its three Typer
        # callers (`migrate`/`lock`/`quality`) pass. TWO templates in this
        # one function (the success arm and the `OSError` arm) share this
        # exact spec and collapse to one declared entry here -- `sites=2`
        # is what now RECORDS that fact instead of leaving it to a comment:
        # before this field existed, nothing distinguished "one function,
        # two known templates" from "one function, one known template plus
        # a silently-collapsed unregistered one" -- see this table's own
        # comment above.
        kind="family",
        suffix=".failed",
        expr="subcommand",
        name="_run_forward",
        arg_index=0,
        expected_calls=3,
        sites=2,
    ),
    ("tan/commands/diff_cmd.py", "_emit_failure"): dict(
        # `Issue(f"diff.{code}", ...)` inside `_emit_failure()` -- `code` is
        # `_emit_failure`'s OWN keyword-only parameter, fed by 4 call sites in
        # `diff()`: 2 literal suffixes (`board-yaml-missing`, `internal-failure`
        # x2) and one forward, `code=failure.code` (the `except ParseFailure as
        # failure` handler) -- `failure.code` is a bare suffix read off
        # `ParseFailure`'s own raise sites (`pyyaml-unavailable`,
        # `schema-violation`), declared in `_FORWARDER_SUFFIXES` below since
        # `_resolve_helper` cannot read a plain (non-Starred) forwarded
        # attribute directly.
        prefix="diff.",
        expr="code",
        name="_emit_failure",
        arg_keyword="code",
        expected_calls=4,
        sites=1,
    ),
    ("tan/commands/trace_cmd.py", "trace.fail"): dict(
        # `Issue(f"trace.{code}", ...)` inside `fail()`, a nested function of
        # the `trace` command -- `code` is `fail`'s own 2nd positional
        # parameter (`exit_code, code, message, data, text_lines`), all 3 call
        # sites literal (`sdk-root-unresolved`, `board-yaml-missing`,
        # `internal-failure`).
        prefix="trace.",
        expr="code",
        name="fail",
        arg_index=1,
        expected_calls=3,
        sites=1,
    ),
}

#: `(file, exact substituted expression text)` -> `dict(suffixes=..., sites=...)`
#: -- `suffixes` is the closed, source-verified set of suffixes a FORWARDED
#: expression can carry, read from the origin, not guessed (see the module
#: docstring's bucket description). `warn(*skew)`/`warn(*ceiling)` are keyed
#: by the call shape rather than the bare unparsed name, because a `Starred`
#: argument is not a template substitution at all -- it is resolved per CALL
#: SITE inside `_resolve_helper`, not per f-string.
#:
#: `sites` carries the SAME discipline `_RESOLVABLE_HELPERS`/
#: `_ACKNOWLEDGED_CEILINGS` do, for a REVIEWER-found reason specific to this
#: table: the key is `(file, expr)` -- no qualname, no enclosing-scope
#: information at all -- so a wholly UNRELATED function elsewhere in the same
#: file whose own f-string happens to substitute an identically-spelled
#: expression collapses onto the same entry and is silently resolved by
#: whatever suffix set that entry already declares. Measured: adding
#: `def _sneaky(refusal): return Issue(f"bootstrap.{refusal.code}", "error",
#: "smuggled")` to `bootstrap_cmd.py` and bumping `EXPECTED_TEMPLATE_COUNT`
#: 11 -> 12 gave "4 passed" before this field existed. For the three plain-
#: expression entries, `sites` is the exact count of [`_prefix_templates`]
#: matches at that `(file, expr)` key (asserted by [`_check_site_counts`],
#: the same function the other two tables share); for the two `*tuple`
#: entries, it is the exact count of Starred-argument call sites
#: [`_resolve_helper`] itself matches to that key -- a DIFFERENT scan
#: (`_resolve_helper`'s own `expected_calls` loop over calls to the ONE
#: helper each entry's `warn(*...)` shape names), fed into the SAME
#: `_check_site_counts` check by `_classify_and_resolve` merging both scans'
#: hits before calling it.
_FORWARDER_SUFFIXES: dict[tuple[str, str], dict] = {
    # `Issue(f"bootstrap.{refusal.code}", ...)` in bootstrap_cmd.py -- no line
    # number on purpose: this whole table was re-keyed off line numbers because
    # they rot (tan-cli#224), and a comment that pins one rots the same way.
    # forwards `check_prerequisites()`'s `PrereqFailure.code`
    # (`tan/core/bootstrap.py`), which is exactly one of these four literals
    # depending on which refusal branch it returned.
    ("tan/commands/bootstrap_cmd.py", "refusal.code"): dict(
        suffixes=frozenset({"prerequisites-missing", "python-not-runnable", "python-too-old", "venv-unusable"}),
        sites=1,
    ),
    # `code=f"bootstrap.{venv_refusal.code}"` at doctor_cmd.py:662 forwards
    # the SAME `PrereqFailure`, but `venv_refusal` there is only ever set
    # from `posix_venv_unusable()` (doctor_cmd.py:2342) -- a strictly
    # narrower value space than the bootstrap_cmd.py forward above.
    ("tan/commands/doctor_cmd.py", "venv_refusal.code"): dict(suffixes=frozenset({"venv-unusable"}), sites=1),
    # `code=failure.code` in `diff()`'s `except ParseFailure as failure:`
    # handler, forwarded to `_emit_failure(...)`. Unlike every entry above,
    # this key is matched from INSIDE `_resolve_helper`'s own call-site scan
    # (see its docstring), not from a direct f-string occurrence
    # `_prefix_templates` finds -- `failure.code` never appears in an f-string
    # at all, it is a plain `code=` keyword value at one of `_emit_failure`'s
    # 4 call sites. `sites=1` counts that one call site. The suffixes are
    # every literal (or, for `_reject_if_sdk_validator_disagrees`'s
    # `ParseFailure(outcome, ...)`, every value `outcome` can hold)
    # `ParseFailure(...)` is raised with, read from source: `pyyaml-
    # unavailable`/`schema-violation` from `_load_document`/`_parse_fields`;
    # `python-too-old`/`spawn-failed` from `_reject_if_sdk_validator_disagrees`'s
    # own two direct refusals; `schema-violation`/`missing-preset`/
    # `hardware-revision`/`failed` (the fifth, `clean`, never reaches a raise)
    # from `_spawn_validator`'s outcome, the same `_STATUS_OUTCOME` +
    # `OUTCOME_FAILED` vocabulary `validate_cmd`'s own `result.outcome` entry
    # below declares (tan-cli#455 review round).
    ("tan/commands/diff_cmd.py", "failure.code"): dict(
        suffixes=frozenset(
            {
                "pyyaml-unavailable",
                "schema-violation",
                "python-too-old",
                "spawn-failed",
                "failed",
                "missing-preset",
                "hardware-revision",
            }
        ),
        sites=1,
    ),
    # `Issue(f"support-bundle.{doctor_cmd.kebab_check_name(c.name)}", ...)` in
    # `_doctor_issues()` -- tan-cli #374 findings 3/4 rewrote this entry. `checks` there is
    # `_debug_doctor_report(...)`'s own output (tan-cli#357; `_doctor_section`,
    # the function this comment used to cite, was DELETED by that same diff),
    # not `doctor_cmd._collect(...)`'s whole build/flash-readiness list -- so
    # `c.name`'s real value space is NOT the 20-ish names
    # `_RESOLVABLE_HELPERS[("tan/commands/doctor_cmd.py", "checks_to_issues")]`
    # resolves for `tan doctor` at all. Re-derived from
    # `_debug_doctor_report`'s own construction (`support_bundle_cmd.py`),
    # narrowed to names whose `Check` can ever leave `pass`/`unknown` --
    # `_doctor_issues` only turns a `warn`/`fail` status into a wire issue, so
    # a name that can never carry either (`workspaceRoot`: hardcoded `pass`;
    # `lldb`: hardcoded `pass`, #131; the three `_extension_check(...)` names:
    # hardcoded `unknown`, #102) would register a code this command can
    # structurally never put on the wire:
    #   * `sdkRoot`, `boardYaml` -- the report's own fixed checks (fail/warn
    #     arms exist for both).
    #   * `jlinkBackend`/`openocdBackend`/`pyocdBackend` -- `_target_checks`'s
    #     `f"{server}Backend"` for the three servers `debug_launch._SERVER_
    #     CHOICES` actually pairs with `zephyr-mcu`/`baremetal-mcu` today.
    #     `gdbserverBackend`/`noneBackend` are the SAME `f"{server}Backend"`
    #     construction for the other two `SERVER_KINDS` members -- not
    #     reachable through TODAY's `_SERVER_CHOICES` pairing (`is_server_
    #     supported_for_target` refuses `--target-kind zephyr-mcu --server
    #     gdbserver` before `_target_checks` ever runs), but `server` is a
    #     plain `str` parameter with no narrower type at this call site, so a
    #     future widening of that pairing table would put either straight on
    #     the wire with nothing here to catch it. Declared now rather than
    #     left for the NEXT audit to rediscover.
    #   * `gdb` -- `_target_checks`'s `yocto-userspace` branch (warn arm
    #     exists).
    #   * `bootstrapManifest`, `hostPrerequisites`, `zephyrSdkAvailableForHost`,
    #     `longPaths`, `homePath` -- `_HOST_CHECK_ORDER`'s five names, harvested
    #     BY NAME from `doctor_cmd._collect(...)` (`_host_checks_from_doctor`).
    #     Also a genuine subset of doctor_cmd.py's own 20-ish resolved names
    #     (unsurprising: they are the identical `Check` objects, not a copy),
    #     but declared here independently rather than intersected from there,
    #     because `_HOST_CHECK_ORDER` -- support_bundle_cmd.py's own tuple --
    #     is the thing that actually bounds what this command can harvest, and
    #     is the one artefact a reviewer changing that tuple will see.
    ("tan/commands/support_bundle_cmd.py", "doctor_cmd.kebab_check_name(c.name)"): dict(
        # `suffixes` stays the raw camelCase `c.name` value space -- what
        # `_debug_doctor_report`'s `Check(...)` sites actually pass, unchanged
        # by the kebab fix (`Check.name` is also a JSON data key/display
        # string and stays camelCase there deliberately). `kebab=True` tells
        # the reconstruction below to run `kebab_check_name` over each of
        # these before combining with the `support-bundle.` literal, mirroring
        # the runtime `Issue(f"support-bundle.{doctor_cmd.kebab_check_name(c.name)}", ...)`.
        suffixes=frozenset(
            {
                "sdkRoot",
                "boardYaml",
                "jlinkBackend",
                "openocdBackend",
                "pyocdBackend",
                "gdbserverBackend",
                "noneBackend",
                "gdb",
                "bootstrapManifest",
                "hostPrerequisites",
                "zephyrSdkAvailableForHost",
                "longPaths",
                "homePath",
            }
        ),
        kebab=True,
        sites=1,
    ),
    # `Issue(f"validate.{result.outcome}", ...)` -- ONE template, fed by BOTH
    # of `validate()`'s paths (that single funnel is deliberate: a second
    # template for the spawn path would be a second, separately-declared site
    # for the same wire fact). `result.outcome`'s value space is
    # `validate_cmd._STATUS_OUTCOME`'s four outcomes plus `failed`, ALL FIVE
    # of them. Was `{"schema-violation"}` alone until tan-cli#376 ported the
    # spawn path -- offline can still only reach `schema-violation` (`outcome
    # = OUTCOME_CLEAN if not messages else OUTCOME_SCHEMA_VIOLATION`), the
    # other four arrive only from a spawned validator.
    # `clean` IS reachable, counter-intuitively: `validate_board_yaml.py`
    # renders every diagnostic to stderr and only RETURNS 1 when
    # `collector.has_errors()`, so a board carrying warnings only exits 0 with
    # findings to report -- exit 0, `data.outcome: "clean"`, and one
    # `validate.clean` issue at severity `warning`. That is the oracle's own
    # `to_cli_issues` shape (it maps every parsed issue to
    # `validate.<outcome>` before its clean-and-empty early return), not an
    # invention of this port.
    ("tan/commands/validate_cmd.py", "result.outcome"): dict(
        suffixes=frozenset(
            {"clean", "schema-violation", "missing-preset", "hardware-revision", "failed"}
        ),
        sites=1,
    ),
    # `log.warn(*skew)` / `log.warn(*ceiling)` in bootstrap_cmd.py (no line
    # numbers -- see the note on the entry above)
    # unpack the `(suffix, message)` pairs `python_floor_skew_warning()` and
    # `python_ceiling_warning()` (`tan/core/bootstrap.py`) return.
    ("tan/commands/bootstrap_cmd.py", "warn(*skew)"): dict(suffixes=frozenset({"python-floor-skew"}), sites=1),
    ("tan/commands/bootstrap_cmd.py", "warn(*ceiling)"): dict(
        suffixes=frozenset({"python-newer-than-verified"}), sites=1
    ),
}

#: `(file, enclosing qualname)` -- the same stable identity
#: `_RESOLVABLE_HELPERS` keys on, for the same reason (see its own comment
#: above: a line number moves on any unrelated edit, tan-cli#224) -- -> a
#: `dict(reason=..., sites=...)`, the same `sites`-pinning discipline
#: `_RESOLVABLE_HELPERS` carries (see its own leading comment): `reason` is
#: why this template is deliberately NOT resolved, stated rather than
#: silently absent (matching `contract.rs`'s own "KNOWN CEILING" paragraph);
#: `sites` is the exact number of `_prefix_templates` matches acknowledged at
#: that key, so a SECOND, unregistered template arriving at an already-
#: acknowledged qualname is a named count mismatch, not a silent collapse --
#: the same hole closed in `_RESOLVABLE_HELPERS`, applied here too since nothing
#: about "this key is an acknowledged ceiling instead of a resolved helper"
#: makes it immune to the same many-to-one key risk.
#: See the module docstring's third bucket. Held EMPTY today: `doctor_cmd
#: .py`'s `check.code or f"doctor.{check.name}"` was the sole entry until
#: tan-cli#224's own review measured its actual cost (48 `Check(...)` call
#: sites, ALL literal) and found it resolvable, not a real ceiling -- it
#: moved to `_RESOLVABLE_HELPERS` above. The bucket stays declared, not
#: deleted, so a genuinely out-of-scope FUTURE template has somewhere honest
#: to go rather than forcing a false resolution.
_ACKNOWLEDGED_CEILINGS: dict[tuple[str, str], dict] = {}


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
    skip_if_keyword: str | None = None,
    kebab: bool = False,
    expected_calls: int,
) -> tuple[set[str], list[str], dict[tuple[str, str], list[int]]]:
    """Scan every call to the declared helper in `path`, read the code
    argument at `arg_index` (positional) or `arg_keyword`, and return
    `(reconstructed codes, unresolved-descriptions, forwarder-hit linenos)`.
    `kind="prefix"` (default) reconstructs `prefix + <scanned suffix>`;
    `kind="family"` reconstructs `<scanned family> + suffix`
    (`west_forward_cmd.py`'s mirrored shape). `kebab=True` runs
    `kebab_check_name` over the scanned suffix before combining -- the
    `doctor_cmd.py` entry's shape, where the SCANNED literal is still
    `Check(...)`'s raw camelCase `name` argument (unchanged: `name` is also a
    JSON data key/display string) but the emitted CODE is the kebab slug.

    `expected_calls` is asserted EXACTLY, mirroring `PREFIXING_SITES`'s own
    pinned per-file counts (`contract.rs:663-682`) for the identical reason:
    a floor lets a call site disappear unnoticed as long as enough others
    remain to clear it. `skip_if_keyword`, when given, EXCLUDES a call from
    both `parts` and `unresolved` (but still counts toward `expected_calls`)
    when that call ALSO passes the named keyword -- `doctor_cmd.py`'s
    `Check(name, ..., code=...)`: `checks_to_issues()`'s own
    `check.code or f"doctor.{check.name}"` never evaluates `name` for the
    code position once `code` is set, so scanning `name` there would either
    misclassify a dynamic non-literal (`f"fix:{tool}"`) as unresolved or
    (worse) quietly add a name that was never the emitted code to `parts` --
    this is a fact about THAT call, not a license to skip counting it.

    A `Starred` argument (`log.warn(*skew)`, unpacking a 2-tuple rather than
    passing the code positionally) is looked up in `_FORWARDER_SUFFIXES` by
    `f"{opener}(*{expr})"`. A non-`Starred`, non-literal argument (a plain
    `Name`/`Attribute`, e.g. `code=failure.code`) is looked up the same table
    by its bare unparsed text (`(rel, expr)`) -- the SAME key space
    `_classify_and_resolve` already uses for a template whose substitution IS
    the forward directly; here the forward sits one level further out, behind
    a declared helper's OWN call site, so `_resolve_helper` is what has to
    make the lookup instead. Anything neither resolves is reported UNRESOLVED
    -- never a silent skip. Every matched forward's lineno (Starred or plain)
    is recorded against its `_FORWARDER_SUFFIXES` key in the returned dict, so
    `_classify_and_resolve` can feed it into the SAME `_check_site_counts`
    that pins `_RESOLVABLE_HELPERS`/`_ACKNOWLEDGED_CEILINGS` -- for a Starred
    forward this is the ONLY place its `sites` count can be measured from,
    since [`_prefix_templates`] never sees a Starred call (it is not an
    f-string); a plain forward CAN also be found directly by
    [`_prefix_templates`] when the f-string substitutes it immediately (the
    `refusal.code`/`venv_refusal.code`/`result.outcome` entries already in
    `_FORWARDER_SUFFIXES`), but `failure.code` here never does -- the f-string
    substitutes `_emit_failure`'s OWN `code` parameter, not `failure.code`
    directly, so THIS scan is the only place that hit is ever counted.
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
    forwarder_hits: dict[tuple[str, str], list[int]] = {}
    for call in calls:
        if skip_if_keyword is not None and any(kw.arg == skip_if_keyword for kw in call.keywords):
            continue

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
                parts |= declared["suffixes"]
                forwarder_hits.setdefault(key, []).append(call.lineno)
            continue
        if arg is not None and not isinstance(arg, ast.Constant):
            plain_key = (rel, ast.unparse(arg))
            declared = _FORWARDER_SUFFIXES.get(plain_key)
            if declared is not None:
                parts |= declared["suffixes"]
                forwarder_hits.setdefault(plain_key, []).append(call.lineno)
                continue
        got = ast.unparse(arg) if arg is not None else "no matching argument"
        unresolved.append(
            f"{rel}:{call.lineno} -- `{opener}(...)`'s code argument is not a literal "
            f"({got}). Either pass a literal suffix, or if it forwards a code from "
            f"elsewhere, resolve the value space by hand and add it to "
            f"_FORWARDER_SUFFIXES."
        )
    if kebab:
        parts = {kebab_check_name(s) for s in parts}
    if kind == "prefix":
        assert prefix is not None
        return {prefix + s for s in parts}, unresolved, forwarder_hits
    assert suffix is not None
    return {s + suffix for s in parts}, unresolved, forwarder_hits


def _check_site_counts(
    declared: dict[tuple[str, str], dict], seen: dict[tuple[str, str], list[int]], bucket: str
) -> list[str]:
    """The count check the qualname/expr re-keying gap needs, shared by all
    THREE many-to-one declared tables (`_RESOLVABLE_HELPERS`,
    `_ACKNOWLEDGED_CEILINGS`, `_FORWARDER_SUFFIXES`): `declared` maps a
    `(file, identity)` key -- `identity` is an enclosing qualname for the
    first two tables, a substituted expression for the third -- to a spec
    carrying a `sites` int, the exact number of matches that key is declared
    to cover. `seen` is what THIS RUN actually found there, keyed the same
    way (from [`_prefix_templates`] for the qualname-keyed tables and the
    plain-expression `_FORWARDER_SUFFIXES` entries; from
    [`_resolve_helper`]'s own Starred-argument scan for the `*tuple`
    `_FORWARDER_SUFFIXES` entries -- see that function's docstring). Any
    mismatch, in EITHER direction, is returned as a message naming the key,
    both counts, and every matched file:line -- never silently absorbed the
    way a bare membership check (`key in declared`) would.

    Deliberately extracted to a MODULE-LEVEL function, not left as a closure
    inside `_classify_and_resolve` (tan-cli#224 review, MAJOR finding): a
    closure captures its enclosing scope and cannot be called with synthetic
    input by a test, so the only thing that had ever watched this exact
    assertion fire was a hand-edit of the production tree -- which does not
    survive into CI, precisely the tan-cli#275 lesson this file cites about
    itself elsewhere. Returns a list of messages rather than raising or
    mutating a list captured from the caller, so a test can call this
    directly and assert on the return value -- see
    `test_check_site_counts_flags_a_declared_vs_actual_mismatch`, which does
    exactly that for both the resolved-helper shape and the (empty-in-
    production, doubly unproven without this) acknowledged-ceiling shape.
    """
    messages: list[str] = []
    for key, spec in declared.items():
        rel, identity = key
        expected = spec["sites"]
        lines = sorted(seen.get(key, []))
        if len(lines) == expected:
            continue
        where = ", ".join(f"{rel}:{ln}" for ln in lines) or "none"
        if len(lines) > expected:
            what_to_do = (
                "a NEW, unregistered site landed at this same declared key instead of "
                "being caught -- read each line above, confirm which is genuinely new, "
                "register its code (add it to contract/issue-codes.json, the same as any "
                "other emit site), and only then bump `sites` to match."
            )
        else:
            what_to_do = (
                "a declared site disappeared -- it was renamed, moved to a different "
                "function, or removed; update or delete this entry (and "
                "EXPECTED_TEMPLATE_COUNT if the total template count changed, not just "
                "this key's share of it)."
            )
        messages.append(
            f"{bucket}[{key!r}] (at {identity}) declares sites={expected} but this run "
            f"found {len(lines)} at {rel}: {where} -- {what_to_do}"
        )
    return messages


def _classify_and_resolve(
    templates: dict[str, list[tuple[int, str, str, str, str]]],
) -> tuple[set[str], list[str], list[str]]:
    """Walk every discovered template, classify it into one of the three
    declared buckets, and resolve the ones that are classified. Returns
    `(reconstructed codes, unresolved call sites, unclassified templates)`.
    """
    codes: set[str] = set()
    unresolved: list[str] = []
    unclassified: list[str] = []
    # `(file, qualname)` -> every `_prefix_templates` lineno matched at that
    # key, for _RESOLVABLE_HELPERS and _ACKNOWLEDGED_CEILINGS respectively.
    # DELIBERATELY not a `set` of keys seen (the old `seen_helper_keys`): the
    # key is `(file, qualname)`, a many-to-one mapping (every template inside
    # one function shares it), so recording only MEMBERSHIP cannot notice a
    # SECOND, unregistered template landing on an already-declared key -- the
    # exact hole a reviewer found in the qualname re-keying (tan-cli#224): a
    # shadowed comprehension variable inside `_refusal` produces a second
    # `f"bootstrap.{code}"` whose kind/literal/expr all match the existing
    # declaration, so it would pass the asserts below unnoticed. Recording
    # every lineno lets the per-key count check further down compare the
    # REAL count against each entry's declared `sites` and name the exact
    # new (or missing) line.
    seen_helper_lines: dict[tuple[str, str], list[int]] = {}
    seen_ceiling_lines: dict[tuple[str, str], list[int]] = {}
    # `(file, expr)` -> every lineno matched at that _FORWARDER_SUFFIXES key,
    # from BOTH sources that can match one: a plain-expression template found
    # right here in this loop, or (merged in below, after the
    # `_RESOLVABLE_HELPERS` loop runs) a Starred call site `_resolve_helper`
    # itself matches. Same many-to-one reasoning as the two dicts above, for
    # the reviewer-found reason `_FORWARDER_SUFFIXES`'s own leading comment
    # gives: this key has no qualname or call-site scope at all, so it is
    # if anything an EASIER key for an unrelated site to collapse onto.
    seen_forwarder_lines: dict[tuple[str, str], list[int]] = {}

    for rel, sites in templates.items():
        for lineno, literal, expr, kind, qualname in sites:
            # `key` is `(file, enclosing qualname)` -- NOT `(file, lineno)` --
            # so an unrelated edit that merely shifts this site's line cannot
            # desync it from its declaration; see _RESOLVABLE_HELPERS' own
            # comment (tan-cli#224). `lineno` still rides along on every
            # message below, purely so a human can jump straight to the site.
            key = (rel, qualname)
            if key in _ACKNOWLEDGED_CEILINGS:
                seen_ceiling_lines.setdefault(key, []).append(lineno)
                continue
            if key in _RESOLVABLE_HELPERS:
                spec = _RESOLVABLE_HELPERS[key]
                spec_kind = spec.get("kind", "prefix")
                assert spec_kind == kind, (
                    f"{rel}:{lineno} (in {qualname}) -- _RESOLVABLE_HELPERS declared "
                    f"kind={spec_kind!r} but the template now reads kind={kind!r}; the "
                    f"f-string changed shape -- update the declaration."
                )
                declared_literal = spec["prefix"] if kind == "prefix" else spec["suffix"]
                assert declared_literal == literal and spec["expr"] == expr, (
                    f"{rel}:{lineno} (in {qualname}) -- _RESOLVABLE_HELPERS declared "
                    f"literal={declared_literal!r} expr={spec['expr']!r} but the template now "
                    f"reads literal={literal!r} expr={expr!r}; the f-string changed shape -- "
                    f"update the declaration."
                )
                seen_helper_lines.setdefault(key, []).append(lineno)
                continue
            fwd_key = (rel, expr)
            fwd = _FORWARDER_SUFFIXES.get(fwd_key)
            if fwd is not None:
                seen_forwarder_lines.setdefault(fwd_key, []).append(lineno)
                suffixes = fwd["suffixes"]
                if fwd.get("kebab"):
                    suffixes = {kebab_check_name(s) for s in suffixes}
                codes |= {literal + s for s in suffixes} if kind == "prefix" else {s + literal for s in suffixes}
                continue
            shape = f'f"{literal}{{{expr}}}"' if kind == "prefix" else f'f"{{{expr}}}{literal}"'
            unclassified.append(
                f"{rel}:{lineno} (in {qualname}) -- new prefix template {shape} is not in "
                f"_RESOLVABLE_HELPERS, _FORWARDER_SUFFIXES or _ACKNOWLEDGED_CEILINGS. "
                f"Classify it in one of the three (see this file's module docstring)."
            )

    for key, spec in _RESOLVABLE_HELPERS.items():
        rel = key[0]
        site_codes, site_unresolved, site_forwarder_hits = _resolve_helper(
            TAN.parent / rel,
            kind=spec.get("kind", "prefix"),
            prefix=spec.get("prefix"),
            suffix=spec.get("suffix"),
            attr=spec.get("attr"),
            name=spec.get("name"),
            arg_index=spec.get("arg_index"),
            arg_keyword=spec.get("arg_keyword"),
            skip_if_keyword=spec.get("skip_if_keyword"),
            kebab=spec.get("kebab", False),
            expected_calls=spec["expected_calls"],
        )
        codes |= site_codes
        unresolved.extend(site_unresolved)
        for fwd_key, lines in site_forwarder_hits.items():
            seen_forwarder_lines.setdefault(fwd_key, []).extend(lines)

    unclassified.extend(_check_site_counts(_RESOLVABLE_HELPERS, seen_helper_lines, "_RESOLVABLE_HELPERS"))
    unclassified.extend(_check_site_counts(_ACKNOWLEDGED_CEILINGS, seen_ceiling_lines, "_ACKNOWLEDGED_CEILINGS"))
    unclassified.extend(_check_site_counts(_FORWARDER_SUFFIXES, seen_forwarder_lines, "_FORWARDER_SUFFIXES"))

    return codes, unresolved, unclassified


def _all_prefix_templates() -> dict[str, list[tuple[int, str, str, str, str]]]:
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
        (
            f'  {rel}:{lineno} (in {qualname}) f"{literal}{{{expr}}}"'
            if kind == "prefix"
            else f'  {rel}:{lineno} (in {qualname}) f"{{{expr}}}{literal}"'
        )
        for rel, sites in templates.items()
        for lineno, literal, expr, kind, qualname in sites
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


def test_check_site_counts_flags_a_declared_vs_actual_mismatch():
    """tan-cli#224 review, MAJOR finding: `_check_site_counts` used to be a
    closure inside `_classify_and_resolve`, so nothing could exercise it
    directly -- the only thing that had ever watched its assertion fire was a
    hand-edit of the production tree, which per tan-cli#275 (this file's own
    standing lesson, applied to itself) does not count as proof an assertion
    fires. Extracting it to a top-level function (see its own docstring)
    makes it directly callable with synthetic input, the same self-test
    discipline `test_gate_rejects_a_deliberately_unregistered_code` and
    `test_prefix_template_scan_finds_a_fresh_synthetic_site` already apply to
    their own targets.

    Exercises BOTH halves `_check_site_counts` is used for. `_RESOLVABLE_HELPERS`'
    shape first, with a synthetic `declared` spec claiming `sites=1` against a
    synthetic `seen` map recording 2 linenos at that key -- mirroring, at the
    unit level, the exact shadowed-comprehension exploit `sites` was added to
    catch (two templates silently sharing one declared key). Then
    `_ACKNOWLEDGED_CEILINGS`'s shape, doubly unproven before this test: that
    table is EMPTY in production today, so nothing had ever driven a real
    value through this exact code path for that bucket at all, synthetic or
    otherwise.
    """
    key = ("tan/commands/selftest_cmd.py", "_selftest_helper")
    declared = {key: dict(prefix="selftest.", expr="code", sites=1)}
    seen_mismatched = {key: [10, 20]}

    messages = _check_site_counts(declared, seen_mismatched, "_RESOLVABLE_HELPERS")
    assert len(messages) == 1, messages
    msg = messages[0]
    # Names the key...
    assert "_RESOLVABLE_HELPERS[('tan/commands/selftest_cmd.py', '_selftest_helper')]" in msg, msg
    # ...and BOTH counts: the declared expectation and the actual finding.
    assert "declares sites=1" in msg, msg
    assert "found 2" in msg, msg
    assert "tan/commands/selftest_cmd.py:10" in msg and "tan/commands/selftest_cmd.py:20" in msg, msg

    # The _ACKNOWLEDGED_CEILINGS half -- same function, same synthetic
    # mismatch, different bucket label -- doubly unproven beforehand since
    # that table carries zero real entries to ever drive this path.
    ceiling_messages = _check_site_counts(declared, seen_mismatched, "_ACKNOWLEDGED_CEILINGS")
    assert len(ceiling_messages) == 1, ceiling_messages
    assert ceiling_messages[0].startswith("_ACKNOWLEDGED_CEILINGS[("), ceiling_messages[0]
    assert "declares sites=1" in ceiling_messages[0] and "found 2" in ceiling_messages[0], ceiling_messages[0]

    # And the negative: a `seen` count that matches `declared` produces no
    # message at all -- proving this is sensitive to the mismatch, not
    # unconditionally red.
    assert _check_site_counts(declared, {key: [10]}, "_RESOLVABLE_HELPERS") == []

    # And the mirrored direction: a declared key with NOTHING seen at all
    # (the "a declared site disappeared" branch) is exactly as loud.
    vanished = _check_site_counts(declared, {}, "_RESOLVABLE_HELPERS")
    assert len(vanished) == 1, vanished
    assert "declares sites=1" in vanished[0] and "found 0" in vanished[0], vanished[0]


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
    assert templates == [(11, "selftestfamily.", "code", "prefix", "emit_two")], templates
