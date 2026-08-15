# SPDX-License-Identifier: Apache-2.0
"""Real-model proof: compile a real int8 model with Vela for the E8 Ethos-U85.

Two fixture sources feed this test, either of which is enough for it to run:

  1. The PUBLIC alp-sdk fixture,
     ``tests/fixtures/models/person_detect_int8.tflite`` -- the canonical
     TFLite-Micro "person detect" MobileNet (Apache-2.0, Google/TensorFlow
     Authors; see that file's own PERSON_DETECT-PROVENANCE.txt for the
     upstream revision + sha256). Reached via ``ALP_SDK_ROOT`` naming an
     alp-sdk checkout. This is the one that makes the test actually PASS on
     a bare maintainer box with no private sibling checked out.
  2. The PRIVATE alp-sdk-internal repo
     (``$ALP_SDK_INTERNAL/vendors/alif-ethos-u/sample-models/*_int8.tflite``)
     -- for a licensed/production model that cannot be made public (mirrors
     ``test_deepx_yolo_internal.py``'s reasoning for its own DeepX fixture).

This test still skips cleanly (not errors) when neither source resolves, or
when `vela` (ethos-u-vela) is not installed -- i.e. it degrades to a skip in
cloud CI, which has neither. Where it DOES resolve, it is the SoM-credibility
proof that the Ethos-U/Vela pipeline compiles a real production model for the
AEN801 (E8) accelerator configs, not just the tiny hermetic fixture in
test_adapters.py.

Moved here from alp-sdk's tests/scripts/test_vela_yolo_internal.py (ADR-0028
Task 6) -- see test_deepx_yolo_internal.py's docstring for why. NOTE: whether
`cutting-a-tan-release`'s checklist asserts this test (and its DeepX sibling)
report PASSED, not skipped, before a release is cut is a release-checklist
change still OWED to that skill (it lives in the alp-lab plugin, outside this
repo) -- not yet made, so no release gate currently enforces that either test
ran. Today this test running is a maintainer choice, not a guarantee.

Run (with an alp-sdk checkout beside this one, vela on PATH):
    ALP_SDK_ROOT=../alp-sdk \\
      python -m pytest tests/model/test_vela_yolo_internal.py
Or, for a licensed/private model instead:
    ALP_SDK_INTERNAL=../alp-sdk-internal \\
      python -m pytest tests/model/test_vela_yolo_internal.py
"""
import os
import shutil
from pathlib import Path

import pytest

from tan.model.adapters.ethos_u import VelaAdapter
from tests.conftest import sdk_root

# See test_deepx_yolo_internal.py's comment on this depth: FOUR levels below
# the tan-cli repo root, not the alp-sdk original's two.
_ROOT = Path(__file__).resolve().parents[3]

# Module-level, like test_package.py's `_SDK = sdk_root()` -- `sdk_root()`
# must be called before collection's autouse fixtures run and scrub
# ALP_SDK_ROOT from the environment (tests/conftest.py's
# `_scrub_sdk_discovery_env`), so it has to happen at import time, not from
# inside a test body or a skipif evaluated lazily at call time.
_SDK = sdk_root()


def _internal_root() -> Path:
    """alp-sdk-internal location: $ALP_SDK_INTERNAL, else the sibling default."""
    env = os.environ.get("ALP_SDK_INTERNAL")
    return Path(env) if env else _ROOT.parent / "alp-sdk-internal"


_PUBLIC_MODELS_DIR = (_SDK / "tests/fixtures/models") if _SDK is not None else None
_INTERNAL_MODELS_DIR = _internal_root() / "vendors/alif-ethos-u/sample-models"


def _real_int8_models() -> list[Path]:
    """Every real (non-toy) int8 .tflite reachable right now: the public
    alp-sdk fixture first (so it's model[0], the one the test below actually
    compiles), then any private alp-sdk-internal sample models."""
    found: list[Path] = []
    if _PUBLIC_MODELS_DIR is not None and _PUBLIC_MODELS_DIR.is_dir():
        found += sorted(_PUBLIC_MODELS_DIR.glob("*_int8.tflite"))
    if _INTERNAL_MODELS_DIR.is_dir():
        found += sorted(_INTERNAL_MODELS_DIR.glob("*_int8.tflite"))
    return found


@pytest.mark.skipif(shutil.which("vela") is None, reason="vela (ethos-u-vela) not installed")
@pytest.mark.skipif(not _real_int8_models(),
                    reason="no real int8 model reachable: set ALP_SDK_ROOT (public "
                           "tests/fixtures/models/person_detect_int8.tflite) or "
                           "ALP_SDK_INTERNAL (private sample-models dir)")
@pytest.mark.parametrize("accel_config", ["ethos-u85-256", "ethos-u55-256"])
def test_vela_compiles_real_model_for_e8(tmp_path, accel_config):
    """A real int8 .tflite -> a vela_tflite blob for the E8 accel configs.

    Proves op-support + a real arena/SRAM footprint on a production-scale model
    (NOT on-device correctness, which is silicon + Ethos-U HAL gated).
    """
    model = _real_int8_models()[0]
    src = tmp_path / model.name
    shutil.copy(model, src)
    blob = VelaAdapter().compile(src, accel_config=accel_config, out_dir=tmp_path)
    assert blob.format == "vela_tflite"
    assert blob.payload[4:8] == b"TFL3"
    # A real (non-toy) model yields a real reported footprint.
    assert blob.arena_bytes > 0 or blob.req_sram_kib > 0
