# SPDX-License-Identifier: Apache-2.0
"""The four decodes `manifest.py` still let through after tan-cli#1058 --
found by sweeping the file for what #1049/#1058's "now closed at the
mechanism" claim did NOT reach.

tan-cli#1064 -- the schema-version gate is the ONE comparison in the module
that reads a decoded value without routing through `_required_field`, so it
never inherited that helper's `bool` rule. `bool` is an `int` subclass and
`True == 1`. Measured on the unguarded code:

    from_cbor v=True   -> NO RAISE, accepted as v1
    from_cbor v=1.0    -> NO RAISE, accepted as v1
    from_cbor v=2      -> ValueError: unsupported manifest version 2; expected 1
    from_cbor v='1'    -> ValueError: unsupported manifest version '1'; expected 1

tan-cli#1063 -- `_TENSOR_TYPES["shape"] = (list, "a list")` guards the
CONTAINER only, the same shape #1049 closed for `Target.requires`. Measured:

    inputs shape=['a', 'b']    -> Tensor(shape=['a', 'b'])       no raise
    inputs shape=[None, None]  -> Tensor(shape=[None, None])     no raise
    inputs shape=[{'a': 1}]    -> Tensor(shape=[{'a': 1}])       no raise
    inputs shape=[True, False] -> Tensor(shape=[True, False])    no raise

tan-cli#1056 -- `_decode_list_field` only type-checked fields in `required`.
`Target.compiler_version`/`caveats` are in `item_keys` (so `_pick` hands them
to the constructor) but in no `required` set. Measured:

    targets=[{...valid..., "compiler_version": 12345, "caveats": "not-a-list"}]
      -> Target(compiler_version=12345, caveats='not-a-list')    no raise
    targets=[{...valid..., "caveats": [1, 2]}]
      -> Target(caveats=[1, 2])                                  no raise

tan-cli#1055 -- `Manifest.from_cbor` let cbor2's own decode exceptions escape.
They are NOT `ValueError` subclasses; `from_json`'s `json.JSONDecodeError`
is, so the two reader paths disagreed on what a caller must catch for the
equivalent failure -- and CBOR is the PRODUCTION `.alpmodel` reader path.
Measured:

    from_json("{not json")  -> json.decoder.JSONDecodeError   isinstance ValueError: True
    from_cbor(b"\\xa1")      -> cbor2.CBORDecodeEOF            isinstance ValueError: False
    from_cbor(b"")          -> cbor2.CBORDecodeEOF            isinstance ValueError: False
"""
import cbor2
import pytest

from tan.model.manifest import Manifest

# A complete, valid `targets[]` element -- every REQUIRED field present and
# well-typed, so a test below can perturb exactly one optional field and know
# the raise (or the silence) came from that field alone.
GOOD_TARGET = {"backend": "cpu", "silicon_ref": "*", "blob_format": "tflite",
               "accel_config": "", "arena": 4096, "requires": {"sram_kib": 0}, "blob": 0}
GOOD_TENSOR = {"dtype": "int8", "rank": 4, "shape": [1, 224, 224, 3], "scale": 0.0078, "zp": -1}


def _cbor(**extra) -> bytes:
    return cbor2.dumps({"name": "x", "src_sha": b"\x00", **extra})


# ---------------------------------------------------------------------------
# tan-cli#1064 -- the schema-version gate
# ---------------------------------------------------------------------------


def test_cbor_schema_version_true_is_refused_not_accepted_as_v1():
    """The issue's headline shape. `True == MANIFEST_SCHEMA_VERSION` is
    `True` in Python, so the bare `!=` comparison accepted a CBOR `0xf5`
    (major type 7, "true") as an unsigned-int version 1."""
    with pytest.raises(ValueError, match=r"unsupported manifest version True; expected 1"):
        Manifest.from_cbor(_cbor(v=True))


def test_cbor_schema_version_float_one_is_refused():
    """`1.0 == 1` is also `True`. A version written as a CBOR float is a
    manifest this reader does not understand, whatever it evaluates to."""
    with pytest.raises(ValueError, match=r"unsupported manifest version 1\.0; expected 1"):
        Manifest.from_cbor(_cbor(v=1.0))


def test_json_schema_version_true_is_refused_not_accepted_as_v1():
    with pytest.raises(ValueError, match=r"unsupported manifest version True; expected 1"):
        Manifest.from_json('{"v": true, "name": "x", "src_sha": "00"}')


def test_json_schema_version_float_one_is_refused():
    with pytest.raises(ValueError, match=r"unsupported manifest version 1\.0; expected 1"):
        Manifest.from_json('{"v": 1.0, "name": "x", "src_sha": "00"}')


