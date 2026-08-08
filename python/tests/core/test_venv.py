# SPDX-License-Identifier: Apache-2.0
"""`tan.core.venv` -- the workspace-venv resolution family (tan-cli#289),
covering the parts of the module `test_build_planner_python.py` (via
`_planner_python`/`venv_python`) and `test_execute.py`/`test_flash_command.py`
(via `west_program`/`tool_in_venv` end-to-end) do not already exercise
directly: `tool_in_venv`'s own file-existence + `.exe` behaviour, and
`with_venv_on_path`'s bare-tool no-op. Mirrors the shape of Rust's own
`venv.rs` unit tests.
"""
import os
from pathlib import Path

from tan.core import venv as venv_module
from tan.core.venv import (
    find_workspace_venv,
    tool_in_venv,
    venv_bin_dir,
    west_program,
    with_venv_on_path,
)


def _venv_parts() -> tuple[str, str]:
    return ("Scripts", "west.exe") if os.name == "nt" else ("bin", "west")


def _plant_west_capable_venv(venv_dir: Path) -> None:
    bin_dir, west_exe = _venv_parts()
    bin_path = venv_dir / bin_dir
    bin_path.mkdir(parents=True)
    (bin_path / west_exe).write_text("", encoding="utf-8")


def test_find_workspace_venv_accepts_the_non_host_layout(tmp_path, monkeypatch):
    """tan-cli#291: bootstrap creation accepts EITHER layout by directory
    presence (`bootstrap_cmd.Workspace.venv_bin`'s directory-wins probe), not
    the host -- resolution must match. A venv whose executables live under
    the layout this host does NOT natively use (a Git-Bash/MSYS-created
    `bin/` venv found on native Windows, or the mirror `Scripts/` venv found
    on POSIX) must not be invisible to `find_workspace_venv`/
    `venv_bin_dir`/`west_program`.

    Exercises the real host's non-native layout rather than faking
    `os.name`: patching `os.name` to the other platform breaks `pathlib.Path`
    construction outright on a real Windows host (`Path.__new__` picks its
    concrete subclass from `os.name`, so a patched value crashes on the very
    next `Path(str)` call -- including ones inside pytest's own failure-repr
    machinery, corrupting the test run itself rather than failing cleanly).
    `_resolve_layout` never reads `os.name` post-fix, so proving it accepts
    THIS host's non-native layout already proves the directory-wins rule;
    the code path is identical for the mirror layout on the mirror host.
    """
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    other_bin, other_exe = ("bin", "west") if os.name == "nt" else ("Scripts", "west.exe")
    venv_bin = tmp_path / ".venv" / other_bin
    venv_bin.mkdir(parents=True)
    (venv_bin / other_exe).write_text("", encoding="utf-8")

    assert find_workspace_venv(str(tmp_path), None) == tmp_path / ".venv"
    assert venv_bin_dir(str(tmp_path), None) == venv_bin
    assert west_program(str(tmp_path), None) == str(venv_bin / other_exe)


# ---------------------------------------------------------------------------
# tan-cli#292 consequence 2: `$ZEPHYR_BASE` must not outrank an explicit/
# resolved `--sdk-root` -- mirrors the manifest guard `_zephyr_base_workspace`
# already applies to the topdir search (tan-cli#61).
# ---------------------------------------------------------------------------


def test_find_workspace_venv_refuses_a_manifest_mismatched_zephyr_base_venv_when_sdk_root_is_known(
    tmp_path, monkeypatch
):
    """Reproduces tan-cli#292 consequence 2 against the published v0.5.0-rc2
    shape (tan-cli#278's comment): an ordinary upstream `west init` workspace
    -- its OWN manifest, not alp-sdk's -- sitting behind a stale exported
    `$ZEPHYR_BASE`. Before this fix, step 2 accepted that venv unconditionally
    and the canonical SDK-derived venv (step 3) was never reached even though
    `sdk_root` was known.
    """
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    foreign = tmp_path / "foreign-zephyrproject"
    foreign_zephyr = foreign / "zephyr"
    foreign_zephyr.mkdir(parents=True)
    (foreign / ".west").mkdir()
    # A real, but UNRELATED, manifest -- points `path` at a sibling that is
    # not the SDK under test at all.
    (foreign / ".west" / "config").write_text("[manifest]\npath = zephyr\n", encoding="utf-8")
    _plant_west_capable_venv(foreign / ".venv")
    monkeypatch.setenv("ZEPHYR_BASE", str(foreign_zephyr))

    sdk = tmp_path / "ws" / "alp-sdk"
    sdk.mkdir(parents=True)
    _plant_west_capable_venv(tmp_path / "ws" / ".venv")

    start = tmp_path / "elsewhere"
    start.mkdir()

    assert find_workspace_venv(str(start), str(sdk)) == tmp_path / "ws" / ".venv"


