# SPDX-License-Identifier: Apache-2.0
"""Scaffold planning: the vendored-tree LF-only guard, and the two string
transforms that decide what lands in a customer's `board.yaml`.

`tan/templates/vendored/` tracks `PINNED_SDK_TAG` (see its own
`MANIFEST.md`) and is measured against a live SDK by
`tests/parity/scaffold_byte_parity.py`, not against `crates/tan-core/src/
wizard/vendored/` -- that tree is frozen at its own permanent vendor point
(`docs/ROADMAP.md`'s Standing Rules) and the two are expected to diverge.
"""
import os
import subprocess
from pathlib import Path

import pytest

from tan.core.scaffold import (
    DEFAULT_SOM_SKU,
    TEMPLATE_IDS,
    CoresError,
    PlannedFile,
    ScaffoldWriteError,
    app_core_for_sku,
    infer_runtime_for_core_id,
    is_plain_relative,
    parse_cores,
    plan_template_files,
    retarget_board_yaml_som,
    scaffold_tree_preview,
    splice_companion_cores,
    vendored_app_core_key,
    vendored_core_ids,
    write_files,
)
from tan.planner_root import bind_sdk_root
from tan.templates import VENDORED_ROOT
from tests.conftest import sdk_root

WINDOWS = os.name == "nt"

#: A real alp-sdk checkout, for the one test below that runs the REAL planner
#: helper (`_zephyr_app_dir`) rather than re-implementing its rule. Read at
#: MODULE level -- see `tests/conftest.py::sdk_root`'s own docstring on why a
#: call from inside a test body always sees it already scrubbed.
SDK = sdk_root()


def _make_dir_link(link: Path, target: Path) -> bool:
    """A directory link `link` -> `target`: a Windows JUNCTION (no elevated
    privilege needed, unlike a real symlink) or a POSIX symlink. `False` when
    the host refuses to make one at all -- matches `test_clean_command.py`'s
    own precedent for this exact tradeoff."""
    target.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        return made.returncode == 0
    link.symlink_to(target, target_is_directory=True)
    return True


def files_under(root: Path):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_the_vendored_tree_is_lf_only():
    """These bytes are written to the customer's files verbatim, and are
    byte-compared against a live SDK emit by `scaffold_byte_parity.py`. A
    Windows checkout with `autocrlf=true` and no `.gitattributes` entry for
    this path rewrites every one of them."""
    crlf = [name for name, content in files_under(VENDORED_ROOT).items() if b"\r\n" in content]
    assert crlf == [], f"CRLF crept into the vendored tree: {crlf}"


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_every_template_plans_a_board_yaml_naming_the_requested_som(template_id):
    # `iot-starter` vendors one SoM family only, and its caller rejects any other
    # SKU before reaching the planner -- so ask it for the one it supports.
    sku = DEFAULT_SOM_SKU
    files = plan_template_files(template_id, sku)
    board = next(f for f in files if f.relative_path == "board.yaml")

    assert f"sku: {sku}" in board.content
    # I-02 again, at the planner level: no top-level `os:` in anything planned.
    assert not any(line.startswith("os:") for line in board.content.splitlines())


def test_vendored_file_order_is_the_rust_macro_order_not_the_platform_sort():
    """`PurePath` ordering is case-FOLDED on Windows, so sorting `Path` objects
    put `board.yaml` before `CMakeLists.txt` there and after it on Linux -- the
    same command emitting a different `data.fileChanges[]` order per platform."""
    paths = [f.relative_path for f in plan_template_files("zephyr-app", DEFAULT_SOM_SKU)]

    assert paths == [
        "CMakeLists.txt",
        "README.md",
        "board.yaml",
        "prj.conf",
        "src/main.c",
        "testcase.yaml",
    ]


def test_minimal_app_plans_the_eight_files_the_golden_pins():
    """`contract/envelopes/init-preview-minimal-app` pins this list on the wire;
    pinning it here too says WHY it is that order (generated files first, then
    the feature files) so a reordering is caught at its source."""
    paths = [f.relative_path for f in plan_template_files("minimal-app", DEFAULT_SOM_SKU)]

    assert paths == [
        "board.yaml",
        "README.md",
        "prj.conf",
        "CMakeLists.txt",
        "src/CMakeLists.txt",
        "include/app/app.h",
        "src/main.c",
        "src/features/app_bootstrap.c",
    ]


