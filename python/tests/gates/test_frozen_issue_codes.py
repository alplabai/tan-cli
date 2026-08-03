# SPDX-License-Identifier: Apache-2.0
"""Python-side registry->source gate: every `contract/issue-codes.json` entry
whose emission site lives in `python/` must still be emitted there, and the one
`retired` spelling must never be re-introduced.

`contract/issue-codes.json` is the single source: alp-sdk-vscode matches five
codes with `===` (tan-cli#106) and that match FAILS OPEN -- an unrecognised
code is indistinguishable from "no problem" on the consumer side, so a rename
or removal here is silent on both sides with CI green.

WHO CHECKS WHAT (tan-cli#363). `crates/tan-cli/tests/contract.rs::frozen_issue_codes`
walks the SAME registry, but only checks entries whose `emittedBy` names Rust
source: there, `literal` is a verbatim slice of `crates/`, and `crates/` is
frozen (it ships to nobody -- the release assets are PyInstaller freezes of
`python/tan`, tan-cli#271), so a substring needle cannot rot under a reformat.
Every entry whose `emittedBy` names a `python/` path is DELEGATED to this file,
which parses the emission with `ast` instead. The Rust gate asserts that
delegation is total and that this file still defines
[`test_every_python_side_registry_entry_is_still_emitted`]; this file asserts
the mirror (nothing is owned by neither side), so repointing an entry at a
third kind of path reddens both.

WHY the split exists rather than one shared needle. The Rust gate used to
substring-check python-side entries too, and their `literal` field is not a
needle at all -- it is a prose DESCRIPTION of the emission shape
(`Issue("sdk.network-required", "warning", ...)`,
`f"support-bundle.{c.name}" where c.name == "boardYaml"`). `sdk.network-required`
is what surfaced it (tan-cli#363): its emission was rewrapped from one line to
four, nothing about the code changed, and `cargo test --locked --workspace`
went red on Linux, Windows AND macOS claiming a live registered code was gone.
That was never one stale row -- MEASURED while fixing it, 42 of the 198
python-side entries failed the identical substring check; the Rust gate only
ever reported the first because `assert!` panics. Formatting had become part of
the wire contract. An `ast` parse cannot be broken by a line wrap, a named
argument, or a comment.

WHAT THIS DOES NOT PROVE, stated plainly (inherited from the Rust gate's own
wording): that the code still REACHES the wire. It proves the spelling still
exists at the emission site. A refactor that deletes the whole refusal branch
but leaves the string behind passes here; the command's own tests cover the
emission, this covers the spelling.
"""

from __future__ import annotations

import ast
import json
import pathlib

# `python/tests/gates/` is on `sys.path` under pytest's default prepend import
# mode (no `__init__.py` here), so the sibling gate imports as a plain module.
# Imported for its DECLARED table only -- no scan runs at import time, so this
# file does not inherit that gate's `expected_calls` pins, which need a hand
# bump whenever an unrelated call site is added.
from test_every_issue_code_is_registered import _FORWARDER_SUFFIXES, _rel

#: `contract/` lives at the repo root, one level above `python/`.
REGISTRY = pathlib.Path(__file__).resolve().parents[3] / "contract" / "issue-codes.json"
REPO_ROOT = REGISTRY.parent.parent
TAN = pathlib.Path(__file__).resolve().parents[2] / "tan"

