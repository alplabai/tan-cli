# SPDX-License-Identifier: Apache-2.0
import os
import stat
import sys

import pytest

from tan.core.build_plan import parse_build_plan
from tan.commands.build.materialise import MaterialiseError, materialise_plan

PLAN = """{
  "schemaVersion": 1, "generatedBy": "g", "boardYaml": "/w/board.yaml", "sku": "S",
  "buildRoot": "build", "warnings": [],
  "sharedArtefacts": [{"path": "shared/alp.conf", "contents": "CONFIG_A=y\\n"}],
  "slices": [{
    "coreId": "c1", "backend": "zephyr", "buildDir": "build/c1", "appDir": "app",
    "configArtefacts": [{"path": "build/c1-zephyr/alp.conf", "contents": "CONFIG_B=y\\n"}],
    "toolchain": null, "artifacts": [], "debug": {},
    "command": {"tool": "west", "args": ["build"], "cwd": "build/c1"},
    "env": {}, "envAppendPath": {}
  }]
}"""


def _plan_with_shared_path(path: str) -> str:
    escaped = path.replace("\\", "\\\\")
    return PLAN.replace('"shared/alp.conf"', f'"{escaped}"', 1)


def test_writes_shared_and_config_artefacts_with_exact_contents(tmp_path):
    written = materialise_plan(parse_build_plan(PLAN), tmp_path)
    shared = tmp_path / "shared/alp.conf"
    config = tmp_path / "build/c1-zephyr/alp.conf"
    assert shared.read_text() == "CONFIG_A=y\n"
    assert config.read_text() == "CONFIG_B=y\n"
    assert set(written) == {shared, config}


def test_writes_shared_before_any_config_artefact(tmp_path):
    """Ordering is a contract property: every sharedArtefact must land before
    any slice's configArtefacts -- verify shared is first in the returned
    write order, not just that both eventually land."""
    written = materialise_plan(parse_build_plan(PLAN), tmp_path)
    assert written[0] == tmp_path / "shared/alp.conf"
    assert written[1] == tmp_path / "build/c1-zephyr/alp.conf"


def test_refuses_to_escape_the_build_root(tmp_path):
    """Plans are trusted input, but writes stay confined under buildRoot."""
    evil = PLAN.replace('"shared/alp.conf"', '"../escaped.conf"')
    try:
        materialise_plan(parse_build_plan(evil), tmp_path)
    except ValueError as e:
        assert "escape" in str(e).lower()
    else:
        raise AssertionError("must refuse a path escaping the build root")
    assert not (tmp_path.parent / "escaped.conf").exists()


def test_escape_error_is_a_structured_materialise_error(tmp_path):
    """The escape refusal must be a coded, structured failure (code/message),
    never a bare exception the caller can't route into the envelope contract."""
    evil = PLAN.replace('"shared/alp.conf"', '"../escaped.conf"')
    with pytest.raises(MaterialiseError) as exc_info:
        materialise_plan(parse_build_plan(evil), tmp_path)
    assert exc_info.value.code
    assert exc_info.value.message


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows-specific path shapes")
def test_refuses_windows_rooted_and_drive_relative_escape(tmp_path):
    """Regression class from the Rust oracle (`path_guard::is_plain_relative`):
    a rooted-no-drive `\\foo`, or a drive-relative `X:foo` naming a DIFFERENT
    drive than build_root, discards build_root when joined -- refuse both
    exactly like a `..` escape.

    Note: unlike Rust's `PathBuf::push` (which replaces the base outright for
    ANY drive-having, root-less component -- verified in
    `tan-core/src/path_guard.rs`'s own test, always against a differing
    drive), Python's `PurePath.__truediv__` special-cases a drive-relative
    component whose drive MATCHES the base: it merges as an ordinary
    relative join instead of discarding the base. So `C:foo` onto a
    C:-rooted build_root is not actually dangerous in Python and is not
    tested here as an escape; a genuinely different drive is."""
    own_drive = tmp_path.drive[:1].upper() or "C"
    other_drive = "Z" if own_drive != "Z" else "Y"
    for raw in [r"\escaped.conf", f"{other_drive}:escaped.conf"]:
        evil = _plan_with_shared_path(raw)
        with pytest.raises(MaterialiseError, match="escape"):
            materialise_plan(parse_build_plan(evil), tmp_path)


def test_artefact_write_failure_is_structured_not_a_bare_exception(tmp_path):
    """A parent path component that's actually a FILE (not a directory) makes
    `mkdir(parents=True)` raise -- must surface as a coded MaterialiseError,
    not an uncaught OSError/NotADirectoryError."""
    (tmp_path / "shared").write_text("i am a file, not a directory", encoding="utf-8")
    with pytest.raises(MaterialiseError) as exc_info:
        materialise_plan(parse_build_plan(PLAN), tmp_path)
    assert "shared/alp.conf" in exc_info.value.message


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX chmod semantics")
def test_artefact_write_permission_denied_is_structured_posix(tmp_path):
    target_dir = tmp_path / "shared"
    target_dir.mkdir()
    os.chmod(target_dir, stat.S_IREAD | stat.S_IEXEC)  # read-only dir: write refused
    try:
        with pytest.raises(MaterialiseError):
            materialise_plan(parse_build_plan(PLAN), tmp_path)
    finally:
        os.chmod(target_dir, stat.S_IRWXU)  # restore so tmp_path cleanup can remove it


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows readonly-attribute semantics")
def test_artefact_write_permission_denied_is_structured_windows(tmp_path):
    """Windows directory permissions don't gate writes the way POSIX chmod
    does, but a pre-existing READ-ONLY file at the target path does refuse
    an overwrite -- exercise that instead, same failure shape (OSError ->
    coded MaterialiseError, no bare traceback)."""
    target_dir = tmp_path / "shared"
    target_dir.mkdir()
    target_file = target_dir / "alp.conf"
    target_file.write_text("old", encoding="utf-8")
    os.chmod(target_file, stat.S_IREAD)
    try:
        with pytest.raises(MaterialiseError) as exc_info:
            materialise_plan(parse_build_plan(PLAN), tmp_path)
        assert "shared/alp.conf" in exc_info.value.message
    finally:
        os.chmod(target_file, stat.S_IWRITE)
