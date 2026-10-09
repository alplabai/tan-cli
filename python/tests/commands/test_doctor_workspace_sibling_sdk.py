# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1432: `tan doctor`'s `workspace` check, for an `--sdk-root` no
workspace has as its manifest, agrees with what `tan build` will then do --
`warn` when west's own walk still finds a `.west` (the build runs against that
workspace's Zephyr), `fail` naming the candidates when nothing does."""

from pathlib import Path

import pytest

from tan.commands import doctor_cmd
from tan.core.west_workspace_refusal import (
    manifest_project,
    unresolved_workspace_verdict,
    west_ancestor,
)


@pytest.fixture(autouse=True)
def _no_ambient_west(tmp_path):
    assert west_ancestor(tmp_path) is None


def _foreign_workspace(tmp_path: Path) -> Path:
    """`<tmp>/home` with `.west/config` naming `alp-sdk` (not the sibling)."""
    home = tmp_path / "home"
    (home / ".west").mkdir(parents=True)
    (home / ".west" / "config").write_text("[manifest]\npath = alp-sdk\nfile = west.yml\n", encoding="utf-8")
    return home


def _sibling_sdk(tmp_path: Path) -> Path:
    sdk = tmp_path / "home" / ".cache" / "sdk-dev"
    sdk.mkdir(parents=True)
    return sdk


def test_project_inside_a_foreign_workspace_warns_and_names_its_manifest(tmp_path):
    home = _foreign_workspace(tmp_path)
    sdk = _sibling_sdk(tmp_path)
    project = home / "proj"
    project.mkdir()
    status, detail = unresolved_workspace_verdict(project, None, sdk)
    assert status == "warn"
    assert f"west will use `{home}`" in detail
    assert f"names `{home / 'alp-sdk'}` as its manifest" in detail
    assert f"`tan bootstrap --sdk-root {sdk}`" in detail
    assert "no Zephyr workspace" not in detail


def test_zephyr_base_inside_a_workspace_is_a_may_not_a_will(tmp_path):
    home = _foreign_workspace(tmp_path)
    sdk = _sibling_sdk(tmp_path)
    project = tmp_path / "scratch" / "app"
    project.mkdir(parents=True)
    status, detail = unresolved_workspace_verdict(project, str(home / "zephyr"), sdk)
    assert status == "warn"
    assert f"west may fall back to `{home}`" in detail


def test_a_relative_zephyr_base_resolves_against_the_project_as_build_does(tmp_path):
    # `tan build`'s refusal resolves a relative ZEPHYR_BASE against the spawn
    # cwd (#1429); doctor's verdict must agree, not resolve it against
    # wherever `tan doctor` happens to run.
    home = _foreign_workspace(tmp_path)
    sdk = _sibling_sdk(tmp_path)
    project = tmp_path / "scratch" / "app"
    project.mkdir(parents=True)
    status, detail = unresolved_workspace_verdict(project, "../../home/zephyr", sdk)
    assert status == "warn"
    assert f"west may fall back to `{home.resolve()}`" in detail


def test_project_outside_every_workspace_fails_naming_the_candidates(tmp_path):
    sdk = _sibling_sdk(tmp_path)
    project = tmp_path / "scratch" / "app"
    project.mkdir(parents=True)
    status, detail = unresolved_workspace_verdict(project, None, sdk)
    assert status == "fail"
    assert f"no Zephyr workspace for the SDK root `{sdk}`" in detail
    assert f"`{project}` has no `.west` on any ancestor" in detail
    assert "ZEPHYR_BASE is unset" in detail
    assert "build.workspace-unresolved" in detail


def test_a_dot_west_without_a_config_names_no_manifest(tmp_path):
    (tmp_path / "ws" / ".west").mkdir(parents=True)
    assert manifest_project(tmp_path / "ws") is None
    sdk = _sibling_sdk(tmp_path)
    status, detail = unresolved_workspace_verdict(tmp_path / "ws", None, sdk)
    assert status == "warn"
    assert "names no manifest project" in detail


def test_the_check_uses_the_verdict_only_when_start_and_sdk_root_are_known(tmp_path):
    home = _foreign_workspace(tmp_path)
    sdk = _sibling_sdk(tmp_path)
    check = doctor_cmd.workspace_preflight_check(None, start=str(home), sdk_root=str(sdk))
    assert (check.name, check.status) == ("workspace", "warn")
    assert doctor_cmd.workspace_preflight_check(None).status == "fail"
    assert doctor_cmd.workspace_preflight_check("/ws", start=str(home), sdk_root=str(sdk)).status == "pass"
