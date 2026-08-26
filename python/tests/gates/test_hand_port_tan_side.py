# SPDX-License-Identifier: Apache-2.0
"""tan-cli#778: `HAND_PORT_HASHES` proves the UPSTREAM file has not moved. It
says nothing about whether tan's counterpart implements what was pinned.

The failure that motivated this shipped. `scripts/alp_cli/model.py` was pinned
at a hash that already carried alp-sdk#1271's `_PATH_OPT_KEYS` fix, while
tan's `_resolve_compile` was still pre-fix -- so `tan model build` resolved
DRP-AI's `input_shape` (`"1,3,224,224"`), `input_name` and `product` into
absolute filesystem paths, in a release, with that gate green the whole time.
Verifying one side of a two-sided correspondence cannot fail on the other
side; nothing had replaced the missing assurance.

WHAT THIS FILE ADDS, and what it deliberately does not.

1. The correspondence becomes MACHINE-READABLE. Until now the sibling gate
   described it in prose (`test_planner_relocation_freshness.py:630-662`),
   which no test can act on. `HAND_PORT_TAN_SIDE` names the tan file(s) for
   each upstream source.

2. EVERY CLASSIFICATION CARRIES INFORMATION, not just a key. Each
   `HAND_PORT_HASHES` key must be exactly one of: mapped to tan file(s) that
   exist, declared unfit for a file-level pairing with a reason THAT NAMES THE
   TAN FILE the port really lives in, or declared already-drifted with the
   issue tracking it -- and the last two tables are each bounded by a pinned
   literal, so neither can grow quietly.

   The first draft bounded only the drift ledger. Review then reproduced
   tan-cli#778's own failure one table over: moving all 19 keys into
   `HAND_PORT_NO_TAN_FILE_PAIRING` with the reason `"lol"` left the gate at
   `6 passed`, auditing nothing. A classification that costs nothing to write
   is not a classification.

3. A SYMBOL-LEVEL parity check, for one entry, as the worked example of the
   instrument that actually closes #778.

WHY NOT A TAN-SIDE FILE HASH, which is #778's own option 1. Measured over the
repo's whole life (first commit 2026-07-18, so 32 days as of writing -- an
earlier draft of this note said "90 days", a window longer than the repository
has existed): 42 distinct commits touched at least one tan file this table
maps, against 20 that touched the sibling gate at all. A tan-side sha256 reds
once per commit, so that is 42 reds, and almost none of them is about upstream
parity -- tan-cli#810 moved an `import yaml` inside `new_som_cmd.py` and would
have been one. A gate that fires that often for reasons it cannot explain is a
gate someone deletes, and the audit goes with it. So the tan-side pin here is
the MAPPING, not the bytes.

WHY SYMBOL-LEVEL, and its honest limit. It is what actually caught #1271:
review extracted both `_resolve_compile` implementations and compared them.
"Do these two pieces of code still agree?" survives reformatting and unrelated
edits elsewhere in the same file, which is the noise a file hash cannot tell
from drift. It is NOT noise-free: tan-cli#810's import move would also have
redded a symbol check over `new_som_cmd`, had one existed. The claim is that
it reds for a REASON A READER CAN ACT ON, not that it never reds. `explain.py`
is done here as the template; the remaining entries are #778's follow-up.
"""
from __future__ import annotations

import ast
import pathlib
import re
import re

import pytest

from tests.gates.test_planner_relocation_freshness import (
    HAND_PORT_HASHES,
    HAND_PORT_SDK_ROOT_ENV,
    _hand_port_sdk_root,
)

