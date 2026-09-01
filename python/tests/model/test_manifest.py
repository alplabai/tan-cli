# SPDX-License-Identifier: Apache-2.0
"""Tests for tan.model.manifest round-trip serialisation."""
import json
import cbor2
import pytest
from tan.model import manifest
from tan.model.manifest import Tensor, Target, Coverage, Manifest


def _sample() -> Manifest:
    return Manifest(
        name="person_detect",
        src_sha=bytes(range(32)),
        inputs=[Tensor(dtype="int8", rank=4, shape=[1, 224, 224, 3], scale=0.0078, zp=-1)],
        outputs=[Tensor(dtype="int8", rank=2, shape=[1, 1000], scale=0.004, zp=12)],
        targets=[
            Target(backend="ethos_u", silicon_ref="alif:ensemble:e8",
                   blob_format="vela_tflite", accel_config="ethos-u85-256",
                   arena=524288, requires={"sram_kib": 512, "op_features": ["transformer"]},
                   blob=0),
            Target(backend="cpu", silicon_ref="*", blob_format="tflite",
                   accel_config="", arena=786432, requires={"sram_kib": 0, "op_features": []},
                   blob=1),
        ],
        coverage=[Coverage(backend="deepx_dxm1", accel_config="",
                           status="skipped", reason="dx-compiler not found")],
    )


def test_manifest_roundtrips_through_dict():
    m = _sample()
    assert Manifest.from_dict(m.to_dict()) == m
    assert m.to_dict()["targets"][0]["backend"] == "ethos_u"


def test_manifest_json_is_human_readable_and_roundtrips():
    m = _sample()
    text = m.to_json()
    doc = json.loads(text)                       # valid JSON
    assert doc["name"] == "person_detect"
    assert doc["src_sha"] == "00010203" + "0405060708090a0b0c0d0e0f" + \
        "101112131415161718191a1b1c1d1e1f"       # hex-encoded bytes
    assert Manifest.from_json(text) == m


def test_manifest_cbor_roundtrips():
    m = _sample()
    blob = m.to_cbor()
    assert isinstance(blob, (bytes, bytearray))
    assert Manifest.from_cbor(blob) == m


def test_cbor_decode_tolerates_unknown_keys():
    # extensibility: a future writer adds a key the current reader ignores
    m = _sample()
    doc = cbor2.loads(m.to_cbor())
    doc["future_field"] = 123
    doc["targets"][0]["future_target_field"] = "x"
    extended = cbor2.dumps(doc)
    assert Manifest.from_cbor(extended) == m      # unknown keys dropped, rest intact


def test_target_compiler_version_is_carried_through_cbor():
    tgt = Target(backend="ethos_u", silicon_ref="alif:ensemble:e8",
                 blob_format="vela_tflite", accel_config="ethos-u85-256",
                 arena=1024, requires={"sram_kib": 64, "op_features": []},
                 blob=0, compiler_version="vela 4.1.0")
    m = Manifest(name="m", src_sha=bytes(32), targets=[tgt])
    assert cbor2.loads(m.to_cbor())["targets"][0]["compiler_version"] == "vela 4.1.0"
    assert Manifest.from_cbor(m.to_cbor()).targets[0].compiler_version == "vela 4.1.0"


def test_missing_compiler_version_decodes_to_empty_default():
    tgt = Target(backend="cpu", silicon_ref="*", blob_format="tflite", accel_config="",
                 arena=0, requires={"sram_kib": 0, "op_features": []}, blob=0)
    m = Manifest(name="m", src_sha=bytes(32), targets=[tgt])
    doc = cbor2.loads(m.to_cbor())
    doc["targets"][0].pop("compiler_version")     # simulate a writer that omitted it
    decoded = Manifest.from_cbor(cbor2.dumps(doc))
    assert decoded.targets[0].compiler_version == ""


