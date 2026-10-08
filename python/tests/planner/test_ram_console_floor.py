# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1401: a generated config must never SHRINK an app-set
`CONFIG_RAM_CONSOLE_BUFFER_SIZE`.

alp.conf (and, for `diagnostics.link: itcm`, `alp-link-itcm.conf`) is merged
AFTER the app's `prj.conf`, so a bare assignment wins and a larger app buffer
wraps. Both now emit max(floor, the size the slice's own `prj.conf` sets): 2048
for the RAM console (alp-sdk#2774, mirrored), 16384 for the itcm retarget.

SDK-free: only the pure helpers and a throwaway app directory, no board.yaml.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.planner._baremetal_support import bound_sdk_root  # noqa: F401


def _app(tmp_path: Path, prj: str | None) -> tuple[SimpleNamespace, SimpleNamespace]:
    (tmp_path / "he").mkdir()
    if prj is not None:
        (tmp_path / "he" / "prj.conf").write_text(prj, encoding="utf-8", newline="\n")
    return SimpleNamespace(source_dir=tmp_path), SimpleNamespace(app="./he")


@pytest.mark.parametrize("prj, want", [
    (None, 0),
    ("CONFIG_FOO=y\n", 0),
    ("CONFIG_RAM_CONSOLE_BUFFER_SIZE=65536\n", 65536),
    ("CONFIG_RAM_CONSOLE_BUFFER_SIZE=0x4000  # sixteen KiB\n", 16384),
    ("CONFIG_RAM_CONSOLE_BUFFER_SIZE=1024\nCONFIG_RAM_CONSOLE_BUFFER_SIZE=8192\n", 8192),
])
def test_the_reader_finds_the_apps_own_size(tmp_path, prj, want):
    from tan.planner.kconfig import _app_ram_console_size

    project, slice_ = _app(tmp_path, prj)
    assert _app_ram_console_size(project, slice_) == want


def test_no_source_dir_or_app_reads_as_nothing_set(tmp_path):
    from tan.planner.kconfig import _app_ram_console_size

    assert _app_ram_console_size(
        SimpleNamespace(source_dir=None), SimpleNamespace(app="./he")) == 0
    assert _app_ram_console_size(
        SimpleNamespace(source_dir=tmp_path), SimpleNamespace(app=None)) == 0


@pytest.mark.parametrize("app_size, want", [(0, 2048), (1024, 2048), (65536, 65536)])
def test_the_ram_console_floor_is_2048(app_size, want):
    from tan.planner.kconfig import _RAM_CONSOLE_MIN_SIZE, _emit_zephyr_console

    size = max(_RAM_CONSOLE_MIN_SIZE, app_size)
    lines = _emit_zephyr_console("ram", ram_size=size)
    assert f"CONFIG_RAM_CONSOLE_BUFFER_SIZE={want}" in lines


@pytest.mark.parametrize("app_size, want", [
    (0, 16384), (4096, 16384), (16384, 16384), (65536, 65536)])
def test_itcm_conf_takes_the_larger_of_16384_and_the_apps_size(app_size, want):
    from tan.planner.link_target import itcm_conf

    conf = itcm_conf(app_ram_console_size=app_size)
    assert f"CONFIG_RAM_CONSOLE_BUFFER_SIZE={want}\n" in conf
    assert conf.count("CONFIG_RAM_CONSOLE_BUFFER_SIZE=") == 1


def test_itcm_conf_default_is_unchanged():
    from tan.planner.link_target import itcm_conf

    assert itcm_conf() == itcm_conf(True, app_ram_console_size=0)
    assert itcm_conf().endswith("CONFIG_RAM_CONSOLE_BUFFER_SIZE=16384\n")


def test_a_uart_itcm_conf_ignores_the_apps_buffer_size():
    from tan.planner.link_target import itcm_conf

    conf = itcm_conf(False, app_ram_console_size=65536)
    assert "CONFIG_RAM_CONSOLE_BUFFER_SIZE" not in conf
    assert "CONFIG_DCACHE" not in conf
    assert "CONFIG_FLASH_LOAD_OFFSET=0x0\n" in conf


def test_the_uart_itcm_artefact_carries_no_buffer_size_even_with_an_app_value(tmp_path):
    from tan.planner.link_target import CONF_NAME, extra_config_artefacts

    project, slice_ = _app(tmp_path, "CONFIG_RAM_CONSOLE_BUFFER_SIZE=65536\n")
    project.diagnostics = {"link": "itcm", "console": "uart"}
    slice_.os, slice_.core_id = "zephyr", "m55_he"
    arts = dict(extra_config_artefacts(project, slice_))
    assert "CONFIG_RAM_CONSOLE_BUFFER_SIZE" not in arts[CONF_NAME]


def test_the_itcm_artefact_reads_the_slices_prj_conf(tmp_path):
    from tan.planner.link_target import CONF_NAME, extra_config_artefacts

    project, slice_ = _app(tmp_path, "CONFIG_RAM_CONSOLE_BUFFER_SIZE=65536\n")
    project.diagnostics = {"link": "itcm"}
    slice_.os, slice_.core_id = "zephyr", "m55_he"
    arts = dict(extra_config_artefacts(project, slice_))
    assert "CONFIG_RAM_CONSOLE_BUFFER_SIZE=65536\n" in arts[CONF_NAME]
