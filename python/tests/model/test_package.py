# SPDX-License-Identifier: Apache-2.0
import re

import cbor2
import pytest
from tan.model.manifest import Manifest, Target
from tan.model.package import (
    CONTAINER_VERSION,
    MAGIC,
    read_manifest_file,
    read_package,
    write_package,
)
from tan.model._gen_fixture import (
    build_fixture_bytes,
    build_onnx_cpu_fixture_bytes,
    to_c_header,
)
from tests.conftest import sdk_root

#: A real, verbatim `Blob.caveats` entry -- vela 5.1.0's own default-profile
#: verdict, as `tan.model.adapters.ethos_u._default_profile_caveats` words it.
#: Used here as manifest CONTENT; the compile that produces it is pinned by
#: `tests/model/test_build.py`'s real-vela end-to-end test.
_CAVEAT = ('vela used its BUILT-IN default profile (system-config '
           'Ethos_U55_High_End_Embedded, memory-mode Shared_Sram), not one authored '
           'for this module -- vela\'s own warning for that is "Compilation may be '
           'invalid or non-optimal". The arena/SRAM figures and the compiled command '
           "stream describe that default memory model, not this module's.")


def _mft() -> Manifest:
    return Manifest(
        name="m", src_sha=bytes(32),
        targets=[Target("ethos_u", "alif:ensemble:e8", "vela_tflite", "ethos-u85-256",
                        524288, {"sram_kib": 512, "op_features": []}, 0),
                 Target("cpu", "*", "tflite", "", 786432, {"sram_kib": 0, "op_features": []}, 1)],
    )


def _manifest_of(raw: bytes) -> dict:
    """The RAW CBOR manifest of a container, decoded with no help from
    `tan.model.manifest` -- so a test can assert what is actually on the wire
    rather than what the dataclass round-trips to."""
    import struct
    _, _, _, mft_off, mft_len, _, _ = struct.Struct("<4sHHIIII").unpack_from(raw, 0)
    return cbor2.loads(raw[mft_off:mft_off + mft_len])


def test_package_roundtrips_manifest_and_blobs():
    blobs = [b"VELA-BLOB-BYTES", b"TFLITE-BLOB"]
    raw = write_package(_mft(), blobs)
    assert raw[:4] == MAGIC
    mft, got_blobs = read_package(raw)
    assert mft == _mft()
    assert got_blobs == blobs                    # retrieved by table order == blob index


# --------------------------------------------------------------------------
# `Target.caveats` -- the additive manifest field, and the compatibility
# property the whole design rests on.
# --------------------------------------------------------------------------


def test_target_caveats_round_trip_through_the_container():
    """A caveat written into a target comes back out of the container
    verbatim -- the package self-describes, so a blob compiled against a
    memory model the module does not have cannot ship silent."""
    mft = _mft()
    mft.targets[0].caveats = [_CAVEAT]
    got, _ = read_package(write_package(mft, [b"A", b"B"]))
    assert got.targets[0].caveats == [_CAVEAT]
    assert got.targets[1].caveats == []
    assert got == mft


def test_an_empty_caveats_list_is_absent_from_the_wire_entirely():
    """THE COMPATIBILITY PROPERTY. `caveats` is emitted only when it has
    something to say, so a package with no caveated target encodes to exactly
    the bytes it did before the field existed.

    That is what lets this field be added WITHOUT moving
    `package.CONTAINER_VERSION` and without regenerating alp-sdk's three
    committed C-test fixtures -- pinned from the other side by
    `test_committed_fixture_matches_generator` below, which compares this
    encoder's output against files generated before `caveats` existed."""
    wire = _manifest_of(write_package(_mft(), [b"A", b"B"]))
    for target in wire["targets"]:
        assert "caveats" not in target, target
    # ...and present, as a flat list of text, the moment there IS one.
    mft = _mft()
    mft.targets[0].caveats = [_CAVEAT]
    wire = _manifest_of(write_package(mft, [b"A", b"B"]))
    assert wire["targets"][0]["caveats"] == [_CAVEAT]
    assert all(isinstance(c, str) for c in wire["targets"][0]["caveats"])
    assert "caveats" not in wire["targets"][1]


def test_a_manifest_written_before_the_field_existed_reads_as_no_caveats():
    """The other direction: alp-sdk's committed `minimal.alpmodel` was
    generated before `caveats` existed and carries the key nowhere. Absent
    must mean "no caveats", never a decode error."""
    mft, _ = read_package(build_fixture_bytes())
    assert [t.caveats for t in mft.targets] == [[], []]


def test_read_manifest_file_returns_the_manifest_without_the_blobs(tmp_path):
    """`tan model build`'s readback path: same manifest as `read_package`,
    without paying a copy of every compiled blob to get it."""
    mft = _mft()
    mft.targets[0].caveats = [_CAVEAT]
    path = tmp_path / "m.alpmodel"
    path.write_bytes(write_package(mft, [b"A" * 4096, b"B" * 4096]))
    assert read_manifest_file(path) == mft


