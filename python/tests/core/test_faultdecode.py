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
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tan.core import faultdecode as port
from tests.conftest import REAL_ENVIRON

_GOLDEN_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "faultdecode_golden.json"
_GOLDEN = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


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
    was never brought along. tan-cli#616 made it load-bearing, since this is
    what resolves the oracle for the gate policing tan-cli#616's divergence
    from upstream (see
    `test_decode_matches_the_sdk_original_including_the_classes_tan_cli_616_used_to_diverge_on`)
    -- CLOSED as of alp-sdk `dad5b35a` (2026-08-12), so that gate now polices
    agreement rather than an excused difference, but still needs the same
    working oracle resolution to do it.
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


#: MLSPERR (MMFSR bit 5) | LSPERR (BFSR bit 13) -- the two CFSR CAUSE bits
#: alp-sdk's `_root_cause` ladder never gave a branch to, which is what let
#: `HFSR.FORCED` (or the bare `<NAME> set (<REG>)` fallback) answer for them.
_LAZY_FP_BITS = (1 << 5) | (1 << 13)


#: `(cfsr, hfsr, dfsr)` words that pair a lazy-FP-preservation bit with an
#: HFSR cause, i.e. the region where the LSPERR/MLSPERR branches sit next
#: to a branch upstream ALREADY had an answer for.
#:
#: The sweep could not reach this region and it mattered: the single-bit sweep
#: never pairs two bits, and the seeded 200 random CFSR words essentially
#: always carry a higher-priority cause, so LSPERR never won one. A draft of
#: tan-cli#616 placed both branches ABOVE `VECTTBL`/`DEBUGEVT` and silently
#: overrode both on exactly these words -- an undeclared third divergence class
#: that this test would have flagged and structurally never saw. These four
#: words must produce byte-identical output on both sides: they are the pin
#: that the branches sit at the BOTTOM of the ladder, and moving them back up
#: reds this test as an undeclared mismatch.
_TWO_BIT_PRECEDENCE_CASES: list[tuple[int, int, int]] = [
    (0x2000, 0x00000002, 0),  # LSPERR  + HFSR.VECTTBL  -> VECTTBL wins
    (0x0020, 0x00000002, 0),  # MLSPERR + HFSR.VECTTBL  -> VECTTBL wins
    (0x2000, 0x80000000, 0),  # LSPERR  + HFSR.DEBUGEVT -> DEBUGEVT wins
    (0x0020, 0x80000000, 0),  # MLSPERR + HFSR.DEBUGEVT -> DEBUGEVT wins
]


def _formerly_divergent_class(cfsr: int, ours: port.FaultReport) -> str | None:
    """Which tan-cli#616 divergence class this case would have exercised, or
    `None`.

    CLOSED as of alp-sdk `dad5b35a` ("fix(faultdecode): lead with the
    escalated fault, not the escalation (#1389)", folding #1358,
    2026-08-12): upstream ported the identical LSPERR/MLSPERR root-cause
    branches and the address-VALID-is-not-a-cause fallback tan-cli#616
    introduced here first, so `theirs.root_cause` and `ours.root_cause` are
    now byte-identical for every case below -- there is nothing left to
    classify a MISMATCH by. This classifies by tan's OWN answer instead,
    purely to prove the sweep still REACHES the specific words that used to
    diverge (the non-vacuity half of the test below); as of the closure it is
    not distinguishing tan's answer from the oracle's, only tagging which of
    the two former classes a case belongs to.

    Both legs are keyed on the ACTUAL branch `_root_cause` took, not on "the
    CFSR happens to carry the relevant bit" -- that used to be true for the
    lazy-FP leg but not for the address-valid one, and the gap made the
    address-valid leg unfalsifiable: `cfsr & ((1<<7)|(1<<15))` is true for
    roughly 80% of random (cfsr, hfsr, dfsr) words (measured: 477/600 on this
    module's own seeded sweep), and "the answer doesn't start with the
    ARVALID flag's own text" is true for nearly all of THOSE too, since almost
    every one is answered by an earlier, unrelated ladder branch instead
    (PRECISERR, VECTTBL, ...) -- so the leg tagged words that never came near
    the fallback it exists to police, and could not have caught the coverage
    loss it was written to catch (tan-cli#692 review, MAJOR 1).
    """
    if cfsr & _LAZY_FP_BITS and ours.root_cause.startswith(
        ("Bus fault while lazily preserving", "MemManage fault while lazily preserving")
    ):
        # #616 defect A: a fault taken during lazy FP state preservation is a
        # CAUSE (with an address, when the VALID bit is set), and `FORCED` is
        # only ever the escalation that carried it to the HardFault handler.
        # Keyed on the branch's own distinctive prose -- no other branch in
        # the ladder produces this text.
        return "lazy-fp-preservation-is-a-cause"
    # The terminal fallback in `_root_cause` (the `next(...)` call that skips
    # over an ARVALID flag) is what this leg exists to keep exercised, so tag
    # a case ONLY when that skip demonstrably happened: `ours.flags[0]` must
    # BE the ARVALID flag (otherwise there was nothing for the fallback to
    # skip) AND `ours.root_cause` must equal the fallback's own
    # `"<NAME> set (<REG>): <meaning>"` formatting of the first flag AFTER
    # it -- the one literal shape no other branch in the ladder produces
    # (every other branch returns hand-written prose), so an exact match
    # proves the fallback ran rather than some earlier, unrelated branch that
    # merely happens not to start with the ARVALID flag's own text.
    if ours.flags and ours.flags[0].name in port._ADDRESS_VALID_FLAGS:
        first_non_arvalid = next(
            (flag for flag in ours.flags if flag.name not in port._ADDRESS_VALID_FLAGS),
            None,
        )
        if first_non_arvalid is not None:
            expected = (
                f"{first_non_arvalid.name} set ({first_non_arvalid.reg}): "
                f"{first_non_arvalid.meaning}"
            )
            if ours.root_cause == expected:
                # Same rule one step down: an address-VALID bit (MMARVALID
                # bit 7 / BFARVALID bit 15) qualifies the register beside it
                # and describes nothing that broke, so it must not be
                # announced as the cause while a real one sits later in the
                # flag list.
                return "address-valid-is-not-a-cause"
    return None


