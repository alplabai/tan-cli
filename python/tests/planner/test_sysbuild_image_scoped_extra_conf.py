# SPDX-License-Identifier: Apache-2.0
"""alp-sdk#866 (alp-sdk `423e0100d`): a `--sysbuild` Zephyr slice carries the
per-core alp.conf as the IMAGE-scoped `-D<image>_EXTRA_CONF_FILE=`, where
`<image>` is the basename of the app directory `west build` is handed --
ported from alp-sdk's `tests/scripts/test_orchestrate_buildplan.py::
test_zephyr_slice_command_wires_sysbuild_overlay`.

Before #866 a sysbuild slice carried no alp.conf define at all: a bare
`-DEXTRA_CONF_FILE` lands on the SYSBUILD image rather than the
application, so the planner dropped it and relied on each example's
`--core`-scoped CMakeLists.txt bridge (#870). Sysbuild names the app image
after the app directory's basename, so the planner now emits the
image-prefixed form itself. A non-sysbuild slice keeps the bare
`-DEXTRA_CONF_FILE=`.

Real-SDK-gated: needs the real E1M-V2N101 preset from a bound checkout.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- the plan is emitted from the real E1M-V2N101 preset.",
)

_V2N_BOOT_MCUBOOT = """
som:
  sku: E1M-V2N101

cores:
  m33_sm:
    os: zephyr
    app: ./m33

boot:
  method: mcuboot
  signing:
    algorithm: rsa2048
    key_file: keys/dev_rsa.pem
"""

_V2N_NO_BOOT = """
som:
  sku: E1M-V2N101

cores:
  m33_sm:
    os: zephyr
    app: ./m33
"""


def _zephyr_slice(tmp_path: Path, body: str, name: str) -> dict:
    from tan.planner import load_board_yaml
    from tan.planner.buildplan import emit_build_plan

    path = tmp_path / name
    path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    plan = json.loads(emit_build_plan(
        load_board_yaml(path), board_yaml=path, build_root=Path("build")))
    return next(s for s in plan["slices"]
                if s["backend"] == "zephyr" and s["command"])


def test_sysbuild_slice_carries_the_image_scoped_extra_conf_file(
    tmp_path: Path,
) -> None:
    z = _zephyr_slice(tmp_path, _V2N_BOOT_MCUBOOT, "board.yaml")
    args = z["command"]["args"]

    assert "--sysbuild" in args
    assert args[-4:-1] == [
        "--",
        "-DPython3_EXECUTABLE=${PYTHON}",
        "-DSB_CONF_FILE=${PROJECT_ROOT}/build/alp_sysbuild.conf",
    ]
    assert "\\" not in args[-2]
    # Never the bare form -- it would land on the sysbuild image.
    assert not any(a.startswith("-DEXTRA_CONF_FILE=") for a in args)
    # `app: ./m33` -> sysbuild's application image is named `m33`.
    assert args[-1] == (
        f"-Dm33_EXTRA_CONF_FILE=${{PROJECT_ROOT}}/{z['buildDir']}/alp.conf")


def test_non_sysbuild_slice_keeps_the_bare_extra_conf_file(
    tmp_path: Path,
) -> None:
    z = _zephyr_slice(tmp_path, _V2N_NO_BOOT, "board-noboot.yaml")
    args = z["command"]["args"]

    assert "--sysbuild" not in args
    assert not any(a.startswith("-DSB_CONF_FILE=") for a in args)
    assert args[-3] == "--"
    assert args[-2] == "-DPython3_EXECUTABLE=${PYTHON}"
    assert args[-1].startswith("-DEXTRA_CONF_FILE=")
    assert args[-1].endswith(f"/{z['buildDir']}/alp.conf")
    assert not any("_EXTRA_CONF_FILE=" in a for a in args)
