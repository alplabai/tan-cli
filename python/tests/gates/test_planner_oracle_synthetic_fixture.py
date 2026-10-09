# SPDX-License-Identifier: Apache-2.0
"""The planner oracle's synthetic error-contract fixture is intact (tan-cli#1424).

`tests/parity/test_planner_oracle_regression.py` is skipped wherever
`ALP_PLANNER_ORACLE_ROOT` is unbound, which is the default gate. These checks
only read checked-in fixture files, so they run everywhere: the `.error`
branch of that module can never again go dead without this file noticing.
"""
from __future__ import annotations

from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "planner_oracle"
SYNTHETIC = FIXTURE / "synthetic"

#: Mirrors `capture_planner_oracle.py`'s mode->extension table on purpose.
_EXTENSION = {
    "build-plan": ".json",
    "system-manifest": ".yaml",
    "ipc-contract-h": ".h",
    "dts-reservations": ".dtsi",
    "dts-partitions": ".dtsi",
    "storage-mounts-c": ".c",
    "tfm-sysbuild-conf": ".conf",
}


def _boards() -> list[Path]:
    return sorted(SYNTHETIC.rglob("board.yaml"))


def test_a_synthetic_board_is_held():
    assert _boards(), f"{SYNTHETIC} holds no board.yaml"


def test_every_synthetic_board_has_exactly_one_golden_per_mode():
    for board in _boards():
        for mode, ext in _EXTENSION.items():
            present = [s for s in (ext, ".error") if (board.parent / (mode + s)).is_file()]
            assert len(present) == 1, (
                f"{board.parent.name}::{mode} has goldens {present}; want exactly "
                "one (a stale other-suffix file makes the test pick arbitrarily)"
            )


def test_the_error_contract_branch_is_exercised():
    errors = [
        g
        for board in _boards()
        for g in board.parent.glob("*.error")
    ]
    assert errors, "no error-contract golden is held, so the .error branch is dead"
    for golden in errors:
        assert golden.read_text(encoding="utf-8").startswith(
            "load:SdkRevisionNotBuildable\n"
        ), f"{golden} is not an SdkRevisionNotBuildable refusal"