#: `python/` -- the package root every tan-side path below is relative to.
PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: alp-sdk source -> the tan file(s) that implement it. Only entries whose
#: correspondence is CLEAN belong here: one upstream module answered by files
#: that exist to implement it, not a region buried in a file doing unrelated
#: work. An entangled pairing goes in `HAND_PORT_NO_TAN_FILE_PAIRING` with the
#: reason, because naming it here would imply a precision this mapping does
#: not have.
HAND_PORT_TAN_SIDE: dict[str, tuple[str, ...]] = {
    "scripts/gen_zephyr_board.py": ("tan/planner/zephyr_board.py",),
    "scripts/alp_project_loader.py": (
        "tan/planner/project_loader.py",
        "tan/planner/som_metadata.py",
    ),
    "scripts/alp_template.py": ("tan/planner/template.py",),
    # BOTH halves, because the mapped file disclaims the second one:
    # `project_emit/__init__.py:15` says `_CHIP_SUBSYSTEMS` is "already in
    # `slugs.py`", and `slugs.py:188` claims the relocation in the same words
    # every other mapped file uses. Naming only the first would have been the
    # incomplete-mapping defect #778 is about.
    "scripts/alp_project_emit/__init__.py": (
        "tan/planner/project_emit/__init__.py",
        "tan/planner/slugs.py",
    ),
    "scripts/alp_project_emit/bom_netlist.py": ("tan/planner/project_emit/bom_netlist.py",),
    "scripts/alp_project_emit/dts.py": ("tan/planner/project_emit/dts.py",),
    "scripts/alp_project_emit/hw_info.py": ("tan/planner/project_emit/hw_info.py",),
    "scripts/alp_project_emit/native_sim.py": ("tan/planner/project_emit/native_sim.py",),
    # Same shape: `project_emit/west_libs.py:7-11` says only the west.yml half
    # came across and that `_emit_library_hw_backends` + `_SOC_FAMILY_TOKEN`
    # went to `libraries.py`, which claims them at `libraries.py:104`.
    "scripts/alp_project_emit/west_libs.py": (
        "tan/planner/project_emit/west_libs.py",
        "tan/planner/libraries.py",
    ),
    # The `scripts/alp_cli/*` half -- the nine #778 is about. Each of these was
    # unmapped before this file existed.
    "scripts/alp_cli/faultdecode.py": (
        "tan/core/faultdecode.py",
        "tan/commands/faultdecode_cmd.py",
    ),
    "scripts/alp_cli/new_som.py": ("tan/commands/new_som_cmd.py",),
    "scripts/alp_cli/monitor.py": ("tan/commands/monitor_cmd.py",),
    # `tan/core/error_catalog.py` ONLY. `explain_cmd.py` also touches this
    # upstream module, but ~80% of it is template registries ported from the
    # retired Rust oracle with no upstream-Python origin at all; pairing it
    # here would attribute that prose to `explain.py`.
    "scripts/alp_cli/explain.py": ("tan/core/error_catalog.py",),
    # Lopsided, and worth knowing before reading a red here as a port failure:
    # 66 of `doctor.py`'s 832 lines crossed (`_check_libraries`). The upstream
    # hash therefore also moves for the ~24 checks that never came over.
    "scripts/alp_cli/doctor.py": ("tan/core/doctor_libraries.py",),
}

#: Upstream sources whose tan counterpart is real but is NOT a file. A
#: file-level pairing would red on every unrelated edit to a large, actively
#: changed module, so claiming one would be worse than declaring the gap.
#: These are the entries #778's follow-up must reach with symbol-level checks.
#:
#: Each reason MUST name the `tan/...py` file the port actually lives in, and
#: that file must exist -- an entry here still has to say where our side is,
#: it is only declining to pin the whole file. `_PAIRING_MAY_ONLY_CONTAIN`
#: below bounds the set the same way the drift ledger is bounded.
HAND_PORT_NO_TAN_FILE_PAIRING: dict[str, str] = {
    "scripts/alp_cli/diagnostic_format.py": (
        "the port is a ~150-line region inside tan/commands/validate_cmd.py "
        "(1613 lines), a file under active change for stderr parsing, "
        "exit-code classification and spawn behaviour"
    ),
    "scripts/alp_cli/validate.py": (
        "no real counterpart: tan/commands/validate_cmd.py is a port of the retired Rust "
        "oracle's validate.rs. What crossed is the --format human/json/sarif "
        "concept, which tan deliberately diverges on, plus two indent=2 calls"
    ),
    "scripts/sentinels.py": (
        "INLINED, not ported as a file: `sentinels.is_tbd` is spelled "
        "`_is_tbd` inside tan/planner/zephyr_board.py, which this table "
        "already pairs with scripts/gen_zephyr_board.py. Pairing it again "
        "here would pin one file to two upstream sources -- the same shape "
        "as validator.py below"
    ),
    "scripts/alp_cli/validator.py": (
        "load_board_schema/iter_schema_errors crossed into tan/planner/"
        "loader.py, 24 lines of 1313 -- and that file is already pinned in "
        "PINNED_HASHES against scripts/alp_orchestrate/loader.py, so a second "
        "file pairing would double-pin one file to two upstream sources"
    ),
}

