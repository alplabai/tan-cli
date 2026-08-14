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


# ---------------------------------------------------------------------------
# tan-cli#495 defect 1: step 1's UPWARD WALK had no manifest guard at all, so
# the two resolvers that jointly decide "which west" disagreed on a dirty host.
# ---------------------------------------------------------------------------


def _plant_foreign_topdir(directory: Path) -> None:
    """A west topdir whose manifest is emphatically not alp-sdk's, plus a
    west-capable `.venv` -- an ordinary upstream `west init` workspace."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".west").mkdir()
    (directory / ".west" / "config").write_text(
        "[manifest]\npath = zephyr\nfile = west.yml\n", encoding="utf-8"
    )
    _plant_west_capable_venv(directory / ".venv")


def test_find_workspace_venv_walks_past_an_ancestor_west_topdir_that_is_not_the_sdks(
    tmp_path, monkeypatch
):
    """**tan-cli#495 defect 1.** Step 1 accepted ANY ancestor `.venv` holding a
    `west`, with no `sdk_root` involvement -- while `west_workspace_dir`
    rejects that same ancestor via its tan-cli#307 `manifest_ok` guard. So a
    project nested under an unrelated `zephyrproject` got its `west` BINARY
    from the foreign venv and its `cwd` from the correct workspace, and that
    interpreter is what tan bakes into every Zephyr slice as
    `-DPython3_EXECUTABLE`.

    The canonical `<sdk-parent>/.venv` (step 3) is planted here too, so this
    pins the RESOLUTION ORDER: the walk must step over the foreign topdir and
    reach it, not merely decline the foreign one.
    """
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    foreign = tmp_path / "zephyrproject"
    _plant_foreign_topdir(foreign)
    project = foreign / "my-project"
    project.mkdir()

    sdk = tmp_path / "sdk-parent" / "alp-sdk"
    sdk.mkdir(parents=True)
    _plant_west_capable_venv(sdk.parent / ".venv")

    assert find_workspace_venv(str(project), str(sdk)) == sdk.parent / ".venv"


def test_find_workspace_venv_still_takes_an_ancestor_topdir_that_is_the_sdks(
    tmp_path, monkeypatch
):
    """The guard is a narrowing, not a blanket refusal on `.west`: the ordinary
    layout -- the project inside the SDK's OWN workspace -- must still resolve
    at step 1, and must still WIN over the step-3 fallback."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    workspace = tmp_path / "alp-workspace"
    sdk = workspace / "alp-sdk"
    sdk.mkdir(parents=True)
    (workspace / ".west").mkdir()
    (workspace / ".west" / "config").write_text(
        "[manifest]\npath = alp-sdk\nfile = west.yml\n", encoding="utf-8"
    )
    _plant_west_capable_venv(workspace / ".venv")
    project = workspace / "my-project"
    project.mkdir()

    assert find_workspace_venv(str(project), str(sdk)) == workspace / ".venv"


def test_find_workspace_venv_still_takes_a_project_local_venv_with_no_west_dir(
    tmp_path, monkeypatch
):
    """The reason the guard is conditional on the directory HOLDING a `.west`
    rather than a naive mirror of tan-cli#307's `manifest_ok`. A plain project
    directory with a `.venv` and no `.west` makes no claim to be a workspace,
    so it is judged on its venv alone -- exactly as before. This is the shape
    tan-cli#307's own e2e test plants at `build_root/.venv`; requiring a
    matching manifest outright would have rejected it.
    """
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    sdk = tmp_path / "sdk-parent" / "alp-sdk"
    sdk.mkdir(parents=True)
    _plant_west_capable_venv(sdk.parent / ".venv")

    project = tmp_path / "build-root"
    project.mkdir()
    _plant_west_capable_venv(project / ".venv")

    assert find_workspace_venv(str(project), str(sdk)) == project / ".venv"


