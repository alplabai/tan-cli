# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1429: a `west build` slice for a sibling `--sdk-root` checkout with
the app outside every workspace tree is refused before west is spawned, coded
`build.workspace-unresolved`, instead of west's own `unknown command "build"`
under only `build.slice-failed`."""

import json
from pathlib import Path

import pytest

import tan.commands.build.execute as execute_module
from tan.commands.build.execute import execute_slices
from tan.commands.build_cmd import _workspace_unresolved_issues
from tan.core.build_plan import parse_build_plan
from tan.core.west_workspace_refusal import (
    WORKSPACE_UNRESOLVED_MSG,
    west_ancestor,
    workspace_unresolved_refusal,
)
from tests.commands.test_execute import _plan, _real_zephyr_build_args


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    """`<tmp>/home/.cache/sdk-dev` (sibling checkout, no workspace next to it)
    and `<tmp>/scratch/app` (outside every workspace) -- the issue's shape."""
    sdk_root = tmp_path / "home" / ".cache" / "sdk-dev"
    sdk_root.mkdir(parents=True)
    app = tmp_path / "scratch" / "app"
    app.mkdir(parents=True)
    return sdk_root, app


@pytest.fixture(autouse=True)
def _no_ambient_west(tmp_path):
    # Every layout below sits under `tmp_path`; a `.west` above it on this
    # host would make the "no ancestor" premise false and the test meaningless.
    assert west_ancestor(tmp_path) is None


def test_refused_when_neither_cwd_nor_zephyr_base_reaches_a_workspace(tmp_path):
    sdk_root, app = _layout(tmp_path)
    refusal = workspace_unresolved_refusal(None, ["build", "-b", "x", str(app)], app, None, sdk_root)
    assert refusal is not None
    assert refusal.message.startswith(WORKSPACE_UNRESOLVED_MSG)
    assert f"`{app}`" in refusal.message
    assert "ZEPHYR_BASE is unset" in refusal.message
    assert f"`tan bootstrap --sdk-root {sdk_root}`" in refusal.message
    assert str(tmp_path) not in refusal.manifest_message


def test_not_refused_when_the_app_sits_inside_some_workspace_tree(tmp_path):
    """The issue's working case: moved under the workspace, west's own
    ancestor walk finds `.west` without tan's manifest guard, and it builds."""
    sdk_root, _ = _layout(tmp_path)
    (tmp_path / "home" / ".west").mkdir()
    app = tmp_path / "home" / "proj" / "app"
    app.mkdir(parents=True)
    assert workspace_unresolved_refusal(None, ["build"], app, None, sdk_root) is None


def test_not_refused_when_zephyr_base_sits_inside_a_workspace(tmp_path):
    sdk_root, app = _layout(tmp_path)
    (tmp_path / "ws" / ".west").mkdir(parents=True)
    zephyr = tmp_path / "ws" / "zephyr"
    zephyr.mkdir()
    assert workspace_unresolved_refusal(None, ["build"], app, str(zephyr), sdk_root) is None


def test_a_zephyr_base_outside_any_workspace_is_named(tmp_path):
    sdk_root, app = _layout(tmp_path)
    stray = tmp_path / "stray" / "zephyr"
    stray.mkdir(parents=True)
    refusal = workspace_unresolved_refusal(None, ["build"], app, str(stray), sdk_root)
    assert refusal is not None
    assert f"ZEPHYR_BASE `{stray}` has no `.west` above it either" in refusal.message


@pytest.mark.parametrize(
    ("workspace", "args", "with_sdk"),
    [
        (Path("/ws"), ["build"], True),  # tan resolved a workspace: never refused
        (None, ["alp-flash"], True),  # not `west build`
        (None, [], True),
        (None, ["build"], False),  # no --sdk-root to name or bootstrap
    ],
)
def test_out_of_scope_shapes_are_never_refused(tmp_path, workspace, args, with_sdk):
    sdk_root, app = _layout(tmp_path)
    got = workspace_unresolved_refusal(workspace, args, app, None, sdk_root if with_sdk else None)
    assert got is None


def test_execute_refuses_before_spawning_west(tmp_path, monkeypatch):
    """End to end through `execute_slices`: nothing resolved, `west` itself
    resolves (so this is not the missing-tool skip), and `Popen` raises if
    west is ever spawned."""
    sdk_root, _ = _layout(tmp_path)
    build_root = tmp_path / "scratch" / "app"
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

    cmd = json.dumps({"tool": "west", "args": _real_zephyr_build_args(str(build_root)), "cwd": None})
    out = execute_slices(
        parse_build_plan(_plan(cmd)), build_root=build_root, sdk_root=str(sdk_root),
        env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None,
    )
    assert out[0].status == "failed"
    assert out[0].exit_code is None
    assert out[0].message.startswith("slice `c1` refused before build: " + WORKSPACE_UNRESOLVED_MSG)
    assert out[0].manifest_message == f"{WORKSPACE_UNRESOLVED_MSG} (run `tan bootstrap`)"

    issues = _workspace_unresolved_issues(out)
    assert [(i.code, i.severity) for i in issues] == [("build.workspace-unresolved", "error")]
    assert issues[0].message == out[0].message


def test_an_unrelated_failure_is_not_promoted():
    out = [execute_module.SliceOutcome("c1", "failed", 2, "slice `c1` terminated with exit code: 2")]
    assert _workspace_unresolved_issues(out) == []
