# SPDX-License-Identifier: Apache-2.0
"""`tan.core.faultdecode` -- pure decode/parse/render unit tests, plus the
register-level fidelity proof the port owes: every (register, bit, name,
meaning) triple in the bit tables here must equal the SDK original's.

That proof is pinned against a COMMITTED golden fixture
(`tests/fixtures/faultdecode_golden.json`, frozen from alp-sdk's
`scripts/alp_cli/faultdecode.py` -- see the fixture's own header) so it holds
on every machine and CI run, not only one with a transient alp-sdk sibling
checkout on disk: a `pytest.skip`-on-missing-oracle guard here would let a
shifted bit or a reworded meaning ship silently the moment that checkout is
gone, which is exactly the failure this module exists to catch (a wrong
meaning is a confident wrong diagnosis of a customer's crash). These two
tests -- `test_bit_tables_match_the_frozen_golden` and
`test_decode_matches_the_frozen_golden` -- MUST NEVER skip.

A second, genuinely optional layer re-diffs live against the SDK original
when one is reachable (`ALP_SDK_ROOT`, or an `alp-sdk` checkout sitting next
to this repo -- see `_resolve_oracle_path`); it skips when neither is found,
same as before, but it is a bonus re-check, not the fidelity guard itself.

`test_decode_matches_the_sdk_original_byte_for_byte` additionally gates on
`_ORACLE_VINTAGE_HASH` (tan-cli#560 review, the one major): a resolved oracle
is only byte-diffed if it is AT the alp-sdk commit
(`tests.gates.test_planner_relocation_freshness.HAND_PORT_PINNED_SDK_COMMIT`)
this sweep was last audited against, else it skips LOUDLY naming the required
vintage. Without that gate, any sibling `alp-sdk` checkout older than alp-sdk
dad5b35a (#1389, the commit that adopted tan-cli#616's LSPERR/MLSPERR fix)
still carries the old `_root_cause` ladder this sweep no longer carves out
for, and turns a correct port red on a contributor's own machine while CI
stays green (the `sdk_parity` job binds `ALP_SDK_ROOT` to the pin; the
non-parity job has no sibling checkout to find at all). Measured: with a
sibling `alp-sdk` checkout that predates dad5b35a as the only reachable
oracle and no `ALP_SDK_ROOT` override, the unguarded sweep was `1 failed` (18
`root_cause` mismatches, all on the LSPERR/MLSPERR words) -- see this
module's git history for the exact command and output.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tan.core import faultdecode as port
from tests.conftest import REAL_ENVIRON
from tests.gates.test_planner_relocation_freshness import (
    HAND_PORT_HASHES,
    HAND_PORT_PINNED_SDK_COMMIT,
)

_GOLDEN_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "faultdecode_golden.json"
_GOLDEN = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))

#: sha256 of `scripts/alp_cli/faultdecode.py` at
#: `HAND_PORT_PINNED_SDK_COMMIT` -- the SAME pin and hash
#: `test_planner_relocation_freshness.py`'s own hand-port freshness gate
#: tracks for this file, reused here rather than re-pinned separately so the
#: two audits cannot drift apart (the tan-cli#296 lesson that split
#: `PINNED_SDK_COMMIT` from `HAND_PORT_PINNED_SDK_COMMIT` in the first place
#: argues against inventing a THIRD, independent pin for the same file).
_ORACLE_VINTAGE_HASH = HAND_PORT_HASHES["scripts/alp_cli/faultdecode.py"]


def _resolve_oracle_path() -> Path | None:
    """Locate alp-sdk's `scripts/alp_cli/faultdecode.py` for the OPTIONAL live
    re-check below -- never for the committed-golden tests above, which never
    need it.

    Tries `ALP_SDK_ROOT` first (a set-but-missing value RAISES rather than
    skipping -- a typo'd override must not silently certify nothing, matching
    `tests/parity/oracle.py`'s `rust_binary()`), then an `alp-sdk` checkout
    sitting next to this repo at any ancestor level (the layout every
    contributor's alp-sdk + tan-cli pair actually uses, worktree or not).
    Returns `None` only when neither is present, so the caller can skip.

    Reads `REAL_ENVIRON` (captured at collection time in `tests/conftest.py`),
    NOT `os.environ` -- this runs from inside test bodies, by which point the
    autouse `_scrub_sdk_discovery_env` fixture has already deleted
    `ALP_SDK_ROOT` from the live process environment, so an `os.environ` read
    here ALWAYS saw it gone.

    Scope of what that broke, stated narrowly because the record will be cited:
    the override was dead, so resolution fell entirely to the sibling walk
    below, and the live re-checks in this module skipped **whenever
    `ALP_SDK_ROOT` named a checkout the sibling walk could not also reach on
    its own**. Both the standard contributor layout and CI's own satisfy that
    walk -- CI checks alp-sdk out to `path: alp-sdk` inside the workspace
    (`.github/workflows/ci.yml`), which the walk finds -- so this was NOT a
    vacuous `sdk_parity` job, and it is NOT a recurrence of tan-cli#275.
    Measured both ways on `dev` @ 1813e46 with the pre-fix reader:
    CI-shaped layout, `ALP_SDK_ROOT` bound as CI binds it -> `18 passed, 0
    skipped`; `ALP_SDK_ROOT` pointing outside the walk (a scratch worktree,
    which is how a bounded change to this file gets developed) -> `8 passed,
    3 skipped`, the 3 being exactly these.

    The fix is still worth making and is strictly stronger: it makes the
    documented `ALP_SDK_ROOT` override authoritative again, which removes a
    silent dependency on where the two checkouts happen to sit relative to
    each other. The sibling module
    `tests/commands/test_faultdecode_command.py` had the identical defect and
    fixed it this way in tan-cli#254/#256; this copy of `_resolve_oracle_path`
    was never brought along. tan-cli#616 made it load-bearing while the
    LSPERR/MLSPERR fix was a live divergence from upstream, policed by
    `test_decode_matches_the_sdk_original_byte_for_byte`; alp-sdk dad5b35a
    (#1389) has since adopted that fix, closing the divergence, but this
    resolver is still what that test (now a plain byte-equality sweep) needs
    to find a live oracle at all.
    """
    override = REAL_ENVIRON.get("ALP_SDK_ROOT")
    if override:
        candidate = Path(override) / "scripts" / "alp_cli" / "faultdecode.py"
        if not candidate.is_file():
            raise RuntimeError(
                f"ALP_SDK_ROOT={override!r} has no scripts/alp_cli/faultdecode.py. "
                "Refusing to skip: a named-but-missing oracle would make this "
                "check pass vacuously. Fix the path, or unset it."
            )
        return candidate
    for parent in Path(__file__).resolve().parents:
        candidate = parent.parent / "alp-sdk" / "scripts" / "alp_cli" / "faultdecode.py"
        if candidate.is_file():
            return candidate
    return None


def _load_original():
    path = _resolve_oracle_path()
    if path is None:
        pytest.skip("no alp-sdk checkout found (set ALP_SDK_ROOT, or run next to one)")
    # `alp_cli.faultdecode` imports `alp_cli.diagnostic` and `colorama` -- both
    # importable in this environment (colorama ships as a transitive dep here
    # too) -- and does a relative-package import (`from alp_cli.diagnostic
    # import _use_color`), so the SDK's `scripts/` dir must be on sys.path for
    # the load to resolve, not just this one file.
    sdk_scripts = str(path.parents[1])
    added = sdk_scripts not in sys.path
    if added:
        sys.path.insert(0, sdk_scripts)
    try:
        spec = importlib.util.spec_from_file_location("_faultdecode_oracle", path)
        module = importlib.util.module_from_spec(spec)
        # `@dataclass(slots=True)` resolves its own module via
        # `sys.modules[cls.__module__]` while `exec_module` runs -- a module
        # object that was never registered there raises `AttributeError:
        # 'NoneType' object has no attribute '__dict__'` deep inside
        # `dataclasses`, so it must be registered before `exec_module`, same as
        # `importlib.import_module` does internally.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if added:
            sys.path.remove(sdk_scripts)


def _require_pinned_oracle_vintage(path: Path) -> None:
    """Refuse to byte-diff against an oracle that is not AT the alp-sdk
    commit `test_decode_matches_the_sdk_original_byte_for_byte` was last
    audited against -- skip LOUDLY naming the required vintage instead of
    silently full-diffing whatever sibling checkout `_resolve_oracle_path`
    happened to find (tan-cli#560 review, the one major).

    A sibling `alp-sdk` checkout older than dad5b35a (#1389) still carries
    the pre-fix `_root_cause` ladder with no LSPERR/MLSPERR branch, which
    this sweep's now-unconditional byte-equality assertion would report as
    18 mismatches with no indication the port is fine and the SDK checkout
    is simply stale -- exactly what the old carve-out existed to prevent
    resurfacing as a false red. `HAND_PORT_PINNED_SDK_COMMIT` and its sha256
    for this file are the SAME pin `test_planner_relocation_freshness.py`'s
    own hand-port freshness gate already tracks -- reused, not duplicated,
    so the two audits cannot silently disagree about which alp-sdk state
    `scripts/alp_cli/faultdecode.py` was last checked against."""
    current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if current_hash != _ORACLE_VINTAGE_HASH:
        pytest.skip(
            "the resolved alp-sdk oracle "
            f"({path}, sha256 {current_hash}) is not at the alp-sdk commit "
            f"this byte-for-byte sweep is pinned to "
            f"({HAND_PORT_PINNED_SDK_COMMIT}, sha256 {_ORACLE_VINTAGE_HASH}) "
            "-- most likely your sibling alp-sdk checkout predates alp-sdk "
            "dad5b35a (#1389), before it adopted tan-cli#616's LSPERR/MLSPERR "
            "fix, and would show a root_cause divergence this port "
            "deliberately no longer carves out for. Point ALP_SDK_ROOT (or "
            f"your sibling alp-sdk checkout) at {HAND_PORT_PINNED_SDK_COMMIT} "
            "to run this sweep for real. If instead the SDK's "
            "faultdecode.py has genuinely changed again, diff it, port the "
            "delta, and re-pin HAND_PORT_HASHES + HAND_PORT_PINNED_SDK_COMMIT "
            "in tests/gates/test_planner_relocation_freshness.py -- "
            "_ORACLE_VINTAGE_HASH here reads that same table, so it moves "
            "with it."
        )


def _all_triples(mod) -> set[tuple[str, int, str, str]]:
    out: set[tuple[str, int, str, str]] = set()
    tables = (
        ("MMFSR", mod.MMFSR_BITS),
        ("BFSR", mod.BFSR_BITS),
        ("UFSR", mod.UFSR_BITS),
        ("HFSR", mod.HFSR_BITS),
        ("DFSR", mod.DFSR_BITS),
    )
    for reg, table in tables:
        for bit, name, meaning in table:
            out.add((reg, bit, name, meaning))
    return out


# --------------------------------------------------------------------------
# Committed-golden fidelity guard -- MUST NEVER skip.
# --------------------------------------------------------------------------


def test_bit_tables_match_the_frozen_golden():
    """Every (register, bit, name, meaning) quadruple, diffed against the
    committed golden fixture frozen from alp-sdk's
    `scripts/alp_cli/faultdecode.py`. A shifted bit or a reworded message
    here is exactly the failure this test exists to catch -- unconditionally,
    with no oracle checkout required."""
    ours = _all_triples(port)
    golden = {tuple(row) for row in _GOLDEN["bit_tables"]}
    assert ours == golden, (
        f"missing from port: {golden - ours}\nextra in port: {ours - golden}"
    )


def test_decode_matches_the_frozen_golden():
    """`decode()` + `report_to_json()` + `render_human()`, diffed against the
    committed golden's swept fault-word cases (single-bit sweep over
    cfsr/hfsr/dfsr plus a handful of composed/realistic combinations)."""
    mismatches = []
    for case in _GOLDEN["decode_cases"]:
        report = port.decode(
            cfsr=case["cfsr"], hfsr=case["hfsr"], dfsr=case["dfsr"],
            bfar=case["bfar"], mmfar=case["mmfar"],
        )
        got_json = port.report_to_json(report, None)
        got_human = port.render_human(report, None, False)
        if got_json != case["report_json"] or got_human != case["render_human"]:
            mismatches.append((case["cfsr"], case["hfsr"], case["dfsr"]))
    assert not mismatches, f"{len(mismatches)} decode mismatches: {mismatches[:5]}"


# --------------------------------------------------------------------------
# Optional live re-check against an actual alp-sdk checkout -- skips (never
# fails) when one is not reachable; the golden tests above are the real gate.
# --------------------------------------------------------------------------


def test_bit_tables_match_the_sdk_original_exactly():
    original = _load_original()
    ours = _all_triples(port)
    theirs = _all_triples(original)
    assert ours == theirs, (
        f"missing from port: {theirs - ours}\nextra in port: {ours - theirs}"
    )


#: `(cfsr, hfsr, dfsr)` words that pair a lazy-FP-preservation bit (LSPERR/
#: MLSPERR) with an HFSR cause, i.e. the region where the LSPERR/MLSPERR
#: branches sit next to a branch that already had an answer.
#:
#: The rest of the sweep could not reach this region on its own: the
#: single-bit sweep never pairs two bits, and the seeded 200 random CFSR words
#: essentially always carry a higher-priority cause, so LSPERR never won one.
#: A draft of tan-cli#616 placed both branches ABOVE `VECTTBL`/`DEBUGEVT`,
#: which these four words would have caught and the rest of the sweep
#: structurally could not.
_TWO_BIT_PRECEDENCE_CASES: list[tuple[int, int, int]] = [
    (0x2000, 0x00000002, 0),  # LSPERR  + HFSR.VECTTBL  -> VECTTBL wins
    (0x0020, 0x00000002, 0),  # MLSPERR + HFSR.VECTTBL  -> VECTTBL wins
    (0x2000, 0x80000000, 0),  # LSPERR  + HFSR.DEBUGEVT -> DEBUGEVT wins
    (0x0020, 0x80000000, 0),  # MLSPERR + HFSR.DEBUGEVT -> DEBUGEVT wins
]

#: `(cfsr, hfsr, dfsr)` words the seeded random sweep essentially never
#: reaches on its own -- the issue #1358 / tan-cli#616 repro itself, LSPERR
#: alone, and a BFARVALID-only word (no cause bit at all) -- pinned
#: deterministically so a regression in any of them cannot hide behind an
#: unlucky seed.
_DETERMINISTIC_REGRESSION_CASES: list[tuple[int, int, int]] = [
    (0x2000, 0x40000000, 0),  # LSPERR + FORCED -- the issue's own repro
    (0x0020, 0x40000000, 0),  # MLSPERR + FORCED
    (0x2000, 0, 0),           # LSPERR alone
    (0x8000, 0, 0x2),         # BFARVALID (no cause) + DFSR BKPT
]


def test_decode_matches_the_sdk_original_byte_for_byte():
    """`decode()` + `render_human()` + `report_to_json()`, swept over every
    single-bit case, a batch of random combinations, and the deterministic
    words above, diffed against the SDK original's own functions.

    Byte-equality, no carve-outs. This test used to tolerate a `root_cause`-
    only divergence tan-cli#616 introduced deliberately: alp-sdk's
    `_root_cause` ladder had no branch for LSPERR (BFSR bit 13) or MLSPERR
    (MMFSR bit 5), so both fell through onto the `FORCED` escalation bit
    instead of naming the fault that triggered it (see
    `tests/fixtures/faultdecode_golden.PROVENANCE.txt` for the full history).
    alp-sdk dad5b35a (#1389, inside the a3173305..d00dbdc1 pin range) ADOPTED
    tan's fix verbatim -- same guard (`_cfsr_names_a_cause`), same branch
    order, same LAST-in-the-ladder placement below VECTTBL/DEBUGEVT -- so
    there is no longer a live SDK build against which tan's port diverges,
    and this test goes back to what it asserted before #616: exact equality.
    If a future SDK pin reopens a gap here, reintroduce a targeted carve-out
    rather than loosening this to "differs somehow" -- unpinned tolerance is
    exactly what tan-cli#502's PROVENANCE calls out as the harm.

    Gated on `_require_pinned_oracle_vintage` (tan-cli#560 review): a resolved
    oracle that predates alp-sdk dad5b35a still has the old ladder this
    unconditional equality no longer tolerates, so it must skip rather than
    report a false regression in the port.
    """
    oracle_path = _resolve_oracle_path()
    if oracle_path is None:
        pytest.skip("no alp-sdk checkout found (set ALP_SDK_ROOT, or run next to one)")
    _require_pinned_oracle_vintage(oracle_path)
    original = _load_original()
    import random

    random.seed(20260730)
    cases: list[tuple[int, int, int]] = []
    for bit in range(32):
        cases.append((1 << bit, 0, 0))
        cases.append((0, 1 << bit, 0))
        cases.append((0, 0, 1 << bit))
    for _ in range(200):
        cases.append(
            (random.getrandbits(32), random.getrandbits(32), random.getrandbits(6))
        )
    cases += _DETERMINISTIC_REGRESSION_CASES
    cases += _TWO_BIT_PRECEDENCE_CASES

    mismatches: list[str] = []
    for cfsr, hfsr, dfsr in cases:
        for bfar, mmfar in ((None, None), (0x20000000, None), (None, 0x20000004)):
            ours = port.decode(cfsr=cfsr, hfsr=hfsr, dfsr=dfsr, bfar=bfar, mmfar=mmfar)
            theirs = original.decode(
                cfsr=cfsr, hfsr=hfsr, dfsr=dfsr, bfar=bfar, mmfar=mmfar
            )
            if port.report_to_json(ours, None) == original.report_to_json(
                theirs, None
            ) and port.render_human(ours, None, False) == original.render_human(
                theirs, None, False
            ):
                continue
            where = f"cfsr={hex(cfsr)} hfsr={hex(hfsr)} dfsr={hex(dfsr)} bfar={bfar} mmfar={mmfar}"
            mismatches.append(f"{where}: {ours.root_cause!r} != {theirs.root_cause!r}")
    assert not mismatches, f"{len(mismatches)} mismatches: {mismatches[:5]}"


def test_parse_dump_matches_the_sdk_original():
    original = _load_original()
    dumps = [
        "CFSR: 0x00008200\nHFSR: 0x40000000\nBFAR Address: 0x20001000\n",
        "mmfsr=0x02 bfsr=0x82 ufsr=0x0001\n",
        "PC=0x08001234 LR: 0x08005678\nDFSR = 0x2\n",
        "random garbage no registers here at all",
        "CFSR 8200 HFSR 40000000",
        "cfsr: 0x1\ncfsr: 0x2\n",
    ]
    for dump in dumps:
        assert port.parse_dump(dump) == original.parse_dump(dump), dump


# --------------------------------------------------------------------------
# Standalone unit coverage (no oracle needed)
# --------------------------------------------------------------------------


def test_no_flags_set_reports_nothing_to_decode():
    report = port.decode()
    assert not report.fault_detected
    assert report.root_cause == "No fault status bits are set -- nothing to decode."


def test_stkof_wins_over_forced_hardfault():
    """STKOF (bit 20 of UFSR -> CFSR bit 20) is the most-specific root cause;
    FORCED (HFSR bit 30) must not shadow it even when both are set."""
    report = port.decode(cfsr=1 << 20, hfsr=1 << 30)
    assert "Stack overflow" in report.root_cause


def test_preciserr_reports_bfar_address_when_valid():
    cfsr = (1 << 9) | (1 << 15)  # PRECISERR | BFARVALID
    report = port.decode(cfsr=cfsr, bfar=0x40001000)
    assert "0x40001000" in report.root_cause
    assert report.bfar_valid is True


def test_bfar_without_valid_bit_is_not_authoritative():
    report = port.decode(cfsr=1 << 9, bfar=0x40001000)  # PRECISERR, no BFARVALID
    assert "0x40001000" not in report.root_cause
    assert report.bfar_valid is False


def test_mmfsr_bfsr_ufsr_sub_registers_compose_into_cfsr():
    """`parse_dump` composes split sub-registers back into one CFSR word at
    their documented bit offsets: MMFSR at bits 0-7, BFSR at 8-15, UFSR at
    16-31."""
    found = port.parse_dump("mmfsr=0x02 bfsr=0x82 ufsr=0x0001")
    assert found["cfsr"] == (0x02) | (0x82 << 8) | (0x0001 << 16)


def test_last_occurrence_of_a_token_wins():
    found = port.parse_dump("cfsr: 0x1\ncfsr: 0x2\n")
    assert found["cfsr"] == 0x2


# --------------------------------------------------------------------------
# tan-cli#616 defect A: HFSR.FORCED is an ESCALATION, never the root cause
# --------------------------------------------------------------------------
#
# These pin the exact strings. `faultdecode` is a diagnostic command whose
# whole output IS its contract -- a reworded root cause is a different
# diagnosis handed to a firmware engineer at 2am, not a cosmetic change -- so
# a substring probe would let the wording rot underneath a green bar.


#: The verbatim answer for `--cfsr 0x2000` (BFSR bit 13, LSPERR).
_LSPERR_ROOT_CAUSE = (
    "Bus fault while lazily preserving the floating-point context -- the deferred push of the "
    "FP registers into the space the exception frame reserved for them hit a faulting address, "
    "so that stack memory is bad or absent (a corrupted or overflowed stack pointer). Check "
    "SP/PSPLIM and that the stack fits the larger FP-extended exception frame."
)

#: The verbatim answer for `--cfsr 0x20` (MMFSR bit 5, MLSPERR).
_MLSPERR_ROOT_CAUSE = (
    "MemManage fault while lazily preserving the floating-point context -- the MPU forbids the "
    "deferred push of the FP registers into the space the exception frame reserved for them "
    "(wrong region permissions, or a stack that has overflowed out of its region)."
)

#: The verbatim answer when FORCED really IS all there is to say.
_FORCED_ROOT_CAUSE = (
    "Forced HardFault -- a configurable fault escalated but its own status bits are clear; the "
    "escalation usually means faults are disabled (SHCSR) or it faulted at priority -1."
)


def test_forced_hardfault_does_not_shadow_the_lsperr_it_escalated():
    """tan-cli#616's own repro: `--cfsr 0x2000 --hfsr 0x40000000`.

    `HFSR.FORCED` (bit 30) says a configurable fault could not be taken by its
    own handler and escalated; WHAT faulted is in CFSR. Leading with the
    escalation handed the operator the least actionable half of the registers
    -- and, here, a factually false half: the old answer asserted "its own
    status bits are clear" with LSPERR demonstrably set."""
    report = port.decode(cfsr=0x2000, hfsr=0x40000000)
    assert report.root_cause == _LSPERR_ROOT_CAUSE
    # The escalation is not lost -- it is reported where a qualifier belongs.
    assert report.has("FORCED")


def test_forced_hardfault_does_not_shadow_the_mlsperr_it_escalated():
    report = port.decode(cfsr=0x20, hfsr=0x40000000)
    assert report.root_cause == _MLSPERR_ROOT_CAUSE
    assert report.has("FORCED")


def test_lsperr_reports_the_bfar_address_when_the_valid_bit_is_set():
    """A cause branch, not the bare `<NAME> set (<REG>)` fallback, means LSPERR
    now carries the faulting address the fallback threw away."""
    report = port.decode(cfsr=(1 << 13) | (1 << 15), hfsr=1 << 30, bfar=0x2000FFF0)
    assert report.root_cause == (
        "Bus fault while lazily preserving the floating-point context at 0x2000fff0 (BFAR) -- "
        "the deferred push of the FP registers into the space the exception frame reserved for "
        "them hit a faulting address, so that stack memory is bad or absent (a corrupted or "
        "overflowed stack pointer). Check SP/PSPLIM and that the stack fits the larger "
        "FP-extended exception frame."
    )


def test_forced_stays_the_root_cause_when_cfsr_names_no_cause():
    """The demotion is conditional, not a deletion. With CFSR genuinely clear,
    `FORCED` is the whole story and the sentence's "its own status bits are
    clear" is true -- which is exactly when it may be said."""
    assert port.decode(hfsr=1 << 30).root_cause == _FORCED_ROOT_CAUSE


def test_forced_stays_the_root_cause_when_cfsr_holds_only_an_address_valid_bit():
    """`BFARVALID` says the address register beside it is trustworthy. It is
    not a fault, so it does not disqualify `FORCED` from answering -- and it
    must not answer itself."""
    report = port.decode(cfsr=1 << 15, hfsr=1 << 30, bfar=0x20000000)
    assert report.root_cause == _FORCED_ROOT_CAUSE


def test_a_cfsr_cause_bit_with_no_ladder_branch_still_beats_forced():
    """The guard is keyed on the flag's REGISTER, not on a list of names, so a
    CFSR bit added to the tables tomorrow outranks `FORCED` the day it lands --
    before anyone remembers to give it a `_root_cause` branch. That omission is
    precisely how LSPERR/MLSPERR, in the bit tables since day one, came to be
    reported as "Forced HardFault" for as long as they were.

    Built by hand rather than by decoding a word, because every real CFSR cause
    bit now HAS a branch: this is the future-bit case, and it is the only way
    to exercise the guard itself rather than a branch above it."""
    report = port.FaultReport(
        flags=[
            port.DecodedFlag(reg="BFSR", name="BFARVALID", bit=15, meaning="valid."),
            port.DecodedFlag(reg="UFSR", name="FUTUREBIT", bit=26, meaning="A bit from 2027."),
            port.DecodedFlag(reg="HFSR", name="FORCED", bit=30, meaning="escalated."),
        ]
    )
    assert port._root_cause(report) == "FUTUREBIT set (UFSR): A bit from 2027."


#: The two HFSR causes that keep their precedence over the new lazy-FP
#: branches, verbatim.
_VECTTBL_ROOT_CAUSE = (
    "Vector-table read fault -- a bus error reading an exception vector (VTOR points at bad "
    "memory, or the vector table is unmapped)."
)
_DEBUGEVT_ROOT_CAUSE = (
    "Debug event with no debugger attached -- a stray BKPT or a watchpoint firing in a "
    "free-running build."
)


@pytest.mark.parametrize(
    ("cfsr", "hfsr", "expected"),
    [
        (0x2000, 0x00000002, _VECTTBL_ROOT_CAUSE),   # LSPERR  + VECTTBL
        (0x0020, 0x00000002, _VECTTBL_ROOT_CAUSE),   # MLSPERR + VECTTBL
        (0x2000, 0x80000000, _DEBUGEVT_ROOT_CAUSE),  # LSPERR  + DEBUGEVT
        (0x0020, 0x80000000, _DEBUGEVT_ROOT_CAUSE),  # MLSPERR + DEBUGEVT
    ],
)
def test_vecttbl_and_debugevt_keep_their_precedence_over_a_lazy_fp_fault(cfsr, hfsr, expected):
    """The new LSPERR/MLSPERR branches sit at the BOTTOM of the ladder, below
    `VECTTBL` and `DEBUGEVT` as well as below every CFSR branch.

    A bad VTOR / unmapped vector table, and a stray BKPT in a free-running
    build, are both more specific findings than "something faulted during the
    deferred FP push" -- and, decisively, both are answers upstream ALREADY
    gave. Overriding them would be a third divergence class that tan-cli#616
    never asked for and that nothing in the issue justifies.

    Oracle-free on purpose: the live sweep that also covers these words skips
    without an alp-sdk checkout, and this precedence must be pinned on every
    machine, not only one with a sibling checkout on disk."""
    assert port.decode(cfsr=cfsr, hfsr=hfsr).root_cause == expected


def test_an_address_valid_bit_is_never_announced_as_the_root_cause():
    """The fallback obeys the same rule as the `FORCED` guard: with `BFARVALID`
    first in the flag list and a real (if unladdered) fault after it, the fault
    is what gets named."""
    report = port.decode(cfsr=1 << 15, dfsr=0x2, bfar=0x20000000)
    assert report.root_cause == "BKPT set (DFSR): Breakpoint -- a BKPT instruction or hardware breakpoint."