#: Upstream sources whose tan counterpart is KNOWN to be wrong right now.
#: An entry here is a defect with a tracking issue, not an exemption.
#:
#: A sentence saying "this may only shrink" is not a mechanism -- review
#: demonstrated the hole by moving all 19 pins in here with the reason string
#: "lol tan-cli#" and watching every test still pass, which is #778's own
#: failure reproduced one file later. So the permitted contents are pinned as
#: a literal in `_LEDGER_MAY_ONLY_CONTAIN` below: growing this table requires
#: editing that set too, in a diff a reviewer sees.
# `scripts/alp_cli/model.py` was here (tan-cli#777: tan/commands/model_cmd.py's
# _resolve_compile was pre-alp-sdk#1271, corrupting DRP-AI's
# input_shape/input_name/product into filesystem paths). ADR-0028
# (tan-cli#791) relocated the engine into `tan/model/` and deleted the
# alp-sdk original -- there is no upstream file left to pin, so this entry
# leaves the ledger rather than moving to HAND_PORT_TAN_SIDE (matching
# `test_planner_relocation_freshness.py`'s HAND_PORT_HASHES comment for the
# same file). Verified fixed, not just orphaned: #791's
# `tan/commands/model_cmd.py` already carries `_PATH_OPT_KEYS = {"config",
# "calibration", "images", "spec"}`, the alp-sdk#1271 restriction #777 was
# tracking.
HAND_PORT_KNOWN_DRIFT: dict[str, str] = {}


#: The ONLY hand-ports allowed to decline a file-level pairing. Pinned for the
#: reason the drift ledger is: review demonstrated that an unbounded table is
#: an escape hatch, not a classification.
_PAIRING_MAY_ONLY_CONTAIN: frozenset[str] = frozenset(
    {
        "scripts/alp_cli/diagnostic_format.py",
        "scripts/alp_cli/validate.py",
        "scripts/alp_cli/validator.py",
        "scripts/sentinels.py",
    }
)

#: A tan-side path, as written in a reason string.
_TAN_PATH = re.compile(r"tan/[\w/]+\.py")


def test_the_unpairable_table_cannot_grow_without_saying_so():
    """The bound review asked for. Same mechanism as the drift ledger: adding
    an entry means editing this literal in the same diff, where a reviewer
    sees it."""
    added = sorted(set(HAND_PORT_NO_TAN_FILE_PAIRING) - _PAIRING_MAY_ONLY_CONTAIN)
    assert not added, (
        "these hand-ports were declared unpairable without being added to "
        "_PAIRING_MAY_ONLY_CONTAIN:\n  " + "\n  ".join(added) + "\n\n"
        "Declining to pin a file is a judgement about that port's shape, not "
        "a way out of a red. Say in review why a file-level pairing is the "
        "wrong instrument for it."
    )
    removed = sorted(_PAIRING_MAY_ONLY_CONTAIN - set(HAND_PORT_NO_TAN_FILE_PAIRING))
    assert not removed, (
        "these are permitted to decline a pairing but no longer do -- drop "
        "them from _PAIRING_MAY_ONLY_CONTAIN too:\n  " + "\n  ".join(removed)
    )


