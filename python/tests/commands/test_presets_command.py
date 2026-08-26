# SPDX-License-Identifier: Apache-2.0
"""`tan presets` -- the oracle's own unit tests, ported, plus the two things the
goldens cannot reach.

Named twins live in `crates/tan-cli/src/commands/presets.rs`
(`read_board_libraries_*`), `crates/tan-core/src/bootstrap/runtime.rs`
(`topology_board_is_zephyr_machine_is_yocto_else_heuristic`) and
`crates/tan-core/src/sdk_catalogue/parse.rs`.

The goldens (`presets-heterogeneous-som`, `presets-no-sdk`) pin the wire format;
what they cannot reach is (a) the text renderer, since both run `--format json`,
and (b) the **no-PyYAML** reader, since the test host has PyYAML. The frozen
binary has it too -- `pyyaml>=6` is a base entry in `pyproject.toml`
`[project].dependencies` and `scripts/build_binary.sh` installs
`-e ".[monitor]"`; this docstring previously claimed that script installed
`typer rich pyinstaller` only, which has not been true since `pyyaml` entered
`pyproject.toml` (tan-cli#574). (b) is reachable via a `--no-deps` install or a
broken venv, and it is still the one that would ship broken silently: `cores`
is what the New Project wizard scaffolds IPC from, so the fallback is held to
reading the SAME cores PyYAML does, on the real preset shape.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tan.cli import app
from tan.commands.presets_cmd import (
    SDK_UNRESOLVED_MESSAGE,
    SomCore,
    SomShapeError,
    _soc_lookups,
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
from tests.conftest import sdk_root

runner = CliRunner()

#: The real alp-sdk checkout this file's `test_soc_lookups_*` tests bind
#: `tan.planner` against, when one is available (`ALP_SDK_PARITY_ROOT` /
#: `ALP_SDK_ROOT` -- `tests.conftest.sdk_root`'s own precedence). Module-level
#: per that helper's own contract: `_scrub_sdk_discovery_env` deletes
#: `ALP_SDK_ROOT` from the environment before every test function runs.
SDK = sdk_root()

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
# `cores[].type` / `cores[].allowedOs` (tan-cli#870)
# --------------------------------------------------------------------------
#
# `parse_som_preset` takes the two SoC-derived lookups as plain injected
# callables -- these tests exercise that plumbing directly, with no SDK
# checkout and no `tan.planner` binding involved, so they run everywhere
# (bound and unbound alike). `test_soc_lookups_*` below is the complementary
# proof that the REAL lookups (bound to a checkout) compute the same values
# `tan.planner.topology._allowed_os_for_core` does -- i.e. that this file
# reuses that rule rather than re-deriving it.


def test_som_core_as_dict_carries_type_and_allowed_os():
    core = SomCore("m55_hp", "zephyr", "cortex-m55", ("zephyr", "baremetal", "off"))
    assert core.as_dict() == {
        "id": "m55_hp",
        "os": "zephyr",
        "type": "cortex-m55",
        "allowedOs": ["zephyr", "baremetal", "off"],
    }


def test_som_core_type_and_allowed_os_default_empty():
    # The default a bare `SomCore(id, os)` gets -- what every call site that
    # predates tan-cli#870 (and every no-lookup `parse_som_preset` call) sees.
    core = SomCore("m33", "zephyr")
    assert core.type == ""
    assert core.allowed_os == ()
    assert core.as_dict() == {"id": "m33", "os": "zephyr", "type": "", "allowedOs": []}


def test_parse_som_preset_enriches_cores_via_the_injected_lookups():
    """`parse_som_preset` reads the SoM's own `silicon:` key, hands it to
    `core_type_lookup`, and runs each core's resolved type through
    `allowed_os_lookup` -- proven here with fakes standing in for the real
    SoC-JSON / `_allowed_os_for_core` reads `_soc_lookups` supplies in
    production. Mutation-proven: deleting the `type=`/`allowed_os=` keyword
    arguments from the `replace(...)` call in `parse_som_preset` (leaving the
    raw `id`/`os`-only core in place) turns this RED; restoring them turns it
    GREEN -- verified by hand while writing this test.
    """
    text = (
        "schema_version: 1\nsku: E1M-X\nsilicon: vendor:family:part\n"
        "topology:\n  a32: { machine: m }\n  m55: { board: b }\n"
    )
    seen_silicon = []

    def core_type_lookup(silicon):
        seen_silicon.append(silicon)
        return {"a32": "cortex-a32", "m55": "cortex-m55"}

    def allowed_os_lookup(core_type):
        return {
            "cortex-a32": ["yocto", "baremetal", "off"],
            "cortex-m55": ["zephyr", "baremetal", "off"],
        }[core_type]

    som = parse_som_preset(
        text, core_type_lookup=core_type_lookup, allowed_os_lookup=allowed_os_lookup
    )
    assert seen_silicon == ["vendor:family:part"]
    assert [c.as_dict() for c in som.cores] == [
        {"id": "a32", "os": "yocto", "type": "cortex-a32",
         "allowedOs": ["yocto", "baremetal", "off"]},
        {"id": "m55", "os": "zephyr", "type": "cortex-m55",
         "allowedOs": ["zephyr", "baremetal", "off"]},
    ]


def test_parse_som_preset_defaults_when_no_lookups_are_given():
    # The existing single-argument call shape (this file's own tests above,
    # and every caller that predates #870) must keep working unchanged.
    som = parse_som_preset(HETEROGENEOUS)
    assert [c.type for c in som.cores] == ["", "", ""]
    assert [c.allowed_os for c in som.cores] == [(), (), ()]


def test_parse_som_preset_defaults_type_when_the_core_id_is_unknown_to_the_lookup():
    # A core the SoC JSON does not name (a typo, a topology-only accessory
    # core) gets `type=""`, not a KeyError -- `.get(c.id, "")`, not `[c.id]`.
    text = "schema_version: 1\nsku: E1M-X\nsilicon: v:f:p\ntopology:\n  m33: {}\n"
    som = parse_som_preset(
        text, core_type_lookup=lambda silicon: {}, allowed_os_lookup=lambda t: ["off"]
    )
    assert som.cores[0].type == ""
    assert som.cores[0].allowed_os == ("off",)


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
# `_soc_lookups` (tan-cli#870) -- proves REUSE, not a second copy of the rule
# --------------------------------------------------------------------------
#
# `_soc_lookups` deliberately does NOT import `tan.planner` (see its own
# docstring: routing this through the process-global-bound planner poisoned
# 292 unrelated `tests/parity/test_planner_emit_parity.py` cases the first
# time this was tried, because `read_soms` -- and so `_soc_lookups` -- is
# called from dozens of independent tests each with their own disposable
# synthetic SDK root). So most of these tests need no gating and no planner
# binding at all -- `test_soc_lookups_resolves_a_synthetic_checkout_end_to_end`
# proves the mechanics with a self-contained fixture, runs unconditionally.
# Only `test_allowed_os_lookup_matches_tan_planner_topology_exactly` binds the
# real planner too, as the STRONGER cross-implementation proof, so IT alone is
# gated on a real checkout and isolates the planner's process-global state.

pytestmark_soc = pytest.mark.skipif(
    SDK is None,
    reason="set ALP_SDK_ROOT/ALP_SDK_PARITY_ROOT to a real alp-sdk checkout",
)


def test_soc_lookups_resolves_a_synthetic_checkout_end_to_end(tmp_path):
    """The full mechanics, self-contained: a `board.schema.json` os enum, a
    SoC JSON with a Cortex-A and a Cortex-M core, and a SoM preset whose
    `silicon:` key names it. No `ALP_SDK_ROOT`, no `tan.planner` -- runs in
    the unbound suite same as bound.

    Mutation-proven: swapping the `c["id"]: c.get("type", "")` dict
    comprehension in `core_type_lookup` for `c["id"]: ""` turns the `type`
    assertions below RED; swapping `allowed_os_lookup`'s
    `[o for o in choices if o not in cross]` for a bare `list(choices)` turns
    the `allowedOs` assertions RED (both classes would see every enum value,
    including the OTHER class's OS). Restoring either turns both GREEN --
    verified by hand while writing this test.
    """
    write(
        tmp_path / "metadata" / "schemas" / "board.schema.json",
        json.dumps({"$defs": {"core_entry": {"properties": {
            "os": {"enum": ["zephyr", "yocto", "baremetal", "off"]}
        }}}}),
    )
    write(
        tmp_path / "metadata" / "socs" / "vendor" / "family" / "part.json",
        json.dumps({"cores": [
            {"id": "a_core", "type": "cortex-a32"},
            {"id": "m_core", "type": "cortex-m33"},
        ]}),
    )

    core_type_lookup, allowed_os_lookup = _soc_lookups(str(tmp_path))
    assert core_type_lookup is not None
    assert allowed_os_lookup is not None

    types = core_type_lookup("vendor:family:part")
    assert types == {"a_core": "cortex-a32", "m_core": "cortex-m33"}
    assert allowed_os_lookup(types["a_core"]) == ["yocto", "baremetal", "off"]
    assert allowed_os_lookup(types["m_core"]) == ["zephyr", "baremetal", "off"]
    # A `silicon:` this registry doesn't resolve (not 3 colon-separated parts,
    # or a real triple with no file on disk) is `{}`, never a raise.
    assert core_type_lookup("not-a-triple") == {}
    assert core_type_lookup("vendor:family:nonexistent") == {}
    assert core_type_lookup(None) == {}
    # Too MANY colon-separated parts is also not-a-triple -- a `!=3` guard
    # covers both directions; a `<3` guard (caught nowhere else in this
    # suite) would let `a:b:c:d` silently resolve to `socs/a/b/c.json`.
    assert core_type_lookup("a:b:c:d") == {}


def test_soc_lookups_degrades_when_the_checkout_has_no_schema(tmp_path):
    """A resolvable SoC JSON but NO `board.schema.json` (a synthetic/partial
    `--sdk-root`, exactly the `presets-heterogeneous-som` golden's shape):
    `type` still resolves, `allowedOs` degrades to `[]` rather than raising."""
    write(
        tmp_path / "metadata" / "socs" / "v" / "f" / "p.json",
        json.dumps({"cores": [{"id": "c1", "type": "cortex-m7"}]}),
    )
    core_type_lookup, allowed_os_lookup = _soc_lookups(str(tmp_path))
    assert core_type_lookup("v:f:p") == {"c1": "cortex-m7"}
    assert allowed_os_lookup("cortex-m7") == []


def test_soc_lookups_is_none_none_when_the_metadata_tree_is_missing(tmp_path):
    assert _soc_lookups(str(tmp_path / "no-such-sdk")) == (None, None)


def test_allowed_os_lookup_degrades_to_empty_for_an_unresolved_core_type(tmp_path):
    """A `board.schema.json` IS present (so `_os_choices()` resolves), but the
    core's `type` is the unresolved sentinel `""` -- the shape a SoM whose
    `silicon:` names no on-disk SoC JSON (or a core id absent from it) hits in
    `read_soms` via `core_types.get(c.id, "")`.

    Before this guard, `cross_class_os("")` subtracted BOTH class runtimes and
    handed back a plausible, populated list (`["baremetal", "off"]`) with no
    way for a consumer to tell the answer was degraded -- offering Bare-metal
    for what may be a Cortex-M core, the exact alp-sdk-vscode#538 defect #870
    exists to close. `allowedOs` must degrade to `[]`, exactly like `type`
    degrades to `""`, not to a plausible-looking subset.

    Mutation-proven: deleting the `if not core_type: return []` guard in
    `allowed_os_lookup` turns this RED (`["baremetal", "off"]` != `[]`);
    restoring it turns it GREEN. Verified by hand while writing this test.
    """
    write(
        tmp_path / "metadata" / "schemas" / "board.schema.json",
        json.dumps({"$defs": {"core_entry": {"properties": {
            "os": {"enum": ["zephyr", "yocto", "baremetal", "off"]}
        }}}}),
    )
    _, allowed_os_lookup = _soc_lookups(str(tmp_path))
    assert allowed_os_lookup is not None
    assert allowed_os_lookup("") == []


@pytestmark_soc
def test_soc_lookups_resolves_e8s_real_core_types():
    """The issue #870 worked example, against the real SoC JSON: E8's
    `a32_cluster`/`m55_hp`/`m55_he` come back with their real `cores[].type`
    strings from `metadata/socs/alif/ensemble/e8.json`."""
    core_type_lookup, _ = _soc_lookups(str(SDK))
    assert core_type_lookup is not None
    assert core_type_lookup("alif:ensemble:e8") == {
        "a32_cluster": "cortex-a32",
        "m55_hp": "cortex-m55",
        "m55_he": "cortex-m55",
    }


@pytestmark_soc
def test_read_soms_reports_e8s_three_cores_with_type_and_allowed_os():
    """End to end, through `read_soms` -- the exact envelope the New Project
    wizard would read for E1M-AEN801, matching issue #870's own worked
    example verbatim."""
    soms = read_soms(str(SDK))
    aen801 = next(s for s in soms if s.sku == "E1M-AEN801")
    assert [c.as_dict() for c in aen801.cores] == [
        {"id": "a32_cluster", "os": "yocto", "type": "cortex-a32",
         "allowedOs": ["yocto", "baremetal", "off"]},
        {"id": "m55_hp", "os": "zephyr", "type": "cortex-m55",
         "allowedOs": ["zephyr", "baremetal", "off"]},
        {"id": "m55_he", "os": "zephyr", "type": "cortex-m55",
         "allowedOs": ["zephyr", "baremetal", "off"]},
    ]


@pytestmark_soc
def test_allowed_os_lookup_matches_tan_planner_topology_exactly(monkeypatch):
    """The STRONGEST reuse proof: `_soc_lookups`'s `allowed_os_lookup` (which
    imports only `tan.core.os_class`) agrees, core type by core type, with
    `tan.planner.topology._allowed_os_for_core` -- the planner's OWN,
    authoritative function, imported from the OTHER module the rule now lives
    behind. If a future edit let the two implementations diverge (e.g. a
    hand-rolled cross-class set that forgot `tan.core.os_class` was the single
    source), this equality check -- not a check against a hard-coded expected
    list -- is what would still catch it.

    Binds `tan.planner` fresh (undoing whatever an earlier test in this
    process left behind, exactly as `tests/core/test_planner_root.py`'s own
    rebind tests do) since THIS is the one test in this file that still needs
    to import it; every other `_soc_lookups`/`read_soms` test above does not.

    Mutation-proven: replacing `_allowed_os_for_core(core_type, METADATA_ROOT)`
    in `tan.planner.topology` with a literal `["zephyr", "baremetal", "off"]`
    turns the `cortex-a32` iteration below RED (a Cortex-A core would wrongly
    get the Cortex-M answer) while leaving `cortex-m55` GREEN by coincidence --
    exactly the drift-that-looks-fine class of bug this check exists to catch;
    restoring the delegation turns both GREEN. Verified by hand while writing
    this test.
    """
    import sys

    from tan import planner_root

    for name in [n for n in sys.modules if n == "tan.planner" or n.startswith("tan.planner.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(planner_root, "_BOUND", None)

    from tan.planner_root import bind_sdk_root

    bind_sdk_root(SDK)
    from tan.planner.paths import METADATA_ROOT
    from tan.planner.topology import _allowed_os_for_core

    _, allowed_os_lookup = _soc_lookups(str(SDK))
    assert allowed_os_lookup is not None
    for core_type in ("cortex-a32", "cortex-m55", "cortex-m33", "", "some-future-core"):
        assert allowed_os_lookup(core_type) == _allowed_os_for_core(core_type, METADATA_ROOT)


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
    #
    # tan-cli#468: `resolve_sdk` now always returns an `ActiveSdk`, never a
    # bare `None` -- `.path is None` is what "resolved to nothing" looks like.
    # `--sdk-root` is terminal, so neither carried-through fact fires here.
    result = resolve_sdk(str(tmp_path / "nope"), str(tmp_path))
    assert result.path is None
    assert result.broken_project_pin is None
    assert result.foreign_global_default_for is None


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
    # `type`/`allowedOs` (tan-cli#870) degrade to `""`/`[]`: this fixture SDK
    # carries no `metadata/socs/**` and no `metadata/schemas/board.schema.json`
    # for either the SoC-type lookup or `_allowed_os_for_core` to resolve
    # against -- see `test_read_soms_reports_e8s_three_cores_with_type_and_allowed_os`
    # below for the populated case.
    assert doc["data"]["soms"][0]["cores"] == [
        {"id": "a55_cluster", "os": "yocto", "type": "", "allowedOs": []},
        {"id": "m33_sm", "os": "zephyr", "type": "", "allowedOs": []},
        {"id": "a32_extra", "os": "yocto", "type": "", "allowedOs": []},
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
    # tan-cli#497 defect 7: with `--sdk-root` GIVEN, the message names the value
    # that was rejected and why. The no-flag message is unchanged and stays
    # pinned by the `presets-no-sdk` golden envelope.
    assert doc["issues"] == [
        {
            "code": "presets.sdk-root-unresolved",
            "severity": "warning",
            "message": (
                'alp-sdk root is unresolved: --sdk-root "./nope" is not an '
                "alp-sdk checkout (scripts/alp_project.py not found under it). "
                "Returning built-in defaults and empty SDK preset lists."
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


def test_text_mode_reports_the_unresolved_sdk_reason_too(tmp_path, monkeypatch):
    """The JSON envelope has always carried `presets.sdk-root-unresolved` in
    `issues`; text mode built the same list and then never printed it -- a
    customer running a bare `tan presets` with no SDK resolvable saw
    `skus=0` and nothing telling them why. Mirrors `examples_cmd.py`'s own
    issue-printing loop for its text branch.

    Driven WITHOUT `--sdk-root` so it pins the shared `SDK_UNRESOLVED_MESSAGE`
    constant: with the flag given, tan-cli#497 replaces the message with the
    one naming the rejected value (covered by its own test below)."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["presets"])
    assert result.exit_code == 0
    assert result.stdout == ""
    assert f"presets: {SDK_UNRESOLVED_MESSAGE}" in result.stderr


def test_a_rejected_sdk_root_flag_is_named_in_the_message(tmp_path, monkeypatch):
    """tan-cli#497 defect 7. `--sdk-root` is TERMINAL, so a path without
    `scripts/alp_project.py` resolves to nothing -- and the warning used to be
    the same string as the no-flag one, whose remediation is "pass --sdk-root
    <path>": the flag the caller had just typed, with the failing value nowhere
    in the JSON envelope or the stderr text. Both modes must now name it."""
    monkeypatch.chdir(tmp_path)
    typo = str(tmp_path / "alp-sdk-typo")

    doc = json.loads(
        runner.invoke(app, ["presets", "--sdk-root", typo, "--format", "json"]).stdout
    )
    assert doc["issues"][0]["code"] == "presets.sdk-root-unresolved"
    assert doc["issues"][0]["message"] == (
        f'alp-sdk root is unresolved: --sdk-root "{typo}" is not an alp-sdk '
        "checkout (scripts/alp_project.py not found under it). Returning "
        "built-in defaults and empty SDK preset lists."
    )

    text = runner.invoke(app, ["presets", "--sdk-root", typo]).stderr
    assert f'presets: alp-sdk root is unresolved: --sdk-root "{typo}" is not' in text
    # The remediation that recommends the flag the caller just passed is GONE
    # from this branch -- that self-defeating sentence is the defect.
    assert "pass --sdk-root <path>" not in text


def test_a_broken_project_pin_is_reported_even_when_nothing_else_resolves(
    tmp_path, monkeypatch
):
    """tan-cli#468. `resolve_sdk` returned a bare `None` whenever nothing
    resolved -- so a workspace whose `.alp/sdk-path` names a checkout that no
    longer exists, with no sibling for discovery to fall through to and no
    `~/.alp/sdk-default` either, reported `presets.sdk-root-unresolved` alone.
    The envelope already says "no SDK"; this is the fix that lets it say WHY.
    Nothing here resolves at all (unlike tan-cli#464's wrong-checkout harm),
    so this is only the diagnostic gap.

    Fails against dev: `doc["issues"]` there is `presets.sdk-root-unresolved`
    alone, with no leading `sdk.project-pin-unresolved` and `"gone-checkout"`
    nowhere in the envelope."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    write(
        tmp_path / ".alp" / "sdk-path",
        json.dumps({"sdkPath": str(tmp_path / "gone-checkout")}),
    )
    result = runner.invoke(app, ["presets", "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    # Absent, not null -- still no usable checkout, so still no `sdk` block.
    assert "sdk" not in doc
    assert doc["data"]["sdkRoot"] is None
    assert [i["code"] for i in doc["issues"]] == [
        "sdk.project-pin-unresolved",
        "presets.sdk-root-unresolved",
    ]
    assert "gone-checkout" in doc["issues"][0]["message"]

    text = runner.invoke(app, ["presets"]).stderr
    assert "gone-checkout" in text


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
