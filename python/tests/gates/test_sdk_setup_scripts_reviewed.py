# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1559: every Zephyr SDK minimal bundle pinned by the alp-sdk lock tan
is tested against has had its setup scripts read by a person first.

Since tan-cli#1556, `tan bootstrap` installs the pinned minimal bundle itself
(`bootstrap_toolchain_fetch.install_pinned_minimal_sdk`) and NEVER runs the
bundle's `setup.sh` / `setup.cmd`. That is correct for the bundles reviewed
below: with `west sdk install --no-gnu-toolchains --no-hosttools` the only step
west ever ran was `setup -c`, i.e. CMake package registration, which tan's
builds do not need (they set `ZEPHYR_SDK_INSTALL_DIR` from the store). The
remaining steps were read too: `-t` (toolchain fetch -- tan does it, pinned),
`-h` (host tools -- optional, skipped deliberately), `-l` (LLVM -- unused) and
`-o` (links for Zephyr < 4.3 -- irrelevant on Zephyr 4.4.x).

A future SDK release can add a step a usable install depends on, and nothing
else would notice: the bundle is sha256-pinned, so new setup scripts arrive
only with a new pin. This gate turns such a pin red until the new bundle's
scripts are read and recorded here. It reads two locks:

- `contract/fixtures/toolchains/toolchains.json`, always. It is kept
  byte-identical to the pinned alp-sdk's `metadata/toolchains.json` by
  `tests/parity/toolchain_lock_parity.py` (seam1), so re-vendoring it for a
  new pin fires this.
- the bound `ALP_SDK_ROOT` checkout's `metadata/toolchains.json`, when there
  is one (CI binds alp-sdk `dev`). That leg catches an alp-sdk pin bump before
  anyone re-vendors the fixture. Unbound, it SKIPS, naming the variable.

What it cannot see: at runtime tan installs whatever `minimal-sdk` row the
USER's SDK checkout pins, which may be any alp-sdk commit. A bundle no alp-sdk
commit this repo is tested against has pinned reaches users unreviewed.

To clear it, for each new `minimal-sdk` archive:
1. Download it, check its sha256 against the pin, and extract it.
2. Read `setup.sh` (Linux/macOS bundles) or `setup.cmd` (Windows) end to end.
   Note every step it can run, and which flag selects it.
3. Confirm a usable SDK needs none of them beyond what tan already does: the
   `gnu/<toolchain>` layout comes from the separately pinned toolchain archive,
   and `sdk_version` plus `cmake/` come from the bundle itself. If one IS
   needed, implement it in tan before recording the pin.
4. Add the sha256 to `REVIEWED_MINIMAL_SDK_PINS` with the date and issue.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import sdk_root

#: Resolved at IMPORT: the autouse `_scrub_sdk_discovery_env` deletes
#: `ALP_SDK_ROOT` before every test body runs (tan-cli#275).
SDK = sdk_root()

#: version -> host -> the `minimal-sdk` sha256 whose setup script was read.
#: 1.0.1: all three read 2026-10-19 for tan-cli#1556/#1559, from the real
#: extracted bundles: `setup.cmd` (Windows, sha256 8ea5f94a42ab...), and
#: `setup.sh`, byte-identical in the Linux and macOS bundles (both sha256
#: 17bb946103e7...).
REVIEWED_MINIMAL_SDK_PINS: dict[str, dict[str, str]] = {
    "1.0.1": {
        "linux-x86_64": "ca9bc0ff66fafca1dac9d592a36d953cf16d096a9d09b1c0357f021cf9f6a7eb",
        "macos-aarch64": "867063901f39528a6175a80ebc20367bd6cb440593e7e2650eda30392f1f6b65",
        "windows-x86_64": "17a557e7c6cea5e5c589508fe45a3f26aca4fa2c6153fe3e7b03aeb4b93fea4b",
    },
}

_FIXTURE = Path(__file__).resolve().parents[3] / "contract" / "fixtures" / "toolchains" / "toolchains.json"


def _pinned_minimal_sdks(lock: Path = _FIXTURE) -> tuple[str, dict[str, str]]:
    doc = json.loads(lock.read_text(encoding="utf-8"))["zephyrSdk"]
    rows = {
        a["host"]: a["sha256"].strip().lower()
        for a in doc["artifacts"]
        if a["component"] == "minimal-sdk"
    }
    return doc["version"], rows


def _assert_reviewed(version: str, pinned: dict[str, str], where: str) -> None:
    reviewed = REVIEWED_MINIMAL_SDK_PINS.get(version, {})

    unreviewed = sorted(
        f"{host}: {sha}" for host, sha in pinned.items() if reviewed.get(host) != sha
    )

    assert not unreviewed, (
        f"{where} pins Zephyr SDK {version} minimal bundles whose setup scripts "
        f"nobody has read since tan stopped running them (tan-cli#1556): "
        f"{unreviewed}. Follow the steps in this file's docstring, then record "
        f"each sha256 in REVIEWED_MINIMAL_SDK_PINS."
    )


def test_every_minimal_sdk_bundle_the_vendored_lock_pins_has_been_reviewed():
    _assert_reviewed(*_pinned_minimal_sdks(), "contract/fixtures/toolchains/toolchains.json")


@pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT / ALP_SDK_PARITY_ROOT unset -- the bound alp-sdk's own "
    "toolchain lock was not checked (the vendored one still was)",
)
def test_every_minimal_sdk_bundle_the_bound_alp_sdk_pins_has_been_reviewed():
    lock = SDK / "metadata" / "toolchains.json"
    _assert_reviewed(*_pinned_minimal_sdks(lock), f"{lock}")


def test_the_fixture_pins_a_minimal_bundle_for_every_host_it_pins_a_toolchain_for():
    """tan refuses a host with a toolchain row but no `minimal-sdk` row
    (tan-cli#1556), so the vendored pin must never carry that shape."""
    doc = json.loads(_FIXTURE.read_text(encoding="utf-8"))["zephyrSdk"]
    hosts = {a["host"] for a in doc["artifacts"]}
    with_bundle = {a["host"] for a in doc["artifacts"] if a["component"] == "minimal-sdk"}

    assert hosts == with_bundle