def test_every_unpairable_reason_names_the_tan_file_it_lives_in():
    """An entry here declines to pin a FILE; it does not get to decline saying
    where our side is. Review moved all 19 keys in with the reason `"lol"` and
    the gate stayed green -- that is the hole, and a non-empty check does not
    close it."""
    bad = []
    for source, reason in HAND_PORT_NO_TAN_FILE_PAIRING.items():
        named = _TAN_PATH.findall(reason)
        if not named:
            bad.append(f"{source}: names no tan/...py file")
            continue
        missing = [n for n in named if not (PACKAGE_ROOT / n).is_file()]
        if missing:
            bad.append(f"{source}: names {missing}, which do not exist")
    assert not bad, (
        "every HAND_PORT_NO_TAN_FILE_PAIRING reason must name the tan file "
        "the port actually lives in, and that file must exist:\n  "
        + "\n  ".join(bad)
    )


def test_every_pinned_hand_port_says_which_tan_code_it_governs():
    """The rule #778 exists for: an upstream pin with no declared tan side is
    an audit of nothing. Every `HAND_PORT_HASHES` key must land in exactly one
    of the three tables above."""
    tables = {
        "HAND_PORT_TAN_SIDE": set(HAND_PORT_TAN_SIDE),
        "HAND_PORT_NO_TAN_FILE_PAIRING": set(HAND_PORT_NO_TAN_FILE_PAIRING),
        "HAND_PORT_KNOWN_DRIFT": set(HAND_PORT_KNOWN_DRIFT),
    }
    declared = set().union(*tables.values())

    unclassified = sorted(set(HAND_PORT_HASHES) - declared)
    assert not unclassified, (
        "these HAND_PORT_HASHES entries pin an alp-sdk file without saying "
        "which tan code it governs, so nothing can check our side of the "
        "port (tan-cli#778):\n  " + "\n  ".join(unclassified) + "\n\n"
        "Add each to HAND_PORT_TAN_SIDE if the correspondence is a file or "
        "two, to HAND_PORT_NO_TAN_FILE_PAIRING with the reason if it is a "
        "region inside a busy module, or to HAND_PORT_KNOWN_DRIFT with the "
        "tracking issue if our side is currently wrong."
    )

    stale = sorted(declared - set(HAND_PORT_HASHES))
    assert not stale, (
        "these entries declare a tan side for an alp-sdk source that is no "
        "longer pinned in HAND_PORT_HASHES -- drop them in the same change "
        "that dropped the pin:\n  " + "\n  ".join(stale)
    )

    hollow = sorted(source for source, rels in HAND_PORT_TAN_SIDE.items() if not rels)
    assert not hollow, (
        "these HAND_PORT_TAN_SIDE entries name NO tan file, so they are "
        "classified without declaring anything -- the silent third state this "
        "test exists to close, wearing a dict key. When a mapped file is "
        "renamed, repair the path; emptying the tuple is the cheaper edit and "
        "it is what this assertion is here to stop:\n  " + "\n  ".join(hollow)
    )

    blank = sorted(
        source
        for source, reason in HAND_PORT_NO_TAN_FILE_PAIRING.items()
        if not reason.strip()
    )
    assert not blank, (
        "these HAND_PORT_NO_TAN_FILE_PAIRING entries declare the gap with no "
        "reason, which is an unclassified pin with extra steps:\n  "
        + "\n  ".join(blank)
    )

    for name, keys in tables.items():
        for other_name, other_keys in tables.items():
            if name >= other_name:
                continue
            both = sorted(keys & other_keys)
            assert not both, (
                f"{both} is in both {name} and {other_name}. The three "
                "states are exclusive: a mapped port is not also unpairable, "
                "and a port known to be wrong is not also audited."
            )


