# SPDX-License-Identifier: Apache-2.0
import pytest
from tan.model.manifest import Manifest, Target
from tan.model.package import write_package, read_package, MAGIC
from tan.model._gen_fixture import (
    build_fixture_bytes,
    build_onnx_cpu_fixture_bytes,
    to_c_header,
)
from tests.conftest import sdk_root


def _mft() -> Manifest:
    return Manifest(
        name="m", src_sha=bytes(32),
        targets=[Target("ethos_u", "alif:ensemble:e8", "vela_tflite", "ethos-u85-256",
                        524288, {"sram_kib": 512, "op_features": []}, 0),
                 Target("cpu", "*", "tflite", "", 786432, {"sram_kib": 0, "op_features": []}, 1)],
    )


def test_package_roundtrips_manifest_and_blobs():
    blobs = [b"VELA-BLOB-BYTES", b"TFLITE-BLOB"]
    raw = write_package(_mft(), blobs)
    assert raw[:4] == MAGIC
    mft, got_blobs = read_package(raw)
    assert mft == _mft()
    assert got_blobs == blobs                    # retrieved by table order == blob index


# In alp-sdk this was `Path(__file__).resolve().parents[2]` -- two levels up
# from `tests/scripts/test_alp_model_package.py` landed on the alp-sdk repo
# root, where `_gen_fixture.py`'s own hardcoded output paths
# (`tests/fixtures/alpmodel/minimal.alpmodel`,
# `tests/unit/alpmodel_reader/src/fixture.h`, `tests/yocto/onnx_cpu_fixture.h`)
# live. Re-anchoring that same `parents[2]` walk on THIS file
# (`python/tests/model/test_package.py`) lands on tan's `python/` dir, not a
# repo root, and none of those three paths exist there -- `tests/unit/` and
# `tests/yocto/` are Zephyr/Yocto C-test trees with no tan-cli equivalent
# (ADR-0028 Decision-3: the on-device `.alpmodel` C reader, and the tests that
# exercise it, stay in alp-sdk).
#
# So these two tests do NOT become tan-local; they stay what they always were,
# a check that the (now relocated) Python encoder's bytes match alp-sdk's
# COMMITTED C fixtures -- a genuinely cross-repo invariant now that the
# generator that used to keep them in sync lives on this side. Read straight
# out of a bound alp-sdk checkout, the same way every other cross-repo test in
# this suite does (`tests.conftest.sdk_root()`); skip, loudly, without one --
# public CI has no alp-sdk checkout to bind.
_SDK = sdk_root()

_needs_bound_sdk = pytest.mark.skipif(
    _SDK is None,
    reason="set ALP_SDK_ROOT to an alp-sdk checkout: these fixtures are "
           "alp-sdk's own committed C headers (tests/unit/alpmodel_reader/, "
           "tests/yocto/), which this relocated encoder must still match.",
)


@_needs_bound_sdk
def test_committed_fixture_matches_generator():
    raw = build_fixture_bytes()
    on_disk = (_SDK / "tests/fixtures/alpmodel/minimal.alpmodel").read_bytes()
    assert raw == on_disk, "regenerate: python -m tan.model._gen_fixture --root <alp-sdk-checkout>"
    header = (_SDK / "tests/unit/alpmodel_reader/src/fixture.h").read_text()
    assert to_c_header(raw) == header, \
        "regenerate: python -m tan.model._gen_fixture --root <alp-sdk-checkout>"


@_needs_bound_sdk
def test_committed_onnx_cpu_fixture_matches_generator():
    # Issue #1254: tests/yocto/alpmodel_onnx_cpu.c's committed byte array
    # (tests/yocto/onnx_cpu_fixture.h) must stay GENERATED, not hand-typed
    # bytes with a generation command parked only in a comment -- a
    # container-format change that forgets to regenerate it fails here.
    raw = build_onnx_cpu_fixture_bytes()
    header = (_SDK / "tests/yocto/onnx_cpu_fixture.h").read_text()
    assert to_c_header(raw, array_name="k_onnx_cpu_alpmodel",
                        guard="ALP_MODEL_ONNX_CPU_FIXTURE_H") == header, \
        "regenerate: python -m tan.model._gen_fixture --root <alp-sdk-checkout>"


def test_bad_magic_rejected():
    raw = bytearray(write_package(_mft(), [b"x", b"y"]))
    raw[0] = ord("Z")
    with pytest.raises(ValueError, match="bad magic"):
        read_package(bytes(raw))


def test_unsupported_version_rejected():
    raw = bytearray(write_package(_mft(), [b"x", b"y"]))
    raw[4] = 99                                   # container_v low byte
    with pytest.raises(ValueError, match="unsupported container version"):
        read_package(bytes(raw))