def test_find_workspace_venv_still_resolves_a_manifest_matching_zephyr_base_venv(
    tmp_path, monkeypatch
):
    """The guard must not be a blanket refusal: an ACTIVATED workspace whose
    manifest really is the resolved SDK's still resolves via `$ZEPHYR_BASE`,
    exactly as before -- the common case (`source ... && tan build`)."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    workspace = tmp_path / "ws"
    zephyr = workspace / "zephyr"
    zephyr.mkdir(parents=True)
    sdk = workspace / "alp-sdk"
    sdk.mkdir()
    (workspace / ".west").mkdir()
    (workspace / ".west" / "config").write_text("[manifest]\npath = alp-sdk\n", encoding="utf-8")
    _plant_west_capable_venv(workspace / ".venv")
    monkeypatch.setenv("ZEPHYR_BASE", str(zephyr))

    start = tmp_path / "elsewhere"
    start.mkdir()

    assert find_workspace_venv(str(start), str(sdk)) == workspace / ".venv"


def test_find_workspace_venv_zephyr_base_venv_wins_unconditionally_when_sdk_root_is_unresolved(
    tmp_path, monkeypatch
):
    """No `sdk_root` means nothing to verify the `$ZEPHYR_BASE` workspace's
    manifest against -- the OLD unconditional accept stands, matching
    `_zephyr_base_workspace`'s own "nothing to verify against" fallback."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    foreign = tmp_path / "foreign-zephyrproject"
    zephyr = foreign / "zephyr"
    zephyr.mkdir(parents=True)
    _plant_west_capable_venv(foreign / ".venv")
    monkeypatch.setenv("ZEPHYR_BASE", str(zephyr))

    start = tmp_path / "elsewhere"
    start.mkdir()

    assert find_workspace_venv(str(start), None) == foreign / ".venv"


# ---------------------------------------------------------------------------
# tan-cli#495 defect 1: the upward `.venv` walk (step 1) must apply the SAME
# manifest guard `west_workspace_dir`'s own upward walk already applies
# (tan-cli#307) -- an ancestor `.venv` with a FOREIGN `.west` beside it must
# not outrank the real workspace venv `west_workspace_dir` already resolves.
# ---------------------------------------------------------------------------


def test_an_ancestor_venv_with_a_foreign_west_does_not_outrank_the_real_workspace(
    tmp_path, monkeypatch
):
    """Reproduces tan-cli#495 defect 1's failure scenario: `<X>/unrelated/proj`
    (a west-capable `.venv` plus an unrelated `.west/config` sit in
    `<X>/unrelated`, an ANCESTOR of the project), and the real SDK workspace
    at the sibling `<X>/wsroot` (`wsroot/.venv`, `wsroot/.west/config` naming
    `alp-sdk`). Before this fix, `find_workspace_venv` returned the foreign
    ancestor venv while `west_workspace_dir` (unaffected -- it already had
    this guard) correctly resolved `wsroot`; `tan build` then spawned the
    wrong venv's `west`/`python` against the RIGHT workspace's cwd."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    root = tmp_path / "X"
    unrelated = root / "unrelated"
    proj = unrelated / "proj"
    proj.mkdir(parents=True)
    (unrelated / ".west").mkdir()
    (unrelated / ".west" / "config").write_text(
        "[manifest]\npath = someother\n", encoding="utf-8"
    )
    _plant_west_capable_venv(unrelated / ".venv")

    wsroot = root / "wsroot"
    sdk = wsroot / "alp-sdk"
    sdk.mkdir(parents=True)
    (wsroot / ".west").mkdir()
    (wsroot / ".west" / "config").write_text("[manifest]\npath = alp-sdk\n", encoding="utf-8")
    _plant_west_capable_venv(wsroot / ".venv")

    assert find_workspace_venv(str(proj), str(sdk)) == wsroot / ".venv"
    assert venv_bin_dir(str(proj), str(sdk)) == wsroot / ".venv" / _venv_parts()[0]


def test_a_foreign_ancestor_west_still_wins_when_sdk_root_is_unresolved(tmp_path, monkeypatch):
    """`sdk_root is None` means nothing to verify a candidate's own `.west`
    against -- the OLD unconditional accept stands, matching every other
    "nothing to check" fallback in this module."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    unrelated = tmp_path / "unrelated"
    proj = unrelated / "proj"
    proj.mkdir(parents=True)
    (unrelated / ".west").mkdir()
    (unrelated / ".west" / "config").write_text(
        "[manifest]\npath = someother\n", encoding="utf-8"
    )
    _plant_west_capable_venv(unrelated / ".venv")

    assert find_workspace_venv(str(proj), None) == unrelated / ".venv"