def test_every_declared_tan_counterpart_exists_on_disk():
    """A mapping to a file that was renamed or deleted is worse than no
    mapping: it reads as coverage. Checked without an alp-sdk checkout, so
    this half runs everywhere, including the `python` job that binds no root."""
    missing = [
        f"{source} -> {rel}"
        for source, rels in HAND_PORT_TAN_SIDE.items()
        for rel in rels
        if not (PACKAGE_ROOT / rel).is_file()
    ]
    assert not missing, (
        "HAND_PORT_TAN_SIDE names tan files that do not exist. If one was "
        "renamed, rename it here; if the port was retired, drop the upstream "
        "pin in the same change:\n  " + "\n  ".join(missing)
    )


#: The ONLY hand-ports allowed to sit in `HAND_PORT_KNOWN_DRIFT`. Pinned as a
#: literal so the ledger cannot grow quietly: adding an entry means editing
#: this set in the same diff, with the reason visible to a reviewer.
_LEDGER_MAY_ONLY_CONTAIN: frozenset[str] = frozenset()

#: A tracking reference is `<repo>#<number>`. A bare `tan-cli#` passed the
#: substring test this replaced, so the reason string could name no issue at
#: all and still read as tracked.
_TRACKING_REF = re.compile(r"(tan-cli|alp-sdk)#[0-9]+")


def test_the_known_drift_ledger_names_a_tracking_issue_for_every_entry():
    """`HAND_PORT_KNOWN_DRIFT` is a defect list, not an allowlist. An entry
    with no issue behind it is an exemption wearing a comment, and this file
    exists because an invisible wrong port is how the corruption shipped."""
    untracked = sorted(
        source
        for source, reason in HAND_PORT_KNOWN_DRIFT.items()
        if not _TRACKING_REF.search(reason)
    )
    assert not untracked, (
        "these known-drifted hand-ports name no tracking issue (`tan-cli#N` or "
        "`alp-sdk#N`):\n  "
        + "\n  ".join(untracked)
        + "\nEvery entry must say what is wrong and where the fix is tracked."
    )


def test_the_known_drift_ledger_cannot_grow_without_saying_so():
    """The shrink invariant, as a mechanism rather than a sentence.

    Review reproduced #778's own failure against the first draft of this file:
    moving all 19 pins into the ledger with the reason `"lol tan-cli#"` left
    every test green, turning the ledger into an unbounded exemption table
    whose only effect is red-to-green. Pinning the permitted set means a new
    entry cannot be added without a reviewer seeing this literal change.
    """
    added = sorted(set(HAND_PORT_KNOWN_DRIFT) - _LEDGER_MAY_ONLY_CONTAIN)
    assert not added, (
        "these hand-ports were declared known-drifted without being added to "
        "_LEDGER_MAY_ONLY_CONTAIN:\n  " + "\n  ".join(added) + "\n\n"
        "A hand-port known to be wrong is a defect, not an exemption. If the "
        "drift is real, add it to that set in the same change and say in "
        "review why it is being accepted rather than fixed."
    )
    resolved = sorted(_LEDGER_MAY_ONLY_CONTAIN - set(HAND_PORT_KNOWN_DRIFT))
    assert not resolved, (
        "these entries are permitted in the ledger but are no longer in it -- "
        "the drift was fixed, so drop them from _LEDGER_MAY_ONLY_CONTAIN too "
        f"and add the pairing to HAND_PORT_TAN_SIDE:\n  " + "\n  ".join(resolved)
    )


# ---------------------------------------------------------------------------
# The worked example: symbol-level parity for one entry.
# ---------------------------------------------------------------------------


def _upstream_ast(rel_path: str) -> ast.Module:
    """Parse an upstream module WITHOUT importing it. Importing would need the
    SDK's own third-party dependencies (click, colorama) resolvable from this
    interpreter, which is not something a gate should require -- and the
    question here is about source, not runtime behaviour."""
    return ast.parse((_hand_port_sdk_root() / rel_path).read_text(encoding="utf-8"))