def test_find_workspace_venv_takes_a_foreign_ancestor_topdir_when_sdk_root_is_unresolved(
    tmp_path, monkeypatch
):
    """No `sdk_root` means nothing to verify the manifest against, so the old
    unconditional accept stands -- the same "nothing to check" fallback
    `_zephyr_base_venv` and `west_workspace_dir.manifest_ok` already apply.
    Without this the guard would regress CI and the contract harness, which
    routinely run with no resolvable SDK."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    foreign = tmp_path / "zephyrproject"
    _plant_foreign_topdir(foreign)
    project = foreign / "my-project"
    project.mkdir()

    assert find_workspace_venv(str(project), None) == foreign / ".venv"


def test_find_workspace_venv_agrees_with_west_workspace_dir_on_the_same_dirty_host(
    tmp_path, monkeypatch
):
    """The defect stated as the invariant it broke, since either resolver alone
    looks defensible: on one tree, the venv that supplies the `west` BINARY and
    the topdir that supplies its `cwd` must come from the same workspace.
    Before the fix `find_workspace_venv` returned the foreign
    `zephyrproject/.venv` while `west_workspace_dir` returned the SDK's
    workspace -- one command, two Zephyrs.
    """
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    foreign = tmp_path / "zephyrproject"
    _plant_foreign_topdir(foreign)
    project = foreign / "my-project"
    project.mkdir()

    workspace = tmp_path / "alp-workspace"
    sdk = workspace / "alp-sdk"
    sdk.mkdir(parents=True)
    (workspace / ".west").mkdir()
    (workspace / ".west" / "config").write_text(
        "[manifest]\npath = alp-sdk\nfile = west.yml\n", encoding="utf-8"
    )
    _plant_west_capable_venv(workspace / ".venv")

    resolved_venv = find_workspace_venv(str(project), str(sdk))
    resolved_topdir = venv_module.west_workspace_dir(str(project), sdk)

    assert resolved_venv == workspace / ".venv"
    assert resolved_topdir is not None
    assert Path(resolved_topdir) == workspace
    assert resolved_venv.parent == Path(resolved_topdir)


def test_a_tan_provenance_record_does_not_make_a_directory_a_foreign_topdir(
    tmp_path, monkeypatch
):
    """The regression the full suite caught in defect 1's first cut, which
    tested for the `.west` DIRECTORY rather than `.west/config`.

    tan writes its own provenance record to `<workspace>/.west/tan-workspace-sdk`
    (`bootstrap_cmd.record_workspace_sdk`), so a workspace holding that record
    but no west config has a `.west` directory and is NOT a topdir. The first
    cut judged it foreign and refused its venv, which took `tan doctor`'s
    `venvProvenance` check out of the report entirely -- three doctor tests
    went from pass to `StopIteration`, because the check was not failing, it
    was absent.
    """
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    sdk = tmp_path / "alp-sdk"
    sdk.mkdir()
    workspace = tmp_path / "ws"
    _plant_west_capable_venv(workspace / ".venv")
    (workspace / ".west").mkdir()
    (workspace / ".west" / "tan-workspace-sdk").write_text("{}", encoding="utf-8")

    assert find_workspace_venv(str(workspace), str(sdk)) == workspace / ".venv"


def test_a_real_west_config_alongside_the_record_is_still_judged(tmp_path, monkeypatch):
    """...and the narrowing does not disarm the guard: once a real
    `.west/config` is there naming someone else's manifest, the directory is a
    topdir again and its venv is refused, provenance record or not."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    sdk = tmp_path / "sdk-parent" / "alp-sdk"
    sdk.mkdir(parents=True)
    _plant_west_capable_venv(sdk.parent / ".venv")

    workspace = tmp_path / "ws"
    _plant_west_capable_venv(workspace / ".venv")
    (workspace / ".west").mkdir()
    (workspace / ".west" / "tan-workspace-sdk").write_text("{}", encoding="utf-8")
    (workspace / ".west" / "config").write_text(
        "[manifest]\npath = zephyr\nfile = west.yml\n", encoding="utf-8"
    )

    assert find_workspace_venv(str(workspace), str(sdk)) == sdk.parent / ".venv"
