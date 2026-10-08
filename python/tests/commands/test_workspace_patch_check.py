# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1376: `tan doctor` / `tan build` notice a workspace that lacks
alp-sdk's `zephyr/patches.yml`. The verifier is a FAKE script that mimics
`scripts/verify_west_patches.py`'s exit codes and failure report; nothing here
needs west, git or a real alp-sdk."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tan.commands import workspace_patch_check as wpc
from tan.commands.build.workspace_patches import workspace_patch_issues
from tan.commands.doctor_cmd import workspace_patches_check, zephyr_base_check
from tan.core import west_patches

FAKE = '''
import sys
mode = open(sys.argv[0].replace("verify_west_patches.py", "mode.txt")).read().strip()
with open(sys.argv[0] + ".calls", "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
if "--list-unapplied" in sys.argv:
    print("alif")
    print("mcuboot")
    sys.exit(0)
if mode == "applied":
    print("verify-west-patches: OK")
    sys.exit(0)
if mode == "missing":
    print("\\nverify-west-patches: 2 of 3 patch(es) are not applied", file=sys.stderr)
    print("  ABSENT      hal_alif/0001-clock-set-rate.patch", file=sys.stderr)
    print("              /ws/modules/hal_alif -- the patch applies cleanly", file=sys.stderr)
    print("  DRIFTED     mcuboot/0001-flash-map.patch", file=sys.stderr)
    sys.exit(1)
if mode == "trace":
    print("Traceback (most recent call last):\\nModuleNotFoundError: yaml", file=sys.stderr)
    sys.exit(1)
sys.exit(int(mode))
'''


@pytest.fixture
def world(tmp_path, monkeypatch):
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "zephyr").mkdir()
    (sdk / "zephyr" / "patches").mkdir()
    (sdk / "zephyr" / "patches.yml").write_text(
        "patches:\n  - path: mod/0001-x.patch\n    module: mod\n"
    )
    (sdk / "zephyr" / "patches" / "mod").mkdir()
    (sdk / "zephyr" / "patches" / "mod" / "0001-x.patch").write_text(
        "--- a/src/f.c\n+++ b/src/f.c\n@@ -1 +1 @@\n-a\n+b\n"
    )
    verifier = sdk / "scripts" / "verify_west_patches.py"
    verifier.write_text(FAKE)
    ws = tmp_path / "ws"
    (ws / "zephyr").mkdir(parents=True)
    monkeypatch.setattr(wpc, "_interpreter", lambda w, s: sys.executable)
    monkeypatch.setattr(wpc, "west_program", lambda w, s: "west")
    moddir = tmp_path / "ws" / "mod"
    (moddir / "src").mkdir(parents=True)
    (moddir / "src" / "f.c").write_text("b\n")
    monkeypatch.setattr(wpc, "_workspace_heads", lambda west, w: {str(moddir): "abc"})

    class W:
        pass

    w = W()
    w.sdk, w.ws, w.verifier, w.tmp, w.moddir = sdk, ws, verifier, tmp_path, moddir
    w.mode = lambda m: (sdk / "scripts" / "mode.txt").write_text(m)
    w.calls = lambda: (
        Path(str(verifier) + ".calls").read_text().splitlines()
        if Path(str(verifier) + ".calls").exists()
        else []
    )
    w.mode("applied")
    return w


def test_applied(world):
    r = wpc.check_workspace_patches(world.ws, str(world.sdk))
    assert r.state == wpc.APPLIED, r.note


def test_missing_names_patches_modules_and_fix(world):
    world.mode("missing")
    r = wpc.check_workspace_patches(world.ws, str(world.sdk))
    assert r.state == wpc.MISSING
    assert [(p.verdict, p.patch) for p in r.patches] == [
        ("ABSENT", "hal_alif/0001-clock-set-rate.patch"),
        ("DRIFTED", "mcuboot/0001-flash-map.patch"),
    ]
    assert r.modules == ["alif", "mcuboot"]
    check = workspace_patches_check(r, str(world.ws))
    assert check.status == "warn"
    assert "hal_alif/0001-clock-set-rate.patch" in check.detail
    assert "west patch --dst-module alif apply" in check.detail
    assert "west patch --dst-module mcuboot apply" in check.detail
    assert check.fix == "tan bootstrap"


