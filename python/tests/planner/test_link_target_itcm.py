# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1350: board.yaml `diagnostics.link: itcm` -> an AEN Flow C
(M55-HE ITCM RAM-run) build.

The planner turns the knob into two extra per-slice config artefacts
(`alp-link-itcm.conf` + `alp-link-itcm.overlay`, written next to `alp.conf`)
and layers them into the Zephyr command (`-DEXTRA_CONF_FILE=...;<conf>`,
`-DEXTRA_DTC_OVERLAY_FILE=<overlay>`). HE-only: every other shape is refused
with a coded error.

Real-SDK-gated: loads the real E1M-AEN801 / E1M-V2N101 presets. The bound
SDK's `board.schema.json` may predate the `diagnostics.link` property (this
repo's CI pins an older alp-sdk until the next re-sync), so each project is
loaded against a throwaway metadata tree whose schema is the SDK's own with
that one property added -- everything else is the real metadata.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- the knob is resolved against real SoM presets.",
)

#: The exact bytes of the Kconfig half.
EXPECTED_CONF = (
    "# Flow C (AEN M55-HE ITCM RAM-run) link retarget -- board.yaml\n"
    "# `diagnostics.link: itcm`.  NOT for an image you will flash to MRAM:\n"
    "# it links at 0x0.  Kconfig half; the devicetree half is\n"
    "# alp-link-itcm.overlay -- BOTH are required.\n"
    "#\n"
    "# Stop deriving the link offset from the DT code-partition and force\n"
    "# the ITCM base (undoes the AEN board defconfig's slot0 default and a\n"
    "# literal CONFIG_FLASH_LOAD_OFFSET an app may hard-code).\n"
    "CONFIG_USE_DT_CODE_PARTITION=n\n"
    "CONFIG_FLASH_LOAD_OFFSET=0x0\n"
    "# The E8 D-cache maintenance loop hangs on this silicon.\n"
    "CONFIG_DCACHE=n\n"
    "# ram_console_out() WRAPS: a buffer smaller than the app's output\n"
    "# reads back as two interleaved points in the run.\n"
    "CONFIG_RAM_CONSOLE_BUFFER_SIZE=16384\n"
)

#: The `console: uart` Kconfig half (tan-cli#1374): the retarget only -- no
#: RAM-console buffer, and the D-cache is left as the board defaults it.
EXPECTED_CONF_UART = (
    "# Flow C (AEN M55-HE ITCM RAM-run) link retarget -- board.yaml\n"
    "# `diagnostics.link: itcm`.  NOT for an image you will flash to MRAM:\n"
    "# it links at 0x0.  Kconfig half; the devicetree half is\n"
    "# alp-link-itcm.overlay -- BOTH are required.\n"
    "#\n"
    "# Stop deriving the link offset from the DT code-partition and force\n"
    "# the ITCM base (undoes the AEN board defconfig's slot0 default and a\n"
    "# literal CONFIG_FLASH_LOAD_OFFSET an app may hard-code).\n"
    "CONFIG_USE_DT_CODE_PARTITION=n\n"
    "CONFIG_FLASH_LOAD_OFFSET=0x0\n"
)

#: The exact bytes of the devicetree half.
EXPECTED_OVERLAY = (
    "/*\n"
    " * Flow C (AEN M55-HE ITCM RAM-run) devicetree retarget -- board.yaml\n"
    " * `diagnostics.link: itcm`.  Moves zephyr,flash to the ITCM and drops\n"
    " * the code-partition so the image links at 0x0.  Kconfig half:\n"
    " * alp-link-itcm.conf -- BOTH are required.  NOT for an MRAM-flashed image.\n"
    " */\n"
    "\n"
    "/ {\n"
    "\tchosen {\n"
    "\t\t/* path-ref form: the pointer form makes FLASH_SIZE=0 and\n"
    "\t\t * overflows the link */\n"
    "\t\tzephyr,flash = &itcm;\n"
    "\t\t/delete-property/ zephyr,code-partition;\n"
    "\t};\n"
    "};\n"
)

_HE_ONLY = """
name: itcm-he
som:
  sku: E1M-AEN801
cores:
  m55_he:
    os: zephyr
    app: ./he
diagnostics:
  link: itcm
"""


