# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1466: golden `issues[]` entries for the three workspace-shaped
`tan build` codes, under `contract/fixtures/build-workspace-issues/`.

These are NOT `contract/envelopes` cases: each needs host state a hermetic
subprocess run cannot pin -- a `.west`-less layout (`build.workspace-unresolved`)
or a resolved workspace plus a patch verifier (`build.workspace-patches-*`) --
so the issue is produced by the real emitting function and diffed against the
committed golden, with the scratch directory spelled `__WORKDIR__` exactly as
the envelope goldens do. Re-record by running with `TAN_RECORD_GOLDENS=1`.
"""
import json
import os
from pathlib import Path

import pytest

import tan.commands.build.execute as execute_module
from tan.commands.build.workspace_patches import workspace_patch_issues
from tan.commands.build_cmd import _workspace_unresolved_issues
from tan.core.build_plan import parse_build_plan
from tan.core.west_workspace_refusal import west_ancestor
from tests.commands.test_execute import _plan, _real_zephyr_build_args
from tests.commands.test_workspace_patch_check import world  # noqa: F401 -- fixture

GOLDENS = Path(__file__).resolve().parents[3] / "contract" / "fixtures" / "build-workspace-issues"
WORK_DIR_TOKEN = "__WORKDIR__"


def _golden(name: str, issue, tmp_path: Path) -> None:
    got = {
        "code": issue.code,
        "severity": issue.severity,
        "message": issue.message.replace(str(tmp_path), WORK_DIR_TOKEN).replace("\\", "/"),
    }
    path = GOLDENS / f"{name}.json"
    if os.environ.get("TAN_RECORD_GOLDENS") == "1":
        GOLDENS.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(got, indent=2) + "\n", encoding="utf-8")
    assert got == json.loads(path.read_text(encoding="utf-8"))


def test_workspace_unresolved(tmp_path, monkeypatch):
    assert west_ancestor(tmp_path) is None
    sdk_root = tmp_path / "home" / ".cache" / "sdk-dev"
    sdk_root.mkdir(parents=True)
    app = tmp_path / "scratch" / "app"
    app.mkdir(parents=True)
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    monkeypatch.setattr(execute_module, "west_workspace_dir", lambda *a, **k: None)
    monkeypatch.setattr(execute_module, "west_program", lambda *a, **k: "west")
    monkeypatch.setattr(
        execute_module, "_resolve_tool",
        lambda tool, env: execute_module._ToolResolution("/fake/bin/west", "n/a"),
    )
    cmd = json.dumps({"tool": "west", "args": _real_zephyr_build_args(str(app)), "cwd": None})
    out = execute_module.execute_slices(
        parse_build_plan(_plan(cmd)), build_root=app, sdk_root=str(sdk_root),
        env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None,
    )
    (issue,) = _workspace_unresolved_issues(out)
    _golden("workspace-unresolved", issue, tmp_path)


def _patch_issues(world, monkeypatch):  # noqa: F811
    monkeypatch.setattr(
        "tan.commands.build.workspace_patches.west_workspace_dir", lambda s, sdk: world.ws
    )
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    return workspace_patch_issues(world.tmp / "b", str(world.sdk), has_zephyr_slice=True)


def test_workspace_patches_missing(world, monkeypatch):  # noqa: F811
    world.mode("missing")
    (issue,) = _patch_issues(world, monkeypatch)
    _golden("workspace-patches-missing", issue, world.tmp)


def test_workspace_patches_uncached(world, monkeypatch):  # noqa: F811
    (world.sdk / "zephyr" / "patches" / "mod" / "0001-x.patch").unlink()
    (issue,) = _patch_issues(world, monkeypatch)
    _golden("workspace-patches-uncached", issue, world.tmp)
