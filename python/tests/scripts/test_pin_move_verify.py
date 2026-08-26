# SPDX-License-Identifier: Apache-2.0
"""`scripts/pin_move_verify.py`'s pure logic -- pin-site detection, payload
construction, and the fail-closed verdict judge -- unit-tested offline, per
this repo's own convention (see the module's docstring: "the judgement is
worth testing offline, 40-90 minutes of a CI job is not a place to discover
a logic bug"). The workflow that shells `git`, mints a token, and polls
`gh api` is NOT exercised here -- there is nothing to unit-test about a
network call, and alp-e2e's own secrets are not provisioned yet (see
tan-cli#820 and the PR this file landed in), so an end-to-end run stays
unproven until a human provisions them.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import pin_move_verify as pmv  # noqa: E402


# ---------------------------------------------------------------------------
# touched_pin_sites
# ---------------------------------------------------------------------------


def test_touched_pin_sites_finds_the_freshness_gate():
    sites = pmv.touched_pin_sites(
        ["python/tests/gates/test_planner_relocation_freshness.py", "README.md"]
    )
    assert sites == ("python/tests/gates/test_planner_relocation_freshness.py",)


def test_touched_pin_sites_finds_all_three_when_all_three_move():
    changed = [
        "python/tests/gates/test_planner_relocation_freshness.py",
        ".github/workflows/parity.yml",
        ".github/workflows/ci.yml",
        "docs/ROADMAP.md",
    ]
    sites = pmv.touched_pin_sites(changed)
    assert set(sites) == {
        "python/tests/gates/test_planner_relocation_freshness.py",
        ".github/workflows/parity.yml",
        ".github/workflows/ci.yml",
    }


def test_touched_pin_sites_empty_when_nothing_matches():
    assert pmv.touched_pin_sites(["python/tan/cli.py", "CHANGELOG.md"]) == ()


def test_touched_pin_sites_empty_diff():
    assert pmv.touched_pin_sites([]) == ()


def test_touched_pin_sites_normalises_windows_separators():
    # A `git diff` invoked from a Windows dev box can hand back backslash
    # separators; the sender's own CI runs on ubuntu-latest, but the unit
    # test suite runs wherever a developer's machine is -- this must not
    # depend on which one produced the list.
    sites = pmv.touched_pin_sites([r".github\workflows\parity.yml"])
    assert sites == (".github/workflows/parity.yml",)


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_pin_sites_docstring_names_every_dict_entry():
    # Every PIN_SITES key must (1) read as a real repo-relative path, no
    # leading slash, no backslash, (2) actually EXIST on disk under
    # REPO_ROOT, and (3) be named verbatim in pmv.__doc__ -- the module's
    # own sweep narrative. (2) is load-bearing, not decorative: a rename of
    # one of these files (`.github/workflows/parity.yml` -> anything else)
    # makes `git diff --name-only` emit only the DESTINATION path, so
    # `touched_pin_sites` stops matching that PIN_SITES entry permanently --
    # exactly the tan-cli#275 / commit 8633d6b shape (a self-disabling gate
    # that never goes red). The previous version of this test asserted only
    # non-absoluteness and no-backslash, which is trivially true of any path
    # literal and would not have caught a rename at all.
    assert pmv.__doc__ is not None
    for path in pmv.PIN_SITES:
        assert not path.startswith("/"), path
        assert "\\" not in path, path
        assert (REPO_ROOT / path).is_file(), (
            f"{path} is a PIN_SITES key but does not exist on disk under "
            f"{REPO_ROOT} -- renamed or deleted without updating PIN_SITES?"
        )
        assert path in pmv.__doc__, (
            f"{path} is a PIN_SITES key but is not named verbatim in "
            "pin_move_verify.py's own module docstring -- the sweep "
            "narrative has drifted from the dict it describes."
        )


# ---------------------------------------------------------------------------
# extract_pinned_sdk_tag
# ---------------------------------------------------------------------------


def test_extract_pinned_sdk_tag_reads_the_live_value():
    text = "name: parity\njobs:\n  x:\n    env:\n      PINNED_SDK_TAG: bd8be484680cf5aa1c1ac0e8b38d84128b5a279d\n"
    assert pmv.extract_pinned_sdk_tag(text) == "bd8be484680cf5aa1c1ac0e8b38d84128b5a279d"


def test_extract_pinned_sdk_tag_tolerates_leading_indentation():
    text = "  PINNED_SDK_TAG:    v0.16.0-rc1   \n"
    assert pmv.extract_pinned_sdk_tag(text) == "v0.16.0-rc1"


def test_extract_pinned_sdk_tag_refuses_zero_matches():
    with pytest.raises(pmv.PayloadError, match="found 0"):
        pmv.extract_pinned_sdk_tag("name: parity\njobs: {}\n")


def test_extract_pinned_sdk_tag_refuses_two_matches():
    text = "PINNED_SDK_TAG: aaa\nPINNED_SDK_TAG: bbb\n"
    with pytest.raises(pmv.PayloadError, match="found 2"):
        pmv.extract_pinned_sdk_tag(text)


def test_extract_pinned_sdk_tag_against_the_real_file():
    # The live source of truth, not a fixture -- this is the one test that
    # would catch parity.yml itself drifting into a shape this parser can't
    # read (e.g. the env block getting quoted, or a second PINNED_SDK_TAG
    # sneaking into a comment at column 0).
    real = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "parity.yml"
    ref = pmv.extract_pinned_sdk_tag(real.read_text(encoding="utf-8"))
    assert pmv._SHA.match(ref) or pmv._REF.match(ref)


# ---------------------------------------------------------------------------
# extract_ci_sdk_parity_ref / extract_freshness_pins
# ---------------------------------------------------------------------------

_CI_CHECKOUT = """      - uses: actions/checkout@abc # v7.0.1
        with:
          persist-credentials: false
          repository: alplabai/alp-sdk
          ref: 88318e759958529fbbd8fe9d481373681c0fa78d
          path: alp-sdk
