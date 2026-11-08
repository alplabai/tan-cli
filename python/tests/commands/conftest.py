# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1312: `tan flash` enumerates the host's J-Links (read-only sysfs)
before a Flow D entry. A bench host carries several, which would refuse every
pre-existing in-process Flow D test for a reason it is not about -- so by
default no probe is visible. Tests of the selection inject their own
enumerator (or patch `flash_cmd.enumerate_jlinks` again); subprocess runs get
the same default from `tests/commands/_no_usb/sitecustomize.py`.

The same shape for the yocto_wic in-use guard: the real write path asks the
host whether the target is mounted, which differs per CI image (no /sys on
macOS). Every test gets a host with nothing mounted and nothing claimed;
`test_yocto_wic_target_in_use.py` installs its own fake over this one."""
import pytest

from tan.commands import flash_cmd
from tan.core.block_busy import HostFs


@pytest.fixture(autouse=True)
def _no_visible_jlinks(monkeypatch):
    monkeypatch.setattr(flash_cmd, "enumerate_jlinks", lambda: [])


@pytest.fixture(autouse=True)
def _idle_host_for_yocto_wic(monkeypatch):
    monkeypatch.setattr(
        flash_cmd,
        "YOCTO_WIC_HOST_FS",
        HostFs(
            read_text=lambda _p: "",
            exists=lambda p: p.startswith("/sys/dev/block/"),
            isdir=lambda _p: True,
            rdev=lambda _p: (8, 0),
            listdir=lambda _p: [],
            open_excl=lambda _p: None,
        ),
    )
