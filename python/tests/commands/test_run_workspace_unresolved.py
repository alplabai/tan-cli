# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1466: `tan run` builds through `build_cmd._build`, so a `west` slice
with no reachable Zephyr workspace must surface the same coded
`build.workspace-unresolved` error `tan build` does (tan-cli#1429) -- not be
swallowed into a bare `build.slice-failed`, and not be dropped on the way
through `run`'s own envelope assembly."""

import json

import pytest

import tan.commands.build.execute as execute_module
from tan.commands import build_cmd, run_cmd
from tan.core.build_plan import parse_build_plan
from tan.core.west_workspace_refusal import WORKSPACE_UNRESOLVED_MSG, west_ancestor
from tan.exit_codes import ExitCode
from tests.commands.test_execute import _plan, _real_zephyr_build_args


@pytest.fixture(autouse=True)
def _no_ambient_west(tmp_path):
    assert west_ancestor(tmp_path) is None


def test_run_surfaces_build_workspace_unresolved_for_a_west_slice(tmp_path, monkeypatch):
    sdk_root = tmp_path / "home" / ".cache" / "sdk-dev"
    sdk_root.mkdir(parents=True)
    app = tmp_path / "scratch" / "app"
    (app / "app").mkdir(parents=True)

    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    monkeypatch.setattr(execute_module, "west_workspace_dir", lambda *a, **k: None)
    monkeypatch.setattr(execute_module, "west_program", lambda *a, **k: "west")
    monkeypatch.setattr(
        execute_module, "_resolve_tool",
        lambda tool, env: execute_module._ToolResolution("/fake/bin/west", "n/a"),
    )

    def _must_not_spawn(*args, **kwargs):
        raise AssertionError("west must never be spawned when no workspace can resolve")

    monkeypatch.setattr(execute_module.subprocess, "Popen", _must_not_spawn)

    cmd = json.dumps({"tool": "west", "args": _real_zephyr_build_args(str(app / "app")), "cwd": None})
    text = _plan(cmd)
    monkeypatch.setattr(build_cmd, "_acquire_plan", lambda *a, **k: (text, parse_build_plan(text)))

    exit_code, _data, issues, _lines = run_cmd._run(
        build_root=str(app),
        sdk_root=str(sdk_root),
        sdk_root_for_stamp=str(sdk_root),
        board_yaml=None,
        flash=False,
        core=None,
        json_mode=True,
    )

    assert exit_code == ExitCode.RUNTIME_FAILURE
    unresolved = [i for i in issues if i.code == "build.workspace-unresolved"]
    assert [i.severity for i in unresolved] == ["error"]
    assert WORKSPACE_UNRESOLVED_MSG in unresolved[0].message
    assert f"`tan bootstrap --sdk-root {sdk_root}`" in unresolved[0].message
