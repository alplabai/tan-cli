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
import os
import sys
from pathlib import Path

import pytest

from tan.core import faultdecode as port

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
    """
    override = os.environ.get("ALP_SDK_ROOT")
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


def test_decode_matches_the_sdk_original_over_many_fault_words():
    """`decode()` + `render_human()` + `report_to_json()`, swept over every
    single-bit case and a batch of random combinations, diffed against the
    SDK original's own functions."""
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

    mismatches = []
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
                mismatches.append((hex(cfsr), hex(hfsr), hex(dfsr), bfar, mmfar))
    assert not mismatches, f"{len(mismatches)} decode mismatches: {mismatches[:5]}"


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
# tan-cli#503, defect 4: LSPERR/MLSPERR must not fall through to the generic
# FORCED message when both are set -- LSPERR/MLSPERR name the real cause.
# --------------------------------------------------------------------------


def test_lsperr_plus_forced_names_lazy_fp_stacking_not_the_generic_forced_message():
    """BFSR.LSPERR (bit 13) escalated to HardFault (HFSR.FORCED, bit 30) used
    to report 'its own status bits are clear' while LSPERR was the very bit
    set -- self-contradictory against the flag list in the same report."""
    report = port.decode(cfsr=1 << 13, hfsr=1 << 30)
    assert report.has("LSPERR")
    assert report.has("FORCED")
    assert "lazy floating-point" in report.root_cause
    assert "its own status bits are clear" not in report.root_cause


def test_mlsperr_plus_forced_names_lazy_fp_stacking_not_the_generic_forced_message():
    """MMFSR.MLSPERR (bit 5) is the MemManage-side twin of the same defect."""
    report = port.decode(cfsr=1 << 5, hfsr=1 << 30)
    assert report.has("MLSPERR")
    assert "lazy floating-point" in report.root_cause


def test_lsperr_alone_without_forced_is_unaffected():
    """The fix must be scoped to the FORCED combination only: LSPERR alone
    (no escalation) must keep decoding exactly as before -- via the generic
    `first = report.flags[0]` fallback -- matching the frozen golden fixture
    and the SDK oracle for every case that is not this exact combination."""
    report = port.decode(cfsr=1 << 13)
    assert report.has("LSPERR")
    assert report.has("FORCED") is False
    assert report.root_cause == (
        "LSPERR set (BFSR): Bus fault during lazy floating-point state preservation."
    )


# --------------------------------------------------------------------------
# tan-cli#503 follow-up: `parse_dump`'s `0x[0-9A-Fa-f]+` alternative has no
# width cap -- `faultdecode_cmd._parse_hexint` bounds a value entering via a
# flag, but this is the SECOND, independent entry point a value can arrive
# by, and it must reject an over-wide match the same way.
# --------------------------------------------------------------------------


def test_parse_dump_skips_a_value_wider_than_32_bits():
    """An over-wide hex run (more than 8 hex digits, i.e. > 0xFFFFFFFF) is
    dropped, not clamped or wrapped -- the same "refuse, don't corrupt"
    contract `_parse_hexint` applies on the flag path, so a value that
    reaches `decode()` from either entry point is always a well-formed
    32-bit word."""
    found = port.parse_dump("cfsr: 0x1FFFFFFFFF\n")
    assert "cfsr" not in found


def test_parse_dump_accepts_the_maximum_32_bit_value():
    """The boundary itself, 0xFFFFFFFF, must still parse -- the guard is
    `value > 0xFFFFFFFF`, not `>=`, so this is not an off-by-one rejection of
    a legitimate all-ones register word."""
    found = port.parse_dump("cfsr: 0xFFFFFFFF\n")
    assert found["cfsr"] == 0xFFFFFFFF


def test_parse_dump_over_wide_value_does_not_suppress_a_later_valid_token():
    """One bad match must not poison the whole parse: an over-wide CFSR is
    skipped, but a well-formed HFSR later in the same text is still found."""
    found = port.parse_dump("cfsr: 0x1FFFFFFFFF\nhfsr: 0x40000000\n")
    assert "cfsr" not in found
    assert found["hfsr"] == 0x40000000