#: Every file each FROZEN code must still be emitted from, on the Python side,
#: relative to `python/tan/`. Most codes have exactly one;
#: `bootstrap.prerequisites-missing`, `bootstrap.python-not-runnable` and
#: `bootstrap.python-too-old` each have two -- a bare-suffix `PrereqFailure` in
#: `core/bootstrap.py` AND a full-dotted `Check(code=...)` in
#: `commands/doctor_cmd.py` that also reaches `issues[].code` verbatim
#: (`doctor_cmd.py::checks_to_issues`) -- and EVERY listed site must hold, not
#: just one.
#:
#: Was a `(file, source-literal)` pair until tan-cli#363. Three of the eight
#: needles spanned two tokens (`ExitCode.VALIDATION_FAILURE, "yocto-host"`,
#: `code="bootstrap.python-too-old"`), so a formatter rewrapping the call would
#: have reported a live frozen code as gone -- the exact defect #363 filed
#: against the Rust gate, sitting here too. The literals are gone; the check is
#: [`_unemitted_reason`], which parses.
#:
#: `bootstrap.yocto-host` has a severity-`warning` sibling in the same file
#: reusing the same `"yocto-host"` suffix (the mixed-board case), and the
#: registry's own `note` says the consumer also requires severity `error` -- the
#: two sites are not interchangeable. A file-level "is it still emitted" check
#: cannot tell them apart, so that one discrimination is pinned separately and
#: structurally by
#: [`test_the_yocto_host_refusal_site_keeps_its_error_severity`].
#:
#: `contract/issue-codes.json`'s own `emittedBy`/`literal` fields point at the
#: Rust sources for these five instead (that is what
#: `contract.rs::frozen_issue_codes` checks) -- this is the Python-side
#: equivalent pin, kept independently so a rename on EITHER side is caught by
#: its own language's gate.
FROZEN_LOCATIONS: dict[str, list[str]] = {
    "bootstrap.yocto-host": ["commands/bootstrap_cmd.py"],
    "bootstrap.prerequisites-missing": ["core/bootstrap.py", "commands/doctor_cmd.py"],
    "presets.sdk-root-unresolved": ["commands/presets_cmd.py"],
    "bootstrap.python-not-runnable": ["core/bootstrap.py", "commands/doctor_cmd.py"],
    "bootstrap.python-too-old": ["core/bootstrap.py", "commands/doctor_cmd.py"],
}