@pytest.fixture
def meta(tmp_path: Path) -> Path:
    """A metadata tree: the bound SDK's, with `diagnostics.link` in the schema."""
    src = Path(SDK) / "metadata"
    dst = tmp_path / "metadata"
    dst.mkdir()
    for child in src.iterdir():
        if child.name != "schemas":
            (dst / child.name).symlink_to(child)
    (dst / "schemas").mkdir()
    for f in (src / "schemas").iterdir():
        if f.name != "board.schema.json":
            (dst / "schemas" / f.name).symlink_to(f)
    schema = json.loads((src / "schemas" / "board.schema.json").read_text("utf-8"))
    schema["properties"]["diagnostics"]["properties"].setdefault(
        "link", {"type": "string", "enum": ["auto", "itcm"]})
    (dst / "schemas" / "board.schema.json").write_text(
        json.dumps(schema), encoding="utf-8")
    return dst


def _project(tmp_path: Path, meta: Path, body: str):
    from tan.planner.loader import load_board_yaml

    (tmp_path / "he").mkdir(exist_ok=True)
    (tmp_path / "hp").mkdir(exist_ok=True)
    path = tmp_path / "board.yaml"
    path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    return load_board_yaml(path, metadata_root=meta)


def _plan(tmp_path: Path, project) -> dict:
    from tan.planner.buildplan import emit_build_plan

    return json.loads(emit_build_plan(
        project, board_yaml=tmp_path / "board.yaml",
        build_root=tmp_path / "build"))


def _slice(plan: dict, core_id: str) -> dict:
    return next(s for s in plan["slices"] if s["coreId"] == core_id)


def _artefacts(sl: dict) -> dict[str, str]:
    return {Path(a["path"]).name: a["contents"] for a in sl["configArtefacts"]}


def test_the_fragment_and_overlay_bytes_are_pinned() -> None:
    from tan.planner.link_target import itcm_conf, itcm_overlay

    assert itcm_conf() == EXPECTED_CONF
    assert itcm_conf(ram_console=True) == EXPECTED_CONF
    assert itcm_conf(ram_console=False) == EXPECTED_CONF_UART
    assert itcm_overlay() == EXPECTED_OVERLAY


def test_itcm_he_slice_gets_both_halves_beside_alp_conf(tmp_path, meta) -> None:
    sl = _slice(_plan(tmp_path, _project(tmp_path, meta, _HE_ONLY)), "m55_he")
    arts = _artefacts(sl)
    # The itcm halves ride right after alp.conf; the plan's additive
    # reference artefacts (tan-cli#1216) follow. This synthetic board has no
    # header under include/alp/boards/, so no alp.overlay (a warning instead).
    assert list(arts) == ["alp.conf", "alp-link-itcm.conf",
                          "alp-link-itcm.overlay", "cmake-args.txt"]
    assert arts["alp-link-itcm.conf"] == EXPECTED_CONF
    assert arts["alp-link-itcm.overlay"] == EXPECTED_OVERLAY
    # both halves live in the slice's build dir, next to alp.conf
    paths = [a["path"] for a in sl["configArtefacts"]]
    assert len({str(Path(p).parent) for p in paths}) == 1


def test_command_layers_the_conf_after_alp_conf_and_passes_the_overlay(
        tmp_path, meta) -> None:
    sl = _slice(_plan(tmp_path, _project(tmp_path, meta, _HE_ONLY)), "m55_he")
    args = sl["command"]["args"]
    build = "${PROJECT_ROOT}/build/m55_he-zephyr"
    assert f"-DEXTRA_CONF_FILE={build}/alp.conf;{build}/alp-link-itcm.conf" in args
    assert f"-DEXTRA_DTC_OVERLAY_FILE={build}/alp-link-itcm.overlay" in args
    # alp.conf must come first: a later fragment wins
    conf = next(a for a in args if a.startswith("-DEXTRA_CONF_FILE="))
    assert conf.index("alp.conf") < conf.index("alp-link-itcm.conf")


