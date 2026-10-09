# SPDX-License-Identifier: Apache-2.0
"""Up-front `--port` URL validation (tan-cli#1448)."""
from __future__ import annotations

import pytest

from tan.core.serial_url import port_url_problem


@pytest.mark.parametrize(
    "port",
    ["/dev/ttyUSB0", "COM7", "/dev/serial/by-id/usb-x-if00", "rfc2217://100.64.0.1:4001",
     "socket://host:23", "rfc2217://[::1]:4001", "rfc2217://gw:4001?ign_set_control",
     "loop://", "spy:///dev/ttyUSB0", "hwgrep://0403"],
)
def test_good_ports_pass(port):
    assert port_url_problem(port) is None


@pytest.mark.parametrize(
    ("port", "fragment"),
    [
        ("rfc2217://100.64.0.1:", "port number is empty"),
        ("socket://host:", "port number is empty"),
        ("rfc2217://100.64.0.1", "port number is missing"),
        ("rfc2217://host:abc", "not a valid"),
        ("rfc2217://host:0", "outside 1-65535"),
        ("rfc2217://host:70000", "not a valid"),
        ("rfc2217://:4001", "no host"),
        ("socket://", "no host"),
        ("telnet://host:23", "unknown serial URL scheme"),
    ],
)
def test_bad_urls_name_the_problem(port, fragment):
    assert fragment in port_url_problem(port)