def _module_constant(tree: ast.Module, name: str):
    """The single module-level binding of `name`. UNIQUENESS IS ASSERTED: an
    extractor that takes the first of several matches can be pointed at a
    symbol that is not the live one, which review demonstrated by drifting all
    three checked things at once behind decoys while this file stayed green."""
    found = []
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target] if isinstance(node, ast.AnnAssign) and node.value else []
        )
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                found.append(ast.literal_eval(node.value))
    if not found:
        raise AssertionError(f"no module-level constant {name!r} in the module")
    if len(found) > 1:
        raise AssertionError(
            f"{name!r} is bound {len(found)} times at module scope; this probe "
            "cannot tell which one is live, so it refuses to guess"
        )
    return found[0]


def _call_keywords(tree: ast.Module, dotted: str) -> dict[str, object]:
    """The keyword arguments of the first call to `dotted` anywhere in the
    module. Upstream spells the tuning inline (`n=5, cutoff=0.4`) where tan
    lifted it into named constants, so the comparison has to reach into the
    call rather than expect a matching constant."""
    want = dotted.split(".")
    found: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        parts: list[str] = []
        func = node.func
        while isinstance(func, ast.Attribute):
            parts.append(func.attr)
            func = func.value
        if isinstance(func, ast.Name):
            parts.append(func.id)
        if list(reversed(parts)) == want:
            found.append(
                {
                    kw.arg: ast.literal_eval(kw.value)
                    for kw in node.keywords
                    if kw.arg is not None
                }
            )
    if not found:
        raise AssertionError(f"no call to {dotted!r} in the module")
    if len(found) > 1:
        raise AssertionError(
            f"{dotted!r} is called {len(found)} times in this module. `ast.walk` "
            "is breadth-first, so 'the first call' is not source order and a "
            "decoy can outrank the live one -- name the call site explicitly "
            "rather than letting this probe pick"
        )
    return found[0]


