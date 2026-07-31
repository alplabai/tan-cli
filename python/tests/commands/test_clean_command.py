# SPDX-License-Identifier: Apache-2.0
"""`tan clean` -- the oracle's own unit tests ported, plus the destructive-path
coverage no golden exists for.

**No conformance fixture covers `clean`.** `contract/README.md` freezes neither
`clean` nor its neighbours, so these tests plus
`tests/parity/test_clean_parity.py` (which diffs the real Rust binary on
mirrored trees) are the ONLY gate. Named twins live in
`crates/tan-cli/src/commands/clean.rs`, `crates/tan-core/src/clean.rs` and
`crates/tan-core/src/path_guard.rs`.

Every destructive test plants a CANARY **outside** the build root and asserts it
survived. That is the assertion that matters: `clean` is the one command where a
bug destroys a customer's work, and "the envelope looked right" is not evidence
that nothing else was deleted.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tan.cli import app
from tan.commands.clean_cmd import (
    _classify,
    _is_under,
    _normalize,
    _rust_join,
    _subsumed_by_build_root,
    is_link,
    is_unsafe_removal_target,
    os_error_text,
    parse_manifest_slices,
    plan_clean_targets,
    sdk_root_resolves,
)

runner = CliRunner()

WINDOWS = os.name == "nt"

#: A minimal v1 manifest with one slice carrying `build_dir`, emitted as an
#: explicit QUOTED scalar so `""` tests `build_dir: ""` (present but empty), not
#: the absent-key case. That distinction is exactly what let an empty
#: `build_dir` reach a recursive removal in the oracle's own history.
def manifest(build_dir: str | None, core_id: str = "m55_hp") -> str:
    body = f"schema_version: 1\nslices:\n- core_id: {core_id}\n  os: zephyr\n  status: ok\n"
    if build_dir is not None:
        body += f'  build_dir: "{build_dir}"\n'
    return body


def make_project(root: Path, *, manifest_text: str | None = None, marker: bool = True) -> Path:
    """A scratch project with a build tree, a state file, sources, and a CANARY
    file plus a `precious/` directory OUTSIDE the build root."""
    proj = root / "proj"
    (proj / "scripts").mkdir(parents=True)
    if marker:
        # The SDK-root marker `resolve_sdk_root` looks for (I-31).
        (proj / "scripts" / "alp_project.py").write_text("")
    (proj / "src").mkdir()
    (proj / "src" / "main.c").write_text("int main(void){return 0;}")
    (proj / "board.yaml").write_text("schema_version: 1\n")
    (proj / ".alp-build-state.json").write_text("{}")
    (proj / "build" / "m55_hp-zephyr" / "zephyr").mkdir(parents=True)
    (proj / "build" / "m55_hp-zephyr" / "zephyr" / "zephyr.elf").write_text("ELF")
    (proj / "build" / "marker").write_text("x")
    (root / "precious").mkdir()
    (root / "precious" / "work.txt").write_text("A CUSTOMER'S WORK")
    (root / "CANARY.txt").write_text("CANARY")
    (root / "home").mkdir()
    if manifest_text is not None:
        (proj / "build" / "system-manifest.yaml").write_text(manifest_text)
    return proj


def isolate(monkeypatch, root: Path, proj: Path) -> None:
    """cwd at the project, `HOME`/`USERPROFILE` at a scratch dir -- a developer's
    real `~/.alp/sdk-default` must not decide what this test resolves."""
    monkeypatch.chdir(proj)
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setenv("USERPROFILE", str(root / "home"))


def survivors(root: Path) -> set[str]:
    return {str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*")}


def assert_canary_intact(root: Path) -> None:
    """The whole point of every destructive test below."""
    assert (root / "CANARY.txt").read_text() == "CANARY"
    assert (root / "precious" / "work.txt").read_text() == "A CUSTOMER'S WORK"
    assert (root / "proj" / "src" / "main.c").is_file()
    assert (root / "proj" / "board.yaml").is_file()


# ---------------------------------------------------------------------------
# Path shape -- `tan_core::path_guard`
# ---------------------------------------------------------------------------


def test_rust_join_lets_a_prefixed_value_replace_the_base():
    """`PathBuf::push` semantics. `ntpath.join` does NOT have them for a
    drive-relative path on the SAME drive, which is the shape a `--build-root`
    guard has to get right (measured against the Rust binary: `--build-root
    C:foo` reports `C:foo`, not `<project>/foo`)."""
    if WINDOWS:
        assert _rust_join(r"C:\proj", "C:foo") == "C:foo"
        assert os.path.join(r"C:\proj", "C:foo") != "C:foo"  # pins the premise
        assert _rust_join(r"C:\proj", r"\\srv\share\x") == r"\\srv\share\x"
        assert _rust_join(r"C:\proj", r"\\?\C:\x") == r"\\?\C:\x"
        # Rooted but prefixless keeps the base's drive, as Rust's push does.
        assert _rust_join(r"C:\proj", r"\rooted") == r"C:\rooted"
        # An empty value appends the separator only -- the oracle reports
        # `<project>\` for a manifest `build_dir: ""`.
        assert _rust_join(r"C:\proj", "") == "C:\\proj\\"
    else:
        assert _rust_join("/proj", "/abs") == "/abs"
        assert _rust_join("/proj", "sub") == "/proj/sub"
        assert _rust_join("/proj", "") == "/proj/"


@pytest.mark.parametrize(
    "target",
    ["", ".", "..", "../..", "../../.."],
)
def test_a_build_root_resolving_to_the_project_or_above_is_unsafe(target):
    """The `rm -rf $UNSET_VAR` shape: every one of these resolves to the project
    root or an ancestor, and before the oracle's guard all of them were
    recursively removed at exit 0."""
    project = os.path.abspath(os.path.join(os.sep, "home", "u", "myapp"))
    assert is_unsafe_removal_target(project, _rust_join(project, target))


def test_filesystem_and_drive_roots_are_unsafe():
    project = os.path.abspath(os.path.join(os.sep, "home", "u", "myapp"))
    assert is_unsafe_removal_target(project, os.sep)
    if WINDOWS:
        assert is_unsafe_removal_target(project, "C:\\")
        assert is_unsafe_removal_target(project, "C:")
        # A bare UNC share is a root: `\\server\share` has no Normal component.
        assert is_unsafe_removal_target(project, r"\\server\share")
        # ...but a path UNDER one is an ordinary directory.
        assert not is_unsafe_removal_target(project, r"\\server\share\x")


def test_real_build_trees_and_out_of_tree_dirs_stay_removable():
    """The rule is "not catastrophic", NOT "not outside" (`path_guard.rs:100-103`).
    An out-of-tree Yocto tmp dir is a supported clean target -- which is why this
    command cannot use `confine_to_build_root` as its only screen."""
    project = os.path.abspath(os.path.join(os.sep, "home", "u", "myapp"))
    assert not is_unsafe_removal_target(project, os.path.join(project, "build"))
    assert not is_unsafe_removal_target(
        project, os.path.abspath(os.path.join(os.sep, "var", "tmp", "yocto"))
    )
    # A sibling project is outside but not an ancestor: allowed.
    assert not is_unsafe_removal_target(
        project, os.path.abspath(os.path.join(os.sep, "home", "u", "other", "build"))
    )


def test_is_under_is_component_wise_not_a_string_prefix():
    base = os.path.abspath(os.path.join(os.sep, "p", "build"))
    assert _is_under(base, os.path.join(base, "x"))
    assert _is_under(base, base)
    # The defect a string prefix would introduce: a SIBLING would count as
    # contained, and containment is what decides whether a slice dir becomes its
    # own `rmtree` call.
    assert not _is_under(base, base + "2")
    assert not _is_under(base, os.path.dirname(base))
    assert not _is_under(base, os.path.join(base, "..", "out"))


# ---------------------------------------------------------------------------
# Planning -- `tan_core::clean`
# ---------------------------------------------------------------------------


def test_no_manifest_yields_build_root_then_state_file():
    project = os.path.abspath(os.path.join(os.sep, "p"))
    build = _rust_join(project, "build")
    plan = plan_clean_targets(project, build, [])
    assert plan.targets == [build, _rust_join(project, ".alp-build-state.json")]
    assert plan.rejected == []


def test_a_slice_under_the_build_root_adds_no_target():
    """Subsumed by the recursive build-root removal, so it contributes nothing --
    and must not become a second `rmtree` call."""
    project = os.path.abspath(os.path.join(os.sep, "p"))
    build = _rust_join(project, "build")
    plan = plan_clean_targets(
        project, build, [{"core_id": "c", "os": "zephyr", "build_dir": "build/m55-zephyr"}]
    )
    assert len(plan.targets) == 2
    assert plan.rejected == []


def test_an_out_of_tree_slice_dir_is_appended_and_duplicates_collapse():
    project = os.path.abspath(os.path.join(os.sep, "home", "u", "p"))
    build = _rust_join(project, "build")
    slices = [
        {"core_id": "a", "os": "yocto", "build_dir": "../oot"},
        {"core_id": "b", "os": "yocto", "build_dir": "../oot"},
    ]
    plan = plan_clean_targets(project, build, slices)
    assert len(plan.targets) == 3
    assert plan.targets[-1] == _rust_join(project, "../oot")
    assert plan.rejected == []


@pytest.mark.parametrize("build_dir", ["", ".", "..", "../..", "../../.."])
def test_a_catastrophic_slice_build_dir_is_rejected_and_reported(build_dir):
    """A manifest naming the project root is a broken manifest, not a reason to
    clean less in silence: the candidate is REFUSED and surfaced, and the command
    fails. Silence here is what let an empty `build_dir` delete a project tree."""
    project = os.path.abspath(os.path.join(os.sep, "home", "u", "myapp"))
    build = _rust_join(project, "build")
    plan = plan_clean_targets(
        project, build, [{"core_id": "c", "os": "zephyr", "build_dir": build_dir}]
    )
    assert len(plan.targets) == 2, f"build_dir {build_dir!r} must add no target"
    assert len(plan.rejected) == 1
    assert "refusing to remove slice 'c'" in plan.rejected[0].reason()


def test_the_state_file_is_never_screened_but_the_build_root_always_is():
    """The state file is one fixed name under the project root and a single
    unlink, never a recursive removal, so the oracle exempts it. The build root
    is not exempt -- which is what the empty/`.`/`..` cases rely on."""
    project = os.path.abspath(os.path.join(os.sep, "p"))
    plan = plan_clean_targets(project, project, [])
    assert plan.targets == [_rust_join(project, ".alp-build-state.json")]
    assert len(plan.rejected) == 1
    assert "refusing to remove build root" in plan.rejected[0].reason()


def test_classify_covers_every_arm():
    assert _classify(True, False, False) == ("dir", "removed")
    assert _classify(True, False, True) == ("dir", "would-remove")
    assert _classify(False, True, False) == ("file", "removed")
    assert _classify(False, True, True) == ("file", "would-remove")
    assert _classify(False, False, False) == ("absent", "absent")
    assert _classify(False, False, True) == ("absent", "absent")


@pytest.mark.skipif(not WINDOWS, reason="drive-relative paths are a Windows shape")
def test_a_drive_relative_slice_build_dir_stays_its_own_target(tmp_path):
    """`Path(build_root) / "C:rel"` re-reads a drive-relative value as relative
    to the base and would answer "already covered by the build-root removal",
    silently dropping a target the oracle reports. Measured against the Rust
    binary: a manifest `build_dir: "C:rel"` yields an `absent` target entry."""
    build = str(tmp_path / "build")
    assert not _subsumed_by_build_root(build, "C:rel")
    project = str(tmp_path / "proj")
    plan = plan_clean_targets(
        project, build, [{"core_id": "c", "os": "z", "build_dir": "C:rel"}]
    )
    assert plan.targets[-1] == "C:rel"


def test_confine_to_build_root_is_reused_for_the_containment_question(tmp_path):
    """The port must not grow a second containment guard. This pins that
    `_subsumed_by_build_root` really does consult `confine_to_build_root`, by
    feeding it a shape only the resolving guard catches: on Windows a
    drive-relative `C:foo` is neither `is_absolute()` nor lexically outside in
    any obvious way, and off Windows a `..` escape stands in."""
    build = tmp_path / "build"
    build.mkdir()
    assert _subsumed_by_build_root(str(build), str(build / "inner"))
    assert _subsumed_by_build_root(str(build), str(build))
    assert not _subsumed_by_build_root(str(build), str(tmp_path / "elsewhere"))
    assert not _subsumed_by_build_root(str(build), str(build / ".." / "elsewhere"))


# ---------------------------------------------------------------------------
# Manifest reading
# ---------------------------------------------------------------------------


def test_a_well_formed_manifest_yields_its_slices():
    slices, err = parse_manifest_slices(manifest("../oot"))
    assert err is None
    assert slices[0]["build_dir"] == "../oot"


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("", "missing field `schema_version`"),
        ("# only a comment\n", "missing field `schema_version`"),
        ("null\n", "invalid type: unit value, expected struct SystemManifest"),
        ("- a\n", "invalid type: sequence, expected struct SystemManifest"),
        ("7\n", "invalid type: integer `7`, expected struct SystemManifest"),
        ('"hi"\n', 'invalid type: string "hi", expected struct SystemManifest'),
        ("slices: []\n", "missing field `schema_version`"),
        ('schema_version: "1"\n', 'schema_version: invalid type: string "1", expected u32'),
        ("schema_version: true\n", "schema_version: invalid type: boolean `true`, expected u32"),
        ("schema_version: 2\n", "unsupported system-manifest schema_version 2"),
        ("schema_version: 1\nslices: 7\n", "slices: invalid type: integer `7`, expected a sequence"),
        ("schema_version: 1\nslices:\n- 7\n", "slices[0]: invalid type: integer `7`"),
        ("schema_version: 1\nslices:\n- os: z\n", "slices[0]: missing field `core_id`"),
        ("schema_version: 1\nslices:\n- core_id: c\n", "slices[0]: missing field `os`"),
        (
            "schema_version: 1\nslices:\n- core_id: c\n  os: z\n  build_dir: [a]\n",
            "slices[0].build_dir: invalid type: sequence, expected a string",
        ),
    ],
)
def test_a_malformed_manifest_fails_closed_with_the_oracles_wording(text, fragment):
    """Every message was harvested from the Rust binary. FAIL-CLOSED: no slices
    survive a document the oracle rejects, so a half-read manifest can never hand
    a garbage `build_dir` to a recursive removal."""
    slices, err = parse_manifest_slices(text)
    assert slices == []
    assert err is not None and fragment in err


def test_a_plain_scalar_core_id_is_accepted_like_serde_yaml_does():
    """serde_yaml hands an untyped plain scalar to whichever visitor the field
    asks for, so `core_id: 7` deserializes into `String`. Verified against the
    Rust binary (exit 0, no issue). Demanding `isinstance(str)` here would emit a
    warning the oracle does not."""
    slices, err = parse_manifest_slices("schema_version: 1\nslices:\n- core_id: 7\n  os: 1\n")
    assert err is None
    assert len(slices) == 1


def test_numeric_build_dir_is_not_turned_into_a_delete_target():
    """A DELIBERATE divergence, pinned so it cannot drift silently.

    serde_yaml coerces `build_dir: 7` to `"7"`, and the Rust binary then adds a
    THIRD removal target at `<project>/7` (measured). The port refuses: this is
    the only manifest field that becomes a path handed to a recursive removal,
    and deriving a delete target from a number in a malformed manifest is not
    behaviour worth reproducing. The port removes strictly LESS, which is the
    only direction a destructive command may drift in.
    """
    slices, err = parse_manifest_slices(
        "schema_version: 1\nslices:\n- core_id: c\n  os: z\n  build_dir: 7\n"
    )
    assert err is None
    project = os.path.abspath(os.path.join(os.sep, "p"))
    plan = plan_clean_targets(project, _rust_join(project, "build"), slices)
    assert len(plan.targets) == 2, "a numeric build_dir must not become a target"
    assert plan.rejected == []


def test_without_pyyaml_the_sweep_is_reported_as_skipped_not_silently_dropped(
    tmp_path, monkeypatch
):
    """**The FROZEN binary has no PyYAML.** `scripts/build_binary.sh` documents
    its build environment as `pip install typer rich pyinstaller` and nothing
    else, so this arm is not hypothetical -- it is what customers run.

    Reported, not swallowed: without a parser this run cannot know whether the
    manifest declares an out-of-tree slice `build_dir`, so `clean` may have left
    one behind. A `clean` that claims success while a Yocto tmp dir survives is
    the silent gap; a warning is not. It stays a WARNING -- `clean` never fails
    over a manifest -- and the build root and state file are removed as normal.

    A second YAML parser is deliberately NOT hand-rolled for this (the precedent
    `presets_cmd` set for SoM presets): a mis-parse here would not merely under-
    report, it would name a PATH handed to a recursive removal. Closing the gap
    belongs in the packaging decision, not in a fallback scanner.
    """
    proj = make_project(tmp_path, manifest_text=manifest("../oot"))
    isolate(monkeypatch, tmp_path, proj)
    real_import = __import__

    def no_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no module named yaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_yaml)
    result = runner.invoke(app, ["clean", "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["ok"] is True
    assert [i["code"] for i in doc["issues"]] == ["clean.manifest-unreadable"]
    assert "PyYAML is not installed" in doc["issues"][0]["message"]
    assert doc["data"]["removed"] == 2
    assert not (proj / "build").exists()
    assert_canary_intact(tmp_path)


def test_an_unreadable_manifest_is_silent_not_a_warning(tmp_path, monkeypatch):
    """A READ failure -- a directory in its place, non-UTF-8 bytes, a denied ACL
    -- matches the oracle's `Err(_) => None` arm: no issue, no text, exit
    unchanged. Only a document that WAS read and could not be understood warns.
    A port that warned here would diverge on every project whose manifest is
    merely inaccessible."""
    proj = make_project(tmp_path)
    (proj / "build" / "system-manifest.yaml").mkdir()
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(app, ["clean", "--dry-run", "--format", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["issues"] == []


def test_a_non_utf8_manifest_is_silent(tmp_path, monkeypatch):
    proj = make_project(tmp_path)
    (proj / "build" / "system-manifest.yaml").write_bytes(
        b"schema_version: 1\nslices: []\n# \xff\xfe\x00\n"
    )
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(app, ["clean", "--dry-run", "--format", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["issues"] == []


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def test_is_link_is_not_os_path_islink(tmp_path):
    """`os.path.islink` returns False for a Windows JUNCTION (`ntpath.islink`
    tests `IO_REPARSE_TAG_SYMLINK` only, and `stat.S_ISLNK` is False for a mount
    point too). A guard written on it lets a junction reach `shutil.rmtree`,
    which then refuses and reports a spurious `remove-failed` -- a live defect in
    the first cut of this port, caught by diffing against the Rust binary."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "inside.txt").write_text("x")
    link = tmp_path / "link"
    if WINDOWS:
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        if made.returncode != 0:  # pragma: no cover -- policy-dependent
            pytest.skip("cannot create a junction on this host")
        assert not os.path.islink(link), "premise: os.path.islink misses junctions"
    else:
        link.symlink_to(target, target_is_directory=True)
    assert is_link(str(link))
    assert not is_link(str(target))
    assert not is_link(str(tmp_path / "absent"))


