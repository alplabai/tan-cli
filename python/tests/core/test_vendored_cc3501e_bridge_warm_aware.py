# SPDX-License-Identifier: Apache-2.0
"""The vendored `iot` scaffold ships alp-sdk's warm-aware CC3501E bring-up
(alp-sdk#2826 / #2797, in the 64481b8e re-sync range; tan-cli#1525).

SDK-free twin of the seam1 scaffold byte-parity failure: the pre-#2826 copy
cycled the WIFI_EN supply on every re-run, which can leave an activated
CC3501E unrecoverable. Pins the behaviour markers, not the whole file (the
byte comparison against the live emit lives in
`tests/parity/scaffold_byte_parity.py`).
"""

from __future__ import annotations

from pathlib import Path

import tan

_BRIDGE = (
    Path(tan.__file__).parent
    / "templates/vendored/iot/E1M-AEN801/src/cc3501e_bridge.c"
)


def test_vendored_iot_bridge_is_warm_aware():
    text = _BRIDGE.read_text(encoding="utf-8")
    assert "cc3501e_hard_reset" in text
    assert "aen_wifi_en_latch_high" in text
    assert "wifi_was_high" in text