def test_decode_matches_the_sdk_original_including_the_classes_tan_cli_616_used_to_diverge_on():
    """`decode()` + `render_human()` + `report_to_json()`, swept over every
    single-bit case, a batch of random combinations, and the deterministic
    words that used to exercise tan-cli#616's divergence, diffed against the
    SDK original's own functions.

    This asserted byte-equality outright before tan-cli#616 made tan diverge
    from upstream ON PURPOSE, deliberately and by name, on the
    `lazy-fp-preservation-is-a-cause` and `address-valid-is-not-a-cause`
    classes (see `tan/core/faultdecode.py::_root_cause` and
    `tests/fixtures/faultdecode_golden.PROVENANCE.txt`). alp-sdk `dad5b35a`
    ("fix(faultdecode): lead with the escalated fault, not the escalation
    (#1389)", folding #1358, 2026-08-12) ported both classes verbatim, so as
    of that commit there is nothing left to excuse: this is byte-equality
    again, full stop -- ANY difference anywhere, including inside the words
    that used to diverge, is now an undeclared failure. Deleting this test, or
    loosening it to "differs somehow", would give a REOPENED divergence
    exactly the unpinned freedom tan-cli#502's PROVENANCE calls out as the
    harm; the non-vacuity check below closes the other direction -- a sweep
    that quietly stopped reaching the formerly-divergent words would prove
    nothing about them either.
    """
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
    # The random sweep reaches `lazy-fp-preservation-is-a-cause` by luck and
    # `address-valid-is-not-a-cause` essentially never (almost every random
    # 32-bit CFSR carries a cause bit). These four make both classes, and the
    # #616 repro itself, reachable deterministically, so the non-vacuity check
    # below cannot be satisfied by a lucky seed alone.
    cases += [
        (0x2000, 0x40000000, 0),  # LSPERR + FORCED -- the issue's own repro
        (0x0020, 0x40000000, 0),  # MLSPERR + FORCED
        (0x2000, 0, 0),           # LSPERR alone
        # BFARVALID (no cause) + DFSR BKPT. Do not delete: this is the ONLY
        # case in the whole sweep that reaches the terminal fallback's
        # ARVALID-skip in `_root_cause` -- proven by reverting the skip
        # (`first = report.flags[0]`) and re-running this sweep unmodified,
        # which reds with exactly 3 mismatches, one per bfar/mmfar variant
        # below, all from this word (tan-cli#692 review, MAJOR 1/3). Delete it
        # and `_formerly_divergent_class`'s non-vacuity check below passes
        # while the fallback goes unreached.
        (0x8000, 0, 0x2),
    ]
    cases += _TWO_BIT_PRECEDENCE_CASES

    mismatches: list[str] = []
    classes: set[str] = set()
    for cfsr, hfsr, dfsr in cases:
        for bfar, mmfar in ((None, None), (0x20000000, None), (None, 0x20000004)):
            ours = port.decode(cfsr=cfsr, hfsr=hfsr, dfsr=dfsr, bfar=bfar, mmfar=mmfar)
            theirs = original.decode(
                cfsr=cfsr, hfsr=hfsr, dfsr=dfsr, bfar=bfar, mmfar=mmfar
            )
            if port.report_to_json(ours, None) != original.report_to_json(
                theirs, None
            ) or port.render_human(ours, None, False) != original.render_human(
                theirs, None, False
            ):
                where = f"cfsr={hex(cfsr)} hfsr={hex(hfsr)} dfsr={hex(dfsr)} bfar={bfar} mmfar={mmfar}"
                mismatches.append(
                    f"{where}: UNDECLARED mismatch -- tan={ours.root_cause!r} "
                    f"sdk={theirs.root_cause!r}"
                )
                continue
            kind = _formerly_divergent_class(cfsr, ours)
            if kind is not None:
                classes.add(kind)
    assert not mismatches, (
        f"{len(mismatches)} mismatches -- alp-sdk dad5b35a closed tan-cli#616's "
        f"divergence, so any difference here is a REOPENED, undeclared one: "
        f"{mismatches[:5]}"
    )
    # Non-vacuity: a sweep that stopped reaching the formerly-divergent words
    # would pass this test while proving nothing about the classes it exists
    # to keep covered.
    assert classes == {"lazy-fp-preservation-is-a-cause", "address-valid-is-not-a-cause"}, (
        f"the sweep no longer exercises every formerly-divergent class: {sorted(classes)}"
    )


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
