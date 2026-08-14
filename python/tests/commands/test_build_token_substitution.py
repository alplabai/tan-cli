# SPDX-License-Identifier: Apache-2.0
import shutil
import subprocess

import pytest

from tan.commands.build.token_substitution import (
    TokenSubstitutionError,
    apply_plan_token_substitution,
)
from tan.core.build_plan import parse_build_plan

LEGACY_PLAN = """{
  "schemaVersion": 1, "generatedBy": "g", "boardYaml": "/work/proj/board.yaml", "sku": "S", "buildRoot": "build",
  "slices": [], "sharedArtefacts": [], "warnings": []
}"""


@pytest.fixture
def sdk_root(tmp_path):
    """A bare directory standing in for a resolved alp-sdk checkout -- this
    layer takes `sdk_root` pre-resolved (unlike tan-cli's real resolver), so
    the fixture only needs to exist on disk for `git -C <dir>` to be
    meaningful."""
    d = tmp_path / "sdk"
    d.mkdir()
    return d


def test_legacy_plan_is_untouched_no_op():
    plan = parse_build_plan(LEGACY_PLAN)
    out, demoted = apply_plan_token_substitution(
        plan,
        board_yaml_path="/work/proj/board.yaml",
        exec_base="/work/proj",
        sdk_root="/opt/alp-sdk",
        python="python3",
        toolchain_root=None,
    )
    assert out == plan
    assert demoted == []


def test_unknown_plan_path_mode_is_refused_before_any_guard_runs():
    """A board.yaml/exec_base pair that would ALSO fail the divergence guard
    -- proving the unknown-mode check short-circuits first."""
    json = """{
      "schemaVersion": 1, "generatedBy": "g", "planPathMode": "tokened-v2",
      "boardYaml": "/work/proj/examples/foo/board.yaml", "sku": "S", "buildRoot": "build",
      "slices": [], "sharedArtefacts": [], "warnings": []
    }"""
    plan = parse_build_plan(json)
    with pytest.raises(TokenSubstitutionError) as e:
        apply_plan_token_substitution(
            plan,
            board_yaml_path="/work/proj/examples/foo/board.yaml",
            exec_base="/work/proj",
            sdk_root="/opt/alp-sdk",
            python="python3",
            toolchain_root=None,
        )
    assert e.value.code == "build.plan-invalid"
    assert "tokened-v2" in e.value.message


def test_missing_board_yaml_path_is_refused():
    json = """{
      "schemaVersion": 1, "generatedBy": "g", "planPathMode": "tokened",
      "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
      "slices": [], "sharedArtefacts": [], "warnings": []
    }"""
    plan = parse_build_plan(json)
    with pytest.raises(TokenSubstitutionError) as e:
        apply_plan_token_substitution(
            plan,
            board_yaml_path=None,
            exec_base="/work/proj",
            sdk_root="/opt/alp-sdk",
            python="python3",
            toolchain_root=None,
        )
    assert e.value.code == "build.plan-invalid"


def test_project_root_mismatch_is_refused():
    json = """{
      "schemaVersion": 1, "generatedBy": "g", "planPathMode": "tokened",
      "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
      "slices": [], "sharedArtefacts": [], "warnings": []
    }"""
    plan = parse_build_plan(json)
    # board.yaml lives nested under the workspace root, but the exec base
    # stays the workspace root itself -- a real PROJECT_ROOT/exec-base split.
    with pytest.raises(TokenSubstitutionError) as e:
        apply_plan_token_substitution(
            plan,
            board_yaml_path="/work/proj/examples/foo/board.yaml",
            exec_base="/work/proj",
            sdk_root="/opt/alp-sdk",
            python="python3",
            toolchain_root=None,
        )
    assert e.value.code == "build.project-root-mismatch"
    assert "examples/foo" in e.value.message


def test_unresolved_sdk_root_is_refused_not_substituted_empty():
    """Regression: a tokened plan with no resolvable sdk_root must not
    degrade ${SDK_ROOT} to "" -- turning ${SDK_ROOT}/scripts into the bare
    /scripts sails right past the leftover-token guard."""
    json = """{
      "schemaVersion": 1, "generatedBy": "g", "planPathMode": "tokened",
      "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
      "slices": [
        { "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1", "appDir": "app",
          "configArtefacts": [], "toolchain": null, "artifacts": {}, "debug": {},
          "command": { "tool": "west", "args": ["build"], "cwd": "build/c1" },
          "env": { "ALP_SDK_ROOT": "${SDK_ROOT}/scripts" }, "envAppendPath": {} }
      ],
      "sharedArtefacts": [], "warnings": []
    }"""
    plan = parse_build_plan(json)
    with pytest.raises(TokenSubstitutionError) as e:
        apply_plan_token_substitution(
            plan,
            board_yaml_path="/work/proj/board.yaml",
            exec_base="/work/proj",
            sdk_root=None,
            python="python3",
            toolchain_root=None,
        )
    assert e.value.code == "build.sdk-root-unresolved"