def test_read_manifest_file_rejects_a_foreign_container(tmp_path):
    path = tmp_path / "m.alpmodel"
    raw = bytearray(write_package(_mft(), [b"A"]))
    raw[0] = ord("Z")
    path.write_bytes(bytes(raw))
    with pytest.raises(ValueError, match="bad magic"):
        read_manifest_file(path)
    raw[0] = MAGIC[0]
    raw[4] = 99
    path.write_bytes(bytes(raw))
    with pytest.raises(ValueError, match="unsupported container version"):
        read_manifest_file(path)


def test_read_manifest_file_rejects_a_file_too_short_to_hold_a_header(tmp_path):
    path = tmp_path / "m.alpmodel"
    path.write_bytes(b"ALPM")
    with pytest.raises(ValueError, match="truncated container"):
        read_manifest_file(path)


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


#: The three map-decode loops in alp-sdk's on-device reader, and the C
#: statement each one must keep. Ordered as they appear in the file so a slice
#: between consecutive signatures is exactly one function body.
_READER_LOOPS = (
    "static bool decode_requires(",   # the nested "requires" map
    "static bool decode_target(",     # one target map
    "alp_status_t alp_model_parse(",  # the top-level manifest map
)
_SKIP_UNKNOWN = "zcbor_any_skip(zs, NULL)"


@_needs_bound_sdk
def test_the_on_device_reader_still_skips_manifest_keys_it_does_not_know():
    """THE CROSS-REPO GUARD BEHIND `Target.caveats`.

    `caveats` is additive-only because alp-sdk's `alp_model_parse`
    (`src/common/alp_model.c`) ends every one of its map-decode loops with an
    `else { ok = zcbor_any_skip(zs, NULL); }` fallback -- an unknown key is
    stepped over, never mis-indexed and never rejected. Measured on that
    reader compiled natively against real zcbor: fed a container carrying a
    per-target `caveats` list, it returns ALP_OK with every existing field
    byte-identical; there is no reader-side change anywhere in this feature.

    If that fallback is ever removed or narrowed, a `tan model build` that
    ships a caveat starts bricking `alp_inference_open_alpmodel` on device
    with ALP_ERR_INVAL, and nothing on THIS side would notice. So it is
    pinned from here, where the writer lives.

    Deliberately NOT a reimplementation of the reader: it asserts the one
    property the writer depends on, in the file that owns it."""
    source = (_SDK / "src/common/alp_model.c").read_text(encoding="utf-8")
    missing = [sig for sig in _READER_LOOPS if sig not in source]
    assert not missing, (
        f"{missing} no longer exist in src/common/alp_model.c -- the reader was "
        "restructured; re-derive this guard against its new shape rather than "
        "deleting it"
    )
    bounds = [source.index(sig) for sig in _READER_LOOPS]
    assert bounds == sorted(bounds), (
        f"{_READER_LOOPS} no longer appear in this order in src/common/alp_model.c; "
        "re-derive the slices before trusting this guard"
    )
    for i, sig in enumerate(_READER_LOOPS):
        end = bounds[i + 1] if i + 1 < len(bounds) else len(source)
        body = source[bounds[i]:end]
        assert _SKIP_UNKNOWN in body, (
            f"alp-sdk's `{sig}...` no longer skips unknown CBOR map keys with "
            f"`{_SKIP_UNKNOWN}`. `tan.model.manifest.Target.caveats` (and every "
            f"future additive manifest field) depends on that fallback: without "
            f"it, a package this repo writes is rejected on device with "
            f"ALP_ERR_INVAL. Fix the reader, or stop writing additive keys."
        )


@_needs_bound_sdk
def test_the_container_version_matches_the_on_device_readers_own_constant():
    """`CONTAINER_VERSION` is the BINARY FRAME's version and is shared with
    `ALP_MODEL_CONTAINER_V` in alp-sdk's `include/alp/model.h`; the reader
    hard-refuses any other value with ALP_ERR_VERSION (-11, measured). Adding
    a manifest KEY must never move it -- that would refuse every fielded
    device to announce a field no on-device reader reads."""
    text = (_SDK / "include/alp/model.h").read_text(encoding="utf-8")
    m = re.search(r"#define\s+ALP_MODEL_CONTAINER_V\s+(\d+)u", text)
    assert m, "ALP_MODEL_CONTAINER_V not found in include/alp/model.h"
    assert int(m.group(1)) == CONTAINER_VERSION


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


def test_read_manifest_file_refuses_a_manifest_region_past_the_end_of_the_file(tmp_path):
    """`mft_off`/`mft_len` are untrusted u32s. `f.read(n)` ALLOCATES n, so a
    crafted 0xFFFFFFFF length would ask for 4 GiB from a few-KiB file --
    bounded against the real size first, the same subtraction-style compare
    alp-sdk's own reader makes for these fields."""
    import struct as _struct

    path = tmp_path / "m.alpmodel"
    raw = bytearray(write_package(_mft(), [b"A"]))
    _struct.pack_into("<I", raw, 12, 0xFFFFFFFF)          # mft_len
    path.write_bytes(bytes(raw))
    with pytest.raises(ValueError, match="runs past the end"):
        read_manifest_file(path)
    _struct.pack_into("<I", raw, 12, 4)
    _struct.pack_into("<I", raw, 8, 0xFFFFFFFF)           # mft_off
    path.write_bytes(bytes(raw))
    with pytest.raises(ValueError, match="runs past the end"):
        read_manifest_file(path)
