# SPDX-License-Identifier: Apache-2.0
"""`tan build`'s workspace-patch warning and `$ZEPHYR_BASE` note (tan-cli#1376).

Warning-only by design: a missing patch never changes `ok` or the exit code
(a workspace can be deliberately unpatched, and the check can be wrong about a
narrow one). Promoting `build.workspace-patches-missing` to an error is a
one-line severity change here, left for a later decision.
"""
from __future__ import annotations

import os
from pathlib import Path

from tan.commands.workspace_patch_check import MISSING, check_workspace_patches
from tan.core.venv import west_workspace_dir
from tan.core.west_patches import describe_unapplied, patch_fix_text, zephyr_base_note
from tan.envelope import Issue

#: The consumer build directory (`build_cmd.CONSUMER_BUILD_ROOT`; not imported
#: because `build_cmd` imports this module). `build_root` here is the project
#: root, so the cache belongs under `<project>/build/`, not beside board.yaml.
_BUILD_DIR = "build"


def workspace_patch_issues(
    build_root: Path, sdk_root: str | None, *, has_zephyr_slice: bool
) -> list[Issue]:
    """Issues for the resolved workspace of a plan with a Zephyr slice; never
    raises, empty when there is nothing to say (including "could not check")."""
    if not has_zephyr_slice:
        return []
    try:
        workspace = west_workspace_dir(str(build_root), Path(sdk_root) if sdk_root else None)
        if workspace is None:
            return []
        issues: list[Issue] = []
        result = check_workspace_patches(
            workspace, sdk_root, cache_dir=build_root / _BUILD_DIR
        )
        if result.state == MISSING:
            issues.append(
                Issue(
                    "build.workspace-patches-missing",
                    "warning",
                    f"alp-sdk's zephyr/patches.yml is not applied in {workspace}: "
                    f"{describe_unapplied(result.patches, result.modules)}. The build can "
                    "succeed yet fail at runtime (for example `alp_camera_open` returns "
                    f"ALP_ERR_NOSUPPORT). Fix: {patch_fix_text(result.modules, str(workspace))}.",
                )
            )
        if result.cache_note:
            issues.append(
                Issue(
                    "build.workspace-patches-uncached",
                    "info",
                    "the workspace patch check is not cached, so every build "
                    f"re-verifies it: {result.cache_note}.",
                )
            )
        note = zephyr_base_note(os.environ.get("ZEPHYR_BASE"), str(workspace / "zephyr"))
        if note is not None:
            issues.append(Issue("build.zephyr-base-ignored", "info", note))
        return issues
    except Exception:  # noqa: BLE001 -- an advisory check must never break a build
        return []
