# SPDX-License-Identifier: Apache-2.0
"""alp-sdk#2469 (alp-sdk `da6c08db4`): `CONFIG_ALP_SDK_SOC_CPUS` tags each core
with `|<zephyr_cpucluster>` so the firmware banner (`alp_banner.c`,
`src/zephyr/alp_soc_cpus.h`) can move the `(active)` marker to the core the
image is actually built for, instead of trusting the board.yaml slice the
example passed. alp-sdk pins it through its emit-snapshot goldens and a
Zephyr unit test; this pins the planner string directly.

Pure function, but `tan.planner.kconfig` cannot be imported before a root is
bound (tan/planner_root.py), so it skips without a real checkout.
"""

from __future__ import annotations

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- tan.planner.kconfig needs a bound root to import.",
)

_SOC = {
    "cores": [
        {"id": "m55_hp", "type": "cortex-m55", "freq_mhz": 400,
         "zephyr_cpucluster": "rtss_hp"},
        {"id": "a32_cluster", "type": "cortex-a32", "count": 2,
         "freq_mhz": 800},
        {"id": "m55_he", "type": "cortex-m55", "freq_mhz": 160,
         "zephyr_cpucluster": "rtss_he"},
    ],
}


def test_each_core_carries_its_cluster_tag_and_the_active_one_leads() -> None:
    from tan.planner.kconfig import _core_display_label, _soc_cpu_complement

    out = _soc_cpu_complement(_SOC, "m55_he")
    parts = out.split(" + ")

    he = _core_display_label(_SOC["cores"][2])
    hp = _core_display_label(_SOC["cores"][0])
    a32 = _core_display_label(_SOC["cores"][1])
    assert parts == [
        f"{he} @160MHz (active)|rtss_he",
        f"{hp} @400MHz|rtss_hp",
        f"2x {a32} @800MHz",
    ]


def test_a_core_with_no_cluster_gets_no_tag() -> None:
    """The `|` separator appears only when the SoC JSON names a cluster --
    an untagged core keeps the pre-#2469 shape exactly."""
    from tan.planner.kconfig import _soc_cpu_complement

    out = _soc_cpu_complement(
        {"cores": [{"id": "a32_cluster", "type": "cortex-a32", "count": 2,
                    "freq_mhz": 800, "zephyr_cpucluster": ""}]},
        "a32_cluster")
    assert "|" not in out
    assert out.endswith("@800MHz (active)")