def test_field_types_maps_stay_in_parity_with_their_whole_key_set():
    """`_check_element_fields` indexes `field_types[field_name]` for every
    name in `item_keys` with no `.get` fallback, so a map that drifts out of
    sync would surface only as a raw `KeyError` at decode time, not here at
    collection/import time. All three pairs match today (`grep -rn` for the
    four map names across `tests/` returned zero hits before this test --
    tan-cli#1058 review round 3); this pins the parity so a future drift is
    caught before decode time.

    The compared set is `item_keys`, not `required` (tan-cli#1056): the two
    were the same thing for `Tensor`/`Coverage` and are still, but
    `_TARGET_TYPES` now also covers the OPTIONAL `compiler_version`/`caveats`
    that `_optional_field` type-checks. Asserting against `_TARGET_REQUIRED`
    here would fail the parity it exists to protect the moment those entries
    landed -- which is exactly what it did."""
    assert set(manifest._TENSOR_TYPES) == manifest._TENSOR_KEYS
    assert set(manifest._TARGET_TYPES) == manifest._TARGET_KEYS
    assert set(manifest._COVERAGE_TYPES) == manifest._COV_KEYS


def test_from_dict_missing_name_raises_curated_value_error_not_keyerror():
    # tan-cli#1074: a `from_dict` mapping missing `name` used to raise a bare
    # `KeyError('name')`, not the curated `ValueError` this module's contract
    # promises everywhere else.
    d = _sample().to_dict()
    del d["name"]
    with pytest.raises(ValueError, match=r"missing required field 'name'"):
        Manifest.from_dict(d)


def test_from_dict_missing_src_sha_raises_curated_value_error_not_keyerror():
    d = _sample().to_dict()
    del d["src_sha"]
    with pytest.raises(ValueError, match=r"missing required field 'src_sha'"):
        Manifest.from_dict(d)


def test_from_dict_wrong_typed_src_sha_raises_curated_value_error():
    d = _sample().to_dict()
    d["src_sha"] = "not-bytes"    # from_dict's contract is post-normalisation bytes
    with pytest.raises(ValueError, match=r"field 'src_sha' must be a bytes object"):
        Manifest.from_dict(d)


def test_unknown_blob_format_is_rejected_with_the_valid_set_named():
    # tan-cli#1074: a manifest naming an unlisted format (a typo'd "dxnn",
    # here "vela_tflite_v2") used to decode clean -- nothing compared a
    # decoded `blob_format` against `VALID_BLOB_FORMATS`. The message must
    # name the offending value AND list the accepted set, so a typo is
    # diagnosable from the error alone.
    d = _sample().to_dict()
    d["targets"][0]["blob_format"] = "vela_tflite_v2"
    with pytest.raises(ValueError, match=r"field 'blob_format' must be one of .*'vela_tflite_v2'.*targets\[0\]"):
        Manifest.from_dict(d)


@pytest.mark.parametrize("blob_format", sorted(manifest.VALID_BLOB_FORMATS))
def test_every_valid_blob_format_round_trips_through_dict_and_cbor(blob_format):
    # Acceptance bar for tan-cli#1074 part 1: enforcing membership must not
    # reject any format a real producer emits today. Parametrized over the
    # RECONCILED set (not a hand-picked subset) so a future addition to
    # `VALID_BLOB_FORMATS` is covered automatically -- "executorch" is the
    # member that would have regressed had enforcement landed against the
    # stale 5-member set (`adapters/executorch.py` emits it, registered by
    # default in `build.py`'s `_ADAPTERS`); "onnx" has no compiler-adapter
    # producer but is real too (`_gen_fixture.py`'s `_onnx_cpu_manifest`,
    # the issue #1254 regression fixture alp-sdk's on-device reader decodes).
    tgt = Target(backend="cpu", silicon_ref="*", blob_format=blob_format, accel_config="",
                 arena=0, requires={"sram_kib": 0, "op_features": []}, blob=0)
    m = Manifest(name="m", src_sha=bytes(32), targets=[tgt])
    assert Manifest.from_dict(m.to_dict()) == m
    assert Manifest.from_cbor(m.to_cbor()) == m


def test_element_type_maps_only_name_fields_declared_as_lists():
    """tan-cli#1063: `_check_element_types` is reached only after
    `_check_element_fields` has already confirmed the field's own value is a
    `list`, so an element-type entry for a field whose `field_types` entry is
    NOT `list` would be unreachable dead configuration -- and, worse, would
    read as a guard that exists. Pins that every element map is a subset of
    its field map AND names only `list`-typed fields."""
    for element_map, field_map in ((manifest._TENSOR_ELEMENT_TYPES, manifest._TENSOR_TYPES),
                                    (manifest._TARGET_ELEMENT_TYPES, manifest._TARGET_TYPES)):
        assert set(element_map) <= set(field_map)
        for name in element_map:
            assert field_map[name][0] is list, f"{name} is not declared as a list"
