# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1312: `tan flash` now enumerates the host's J-Links (read-only
sysfs) before a Flow D entry. A bench host carries several, which would refuse
every pre-existing Flow D test for a reason it is not about -- so by default no
probe is visible, both in-process (the enumerator is stubbed) and in the
`tan` subprocesses the suite spawns (`TAN_USB_SYSFS_ROOT` points at an empty
tree). Tests of the selection itself inject their own enumerator."""
import pytest

from tan.commands import flash_cmd


@pytest.fixture(autouse=True)
def _no_visible_jlinks(monkeypatch, tmp_path_factory):
    monkeypatch.setattr(flash_cmd, "enumerate_jlinks", lambda: [])
    monkeypatch.setenv("TAN_USB_SYSFS_ROOT", str(tmp_path_factory.mktemp("no-usb")))
