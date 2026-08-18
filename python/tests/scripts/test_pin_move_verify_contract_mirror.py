# SPDX-License-Identifier: Apache-2.0
"""Freshness gate for `pin_move_verify.py`'s MIRRORED `alpe2e.pinverify`
contract (PR #823 review, finding 6): `_REF`/`_SKU`/`_REPO`/`_SHA`,
`MAX_SOMS`, and `PinMoveTuple.check_name`'s format string are duplicated by
hand from alp-e2e (private repo, `.github/workflows/pin-move-verify.yml`'s
receiver) for cost reasons -- see `pin_move_verify.py`'s own module
docstring, "The dispatch contract -- mirrored, not reinvented". An ungated
second copy of a contract is tan-cli#358's exact defect shape: the receiver
already enforces `MAX_SOMS = 8` and this sender had no cap at all until this
gate's own PR, silently accepting a 9-SoM request the receiver's `plan` job
would refuse -- whose `report` job (`if: always() && needs.plan.result ==
'success'`) then never posts a Check Run, so the sender burns its full
90-minute poll deadline discovering that. Client-side validation only pays
for itself if it stays IN STEP with what it mirrors.

Two checks, the same PINNED_HASHES / `ALP_SDK_ROOT` shape
`test_planner_relocation_freshness.py` uses against alp-sdk, pointed at
alp-e2e instead:

1. A SELF-check, unconditional, no checkout needed: `pmv.MIRRORED_CONTRACT_HASH`
   must equal the live sha256 of `pmv.MIRRORED_CONTRACT_TEXT`. Catches a local
   edit to any of the four regexes / `MAX_SOMS` / `check_name`'s format string
   that forgot to bump the pinned hash alongside it.
2. A LIVE-DRIFT check, `ALP_E2E_ROOT`-gated: dynamically loads the real
   `alpe2e/pinverify.py` out of a local alp-e2e checkout and re-derives the
   SAME text from the LIVE objects (not a source-text regex scrape -- a
   value-level comparison is the right level for a cost-saving mirror; an
   incidental formatting change upstream that leaves every value identical
   should not force a re-audit here). A mismatch means the receiver's
   contract moved and this mirror has NOT been re-audited against the new
   shape. Without `ALP_E2E_ROOT` this SKIPS, loudly, naming the missing var
   -- never a silent pass, the exact trap tan-cli#275 closed for the
   planner-relocation gate (a gate that cannot fail is not a gate).

When either check fails: diff `alpe2e/pinverify.py`'s `_REF`/`_SKU`/`_REPO`/
`_SHA`/`MAX_SOMS`/`check_name` against `pin_move_verify.py`'s copies, port the
delta into `pin_move_verify.py`, and update `MIRRORED_CONTRACT_HASH` (and its
own "last re-audited against" comment) to match -- tan-cli#835 tracks this
re-audit and is the place to open the next one from.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import pathlib
import sys
import types

import pytest

SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import pin_move_verify as pmv  # noqa: E402


def test_mirrored_contract_hash_matches_the_live_text():
    live_hash = hashlib.sha256(pmv.MIRRORED_CONTRACT_TEXT.encode("utf-8")).hexdigest()
    assert live_hash == pmv.MIRRORED_CONTRACT_HASH, (
        "pin_move_verify.py's mirrored contract (the four regexes, MAX_SOMS, "
        "check_name's format string) changed locally without "
        "MIRRORED_CONTRACT_HASH being updated to match. If this was a "
        "deliberate re-audit against a newer alp-e2e, update the pinned hash "
        "and its 'last re-audited against' comment; if not, this is an "
        "accidental drift between the mirror and what it is pinned to."
    )


def _load_alp_e2e_pinverify(root: pathlib.Path) -> types.ModuleType:
    package_dir = root / "alpe2e"
    # Two genuinely different situations hide behind "the file isn't there",
    # and conflating them misdiagnoses a correct checkout (tan-cli#835): a
    # `root` that isn't an alp-e2e checkout at all is a setup mistake worth
    # a hard FAIL, but a real alp-e2e checkout sitting on `main` -- where
    # `alpe2e/pinverify.py` genuinely does not exist, because the
    # pin-move-verify receiver has not merged there yet -- is not a mistake
    # this test can do anything about. Telling that second case "this does
    # not look like an alp-e2e checkout" is false and sends the reader off
    # to debug the wrong thing.
    if not package_dir.is_dir() or not (root / ".git").exists():
        pytest.fail(
            f"ALP_E2E_ROOT={root} has no alpe2e/ package (or no .git) -- "
            "this does not look like an alp-e2e checkout at all"
        )
    path = package_dir / "pinverify.py"
    if not path.is_file():
        pytest.skip(
            f"ALP_E2E_ROOT={root} IS an alp-e2e checkout, but it has no "
            "alpe2e/pinverify.py -- the pin-move-verify receiver currently "
            "lives only on alp-e2e's unmerged branch "
            "'feat/pin-move-verify-receiver', not on 'main'. A checkout on "
            "'main' genuinely cannot audit this mirrored contract, so this "
            "is a SKIP, not a failure of tan-cli's own code: bind "
            "ALP_E2E_ROOT at a checkout of 'feat/pin-move-verify-receiver' "
            "to actually run this check."
        )
    spec = importlib.util.spec_from_file_location("_alp_e2e_pinverify_live", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered in sys.modules BEFORE exec: `pinverify.py` uses `from
    # __future__ import annotations`, so its `@dataclass` needs
    # `sys.modules[cls.__module__]` to resolve `PinMoveRequest`'s field
    # types at class-creation time -- without this line that lookup finds
    # nothing and dataclass's own `_process_class` raises `AttributeError`
    # on `None.__dict__` (measured).
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[spec.name]
        raise
    return module


def test_missing_pinverify_on_a_real_checkout_skips_not_fails(tmp_path):
    """tan-cli#835 regression: a `root` that IS an alp-e2e checkout (has
    `alpe2e/` and `.git`) but has no `alpe2e/pinverify.py` -- exactly the
    shape of a checkout sitting on alp-e2e's `main`, where the
    pin-move-verify receiver has not merged yet -- must SKIP with a message
    that names the branch the receiver actually lives on
    (`feat/pin-move-verify-receiver`), never the generic "this does not
    look like an alp-e2e checkout" wording that misdiagnoses a correct
    checkout as bogus.
    """
    (tmp_path / "alpe2e").mkdir()
    (tmp_path / "alpe2e" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / ".git").mkdir()

    with pytest.raises(pytest.skip.Exception) as exc_info:
        _load_alp_e2e_pinverify(tmp_path)

    message = str(exc_info.value)
    assert "feat/pin-move-verify-receiver" in message
    assert "does not look like an alp-e2e checkout" not in message


def test_a_non_checkout_root_still_fails_with_the_generic_message(tmp_path):
    """The other half of the same fix: a `root` that is NOT an alp-e2e
    checkout at all (no `alpe2e/` package, no `.git`) must still FAIL --
    only the "checkout exists but the receiver hasn't merged" case became a
    SKIP, not every empty-directory mistake.
    """
    with pytest.raises(pytest.fail.Exception) as exc_info:
        _load_alp_e2e_pinverify(tmp_path)

    assert "does not look like an alp-e2e checkout" in str(exc_info.value)


def _resolve_alp_e2e_root() -> pathlib.Path | None:
    raw = os.environ.get("ALP_E2E_ROOT")
    if not raw:
        return None
    return pathlib.Path(raw).resolve()


def test_mirrored_contract_matches_the_live_alp_e2e_receiver():
    root = _resolve_alp_e2e_root()
    if root is None:
        pytest.skip(
            "ALP_E2E_ROOT is not set -- cannot compare pin_move_verify.py's "
            "mirrored contract against the live alp-e2e receiver. This is a "
            "SKIP, not a pass: bind ALP_E2E_ROOT to a checkout of "
            "alplabai/alp-e2e to actually run this check."
        )
    live = _load_alp_e2e_pinverify(root)

    for name in ("_REF", "_SKU", "_REPO", "_SHA"):
        assert hasattr(live, name), f"alpe2e.pinverify no longer defines {name}"
    assert hasattr(live, "MAX_SOMS"), "alpe2e.pinverify no longer defines MAX_SOMS"
    assert hasattr(live, "PinMoveRequest"), "alpe2e.pinverify no longer defines PinMoveRequest"

    live_example = live.PinMoveRequest(
        tan_ref="TAN_REF",
        sdk_ref="SDK_REF",
        soms=("EXAMPLE",),
        source_repo="owner/name",
        source_sha="0" * 40,
        source_pr=None,
    )
    live_text = (
        f"_REF = {live._REF.pattern!r}\n"
        f"_SKU = {live._SKU.pattern!r}\n"
        f"_REPO = {live._REPO.pattern!r}\n"
        f"_SHA = {live._SHA.pattern!r}\n"
        f"MAX_SOMS = {live.MAX_SOMS!r}\n"
        f"check_name = {live_example.check_name!r}\n"
    )

    assert live_text == pmv.MIRRORED_CONTRACT_TEXT, (
        "pin_move_verify.py's mirrored alpe2e.pinverify contract has drifted "
        "from the live receiver:\n\n"
        f"tan-cli's copy:\n{pmv.MIRRORED_CONTRACT_TEXT}\n"
        f"alp-e2e's live contract:\n{live_text}\n"
        "Diff the two, port the delta into pin_move_verify.py's _REF/_SKU/"
        "_REPO/_SHA/MAX_SOMS/PinMoveTuple.check_name, and update "
        "MIRRORED_CONTRACT_HASH (and its own 'last re-audited against' "
        "comment) to match -- tan-cli#835 tracks this re-audit."
    )