def test_auto_console_is_promoted_to_the_ram_console(tmp_path, meta) -> None:
    sl = _slice(_plan(tmp_path, _project(tmp_path, meta, _HE_ONLY)), "m55_he")
    alp_conf = _artefacts(sl)["alp.conf"]
    assert "CONFIG_RAM_CONSOLE=y" in alp_conf
    assert "CONFIG_UART_CONSOLE=n" in alp_conf
    assert "CONFIG_UART_CONSOLE=y" not in alp_conf


def test_explicit_ram_console_is_accepted(tmp_path, meta) -> None:
    body = _HE_ONLY + "  console: ram\n"
    sl = _slice(_plan(tmp_path, _project(tmp_path, meta, body)), "m55_he")
    assert "alp-link-itcm.conf" in _artefacts(sl)


def test_other_slices_of_the_project_are_untouched(tmp_path, meta) -> None:
    """The stock M55-HP shim and the A32 Linux slice build exactly as without
    the knob -- a Flow C image is the HE core's alone."""
    on = _plan(tmp_path, _project(tmp_path, meta, _HE_ONLY))
    off = _plan(tmp_path, _project(
        tmp_path, meta, _HE_ONLY.replace("diagnostics:\n  link: itcm\n", "")))
    for core in ("m55_hp", "a32_cluster"):
        assert _slice(on, core) == _slice(off, core)
        assert not any("itcm" in n for n in _artefacts(_slice(on, core)))
        assert "EXTRA_DTC_OVERLAY_FILE" not in json.dumps(_slice(on, core)["command"])


def test_unset_and_auto_are_byte_identical_to_before(tmp_path, meta) -> None:
    unset = _plan(tmp_path, _project(
        tmp_path, meta, _HE_ONLY.replace("diagnostics:\n  link: itcm\n", "")))
    auto = _plan(tmp_path, _project(
        tmp_path, meta, _HE_ONLY.replace("link: itcm", "link: auto")))
    assert unset == auto
    he = _slice(unset, "m55_he")
    assert list(_artefacts(he)) == ["alp.conf", "cmake-args.txt"]
    assert "EXTRA_DTC_OVERLAY_FILE" not in json.dumps(he["command"])


def test_hp_only_project_is_refused(tmp_path, meta) -> None:
    from tan.planner.link_target import UNSUPPORTED_CODE, LinkTargetError

    body = """
        name: itcm-hp
        som:
          sku: E1M-AEN801
        cores:
          m55_hp:
            os: zephyr
            app: ./hp
        diagnostics:
          link: itcm
    """
    with pytest.raises(LinkTargetError) as ei:
        _project(tmp_path, meta, body)
    assert ei.value.code == UNSUPPORTED_CODE == "build.link-itcm-unsupported"
    assert "m55_hp" in str(ei.value) and "0x50000000" in str(ei.value)


def test_hp_app_alongside_the_he_app_only_retargets_the_he(tmp_path, meta) -> None:
    body = """
        name: itcm-both
        som:
          sku: E1M-AEN801
        cores:
          m55_he:
            os: zephyr
            app: ./he
          m55_hp:
            os: zephyr
            app: ./hp
        diagnostics:
          link: itcm
    """
    plan = _plan(tmp_path, _project(tmp_path, meta, body))
    assert "alp-link-itcm.conf" in _artefacts(_slice(plan, "m55_he"))
    assert "alp-link-itcm.conf" not in _artefacts(_slice(plan, "m55_hp"))


def test_non_aen_som_is_refused(tmp_path, meta) -> None:
    from tan.planner.link_target import UNSUPPORTED_CODE, LinkTargetError

    body = """
        name: itcm-v2n
        som:
          sku: E1M-V2N101
        cores:
          m33_sm:
            os: zephyr
            app: ./he
        diagnostics:
          link: itcm
    """
    with pytest.raises(LinkTargetError) as ei:
        _project(tmp_path, meta, body)
    assert ei.value.code == UNSUPPORTED_CODE
    assert "E1M-V2N101" in str(ei.value)


