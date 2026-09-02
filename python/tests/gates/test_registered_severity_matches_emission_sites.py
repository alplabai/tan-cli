# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1112: ties a registered `contract/issue-codes.json` `severity` to
the severity its own emission sites actually construct -- the gate that did
not exist when `clean.remove-failed` and `build.toolchain-root-unresolved`
drifted (registered `warning`-only, emitted `error` at a real site too) and
sat undetected until a manual grep during an unrelated review (PR #1110)
surfaced it.

WHAT THIS ASSERTS, and nothing wider. For each code in `_SEEDED_SEVERITIES`
below -- an explicit, opt-in allow-list, seeded with exactly the four codes
this repo has measured to be GENUINELY dual-severity today (not a
one-severity-per-code sweep; see "dual severity is supported" below) -- three
things must all agree:

  1. `_SEEDED_SEVERITIES[code]`'s own declared expectation (independently
     written by a human, not derived from either side below, so the registry
     and the source cannot silently drift to the same wrong answer together);
  2. `contract/issue-codes.json`'s registered `severity` for that code,
     normalised (`"error or warning"` -> `{"error", "warning"}`, else a
     singleton set);
  3. every severity [`_emitted_severities_for`] can prove that code is
     actually constructed with, AST-walking the THREE files below.

WHAT THIS DOES NOT ASSERT, deliberately. Nothing about any code not in
`_SEEDED_SEVERITIES` -- a drift on an unseeded code is exactly as invisible to
this gate as it was before this file existed. Nothing about any file other
than `commands/build_cmd.py`, `commands/clean_cmd.py`, `commands/flash_cmd.py`
-- the three real emission homes of the four seeded codes; a fifth home this
gate has never heard of is not scanned. And nothing at all about a severity
argument that is dynamic (a bare variable, an f-string) UNLESS the CODE
argument at that same call site is itself one of the four seeded literals --
an unrelated call elsewhere in these three files with a severity this gate
cannot resolve is silently out of scope, not a failure, by design (see
`_emitted_severities_for`'s own docstring for exactly which shapes resolve and
which don't).

Building the seeded set WIDER than these four -- scanning every `Issue(`
construction site in these three files and asserting registered-equals-
emitted for ALL of them -- was tried and measured RED on arrival, which is
exactly the failure mode "seeded (opt-in) with these codes" exists to avoid:
`build.missing-tool` (`build_cmd.py:1020`, `"error" if failed else
"warning"`) is a FIFTH code that is ALSO genuinely dual-severity in source but
registered `"error"`-only -- a real, pre-existing drift this issue's own scope
does not cover (tan-cli#1112 named exactly `clean.remove-failed` and
`build.toolchain-root-unresolved`). Seeding it here would fix nothing (this
file only ASSERTS, it does not edit the registry) and would instead red this
gate the moment it landed on a code nobody asked this change to touch --
follow-up work, not silently absorbed here.

DUAL SEVERITY IS SUPPORTED, ON PURPOSE -- do not read this file as an argument
for one-severity-per-code. `flash.nothing-matched` and `build.unknown-backend`
are already correctly registered `"error or warning"` (tan-cli#807, tan-cli
review) and are seeded here NOT because they needed fixing, but so a LATER
sweep cannot flatten them back to a single severity without this gate naming
the exact site and both severities it would be dropping -- see
`test_dual_severity_codes_stay_dual` below, which mutates the normaliser
itself to prove that protection is not vacuous.

HOW EMISSION IS RESOLVED -- see [`_emitted_severities_for`]'s own docstring
for the two shapes it reads (a literal `Issue("code", "severity", ...)`, and
a `"error" if <cond> else "warning"` ternary in the severity position) and the
one FORWARDING shape it follows (a `raise BuildError("code", ...)` inside a
scanned file, resolved through that SAME file's own
`except BuildError as err: ... Issue(err.code, <severity>, ...)` handler --
`build_cmd.py`'s top-level dispatcher, which is what actually turns
`build.toolchain-root-unresolved`'s `executionPolicy.missingTool=fail` arm
into a delivered `error`). Every one of these is read from the real AST, not
assumed or hand-copied -- a rewrap of any of these call sites changes nothing
this gate checks, only a change to WHICH severities a site constructs does.
"""

from __future__ import annotations

import ast
import functools
import json
from pathlib import Path

import pytest

#: Same resolution `test_frozen_issue_codes.py` / `test_issue_code_registry_shape.py`
#: use: `contract/` sits three levels above this file (gates -> tests -> python -> repo root).
REGISTRY = Path(__file__).resolve().parents[3] / "contract" / "issue-codes.json"
TAN = Path(__file__).resolve().parents[2] / "tan"

#: The three real emission homes of every code seeded below -- see the module
#: docstring's "WHAT THIS ASSERTS" for why this list is not wider.
_SCANNED_FILES = (
    TAN / "commands" / "build_cmd.py",
    TAN / "commands" / "clean_cmd.py",
    TAN / "commands" / "flash_cmd.py",
)

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
    strings it can evaluate to at runtime.

    Two shapes resolve: a plain string constant (`"error"`) resolves to a
    singleton set; a `"error" if <cond> else "warning"` ternary -- the exact
    idiom `build_cmd.py` uses for BOTH `build.toolchain-root-unresolved` and
    `build.unknown-backend` -- resolves to the union of both branches WHEN
    both are themselves literal strings. Anything else (a variable, an
    f-string, a ternary with a non-literal branch) is unresolved (`None`),
    never guessed at.
    """
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
    (a `Constant` string shaped like `family.name` -- has a dot). A
    non-literal code argument (`err.code`) is not this function's concern --
    see `_emitted_severities_for`'s BuildError-forwarding pass for how that
    shape is followed instead."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and "." in node.value:
        return node.value
    return None


def _is_err_code_attribute(node: ast.expr | None, exc_name: str) -> bool:
    """True for `<exc_name>.code` -- the shape `except BuildError as <exc_name>:
    ... Issue(<exc_name>.code, <severity>, ...)` re-emits a caught error's own
    code under, e.g. `err.code`."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "code"
        and isinstance(node.value, ast.Name)
        and node.value.id == exc_name
    )


def _emitted_severities_for(path: Path, seeded: frozenset[str]) -> dict[str, tuple[frozenset[str], int]]:
    """`{code: (severities, site count)}` for every code in `seeded` that
    `path`'s AST shows being constructed -- restricted to `seeded` so a
    hard-failure below can never fire over an unrelated, unseeded call site
    in one of the three scanned files (see the module docstring's "WHAT THIS
    DOES NOT ASSERT").

    Three passes, in order:

      1. Every direct `Issue(<literal code>, <severity>, ...)` call whose
         code IS one of `seeded` -- `_severity_from_node` resolves the
         severity (literal or ternary-of-literals). Unresolved is a HARD
         FAILURE (never a silent skip) -- the same "cannot verify
         statically, do not pass anyway" discipline
         `test_blob_format_producers_stay_in_valid_blob_formats.py`
         (tan-cli#1074) established for this shape of gate.
      2. Every `BuildError(<literal code>, ...)` construction (`raise
         BuildError(...)`, or any other call -- the shape does not care)
         whose code is one of `seeded`, recorded so pass 3 can attribute a
         severity to it.
      3. This SAME FILE's own `except BuildError as <name>:` handler(s):
         whatever severity they forward through `Issue(<name>.code,
         <severity>, ...)` applies to EVERY seeded code found via pass 2 in
         THIS file, since the handler catches by exception TYPE, not by
         code -- it cannot distinguish which code it is re-emitting.
         Unresolved is again a hard failure, but ONLY if pass 2 found at
         least one seeded code that could reach it (an unresolved severity
         in a handler that forwards no seeded code is out of scope, same
         restriction as pass 1).

    Site count is every AST node pass 1 or pass 2 matched for that code --
    not the number of distinct severities -- so `test_the_scan_actually_
    finds_the_expected_sites` can catch a site silently vanishing (a rewrite
    that still produces the same severities from FEWER call sites is a
    real change worth noticing, even though it does not change the set this
    gate's main assertion compares).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    severities: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    raised_via_builderror: set[str] = set()

    def _record(code: str, sevs: frozenset[str]) -> None:
        severities.setdefault(code, set()).update(sevs)
        counts[code] = counts.get(code, 0) + 1

    # Pass 1 + pass 2.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "Issue" and len(node.args) >= 2:
            code = _code_from_node(node.args[0])
            if code is None or code not in seeded:
                continue
            sevs = _severity_from_node(node.args[1])
            if sevs is None:
                raise AssertionError(
                    f"{path}:{node.lineno} constructs Issue({code!r}, ...) with a "
                    f"severity argument this gate cannot resolve statically -- "
                    f"extend _severity_from_node, or make the argument a literal "
                    f"or a ternary of literals."
                )
            _record(code, sevs)
        elif node.func.id == "BuildError" and node.args:
            code = _code_from_node(node.args[0])
            if code is not None and code in seeded:
                raised_via_builderror.add(code)
                counts[code] = counts.get(code, 0) + 1

    # Pass 3: this file's own `except BuildError as <name>:` forwarding.
    if raised_via_builderror:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not (isinstance(node.type, ast.Name) and node.type.id == "BuildError" and node.name):
                continue
            exc_name = node.name
            for inner in ast.walk(node):
                if not (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "Issue"
                    and len(inner.args) >= 2
                    and _is_err_code_attribute(inner.args[0], exc_name)
                ):
                    continue
                sevs = _severity_from_node(inner.args[1])
                if sevs is None:
                    raise AssertionError(
                        f"{path}:{inner.lineno} forwards a caught BuildError "
                        f"through Issue({exc_name}.code, ...) with a severity "
                        f"this gate cannot resolve statically, and at least one "
                        f"seeded code ({sorted(raised_via_builderror)}) is "
                        f"raised via BuildError(...) in this same file."
                    )
                for code in raised_via_builderror:
                    severities.setdefault(code, set()).update(sevs)

    return {code: (frozenset(sevs), counts.get(code, 0)) for code, sevs in severities.items()}


@functools.cache
def _emitted_by_code() -> dict[str, tuple[frozenset[str], int]]:
    merged_sevs: dict[str, set[str]] = {}
    merged_counts: dict[str, int] = {}
    for path in _SCANNED_FILES:
        for code, (sevs, count) in _emitted_severities_for(path, frozenset(_SEEDED_SEVERITIES)).items():
            merged_sevs.setdefault(code, set()).update(sevs)
            merged_counts[code] = merged_counts.get(code, 0) + count
    return {code: (frozenset(sevs), merged_counts[code]) for code, sevs in merged_sevs.items()}


#: Opt-in: a code is in scope for this gate if and only if it is a key here.
#: `(expected severities, expected AST site count across _SCANNED_FILES, why)`
#: -- the independently-declared truth this gate checks BOTH the registry and
#: the real emission sites against, so the two sides cannot silently drift to
#: the same wrong answer together. See the module docstring for why exactly
#: these four and no more.
_SEEDED_SEVERITIES: dict[str, tuple[frozenset[str], int, str]] = {
    "clean.remove-failed": (
        frozenset({"warning", "error"}),
        2,
        "tan-cli#1112: was registered `warning`-only -- drift, not the true "
        "shape. clean_cmd.py's best-effort DIRECTORY removal (matching "
        "rmtree(ignore_errors=True), clean_cmd.py:852) warns and does not "
        "fail the command; its NOT-ignore-errors STATE-FILE removal "
        "(os.remove, clean_cmd.py:864) fails the command outright. Both arms "
        "are genuine -- fixed by registering `error or warning`.",
    ),
    "build.toolchain-root-unresolved": (
        frozenset({"warning", "error"}),
        3,
        "tan-cli#1112: was registered `warning`-only -- drift, not the true "
        "shape. build_cmd.py demotes a slice to `warning` under "
        "executionPolicy.missingTool=skip (the default) and to `error` under "
        "=fail -- either directly (a held-outcome retry's own `\"error\" if "
        "failed else \"warning\"`) or via `raise BuildError(...)`, caught and "
        "re-emitted `error` by this command's own top-level handler. "
        "Policy-driven, like its build.unknown-backend sibling -- fixed by "
        "registering `error or warning`.",
    ),
    "build.unknown-backend": (
        frozenset({"warning", "error"}),
        1,
        "already correctly registered `error or warning` -- seeded here so a "
        "LATER sweep cannot flatten it back to one severity without this "
        "gate naming the exact site and both severities being dropped "
        "(tan-cli#1112).",
    ),
    "flash.nothing-matched": (
        frozenset({"warning", "error"}),
        2,
        "already correctly registered `error or warning` (tan-cli#807) -- "
        "seeded here for the same reason as build.unknown-backend "
        "(tan-cli#1112).",
    ),
}


@pytest.mark.parametrize("code", sorted(_SEEDED_SEVERITIES), ids=lambda c: c)
def test_registered_severity_matches_every_emission_site(code):
    expected, _expected_sites, why = _SEEDED_SEVERITIES[code]
    registry = _registry_entries()
    assert code in registry, (
        f"{code!r} is seeded in _SEEDED_SEVERITIES but is not registered in "
        f"{REGISTRY} at all -- {why}"
    )
    registered = _severity_set(registry[code])
    assert registered == expected, (
        f"{REGISTRY} registers {code!r} at severity {registry[code]!r} "
        f"(-> {sorted(registered)}), but this gate's own seeded expectation "
        f"is {sorted(expected)} -- {why}\n\n"
        f"If the real emission genuinely changed, update _SEEDED_SEVERITIES "
        f"in the same change as the registry edit; if the registry drifted "
        f"from the emission sites, fix the registry instead."
    )

    emitted, _site_count = _emitted_by_code().get(code, (frozenset(), 0))
    assert emitted == expected, (
        f"{code!r} is constructed at severities {sorted(emitted)} across "
        f"{', '.join(str(p.relative_to(TAN.parents[1])) for p in _SCANNED_FILES)}, "
        f"but this gate's seeded expectation (and the registry) says "
        f"{sorted(expected)} -- {why}\n\n"
        f"A construction site's severity changed without updating "
        f"_SEEDED_SEVERITIES (or this IS the drift the registry now needs to "
        f"catch up to -- contract/issue-codes.json's own `severity` field for "
        f"{code!r} is the wire contract)."
    )


def test_the_scan_actually_finds_the_expected_sites():
    """Anti-vacuity, the AST-COUNT half: a walk that silently stopped
    matching (a rename, a rewrap that moved the call out of a shape this
    file resolves) could still leave the SET comparison above green by
    accident if the surviving sites happen to still cover both severities.
    Pins the exact site count per seeded code instead, so a site vanishing
    -- even one that does not change the resulting severity SET -- is its
    own loud failure. See `_emitted_severities_for`'s own docstring for what
    counts as a site.
    """
    emitted = _emitted_by_code()
    mismatched = []
    for code, (_expected_sevs, expected_sites, _why) in sorted(_SEEDED_SEVERITIES.items()):
        _sevs, actual_sites = emitted.get(code, (frozenset(), 0))
        if actual_sites != expected_sites:
            mismatched.append(f"{code}: expected {expected_sites} site(s), found {actual_sites}")
    assert mismatched == [], (
        "the AST walk found a different number of construction sites than "
        "_SEEDED_SEVERITIES pins for at least one seeded code -- a site was "
        "added, removed, or rewritten into a shape this gate no longer "
        "resolves (bump the count after confirming the new total is a real, "
        "already-registered set of sites):\n  " + "\n  ".join(mismatched)
    )


def test_the_seeded_table_is_not_empty():
    """Anti-vacuity for the LIST itself -- emptying it would make the
    parametrised check above collect zero cases (pytest turns that into a
    silent skip, not a failure -- tan-cli#275's standing lesson, cited by
    every sibling gate in this file's family)."""
    assert _SEEDED_SEVERITIES, (
        "_SEEDED_SEVERITIES is empty, so the parametrised check above has no "
        "cases and this whole file enforces nothing while still reporting "
        "green. If every seeded code was genuinely retired, delete this file "
        "outright rather than leaving an empty allow-list behind."
    )


def test_dual_severity_codes_stay_dual():
    """Structural proof that `_severity_set` -- the ONLY place this file
    decides whether a registry string means one severity or two -- cannot be
    fooled into reading `"error or warning"` as single-severity, which is
    exactly the shape a later "normalise the registry" sweep could introduce
    (this repo's own recurring defect class: #1059, #1062, PR #1070's :1350,
    and four tests on PR #1111 -- a protection that cannot fail is not a
    protection).

    Drives the two ACTUAL dual registrations through the parser and asserts
    each resolves to BOTH severities, then asserts the parser does NOT
    collapse a genuinely single-severity string into that same two-member
    set -- the mutation this test exists to catch is `_severity_set` (or a
    future edit to it) treating `"error or warning"` and a plain `"error"`
    as indistinguishable.
    """
    for code in ("build.unknown-backend", "flash.nothing-matched"):
        expected, _sites, _why = _SEEDED_SEVERITIES[code]
        assert expected == frozenset({"error", "warning"}), (
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
