# SPDX-License-Identifier: Apache-2.0
"""Scaffold planning: the vendored-tree byte parity gate, and the two string
transforms that decide what lands in a customer's `board.yaml`.

The load-bearing case is ``test_the_vendored_tree_is_byte_identical_to_the_rust_one``.
`tan/templates/vendored/` is a SECOND copy of `crates/tan-core/src/wizard/
vendored/` -- the Rust bakes its copy in with `include_str!`, this port reads
its own -- and the upstream parity script (`tests/parity/
scaffold_byte_parity.py`) only ever compares the RUST copy to the SDK. So a
re-vendor that refreshes one language and not the other is invisible to every
other gate in either repo: two `tan` binaries, same version, scaffolding
different bytes. This is the only thing that catches it.
"""
from pathlib import Path

import pytest

from tan.core.scaffold import (
    DEFAULT_SOM_SKU,
    TEMPLATE_IDS,
    app_core_for_sku,
    is_plain_relative,
    plan_template_files,
    retarget_board_yaml_som,
    scaffold_tree_preview,
)
from tan.templates import VENDORED_ROOT

#: The Rust crate's copy of the same trees. Read-only, and OUTSIDE `python/` --
#: which is the point: this test exists to compare the two languages.
RUST_VENDORED_ROOT = (
    Path(__file__).resolve().parents[3] / "crates" / "tan-core" / "src" / "wizard" / "vendored"
)


def files_under(root: Path):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.skipif(
    not RUST_VENDORED_ROOT.is_dir(),
    reason="Rust crate tree absent (a python-only distribution); nothing to diff against",
)
def test_the_vendored_tree_is_byte_identical_to_the_rust_one():
    ours = files_under(VENDORED_ROOT)
    theirs = files_under(RUST_VENDORED_ROOT)

    assert sorted(ours) == sorted(theirs), (
        "the two vendored trees no longer hold the same files -- re-vendor BOTH "
        "by re-running the SDK emit (see tan/templates/vendored/MANIFEST.md), "
        "never by hand-editing one"
    )
    drifted = [name for name, content in ours.items() if theirs[name] != content]
    assert drifted == [], f"vendored content drifted between Rust and Python: {drifted}"


def test_the_vendored_tree_is_lf_only():
    """These bytes are written to the customer's files verbatim and byte-compared
    above. A Windows checkout with `autocrlf=true` and no `.gitattributes` entry
    for this path rewrites every one of them."""
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


def test_retarget_replaces_only_the_value_token():
    out = retarget_board_yaml_som(
        "som:\n  sku: E1M-AEN801           # aligned\ncores:\n", "E1M-V2N101"
    )

    assert out == "som:\n  sku: E1M-V2N101           # aligned\ncores:\n"


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


def test_tree_preview_marks_the_last_entry():
    files = plan_template_files("minimal-app", DEFAULT_SOM_SKU)
    preview = scaffold_tree_preview(files).splitlines()

    assert preview[0] == "."
    assert preview[1] == "|-- CMakeLists.txt"
    assert preview[-1].startswith("`-- ")