def test_uart_console_keeps_the_itcm_retarget_without_ram_console_bits(
        tmp_path, meta) -> None:
    """tan-cli#1374: a RAM-run with a UART shell (bench 2026-10-07, UART5)
    needs the ITCM retarget but not the RAM-console buffer / DCACHE=n."""
    sl = _slice(_plan(tmp_path, _project(tmp_path, meta,
                                         _HE_ONLY + "  console: uart\n")),
                "m55_he")
    arts = _artefacts(sl)
    # The plan may carry other artefacts (cmake-args.txt, alp.overlay on header
    # boards); only the two itcm halves and their order after alp.conf matter.
    names = list(arts)
    assert names[0] == "alp.conf"
    assert names.index("alp-link-itcm.conf") < names.index("alp-link-itcm.overlay")
    assert arts["alp-link-itcm.conf"] == EXPECTED_CONF_UART
    assert arts["alp-link-itcm.overlay"] == EXPECTED_OVERLAY
    conf = arts["alp-link-itcm.conf"]
    assert "CONFIG_USE_DT_CODE_PARTITION=n\n" in conf
    assert "CONFIG_FLASH_LOAD_OFFSET=0x0\n" in conf
    assert "CONFIG_DCACHE" not in conf
    assert "CONFIG_RAM_CONSOLE_BUFFER_SIZE" not in conf
    # the console itself stays the UART one: alp.conf is NOT promoted to RAM
    assert "CONFIG_RAM_CONSOLE=y" not in arts["alp.conf"]
    assert "CONFIG_UART_CONSOLE=y" in arts["alp.conf"]


def test_uses_ram_console_is_true_only_for_auto_and_ram() -> None:
    from tan.planner.link_target import uses_ram_console

    assert uses_ram_console(None)
    assert uses_ram_console({})
    assert uses_ram_console({"console": "auto"})
    assert uses_ram_console({"console": "ram"})
    for other in ("uart", "alp", "linux", "none"):
        assert not uses_ram_console({"console": other})


def test_uart_conf_overwrites_a_ram_conf_in_the_same_build_dir(
        tmp_path, meta) -> None:
    """Switching `console: ram` -> `uart` and rebuilding in one build dir must
    leave the uart bytes in `alp-link-itcm.conf` (no stale RAM-console bits)."""
    from tan.commands.build.materialise import materialise_plan
    from tan.core.build_plan import parse_build_plan

    root = tmp_path / "build"
    for console in ("ram", "uart"):
        plan = parse_build_plan(json.dumps(_plan(tmp_path, _project(
            tmp_path, meta, _HE_ONLY + f"  console: {console}\n"))))
        written = materialise_plan(plan, root)
    conf = next(p for p in written if p.name == "alp-link-itcm.conf")
    assert conf.read_text("utf-8") == EXPECTED_CONF_UART


def test_explicit_ram_console_keeps_the_ram_console_bits(tmp_path, meta) -> None:
    sl = _slice(_plan(tmp_path, _project(tmp_path, meta,
                                         _HE_ONLY + "  console: ram\n")),
                "m55_he")
    assert _artefacts(sl)["alp-link-itcm.conf"] == EXPECTED_CONF


@pytest.mark.parametrize("console", ["alp", "linux", "none"])
def test_other_explicit_consoles_are_still_refused(tmp_path, meta, console) -> None:
    from tan.planner.link_target import CONSOLE_CONFLICT_CODE, LinkTargetError

    with pytest.raises(LinkTargetError) as ei:
        _project(tmp_path, meta, _HE_ONLY + f"  console: {console}\n")
    assert ei.value.code == CONSOLE_CONFLICT_CODE == "build.link-itcm-console-conflict"
    assert "uart" in str(ei.value)


def test_sysbuild_project_is_refused(tmp_path, meta) -> None:
    from tan.planner.link_target import UNSUPPORTED_CODE, LinkTargetError

    body = _HE_ONLY + """
boot:
  method: mcuboot
  signing:
    algorithm: ecdsa_p256
    key_file: keys/k.pem
"""
    with pytest.raises(LinkTargetError) as ei:
        _project(tmp_path, meta, body)
    assert ei.value.code == UNSUPPORTED_CODE
    assert "sysbuild" in str(ei.value)


def _sdk_schema_knows_link() -> bool:
    schema = json.loads(
        (Path(SDK) / "metadata" / "schemas" / "board.schema.json").read_text("utf-8"))
    return "link" in schema["properties"]["diagnostics"]["properties"]


@pytest.mark.skipif(
    SDK is not None and not _sdk_schema_knows_link(),
    reason="the bound alp-sdk predates `diagnostics.link` in board.schema.json "
           "(`_emit_plan` plans against the SDK's own schema, which cannot be "
           "swapped for a patched one without rebinding the planner)")
