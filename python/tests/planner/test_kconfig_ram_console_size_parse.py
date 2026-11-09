# SPDX-License-Identifier: Apache-2.0
"""`CONFIG_RAM_CONSOLE_BUFFER_SIZE` parsing from an app prj.conf (tan-cli#1487;
ported from alp-sdk#2822's `tests/scripts/test_kconfig_ram_console_size_parse.py`).
A leading-zero decimal (`016384`) used to raise an uncaught ValueError from
int(x, 0); Kconfig reads it as decimal 16384.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner requires a bound root. A SKIP "
           "about the missing root, not a pass.",
)


@pytest.mark.parametrize("text,expected", [
    ("CONFIG_RAM_CONSOLE_BUFFER_SIZE=016384\n", 16384),
    ("CONFIG_RAM_CONSOLE_BUFFER_SIZE=0\n", 0),
    ("CONFIG_RAM_CONSOLE_BUFFER_SIZE=0x4000\n", 0x4000),
    ("CONFIG_RAM_CONSOLE_BUFFER_SIZE=0X10\n", 16),
    ("CONFIG_RAM_CONSOLE_BUFFER_SIZE=4096 # c\n", 4096),
    ("CONFIG_RAM_CONSOLE_BUFFER_SIZE=1024\nCONFIG_RAM_CONSOLE_BUFFER_SIZE=010\n", 10),
])
def test_size_parse(tmp_path, monkeypatch, text, expected):
    from tan.planner import kconfig, orchestrator

    (tmp_path / "prj.conf").write_text(text, encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_zephyr_app_dir",
                        lambda app, src: tmp_path)
    project = SimpleNamespace(source_dir=tmp_path)
    slice_ = SimpleNamespace(app="./app")
    assert kconfig._app_ram_console_size(project, slice_) == expected