def test_minimal_app_cmake_is_a_real_zephyr_app_and_reaches_every_source():
    """tan-cli#309: `minimal-app`'s `board.yaml` declares `os: zephyr`, but
    through v0.5.0-rc3 the file `west build` actually configured
    (`src/CMakeLists.txt` -- see `test_minimal_app_board_yaml_app_resolves_
    to_the_directory_the_planner_actually_configures` below for WHY it was
    that file and not the root one) called plain `add_executable(alp_app
    ...)`, no `find_package(Zephyr ...)` anywhere in either CMake file. CMake
    configures and links that shape fine, so `tan build` exited 0 for a
    project that was never Zephyr at all.

    Content only, and only half the defect -- this pins the CMake *shape*;
    `test_minimal_app_board_yaml_app_resolves_to_the_directory_the_planner_
    actually_configures` below pins that `board.yaml` actually points `west
    build` AT this shape, closing the gap that made the shape-only version of
    this test pass while the real end-to-end repro (tan-cli#309's own
    adversarial review) still failed. Against the reverted (pre-#309)
    generator this fails at the first assertion (no `find_package(Zephyr` in
    the root file at all); the second block below never even reaches an
    assert against that generator -- `src_cmake.index("target_sources(app")`
    raises `ValueError: substring not found`, since the old `src/
    CMakeLists.txt` had no such call.

    `test_minimal_app_plans_the_eight_files_the_golden_pins` above already
    pins the (unchanged) file list `contract/envelopes/
    init-preview-minimal-app/expected.json` pins on the wire.
    """
    files = {f.relative_path: f.content for f in plan_template_files("minimal-app", DEFAULT_SOM_SKU)}
    root_cmake = files["CMakeLists.txt"]
    src_cmake = files["src/CMakeLists.txt"]

    assert "find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})" in root_cmake
    # `find_package(Zephyr ...)` must run before `project()` -- it resolves the
    # toolchain/board machinery `project()` consumes when it enables the C
    # language (the same order every vendored template's own CMakeLists.txt
    # uses); `project()` itself has no dependency on Zephyr's `app` target.
    assert root_cmake.index("find_package(Zephyr") < root_cmake.index("project(")

    # Every file `_minimal_app_files` plans under `src/` must be reachable from
    # a `target_sources(app ...)` call -- not a second `add_executable`, which
    # Zephyr's build never links in.
    sources_block = src_cmake[src_cmake.index("target_sources(app") :]
    assert "add_executable(" not in root_cmake + src_cmake
    for path in ("main.c", "features/app_bootstrap.c"):
        assert path in sources_block, f"{path} not reachable from target_sources(app ...): {src_cmake}"


@pytest.mark.skipif(SDK is None, reason="set ALP_SDK_ROOT/ALP_SDK_PARITY_ROOT to a real alp-sdk checkout")
def test_minimal_app_board_yaml_app_resolves_to_the_directory_the_planner_actually_configures(tmp_path):
    """tan-cli#309's real blocker, closed against the REAL planner helper, not
    a reimplementation of its rule: `_zephyr_app_dir`
    (`tan/planner/orchestrator.py`) resolves `board.yaml`'s `app:` to whichever
    of that path or its PARENT holds a `CMakeLists.txt`, preferring the path
    itself. This template's `src/` deliberately keeps a `CMakeLists.txt` of
    its own, so `app: ./src` (the pre-#309 value) resolved to `src/` -- the
    file with no `find_package(Zephyr ...)` -- and the real root file
    (`test_minimal_app_cmake_is_a_real_zephyr_app_and_reaches_every_source`
    above) was never reached by the planner at all. `test_minimal_app_plans_
    the_eight_files_the_golden_pins` cannot catch this -- it never reads
    `app:`'s value, and neither language's contract fixture pins it (see
    `MANIFEST.md`)."""
    files = plan_template_files("minimal-app", DEFAULT_SOM_SKU)
    write_files(tmp_path, files)
    board_yaml = next(f.content for f in files if f.relative_path == "board.yaml")
    assert "    app: .\n" in board_yaml, board_yaml

    bind_sdk_root(SDK)
    from tan.planner.orchestrator import _zephyr_app_dir  # noqa: PLC0415 -- must import AFTER bind_sdk_root, see planner_root.py

    app_dir = _zephyr_app_dir(".", tmp_path)

    assert app_dir == tmp_path
    assert "find_package(Zephyr" in (app_dir / "CMakeLists.txt").read_text(encoding="utf-8")


def test_app_core_follows_the_som_family():
    assert app_core_for_sku("E1M-V2N101") == "m33_sm"
    assert app_core_for_sku("E1M-V2M101") == "m33_sm"
    assert app_core_for_sku("E1M-NX9101") == "m33"
    assert app_core_for_sku("E1M-AEN801") == "m55_hp"


# ---------------------------------------------------------------------------
# retarget_board_yaml_som
# ---------------------------------------------------------------------------


