# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1388: the sdk-switch-pristine wipe (`shutil.rmtree(cwd / "build")`)
must only ever target a slice cwd that really sits under `<project>/build`.

The guard used to check only the FIRST component of the plan-supplied `cwd`
string. `confine_to_build_root` already refuses a cwd that leaves the
project (`build/../..` fails with `build.path-escape`), but a cwd that leaves
`build/` and stays inside the project passed both checks:

- `build/../src/c1` -- first component `build`, resolves to `<project>/src/c1`;
- `build/c1` where `build/c1` is a symlink to `<project>/src/c1`.

Either one let `--pristine` (or an SDK switch) wipe `<project>/src/c1/build`,
a directory tan never created. Both must now be declined and reported as
`build.pristine-skipped`, same as a plain `src/c1` cwd.
"""
import os

import pytest

import tan.commands.build.execute as execute_module
from tan.commands.build.execute import execute_slices
from tan.core.build_plan import parse_build_plan
from tests.commands.test_execute import PYTHON, _configure, _plan, _stub_manifest_write


def _run_pristine(tmp_path, cwd: str) -> list:
    cmd = f'{{"tool": {PYTHON}, "args": ["-c", "pass"], "cwd": "{cwd}"}}'
    execute_slices(
        parse_build_plan(_plan(cmd)), build_root=tmp_path,
        env_lookup=lambda k: None, gap_fillers=[], on_output=lambda s: None,
        sdk_root="/sdk/v1", force_pristine=True,
    )
    return execute_module.last_sdk_switch_issues()


def test_a_dotdot_cwd_that_leaves_build_is_never_wiped(tmp_path, monkeypatch):
    _stub_manifest_write(monkeypatch)
    (tmp_path / "build").mkdir()
    victim = tmp_path / "src" / "c1"
    _configure(victim, "/sdk/v1")

    issues = _run_pristine(tmp_path, "build/../src/c1")

    assert (victim / "build" / "CMakeCache.txt").is_file(), (
        "--pristine wiped <project>/src/c1/build through a `build/..` cwd"
    )
    assert [i.code for i in issues] == ["build.pristine-skipped"]
    assert "build/" in issues[0].message


def test_a_dotdot_cwd_that_stays_inside_build_is_still_declined(tmp_path, monkeypatch):
    """`..` is refused lexically, not only when it happens to escape: the
    guard decides on what the plan SAID, so a `..` anywhere makes the cwd one
    tan cannot vouch for."""
    _stub_manifest_write(monkeypatch)
    slice_dir = tmp_path / "build" / "c1"
    _configure(slice_dir, "/sdk/v1")

    issues = _run_pristine(tmp_path, "build/x/../c1")

    assert (slice_dir / "build" / "CMakeCache.txt").is_file()
    assert [i.code for i in issues] == ["build.pristine-skipped"]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_a_symlinked_slice_dir_pointing_out_of_build_is_never_wiped(tmp_path, monkeypatch):
    _stub_manifest_write(monkeypatch)
    victim = tmp_path / "src" / "c1"
    _configure(victim, "/sdk/v1")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "c1").symlink_to(victim, target_is_directory=True)

    issues = _run_pristine(tmp_path, "build/c1")

    assert (victim / "build" / "CMakeCache.txt").is_file(), (
        "--pristine followed a build/c1 symlink and wiped <project>/src/c1/build"
    )
    assert [i.code for i in issues] == ["build.pristine-skipped"]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_a_symlinked_build_root_is_never_wiped(tmp_path, monkeypatch):
    """`<project>/build` itself a symlink to somewhere else: every slice cwd
    then resolves outside the project's own build root."""
    _stub_manifest_write(monkeypatch)
    victim = tmp_path / "src" / "c1"
    _configure(victim, "/sdk/v1")
    (tmp_path / "build").symlink_to(tmp_path / "src", target_is_directory=True)

    issues = _run_pristine(tmp_path, "build/c1")

    assert (victim / "build" / "CMakeCache.txt").is_file()
    assert [i.code for i in issues] == ["build.pristine-skipped"]


def test_a_plain_slice_cwd_under_build_is_still_wiped(tmp_path, monkeypatch):
    """The guard tightens, it does not switch the wipe off: tan's own
    planner-shaped `build/<core>` cwd keeps working."""
    _stub_manifest_write(monkeypatch)
    slice_dir = tmp_path / "build" / "c1"
    _configure(slice_dir, "/sdk/v1")

    issues = _run_pristine(tmp_path, "build/c1")

    assert not (slice_dir / "build" / "CMakeCache.txt").exists()
    assert [i.code for i in issues] == ["build.sdk-switch-pristine"]