"""


def test_extract_ci_sdk_parity_ref_reads_the_ref_under_the_alp_sdk_repository():
    assert (
        pmv.extract_ci_sdk_parity_ref(_CI_CHECKOUT)
        == "88318e759958529fbbd8fe9d481373681c0fa78d"
    )


def test_extract_ci_sdk_parity_ref_ignores_an_unrelated_checkout_ref():
    # Anchoring on `repository: alplabai/alp-sdk` rather than "the only
    # `ref:` in the file" is the whole point -- an unrelated checkout's ref
    # must not be graded as the sdk_parity pin.
    unrelated = """      - uses: actions/checkout@abc
        with:
          ref: v1.2.3
"""
    text = unrelated + _CI_CHECKOUT
    assert (
        pmv.extract_ci_sdk_parity_ref(text) == "88318e759958529fbbd8fe9d481373681c0fa78d"
    )


def test_extract_ci_sdk_parity_ref_refuses_zero_matches():
    with pytest.raises(pmv.PayloadError, match="found 0"):
        pmv.extract_ci_sdk_parity_ref("jobs:\n  x:\n    steps: []\n")


def test_extract_ci_sdk_parity_ref_refuses_two_matches():
    with pytest.raises(pmv.PayloadError, match="found 2"):
        pmv.extract_ci_sdk_parity_ref(_CI_CHECKOUT + _CI_CHECKOUT)


# A second alp-sdk checkout the two-line pattern CANNOT see is the dangerous
# case, not the identical-block one above: it leaves exactly one match, so an
# extractor that counted only matches would return the block it happens to
# read and report a move of the OTHER ref as `moved: false`. Each shape below
# was measured returning happily before `_ALP_SDK_CHECKOUT`'s independent
# count landed.
_SECOND_CHECKOUT_SHAPES = {
    "reordered keys": """          ref: bbb
          repository: alplabai/alp-sdk
""",
    "path line between the two keys": """          repository: alplabai/alp-sdk
          path: two
          ref: bbb
""",
    "inline flow map": """          with: {repository: alplabai/alp-sdk, ref: bbb}
""",
    "quoted repo name": """          repository: "alplabai/alp-sdk"
          path: two
          ref: bbb
""",
}


@pytest.mark.parametrize("shape", sorted(_SECOND_CHECKOUT_SHAPES))
def test_extract_ci_sdk_parity_ref_refuses_an_unparseable_second_checkout(shape):
    with pytest.raises(pmv.PayloadError, match="2 alp-sdk checkout"):
        pmv.extract_ci_sdk_parity_ref(_CI_CHECKOUT + _SECOND_CHECKOUT_SHAPES[shape])


def test_extract_freshness_pins_refuses_an_unparseable_second_assignment():
    # Same shape one file over: an INDENTED fourth assignment is invisible to
    # the line-anchored pattern, so only the independent `_FRESHNESS_PIN_ANY`
    # count catches it.
    text = _FRESHNESS_PINS + '    PINNED_SDK_COMMIT = "' + "e" * 40 + '"' + chr(10)
    with pytest.raises(pmv.PayloadError, match="refusing to guess"):
        pmv.extract_freshness_pins(text)


def test_extract_pinned_sdk_tag_refuses_an_unparseable_second_pin():
    # A flow-mapped second pin: invisible to the line-anchored pattern, so
    # only the independent `PINNED_SDK_TAG:` count refuses it.
    text = """env:
  PINNED_SDK_TAG: aaa