def test_tokened_plan_with_matching_project_root_substitutes(sdk_root):
    json = """{
      "schemaVersion": 1, "generatedBy": "g", "planPathMode": "tokened",
      "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
      "slices": [
        { "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1", "appDir": "app",
          "configArtefacts": [], "toolchain": null, "artifacts": {}, "debug": {},
          "command": { "tool": "west", "args": ["build"], "cwd": "build/c1" },
          "env": { "ALP_SDK_ROOT": "${SDK_ROOT}" }, "envAppendPath": {} }
      ],
      "sharedArtefacts": [], "warnings": []
    }"""
    plan = parse_build_plan(json)
    out, demoted = apply_plan_token_substitution(
        plan,
        board_yaml_path="/work/proj/board.yaml",
        exec_base="/work/proj",
        sdk_root=str(sdk_root),
        python="python3",
        toolchain_root=None,
    )
    assert out.board_yaml == "/work/proj/board.yaml"
    assert out.slices[0].env["ALP_SDK_ROOT"] == str(sdk_root)
    assert demoted == []


def test_slice_confined_toolchain_root_is_demoted_not_a_hard_error(sdk_root):
    """tan-cli #89: an unresolved ${TOOLCHAIN_ROOT} confined to one slice's
    own field must not fail the whole substitution pass -- it comes back as
    a SliceDemotion for the executor to route through
    executionPolicy.missingTool at dispatch instead of erroring here."""
    json = """{
      "schemaVersion": 1, "generatedBy": "g", "planPathMode": "tokened",
      "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
      "slices": [
        { "coreId": "m33_sm", "backend": "zephyr", "buildDir": "build/c1", "appDir": "app",
          "configArtefacts": [], "toolchain": null, "artifacts": {}, "debug": {},
          "command": { "tool": "west", "args": ["build"], "cwd": "build/c1" },
          "env": { "ZEPHYR_SDK_INSTALL_DIR": "${TOOLCHAIN_ROOT}" }, "envAppendPath": {} }
      ],
      "sharedArtefacts": [], "warnings": []
    }"""
    plan = parse_build_plan(json)
    out, demoted = apply_plan_token_substitution(
        plan,
        board_yaml_path="/work/proj/board.yaml",
        exec_base="/work/proj",
        sdk_root=str(sdk_root),
        python="python3",
        toolchain_root=None,
    )
    assert len(demoted) == 1
    d = demoted[0]
    assert d.slice_index == 0
    assert d.core_id == "m33_sm"
    assert "slices[0].env.ZEPHYR_SDK_INSTALL_DIR" in d.reason
    assert "ZEPHYR_SDK_INSTALL_DIR" in d.reason or "west sdk install" in d.reason
    # The literal token survives in the (never-dispatched) output plan --
    # not substituted blank.
    assert out.slices[0].env["ZEPHYR_SDK_INSTALL_DIR"] == "${TOOLCHAIN_ROOT}"


def test_leftover_token_after_substitution_is_refused(sdk_root):
    json = """{
      "schemaVersion": 1, "generatedBy": "g", "planPathMode": "tokened",
      "boardYaml": "${UNKNOWN}/board.yaml", "sku": "S", "buildRoot": "build",
      "slices": [], "sharedArtefacts": [], "warnings": []
    }"""
    plan = parse_build_plan(json)
    with pytest.raises(TokenSubstitutionError) as e:
        apply_plan_token_substitution(
            plan,
            board_yaml_path="/work/proj/board.yaml",
            exec_base="/work/proj",
            sdk_root=str(sdk_root),
            python="python3",
            toolchain_root=None,
        )
    assert e.value.code == "build.plan-token-unresolved"
    assert "${UNKNOWN}" in e.value.message


def test_missing_git_head_is_no_signal_not_a_hard_error(sdk_root):
    """A resolved SDK root that is NOT a git checkout at all (no .git) -- the
    sdkCommit guard must treat "could not resolve HEAD" as no signal (an SDK
    release tarball is a normal, supported setup), not fail the build."""
    json = """{
      "schemaVersion": 1, "generatedBy": "g", "planPathMode": "tokened", "sdkCommit": "deadbee",
      "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
      "slices": [], "sharedArtefacts": [], "warnings": []
    }"""
    plan = parse_build_plan(json)
    out, demoted = apply_plan_token_substitution(
        plan,
        board_yaml_path="/work/proj/board.yaml",
        exec_base="/work/proj",
        sdk_root=str(sdk_root),
        python="python3",
        toolchain_root=None,
    )
    assert demoted == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git must be on PATH for this test")
