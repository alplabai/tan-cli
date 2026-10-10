# SPDX-License-Identifier: Apache-2.0
"""The skew detector's pins are the freshness gate's HAND_PORT pins (tan-cli#1484)."""

from __future__ import annotations

from tan.core.board_validator_skew import PORTED_SOURCE_HASHES
from tests.gates.test_planner_relocation_freshness import HAND_PORT_HASHES


def test_skew_pins_equal_the_hand_port_pins() -> None:
    for rel, digest in PORTED_SOURCE_HASHES.items():
        assert HAND_PORT_HASHES[rel] == digest, rel