def test_the_int_schema_version_is_still_accepted_on_both_readers():
    """Non-vacuity: the stricter gate must refuse only the wrong TYPE, not
    the right value. A real `v: 1` (and an absent `v`, the older-writer case)
    still decodes."""
    assert Manifest.from_cbor(_cbor(v=1)).name == "x"
    assert Manifest.from_cbor(_cbor()).name == "x"
    assert Manifest.from_json('{"v": 1, "name": "x", "src_sha": "00"}').name == "x"


# ---------------------------------------------------------------------------
# tan-cli#1063 -- Tensor.shape ELEMENTS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape, got", [
    (["a", "b"], "str"),
    ([None, None], "NoneType"),
    ([{"a": 1}], "dict"),
    ([True, False], "bool"),
])
def test_cbor_wrong_typed_shape_element_raises_a_curated_valueerror(shape, got):
    """All four of the issue's measured shapes, plus the `bool` one the
    shared `_check_type` gate brings for free -- `shape=[True, False]` is
    `[1, 0]` to every comparison but re-emits as CBOR `0xf5`/`0xf4`, not as
    unsigned ints, so it is the same silent-wrong class as the rest."""
    with pytest.raises(ValueError,
                        match=rf"element 0 must be an int, got {got} in inputs\[0\]\.shape"):
        Manifest.from_cbor(_cbor(inputs=[{**GOOD_TENSOR, "rank": 1, "shape": shape}]))


def test_json_wrong_typed_shape_element_raises_a_curated_valueerror():
    with pytest.raises(ValueError,
                        match=r"element 0 must be an int, got str in inputs\[0\]\.shape"):
        Manifest.from_json(
            '{"name": "x", "src_sha": "00", "inputs": '
            '[{"dtype": "int8", "rank": 2, "shape": ["a", "b"], "scale": 1.0, "zp": 0}]}'
        )


def test_a_shape_element_is_named_by_its_own_index_not_just_the_field():
    """The FIRST element is a well-formed int; only the second is broken. The
    error must point at the actual offender -- every element is checked, not
    just element 0."""
    with pytest.raises(ValueError,
                        match=r"element 2 must be an int, got str in outputs\[0\]\.shape"):
        Manifest.from_cbor(_cbor(outputs=[{**GOOD_TENSOR, "rank": 3, "shape": [1, 2, "3"]}]))


def test_a_well_typed_shape_still_decodes_on_both_readers():
    """Non-vacuity: the element guard must not reject a valid shape, and an
    EMPTY shape (a scalar tensor) has no elements to check and must stay
    legal."""
    m = Manifest.from_cbor(_cbor(inputs=[GOOD_TENSOR], outputs=[{**GOOD_TENSOR, "rank": 0,
                                                                 "shape": []}]))
    assert m.inputs[0].shape == [1, 224, 224, 3]
    assert m.outputs[0].shape == []

    m2 = Manifest.from_json(
        '{"name": "x", "src_sha": "00", "inputs": '
        '[{"dtype": "int8", "rank": 4, "shape": [1, 224, 224, 3], "scale": 0.0078, "zp": -1}]}'
    )
    assert m2.inputs[0].shape == [1, 224, 224, 3]


# ---------------------------------------------------------------------------
# tan-cli#1056 -- Target's OPTIONAL wire fields
# ---------------------------------------------------------------------------


def test_cbor_wrong_typed_optional_compiler_version_raises_a_curated_valueerror():
    with pytest.raises(ValueError,
                        match=r"field 'compiler_version' must be a string, got int in targets\[0\]"):
        Manifest.from_cbor(_cbor(targets=[{**GOOD_TARGET, "compiler_version": 12345}]))


def test_cbor_wrong_typed_optional_caveats_raises_a_curated_valueerror():
    with pytest.raises(ValueError,
                        match=r"field 'caveats' must be a list, got str in targets\[0\]"):
        Manifest.from_cbor(_cbor(targets=[{**GOOD_TARGET, "caveats": "not-a-list"}]))


def test_json_wrong_typed_optional_caveats_raises_a_curated_valueerror():
    with pytest.raises(ValueError,
                        match=r"field 'caveats' must be a list, got str in targets\[0\]"):
        Manifest.from_json(
            '{"name": "x", "src_sha": "00", "targets": '
            '[{"backend": "cpu", "silicon_ref": "*", "blob_format": "tflite", '
            '"accel_config": "", "arena": 4096, "requires": {"sram_kib": 0}, "blob": 0, '
            '"caveats": "not-a-list"}]}'
        )


