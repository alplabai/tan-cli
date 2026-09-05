# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1211: keep ``monitor_cmd._available_ports``'s read of pyserial's
``ListPortInfo`` in lockstep with the wire shape
``contract/envelopes/monitor-no-port`` publishes.

``monitor_cmd.py``'s ``return [(p.device, p.description or "") for p in
list_ports.comports()]`` is the only line in the shipping tree that names a
pyserial attribute, and every value that reaches
``data.availablePorts[].device`` on the wire came out of it. Until this module
existed no test executed it: ``git grep comports -- python/tests`` matched
eight lines, every one a comment or ``test_a_character_device_not_in_comports_
is_accepted``, which is about a device ``comports()`` *omits*.

**Why the golden cannot cover this, which is the whole reason this file
exists.** ``contract/envelopes/monitor-no-port`` (tan-cli#1165) arms
``_TEST_PORTS_ENV`` from its ``env.json``, and that seam returns a canned list
from the TOP of ``_available_ports`` -- before pyserial is imported, let alone
called. That is the right design for the golden: ``comports()`` enumerates
whatever serial hardware is physically attached, so a recording taken off it
pins one machine rather than the contract. The consequence is that the golden
replays ``_ports_data`` -> ``_refuse_listing_ports`` -> ``emit()`` and never
reaches the pyserial read, so the object-to-wire mapping was the one part of
this path nothing checked while looking covered.

MEASURED, and the reason this is a gate rather than a comment: ``ListPortInfo``
carries BOTH ``.device`` and ``.name``, so ``p.device`` -> ``p.name`` is a
rename that raises nothing and reviews clean. Applied to the shipping line it
turns ``/dev/cu.debug-console`` into ``cu.debug-console`` -- a device string
the consumer cannot open -- and the monitor and contract-envelope suites stayed
at ``69 passed, 1 xfailed``, byte-identical to the unmutated run.

**What this proves, and what it does not.** It replaces only pyserial itself --
``serial``, ``serial.tools`` and ``serial.tools.list_ports`` planted in
``sys.modules`` -- and then runs the real ``_available_ports`` and the real
``_ports_data``. So it proves that the mapping from a ``ListPortInfo``-shaped
object to tan's wire dict is the declared one. It does NOT prove that pyserial
still enumerates correctly, or that ``ListPortInfo`` still spells its
attributes this way; nothing short of driving the real ``comports()`` can, and
the only place that happens is ``test_packaged_binary.py::
test_the_artifact_carries_pyserial``, whose whole module is skipped without a
``dist/`` build -- which the required CI legs never produce. This is exactly
the split ``test_sdk_list_contract_key_set.py`` states for ``sdk list``, where
the socket is what gets replaced instead of the enumeration.

The ``sys.modules`` plant is deliberate over a ``monkeypatch.setattr`` on
``_available_ports``: stubbing the function is what left the line uncovered in
the first place. It is also environment-independent -- ``sys.modules`` is
consulted before any finder -- so this runs identically where pyserial 3.5 is
installed and where ``importlib.util.find_spec("serial") is None``, which is
what ``ci.yml``'s deliberately extras-less ``pip install -e ./python`` gives.
"""

from __future__ import annotations

import sys
import types

import pytest

from tan.commands import monitor_cmd


class _FakeListPortInfo:
    """The pyserial object shape ``_available_ports`` reads.

    ``name`` is populated and DIFFERENT from ``device`` on purpose: real
    pyserial sets ``name`` to the bare port name and ``device`` to the full
    path it can be opened by, so a ``p.device`` -> ``p.name`` rename must
    change what this test observes rather than being invisible to it.
    """

    def __init__(self, device: str, description: str | None, name: str) -> None:
        self.device = device
        self.description = description
        self.name = name


@pytest.fixture
def plant_pyserial(monkeypatch):
    """Install a fake ``serial.tools.list_ports`` whose ``comports()`` answers
    a fixed list, and disarm ``_TEST_PORTS_ENV`` so the real pyserial branch of
    ``_available_ports`` is the one that runs."""

    def _plant(ports: list) -> None:
        monkeypatch.delenv(monitor_cmd._TEST_PORTS_ENV, raising=False)
        serial_mod = types.ModuleType("serial")
        tools_mod = types.ModuleType("serial.tools")
        list_ports_mod = types.ModuleType("serial.tools.list_ports")
        list_ports_mod.comports = lambda: list(ports)
        tools_mod.list_ports = list_ports_mod
        serial_mod.tools = tools_mod
        monkeypatch.setitem(sys.modules, "serial", serial_mod)
        monkeypatch.setitem(sys.modules, "serial.tools", tools_mod)
        monkeypatch.setitem(sys.modules, "serial.tools.list_ports", list_ports_mod)

    return _plant


def test_the_wire_device_is_pyserial_device_and_not_pyserial_name(plant_pyserial):
    """THE tan-cli#1211 case. `device` must be the openable path pyserial puts
    on `ListPortInfo.device`, never the bare `name` beside it."""
    plant_pyserial(
        [
            _FakeListPortInfo("/dev/ttyUSB0", "USB Serial", name="ttyUSB0"),
            _FakeListPortInfo("COM7", "USB Serial Device", name="COM7"),
        ]
    )

    assert monitor_cmd._ports_data(monitor_cmd._available_ports()) == [
        {"device": "/dev/ttyUSB0", "description": "USB Serial"},
        {"device": "COM7", "description": "USB Serial Device"},
    ]


def test_a_description_less_port_reaches_the_wire_as_the_empty_string(plant_pyserial):
    """`monitor_cmd`'s own `p.description or ""` fallback, which real pyserial
    can never trigger: `ListPortInfo` defaults `description` to the truthy
    literal `"n/a"`, so `None` only ever arrives from a stand-in like this one.
    The wire key must still be PRESENT and a string -- the consumer drops any
    entry whose fields are not strings."""
    plant_pyserial([_FakeListPortInfo("/dev/ttyACM0", None, name="ttyACM0")])

    assert monitor_cmd._ports_data(monitor_cmd._available_ports()) == [
        {"device": "/dev/ttyACM0", "description": ""}
    ]


def test_pyserials_own_n_a_default_is_passed_through_untouched(plant_pyserial):
    """`"n/a"` is PYSERIAL's default, not tan's: `serial/tools/
    list_ports_common.py`'s `ListPortInfo.__init__` assigns
    `self.description = 'n/a'` (verified against pyserial 3.5). It is truthy,
    so the `or ""` guard never fires against it and it reaches the consumer
    verbatim. `contract/envelopes/monitor-no-port/expected.json` pins that same
    string; this is the producer-side half of why it is the right value to
    pin."""
    plant_pyserial([_FakeListPortInfo("COM8", "n/a", name="COM8")])

    assert monitor_cmd._ports_data(monitor_cmd._available_ports()) == [
        {"device": "COM8", "description": "n/a"}
    ]


def test_a_port_object_with_no_device_attribute_fails_loudly(plant_pyserial):
    """The one shape that is already safe, pinned so it stays that way: an
    object carrying `name` but no `device` must RAISE rather than quietly
    yielding an entry the consumer would drop -- so a later "harden this"
    edit to `getattr(p, "device", "")` is a RED here rather than a silent
    regression to empty device strings.

    Scoped deliberately to the raise: what the CLI then reports for an
    unexpected exception (`monitor.internal-failure` at exit 5, per
    `_available_ports`' own docstring on the ImportError guard) is the generic
    handler's business and is not asserted here.
    """

    class _NoDevice:
        def __init__(self) -> None:
            self.name = "ttyUSB0"
            self.description = "USB Serial"

    plant_pyserial([_NoDevice()])

    with pytest.raises(AttributeError):
        monitor_cmd._available_ports()


def test_no_ports_detected_is_an_empty_list_not_an_absent_key(plant_pyserial):
    """`availablePorts` is `[]`, never missing, when `comports()` answers
    nothing -- the shape the consumer's `?? []` fallback is written against."""
    plant_pyserial([])

    assert monitor_cmd._ports_data(monitor_cmd._available_ports()) == []
