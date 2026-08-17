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
