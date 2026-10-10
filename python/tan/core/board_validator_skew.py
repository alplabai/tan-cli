# SPDX-License-Identifier: Apache-2.0
"""Is the bound alp-sdk checkout's validator newer than the one tan ported?

`tan validate` runs a PORT of the SDK's board.yaml validator in-process, so a
checkout whose validator sources moved past the audited commit may carry rules
tan does not apply (a clean verdict that the SDK's own script would reject).
The four sources the port was cut from are hashed here; a differing hash in the
bound checkout is reported as a non-fatal warning. The hashes are kept equal to
`tests/gates/test_planner_relocation_freshness.py::HAND_PORT_HASHES` by
`tests/gates/test_board_validator_skew_pins.py`, so a re-port moves both.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: alp-sdk-relative source -> sha256 at the commit the port was audited against.
PORTED_SOURCE_HASHES: dict[str, str] = {
    "scripts/alp_cli/validator.py": "ce44ba907c411f763e39b07c168f959dd4455da20d3ffba9f8c28eaa5a1f810d",
    "scripts/alp_cli/diagnostic.py": "6a96eb120d73640196cefbd30d13e40a515d00d11368e440123594189cd16d96",
    "scripts/alp_cli/yaml_pos.py": "c83580d86a8f2bc575e20b44f4893847a6a08f765774861f2d29a8bd61ae5b74",
    "scripts/validate_board_yaml.py": "91e06880f068447f98a2c68f1ffb4b6da29c18d2fcadfa0232d982e6612557d4",
}


def _digest(path: Path) -> str:
    # CRLF -> LF so a Windows autocrlf checkout of identical sources is not "newer".
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def skewed_sources(sdk_root: Path) -> tuple[str, ...]:
    """SDK-relative validator sources whose bytes differ from the audited ones.

    A source absent from the checkout (or unreadable) is skipped: an SDK that
    dropped the script has no newer rules for tan to miss.
    """
    skewed: list[str] = []
    for rel, pinned in PORTED_SOURCE_HASHES.items():
        path = Path(sdk_root, *rel.split("/"))
        try:
            if path.is_file() and _digest(path) != pinned:
                skewed.append(rel)
        except OSError:
            continue
    return tuple(skewed)
