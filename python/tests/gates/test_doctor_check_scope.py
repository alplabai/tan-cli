# SPDX-License-Identifier: Apache-2.0
"""No `tan doctor` check may ship without a `scope` (tan-cli#549).

`checks[].scope` exists so a consumer can split HOST rows from PROJECT rows
from the DATA instead of from a hand-written list of tan's own check names.
That only holds if EVERY emitted check carries one: a single check without a
scope puts the consumer back on a hand-list for the remainder, which is the
exact seam that broke downstream (`zephyrSdkHost` -> `zephyrSdkAvailableForHost`,
alp-sdk-vscode#472 -- the stale entry matched nothing, no error anywhere, and
the row it was meant to admit was silently never admitted).

## Why this gate is STATIC as well as runtime

`Check.scope` is a required keyword-only field, so a missing scope is already a
`TypeError` the moment the check is constructed. That is the strongest half of
the enforcement, and `test_every_emitted_check_carries_a_scope` below exercises
it on a real `_collect()` run.

But a runtime walk only reaches the checks THIS host produces. Six `fix:*`
outcomes need a `--fix` run against a missing prerequisite; `longPaths` and
`sevenZip` are Windows-only; several branches need a J-Link, a Zephyr SDK, or a
rejected `metadata/bootstrap.json` to reach. `test_every_check_call_site_
declares_a_literal_scope` therefore reads the SOURCE: every `Check(...)` call
site in `python/tan/`, on every branch, whether or not any host runs it.

The static walk also buys the one thing a required field cannot: consistency.
Nothing about a required argument stops `west_check`'s seven return statements
from disagreeing with each other, and a check whose scope depends on which arm
answered is unusable to a consumer -- it would move rows between the two panes
run to run. `test_one_check_name_never_carries_two_scopes` pins that.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tan.commands import doctor_cmd
from tan.core.doctor_scope import CHECK_SCOPES

#: `python/tan`, found from this file rather than from a cwd, so the gate is
#: identical however pytest was started (the convention
#: `test_module_size_budget.py` records as tan-cli#423's lesson).
_PACKAGE = Path(__file__).resolve().parents[2] / "tan"

#: Every literally-named check and the scope it ships with, DERIVED from the
#: call sites below and pinned here.
#:
#: This is not a second source of truth -- `test_the_pinned_scopes_still_match_
#: the_call_sites` fails in BOTH directions, so the table cannot drift from the
#: code and the code cannot drift from the table. What it buys is that moving a
#: name across the two scopes is a DELIBERATE, reviewed edit: a consumer renders
#: the two scopes in different places, so a reclassification moves a row between
#: panes on the customer's screen. That is a wire-behaviour change and should
#: read like one in a diff, not like a one-word refactor.
#:
#: The computed names are absent by construction -- `fix:{tool}` /
#: `fix:{installer}` (all `host`), `{server}Backend` (`host`), and
#: `support_bundle_cmd._extension_check`'s `name` parameter, which the three
#: VS Code extension checks pass in (`host`). They are still covered by
#: `test_every_check_call_site_declares_a_literal_scope`, which keys on nothing.
_PINNED_SCOPES = {
    # doctor_cmd -- host: the verdict is about this machine.
    "homePath": "host",
    "hostPrerequisites": "host",
    "hostPython": "host",
    "jlink": "host",
    "longPaths": "host",
    "setools": "host",
    "sevenZip": "host",
    "west": "host",
    "zephyrSdk": "host",
    "zephyrSdkAvailableForHost": "host",
    # doctor_cmd -- project: the verdict is about the selected project, the
    # resolved alp-sdk checkout, or the Zephyr workspace built for it.
    "boardYaml": "project",
    "bootstrapManifest": "project",
    "pythonFloor": "project",
    "sdk": "project",
    "sdkProvenance": "project",
    "venvProvenance": "project",
    "westResolved": "project",
    "workspace": "project",
    "zephyrVersion": "project",
    "zephyrWorkspace": "project",
    # support_bundle_cmd's own debug report, built from the same class.
    "gdb": "host",
    "lldb": "host",
    "sdkRoot": "project",
    "workspaceRoot": "project",
}


def _check_calls() -> list[tuple[str, ast.Call]]:
    """Every `Check(...)`/`<mod>.Check(...)` construction under `python/tan/`,
    with the file it sits in. Matched on the callee SPELLING, both the bare
    name (`doctor_cmd`'s own sites) and the attribute form
    (`support_bundle_cmd`, which builds a doctor-shaped report of its own out
    of the same class)."""
    found: list[tuple[str, ast.Call]] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as err:  # pragma: no cover -- fails elsewhere first
            pytest.fail(f"{path} does not parse: {err}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            named = (isinstance(func, ast.Name) and func.id == "Check") or (
                isinstance(func, ast.Attribute) and func.attr == "Check"
            )
            if named:
                found.append((path.relative_to(_PACKAGE.parent).as_posix(), node))
    return found


def _literal_scope(call: ast.Call) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == "scope" and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            return value if isinstance(value, str) else None
    return None


def _literal_name(call: ast.Call) -> str | None:
    """The check's name when it is a plain string literal. `None` for the
    computed ones -- `fix:{tool}`, `{server}Backend`, and
    `support_bundle_cmd._extension_check`'s `name` parameter -- which still
    have to declare a scope, they just cannot be keyed by name here."""
    if call.args and isinstance(call.args[0], ast.Constant):
        value = call.args[0].value
        return value if isinstance(value, str) else None
    return None


def test_the_walk_finds_the_call_sites_it_is_meant_to_guard():
    """A gate that silently matches nothing passes forever. `Check` is
    constructed in exactly two shipping modules today; if a refactor renames
    the class or moves every construction behind a factory, this fails rather
    than going quietly green."""
    calls = _check_calls()
    assert len(calls) >= 60, f"only {len(calls)} Check(...) call sites found -- has the shape moved?"
    files = {rel for rel, _ in calls}
    assert files == {
        "tan/commands/doctor_cmd.py",
        "tan/commands/support_bundle_cmd.py",
    }, f"Check(...) is now constructed in {sorted(files)} -- confirm the new site declares scopes"


def test_every_check_call_site_declares_a_literal_scope():
    """The static half. A missing `scope=` is already a `TypeError`, so what
    this really catches is the other two ways to be unusable: a scope passed
    as a computed expression (unreadable to a reviewer, and one refactor away
    from being wrong on one branch only) and a value outside the wire
    vocabulary."""
    offenders = []
    for rel, call in _check_calls():
        scope = _literal_scope(call)
        if scope is None:
            offenders.append(f"{rel}:{call.lineno}: no literal scope= keyword")
        elif scope not in CHECK_SCOPES:
            offenders.append(f"{rel}:{call.lineno}: scope={scope!r} is not one of {CHECK_SCOPES}")
    assert offenders == [], (
        "every doctor check must declare a literal `scope=` from "
        f"{CHECK_SCOPES}:\n  " + "\n  ".join(offenders)
    )


def test_one_check_name_never_carries_two_scopes():
    """A name that answers `host` on one branch and `project` on another moves
    rows between a consumer's two panes depending on which arm ran. Every
    multi-return check function here (`west_check` has seven) must agree with
    itself."""
    by_name: dict[str, set[str]] = {}
    for _, call in _check_calls():
        name = _literal_name(call)
        scope = _literal_scope(call)
        if name is not None and scope is not None:
            by_name.setdefault(name, set()).add(scope)
    split = {name: sorted(scopes) for name, scopes in by_name.items() if len(scopes) > 1}
    assert split == {}, f"these check names declare more than one scope: {split}"


def test_the_pinned_scopes_still_match_the_call_sites():
    """Both directions at once: a name that changed scope, a name that gained
    one, and a name that went away all fail here. See `_PINNED_SCOPES` for why
    a reclassification is worth making somebody type twice."""
    actual: dict[str, str] = {}
    for _, call in _check_calls():
        name = _literal_name(call)
        scope = _literal_scope(call)
        if name is not None and scope is not None:
            actual[name] = scope
    assert actual == _PINNED_SCOPES


def test_every_emitted_check_carries_a_scope(tmp_path):
    """The runtime half, on the checks THIS host actually produces -- the
    envelope's `data.checks[]` is `[c.as_dict() for c in _collect(...)]`, so
    asserting on `as_dict()` is asserting on the wire."""
    checks = doctor_cmd._collect(None, workspace_root=str(tmp_path))
    assert checks, "_collect produced no checks at all"
    for check in checks:
        payload = check.as_dict()
        assert payload["scope"] in CHECK_SCOPES, f"{check.name}: {payload}"


def test_a_scope_outside_the_vocabulary_is_refused_at_construction():
    """`Check.__post_init__`'s arm, for the caller that computes a scope
    rather than writing a literal -- unreachable from shipping code today
    (the test above pins that), which is why it needs its own case."""
    with pytest.raises(ValueError, match="wire vocabulary"):
        doctor_cmd.Check("invented", "pass", "detail", scope="Host")


def test_both_scopes_are_actually_emitted(tmp_path):
    """The split is only useful if it splits. A run in an empty directory with
    no SDK resolved must still produce both kinds -- that is precisely the
    no-folder-open case the consumer needs to filter."""
    scopes = {c.scope for c in doctor_cmd._collect(None, workspace_root=str(tmp_path))}
    assert scopes == set(CHECK_SCOPES), f"only {sorted(scopes)} emitted"


def test_build_and_plain_doctor_emit_the_same_checks(tmp_path):
    """The `--build` half of tan-cli#549. `alp-sdk-vscode` spawns `doctor
    --build` and plain `doctor` in parallel on every dependency-panel refresh
    and merges the two, because nothing in the contract said the check sets
    were the same -- so the second subprocess could not be deleted even after
    it was measured to add nothing.

    They have in fact been identical since tan-cli#290 retired the last check
    `--build` gated (`zephyrWorkspace`); `_collect` now accepts `build` and
    reads it nowhere. This pins that as a CONTRACT rather than an observation
    about one pin, so `contract/README.md` can say it and a consumer can act
    on it. Compared as `(name, scope)` pairs, not whole `Check`s: a status can
    legitimately differ between two runs on a busy host (a probe timing out
    once), the check SET cannot."""
    plain = [(c.name, c.scope) for c in doctor_cmd._collect(None, workspace_root=str(tmp_path))]
    build = [
        (c.name, c.scope)
        for c in doctor_cmd._collect(None, build=True, workspace_root=str(tmp_path))
    ]
    assert plain == build
