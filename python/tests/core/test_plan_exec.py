# SPDX-License-Identifier: Apache-2.0
import os
from pathlib import Path

from tan.core.plan_exec import (
    ExecutionPolicy,
    PolicyAction,
    SdkStampAction,
    apply_env_append,
    assemble_slice_env,
    normalize_path,
    resolve_action,
    sdk_stamp_action,
    sdk_stamp_key,
)

SEP = os.pathsep


def test_apply_env_append_appends_and_dedups():
    base = [("PYTHONPATH", f"/a{SEP}/b")]
    apply_env_append(base, {"PYTHONPATH": ["/b", "/c"]})
    assert base == [("PYTHONPATH", f"/a{SEP}/b{SEP}/c")]


def test_extra_zephyr_modules_always_uses_semicolon():
    """CMake list -- ';' on EVERY platform. ':' on Linux makes west build fail
    configure with 'is not a valid zephyr module'."""
    base = [("EXTRA_ZEPHYR_MODULES", "/a")]
    apply_env_append(base, {"EXTRA_ZEPHYR_MODULES": ["/b"]})
    assert base == [("EXTRA_ZEPHYR_MODULES", "/a;/b")]


def test_apply_env_append_seeds_absent_var():
    base = []
    apply_env_append(base, {"EXTRA_ZEPHYR_MODULES": ["/sdk"]})
    assert base == [("EXTRA_ZEPHYR_MODULES", "/sdk")]


def test_assemble_slice_env_appends_dedups_seeds_and_fills_gaps():
    env = assemble_slice_env(
        slice_env={"ALP_SDK_ROOT": "/sdk"},
        env_append_path={
            "PYTHONPATH": ["/sdk/scripts"],
            "EXTRA_ZEPHYR_MODULES": ["/plan/sdk"],
        },
        inherited=lambda k: "/inh" if k == "PYTHONPATH" else None,
        gap_fillers=[("ZEPHYR_BASE", "/ws/zephyr")],
    )
    got = dict(env)
    assert got["ALP_SDK_ROOT"] == "/sdk"
    # Inherited PYTHONPATH is EXTENDED, never replaced.
    assert got["PYTHONPATH"] == f"/inh{SEP}/sdk/scripts"
    assert got["EXTRA_ZEPHYR_MODULES"] == "/plan/sdk"
    assert got["ZEPHYR_BASE"] == "/ws/zephyr"


def test_resolve_action_honors_policy_with_default_fallback():
    policy = ExecutionPolicy(unknown_backend=None, missing_tool=PolicyAction.FAIL,
                             null_command=PolicyAction.SKIP)
    assert resolve_action(policy, "missing_tool", PolicyAction.SKIP) is PolicyAction.FAIL
    assert resolve_action(policy, "null_command", PolicyAction.FAIL) is PolicyAction.SKIP
    # Absent entry -> default; None policy (older plan) -> default.
    assert resolve_action(policy, "unknown_backend", PolicyAction.SKIP) is PolicyAction.SKIP
    assert resolve_action(None, "missing_tool", PolicyAction.SKIP) is PolicyAction.SKIP


def test_sdk_stamp_action_matrix():
    keep, pristine = SdkStampAction.KEEP, SdkStampAction.PRISTINE
    # Matching stamp -> keep.
    assert sdk_stamp_action("/sdk/v0.13.0", "/sdk/v0.13.0", True, False, True) is keep
    # Mismatched stamp -> wipe (the reported v0.11.0 -> v0.13.0 case).
    assert sdk_stamp_action("/sdk/v0.11.0", "/sdk/v0.13.0", True, False, True) is pristine
    # Missing stamp on an already-configured dir -> wipe (the ONLY self-heal path).
    assert sdk_stamp_action(None, "/sdk/v0.13.0", True, False, True) is pristine
    # Never configured -> nothing to go stale.
    assert sdk_stamp_action(None, "/sdk/v0.13.0", False, False, True) is keep
    # -d/--build-dir override -> west wrote somewhere we cannot know.
    assert sdk_stamp_action("/sdk/v0.11.0", "/sdk/v0.13.0", True, True, True) is keep
    # cwd outside build root -> refuse to target <project>/src/build.
    assert sdk_stamp_action("/sdk/v0.11.0", "/sdk/v0.13.0", True, False, False) is keep
    # Unresolved current sdk root -> nothing to compare against.
    assert sdk_stamp_action("/sdk/v0.11.0", None, True, False, True) is keep


def test_sdk_stamp_key_combines_root_and_commit_when_both_present():
    assert sdk_stamp_key("/sdk", "3ffd8774") == "/sdk@3ffd8774"


def test_sdk_stamp_key_degrades_to_the_root_alone_when_commit_is_absent_or_blank():
    assert sdk_stamp_key("/sdk", None) == "/sdk"
    assert sdk_stamp_key("/sdk", "   ") == "/sdk"


def test_sdk_stamp_key_is_none_when_the_root_is_unresolved():
    assert sdk_stamp_key(None, "3ffd8774") is None
    assert sdk_stamp_key(None, None) is None


def test_sdk_stamp_key_changes_when_only_the_commit_changes_at_the_same_root():
    # A `git pull`/`git checkout <tag>` at the SAME `--sdk-root` path must
    # produce a DIFFERENT key so `sdk_stamp_action` treats it as a switch,
    # closing the tan-cli#163 content-change-at-the-same-path gap.
    a = sdk_stamp_key("/sdk", "deadbee")
    b = sdk_stamp_key("/sdk", "3ffd8774")
    assert a != b


def test_normalize_path_collapses_dot_and_dot_dot():
    assert normalize_path(Path("/a/b/./c")) == Path("/a/b/c")
    assert normalize_path(Path("/a/b/../c")) == Path("/a/c")


def test_normalize_path_drops_a_dot_dot_that_would_pop_past_the_root():
    assert normalize_path(Path("/a/../../../c")) == Path("/c")


def test_normalize_path_treats_a_relative_and_absolute_form_of_the_same_root_as_equal():
    """The reported thrash (tan-cli#163): `--sdk-root ../alp-sdk` (kept
    verbatim by CLI parsing) must normalize identically to the absolute
    pointer `tan sdk switch` already pins for the SAME checkout, once both
    are joined onto the same workspace root -- otherwise alternating
    between the two forms across invocations pristines (a full wipe) every
    other build."""
    workspace_root = Path("/ws/project")
    sdk_abs = workspace_root.parent / "alp-sdk"

    via_absolute = normalize_path(workspace_root / sdk_abs)
    via_relative = normalize_path(workspace_root / "../alp-sdk")

    assert via_absolute == via_relative == Path("/ws/alp-sdk")
