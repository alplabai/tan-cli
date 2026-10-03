# SPDX-License-Identifier: Apache-2.0
"""alp-sdk#2316 (tan-cli#1295): `security.psa.attestation_root:
optiga_trust_m` must follow the part's POPULATION, not just its presence
under `on_module:` / `capabilities:` -- ported alongside alp-sdk's
`tests/scripts/test_orchestrate_security.py::
test_security_psa_attestation_optiga_follows_population` (alp-sdk
`ed430e271`).

E1M-AEN801 names OPTIGA Trust M under `on_module:` and sets
`capabilities.optiga_trust_m: true`, but its `on_module.i2c_devices` entry is
`assembled: false` (DNP). The loader used to accept the attestation root
there, so the build succeeded and the firmware then failed on silicon. The
same class as alp-sdk#2311's unassembled-OSPI guard
(`test_security_psa_unassembled_ospi.py`). A SKU with the part fitted
(E1M-AEN401) must still accept it.

Real-SDK-gated: reads the real E1M-AEN801 / E1M-AEN401 presets from a bound
alp-sdk checkout.

Run locally:

    python -m pytest python/tests/planner/test_security_psa_optiga_population.py -v
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner requires SOME bound root "
           "(tan/planner_root.py), and every case here reads a real SoM "
           "preset from the bound checkout.",
)


def _write_board(tmp_path: Path, sku: str) -> Path:
    path = tmp_path / "board.yaml"
    path.write_text(textwrap.dedent(f"""
        name: test-optiga-population
        som:
          sku: {sku}
        cores:
          m55_hp:
            os: zephyr
            app: ./m55_hp
        security:
          psa:
            attestation_root: optiga_trust_m
            tfm: true
    """).lstrip("\n"), encoding="utf-8")
    return path


def test_optiga_attestation_root_refused_where_the_part_is_dnp(
    tmp_path: Path,
) -> None:
    """E1M-AEN801 carries OPTIGA as a DNP footprint -- refused with the
    specific population reason, naming the SKU and the preset file."""
    from tan.planner import load_board_yaml
    from tan.planner.models import OrchestratorError

    with pytest.raises(OrchestratorError) as excinfo:
        load_board_yaml(_write_board(tmp_path, "E1M-AEN801"))
    msg = str(excinfo.value)
    assert "security.psa.attestation_root: optiga_trust_m" in msg
    assert "'optiga_trust_m', which is not assembled on SoM E1M-AEN801" in msg
    assert "assembled: false in metadata/e1m_modules/E1M-AEN801.yaml" in msg
    # The population reason, not the older "does not list it" reason a SKU
    # with no OPTIGA at all gets.
    assert "does not list it" not in msg


def test_optiga_attestation_root_accepted_where_the_part_is_fitted(
    tmp_path: Path,
) -> None:
    """E1M-AEN401 fits OPTIGA -- the attestation root still loads."""
    from tan.planner import load_board_yaml

    project = load_board_yaml(_write_board(tmp_path, "E1M-AEN401"))
    assert project.sku == "E1M-AEN401"


@pytest.mark.parametrize(
    ("devices", "expected"),
    [
        # Every entry DNP -> unassembled.
        ([{"chip": "optiga_trust_m", "assembled": False}], True),
        # Fitted (explicit True or the key absent) -> not unassembled.
        ([{"chip": "optiga_trust_m", "assembled": True}], False),
        ([{"chip": "optiga_trust_m"}], False),
        # One fitted entry among DNP ones -> the part is on the module.
        ([{"chip": "optiga_trust_m", "assembled": False},
          {"chip": "optiga_trust_m"}], False),
        # Never listed -> not "unassembled"; the presence checks decide.
        ([{"chip": "tmp112", "assembled": False}], False),
        ([], False),
    ],
)
def test_is_i2c_chip_unassembled_only_refuses_an_explicit_dnp(
    devices: list[dict], expected: bool,
) -> None:
    from tan.planner.loader import _is_i2c_chip_unassembled

    preset = {"on_module": {"i2c_devices": {"brd_i2c": {"devices": devices}}}}
    assert _is_i2c_chip_unassembled(preset, "optiga_trust_m") is expected


def test_is_i2c_chip_unassembled_tolerates_a_preset_with_no_i2c_devices() -> None:
    from tan.planner.loader import _is_i2c_chip_unassembled

    assert _is_i2c_chip_unassembled({}, "optiga_trust_m") is False
    assert _is_i2c_chip_unassembled(
        {"on_module": {"i2c_devices": None}}, "optiga_trust_m") is False
