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
Task 6) -- see test_deepx_yolo_internal.py's docstring for why. The
release-checklist change this docstring first recorded as OWED, then wrongly
recorded as already made, is made in alplabai/alp-lab-plugin#65, landed as
`99bdb4e9` (tan-cli#785). `cutting-a-tan-release`'s pre-tag checklist names all
three node IDs -- this test's two, plus its DeepX sibling's -- and has the
releaser run them with `-v` and RECORD the per-node-id result. To find that row
in the skill, grep for
`test_vela_compiles_real_model_for_e8` rather than for prose: a heading can be
reworded silently from the other repo, a node ID cannot drift without this
file's own tests failing first. A
SKIP does not block the tag; an UNRECORDED result does. So a skip here is READ
at a cut, and every skip condition in this file has to be worth reading --
which is why the public fixture is named rather than globbed below.

The grading shape the stale version of this docstring proposed -- `-rA`, then
`grep -c '^PASSED'` == 3 and `grep -c '^SKIPPED'` == 0 -- was NOT adopted,
because that second count is not a test count: `-rA` groups skips by
(location, reason), so this file's two node IDs share ONE `SKIPPED [2] ...`
line and a fully-skipped run of all three prints 2, not 3. Measured, not
reasoned. The checklist reads per-node-id `-v` lines instead, which are one
per test by construction.

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


#: The public fixture, NAMED, not globbed (tan-cli#791). It used to be reached
#: with `glob("*_int8.tflite")` over the same directory, and alp-sdk's own
#: 712-byte, 1-operator `tiny_int8.tflite` -- the toy this proof exists to be
#: MORE than -- matches that pattern. Bound to an alp-sdk from before
#: `person_detect_int8.tflite` landed (alp-sdk `4fd5fab5`, alplabai/alp-sdk#1470,
#: still open), the glob therefore did not skip: it silently substituted the
#: toy and compiled THAT, which is the worse failure of the two this file can
#: have. Measured, flagless `VelaAdapter().compile()` on `ethos-u-vela` 5.1.0:
#:
#:   tiny_int8.tflite          ethos-u85-256  REFUSED (0 KiB SRAM)
#:                             ethos-u55-256  req_sram_kib=1  arena_bytes=32     <- "PASSED"
#:   person_detect_int8.tflite ethos-u85-256  req_sram_kib=73 arena_bytes=74480
#:                             ethos-u55-256  req_sram_kib=73 arena_bytes=74480
#:
#: i.e. one half of the pair went red for the right reason while the OTHER
#: half reported PASSED on a 712-byte toy -- and `cutting-a-tan-release`'s
#: release checklist reads exactly that word, PER NODE ID, as the evidence
#: that the real-model proofs ran. Naming the file makes the absent case a
#: SKIP the releaser has to record, instead of a pass nothing downstream can
#: distinguish from the real thing.
_PUBLIC_REAL_MODEL = (
    (_SDK / "tests/fixtures/models/person_detect_int8.tflite") if _SDK is not None else None
)
_INTERNAL_MODELS_DIR = _internal_root() / "vendors/alif-ethos-u/sample-models"


def _real_int8_models() -> list[Path]:
    """Every real (non-toy) int8 .tflite reachable right now: the public
    alp-sdk fixture first (so it's model[0], the one the test below actually
    compiles), then any private alp-sdk-internal sample models.

    The private directory keeps its glob -- it is a directory OF licensed
    sample models with no toy in it, and its contents are not this repo's to
    enumerate. The public side is a single named file for the reason above."""
    found: list[Path] = []
    if _PUBLIC_REAL_MODEL is not None and _PUBLIC_REAL_MODEL.is_file():
        found.append(_PUBLIC_REAL_MODEL)
    if _INTERNAL_MODELS_DIR.is_dir():
        found += sorted(_INTERNAL_MODELS_DIR.glob("*_int8.tflite"))
    return found


@pytest.mark.skipif(shutil.which("vela") is None, reason="vela (ethos-u-vela) not installed")
@pytest.mark.skipif(not _real_int8_models(),
                    reason="no real int8 model reachable: set ALP_SDK_ROOT to an "
                           "alp-sdk carrying tests/fixtures/models/"
                           "person_detect_int8.tflite (alp-sdk 4fd5fab5, "
                           "alplabai/alp-sdk#1470, still open -- an SDK pinned "
                           "before it ships only the 1-op tiny_int8.tflite toy, "
                           "which is deliberately NOT accepted here), or set "
                           "ALP_SDK_INTERNAL to an alp-sdk-internal checkout "
                           "with private sample-models")
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
    # A real (non-toy) model yields a real, nonzero reported footprint on
    # BOTH figures -- not just one or the other. A regression to the KiB/bytes
    # unit bug (arena sourced from vela's `arena_cache_size` config knob, and
    # `sram_memory_used` misread as bytes instead of KiB) silently zeroed
    # req_sram_kib while leaving arena_bytes looking plausible (384), so this
    # must assert req_sram_kib specifically, not just "either one is nonzero".
    assert blob.req_sram_kib > 0
    assert blob.arena_bytes > 0
