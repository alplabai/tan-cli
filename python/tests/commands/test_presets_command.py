# SPDX-License-Identifier: Apache-2.0
"""`tan presets` -- the oracle's own unit tests, ported, plus the two things the
goldens cannot reach.

Named twins live in `crates/tan-cli/src/commands/presets.rs`
(`read_board_libraries_*`), `crates/tan-core/src/bootstrap/runtime.rs`
(`topology_board_is_zephyr_machine_is_yocto_else_heuristic`) and
`crates/tan-core/src/sdk_catalogue/parse.rs`.

The goldens (`presets-heterogeneous-som`, `presets-no-sdk`) pin the wire format;
what they cannot reach is (a) the text renderer, since both run `--format json`,
and (b) the **no-PyYAML** reader, since the test host has PyYAML and the frozen
binary does not (`scripts/build_binary.sh` installs `typer rich pyinstaller`
only). (b) is the one that would ship broken silently: `cores` is what the New
Project wizard scaffolds IPC from, so the fallback is held to reading the SAME
cores PyYAML does, on the real preset shape.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tan.cli import app
from tan.commands.presets_cmd import (
    SomShapeError,
    infer_runtime_for_core_id,
    parse_som_preset,
    read_board_libraries,
    read_soms,
    render_presets_text,
    resolve_project_paths,
    resolve_sdk,
    runtime_for_core,
    scan_som_preset,
)

runner = CliRunner()

#: A heterogeneous preset in the real shipped shape: interleaved comments, a
#: quoted display name, a trailing `# comment` on a value, an `a55` Yocto cluster
#: next to an `m33` Zephyr one, and a third core that declares neither key so the
#: core-id heuristic has to decide.
HETEROGENEOUS = """\
# A comment before anything.
schema_version: 1

sku: E1M-V2N101
family: renesas-rzv2n
display_name: "E1M-V2N101 (Renesas RZ/V2N)"

topology:
  a55_cluster:
    app: alp-image-edge
    machine: e1m-v2n101-a55           # Yocto MACHINE
    toolchain: poky-glibc
  m33_sm:
    # A comment inside a core.
    board: alp_e1m_v2n101_m33_sm/r9a09g056n48gbg/cm33
    toolchain: arm-zephyr-eabi
  a32_extra:
    app: alp-stock-shim

status:
  preliminary: false