def test_a_junctioned_build_root_unlinks_the_link_not_its_target(tmp_path, monkeypatch):
    """Never follow a link OUT of the tree. Matches the oracle, whose
    `remove_dir_all` removes the link only (verified against the Rust binary)."""
    proj = make_project(tmp_path)
    import shutil as _shutil

    _shutil.rmtree(proj / "build")
    if WINDOWS:
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(proj / "build"), str(tmp_path / "precious")],
            capture_output=True,
        )
        if made.returncode != 0:  # pragma: no cover
            pytest.skip("cannot create a junction on this host")
    else:
        (proj / "build").symlink_to(tmp_path / "precious", target_is_directory=True)
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(app, ["clean", "--format", "json"])
    assert result.exit_code == 0
    assert not (proj / "build").exists()
    assert_canary_intact(tmp_path)


def test_a_link_inside_the_build_root_is_never_followed(tmp_path, monkeypatch):
    proj = make_project(tmp_path)
    escape = proj / "build" / "escape"
    if WINDOWS:
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(escape), str(tmp_path / "precious")],
            capture_output=True,
        )
        if made.returncode != 0:  # pragma: no cover
            pytest.skip("cannot create a junction on this host")
    else:
        escape.symlink_to(tmp_path / "precious", target_is_directory=True)
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(app, ["clean", "--format", "json"])
    assert result.exit_code == 0
    assert not (proj / "build").exists()
    assert_canary_intact(tmp_path)


