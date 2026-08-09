# SPDX-License-Identifier: Apache-2.0
"""Scaffold planning: the vendored-tree LF-only guard, and the two string
transforms that decide what lands in a customer's `board.yaml`.

`tan/templates/vendored/` tracks `PINNED_SDK_TAG` (see its own
`MANIFEST.md`) and is measured against a live SDK by
`tests/parity/scaffold_byte_parity.py`, not against `crates/tan-core/src/
wizard/vendored/` -- that tree is frozen at its own permanent vendor point
(`docs/ROADMAP.md`'s Standing Rules) and the two are expected to diverge.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tan.core import scaffold as scaffold_module
from tan.core.scaffold import (
    DEFAULT_SOM_SKU,
    TEMPLATE_IDS,
    CoresError,
    PlannedFile,
    ScaffoldWriteError,
    TemplateDataError,
    UnsupportedSomError,
    app_core_for_sku,
    infer_runtime_for_core_id,
    is_plain_relative,
    parse_cores,
    plan_template_files,
    read_example_tree,
    retarget_board_yaml_cores,
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

#: ``python/`` -- pinned onto the child's PYTHONPATH by ``run_tan`` below.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

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


def run_tan(*argv, cwd):
    """`python -m tan ...` as a real subprocess, the harness
    `tests/commands/test_init_command.py` uses -- repeated here for the ONE
    end-to-end case this module owns (tan-cli#404) rather than importing across
    test packages. `PYTHONPATH` carries `python/` so the child resolves `tan`
    from a scratch cwd with no `pip install`."""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=env,
    )


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
# retarget_board_yaml_som -- tan-cli#404: wrapped comments, and CRLF sources
# ---------------------------------------------------------------------------

#: `examples/aen/edgeai-vision-aen/board.yaml`'s real `sku:` block, one of the
#: 12 in the SDK example catalogue whose trailing comment wraps past its first
#: physical line. Reproduced with its real column alignment, because the
#: alignment is exactly what makes the continuation lines LOOK like part of the
#: comment while a per-line rewrite treats them as unrelated content (#404).
WRAPPED_COMMENT_BOARD_YAML = (
    "som:\n"
    "  sku: E1M-AEN801          # Alif Ensemble E8 (the lead AEN part) -- carries\n"
    "                           # a pair of Ethos-U55s (vision) + an Ethos-U85\n"
    "                           # (generative) this example dispatches inference\n"
    "                           # to, plus the on-die VeriSilicon ISP Pico\n"
    "                           # (vsi,isp-pico) + JPEG encoder.\n"
    "preset: e1m-evk\n"
    "cores:\n"
    "  m55_hp:\n"
    "    app: ./src\n"
)


def test_retarget_drops_every_physical_line_of_a_wrapped_trailing_comment():
    """tan-cli#404, defect 1. Dropping a stale trailing comment was a per-LINE
    rewrite, so retargeting the block above onto a Renesas SKU deleted the
    comment's opening clause and left its tail attached to nothing -- a
    RZ/V2N `board.yaml` documenting a pair of Alif Ethos-U55s, an Ethos-U85 and
    a VeriSilicon ISP Pico (`vsi,isp-pico`) that the module does not have. The
    whole block has to go, not just the physical line the `#` opened on."""
    out = retarget_board_yaml_som(WRAPPED_COMMENT_BOARD_YAML, "E1M-V2N101")

    lines = out.split("\n")
    sku_index = next(i for i, line in enumerate(lines) if line.strip().startswith("sku:"))
    assert lines[sku_index] == "  sku: E1M-V2N101"
    # Not one `#` line survives between the rewritten `sku:` and the next key.
    assert lines[sku_index + 1] == "preset: e1m-evk"
    assert [line for line in lines if line.lstrip().startswith("#")] == []
    # Named silicon is the actual harm: assert on the words, not just the `#`.
    for token in ("Alif", "Ethos-U55", "Ethos-U85", "vsi,isp-pico"):
        assert token not in out, out
    # Everything below the `som:` block is untouched.
    assert out.endswith("preset: e1m-evk\ncores:\n  m55_hp:\n    app: ./src\n")


def test_retarget_keeps_a_wrapped_comment_when_the_sku_does_not_change():
    """The no-op stays a byte-exact no-op with a wrapped comment too: nothing
    in that comment is stale when the SKU it names is the one being written."""
    assert (
        retarget_board_yaml_som(WRAPPED_COMMENT_BOARD_YAML, "E1M-AEN801")
        == WRAPPED_COMMENT_BOARD_YAML
    )


def test_retarget_leaves_a_comment_block_that_never_belonged_to_the_sku_line():
    """Only the wrapped continuation of the `sku:` line's OWN inline comment is
    consumed. A `sku:` with no inline comment starts no block, so a following
    comment -- which documents whatever comes next, not the SoM -- survives a
    value change untouched (#404)."""
    out = retarget_board_yaml_som(
        "som:\n  sku: E1M-AEN801\n  # the carrier below, not the SoM\n  rev: a\n",
        "E1M-V2N101",
    )

    assert out == "som:\n  sku: E1M-V2N101\n  # the carrier below, not the SoM\n  rev: a\n"


def test_retarget_preserves_a_crlf_sku_lines_terminator():
    """tan-cli#404, defect 2. `re.search(r"\\s", ...)` matched the `\\r` of a
    CRLF line, so the terminator was consumed as part of the trailing tail and
    discarded with it -- every other line kept its `\\r\\n` and only the
    rewritten `sku:` line lost it. That is the byte-parity contract
    `_read_verbatim` exists to hold, and a Windows alp-sdk checkout with
    `core.autocrlf=true` hits it on every `--from-example` scaffold."""
    original = (
        "som:\r\n"
        "  sku: E1M-AEN801\r\n"
        "preset: e1m-evk\r\n"
        "cores:\r\n"
        "  m55_hp:\r\n"
        "    app: ./src\r\n"
    )

    out = retarget_board_yaml_som(original, "E1M-V2N101")

    # Byte equality of every other line, stated as "exactly one substitution".
    assert out == original.replace("  sku: E1M-AEN801\r\n", "  sku: E1M-V2N101\r\n")
    # And stated again as the property that broke: no line ending changed kind.
    assert out.count("\r\n") == original.count("\r\n")
    assert "\n" not in out.replace("\r\n", "")


def test_retarget_drops_a_crlf_lines_comment_without_eating_its_terminator():
    """The two halves of #404 meeting: a CRLF line whose comment IS stale. The
    comment goes, the `\\r\\n` stays."""
    out = retarget_board_yaml_som(
        "som:\r\n  sku: E1M-AEN801  # Alif Ensemble E8 SoM\r\ncores:\r\n", "E1M-V2N101"
    )

    assert out == "som:\r\n  sku: E1M-V2N101\r\ncores:\r\n"


def test_retarget_handles_a_value_less_sku_key_on_crlf():
    """The `\\r` also defeated the value-less guard: `after_key` was `"\\r"`,
    which `lstrip(" \\t")` does not clear, so control fell through to the
    splice branch with an empty gap and emitted `sku:E1M-V2N101` glued
    together. `tan validate --offline` then refused the result with `som:`
    parsed as the scalar `'sku:E1M-V2N101'`, exit 2 -- one command after the
    one that caused it (#404)."""
    out = retarget_board_yaml_som("som:\r\n  sku:\r\npreset: e1m-evk\r\n", "E1M-V2N101")

    assert out == "som:\r\n  sku: E1M-V2N101\r\npreset: e1m-evk\r\n"
    # The consequence, not just the bytes: it is a mapping again.
    yaml = pytest.importorskip("yaml")
    assert yaml.safe_load(out)["som"] == {"sku": "E1M-V2N101"}


def test_init_from_example_with_a_som_drops_the_whole_wrapped_comment(tmp_path):
    """#404 end to end, over the path that actually reaches customers:
    alp-sdk-vscode's New Project wizard appends `--som <moduleId>` to `init
    --from-example` whenever an example template and a SoM are both chosen
    (`src/ideHub/newProjectFlowPanel.ts`), so picking any of the 12
    wrapped-comment examples with a non-native SoM landed the mis-annotated
    file. Driven as a subprocess against a fake SDK checkout -- the unit test
    above pins the transform, this pins that `tan init` still routes through
    it.

    The example's `board.yaml` is written as BYTES: `Path.write_text` newline-
    translates on Windows, which would silently make the LF fixture CRLF and
    test something else there than here."""
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    example = sdk / "examples" / "aen" / "edgeai-vision-aen"
    (example / "src").mkdir(parents=True)
    (example / "board.yaml").write_bytes(WRAPPED_COMMENT_BOARD_YAML.encode("utf-8"))
    (example / "src" / "main.c").write_bytes(b"int main(void) { return 0; }\n")

    proc = run_tan(
        "init", "--from-example", "aen/edgeai-vision-aen", "--name", "vision",
        "--som", "E1M-V2N101", "--sdk-root", "./sdk", "--format", "json", cwd=tmp_path,
    )

    assert "Traceback" not in proc.stderr, proc.stderr
    env = json.loads(proc.stdout)
    assert proc.returncode == 0, env["issues"]
    board = (tmp_path / "vision" / "board.yaml").read_bytes().decode("utf-8")
    assert "  sku: E1M-V2N101\n" in board
    for token in ("Alif", "Ethos-U55", "Ethos-U85", "vsi,isp-pico"):
        assert token not in board, board


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


# ---------------------------------------------------------------------------
# tan-cli#494 defects 1-4: what `tan init` reads off disk, and what it refuses
# to read. Every one of these shipped as `ok:true` or as an unactionable
# message, which is why each test pins the REPORT as well as the file set.
# ---------------------------------------------------------------------------


def _example(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def test_from_example_leaves_a_built_in_place_example_s_build_tree_behind(tmp_path):
    """**tan-cli#494 defect 1.** `read_example_tree` globbed every regular file,
    so an example someone had run `west build` in came across as the customer's
    new project: `CMakeCache.txt` with absolute host paths, `.ninja_deps`,
    `libapp.a`.

    Measured on the real tree at `examples/v2n/v2n-brd-i2c-bringup` in the
    alp-sdk checkout beside this repo: **613** files on disk, **607** of them
    under `build/`, **6** tracked by git. The first binary among the 607 also
    aborted the whole command with `init.example-unreadable`, so the visible
    symptom was a hard failure and the silent one was a 613-file project.
    """
    source = _example(
        tmp_path / "ex",
        {
            "CMakeLists.txt": "target_sources(app PRIVATE src/main.c)\n",
            "src/main.c": "int main(void) { return 0; }\n",
            "board.yaml": "som:\n  sku: E1M-AEN801\n",
            "build/CMakeCache.txt": "CMAKE_HOME_DIRECTORY:INTERNAL=/srv/alp-sdk\n",
            "build/zephyr/libapp.a": "!<arch>\n",
            "build_debug/x.o": "obj\n",
            "cmake-build-release/y.o": "obj\n",
            "twister-out/handler.log": "log\n",
            "twister-out.1/handler.log": "log\n",
        },
    )

    planned = sorted(f.relative_path for f in read_example_tree(source))

    assert planned == ["CMakeLists.txt", "board.yaml", "src/main.c"]


def test_from_example_keeps_a_hand_written_directory_that_merely_starts_with_build(
    tmp_path,
):
    """The pruning list is EXACTLY alp-sdk's five `.gitignore` patterns
    (`build/`, `build_*/`, `cmake-build-*/`, `twister-out/`, `twister-out.*/`)
    and nothing more. An earlier cut of this fix also pruned `build-`, which
    appears in no pattern there -- an invented rule that silently drops a
    hand-written `build-utils/` from the customer's project, which is the same
    damage as copying the build tree in, just in the other direction.
    """
    source = _example(
        tmp_path / "ex",
        {
            "CMakeLists.txt": "target_sources(app PRIVATE src/main.c)\n",
            "src/main.c": "int main(void) { return 0; }\n",
            "build-utils/gen.py": "print('hi')\n",
            "buildings.md": "not a build dir\n",
        },
    )

    planned = sorted(f.relative_path for f in read_example_tree(source))

    assert planned == [
        "CMakeLists.txt",
        "build-utils/gen.py",
        "buildings.md",
        "src/main.c",
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_from_example_does_not_inline_a_symlink_pointing_outside_the_example(tmp_path):
    """Skipping symlinks matches the oracle's `DirEntry::file_type()`, whose own
    doc comment says non-regular entries are skipped. `Path.is_file()` FOLLOWS a
    symlinked file, so a link out of the example would have copied that outside
    file's CONTENT into the customer's new project under an innocuous name --
    here a private key, which is the version of this that matters."""
    secret = tmp_path / "outside" / "id_ed25519"
    secret.parent.mkdir(parents=True)
    secret.write_text("PRIVATE KEY MATERIAL\n", encoding="utf-8")
    source = _example(
        tmp_path / "ex",
        {
            "CMakeLists.txt": "target_sources(app PRIVATE src/main.c)\n",
            "src/main.c": "int main(void) { return 0; }\n",
        },
    )
    (source / "keys.txt").symlink_to(secret)
    (source / "dangling.txt").symlink_to(tmp_path / "nope")

    planned = read_example_tree(source)

    assert sorted(f.relative_path for f in planned) == ["CMakeLists.txt", "src/main.c"]
    assert all("PRIVATE KEY MATERIAL" not in f.content for f in planned)


def test_a_vendored_tree_keeps_its_own_sku_s_cores_byte_for_byte():
    """**tan-cli#494 defect 2's** no-op half, first because it is what protects
    every existing fixture: the two representative SKUs are the trees' own, so
    `retarget_board_yaml_cores` must be byte-exact passthrough for them. The
    `os: "off"` companion cluster each tree declares (`a32_cluster` on the E8,
    `a55_cluster` on the V2N) is REAL for that SKU and must survive."""
    aen = _board_yaml_of("edge-ai", "edge-ai-starter", "E1M-AEN801")
    v2n = _board_yaml_of("edge-ai", "edge-ai-starter", "E1M-V2N101")

    assert "a32_cluster:" in aen and "m55_hp:" in aen
    assert "a55_cluster:" in v2n and "m33_sm:" in v2n


@pytest.mark.parametrize(
    ("sku", "app_core", "dropped"),
    [
        ("E1M-AEN301", "m55_hp", "a32_cluster"),
        # `E1M-NX9101` used to be a second row here. tan-cli#579 refuses it at
        # `_vendored_files` -- an NXP SoM no longer reaches the Alif tree at all
        # -- so this end-to-end shape cannot cover it any more. The transform
        # itself is still exercised for that SKU, directly, in
        # `test_cores_retargeting_still_handles_a_family_with_no_tree` below;
        # dropping the row without moving the coverage would have quietly
        # retired half of tan-cli#494 defect 2's regression guard.
    ],
)
def test_a_vendored_tree_re_derives_cores_for_a_sku_that_is_not_its_own(
    sku, app_core, dropped
):
    """**tan-cli#494 defect 2.** `tan init` picks a vendored tree by FAMILY and
    used to retarget only the `som: sku:` line, so every SKU but the two
    representative ones inherited that tree's core ids verbatim.

    `--template edge-ai-starter --som E1M-AEN301` wrote `a32_cluster` for an
    Ensemble E3 that has no Cortex-A32 -- `ok:true`, `exitCode 0`, `issues:[]`
    -- and `tan validate` then hard-errored `unknown core id ['a32_cluster']`
    on the very next command. `--som E1M-NX9101` landed on the Alif tree and
    kept `m55_hp` against a topology of `a55_cluster`/`m33`, contradicting
    `app_core_for_sku` in this same module. (That second SKU is now refused
    outright -- tan-cli#579 -- because fixing its CORE id left the rest of the
    Alif tree in place and only made the artefact look more plausible.)

    Both edits only ever REMOVE wrong facts: the app entry is renamed to tan's
    own `app_core_for_sku`, and the companion cluster -- true only of the
    tree's own SKU -- is dropped, since a core absent from `cores:` is simply
    not built.
    """
    content = _board_yaml_of("edge-ai", "edge-ai-starter", sku)

    assert f"  {app_core}:" in content
    assert app_core_for_sku(sku) == app_core
    assert dropped not in content
    # The app entry keeps its body -- this is a rename, not a rebuild.
    assert "app: ./src" in content
    assert "default_arena_kib: 64" in content
    # And the SoM line was retargeted too, as it always was.
    assert f"sku: {sku}" in content


def test_cores_retargeting_still_handles_a_family_with_no_tree():
    """tan-cli#494 defect 2's NXP coverage, moved off the `_vendored_files`
    path that tan-cli#579 now refuses. The TRANSFORM is unchanged and still
    correct for an NXP SKU -- it is reached through `--board-yaml` and
    `--from-example`, neither of which goes near `_family_bucket` -- so the
    refusal must not be read as retiring it."""
    content = _board_yaml_of("edge-ai", "edge-ai-starter", "E1M-AEN801")

    retargeted = retarget_board_yaml_cores(content, "E1M-NX9101", "E1M-AEN801")

    assert "  m33:" in retargeted
    assert "  m55_hp:" not in retargeted
    assert "  a32_cluster:" not in retargeted
    assert "app: ./src" in retargeted
    # Not asserted as absent, deliberately: this tree's `libraries:` entry is
    # core-SCOPED (`cores: [m55_hp]`) and `retarget_board_yaml_cores` only
    # rewrites the `cores:` block, so that scope still names a core the
    # retargeted file no longer declares. Out of tan-cli#579's scope -- the
    # vendored path that produced it is now refused outright, and the residue
    # is only reachable via `--from-example`/`--board-yaml`, which are
    # tan-cli#494 defect 2's territory, not this fix's.
    assert "cores: [m55_hp]" in retargeted


def test_cores_retargeting_leaves_an_unrecognised_block_alone():
    """A block with no single unambiguous `app:` entry is left verbatim rather
    than half-rewritten -- the transform never guesses. Same rule as
    `retarget_board_yaml_som`'s own untouched-on-no-match behaviour."""
    two_apps = "cores:\n  m55_hp:\n    app: ./a\n  m33:\n    app: ./b\n\nsom:\n  sku: X\n"
    assert retarget_board_yaml_cores(two_apps, "E1M-NX9101", "E1M-AEN801") == two_apps

    no_block = "som:\n  sku: E1M-AEN801\n"
    assert retarget_board_yaml_cores(no_block, "E1M-NX9101", "E1M-AEN801") == no_block


def test_a_partially_delivered_vendored_tree_is_refused_not_half_written(tmp_path, monkeypatch):
    """**tan-cli#494 defect 3.** The emptiness guard was all-or-nothing -- it
    fired only at ZERO files, so any non-empty SUBSET read as a complete tree.

    With `src/main.c` missing (a mis-scoped PyInstaller `--add-data`, a partial
    copy, an extracted onedir whose `src/` lost `+x`), `tan init` wrote the
    other files, reported `ok:true` / `issues:[]`, and the `CMakeLists.txt` it
    DID write still said `target_sources(app PRIVATE src/main.c)`. The
    customer's first `tan build` died inside CMake with "Cannot find source
    file", reported to them as their project's problem.

    The expected set is DERIVED from the tree's own `CMakeLists.txt`, so it
    tracks a template that gains a source file with no edit here.
    """
    tree = tmp_path / "vendored" / "edge-ai" / "E1M-AEN801"
    tree.mkdir(parents=True)
    (tree / "CMakeLists.txt").write_text(
        "target_sources(app PRIVATE\n  src/main.c\n  src/extra.c\n)\n", encoding="utf-8"
    )
    (tree / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    (tree / "prj.conf").write_text("CONFIG_LOG=y\n", encoding="utf-8")
    monkeypatch.setattr(scaffold_module, "VENDORED_ROOT", tmp_path / "vendored")

    with pytest.raises(TemplateDataError) as excinfo:
        scaffold_module._vendored_files("edge-ai", "edge-ai-starter", "E1M-AEN801")

    message = str(excinfo.value)
    # Names BOTH missing sources, and says whose problem it is.
    assert "src/extra.c" in message and "src/main.c" in message
    assert "broken tan installation, not a project problem" in message


def test_a_complete_vendored_tree_passes_the_completeness_check(tmp_path, monkeypatch):
    """Defect 3's control: the check must not fire on a whole tree. Pins that
    the derived expectation really is satisfiable -- a regex that matched
    nothing, or one that swept up a `${VAR}`, would make every scaffold
    unbuildable, which is a far worse failure than the one being fixed."""
    tree = tmp_path / "vendored" / "edge-ai" / "E1M-AEN801"
    (tree / "src").mkdir(parents=True)
    (tree / "CMakeLists.txt").write_text(
        "target_sources(app PRIVATE\n  src/main.c\n  ${EXTRA_SOURCES}\n)\n", encoding="utf-8"
    )
    (tree / "board.yaml").write_text("som:\n  sku: E1M-AEN801\n", encoding="utf-8")
    (tree / "prj.conf").write_text("CONFIG_LOG=y\n", encoding="utf-8")
    (tree / "src" / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
    monkeypatch.setattr(scaffold_module, "VENDORED_ROOT", tmp_path / "vendored")

    planned = scaffold_module._vendored_files("edge-ai", "edge-ai-starter", "E1M-AEN801")

    assert sorted(f.relative_path for f in planned) == [
        "CMakeLists.txt",
        "board.yaml",
        "prj.conf",
        "src/main.c",
    ]


def test_an_undecodable_example_file_is_named_in_the_error(tmp_path):
    """**tan-cli#494 defect 4.** `_read_verbatim` re-raised a bare
    `UnicodeDecodeError`, whose `str()` is only `'utf-8' codec can't decode
    byte 0xff in position 88: invalid start byte` -- no path. The caller wraps
    that verbatim into `init.example-unreadable`, so a customer with a stray
    binary in a large example tree got a byte offset and nothing to act on.
    Python's own `open()` names the file in every OSError it raises, so only
    the codec branch -- the one a binary actually trips -- was silent.
    """
    source = _example(
        tmp_path / "ex", {"CMakeLists.txt": "target_sources(app PRIVATE src/main.c)\n"}
    )
    (source / "blob.bin").write_bytes(b"\xff\xfe\x00\x01binary")

    with pytest.raises(Exception) as excinfo:
        read_example_tree(source)

    message = str(excinfo.value)
    assert "blob.bin" in message
    # The codec detail is kept, not replaced -- it says WHY the file is not text.
    assert "utf-8" in message


def _board_yaml_of(tree: str, template_id: str, sku: str) -> str:
    """The `board.yaml` a real vendored-tree plan produces for `sku` -- read
    through `_vendored_files` so the test exercises the same retargeting chain
    `tan init` does, rather than calling the transform in isolation."""
    files = scaffold_module._vendored_files(tree, template_id, sku)
    return next(f.content for f in files if f.relative_path == "board.yaml")


# ---------------------------------------------------------------------------
# tan-cli#579 -- an NXP SKU used to render the Alif tree's content
# ---------------------------------------------------------------------------


def test_a_som_family_with_no_vendored_tree_is_refused_not_rendered():
    """**tan-cli#579.** `_family_bucket` was
    `_FAMILY_TREES[1] if sku.startswith(("E1M-V2N","E1M-V2M")) else _FAMILY_TREES[0]`,
    so E1M-NX9* -- a family `app_core_for_sku` in this same module already
    knows (`E1M-NX9` -> `m33`) -- fell down the `else` arm onto the Alif tree.
    Measured on `dev` before this fix: `plan_template_files("sensor-starter",
    "E1M-NX9101")` returned the E1M-AEN801 tree with every file except
    `board.yaml` BYTE-IDENTICAL to the Alif render -- `preset: e1m-evk`,
    `chips: [tmp112]`, a README whose build line is `west build -b
    alp_e1m_aen801_m55_hp/ae822fa0e5597ls0/rtss_hp .`, and a `CMakeLists.txt`
    that asks the SDK loader for `--emit zephyr-conf --core m55_hp` while
    tan-cli#494's own `retarget_board_yaml_cores` had already rewritten the
    same scaffold's `cores:` key to `m33`. `tan init` reported `ok: true` /
    exit 0 / `issues: []` for all of it.
    """
    for template_id in ("zephyr-app", "sensor-starter", "edge-ai-starter", "board-diagnostics"):
        with pytest.raises(UnsupportedSomError) as excinfo:
            plan_template_files(template_id, "E1M-NX9101")
        assert "E1M-NX9101" in str(excinfo.value)
        assert template_id in str(excinfo.value)


def test_the_refusal_names_the_two_paths_that_still_work():
    """A refusal that leaves the customer with nothing is a worse defect than
    the one it fixes. `minimal-app` is tan's OWN hand-generated, vendor-neutral
    template (no vendored tree, so `_family_bucket` never runs for it) and
    `--from-example` copies a real SDK example -- both are named."""
    with pytest.raises(UnsupportedSomError) as excinfo:
        plan_template_files("sensor-starter", "E1M-NX9101")

    message = str(excinfo.value)
    assert "minimal-app" in message
    assert "--from-example" in message


def test_minimal_app_still_renders_for_a_family_with_no_vendored_tree():
    """The refusal is scoped to the VENDORED trees. `minimal-app` is
    hand-generated here and carries no vendor content at all, so it is correct
    for an NXP SoM -- and it is the escape hatch the refusal names."""
    files = {f.relative_path: f.content for f in plan_template_files("minimal-app", "E1M-NX9101")}

    assert "sku: E1M-NX9101" in files["board.yaml"]
    assert "  m33:\n" in files["board.yaml"]
    assert "m55_hp" not in files["board.yaml"]


def test_the_two_family_derivations_read_one_table():
    """The invariant this module's docstring asserts -- "the two derivations
    can never disagree" -- restored by CONSTRUCTION rather than by two
    hand-synced prefix tests. Every family `app_core_for_sku` recognises
    resolves to a tree, or to an explicit refusal; none of them can fall
    through to Alif by accident again."""
    for sku, core, tree in (
        ("E1M-AEN801", "m55_hp", "E1M-AEN801"),
        ("E1M-AEN301", "m55_hp", "E1M-AEN801"),
        ("E1M-V2N101", "m33_sm", "E1M-V2N101"),
        ("E1M-V2N102", "m33_sm", "E1M-V2N101"),
        ("E1M-V2M101", "m33_sm", "E1M-V2N101"),
        ("E1M-NX9101", "m33", None),
    ):
        assert app_core_for_sku(sku) == core, sku
        assert scaffold_module._family_bucket(sku) == tree, sku


def test_an_unrecognised_prefix_still_takes_the_alif_default_in_both_derivations():
    """Deliberately NOT refused. `tan init` is SDK-free and cannot tell a
    future SKU from a typo, so an unrecognised prefix keeps the pre-existing
    Alif fallback -- and takes it in BOTH derivations at once, which is what
    stops an unknown SKU getting a `board.yaml` whose core contradicts its own
    `CMakeLists.txt`. `tan validate` re-checks the guess once an SDK
    resolves."""
    assert app_core_for_sku("E1M-ZZZ999") == "m55_hp"
    assert scaffold_module._family_bucket("E1M-ZZZ999") == "E1M-AEN801"
    planned = plan_template_files("sensor-starter", "E1M-ZZZ999")
    assert any(f.relative_path == "board.yaml" for f in planned)