def test_verifier_runs_from_isolated_cwd_with_workspace_args(world, monkeypatch):
    seen = {}
    real = wpc._run

    def spy(argv, cwd, *rest):
        seen.setdefault("cwd", cwd)
        return real(argv, cwd, *rest)

    monkeypatch.setattr(wpc, "_run", spy)
    wpc.check_workspace_patches(world.ws, str(world.sdk))
    assert Path(seen["cwd"]) not in (world.ws, world.sdk)
    assert f"--topdir {world.ws}" in world.calls()[0]


def test_old_sdk_without_verifier_is_unchecked_not_a_failure(world):
    world.verifier.unlink()
    r = wpc.check_workspace_patches(world.ws, str(world.sdk))
    assert r.state == wpc.UNCHECKED
    assert workspace_patches_check(r, str(world.ws)).status == "unknown"


@pytest.mark.parametrize("rc", ["2", "3"])
def test_uninspectable_or_unchecked_modules_are_unchecked(world, rc):
    world.mode(rc)
    assert wpc.check_workspace_patches(world.ws, str(world.sdk)).state == wpc.UNCHECKED


def test_rc1_with_traceback_is_unchecked(world):
    # the verifier died on `import yaml`: exit 1 plus a Python traceback.
    world.mode("trace")
    assert wpc.check_workspace_patches(world.ws, str(world.sdk)).state == wpc.UNCHECKED


def test_rc1_without_parsable_lines_is_missing_unnamed(world):
    world.mode("1")
    r = wpc.check_workspace_patches(world.ws, str(world.sdk))
    assert r.state == wpc.MISSING and r.patches == []
    assert "unnamed patches" in workspace_patches_check(r, str(world.ws)).detail


def test_reverting_a_patched_file_without_moving_head_misses_the_cache(world):
    cache = world.tmp / "build"
    wpc.check_workspace_patches(world.ws, str(world.sdk), cache_dir=cache)
    assert wpc.check_workspace_patches(world.ws, str(world.sdk), cache_dir=cache).cached
    # `west patch clean` / `git checkout -- .`: file rewritten, HEAD untouched.
    (world.moddir / "src" / "f.c").write_text("a-original-longer\n")
    world.mode("missing")
    r = wpc.check_workspace_patches(world.ws, str(world.sdk), cache_dir=cache)
    assert not r.cached and r.state == wpc.MISSING


def test_exit_3_narrow_workspace_is_cached_too(world):
    cache = world.tmp / "build"
    world.mode("3")
    assert not wpc.check_workspace_patches(world.ws, str(world.sdk), cache_dir=cache).cached
    r = wpc.check_workspace_patches(world.ws, str(world.sdk), cache_dir=cache)
    assert r.cached and r.state == wpc.UNCHECKED and len(world.calls()) == 1