# ---------------------------------------------------------------------------
# End to end -- the destructive paths
# ---------------------------------------------------------------------------


def test_dry_run_lists_everything_and_writes_nothing(tmp_path, monkeypatch):
    """A preview path must write NOTHING. Asserted on the whole tree, not just
    the two targets: a dry run that created so much as a parent directory would
    fail here."""
    proj = make_project(tmp_path, manifest_text=manifest("../oot"))
    before = survivors(tmp_path)
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(app, ["clean", "--dry-run", "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["command"] == "clean"
    assert doc["ok"] is True
    assert doc["data"]["dryRun"] is True
    assert doc["data"]["removed"] == 0
    assert [t["action"] for t in doc["data"]["targets"]] == [
        "would-remove",
        "would-remove",
        "absent",
    ]
    assert survivors(tmp_path) == before, "a dry run must not touch the filesystem"
    assert_canary_intact(tmp_path)


def test_dry_run_writes_nothing_even_when_it_would_refuse(tmp_path, monkeypatch):
    proj = make_project(tmp_path, manifest_text=manifest(""))
    before = survivors(tmp_path)
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(app, ["clean", "--dry-run", "--build-root", "", "--format", "json"])
    assert result.exit_code == 1
    assert survivors(tmp_path) == before
    assert_canary_intact(tmp_path)


def test_a_real_run_removes_the_build_tree_and_the_state_file(tmp_path, monkeypatch):
    proj = make_project(tmp_path)
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(app, ["clean", "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["data"]["removed"] == 2
    assert [t["action"] for t in doc["data"]["targets"]] == ["removed", "removed"]
    assert not (proj / "build").exists()
    assert not (proj / ".alp-build-state.json").exists()
    assert_canary_intact(tmp_path)


def test_a_second_pass_reports_nothing_to_remove(tmp_path, monkeypatch):
    proj = make_project(tmp_path)
    isolate(monkeypatch, tmp_path, proj)

    assert runner.invoke(app, ["clean"]).exit_code == 0
    result = runner.invoke(app, ["clean"])
    assert result.exit_code == 0
    assert result.stdout == "", "stdout is the envelope channel in text mode too"
    assert "clean: nothing to remove" in result.stderr
    assert_canary_intact(tmp_path)


@pytest.mark.parametrize("raw", ["", ".", "..", "../.."])
def test_an_unsafe_build_root_is_refused_and_the_project_survives(raw, tmp_path, monkeypatch):
    """The `rm -rf $UNSET_VAR` shape, end to end. Before the guard, all of these
    recursively removed the source tree and exited 0."""
    proj = make_project(tmp_path)
    before = survivors(tmp_path)
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(app, ["clean", "--build-root", raw, "--format", "json"])
    assert result.exit_code == 1, f"--build-root {raw!r} must fail"
    doc = json.loads(result.stdout)
    assert doc["ok"] is False
    assert [i["code"] for i in doc["issues"]] == ["clean.unsafe-build-root"]
    assert doc["data"]["targets"] == []
    assert doc["data"]["removed"] == 0
    assert survivors(tmp_path) == before, f"--build-root {raw!r} deleted something"
    assert_canary_intact(tmp_path)


@pytest.mark.parametrize("build_dir", ["", ".", "..", "../../.."])
def test_an_unsafe_manifest_build_dir_is_refused_and_reported(build_dir, tmp_path, monkeypatch):
    """`build/` is legitimately gone; the project root is not, and the refusal
    fails the command rather than passing in silence."""
    proj = make_project(tmp_path, manifest_text=manifest(build_dir))
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(app, ["clean", "--format", "json"])
    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert doc["ok"] is False
    assert "clean.unsafe-target" in [i["code"] for i in doc["issues"]]
    assert doc["data"]["targets"][0]["action"] == "refused-unsafe"
    assert not (proj / "build").exists()
    assert_canary_intact(tmp_path)


@pytest.mark.skipif(not WINDOWS, reason="Windows-only path shapes")
@pytest.mark.parametrize(
    "raw",
    ["C:foo", r"\\server\share\x", r"\\?\C:\nope", r"\rooted-nope", "bad<>|name", "x" * 300],
)
def test_hostile_windows_build_root_shapes_destroy_nothing(raw, tmp_path, monkeypatch):
    """`C:foo`, a UNC share, the `\\\\?\\` device namespace, a drive-relative
    root, an illegal name and an over-long path. Each must produce an envelope
    and leave the canary alone -- whatever the exit code."""
    proj = make_project(tmp_path)
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(app, ["clean", "--build-root", raw, "--format", "json"])
    assert result.exit_code in (0, 1)
    doc = json.loads(result.stdout)
    assert doc["command"] == "clean"
    assert doc["exitCode"] == result.exit_code
    assert_canary_intact(tmp_path)


def test_a_build_root_outside_the_tree_is_still_removable(tmp_path, monkeypatch):
    """The rule is "not catastrophic", not "not outside" -- verified against the
    Rust binary, which removes `../outside` and exits 0. This is why `clean`
    cannot screen every target with `confine_to_build_root`."""
    proj = make_project(tmp_path)
    (tmp_path / "oot").mkdir()
    (tmp_path / "oot" / "tmp.txt").write_text("x")
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(app, ["clean", "--build-root", "../oot", "--format", "json"])
    assert result.exit_code == 0
    assert not (tmp_path / "oot").exists()
    assert_canary_intact(tmp_path)


def test_a_missing_sdk_root_fails_before_anything_is_removed(tmp_path, monkeypatch):
    proj = make_project(tmp_path, marker=False)
    before = survivors(tmp_path)
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(app, ["clean", "--sdk-root", str(tmp_path / "nope"), "--format", "json"])
    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert [i["code"] for i in doc["issues"]] == ["clean.sdk-root-not-found"]
    assert doc["data"]["buildRoot"] == ""
    # Absent, not null -- `sdk` is omitted when nothing resolved.
    assert "sdk" not in doc
    assert survivors(tmp_path) == before
    assert_canary_intact(tmp_path)


def test_an_explicit_sdk_root_is_terminal_and_reported(tmp_path, monkeypatch):
    """I-31: `--sdk-root` never falls through to a lower tier. A valid one is
    reported with the `sdkRootFlag` tier, and `sdk.root` is forward-slash on
    every platform (normalised in `SdkInfo.as_dict`)."""
    proj = make_project(tmp_path)
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(
        app, ["clean", "--sdk-root", str(proj), "--dry-run", "--format", "json"]
    )
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["sdk"]["sourceTier"] == "sdkRootFlag"
    assert "\\" not in doc["sdk"]["root"]


def test_a_positional_app_path_roots_the_removal(tmp_path, monkeypatch):
    """A non-`.` positional overrides `--project` for the removal, while
    `project.root` still reports the `--project`-derived root."""
    proj = make_project(tmp_path)
    other = tmp_path / "other"
    (other / "build").mkdir(parents=True)
    (other / "build" / "art.bin").write_text("x")
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(app, ["clean", str(other), "--format", "json"])
    assert result.exit_code == 0
    assert not (other / "build").exists()
    assert (proj / "build").is_dir(), "the positional must override --project"
    assert_canary_intact(tmp_path)


def test_a_read_only_artefact_inside_the_build_tree_is_still_removed(tmp_path, monkeypatch):
    """Rust's `remove_dir_all` deletes a read-only file; `shutil.rmtree` alone
    fails the WHOLE tree with `Access is denied`. Measured against the Rust
    binary: without the retry hook the port left every artefact in place and
    warned, where Rust exited 0 with the build dir gone."""
    proj = make_project(tmp_path)
    locked = proj / "build" / "locked.txt"
    locked.write_text("x")
    os.chmod(locked, stat.S_IREAD)
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(app, ["clean", "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["issues"] == []
    assert doc["data"]["removed"] == 2
    assert not (proj / "build").exists()
    assert_canary_intact(tmp_path)


def test_a_failed_state_file_unlink_is_an_error_and_exit_1(tmp_path, monkeypatch):
    """The state-file unlink is NOT `ignore_errors` in the Python source, so a
    failure propagates to exit 1 -- and the envelope must say `remove-failed`,
    never claim a removal that did not happen."""
    proj = make_project(tmp_path)
    isolate(monkeypatch, tmp_path, proj)
    state = proj / ".alp-build-state.json"
    # The state file must be the ONLY removal left to fail, so take the build
    # tree out of the plan first (it reports `absent`, no issue). The two
    # blockers below are not equally precise: Windows' open handle stops that
    # one file, where POSIX has no per-file equivalent and a read-only PARENT
    # stops every unlink in `proj` -- including the `rmdir` that finishes
    # `build/`, which `clean` then reports as a SECOND, warning-severity
    # `clean.remove-failed`. That report is correct (two removals failed, two
    # issues, and the envelope must never claim a directory it left behind was
    # removed -- see the test below), but it is not what this pins, and it made
    # this test pass on Windows and fail on Linux.
    shutil.rmtree(proj / "build")

    if WINDOWS:
        handle = state.open("rb")  # an open handle blocks unlink
        try:
            result = runner.invoke(app, ["clean", "--format", "json"])
        finally:
            handle.close()
    else:
        os.chmod(proj, stat.S_IRUSR | stat.S_IXUSR)
        try:
            result = runner.invoke(app, ["clean", "--format", "json"])
        finally:
            os.chmod(proj, stat.S_IRWXU)

    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert doc["ok"] is False
    assert [i["code"] for i in doc["issues"]] == ["clean.remove-failed"]
    assert doc["data"]["targets"][-1]["action"] == "remove-failed"
    assert doc["data"]["removed"] < 2
    assert state.exists()
    assert_canary_intact(tmp_path)


def test_a_directory_removal_failure_is_a_warning_not_a_failure(tmp_path, monkeypatch):
    """Best-effort, matching `rmtree(ignore_errors=True)`: exit stays 0. It is
    still REPORTED -- previously a text-only line, invisible under `--format
    json`, while the envelope claimed the directory was removed."""
    proj = make_project(tmp_path)
    isolate(monkeypatch, tmp_path, proj)
    import tan.commands.clean_cmd as mod

    def explode(_path):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(mod, "_remove_dir", explode)
    result = runner.invoke(app, ["clean", "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["ok"] is True
    assert doc["issues"][0]["code"] == "clean.remove-failed"
    assert doc["issues"][0]["severity"] == "warning"
    assert doc["data"]["targets"][0]["action"] == "remove-failed"
    assert doc["data"]["removed"] == 1
    assert (proj / "build").is_dir()
    assert_canary_intact(tmp_path)


def test_quiet_suppresses_the_manifest_notice_but_keeps_the_issue(tmp_path, monkeypatch):
    proj = make_project(tmp_path, manifest_text="- not a manifest\n")
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(app, ["clean", "--quiet", "--dry-run", "--format", "json"])
    assert result.exit_code == 0
    assert [i["code"] for i in json.loads(result.stdout)["issues"]] == [
        "clean.manifest-unreadable"
    ]

    proj2 = make_project(tmp_path / "second", manifest_text="- not a manifest\n")
    monkeypatch.chdir(proj2)
    quiet = runner.invoke(app, ["clean", "--quiet", "--dry-run"])
    loud = runner.invoke(app, ["clean", "--dry-run"])
    assert "ignoring unreadable" not in quiet.stderr
    assert "ignoring unreadable" in loud.stderr


def test_text_mode_never_writes_to_stdout(tmp_path, monkeypatch):
    """One stray byte on stdout silently breaks the extension: it renders nothing
    and reports no error."""
    proj = make_project(tmp_path, manifest_text=manifest(""))
    isolate(monkeypatch, tmp_path, proj)

    for argv in (["clean", "--dry-run"], ["clean"], ["clean", "--build-root", ""]):
        assert runner.invoke(app, argv).stdout == "", argv


def test_a_bogus_format_is_a_usage_error():
    assert runner.invoke(app, ["clean", "--format", "yaml"]).exit_code == 2


@pytest.mark.parametrize(
    "flag",
    [["--verbose"], ["--no-color"], ["--non-interactive"], ["--ci"], ["--all"],
     ["--target", "m55_hp"]],
)
def test_the_globals_the_oracle_ignores_are_accepted_not_rejected(flag, tmp_path, monkeypatch):
    """`clap`'s `GlobalArgs` members `clean.rs` accepts and never reads. Without
    them declared, `tan clean --ci` in a CI script was a Click usage error at
    exit 2 -- the oracle exits 0 -- so nothing got cleaned and the reason was a
    flag the command does not even use."""
    proj = make_project(tmp_path)
    isolate(monkeypatch, tmp_path, proj)

    result = runner.invoke(app, ["clean", "--dry-run", *flag, "--format", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["command"] == "clean"


def test_an_unknown_flag_is_still_a_usage_error(tmp_path, monkeypatch):
    """The accepted-globals list above must not turn into "accept anything"."""
    proj = make_project(tmp_path)
    isolate(monkeypatch, tmp_path, proj)
    assert runner.invoke(app, ["clean", "--not-a-real-flag"]).exit_code == 2


def test_an_unexpected_exception_becomes_an_envelope_not_a_traceback(tmp_path, monkeypatch):
    """The recurring bug class in this port: an escaping traceback puts nothing
    parseable on stdout, and the extension renders an empty panel with no error.
    The recovery path must itself be incapable of throwing -- so it re-runs NO
    helper, in particular not the cwd-reading project resolver."""
    proj = make_project(tmp_path)
    isolate(monkeypatch, tmp_path, proj)
    import tan.commands.clean_cmd as mod

    def explode(*_a, **_k):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(mod, "resolve_project_paths", explode)
    result = runner.invoke(app, ["clean", "--format", "json"])
    assert result.exit_code == 5
    doc = json.loads(result.stdout)
    assert doc["command"] == "clean"
    assert doc["exitCode"] == 5
    assert doc["ok"] is False
    assert doc["issues"][0]["code"] == "clean.internal-failure"
    assert doc["project"] == {"root": None, "boardYaml": None}
    assert "sdk" not in doc
    assert survivors(tmp_path) >= {"CANARY.txt", "proj/build"}


def test_the_guard_survives_a_helper_that_throws_on_the_recovery_path(tmp_path, monkeypatch):
    """A DOUBLE fault is how the last Critical in this port shipped: the guard
    caught the first failure, then its own recovery called a helper that threw
    again and the process died with a raw traceback and EMPTY stdout. Break
    every helper the guard could conceivably reach and require an envelope
    anyway."""
    proj = make_project(tmp_path)
    isolate(monkeypatch, tmp_path, proj)
    import tan.commands.clean_cmd as mod

    def explode(*_a, **_k):
        raise RuntimeError("synthetic")

    for name in ("resolve_project_paths", "resolve_sdk", "sdk_root_resolves", "plan_clean_targets"):
        monkeypatch.setattr(mod, name, explode)
    result = runner.invoke(app, ["clean", "--format", "json"])
    assert result.exit_code == 5
    assert json.loads(result.stdout)["issues"][0]["code"] == "clean.internal-failure"


def test_sdk_root_resolves_finds_a_child_checkout(tmp_path):
    """The discovery tier is `util.rs`'s WIDER candidate set, not
    `discover_workspace_sdk`. `tan bootstrap` clones into `<ws>/alp-sdk`, so at
    that moment the checkout is a CHILD of the cwd (tan-cli#218) -- collapsing
    the two resolutions would make `tan clean` refuse to run there."""
    ws = tmp_path / "ws"
    (ws / "alp-sdk" / "scripts").mkdir(parents=True)
    (ws / "alp-sdk" / "scripts" / "alp_project.py").write_text("")
    assert sdk_root_resolves(None, ws)
    assert not sdk_root_resolves(None, tmp_path / "empty")


def test_os_error_text_matches_the_rust_io_error_shape():
    """`<system message> (os error <code>)`. Python's own `str(OSError)` is
    `[WinError 32] <message>: '<path>'`, which both differs from the oracle and
    repeats a path the caller's message already names."""
    err = OSError(13, "Permission denied")
    assert os_error_text(err) == "Permission denied (os error 13)"
    assert os_error_text(RuntimeError("plain")) == "plain"
    # An OSError carrying neither is still rendered, never crashed on.
    assert os_error_text(OSError()) == str(OSError())


def test_normalize_is_lexical_and_never_resolves_a_symlink(tmp_path):
    """`os.path.normpath`, not `Path.resolve()`: a project reached through a
    symlink must keep the name the user typed, and a build root that does not
    exist yet must still normalise."""
    assert _normalize(os.path.join("a", "b", "..", "c")) == os.path.join("a", "c")
    assert _normalize(os.path.join("a", ".", "b")) == os.path.join("a", "b")
