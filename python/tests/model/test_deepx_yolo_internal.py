# SPDX-License-Identifier: Apache-2.0
"""Real-model proof: compile a real yolo11n with dxcom (alp-sdk-internal fixture).

The fixture (`yolo11n.onnx`, ~10 MB) lives in the PRIVATE alp-sdk-internal repo
(`vendors/deepx-dxm1/sample-models/yolo11n/`), not the public tree. This test
runs only when that sibling repo is checked out AND the licensed dx-com wheel is
installed AND host RAM clears dxcom's ~15 GiB floor -- i.e. on a maintainer's
box, never in cloud CI. It is the SoM-credibility proof that the DEEPX pipeline
compiles a real production model (a YOLO11n object detector), not just the tiny
hermetic fixture in test_adapters.py.

Moved here from alp-sdk's tests/scripts/test_deepx_yolo_internal.py (ADR-0028
Task 6): the customer-facing claim this test defends -- "a real production
model compiles for this SoM" -- is broken by a change to the engine, and the
engine is tan's now. A proof living in alp-sdk would detect that break late, in
a different repo, on a different release train.

Because this test only ever runs on a maintainer's box, it is one release away
from being no proof at all if nobody runs it. `cutting-a-tan-release`'s
checklist closes that in alplabai/alp-lab-plugin#65 (tan-cli#785; companion
PRs, merged together -- re-check that one is IN before trusting this): its
"real-model proofs" section -- grep the skill for that phrase, not for its
heading, which carries an em dash this ASCII docstring cannot reproduce --
names this test's node ID and its Vela sibling's two, and requires the releaser
RUN all three with `-v`
and RECORD the per-node-id result before a tag. A SKIP does not block the tag
-- this test cannot pass without the licensed `dx-com` wheel (the binary it
puts on PATH, and what the skipif probes, is `dxcom`), the private fixture, AND
host RAM over dxcom's ~15 GiB floor. That last one is not merely "not
guaranteed": `_host_mem_avail_gib()` below reads `/proc/meminfo` and returns
`0.0` on OSError, so on macOS and Windows -- where that file does not exist --
the RAM skipif is unconditionally true and this node ID can NEVER pass, however
much RAM is installed. A release cut on a darwin box will always record this
one as SKIPPED, correctly. But an UNRECORDED result DOES block. Running it is
no longer a maintainer choice that leaves no trace either way.

NOTE: a real yolo11n compile takes ~5 min at dxcom's default opt_level.

Run (from a dx-com venv, with alp-sdk-internal beside this checkout):
    ALP_SDK_INTERNAL=../alp-sdk-internal \\
      ~/dxcom-venv/bin/python -m pytest tests/model/test_deepx_yolo_internal.py
"""
import json
import os
import shutil
from pathlib import Path

import pytest

from tan.model.adapters.deepx import DeepxAdapter

# `python/tests/model/test_deepx_yolo_internal.py` is FOUR levels below the
# tan-cli repo root (model -> tests -> python -> root), not two: the
# alp-sdk-original's `parents[2]` walk landed on the alp-sdk repo root from
# `tests/scripts/`, which is a different depth than this file's home. A
# `_gen_fixture.py` fix earlier in this same migration shipped exactly this
# bug once already (a naive parents[2] port); this file does not repeat it.
_ROOT = Path(__file__).resolve().parents[3]


def _internal_root() -> Path:
    """alp-sdk-internal location: $ALP_SDK_INTERNAL, else the sibling default.

    The sibling relationship is the same regardless of which checkout (alp-sdk
    or tan-cli) `_ROOT` names: both live directly under the same parent
    directory as alp-sdk-internal on a maintainer's box."""
    env = os.environ.get("ALP_SDK_INTERNAL")
    return Path(env) if env else _ROOT.parent / "alp-sdk-internal"


_YOLO_ONNX = _internal_root() / "vendors/deepx-dxm1/sample-models/yolo11n/yolo11n.onnx"


def _host_mem_avail_gib() -> float:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except OSError:
        pass
    return 0.0


@pytest.mark.skipif(shutil.which("dxcom") is None, reason="dxcom (dx-com wheel) not installed")
@pytest.mark.skipif(not _YOLO_ONNX.is_file(),
                    reason="alp-sdk-internal yolo11n fixture absent (set ALP_SDK_INTERNAL)")
@pytest.mark.skipif(_host_mem_avail_gib() < 15.5, reason="dxcom needs >15 GiB host RAM")
def test_deepx_compiles_real_yolo11n(tmp_path):
    """yolo11n.onnx (input images[1,3,640,640]) -> a single ~7 MB .dxnn via dxcom."""
    import numpy as np
    from PIL import Image          # Pillow ships as a dx-com wheel dependency

    calib = tmp_path / "calib"
    calib.mkdir()
    rng = np.random.default_rng(0)
    for i in range(8):
        Image.fromarray(rng.integers(0, 256, (640, 640, 3), dtype=np.uint8)).save(calib / f"{i}.png")

    cfg = tmp_path / "yolo11n.json"
    cfg.write_text(json.dumps({
        "inputs": {"images": [1, 3, 640, 640]},
        "calibration_method": "minmax",
        "calibration_num": 8,
        "default_loader": {
            "dataset_path": str(calib),
            "file_extensions": ["png"],
            "preprocessings": [
                {"resize": {"width": 640, "height": 640}},
                {"normalize": {"mean": [0, 0, 0], "std": [255, 255, 255]}},
                {"transpose": {"axis": [2, 0, 1]}},      # HWC->CHW for the NCHW model
            ],
        },
    }), encoding="utf-8")

    blob = DeepxAdapter().compile(_YOLO_ONNX, accel_config="", out_dir=tmp_path,
                                  opts={"config": str(cfg)})
    assert blob.format == "dxnn"
    assert blob.payload[:4] == b"DXNN"            # self-describing .dxnn flatbuffer magic
    assert len(blob.payload) > 1_000_000          # a real yolo11n .dxnn is ~7 MB
    assert blob.compiler_version.startswith("DX-COM")