def _call_keyword_sources(tree: ast.Module, dotted: str) -> dict[str, str]:
    """Like `_call_keywords`, but returns each keyword's SOURCE rather than its
    value -- so a keyword passed as a name (`n=SUGGESTION_LIMIT`) is readable,
    where `literal_eval` would refuse it.

    This is what lets the parity check reach tan's own CALL SITE. Comparing
    upstream's literals against tan's CONSTANTS alone leaves the constants
    correct while the call passes something else, which is a behavioural break
    with the gate green -- the shape this whole file exists to end.
    """
    want = dotted.split(".")
    found: list[dict[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        parts: list[str] = []
        func = node.func
        while isinstance(func, ast.Attribute):
            parts.append(func.attr)
            func = func.value
        if isinstance(func, ast.Name):
            parts.append(func.id)
        if list(reversed(parts)) == want:
            found.append(
                {kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg is not None}
            )
    if len(found) != 1:
        raise AssertionError(
            f"{dotted!r} is called {len(found)} times in this module; this "
            "probe refuses to guess which one is live"
        )
    return found[0]


def _function_body_source(tree: ast.Module, name: str) -> str:
    """A function's body with its docstring dropped, unparsed back to source.
    `ast.unparse` normalises formatting, so this compares what the code DOES
    and not how either repo happens to lay it out."""
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    if len(matches) > 1:
        raise AssertionError(
            f"{name!r} is defined {len(matches)} times in this module (a "
            "redefinition, or a nested def of the same name); this probe "
            "refuses to guess which one is live"
        )
    for node in matches:
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]
            return "\n".join(ast.unparse(stmt) for stmt in body)
    raise AssertionError(f"no function {name!r} in the module")


def test_explain_hand_port_still_agrees_with_its_upstream_symbols():
    """`scripts/alp_cli/explain.py` -> `tan/core/error_catalog.py`, checked at
    the level the correspondence is actually claimed at.

    This is the instrument #778 asks for, sized to one entry so the next
    person has a template rather than a description. It compares three things
    the tan module's own comments assert are verbatim, and it does NOT compare
    the enclosing files -- `error_catalog.py` may grow docstrings, exceptions
    and helpers of its own without any of that being drift.

    Skips visibly without a bound checkout, same contract as the sibling
    gate: a run that never bound the root is not set up to do this audit.
    """
    upstream = _upstream_ast("scripts/alp_cli/explain.py")

    from tan.core import error_catalog

    assert tuple(tuple(pair) for pair in error_catalog.FIELD_ORDER) == tuple(
        tuple(pair) for pair in _module_constant(upstream, "_FIELD_ORDER")
    ), (
        "tan/core/error_catalog.py's FIELD_ORDER no longer matches explain.py's "
        "_FIELD_ORDER. This is user-visible render order; port the upstream "
        "change or record the divergence deliberately."
    )

    tuning = _call_keywords(upstream, "difflib.get_close_matches")
    assert (error_catalog.SUGGESTION_LIMIT, error_catalog.SUGGESTION_CUTOFF) == (
        tuning["n"],
        tuning["cutoff"],
    ), (
        "the did-you-mean tuning drifted from explain.py's "
        f"difflib.get_close_matches({tuning}). The suggestion list is "
        "user-visible output: a different cutoff silently changes what a typo "
        "is answered with."
    )

    tan_tree = ast.parse(
        (PACKAGE_ROOT / "tan/core/error_catalog.py").read_text(encoding="utf-8")
    )

    # The constants being right is not enough: they have to be what the call
    # actually passes. Checking only the declarations leaves `n=3` hardcoded at
    # the call site breaking `tan explain`'s suggestions with this gate green.
    assert _call_keyword_sources(tan_tree, "difflib.get_close_matches") == {
        "n": "SUGGESTION_LIMIT",
        "cutoff": "SUGGESTION_CUTOFF",
    }, (
        "tan/core/error_catalog.py no longer passes its own SUGGESTION_LIMIT/"
        "SUGGESTION_CUTOFF to difflib.get_close_matches. The constants above "
        "are then decoration and the comparison against explain.py is "
        "measuring something the command does not use."
    )

    assert _function_body_source(tan_tree, "normalise") == _function_body_source(
        upstream, "_normalise"
    ), (
        "error_catalog.normalise and explain.py's _normalise no longer have the "
        "same body. Matching is case- and whitespace-normalised on both sides "
        "or a code that resolves in one repo does not in the other."
    )


def test_the_symbol_probe_can_actually_fail():
    """Anti-vacuity for the helpers above. Every assertion in the parity test
    is an equality against something extracted from source, so a helper that
    silently returned the same object for any input -- or swallowed a missing
    symbol -- would make the whole check pass while measuring nothing.

    Needs no alp-sdk checkout: it exercises the extractors against this
    repo's own module, where the expected answers are known.
    """
    tan_tree = ast.parse(
        (PACKAGE_ROOT / "tan/core/error_catalog.py").read_text(encoding="utf-8")
    )

    assert _module_constant(tan_tree, "SUGGESTION_LIMIT") == 5
    assert _module_constant(tan_tree, "SUGGESTION_CUTOFF") == 0.4
    with pytest.raises(AssertionError):
        _module_constant(tan_tree, "_definitely_not_a_constant_here")

    assert _call_keyword_sources(tan_tree, "difflib.get_close_matches") == {
        "n": "SUGGESTION_LIMIT",
        "cutoff": "SUGGESTION_CUTOFF",
    }
    with pytest.raises(AssertionError):
        _call_keyword_sources(tan_tree, "difflib.no_such_function")

    body = _function_body_source(tan_tree, "normalise")
    assert body == "return code.strip().upper()", body
    with pytest.raises(AssertionError):
        _function_body_source(tan_tree, "_definitely_not_a_function_here")

    assert HAND_PORT_SDK_ROOT_ENV, "the sibling gate's env-var name is empty"