def test_retarget_is_byte_exact_for_the_trees_own_sku():
    """The vendored `iot` board.yaml carries a column-aligned trailing comment on
    its `sku:` line. Rebuilding the tail as a fixed two-space gap collapsed that
    alignment even when the SKU did not change -- so a no-op has to be a real
    no-op."""
    original = "som:\n  sku: E1M-AEN801           # the only supported SoM\ncores:\n"

    assert retarget_board_yaml_som(original, "E1M-AEN801") == original


def test_retarget_drops_a_stale_trailing_comment_when_the_value_changes():
    """A trailing comment routinely names the ORIGINAL SoM's vendor (e.g.
    `# Alif Ensemble E8 SoM`) -- carrying it onto a genuinely different SKU
    would misname the new board's vendor entirely, so an actual value change
    drops the tail rather than relocating it."""
    out = retarget_board_yaml_som(
        "som:\n  sku: E1M-AEN801           # Alif Ensemble E8 SoM\ncores:\n", "E1M-V2N101"
    )

    assert out == "som:\n  sku: E1M-V2N101\ncores:\n"


def test_retarget_handles_a_sku_key_with_no_value():
    """A bare `sku:` (or `sku:  # comment`) has no token to overwrite. Splicing at
    the first whitespace run would either glue the value onto the key -- read back
    as a scalar, not a mapping entry -- or swallow the `#`."""
    assert retarget_board_yaml_som("som:\n  sku:\n", "E1M-V2N101") == "som:\n  sku: E1M-V2N101\n"
    assert (
        retarget_board_yaml_som("som:\n  sku:  # tbd\n", "E1M-V2N101")
        == "som:\n  sku: E1M-V2N101  # tbd\n"
    )


def test_retarget_ignores_a_sku_outside_the_som_block():
    out = retarget_board_yaml_som(
        "meta:\n  sku: LEAVE-ME\nsom:\n  sku: E1M-AEN801\n", "E1M-V2N101"
    )

    assert "sku: LEAVE-ME" in out
    assert "sku: E1M-V2N101" in out


# ---------------------------------------------------------------------------
# is_plain_relative -- the guard on the two inputs that decide WHERE files go
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["my-app", "sub/dir", "a/b/c"])
def test_plain_relative_accepts_plain_relative_paths(value):
    assert is_plain_relative(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",  # `--from-example .` joined straight back to examples/ itself
        "./audio/i2s-tone",  # same gap, one level deeper
        "../escape",
        "a/../../b",
        "/etc/passwd",
        "\\windows\\x",  # not is_absolute() on Windows, still escapes a join
        "C:foo",  # drive-RELATIVE
        "C:\\x",
    ],
)
def test_plain_relative_rejects_anything_that_can_escape_a_join(value):
    assert not is_plain_relative(value)


# ---------------------------------------------------------------------------
# --cores: parse_cores
# ---------------------------------------------------------------------------


def test_parse_cores_is_empty_by_default():
    assert parse_cores(None) == []
    assert parse_cores("") == []


def test_parse_cores_infers_os_from_the_core_id():
    assert parse_cores("m33_sm") == [("m33_sm", "zephyr")]
    assert parse_cores("a55_cluster") == [("a55_cluster", "yocto")]


def test_parse_cores_accepts_an_explicit_os_and_multiple_entries():
    assert parse_cores("a55_cluster:off, m33_sm:zephyr") == [
        ("a55_cluster", "off"),
        ("m33_sm", "zephyr"),
    ]


@pytest.mark.parametrize(
    "raw",
    ["1bad", "M33", "m", "m33_sm:nope", "m33_sm:zephyr,m33_sm:off"],
)
def test_parse_cores_rejects_bad_id_bad_os_and_duplicates(raw):
    with pytest.raises(CoresError):
        parse_cores(raw)


def test_infer_runtime_defaults_to_zephyr():
    assert infer_runtime_for_core_id("m55_hp") == "zephyr"
    assert infer_runtime_for_core_id("a55_cluster") == "yocto"
    assert infer_runtime_for_core_id("a32_cluster") == "yocto"


# ---------------------------------------------------------------------------
# --cores: splicing into a planned board.yaml
# ---------------------------------------------------------------------------


def test_vendored_app_core_key_skips_a_pre_declared_companion_listed_first():
    """`edge-ai-starter`'s vendored board.yaml lists its companion core BEFORE
    the app core -- trusting position would return the companion."""
    board = plan_template_files("edge-ai-starter", "E1M-AEN801")
    content = next(f.content for f in board if f.relative_path == "board.yaml")

    assert vendored_app_core_key(content) == "m55_hp"
    ids = dict(vendored_core_ids(content))
    assert ids["a32_cluster"] == '"off"'
    assert ids["m55_hp"] == "zephyr"