def test_never_raises_even_when_the_spawn_layer_does(world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(wpc, "_run", boom)
    assert wpc.check_workspace_patches(world.ws, str(world.sdk)).state == wpc.UNCHECKED


def test_no_interpreter_never_raises(world, monkeypatch):
    monkeypatch.setattr(wpc, "_interpreter", lambda w, s: None)
    assert wpc.check_workspace_patches(world.ws, str(world.sdk)).state == wpc.UNCHECKED


def test_no_sdk_is_unchecked(world):
    assert wpc.check_workspace_patches(world.ws, None).state == wpc.UNCHECKED


def test_applied_is_cached_and_key_changes_with_head_or_yml(world, monkeypatch):
    cache = world.tmp / "build"
    cache.mkdir()
    first = wpc.check_workspace_patches(world.ws, str(world.sdk), cache_dir=cache)
    assert not first.cached and len(world.calls()) == 1
    second = wpc.check_workspace_patches(world.ws, str(world.sdk), cache_dir=cache)
    assert second.cached and second.state == wpc.APPLIED and len(world.calls()) == 1
    monkeypatch.setattr(wpc, "_workspace_heads", lambda west, w: {str(world.moddir): "def"})
    assert not wpc.check_workspace_patches(world.ws, str(world.sdk), cache_dir=cache).cached
    (world.sdk / "zephyr" / "patches.yml").write_text("patches: [x]\n")
    assert not wpc.check_workspace_patches(world.ws, str(world.sdk), cache_dir=cache).cached


def test_missing_is_never_cached(world):
    cache = world.tmp / "build"
    cache.mkdir()
    world.mode("missing")
    wpc.check_workspace_patches(world.ws, str(world.sdk), cache_dir=cache)
    assert not (cache / wpc.CACHE_FILE).exists()
    world.mode("applied")  # the user ran `tan bootstrap`
    assert wpc.check_workspace_patches(world.ws, str(world.sdk), cache_dir=cache).state == wpc.APPLIED


def test_build_issue_is_a_warning_naming_patches(world, monkeypatch):
    monkeypatch.setattr(
        "tan.commands.build.workspace_patches.west_workspace_dir", lambda s, sdk: world.ws
    )
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    world.mode("missing")
    issues = workspace_patch_issues(world.tmp / "b", str(world.sdk), has_zephyr_slice=True)
    assert [(i.code, i.severity) for i in issues] == [("build.workspace-patches-missing", "warning")]
    assert "hal_alif/0001-clock-set-rate.patch" in issues[0].message
    assert workspace_patch_issues(world.tmp / "b", str(world.sdk), has_zephyr_slice=False) == []
    world.mode("applied")
    assert workspace_patch_issues(world.tmp / "b", str(world.sdk), has_zephyr_slice=True) == []


def test_zephyr_base_note_only_when_different(tmp_path):
    ws_z = tmp_path / "ws" / "zephyr"
    ws_z.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    assert west_patches.zephyr_base_note(None, str(ws_z)) is None
    assert west_patches.zephyr_base_note(str(ws_z), str(ws_z)) is None
    note = west_patches.zephyr_base_note(str(other), str(ws_z))
    assert note and "ignored" in note and str(other) in note
    assert zephyr_base_check(str(other), str(tmp_path / "ws")).status == "pass"
    assert zephyr_base_check(None, str(tmp_path / "ws")) is None


def test_build_reports_zephyr_base_info_without_touching_ok(world, monkeypatch):
    monkeypatch.setattr(
        "tan.commands.build.workspace_patches.west_workspace_dir", lambda s, sdk: world.ws
    )
    monkeypatch.setenv("ZEPHYR_BASE", str(world.tmp / "elsewhere"))
    issues = workspace_patch_issues(world.tmp / "b", str(world.sdk), has_zephyr_slice=True)
    assert [(i.code, i.severity) for i in issues] == [("build.zephyr-base-ignored", "info")]


def test_parse_unapplied_patches_ignores_noise():
    text = "verify-west-patches: 1 of 2\n  UNRESOLVED  foo/0002-x.patch\n    module 'foo' matches\n"
    assert west_patches.parse_unapplied_patches(text) == [
        west_patches.UnappliedPatch("UNRESOLVED", "foo/0002-x.patch")
    ]


def test_build_cache_lands_in_the_build_dir_not_the_project_root(world, monkeypatch):
    monkeypatch.setattr(
        "tan.commands.build.workspace_patches.west_workspace_dir", lambda s, sdk: world.ws
    )
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    project = world.tmp / "proj"
    project.mkdir()
    workspace_patch_issues(project, str(world.sdk), has_zephyr_slice=True)
    assert (project / "build" / wpc.CACHE_FILE).is_file()
    assert not (project / wpc.CACHE_FILE).exists()