"""


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def som_yaml(sku, *, topology="  m33: {}\n", extra=""):
    return f"schema_version: 1\nsku: {sku}\n{extra}topology:\n{topology}"


# --------------------------------------------------------------------------
# The derived runtime -- the invariant that the customer never picks an OS
# --------------------------------------------------------------------------


def test_runtime_comes_from_the_topology_then_the_core_id():
    assert runtime_for_core("m33_sm", board=True, machine=False) == "zephyr"
    assert runtime_for_core("a55_cluster", board=False, machine=True) == "yocto"
    # `board` wins when a preset declares both, matching the oracle's match arms.
    assert runtime_for_core("m33_sm", board=True, machine=True) == "zephyr"
    # Neither key -> the shared core-id heuristic.
    assert runtime_for_core("a32_cluster", board=False, machine=False) == "yocto"
    assert runtime_for_core("m55_he", board=False, machine=False) == "zephyr"


def test_core_id_heuristic_is_word_anchored():
    assert infer_runtime_for_core_id("a55_cluster") == "yocto"
    assert infer_runtime_for_core_id("cluster_a32") == "yocto"
    assert infer_runtime_for_core_id("A55") == "yocto"
    # `a` not at a word start, and `a` not followed by a digit: both Cortex-M.
    assert infer_runtime_for_core_id("data55") == "zephyr"
    assert infer_runtime_for_core_id("app_core") == "zephyr"
    assert infer_runtime_for_core_id("") == "zephyr"


# --------------------------------------------------------------------------
# Preset parsing
# --------------------------------------------------------------------------


def test_heterogeneous_preset_reports_two_runtimes_on_one_som():
    som = parse_som_preset(HETEROGENEOUS)
    assert som.sku == "E1M-V2N101"
    assert som.display_name == "E1M-V2N101 (Renesas RZ/V2N)"
    assert som.family == "renesas-rzv2n"
    # Order is the YAML's, not sorted: the wizard maps cores positionally.
    assert [(c.id, c.os) for c in som.cores] == [
        ("a55_cluster", "yocto"),
        ("m33_sm", "zephyr"),
        ("a32_extra", "yocto"),
    ]


def test_the_no_pyyaml_reader_agrees_with_pyyaml_on_the_real_shape():
    """The fallback is THE path on the shipped binary; a divergence here means a
    heterogeneous SoM scaffolds single-core with no IPC for every customer who
    installed the release artifact rather than the wheel."""
    scanned = scan_som_preset(HETEROGENEOUS)
    assert scanned["schema_version"] == 1
    assert scanned["sku"] == "E1M-V2N101"
    assert scanned["display_name"] == "E1M-V2N101 (Renesas RZ/V2N)"
    assert scanned["family"] == "renesas-rzv2n"
    assert [(c.id, c.os) for c in scanned["cores"]] == [
        ("a55_cluster", "yocto"),
        ("m33_sm", "zephyr"),
        ("a32_extra", "yocto"),
    ]


def test_the_no_pyyaml_reader_handles_the_flow_topology_form():
    # The form the oracle's own parse tests use.
    scanned = scan_som_preset(
        "schema_version: 1\nsku: E1M-X\ntopology:\n"
        "  a55: { app: ./src, machine: m }\n"
        "  m33: { board: b }\n"
        "  m55: { app: ./src }\n"
        # A key that merely CONTAINS `board` must not count as `board:`.
        "  a99: { zephyr_board: b }\n"
    )
    assert [(c.id, c.os) for c in scanned["cores"]] == [
        ("a55", "yocto"),
        ("m33", "zephyr"),
        ("m55", "zephyr"),
        ("a99", "yocto"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "sku: E1M-X\n",  # no schema_version at all
        "schema_version: 2\nsku: E1M-X\n",  # a version this CLI does not consume
        'schema_version: "1"\nsku: E1M-X\n',  # a string is not an integer
        "schema_version: true\nsku: E1M-X\n",  # nor is a bool, despite True == 1
        "- not a mapping\n",
        "just a scalar\n",
        "schema_version: 1\n  bad: [indent\n",  # not YAML at all
    ],
)
def test_a_preset_this_cli_cannot_consume_raises_for_the_caller_to_skip(text):
    with pytest.raises(SomShapeError):
        parse_som_preset(text)


def test_display_name_falls_back_to_sku_and_tbd_is_absent():
    assert parse_som_preset(som_yaml("E1M-X")).display_name == "E1M-X"
    som = parse_som_preset(som_yaml("E1M-X", extra="display_name: TBD\nfamily: TBD\n"))
    assert som.display_name == "E1M-X"
    assert som.family == ""


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_read_soms_supports_both_layouts_skips_the_rest_and_sorts(tmp_path):
    modules = tmp_path / "metadata" / "e1m_modules"
    write(modules / "E1M-V2N101" / "som.yaml", som_yaml("E1M-V2N101"))
    write(modules / "E1M-AEN801.yaml", som_yaml("E1M-AEN801"))
    # Skipped: not `E1M-*`; an `E1M-*` dir with no som.yaml; a non-yaml file; a
    # preset whose schema_version this CLI does not consume.
    write(modules / "OTHER.yaml", som_yaml("OTHER"))
    (modules / "E1M-EMPTY").mkdir()
    write(modules / "E1M-NOTES.txt", "not yaml")
    write(modules / "E1M-V2.yaml", "schema_version: 2\nsku: E1M-V2\n")

    assert [s.sku for s in read_soms(str(tmp_path))] == ["E1M-AEN801", "E1M-V2N101"]


def test_read_soms_skips_a_preset_that_is_not_utf8(tmp_path):
    modules = tmp_path / "metadata" / "e1m_modules"
    write(modules / "E1M-GOOD.yaml", som_yaml("E1M-GOOD"))
    (modules / "E1M-BAD.yaml").write_bytes(b"schema_version: 1\nsku: \xff\xfe\n")
    # An undecodable byte is a skipped entry, never a traceback.
    assert [s.sku for s in read_soms(str(tmp_path))] == ["E1M-GOOD"]


def test_read_soms_is_empty_when_the_tree_is_missing(tmp_path):
    assert read_soms(str(tmp_path / "no-such-sdk")) == []


def test_read_board_libraries_lists_yaml_stems_sorted_and_skips_readme(tmp_path):
    libraries = tmp_path / "metadata" / "libraries"
    for name in ("lvgl.yaml", "aws-iot.yaml", "README.md", "readme.yaml", "notes.txt"):
        write(libraries / name, "x")
    assert read_board_libraries(str(tmp_path)) == ["aws-iot", "lvgl"]


def test_read_board_libraries_empty_when_dir_missing(tmp_path):
    assert read_board_libraries(str(tmp_path / "no-such-sdk")) == []


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_project_paths_are_absolute_posix_and_the_board_path_hangs_off_the_root(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    root, board = resolve_project_paths(None, None)
    assert root == str(tmp_path).replace("\\", "/")
    assert board == f"{root}/board.yaml"
    # `--project` joins under the CWD; `.`/`..` are normalised out of the ROOT.
    assert resolve_project_paths("sub/../sub", None)[0] == f"{root}/sub"
    # ...but NOT out of the board path, matching `resolve_board_yaml_path`.
    assert resolve_project_paths(None, "custom/foo.yaml")[1] == f"{root}/custom/foo.yaml"
    # An absolute `--board-yaml` is reported as given.
    absolute = str(tmp_path / "elsewhere.yaml")
    assert resolve_project_paths(None, absolute)[1] == absolute.replace("\\", "/")


def test_an_invalid_sdk_root_flag_resolves_to_nothing_rather_than_a_lower_tier(tmp_path):
    # I-31: `--sdk-root` is terminal. A typo must not silently report whatever
    # else happens to be resolvable.
    assert resolve_sdk(str(tmp_path / "nope"), str(tmp_path)) is None


def test_a_valid_sdk_root_flag_keeps_the_path_as_typed(tmp_path, monkeypatch):
    write(tmp_path / "sdk" / "scripts" / "alp_project.py", "x")
    monkeypatch.chdir(tmp_path)
    # `./sdk`, not `sdk`: a `Path` round-trip drops the `./` the user typed and
    # `sdk.root`/`data.sdkRoot` are both on the wire.
    resolved = resolve_sdk("./sdk", str(tmp_path))
    assert (resolved.path, resolved.tier, resolved.broken_project_pin) == (
        "./sdk", "sdkRootFlag", None,
    )


# --------------------------------------------------------------------------
# Text rendering (both goldens run `--format json`)
# --------------------------------------------------------------------------


def test_text_lists_skus_with_display_names_and_verbose_adds_family_cores():
    """Was count-only-then-bare-skus (tan-cli#164-adjacent regression): three
    integers and a sku list answer no question a user has. Superseded by
    `render_presets_text` taking `Som` and printing the display name by
    default, family/cores under `--verbose`."""
    from tan.commands.presets_cmd import Som, SomCore

    soms = [
        Som(
            sku="E1M-A",
            display_name="Alpha",
            family="fam-a",
            cores=(SomCore(id="c1", os="zephyr"),),
        ),
        Som(
            sku="E1M-B",
            display_name="Bravo",
            family="fam-b",
            cores=(SomCore(id="c2", os="yocto"),),
        ),
    ]
    lines = render_presets_text(soms, ["lvgl"], False)
    assert lines[0] == "presets: skus=2 libraries=8 boardLibraries=1"
    assert any("E1M-A" in line and "Alpha" in line for line in lines)
    assert any("E1M-B" in line and "Bravo" in line for line in lines)

    verbose = render_presets_text(soms[:1], [], True)
    assert verbose[0] == "presets: skus=1 libraries=8 boardLibraries=0"
    assert any("fam-a" in line for line in verbose)
    assert any("c1" in line and "zephyr" in line for line in verbose)


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def test_json_reports_the_som_and_stdout_carries_nothing_else(tmp_path, monkeypatch):
    sdk = tmp_path / "sdk"
    write(sdk / "scripts" / "alp_project.py", "x")
    write(sdk / "metadata" / "e1m_modules" / "E1M-V2N101" / "som.yaml", HETEROGENEOUS)
    write(sdk / "metadata" / "libraries" / "lvgl.yaml", "x")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["presets", "--sdk-root", "./sdk", "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["command"] == "presets"
    assert doc["ok"] is True
    assert doc["sdk"] == {"root": "./sdk", "sourceTier": "sdkRootFlag"}
    assert doc["issues"] == []
    assert doc["data"]["sdkRoot"] == "./sdk"
    assert doc["data"]["skus"] == ["E1M-V2N101"]
    assert doc["data"]["boardLibraries"] == ["lvgl"]
    assert doc["data"]["soms"][0]["cores"] == [
        {"id": "a55_cluster", "os": "yocto"},
        {"id": "m33_sm", "os": "zephyr"},
        {"id": "a32_extra", "os": "yocto"},
    ]
    # `osChoices` is a vocabulary, never a per-SoM menu -- nothing in `soms`
    # offers the customer an OS to pick.
    assert doc["data"]["osChoices"] == ["zephyr", "yocto", "baremetal"]


def test_an_unresolved_sdk_is_a_warning_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["presets", "--sdk-root", "./nope", "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    # Absent, not null (`sdk` is omitted when nothing resolved).
    assert "sdk" not in doc
    assert doc["data"]["sdkRoot"] is None
    assert doc["data"]["soms"] == []
    assert doc["data"]["boardLibraries"] == []
    # The FROZEN code, spelled exactly; the wizard matches it with `===`.
    assert doc["issues"] == [
        {
            "code": "presets.sdk-root-unresolved",
            "severity": "warning",
            "message": (
                "alp-sdk root is unresolved. Returning built-in defaults and "
                "empty SDK preset lists."
            ),
        }
    ]
    # Built-in defaults still answer without a checkout -- that is the point.
    assert len(doc["data"]["libraries"]) == 8


def test_text_mode_writes_nothing_to_stdout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["presets", "--sdk-root", "./nope"])
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "presets: skus=0 libraries=8 boardLibraries=0" in result.stderr


def test_a_bad_format_is_a_usage_error_not_a_traceback():
    result = runner.invoke(app, ["presets", "--format", "yaml"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_presets_text_lists_skus_with_display_names_by_default():
    """Three integers answer no question a user has. The SoM entries carry a
    display name already; the default output should show it."""
    from tan.commands.presets_cmd import Som, SomCore, render_presets_text

    soms = [
        Som(
            sku="E1M-AEN301",
            display_name="E1M-AEN301 (Alif Ensemble E3)",
            family="alif-ensemble",
            cores=(SomCore(id="m55_hp", os="zephyr"), SomCore(id="m55_he", os="zephyr")),
        )
    ]
    lines = render_presets_text(soms, ["lib-a"], verbose=False)

    assert lines[0] == "presets: skus=1 libraries=8 boardLibraries=1"
    assert any("E1M-AEN301" in line and "Alif Ensemble E3" in line for line in lines)
    # family/cores are the --verbose tier, not the default
    assert not any("m55_hp" in line for line in lines)


def test_presets_text_verbose_adds_family_and_cores():
    from tan.commands.presets_cmd import Som, SomCore, render_presets_text

    soms = [
        Som(
            sku="E1M-AEN301",
            display_name="E1M-AEN301 (Alif Ensemble E3)",
            family="alif-ensemble",
            cores=(SomCore(id="m55_hp", os="zephyr"),),
        )
    ]
    lines = render_presets_text(soms, [], verbose=True)

    assert any("alif-ensemble" in line for line in lines)
    assert any("m55_hp" in line and "zephyr" in line for line in lines)
