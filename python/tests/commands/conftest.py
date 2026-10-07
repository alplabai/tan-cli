# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1312: `tan flash` enumerates the host's J-Links (read-only sysfs)
before a Flow D entry. A bench host carries several, which would refuse every
pre-existing in-process Flow D test for a reason it is not about -- so by
default no probe is visible. Tests of the selection inject their own
enumerator (or patch `flash_cmd.enumerate_jlinks` again); subprocess runs get
the same default from `tests/commands/_no_usb/sitecustomize.py`."""
import pytest

from tan.commands import flash_cmd


@pytest.fixture(autouse=True)
def _no_visible_jlinks(monkeypatch):
    monkeypatch.setattr(flash_cmd, "enumerate_jlinks", lambda: [])