def test_build_surfaces_the_refusal_as_a_coded_validation_failure(tmp_path) -> None:
    """`tan build` reports the planner's refusal under its own code, exit 2 --
    not the generic `build.plan-unavailable` runtime failure."""
    from tan.commands.build_cmd import BuildError, _emit_plan
    from tan.exit_codes import ExitCode

    (tmp_path / "hp").mkdir()
    board = tmp_path / "board.yaml"
    board.write_text(textwrap.dedent("""
        name: itcm-hp
        som:
          sku: E1M-AEN801
        cores:
          m55_hp:
            os: zephyr
            app: ./hp
        diagnostics:
          link: itcm
    """).lstrip("\n"), encoding="utf-8")
    with pytest.raises(BuildError) as ei:
        _emit_plan(str(SDK), str(board))
    assert ei.value.code == "build.link-itcm-unsupported"
    assert ei.value.exit_code == ExitCode.VALIDATION_FAILURE


def test_an_itcm_slice_carries_no_mram_flash_recipe(tmp_path, meta) -> None:
    """tan-cli#1350 review: plain `tan flash` must not sign + write a
    0x0-linked image to MRAM slot0. The manifest slice is `ram_run_only` with
    ONLY the wrong-board identity pair, never Flow D's keys."""
    on = _project(tmp_path, meta, _HE_ONLY)
    entry = on.cores["m55_he"].to_manifest_entry()
    assert on.cores["m55_he"].link_target == "itcm"
    assert entry["flash_method"] == "ram_run_only"
    assert set(entry["flash_args"]) <= {"expect_dpidr", "jlink_device"}
    # the other slices keep their ordinary recipe
    assert on.cores["m55_hp"].link_target is None
    assert on.cores["m55_hp"].to_manifest_entry()["flash_method"] == "zephyr_west_flash"
    # and with the knob off the HE recipe is exactly what it was
    off = _project(tmp_path, meta, _HE_ONLY.replace("diagnostics:\n  link: itcm\n", ""))
    he = off.cores["m55_he"].to_manifest_entry()
    assert he["flash_method"] == "zephyr_west_flash"
    assert "jlink_flash_device" in he["flash_args"]


def test_an_unproven_aen_sku_is_refused(tmp_path, meta) -> None:
    from tan.planner.link_target import UNSUPPORTED_CODE, LinkTargetError

    body = _HE_ONLY.replace("E1M-AEN801", "E1M-AEN401")
    with pytest.raises(LinkTargetError) as ei:
        _project(tmp_path, meta, body)
    assert ei.value.code == UNSUPPORTED_CODE
    assert "E1M-AEN401" in str(ei.value)


def test_the_other_proven_sku_is_accepted(tmp_path, meta) -> None:
    body = _HE_ONLY.replace("E1M-AEN801", "E1M-AEN803")
    sl = _slice(_plan(tmp_path, _project(tmp_path, meta, body)), "m55_he")
    assert "alp-link-itcm.conf" in _artefacts(sl)


_HE_EVK = """
som:
  sku: E1M-AEN801
preset: e1m-evk
cores:
  m55_he:
    os: zephyr
    app: ./he
diagnostics:
  link: itcm
"""


def test_itcm_he_slice_on_a_preset_board_has_the_five_artefacts_in_order(
        tmp_path, meta) -> None:
    """tan-cli#1398: a real `preset: e1m-evk` board has a header under
    include/alp/boards/, so `alp.overlay` joins the itcm halves. The order is
    the plan's contract: alp.conf, the itcm conf + overlay, alp.overlay, then
    the additive cmake-args.txt."""
    sl = _slice(_plan(tmp_path, _project(tmp_path, meta, _HE_EVK)), "m55_he")
    arts = _artefacts(sl)
    assert list(arts) == ["alp.conf", "alp-link-itcm.conf",
                          "alp-link-itcm.overlay", "alp.overlay",
                          "cmake-args.txt"]
    assert arts["alp-link-itcm.conf"] == EXPECTED_CONF
    assert arts["alp-link-itcm.overlay"] == EXPECTED_OVERLAY