def _registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def _strip_comments(text: str) -> str:
    """Blank out whole-line `#` comments."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def _retired_code_lines(text: str) -> str:
    """Comments only, deliberately NOT docstrings, and deliberately still a
    TEXT scan rather than the `ast` parse the frozen/reserved direction now
    uses -- both asymmetries point the same way. A retired spelling that
    survives only in a docstring should still count as a hit: a false FAILURE
    (flagging prose) is the safe side of this gate, where for the
    still-emitted checks a false PASS (missing a real removal) is the
    dangerous side. And "this exact quoted token must appear NOWHERE" is a
    question a substring scan answers correctly -- a line wrap cannot split a
    single quoted string literal, so the tan-cli#363 shape-blindness has no
    grip here."""
    return _strip_comments(text)


def _string_constants(path: pathlib.Path) -> set[str]:
    """Every `str` constant in `path`'s AST, minus docstrings and any other
    bare string statement.

    The formatting-immune replacement for the substring needles tan-cli#363
    broke: `ast` sees a call's arguments the same whether they sit on one line
    or six, whether they are positional or named, and it never sees a `#`
    comment at all. Docstrings ARE `ast` nodes, so they are excluded here by
    hand (an `ast.Expr` whose value is a string constant) -- otherwise a code
    that now survives only in prose about itself would read as live, the
    fail-open this gate exists to close.

    Deliberate ceiling: this is every string constant in the file, not only
    the ones in a code position. Narrowing it further means reproducing
    `test_every_issue_code_is_registered.py`'s declared `_RESOLVABLE_HELPERS`/
    `_FULL_CODE_CALLABLES` tables and their hand-bumped call counts, and that
    gate already owns the source->registry direction with them. For THIS
    direction the question is only "did the spelling disappear", where a
    coincidental unrelated string of the same spelling is a rare false PASS --
    strictly less bad than the false FAILURE the old text needle produced on
    every reformat, and never worse than the substring scan it replaces, which
    matched the same spelling anywhere in the file too.
    """
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    }


def _assembled_suffixes() -> dict[str, frozenset[str]]:
    """Collapses the sibling gate's `(file, expression)` keys to `file`: this
    direction only asks whether the FILE can still produce the code, not which
    forwarded expression inside it does."""
    merged: dict[str, frozenset[str]] = {}
    for (rel, _expr), spec in _FORWARDER_SUFFIXES.items():
        merged[rel] = merged.get(rel, frozenset()) | spec["suffixes"]
    return merged


#: `tan/...` relative path -> every suffix a code assembled IN THAT FILE from a
#: value defined ELSEWHERE can carry. Read straight off the sibling gate's
#: `_FORWARDER_SUFFIXES`, the declared, source-verified home of exactly this
#: fact, rather than re-derived here: `support_bundle_cmd.py`'s
#: `Issue(f"support-bundle.{c.name}", ...)` takes its 20 `c.name`s from
#: `doctor_cmd.py`'s own `Check(...)` list, so those suffixes are genuinely not
#: string constants in the file that emits them.
_ASSEMBLED_SUFFIXES: dict[str, frozenset[str]] = _assembled_suffixes()


def _unemitted_reason(path: pathlib.Path, code: str) -> str | None:
    """`None` when `code` is still emitted by `path`, else a one-line reason.

    Four shapes are accepted, and between them they cover all 198 python-side
    registry entries (measured, tan-cli#363) -- the registry's own policy is
    that a dynamically assembled code is registered whole even though no whole
    literal exists anywhere, so the assembled shapes have to be recognised, not
    ignored:

      1. WHOLE code as a string constant -- `Issue("sdk.network-required", ...)`
         however it is wrapped, `code="kconfig.emit-failed"`, or a module
         constant (`DEFERRED_ISSUE_CODE = "cli.command-deferred"`).
      2. BARE SUFFIX as a string constant -- the dominant shape: a prefixing
         helper is handed `"venv-unusable"` / `"yocto-host"` / `"boardYaml"`
         and assembles `family.` + suffix itself.
      3. MIRRORED family template -- `west_forward_cmd.py`'s
         `f"{subcommand}.failed"`, where the FAMILY is substituted and the
         suffix is fixed: both halves must be present as constants (`"migrate"`
         and `".failed"`), so a stale `bogus.failed` row still fails.
      4. CROSS-FILE assembled -- the fixed prefix (`"support-bundle."`) is a
         constant here but the suffix is defined in another module; the suffix
         must appear in that file's declared `_ASSEMBLED_SUFFIXES` set, so a
         fabricated `support-bundle.bogus` row still fails.
    """
    if not path.is_file():
        return f"{code}: {path.name} does not exist"
    try:
        constants = _string_constants(path)
    except SyntaxError as exc:
        # Raised as a FAILURE, never swallowed: a fallback to a text scan here
        # would reintroduce exactly the shape-blindness this replaces, and the
        # scanned files are this package's own sources, so a parse failure is
        # itself worth failing on.
        return f"{code}: {path.name} does not parse ({exc}) -- cannot verify the emission"
    family, _, suffix = code.partition(".")
    if code in constants or suffix in constants:
        return None
    if family in constants and f".{suffix}" in constants:
        return None
    # `_rel` (the sibling gate's, reused) is the one spelling `_ASSEMBLED_SUFFIXES`
    # is keyed by, and it falls back to the path unchanged outside `tan/` -- so a
    # synthetic self-test file under pytest's `tmp_path` is reported, not crashed on.
    rel = _rel(path)
    if f"{family}." in constants and suffix in _ASSEMBLED_SUFFIXES.get(rel, frozenset()):
        return None
    return (
        f"{code}: neither {code!r} nor the bare suffix {suffix!r} is a string constant in "
        f"{rel} (nor an assembled-code shape this scan recognises) -- the emission is gone, "
        f"or it moved to another file and the registry's `emittedBy` is now stale"
    )


def _missing_frozen_emissions(codes: list[dict]) -> list[str]:
    """Every FROZEN code no longer emitted at EVERY site `FROZEN_LOCATIONS`
    names for it -- unpinned, moved, or renamed at any one of possibly several
    live emission sites."""
    offenders: list[str] = []
    for entry in codes:
        if entry["status"] != "frozen":
            continue
        code = entry["code"]
        if code not in FROZEN_LOCATIONS:
            # A frozen code with no pin at all is
            # test_the_pin_set_matches_the_registrys_frozen_codes' job -- one
            # owner per break, not duplicated here.
            continue
        for rel in FROZEN_LOCATIONS[code]:
            reason = _unemitted_reason(TAN / rel, code)
            if reason is not None:
                offenders.append(reason)
    return offenders


def _reused_retired_spellings(codes: list[dict], tan_root: pathlib.Path) -> list[str]:
    """Every RETIRED code whose bare suffix has been re-introduced as a string
    literal anywhere under `tan_root`."""
    offenders: list[str] = []
    for entry in codes:
        if entry["status"] != "retired":
            continue
        suffix = entry["code"].rsplit(".", 1)[-1]
        needles = (f'"{suffix}"', f"'{suffix}'")
        for path in sorted(tan_root.rglob("*.py")):
            text = _retired_code_lines(path.read_text(encoding="utf-8", errors="replace"))
            if any(needle in text for needle in needles):
                offenders.append(
                    f"{entry['code']}: re-appears in {path.relative_to(tan_root.parent)}"
                )
    return offenders


def _python_side_entries(codes: list[dict]) -> list[dict]:
    """Every registry entry the Rust gate delegates here: status `frozen` or
    `reserved` (a `retired` code has no emission site by definition) with an
    `emittedBy` under `python/`."""
    return [
        e
        for e in codes
        if e["status"] in ("frozen", "reserved") and (e.get("emittedBy") or "").startswith("python/")
    ]


def test_frozen_and_retired_issue_codes_stay_pinned():
    codes = _registry()["issueCodes"]
    frozen = [c for c in codes if c["status"] == "frozen"]
    retired = [c for c in codes if c["status"] == "retired"]
    assert frozen and retired, (
        "registry has no frozen/retired codes -- this gate would be vacuous"
    )

    offenders = _missing_frozen_emissions(codes) + _reused_retired_spellings(codes, TAN)
    assert not offenders, (
        "A frozen or retired issue code drifted on the Python side.\n"
        "FROZEN codes: alp-sdk-vscode matches them with `===` and that match "
        "FAILS OPEN -- silently. If a rename is deliberate: bump the CLI "
        "MAJOR/MINOR, update contract/issue-codes.json + CHANGELOG.md, and "
        "open the matching alp-sdk-vscode issue. Do NOT loosen the consumer "
        "to a prefix match.\n"
        "RETIRED codes: alp-sdk-vscode still maps the spelling to a permanent "
        "back-compat verdict for old pinned binaries "
        "(bootstrap.windows-unsupported -> 'Reopen in WSL'); reusing it for a "
        "different verdict corrupts that. Pick a different code.\n  "
        + "\n  ".join(offenders)
    )


def test_the_pin_set_matches_the_registrys_frozen_codes():
    """A second, independent gate on the gate itself: `FROZEN_LOCATIONS` above
    must name EXACTLY the registry's `frozen` codes, in both directions. A code
    promoted to `frozen` with no entry here would otherwise silently never be
    checked by the test above; a stale leftover entry for a demoted or removed
    code is debt the same way `test_the_allowlist_has_no_stale_entries` guards
    the hardware-fact allowlist in this same directory."""
    registry_frozen = {c["code"] for c in _registry()["issueCodes"] if c["status"] == "frozen"}
    pinned = set(FROZEN_LOCATIONS)
    assert pinned == registry_frozen, (
        "FROZEN_LOCATIONS has drifted from contract/issue-codes.json's frozen set.\n"
        f"  pinned here but not frozen in the registry: {sorted(pinned - registry_frozen)}\n"
        f"  frozen in the registry but not pinned here: {sorted(registry_frozen - pinned)}"
    )


def test_every_python_side_registry_entry_is_still_emitted():
    """tan-cli#363: the half of the registry->source direction `contract.rs`
    delegates here, because Rust cannot import Python's `ast` and a hand-rolled
    Python parser over there would be a weaker copy of the one that already
    lives in this directory.

    `contract.rs::frozen_issue_codes` pins THIS function by name and fails if
    it disappears, so deleting it cannot leave 198 entries owned by neither
    gate. The mirror assertion is below: every frozen/reserved entry must sit
    under `crates/` (checked there) or `python/` (checked here), so repointing
    one at a third kind of path reddens both sides rather than falling into a
    gap between them.
    """
    codes = _registry()["issueCodes"]

    unowned = [
        f"{e['code']}: emittedBy={e.get('emittedBy')!r}"
        for e in codes
        if e["status"] in ("frozen", "reserved")
        and not (e.get("emittedBy") or "").startswith(("crates/", "python/"))
    ]
    assert not unowned, (
        "issue-codes.json entr(ies) whose `emittedBy` is under neither `crates/` "
        "(checked by contract.rs::frozen_issue_codes) nor `python/` (checked "
        "here) -- they are gated by NOTHING. Point `emittedBy` at the real "
        "emission site:\n  " + "\n  ".join(unowned)
    )

    entries = _python_side_entries(codes)
    # Non-vacuity: the whole python-side half vanishing (a registry rewrite, a
    # path convention change) would otherwise make this gate pass by finding
    # nothing to check -- the tan-cli#275 lesson. 198 today; a floor, not a pin,
    # because every new command legitimately adds rows.
    assert len(entries) > 100, (
        f"only {len(entries)} python-side registry entries found -- the `emittedBy` "
        f"convention changed and this gate is now checking almost nothing"
    )

    offenders = [
        reason
        for e in entries
        if (reason := _unemitted_reason(REPO_ROOT / e["emittedBy"], e["code"])) is not None
    ]
    assert not offenders, (
        f"{len(offenders)} registry entr(ies) name a python/ emission site that no longer "
        "emits them. Either restore the emission, or update contract/issue-codes.json "
        "(for a `reserved` code that is a free rename -- nothing matches it with `===` "
        "yet; for a `frozen` one it is a wire break, see this file's docstring):\n  "
        + "\n  ".join(offenders)
    )


def test_the_yocto_host_refusal_site_keeps_its_error_severity():
    """The one discrimination [`_unemitted_reason`] structurally cannot make.

    `bootstrap_cmd.py` emits the `"yocto-host"` suffix TWICE: once through
    `_refusal(ExitCode.VALIDATION_FAILURE, "yocto-host", ...)` (the frozen
    `bootstrap.yocto-host` at severity `error`, which the consumer requires --
    see the registry's own `note`) and once through
    `log.warn("yocto-host", ...)` for the mixed-board case, deliberately the
    same spelling at severity `warning`. A file-level "is this suffix still
    emitted here" check passes on either one alone, so the ERROR site is
    pinned here by shape instead: the call, its argument POSITION and its exit
    code, all read from the AST rather than from one formatting of the line
    (which is what the old `'ExitCode.VALIDATION_FAILURE, "yocto-host"'` text
    needle did, and what tan-cli#363 is about).
    """
    tree = ast.parse((TAN / "commands/bootstrap_cmd.py").read_text(encoding="utf-8"), filename="bootstrap_cmd.py")
    sites = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "_refusal"
        and len(call.args) > 1
        and isinstance(call.args[1], ast.Constant)
        and call.args[1].value == "yocto-host"
    ]
    assert len(sites) == 1, (
        f"expected exactly 1 `_refusal(..., \"yocto-host\", ...)` call in "
        f"commands/bootstrap_cmd.py, found {len(sites)} at lines "
        f"{[c.lineno for c in sites]} -- the frozen error-severity site was removed "
        f"(a wire break: alp-sdk-vscode matches bootstrap.yocto-host with `===`), or a "
        f"second one appeared and it is no longer obvious which the consumer sees."
    )
    exit_code = ast.unparse(sites[0].args[0])
    assert exit_code == "ExitCode.VALIDATION_FAILURE", (
        f"commands/bootstrap_cmd.py:{sites[0].lineno} -- the frozen `bootstrap.yocto-host` "
        f"refusal now exits with {exit_code}, not ExitCode.VALIDATION_FAILURE. The registry's "
        f"note pins severity `error` for this code because the consumer requires it; the "
        f"severity-`warning` sibling (`log.warn(\"yocto-host\", ...)`, the mixed-board case) "
        f"is a DIFFERENT verdict that happens to share the spelling."
    )


def test_the_scan_reads_a_multiline_issue_call(tmp_path: pathlib.Path):
    """The tan-cli#363 regression fixture: the exact multiline `Issue(...)`
    shape `sdk_cmd.py` emits `sdk.network-required` from.

    Written under pytest's `tmp_path`, not under `TAN` -- a scratch file inside
    the production package is one interrupted run away from surviving into
    `python/tan/`, which every other test here globs (the same reasoning
    `test_every_issue_code_is_registered.py`'s own synthetic-source test
    records).

    Asserts BOTH directions, so this cannot pass by accident: the AST scan
    finds the code, and the needle the registry carried when #363 was filed
    does NOT match the fixture -- proving the fixture really reproduces the
    reported shape rather than an accidentally single-line form that would
    have satisfied the old scanner too.
    """
    fixture = tmp_path / "multiline_emission.py"
    fixture.write_text(
        "def _offline_list(json_mode):\n"
        "    return _emit(\n"
        "        json_mode=json_mode,\n"
        "        data=_list_data([]),\n"
        "        issues=[\n"
        "            Issue(\n"
        '                "sdk.network-required",\n'
        '                "warning",\n'
        '                "`sdk list` reports the Alp SDK releases published upstream "\n'
        '                "on GitHub -- there is no local/offline copy to answer from. "\n'
        '                "Add --online to fetch them.",\n'
        "            )\n"
        "        ],\n"
        "        exit_code=ExitCode.SUCCESS,\n"
        "    )\n",
        encoding="utf-8",
        newline="\n",
    )

    assert _unemitted_reason(fixture, "sdk.network-required") is None

    old_needle = 'Issue("sdk.network-required", "warning", ...)'
    assert old_needle not in fixture.read_text(encoding="utf-8"), (
        "this fixture no longer reproduces the tan-cli#363 shape -- the whole point is "
        "that the emission is WRAPPED, so the single-line needle cannot match it"
    )

    # And the negative, so the fixture cannot pass by matching anything: a code
    # the fixture does not emit is reported, with the file named.
    absent = _unemitted_reason(fixture, "sdk.never-emitted-here")
    assert absent is not None and "sdk.never-emitted-here" in absent, absent


def test_the_scan_rejects_a_code_that_is_no_longer_emitted(tmp_path: pathlib.Path):
    """tan-cli#275's standing lesson applied to this gate: an assertion nobody
    has watched fail is not proven to fire. Drives [`_unemitted_reason`] --
    the single predicate BOTH the frozen check and the python-side registry
    check route through -- across every accept/reject shape it declares, with
    synthetic source, so no hand-edit of the production tree is needed to know
    it can go red.
    """
    source = tmp_path / "shapes.py"
    source.write_text(
        '"""A docstring naming sdk.only-in-prose and "only-in-prose-suffix"."""\n'
        "\n"
        'WHOLE = "sdk.whole-literal"\n'
        "\n"
        "\n"
        "def emit(log, subcommand):\n"
        '    log.warn("bare-suffix", "x")            # family assembled by the helper\n'
        '    log.fail(f"{subcommand}.failed", "x")   # mirrored family template\n'
        '    log.note(f"support-bundle.{check}", "x")\n'
        '    return ["migrate"]\n',
        encoding="utf-8",
        newline="\n",
    )

    # Accepted, one per declared shape (see `_unemitted_reason`'s docstring).
    assert _unemitted_reason(source, "sdk.whole-literal") is None
    assert _unemitted_reason(source, "bootstrap.bare-suffix") is None
    assert _unemitted_reason(source, "migrate.failed") is None

    # Rejected: a spelling that exists ONLY in the docstring. This is the
    # fail-open the `_string_constants` docstring exclusion closes -- without
    # it, prose about a deleted code would keep its registry row looking live.
    assert _unemitted_reason(source, "sdk.only-in-prose") is not None
    assert _unemitted_reason(source, "bootstrap.only-in-prose-suffix") is not None

    # Rejected: a code nothing in the file mentions at all.
    gone = _unemitted_reason(source, "sdk.deleted-last-week")
    assert gone is not None and "deleted-last-week" in gone, gone

    # Rejected: the cross-file assembled shape is NOT a blanket pass for its
    # family -- `support-bundle.` is a constant here, but the suffix must still
    # be one the declared `_ASSEMBLED_SUFFIXES` set for that file carries, and
    # this synthetic path has no declared set at all.
    assert _unemitted_reason(source, "support-bundle.bogus") is not None

    # Rejected: a pinned file that does not exist (a moved emission site), and
    # one that does not parse -- both reported, never silently skipped.
    assert _unemitted_reason(tmp_path / "absent.py", "sdk.whole-literal") is not None
    broken = tmp_path / "broken.py"
    broken.write_text("def (\n", encoding="utf-8", newline="\n")
    unparseable = _unemitted_reason(broken, "sdk.whole-literal")
    assert unparseable is not None and "does not parse" in unparseable, unparseable