def test_splice_is_a_no_op_with_no_cores():
    board = "som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    app: ./src\n"
    assert splice_companion_cores(board, []) == board


def test_splice_adds_a_companion_and_a_default_rpmsg_channel():
    board = "som:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    app: ./src\nlibraries:\n  - x\n"
    out = splice_companion_cores(board, [("a55_cluster", "yocto")])

    assert "  a55_cluster:\n    os: yocto\n    image: alp-image-edge\n" in out
    assert "ipc:\n  - kind: rpmsg\n" in out
    assert "endpoints: [m55_hp, a55_cluster]" in out
    # The companion lands inside the `cores:` block, before the next top-level key.
    assert out.index("a55_cluster:") < out.index("libraries:")


def test_splice_skips_a_core_already_declared_and_never_ipcs_an_off_companion():
    board = "cores:\n  a32_cluster:\n    os: \"off\"\n  m55_hp:\n    app: ./src\n"
    out = splice_companion_cores(board, [("a32_cluster", "off"), ("m55_hp", "zephyr")])

    # Both requested ids already exist in the block -- nothing new spliced.
    assert out == board
    assert "ipc:" not in out


def test_tree_preview_marks_the_last_entry():
    files = plan_template_files("minimal-app", DEFAULT_SOM_SKU)
    preview = scaffold_tree_preview(files).splitlines()

    assert preview[0] == "."
    assert preview[1] == "|-- CMakeLists.txt"
    assert preview[-1].startswith("`-- ")


# ---------------------------------------------------------------------------
# tan-cli#325: `write_files` must not follow a symlink out of the project,
# and must refuse the WHOLE run -- before anything lands on disk -- rather
# than writing through it and reporting the in-project logical path as
# written.
# ---------------------------------------------------------------------------


def test_write_files_refuses_a_write_through_a_symlinked_parent_with_a_missing_leaf(
    tmp_path,
):
    """The `tan init` repro shape exactly: `<project>/src` is a pre-existing
    directory link, and the planned leaf (`src/main.c`) does not exist yet."""
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    if not _make_dir_link(project / "src", outside):
        pytest.skip("cannot create a directory link on this host")

    files = [PlannedFile("src/main.c", "int main(void) { return 0; }\n")]
    with pytest.raises(ScaffoldWriteError):
        write_files(project, files)

    assert not (outside / "main.c").exists()
    assert not (project / "src" / "main.c").exists()


def test_write_files_refuses_a_write_through_a_symlinked_existing_leaf(tmp_path):
    """The second regression shape the issue names: the planned file itself,
    not just a parent directory, is a symlink to somewhere outside."""
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    (project / "src").mkdir(parents=True)
    outside.mkdir()
    real_file = outside / "main.c"
    real_file.write_text("old content\n", encoding="utf-8")
    link = project / "src" / "main.c"
    try:
        link.symlink_to(real_file)
    except OSError:
        pytest.skip("cannot create a file symlink on this host")

    files = [PlannedFile("src/main.c", "new content\n")]
    with pytest.raises(ScaffoldWriteError):
        write_files(project, files)

    assert real_file.read_text(encoding="utf-8") == "old content\n"


def test_write_files_refuses_the_whole_run_when_one_of_several_targets_escapes(
    tmp_path,
):
    """`README.md` alone would succeed; paired with the escaping `src/main.c`
    in the SAME run, neither may land -- 'refuse before the first write'."""
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    if not _make_dir_link(project / "src", outside):
        pytest.skip("cannot create a directory link on this host")

    files = [
        PlannedFile("README.md", "hello\n"),
        PlannedFile("src/main.c", "int main(void) { return 0; }\n"),
    ]
    with pytest.raises(ScaffoldWriteError):
        write_files(project, files)

    assert not (project / "README.md").exists()
    assert not (outside / "main.c").exists()


def test_write_files_still_works_through_a_symlinked_project_root(tmp_path):
    """Not over-tightened: a project reached THROUGH a symlinked root is a
    legitimate write, because it still lands inside the resolved root."""
    real_project = tmp_path / "real_project"
    real_project.mkdir()
    link = tmp_path / "project_link"
    if not _make_dir_link(link, real_project):
        pytest.skip("cannot create a directory link on this host")

    files = [PlannedFile("board.yaml", "som:\n  sku: E1M-AEN801\n")]
    result = write_files(link, files)

    assert result.written == ["board.yaml"]
    assert (real_project / "board.yaml").read_text(encoding="utf-8") == (
        "som:\n  sku: E1M-AEN801\n"
    )
