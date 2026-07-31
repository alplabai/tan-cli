# SPDX-License-Identifier: Apache-2.0
"""Unit tests for `tan.core.run` -- ported 1:1 from
`crates/tan-core/src/run.rs`'s own `#[cfg(test)]` module, so a divergence from
the Rust oracle's decision table shows up here rather than only end to end."""
from tan.core.run import (
    RunAction,
    decide_run_action,
    is_native_sim_board,
    native_sim_exe_beside,
    native_sim_slice,
)
from tan.core.system_manifest import parse_system_manifest


def test_build_failure_short_circuits_before_run_or_flash():
    assert decide_run_action(False, False, False, True) == RunAction.BUILD_FAILED
    assert decide_run_action(False, True, True, True) == RunAction.BUILD_FAILED
    assert decide_run_action(False, None, True, False) == RunAction.BUILD_FAILED


def test_native_sim_target_executes_regardless_of_flash_or_manifest_written():
    assert decide_run_action(True, True, False, True) == RunAction.EXECUTE_NATIVE
    assert decide_run_action(True, True, True, True) == RunAction.EXECUTE_NATIVE
    assert decide_run_action(True, True, True, False) == RunAction.EXECUTE_NATIVE


def test_hardware_flashes_only_with_explicit_flag_and_confirmed_write():
    assert decide_run_action(True, False, True, True) == RunAction.FLASH


def test_hardware_without_flash_is_build_only_regardless_of_manifest_written():
    assert decide_run_action(True, False, False, True) == RunAction.BUILD_ONLY
    assert decide_run_action(True, False, False, False) == RunAction.BUILD_ONLY


def test_flash_refused_when_hardware_manifest_write_failed():
    assert decide_run_action(True, False, True, False) == RunAction.MANIFEST_STALE


def test_flash_refused_when_target_unknown():
    assert decide_run_action(True, None, True, True) == RunAction.MANIFEST_STALE
    assert decide_run_action(True, None, True, False) == RunAction.MANIFEST_STALE


def test_build_only_when_target_unknown_and_no_flash():
    assert decide_run_action(True, None, False, True) == RunAction.BUILD_ONLY
    assert decide_run_action(True, None, False, False) == RunAction.BUILD_ONLY


def test_a1_native_sim_target_with_failed_manifest_write_never_flashes():
    """Root-cause regression (A1, Rust's own naming): a native_sim target this
    run's build established (`True`) must settle EXECUTE_NATIVE before
    `manifest_written` is even consulted -- never FLASH, however `--flash` is
    set."""
    assert decide_run_action(True, True, True, False) != RunAction.FLASH
    assert decide_run_action(True, True, True, False) == RunAction.EXECUTE_NATIVE


def test_a2_hardware_target_bare_run_never_executes_native():
    """Root-cause regression (A2): a confirmed hardware target with no
    `--flash` stops at BUILD_ONLY, never EXECUTE_NATIVE."""
    assert decide_run_action(True, False, False, True) == RunAction.BUILD_ONLY
    assert decide_run_action(True, False, False, True) != RunAction.EXECUTE_NATIVE


MANIFEST_NATIVE = """
schema_version: 1
hw_info:
  sku: E1M-AEN701
slices:
- core_id: native_sim
  os: zephyr
  board: native_sim
  status: ok
  output_artefact: build/native_sim-zephyr/build/zephyr/zephyr.elf
ipc: []
helper_mcus: []
boot_order: []
"""

MANIFEST_HARDWARE = """
schema_version: 1
hw_info:
  sku: E1M-AEN701
slices:
- core_id: m55_hp
  os: zephyr
  board: alp_e1m_aen701_m55_hp
  status: ok
ipc: []
helper_mcus: []
boot_order: []
"""


def test_native_sim_slice_found_by_board_target():
    manifest = parse_system_manifest(MANIFEST_NATIVE)
    slice_ = native_sim_slice(manifest)
    assert slice_ is not None
    assert slice_["core_id"] == "native_sim"
    assert slice_["output_artefact"] == "build/native_sim-zephyr/build/zephyr/zephyr.elf"


def test_native_sim_slice_absent_for_a_hardware_manifest():
    manifest = parse_system_manifest(MANIFEST_HARDWARE)
    assert native_sim_slice(manifest) is None


def test_native_sim_slice_found_for_qualified_board_form():
    manifest = parse_system_manifest(
        "schema_version: 1\nhw_info:\n  sku: S\nslices:\n- core_id: native_sim\n  "
        "os: zephyr\n  board: native_sim/native/64\n  status: ok\nipc: []\n"
        "helper_mcus: []\nboot_order: []\n"
    )
    assert native_sim_slice(manifest) is not None


def test_native_sim_slice_rejects_unrelated_prefix_collision():
    manifest = parse_system_manifest(
        "schema_version: 1\nhw_info:\n  sku: S\nslices:\n- core_id: c1\n  "
        "os: zephyr\n  board: native_simulated_soc\n  status: ok\nipc: []\n"
        "helper_mcus: []\nboot_order: []\n"
    )
    assert native_sim_slice(manifest) is None


def test_is_native_sim_board_rejects_a_non_string():
    assert is_native_sim_board(None) is False
    assert is_native_sim_board(123) is False


def test_native_sim_exe_replaces_only_the_elf_filename():
    assert (
        native_sim_exe_beside("/ws/build/native_sim-zephyr/build/zephyr/zephyr.elf")
        == "/ws/build/native_sim-zephyr/build/zephyr/zephyr.exe"
    )
    assert native_sim_exe_beside("zephyr.elf") == "zephyr.exe"
    assert native_sim_exe_beside("a/zephyr.exe") == "a/zephyr.exe"
    assert (
        native_sim_exe_beside(r"C:\ws\build\zephyr\zephyr.elf")
        == r"C:\ws\build\zephyr\zephyr.exe"
    )