def test_cbor_wrong_typed_caveat_element_raises_a_curated_valueerror():
    """`caveats` is `list[str]` -- each entry a complete customer-readable
    sentence `tan model build` / `tan model check --exact` render verbatim.
    A `list` that IS a list but carries ints is the tan-cli#1063 shape
    reaching the field tan-cli#1056 names."""
    with pytest.raises(ValueError,
                        match=r"element 0 must be a string, got int in targets\[0\]\.caveats"):
        Manifest.from_cbor(_cbor(targets=[{**GOOD_TARGET, "caveats": [1, 2]}]))


def test_an_absent_optional_target_field_is_still_accepted():
    """Non-vacuity, the ABSENT half: `compiler_version`/`caveats` are
    OPTIONAL on the wire by design (`_target_dict` omits an empty `caveats`
    entirely, which is what keeps the field additive against alp-sdk's
    committed fixtures). Absence must take the dataclass default, not
    raise."""
    m = Manifest.from_cbor(_cbor(targets=[GOOD_TARGET]))
    assert m.targets[0].compiler_version == ""
    assert m.targets[0].caveats == []


def test_a_well_typed_optional_target_field_is_still_carried_through():
    """Non-vacuity, the PRESENT half: a correctly-typed optional field must
    reach the constructor unchanged."""
    m = Manifest.from_cbor(_cbor(targets=[{**GOOD_TARGET, "compiler_version": "vela 4.1.0",
                                            "caveats": ["Compilation may be invalid."]}]))
    assert m.targets[0].compiler_version == "vela 4.1.0"
    assert m.targets[0].caveats == ["Compilation may be invalid."]


# ---------------------------------------------------------------------------
# tan-cli#1055 -- malformed CBOR bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blob", [b"", b"\xa1", b"\x9f", b"\x82\x01"])
def test_from_cbor_raises_a_valueerror_on_malformed_wire_bytes(blob):
    """Truncated map header, empty region, unterminated indefinite array, a
    2-element array with one element -- each is in-bounds for
    `package.read_package`'s offset/length checks (tan-cli#1045) and still
    not well-formed CBOR. Before this each escaped as a raw
    `cbor2.CBORDecodeEOF`, which is not a `ValueError`."""
    with pytest.raises(ValueError, match=r"malformed \.alpmodel manifest: not valid CBOR"):
        Manifest.from_cbor(blob)


def test_the_cbor_decode_failure_is_named_in_the_curated_message():
    """The curated error must not swallow WHICH failure cbor2 reported --
    "premature end of stream" is the difference between a truncated write and
    a bit-flipped major type."""
    with pytest.raises(ValueError,
                        match=r"not valid CBOR \(CBORDecodeEOF: premature end of stream"):
        Manifest.from_cbor(b"\xa1")


def test_both_readers_raise_valueerror_for_malformed_syntax():
    """The bug's own shape: the two reader paths disagreed on what a caller
    must catch. `from_json`'s `json.JSONDecodeError` is a `ValueError`
    subclass; cbor2's decode exceptions are not. A single `except ValueError`
    must now cover both."""
    for call in (lambda: Manifest.from_json("{not json"),
                 lambda: Manifest.from_cbor(b"\xa1")):
        try:
            call()
        except ValueError:
            continue
        pytest.fail("malformed input decoded without raising a ValueError")


def test_well_formed_cbor_still_decodes():
    """Non-vacuity: the try/except must wrap only the decode, not change what
    a valid manifest does."""
    m = Manifest.from_cbor(_cbor(targets=[GOOD_TARGET], inputs=[GOOD_TENSOR]))
    assert m.name == "x"
    assert m.targets[0].arena == 4096
    assert m.inputs[0].shape == [1, 224, 224, 3]


def test_a_real_round_trip_still_survives_both_readers():
    """End-to-end non-vacuity across all four guards at once: a manifest this
    module WROTE must still read back through both readers, caveats and
    all."""
    original = Manifest.from_cbor(_cbor(
        targets=[{**GOOD_TARGET, "compiler_version": "vela 4.1.0", "caveats": ["a caveat"]}],
        inputs=[GOOD_TENSOR], outputs=[GOOD_TENSOR],
        coverage=[{"backend": "ethos_u", "accel_config": "u55-256",
                   "status": "skipped", "reason": "no vela"}],
    ))
    assert Manifest.from_cbor(original.to_cbor()) == original
    assert Manifest.from_json(original.to_json()) == original