def test_sdk_commit_mismatch_is_refused(sdk_root):
    def git(*args):
        # `encoding=`, not bare `text=True`: git localises its own messages, so
        # `check=True` capturing a failure decodes them with the platform locale
        # and a `UnicodeDecodeError` would replace the real assertion.
        return subprocess.run(
            ["git", "-C", str(sdk_root), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=True,
        )

    git("init", "-q")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "x")
    head = git("rev-parse", "--short", "HEAD").stdout.strip()

    json = """{
      "schemaVersion": 1, "generatedBy": "g", "planPathMode": "tokened", "sdkCommit": "0000000",
      "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
      "slices": [], "sharedArtefacts": [], "warnings": []
    }"""
    plan = parse_build_plan(json)
    assert plan.sdk_commit != head
    with pytest.raises(TokenSubstitutionError) as e:
        apply_plan_token_substitution(
            plan,
            board_yaml_path="/work/proj/board.yaml",
            exec_base="/work/proj",
            sdk_root=str(sdk_root),
            python="python3",
            toolchain_root=None,
        )
    assert e.value.code == "build.sdk-commit-mismatch"
    assert "0000000" in e.value.message


@pytest.mark.skipif(shutil.which("git") is None, reason="git must be on PATH for this test")
def test_sdk_commit_match_does_not_refuse(sdk_root):
    def git(*args):
        # See the sibling above: explicit `encoding=`, never the platform locale.
        return subprocess.run(
            ["git", "-C", str(sdk_root), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=True,
        )

    git("init", "-q")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "x")
    head = git("rev-parse", "--short", "HEAD").stdout.strip()

    json = f"""{{
      "schemaVersion": 1, "generatedBy": "g", "planPathMode": "tokened", "sdkCommit": "{head}",
      "boardYaml": "${{PROJECT_ROOT}}/board.yaml", "sku": "S", "buildRoot": "build",
      "slices": [], "sharedArtefacts": [], "warnings": []
    }}"""
    plan = parse_build_plan(json)
    out, demoted = apply_plan_token_substitution(
        plan,
        board_yaml_path="/work/proj/board.yaml",
        exec_base="/work/proj",
        sdk_root=str(sdk_root),
        python="python3",
        toolchain_root=None,
    )
    assert demoted == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git must be on PATH for this test")
def test_git_short_head_does_not_attribute_an_enclosing_repos_commit(tmp_path):
    """tan-cli#488 defect 4, second site: `git -C <root> ...` discovery walks
    UPWARD, so an SDK vendored with no `.git` of its own inside a customer's
    own application repository -- a setup this port explicitly supports --
    used to make `git_short_head` answer with the ENCLOSING app repo's HEAD
    instead of no signal (`""`). Left unfixed, a build stamps a plan's
    `sdkCommit` with the app repo's commit, and the split-brain guard in
    `apply_plan_token_substitution` compares the wrong repository's HEAD.
    """
    from tan.commands.build.token_substitution import _is_own_git_checkout, git_short_head

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        )

    git("init", "-q")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "outer")
    outer_head = git("rev-parse", "--short", "HEAD").stdout.strip()

    vendored_sdk = tmp_path / "vendor" / "alp-sdk"
    vendored_sdk.mkdir(parents=True)

    # Pre-fix: this returned `outer_head` (the enclosing app repo's HEAD).
    assert git_short_head(vendored_sdk) == ""
    assert _is_own_git_checkout(vendored_sdk) is False
    assert _is_own_git_checkout(tmp_path) is True

    # And the split-brain guard must not fire off that misattribution: a plan
    # captured with no real signal (`sdkCommit` from a checkout `git_short_head`
    # cannot see) must not be refused as a mismatch against `outer_head`.
    json = """{
      "schemaVersion": 1, "generatedBy": "g", "planPathMode": "tokened", "sdkCommit": "deadbee",
      "boardYaml": "${PROJECT_ROOT}/board.yaml", "sku": "S", "buildRoot": "build",
      "slices": [], "sharedArtefacts": [], "warnings": []
    }"""
    plan = parse_build_plan(json)
    assert plan.sdk_commit != outer_head
    out, demoted = apply_plan_token_substitution(
        plan,
        board_yaml_path="/work/proj/board.yaml",
        exec_base="/work/proj",
        sdk_root=str(vendored_sdk),
        python="python3",
        toolchain_root=None,
    )
    assert demoted == []