def test_a_project_local_venv_with_no_west_at_all_is_unaffected(tmp_path, monkeypatch):
    """tan-cli#495 defect 1's own caveat (b): the fix must not be a naive
    mirror of `west_workspace_dir`'s guard -- `manifest_ok` there is False for
    a directory with no `.west` at all, which would wrongly reject the
    supported project-local `.venv` shape tan-cli#307's own e2e test plants at
    `build_root/.venv` (no `.west` beside it). A candidate directory with no
    `.west` must keep resolving exactly as before, `sdk_root` known or not."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    build_root = tmp_path / "proj" / "build"
    build_root.mkdir(parents=True)
    _plant_west_capable_venv(build_root / ".venv")

    sdk = tmp_path / "ws" / "alp-sdk"
    sdk.mkdir(parents=True)

    assert find_workspace_venv(str(build_root), str(sdk)) == build_root / ".venv"


def test_tool_in_venv_resolves_only_files_that_exist(tmp_path):
    bin_dir, west_exe = _venv_parts()
    venv_bin = tmp_path / bin_dir
    venv_bin.mkdir()
    (venv_bin / west_exe).write_text("", encoding="utf-8")

    resolved = tool_in_venv(venv_bin, "west")
    assert resolved == str(venv_bin / west_exe)
    assert tool_in_venv(venv_bin, "openocd") is None


def test_tool_in_venv_appends_exe_only_on_windows_and_only_once(tmp_path):
    """A caller that already spelled `.exe` must not become `west.exe.exe`."""
    bin_dir, west_exe = _venv_parts()
    venv_bin = tmp_path / bin_dir
    venv_bin.mkdir()
    (venv_bin / west_exe).write_text("", encoding="utf-8")

    assert tool_in_venv(venv_bin, "west") == str(venv_bin / west_exe)
    if os.name == "nt":
        assert tool_in_venv(venv_bin, "west.exe") == str(venv_bin / west_exe)


def test_west_program_falls_back_to_the_bare_path_name(tmp_path, monkeypatch):
    """Pinned like `test_build_planner_python.py:74-84` pins the identical
    `find_workspace_venv` walk on `_planner_python`'s side: `venv_bin_dir`
    (which `west_program` calls) walks from `empty` all the way to the
    filesystem root looking for a west-capable `.venv` -- a developer machine
    with one anywhere above the OS temp dir would red this test for reasons
    unrelated to the code under test."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    monkeypatch.setattr(venv_module, "find_workspace_venv", lambda *_args: None)
    empty = tmp_path / "no-venv-here"
    empty.mkdir()
    assert west_program(str(empty), None) == "west"


def test_with_venv_on_path_prepends_onto_an_already_staged_path():
    venv_west = str(Path("/ws/.venv/bin/west")) if os.name != "nt" else r"C:\ws\.venv\Scripts\west.exe"
    env = with_venv_on_path({"PATH": "/plan/toolchain/bin"}, venv_west)
    venv_dir = str(Path(venv_west).parent)
    assert env["PATH"].startswith(venv_dir)
    assert "/plan/toolchain/bin" in env["PATH"]


def test_with_venv_on_path_leaves_a_bare_tool_name_untouched():
    env = with_venv_on_path({"PATH": "/plan/toolchain/bin"}, "west")
    assert env == {"PATH": "/plan/toolchain/bin"}


def test_with_venv_on_path_does_not_mutate_the_caller_s_dict():
    original = {"PATH": "/plan/toolchain/bin"}
    venv_west = r"C:\ws\.venv\Scripts\west.exe" if os.name == "nt" else "/ws/.venv/bin/west"
    with_venv_on_path(original, venv_west)
    assert original == {"PATH": "/plan/toolchain/bin"}
