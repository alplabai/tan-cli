# SPDX-License-Identifier: Apache-2.0
"""Frozen issue-code gate: pins the Python-side spelling of every `frozen`
`issues[].code` string, and bans re-use of the one `retired` spelling.

`contract/issue-codes.json` is the single source: alp-sdk-vscode matches five
codes with `===` (tan-cli#106) and that match FAILS OPEN -- an unrecognised
code is indistinguishable from "no problem" on the consumer side, so a rename
or removal here is silent on both sides with CI green.
`crates/tan-cli/tests/contract.rs::frozen_issue_codes` already gates the Rust
emission sites against the same registry; this mirrors its two live
guarantees -- a frozen code's literal must still exist at EVERY one of its
emission sites, and a retired code's spelling must never be re-introduced
anywhere -- for the Python executor, which emits the same codes from
different files.

Not mirrored: the Rust gate's `reserved`-status check. A `reserved` code's
`consumer` is `"none"` (`contract/issue-codes.json`'s own definition of the
status) -- nobody matches it with `===` yet, so renaming or dropping it costs
nothing on the wire, unlike a `frozen` or `retired` spelling. The shared
registry itself is already kept honest against source drift by
`contract.rs::frozen_issue_codes` on the Rust side; pinning every reserved
code's Python site here too would just be duplicate bookkeeping for no
wire-safety gain.
"""

from __future__ import annotations

import ast
import json
import pathlib

#: `contract/` lives at the repo root, one level above `python/`.
REGISTRY = pathlib.Path(__file__).resolve().parents[3] / "contract" / "issue-codes.json"
TAN = pathlib.Path(__file__).resolve().parents[2] / "tan"

#: Every site each FROZEN code's literal must be found at, on the Python
#: side. Most codes have exactly one; `bootstrap.prerequisites-missing`,
#: `bootstrap.python-not-runnable` and `bootstrap.python-too-old` each have
#: two -- a bare-suffix `PrereqFailure` in `core/bootstrap.py` AND a
#: full-dotted `Check(code=...)` in `commands/doctor_cmd.py` that also
#: reaches `issues[].code` verbatim (`doctor_cmd.py::checks_to_issues`) --
#: and EVERY pinned site must hold, not just one.
#:
#: `bootstrap.yocto-host` pins a literal wider than the bare suffix on
#: purpose: `commands/bootstrap_cmd.py` has a severity-`warning` sibling
#: (the mixed-board case) that reuses the same `"yocto-host"` suffix at a
#: different call site, and the registry's own `note` for this code says the
#: consumer also requires severity `error` -- the two sites are not
#: interchangeable, so the pin must not match both.
#:
#: `contract/issue-codes.json`'s own `emittedBy`/`literal` fields point at the
#: Rust sources instead (that is what `contract.rs::frozen_issue_codes`
#: checks) -- this is the Python-side equivalent pin, kept independently so a
#: rename on EITHER side is caught by its own language's gate.
FROZEN_LOCATIONS: dict[str, list[tuple[str, str]]] = {
    "bootstrap.yocto-host": [
        ("commands/bootstrap_cmd.py", 'ExitCode.VALIDATION_FAILURE, "yocto-host"'),
    ],
    "bootstrap.prerequisites-missing": [
        ("core/bootstrap.py", '"prerequisites-missing"'),
        ("commands/doctor_cmd.py", 'code="bootstrap.prerequisites-missing"'),
    ],
    "presets.sdk-root-unresolved": [
        ("commands/presets_cmd.py", '"presets.sdk-root-unresolved"'),
    ],
    "bootstrap.python-not-runnable": [
        ("core/bootstrap.py", '"python-not-runnable"'),
        ("commands/doctor_cmd.py", 'code="bootstrap.python-not-runnable"'),
    ],
    "bootstrap.python-too-old": [
        ("core/bootstrap.py", '"python-too-old"'),
        ("commands/doctor_cmd.py", 'code="bootstrap.python-too-old"'),
    ],
}


def _registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def _strip_comments(text: str) -> str:
    """Blank out whole-line `#` comments."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def _strip_docstrings(text: str) -> str:
    """Blank out module/class/function docstring line ranges. A docstring is
    Python's comment form too, but `_strip_comments` only catches `#` lines --
    without this, a literal that survives only in prose (not in executable
    code) would still count as a hit.

    Raises `SyntaxError` as-is on an unparseable file rather than falling back
    to the unstripped text: a fallback here would let a frozen literal that
    now lives ONLY in a docstring read as a live hit, which is exactly the
    fail-open this gate exists to close. The pinned files are this package's
    own sources, so a parse failure is itself worth failing the gate on."""
    tree = ast.parse(text)
    lines = text.splitlines()
    nodes: list[ast.AST] = [tree]
    nodes.extend(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    for node in nodes:
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            end = first.end_lineno or first.lineno
            for lineno in range(first.lineno, end + 1):
                lines[lineno - 1] = ""
    return "\n".join(lines)


def _frozen_code_lines(text: str) -> str:
    """Comments AND docstrings blanked out -- airtight for the FROZEN check: a
    renamed call site must not read as unchanged just because the old
    spelling still sits in a nearby docstring."""
    return _strip_comments(_strip_docstrings(text))


def _retired_code_lines(text: str) -> str:
    """Comments only, deliberately NOT docstrings -- the opposite direction
    from the frozen check on purpose. Here, a retired spelling that survives
    only in a docstring should still count as a hit: a false FAILURE (flagging
    prose) is the safe side of this gate, where for the frozen check above a
    false PASS (missing a real rename) is the dangerous side."""
    return _strip_comments(text)


def _missing_frozen_literals(codes: list[dict], tan_root: pathlib.Path) -> list[str]:
    """Every FROZEN code whose pinned literal cannot be found at EVERY site
    `FROZEN_LOCATIONS` names for it -- unpinned, moved, or renamed at any one
    of possibly several live emission sites."""
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
        for rel, literal in FROZEN_LOCATIONS[code]:
            path = tan_root / rel
            if not path.is_file():
                offenders.append(f"{code}: pinned file {rel} does not exist")
                continue
            try:
                text = _frozen_code_lines(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError as exc:
                offenders.append(f"{code}: {rel} does not parse ({exc}) -- cannot verify pin")
                continue
            if literal not in text:
                offenders.append(
                    f"{code}: {literal!r} no longer found in {rel} outside comments/docstrings"
                )
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


def test_frozen_and_retired_issue_codes_stay_pinned():
    codes = _registry()["issueCodes"]
    frozen = [c for c in codes if c["status"] == "frozen"]
    retired = [c for c in codes if c["status"] == "retired"]
    assert frozen and retired, (
        "registry has no frozen/retired codes -- this gate would be vacuous"
    )

    offenders = _missing_frozen_literals(codes, TAN) + _reused_retired_spellings(codes, TAN)
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