jobs: {x: {env: {PINNED_SDK_TAG: bbb}}}
"""
    with pytest.raises(pmv.PayloadError, match="occurrence"):
        pmv.extract_pinned_sdk_tag(text)


def test_extract_ci_sdk_parity_ref_against_the_real_file():
    real = Path(__file__).resolve().parents[3] / pmv.CI_YML
    ref = pmv.extract_ci_sdk_parity_ref(real.read_text(encoding="utf-8"))
    assert pmv._SHA.match(ref) or pmv._REF.match(ref)


_FRESHNESS_PINS = (
    'PINNED_SDK_COMMIT = "' + "a" * 40 + '"  # alp-sdk origin/main\n'
    'HAND_PORT_PINNED_SDK_COMMIT = "' + "b" * 40 + '"  # see above\n'
    'STRICT_LOADERS_PINNED_SDK_COMMIT = "' + "c" * 40 + '"  # frozen\n'
)


def test_extract_freshness_pins_reads_all_three_name_sorted():
    assert pmv.extract_freshness_pins(_FRESHNESS_PINS) == ("b" * 40, "a" * 40, "c" * 40)


def test_extract_freshness_pins_refuses_a_missing_pin():
    text = _FRESHNESS_PINS.replace('STRICT_LOADERS_PINNED_SDK_COMMIT = "' + "c" * 40 + '"', "")
    with pytest.raises(pmv.PayloadError, match="refusing to guess"):
        pmv.extract_freshness_pins(text)


def test_extract_freshness_pins_refuses_a_duplicated_pin():
    with pytest.raises(pmv.PayloadError, match="refusing to guess"):
        pmv.extract_freshness_pins(_FRESHNESS_PINS + _FRESHNESS_PINS)


def test_extract_freshness_pins_against_the_real_file():
    real = Path(__file__).resolve().parents[3] / pmv.FRESHNESS_GATE
    pins = pmv.extract_freshness_pins(real.read_text(encoding="utf-8"))
    assert len(pins) == len(pmv.PIN_SITES[pmv.FRESHNESS_GATE])
    assert all(pmv._SHA.match(p) for p in pins)


def test_every_pin_site_has_an_extractor_today():
    # Not a structural requirement -- a site without one degrades to
    # touch-based (always MOVED) behaviour, which is the SAFE direction. This
    # asserts the state as of now: all three are value-gated, so a future
    # deletion of an extractor is a deliberate act, not an accident.
    assert set(pmv.PIN_VALUE_EXTRACTORS) == set(pmv.PIN_SITES)


# ---------------------------------------------------------------------------
# moved_pin_sites -- touched is not moved (ADR-0029 clause 2/3)
# ---------------------------------------------------------------------------

PARITY_AT_BASE = "env:\n  PINNED_SDK_TAG: " + "a" * 40 + "\n"
PARITY_AT_HEAD_SAME_PIN = "# a comment the diff added\n" + PARITY_AT_BASE
PARITY_AT_HEAD_MOVED = "env:\n  PINNED_SDK_TAG: " + "d" * 40 + "\n"


def _reader(tree):
    """`read_at(rev, path)` backed by a dict; a missing key is a file that
    does not exist at that revision, exactly like `_git_show` returning None.
    """
    return lambda rev, path: tree.get((rev, path))


def _moved(tree, sites=(pmv.PARITY_YML,)):
    return pmv.moved_pin_sites(sites, _reader(tree), base="BASE", head="HEAD")


def test_moved_pin_sites_touched_but_value_unchanged_is_not_moved():
    # The tan-cli PR #848 shape: `parity.yml` edited (Renode steps removed),
    # `PINNED_SDK_TAG` byte-identical. ADR-0029 clause 2/3 gate on the VALUE,
    # so there is nothing to dispatch.
    assert (
        _moved(
            {
                ("BASE", pmv.PARITY_YML): PARITY_AT_BASE,
                ("HEAD", pmv.PARITY_YML): PARITY_AT_HEAD_SAME_PIN,
            }
        )
        == ()
    )


def test_moved_pin_sites_value_changed_is_moved():
    assert _moved(
        {
            ("BASE", pmv.PARITY_YML): PARITY_AT_BASE,
            ("HEAD", pmv.PARITY_YML): PARITY_AT_HEAD_MOVED,
        }
    ) == (pmv.PARITY_YML,)


def test_moved_pin_sites_extraction_failure_is_moved():
    # THE non-negotiable direction: a shape the extractor cannot read must
    # never downgrade to "no dispatch needed" -- that would turn this
    # narrowing into the hole the gate exists to close.
    assert _moved(
        {
            ("BASE", pmv.PARITY_YML): PARITY_AT_BASE,
            ("HEAD", pmv.PARITY_YML): "env:\n  # PINNED_SDK_TAG went away\n",
        }
    ) == (pmv.PARITY_YML,)


def test_moved_pin_sites_extraction_failure_at_base_is_moved():
    assert _moved(
        {
            ("BASE", pmv.PARITY_YML): "env: {}\n",
            ("HEAD", pmv.PARITY_YML): PARITY_AT_BASE,
        }
    ) == (pmv.PARITY_YML,)


@pytest.fixture
def spy_extractor(monkeypatch):
    """Records every text `parity.yml`'s extractor is handed, and never
    raises. Both file-absent tests below need this: with the `before is None
    or after is None` guard deleted, the real extractor blows up on `None`
    and the catch-all returns True, so asserting only the RESULT leaves that
    guard unpinned (measured: it does). Asserting the extractor was never
    CALLED is what fails the moment the guard goes.
    """
    seen: list = []
    monkeypatch.setitem(
        pmv.PIN_VALUE_EXTRACTORS, pmv.PARITY_YML, lambda text: seen.append(text) or ("v",)
    )
    return seen


def test_moved_pin_sites_file_added_is_moved(spy_extractor):
    assert _moved({("HEAD", pmv.PARITY_YML): PARITY_AT_BASE}) == (pmv.PARITY_YML,)
    assert spy_extractor == []


def test_moved_pin_sites_file_deleted_is_moved(spy_extractor):
    assert _moved({("BASE", pmv.PARITY_YML): PARITY_AT_BASE}) == (pmv.PARITY_YML,)
    assert spy_extractor == []


def test_moved_pin_sites_reader_raising_is_moved():
    def boom(rev, path):
        raise OSError("git is not on PATH")

    assert pmv.moved_pin_sites([pmv.PARITY_YML], boom, base="BASE", head="HEAD") == (
        pmv.PARITY_YML,
    )


def test_moved_pin_sites_site_without_an_extractor_is_moved():
    # A `PIN_SITES` entry added without an extractor must be MOVED whenever
    # it is touched -- adding the path alone is always the safe half-step.
    #
    # The result alone does NOT pin the `extract is None` guard: with it
    # deleted, `extract(text)` raises TypeError and the catch-all returns True
    # anyway, so the assertion below would still pass. Asserting that NOTHING
    # WAS READ is what pins it -- with the guard gone, `read_at` runs first.
    reads = []

    def reader(rev, path):
        reads.append((rev, path))
        return "x"

    assert pmv.moved_pin_sites(
        ["docs/some-future-pin-site.md"], reader, base="BASE", head="HEAD"
    ) == ("docs/some-future-pin-site.md",)
    assert reads == []


def test_moved_pin_sites_untouched_tree_is_neither():
    assert pmv.moved_pin_sites([], _reader({}), base="BASE", head="HEAD") == ()


def test_moved_pin_sites_reports_only_the_sites_that_moved():
    tree = {
        ("BASE", pmv.PARITY_YML): PARITY_AT_BASE,
        ("HEAD", pmv.PARITY_YML): PARITY_AT_HEAD_SAME_PIN,
        ("BASE", pmv.FRESHNESS_GATE): _FRESHNESS_PINS,
        ("HEAD", pmv.FRESHNESS_GATE): _FRESHNESS_PINS.replace("a" * 40, "e" * 40),
    }
    assert _moved(tree, sites=(pmv.PARITY_YML, pmv.FRESHNESS_GATE)) == (pmv.FRESHNESS_GATE,)


# ---------------------------------------------------------------------------
# build_tuple / PinMoveTuple
# ---------------------------------------------------------------------------

VALID_SHA = "bd8be484680cf5aa1c1ac0e8b38d84128b5a279d"


def test_build_tuple_happy_path():
    t = pmv.build_tuple(
        tan_ref="v0.5.1",
        sdk_ref="v0.16.0-rc1",
        source_sha=VALID_SHA,
        source_pr="820",
    )
    assert t.tan_ref == "v0.5.1"
    assert t.sdk_ref == "v0.16.0-rc1"
    assert t.soms == pmv.DEFAULT_SOMS
    assert t.source_pr == 820
    assert t.source_repo == "alplabai/tan-cli"


def test_build_tuple_check_name_matches_the_receiver_byte_for_byte():
    t = pmv.build_tuple(tan_ref="v0.5.1", sdk_ref="v0.16.0-rc1", source_sha=VALID_SHA)
    assert t.check_name == "pin-verify · v0.5.1 × v0.16.0-rc1"


def test_build_tuple_dispatch_body_shape():
    t = pmv.build_tuple(
        tan_ref="v0.5.1", sdk_ref="v0.16.0-rc1", source_sha=VALID_SHA, source_pr=1
    )
    body = t.as_dispatch_body()
    assert body["event_type"] == "pin-move-verify"
    payload = body["client_payload"]
    assert payload == {
        "tan_ref": "v0.5.1",
        "sdk_ref": "v0.16.0-rc1",
        "soms": list(pmv.DEFAULT_SOMS),
        "source_repo": "alplabai/tan-cli",
        "source_sha": VALID_SHA,
        "source_pr": 1,
    }


def test_build_tuple_null_pr_round_trips_as_none():
    t = pmv.build_tuple(tan_ref="v0.5.1", sdk_ref="v0.16.0-rc1", source_sha=VALID_SHA)
    assert t.source_pr is None
    assert t.as_client_payload()["source_pr"] is None


@pytest.mark.parametrize("bad_sha", ["short", "g" * 40, "", "  ", "x" * 39])
def test_build_tuple_refuses_a_bad_source_sha(bad_sha):
    with pytest.raises(pmv.PayloadError):
        pmv.build_tuple(tan_ref="v0.5.1", sdk_ref="v0.16.0-rc1", source_sha=bad_sha)


def test_build_tuple_refuses_an_injection_shaped_ref():
    # `--upload-pack=...` reaches `git clone --branch "$REF"` inside the
    # receiver's container -- refusing it here is the same defence
    # alpe2e.pinverify._REF documents, applied before the network call.
    with pytest.raises(pmv.PayloadError):
        pmv.build_tuple(tan_ref="--upload-pack=x", sdk_ref="v0.16.0-rc1", source_sha=VALID_SHA)


def test_build_tuple_refuses_empty_soms():
    with pytest.raises(pmv.PayloadError, match="nothing to verify"):
        pmv.build_tuple(tan_ref="v0.5.1", sdk_ref="v0.16.0-rc1", source_sha=VALID_SHA, soms=[])


def test_build_tuple_dedupes_soms_preserving_order():
    t = pmv.build_tuple(
        tan_ref="v0.5.1",
        sdk_ref="v0.16.0-rc1",
        source_sha=VALID_SHA,
        soms=["E1M-AEN801", "E1M-V2N101", "E1M-AEN801"],
    )
    assert t.soms == ("E1M-AEN801", "E1M-V2N101")


def test_build_tuple_refuses_bad_pr():
    with pytest.raises(pmv.PayloadError):
        pmv.build_tuple(tan_ref="v0.5.1", sdk_ref="x", source_sha=VALID_SHA, source_pr="0")
    with pytest.raises(pmv.PayloadError):
        pmv.build_tuple(tan_ref="v0.5.1", sdk_ref="x", source_sha=VALID_SHA, source_pr="not-a-number")


def test_build_tuple_accepts_exactly_max_soms():
    # The receiver's own cap (alpe2e.pinverify.MAX_SOMS) is 8 -- the boundary
    # itself must still be accepted, not just anything under it.
    soms = [f"E1M-X{i}" for i in range(pmv.MAX_SOMS)]
    t = pmv.build_tuple(tan_ref="v0.5.1", sdk_ref="v0.16.0-rc1", source_sha=VALID_SHA, soms=soms)
    assert len(t.soms) == pmv.MAX_SOMS


def test_build_tuple_refuses_more_than_max_soms():
    # Mirrors the receiver's alpe2e.pinverify.MAX_SOMS = 8: the receiver's
    # `plan` job refuses a payload over the cap, and its `report` job
    # (`if: always() && needs.plan.result == 'success'`) then never posts a
    # Check Run -- so a payload this sender let through would silently burn
    # the workflow's whole poll deadline. Refusing here, client-side, is the
    # entire point of validating client-side at all (tan-cli#358's shape,
    # avoided).
    soms = [f"E1M-X{i}" for i in range(pmv.MAX_SOMS + 1)]
    with pytest.raises(pmv.PayloadError, match=f"cap is {pmv.MAX_SOMS}"):
        pmv.build_tuple(tan_ref="v0.5.1", sdk_ref="v0.16.0-rc1", source_sha=VALID_SHA, soms=soms)


def test_build_tuple_soms_cap_is_after_dedupe():
    # A caller that (accidentally or not) repeats the same SoM MAX_SOMS+1
    # times is not actually asking for more than MAX_SOMS distinct builds --
    # the cap is a cost statement about DISTINCT SoMs, matching how the
    # receiver's own MAX_SOMS check reads `len(seen)` after its own dedupe.
    soms = ["E1M-V2N101"] * (pmv.MAX_SOMS + 5)
    t = pmv.build_tuple(tan_ref="v0.5.1", sdk_ref="v0.16.0-rc1", source_sha=VALID_SHA, soms=soms)
    assert t.soms == ("E1M-V2N101",)


def test_build_tuple_default_source_repo_is_this_repo():
    t = pmv.build_tuple(tan_ref="v0.5.1", sdk_ref="v0.16.0-rc1", source_sha=VALID_SHA)
    assert t.source_repo == "alplabai/tan-cli"


def test_build_tuple_accepts_an_explicit_source_repo():
    # `${{ github.repository }}` reaches this rather than the hardcoded
    # default, so a fork of this repo does not silently claim to be
    # alplabai/tan-cli in its own dispatch.
    t = pmv.build_tuple(
        tan_ref="v0.5.1",
        sdk_ref="v0.16.0-rc1",
        source_sha=VALID_SHA,
        source_repo="someone/tan-cli-fork",
    )
    assert t.source_repo == "someone/tan-cli-fork"


# ---------------------------------------------------------------------------
# judge_polled_check_run -- the fail-closed core
# ---------------------------------------------------------------------------

EXPECTED_CHECK_NAME = "pin-verify · v0.5.1 × v0.16.0-rc1"
EXPECTED_SOURCE_SHA = VALID_SHA
EXPECTED_TAN_REF = "v0.5.1"


def _judge(check_run, *, timed_out=False):
    return pmv.judge_polled_check_run(
        check_run,
        timed_out=timed_out,
        expected_check_name=EXPECTED_CHECK_NAME,
        expected_source_sha=EXPECTED_SOURCE_SHA,
        tan_ref=EXPECTED_TAN_REF,
    )


def _completed(conclusion, **extra):
    return {
        "name": EXPECTED_CHECK_NAME,
        "head_sha": EXPECTED_SOURCE_SHA,
        "status": "completed",
        "conclusion": conclusion,
        **extra,
    }


def test_judge_success_passes():
    verdict, reason = _judge(_completed("success", output={"title": "ok"}))
    assert verdict is pmv.Verdict.PASS
    assert "ok" in reason


def test_judge_success_names_the_published_tan_ref_not_this_prs_code():
    # PR #823 review, finding 5: a reader of a green $GITHUB_STEP_SUMMARY
    # line must see that the tuple was verified against the PUBLISHED tan
    # release, not this PR's own diff -- not just the workflow header or the
    # module docstring, which a reader of the summary line never sees.
    verdict, reason = _judge(_completed("success"))
    assert verdict is pmv.Verdict.PASS
    assert EXPECTED_TAN_REF in reason
    assert "PUBLISHED" in reason
    assert "NOT" in reason and "this PR's own code" in reason


def test_judge_failure_fails():
    verdict, _ = _judge(_completed("failure"))
    assert verdict is pmv.Verdict.FAIL


def test_judge_neutral_fails_even_though_github_would_pass_it():
    # The whole point: GitHub's own required-check semantics treat `neutral`
    # as satisfying a required check. This sender must not.
    verdict, reason = _judge(_completed("neutral"))
    assert verdict is pmv.Verdict.FAIL
    assert "neutral" in reason.lower() or "not 'success'" in reason


def test_judge_no_check_run_and_timed_out_fails_closed():
    verdict, reason = _judge(None, timed_out=True)
    assert verdict is pmv.Verdict.FAIL
    assert "timeout" in reason.lower()


def test_judge_no_check_run_and_no_timeout_fails_closed():
    # Should never happen in practice (the caller only stops polling on a
    # completed run or a timeout) -- but if it does, this must not read as a
    # pass by omission.
    verdict, _ = _judge(None, timed_out=False)
    assert verdict is pmv.Verdict.FAIL


def test_judge_empty_mapping_treated_as_missing():
    verdict, _ = _judge({}, timed_out=True)
    assert verdict is pmv.Verdict.FAIL


def test_judge_still_in_progress_fails_closed():
    verdict, reason = _judge(
        {
            "name": EXPECTED_CHECK_NAME,
            "head_sha": EXPECTED_SOURCE_SHA,
            "status": "in_progress",
            "conclusion": None,
        },
        timed_out=True,
    )
    assert verdict is pmv.Verdict.FAIL
    assert "in_progress" in reason


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "action_required", "stale", None])
def test_judge_every_non_success_conclusion_fails_closed(conclusion):
    verdict, _ = _judge(_completed(conclusion))
    assert verdict is pmv.Verdict.FAIL


# ---------------------------------------------------------------------------
# judge_polled_check_run -- identity (PR #823 review, finding 7)
#
# `check_name` (`pin-verify · <tan_ref> × <sdk_ref>`) encodes only
# tan_ref/sdk_ref, not `soms` -- a first-class member of ADR-0029's tuple.
# The receiver's `workflow_dispatch` entrypoint means a hand-run with the
# same two refs but a DIFFERENT SoM set lands under the same name on the
# same commit, so `judge_polled_check_run` must not grade whatever it is
# handed just because SOME check run with that name showed up.
# ---------------------------------------------------------------------------


def test_judge_refuses_a_check_run_with_the_wrong_name():
    run = _completed("success")
    run["name"] = "pin-verify · v0.5.1 × v9.9.9-someone-elses-dispatch"
    verdict, reason = _judge(run)
    assert verdict is pmv.Verdict.FAIL
    assert "identity" in reason.lower()
    assert EXPECTED_CHECK_NAME in reason


def test_judge_refuses_a_check_run_with_the_wrong_head_sha():
    run = _completed("success")
    run["head_sha"] = "0" * 40
    verdict, reason = _judge(run)
    assert verdict is pmv.Verdict.FAIL
    assert "identity" in reason.lower()
    assert EXPECTED_SOURCE_SHA in reason


def test_judge_identity_mismatch_is_checked_before_conclusion():
    # A wrong-identity run with a `success` conclusion must still fail --
    # the identity check has to run BEFORE the conclusion is trusted, not
    # after, or a same-named check from an unrelated dispatch could pass
    # this one by pure coincidence of timing.
    run = _completed("success")
    run["head_sha"] = "1" * 40
    verdict, _ = _judge(run)
    assert verdict is pmv.Verdict.FAIL


def test_git_show_returns_none_when_git_fails_even_with_stdout(monkeypatch, tmp_path: Path):
    # `if out.returncode == 0 else None` is otherwise unpinned: returning
    # stdout unconditionally leaves every other test green. A git that exits
    # non-zero having already written to stdout must still read as "absent",
    # never as a pin value -- extracting one from a partial/failed read is
    # exactly the silent-downgrade shape this module refuses.
    monkeypatch.setattr(
        pmv.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            ["git"], 128, "PINNED_SDK_TAG: leftover-from-a-failed-read", "fatal: bad object"
        ),
    )
    assert pmv._git_show(tmp_path, "nope", pmv.PARITY_YML) is None


# ---------------------------------------------------------------------------
# CLI subcommands -- thin wrappers, exercised through subprocess so the
# argparse wiring and exit codes are proven, not just the functions behind it
# ---------------------------------------------------------------------------


def _run(*args: str, cwd: Path | None = None, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "pin_move_verify.py"), *args],
        cwd=str(cwd) if cwd else None,
        input=input_text,
        capture_output=True,
        text=True,
    )


def test_cli_dispatch_body_happy_path():
    proc = _run(
        "dispatch-body",
        "--tan-ref", "v0.5.1",
        "--sdk-ref", "v0.16.0-rc1",
        "--source-sha", VALID_SHA,
        "--source-pr", "820",
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["client_payload"]["source_pr"] == 820


def test_cli_dispatch_body_refuses_a_bad_tuple():
    proc = _run(
        "dispatch-body",
        "--tan-ref", "v0.5.1",
        "--sdk-ref", "v0.16.0-rc1",
        "--source-sha", "not-a-sha",
    )
    assert proc.returncode == 2
    assert "refusing this tuple" in proc.stderr


def test_cli_dispatch_body_refuses_more_than_max_soms():
    args = ["dispatch-body", "--tan-ref", "v0.5.1", "--sdk-ref", "v0.16.0-rc1",
            "--source-sha", VALID_SHA]
    for i in range(pmv.MAX_SOMS + 1):
        args += ["--som", f"E1M-X{i}"]
    proc = _run(*args)
    assert proc.returncode == 2
    assert f"cap is {pmv.MAX_SOMS}" in proc.stderr


def test_cli_dispatch_body_honours_an_explicit_source_repo():
    # `${{ github.repository }}` reaches here rather than the hardcoded
    # SOURCE_REPO default (PR #823 review, finding 10).
    proc = _run(
        "dispatch-body",
        "--tan-ref", "v0.5.1",
        "--sdk-ref", "v0.16.0-rc1",
        "--source-sha", VALID_SHA,
        "--source-repo", "someone/tan-cli-fork",
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["client_payload"]["source_repo"] == "someone/tan-cli-fork"


_JUDGE_IDENTITY_ARGS = [
    "--expected-check-name", EXPECTED_CHECK_NAME,
    "--expected-source-sha", EXPECTED_SOURCE_SHA,
    "--tan-ref", EXPECTED_TAN_REF,
]


def test_cli_judge_success_exits_zero():
    doc = json.dumps(_completed("success"))
    proc = _run("judge", "--check-run", "-", *_JUDGE_IDENTITY_ARGS, input_text=doc)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("pass:")
    assert EXPECTED_TAN_REF in proc.stdout


def test_cli_judge_failure_exits_one():
    doc = json.dumps(_completed("failure"))
    proc = _run("judge", "--check-run", "-", *_JUDGE_IDENTITY_ARGS, input_text=doc)
    assert proc.returncode == 1
    assert proc.stdout.startswith("fail:")


def test_cli_judge_timed_out_with_no_run_exits_one():
    proc = _run("judge", "--check-run", "-", "--timed-out", *_JUDGE_IDENTITY_ARGS, input_text="")
    assert proc.returncode == 1
    assert "timeout" in proc.stdout.lower()


def test_cli_judge_wrong_identity_exits_one():
    run = _completed("success")
    run["head_sha"] = "0" * 40
    proc = _run("judge", "--check-run", "-", *_JUDGE_IDENTITY_ARGS, input_text=json.dumps(run))
    assert proc.returncode == 1
    assert "identity" in proc.stdout.lower()


def test_cli_sdk_ref_reads_the_real_parity_yml():
    real = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "parity.yml"
    proc = _run("sdk-ref", "--parity-yml", str(real))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip()


def test_cli_detect_against_a_real_git_repo(tmp_path: Path):
    # A throwaway repo, not this one -- exercises the git-diff plumbing
    # without depending on tan-cli's own history (which changes under this
    # test as the repo evolves).
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / "python" / "tests" / "gates").mkdir(parents=True)

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "test")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    git("branch", "-q", "-m", "base-branch")

    git("checkout", "-q", "-b", "feature")
    (repo / ".github" / "workflows" / "parity.yml").write_text(
        "env:\n  PINNED_SDK_TAG: newsha\n", encoding="utf-8"
    )
    git("add", "-A")
    git("commit", "-q", "-m", "bump the pin")

    proc = _run("detect", "--base", "base-branch", "--head", "feature", "--repo-root", str(repo))
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["touched"] is True
    assert doc["sites"] == [".github/workflows/parity.yml"]
    # The file does not exist at base at all -- one of the conservative
    # shapes, so MOVED even though there is no "before" value to compare.
    assert doc["moved"] is True
    assert doc["moved_sites"] == [".github/workflows/parity.yml"]


def test_cli_detect_reports_untouched_when_no_pin_site_moves(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "test")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    git("branch", "-q", "-m", "base-branch")

    git("checkout", "-q", "-b", "feature")
    (repo / "README.md").write_text("x\ny\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "unrelated docs edit")

    proc = _run("detect", "--base", "base-branch", "--head", "feature", "--repo-root", str(repo))
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["touched"] is False
    assert doc["sites"] == []
    assert doc["moved"] is False
    assert doc["moved_sites"] == []


def _repo_with_parity_yml(tmp_path: Path, head_text: str | None) -> Path:
    """A throwaway repo carrying `parity.yml` on `base-branch`, plus a
    `feature` branch that rewrites it to `head_text` -- or, with `None`, an
    empty `feature` branch for the caller to change however it likes.
    """
    repo = tmp_path / "repo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    parity = repo / ".github" / "workflows" / "parity.yml"

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "test")
    parity.write_text(PARITY_AT_BASE, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    git("branch", "-q", "-m", "base-branch")

    git("checkout", "-q", "-b", "feature")
    if head_text is not None:
        parity.write_text(head_text, encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "edit parity.yml")
    return repo


def test_cli_detect_touched_but_no_value_moved(tmp_path: Path):
    # End-to-end over real git objects, the tan-cli PR #848 shape: the pin
    # site is in the diff, `PINNED_SDK_TAG` is byte-identical. `touched`
    # stays true (the summary still says which file), `moved` -- the flag
    # the workflow gates on -- is false.
    repo = _repo_with_parity_yml(tmp_path, PARITY_AT_HEAD_SAME_PIN)
    proc = _run("detect", "--base", "base-branch", "--head", "feature", "--repo-root", str(repo))
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["touched"] is True
    assert doc["sites"] == [".github/workflows/parity.yml"]
    assert doc["moved"] is False
    assert doc["moved_sites"] == []


def test_cli_detect_reports_a_moved_pin_value(tmp_path: Path):
    repo = _repo_with_parity_yml(tmp_path, PARITY_AT_HEAD_MOVED)
    proc = _run("detect", "--base", "base-branch", "--head", "feature", "--repo-root", str(repo))
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["touched"] is True
    assert doc["moved"] is True
    assert doc["moved_sites"] == [".github/workflows/parity.yml"]


def test_cli_detect_deleted_pin_site_is_moved(tmp_path: Path):
    # End-to-end over real git objects. This one does NOT pin the file-absent
    # guard on its own (deleting the guard leaves it green -- the extractor
    # raises on `None` and the catch-all returns True); its unit-level twin
    # `test_moved_pin_sites_file_deleted_is_moved` carries that. What this
    # proves is the `_git_show`-to-`moved_sites` plumbing on a real deletion.
    repo = _repo_with_parity_yml(tmp_path, None)
    for args in (
        ["rm", "-q", ".github/workflows/parity.yml"],
        ["commit", "-q", "-m", "delete the pin site"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    proc = _run("detect", "--base", "base-branch", "--head", "feature", "--repo-root", str(repo))
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["touched"] is True
    assert doc["moved"] is True
    assert doc["moved_sites"] == [".github/workflows/parity.yml"]
