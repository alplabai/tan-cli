# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1039 + tan-cli#1040 -- both found during #1023's round-two review.

#1039: `Manifest.from_cbor` never checked `MANIFEST_SCHEMA_VERSION`, while
`from_json` did -- CBOR is the PRODUCTION reader path for `.alpmodel`
packages, so the stricter check sat on the path that matters less. Measured
on the unguarded code:

    from_cbor(cbor2.dumps({"v": 99, "name": "x", "src_sha": b"\\x00"}))
        -> NO RAISE, silently parsed as v1
    from_json('{"v": 99, "name": "x", "src_sha": "00"}')
        -> ValueError: unsupported manifest version 99; expected 1

#1040: the nested `Tensor`/`Target`/`Coverage` decodes were unguarded against
a wrong-typed or incomplete element, on BOTH reader paths (`_pick`-based
`from_cbor`, and the bare `Tensor(**t)` `from_dict` used for `from_json`).
Measured on the unguarded code:

    Manifest.from_cbor(cbor2.dumps({"name": "x", "src_sha": b"\\x00",
                                     "inputs": ["abc"]}))
        -> TypeError: Tensor.__init__() missing 5 required positional
           arguments: 'dtype', 'rank', 'shape', 'scale', and 'zp'
    Manifest.from_cbor(cbor2.dumps({"name": "x", "src_sha": b"\\x00",
                                     "targets": [{"backend": "cpu"}]}))
        -> TypeError: Target.__init__() missing 6 required positional
           arguments: 'silicon_ref', 'blob_format', 'accel_config', 'arena',
           'requires', and 'blob'
    Manifest.from_json('{"name": "x", "src_sha": "00", "inputs": ["abc"]}')
        -> TypeError: tan.model.manifest.Tensor() argument after ** must be
           a mapping, not str

Both fixes route through a single shared check reused from #1023
(`_required_field`, extended with a `context` kwarg) via a new
`_decode_list_field` helper that both `from_json` (through `from_dict`) and
`from_cbor` (now also routed through `from_dict`) call -- mirrors
`test_manifest_malformed_document.py`'s assertion style for the sibling
document-level guard."""
import cbor2
import pytest

from tan.model.manifest import Manifest

# ---------------------------------------------------------------------------
# #1039 -- MANIFEST_SCHEMA_VERSION, CBOR side
# ---------------------------------------------------------------------------


def test_cbor_manifest_rejects_an_unsupported_schema_version():
    with pytest.raises(ValueError, match=r"unsupported manifest version 99; expected 1"):
        Manifest.from_cbor(cbor2.dumps({"v": 99, "name": "x", "src_sha": b"\x00"}))


def test_cbor_manifest_accepts_the_current_schema_version():
    m = Manifest.from_cbor(cbor2.dumps({"v": 1, "name": "x", "src_sha": b"\x00"}))
    assert m.name == "x"


def test_cbor_manifest_without_a_v_field_defaults_to_current_and_is_accepted():
    # `v` is itself optional-on-the-wire (older writers) -- absence must not
    # be confused with an unsupported version.
    m = Manifest.from_cbor(cbor2.dumps({"name": "x", "src_sha": b"\x00"}))
    assert m.name == "x"


def test_json_and_cbor_agree_on_an_unsupported_schema_version():
    # The bug's own shape: the two readers disagreed. They must not any more.
    with pytest.raises(ValueError, match=r"unsupported manifest version 99; expected 1"):
        Manifest.from_json('{"v": 99, "name": "x", "src_sha": "00"}')
    with pytest.raises(ValueError, match=r"unsupported manifest version 99; expected 1"):
        Manifest.from_cbor(cbor2.dumps({"v": 99, "name": "x", "src_sha": b"\x00"}))


# ---------------------------------------------------------------------------
# #1040 -- nested Tensor/Target/Coverage decodes, CBOR side
# ---------------------------------------------------------------------------


def test_cbor_manifest_non_mapping_input_element_raises_a_curated_valueerror():
    with pytest.raises(ValueError, match=r"inputs\[0\] must be a mapping, got str"):
        Manifest.from_cbor(cbor2.dumps({"name": "x", "src_sha": b"\x00", "inputs": ["abc"]}))


def test_cbor_manifest_incomplete_target_element_raises_a_curated_valueerror():
    with pytest.raises(ValueError, match=r"missing required field 'accel_config' in targets\[0]"):
        Manifest.from_cbor(
            cbor2.dumps({"name": "x", "src_sha": b"\x00", "targets": [{"backend": "cpu"}]})
        )


def test_cbor_manifest_incomplete_output_element_raises_a_curated_valueerror():
    with pytest.raises(ValueError, match=r"missing required field 'dtype' in outputs\[0\]"):
        Manifest.from_cbor(
            cbor2.dumps({"name": "x", "src_sha": b"\x00", "outputs": [{"rank": 1}]})
        )


def test_cbor_manifest_incomplete_coverage_element_raises_a_curated_valueerror():
    with pytest.raises(ValueError, match=r"missing required field 'reason' in coverage\[0\]"):
        Manifest.from_cbor(
            cbor2.dumps({"name": "x", "src_sha": b"\x00",
                         "coverage": [{"backend": "cpu", "accel_config": "", "status": "skipped"}]})
        )


def test_cbor_manifest_non_list_targets_field_raises_a_curated_valueerror():
    # One shape up from a bad element: the whole field isn't a list at all.
    # Left unguarded this would `enumerate()` a string's CHARACTERS.
    with pytest.raises(ValueError, match=r"field 'targets' must be a list, got str"):
        Manifest.from_cbor(cbor2.dumps({"name": "x", "src_sha": b"\x00", "targets": "nope"}))


def test_cbor_manifest_second_target_element_is_named_by_index():
    # The FIRST target is well-formed; only the second is broken. The index
    # in the error must point at the actual offender, not just "targets".
    good = {"backend": "cpu", "silicon_ref": "*", "blob_format": "tflite",
            "accel_config": "", "arena": 0, "requires": {}, "blob": 0}
    with pytest.raises(ValueError, match=r"missing required field 'blob' in targets\[1\]"):
        Manifest.from_cbor(cbor2.dumps({
            "name": "x", "src_sha": b"\x00",
            "targets": [good, {"backend": "cpu", "silicon_ref": "*", "blob_format": "tflite",
                                "accel_config": "", "arena": 0, "requires": {}}],
        }))


# ---------------------------------------------------------------------------
# #1040 -- nested Tensor/Target/Coverage decodes, JSON side (the sibling gap:
# `from_dict`, reached from `from_json`, built these straight out of
# `Tensor(**t)` with no `_pick` at all -- a bare non-mapping element failed
# even earlier, on the `**` unpack itself, with a different raw TypeError
# than the CBOR side's).
# ---------------------------------------------------------------------------


def test_json_manifest_non_mapping_input_element_raises_a_curated_valueerror():
    with pytest.raises(ValueError, match=r"inputs\[0\] must be a mapping, got str"):
        Manifest.from_json('{"name": "x", "src_sha": "00", "inputs": ["abc"]}')


def test_json_manifest_incomplete_target_element_raises_a_curated_valueerror():
    with pytest.raises(ValueError, match=r"missing required field 'accel_config' in targets\[0]"):
        Manifest.from_json('{"name": "x", "src_sha": "00", "targets": [{"backend": "cpu"}]}')


def test_json_manifest_non_list_targets_field_raises_a_curated_valueerror():
    with pytest.raises(ValueError, match=r"field 'targets' must be a list, got str"):
        Manifest.from_json('{"name": "x", "src_sha": "00", "targets": "nope"}')


# ---------------------------------------------------------------------------
# A well-formed nested element still round-trips (non-vacuity check: the
# guard above must not reject valid input, only wrong-typed/incomplete
# input).
# ---------------------------------------------------------------------------


def test_a_complete_target_element_still_decodes_on_both_readers():
    good = {"backend": "cpu", "silicon_ref": "*", "blob_format": "tflite",
            "accel_config": "", "arena": 4096, "requires": {"sram_kib": 0}, "blob": 0}
    doc_cbor = {"name": "x", "src_sha": b"\x00", "targets": [good]}
    m = Manifest.from_cbor(cbor2.dumps(doc_cbor))
    assert m.targets[0].backend == "cpu"
    assert m.targets[0].arena == 4096

    doc_json = '{"name": "x", "src_sha": "00", "targets": [%s]}' % (
        '{"backend": "cpu", "silicon_ref": "*", "blob_format": "tflite", '
        '"accel_config": "", "arena": 4096, "requires": {"sram_kib": 0}, "blob": 0}'
    )
    m2 = Manifest.from_json(doc_json)
    assert m2.targets[0].backend == "cpu"
    assert m2.targets[0].arena == 4096


# ---------------------------------------------------------------------------
# tan-cli#1049 -- the element guard above was PRESENCE-ONLY (`expected=object`
# at `manifest.py:131`): a well-formed element carrying a wrong-typed VALUE
# was silently accepted --
#
#   inputs=[{"dtype": 5, "rank": "nope", "shape": None, "scale": None, "zp": None}]
#     -> Tensor(dtype=5, rank='nope', shape=None, ...)      no raise
#
# `_decode_list_field` now takes a per-field `field_types` map so
# `_required_field`'s type branch is no longer dead code at this call site.
# ---------------------------------------------------------------------------


def test_cbor_manifest_wrong_typed_input_element_raises_a_curated_valueerror():
    """The issue's own measured shape. `dtype` sorts first among
    `_TENSOR_KEYS` (`sorted(required)` is alphabetical), so the wrong-typed
    `dtype` is the field named."""
    with pytest.raises(ValueError, match=r"field 'dtype' must be a string, got int in inputs\[0\]"):
        Manifest.from_cbor(cbor2.dumps({
            "name": "x", "src_sha": b"\x00",
            "inputs": [{"dtype": 5, "rank": "nope", "shape": None, "scale": None, "zp": None}],
        }))


def test_json_manifest_wrong_typed_input_element_raises_a_curated_valueerror():
    with pytest.raises(ValueError, match=r"field 'dtype' must be a string, got int in inputs\[0\]"):
        Manifest.from_json(
            '{"name": "x", "src_sha": "00", "inputs": '
            '[{"dtype": 5, "rank": "nope", "shape": null, "scale": null, "zp": null}]}'
        )


def test_cbor_manifest_wrong_typed_output_rank_raises_a_curated_valueerror():
    # A well-formed `dtype` this time, so the SECOND field checked (`rank`,
    # alphabetically) is the one that trips -- proves every required field is
    # checked, not just the first.
    with pytest.raises(ValueError, match=r"field 'rank' must be an int, got str in outputs\[0\]"):
        Manifest.from_cbor(cbor2.dumps({
            "name": "x", "src_sha": b"\x00",
            "outputs": [{"dtype": "int8", "rank": "nope", "shape": [1], "scale": 1.0, "zp": 0}],
        }))


def test_json_manifest_wrong_typed_output_rank_raises_a_curated_valueerror():
    with pytest.raises(ValueError, match=r"field 'rank' must be an int, got str in outputs\[0\]"):
        Manifest.from_json(
            '{"name": "x", "src_sha": "00", "outputs": '
            '[{"dtype": "int8", "rank": "nope", "shape": [1], "scale": 1.0, "zp": 0}]}'
        )


def test_cbor_manifest_wrong_typed_target_arena_raises_a_curated_valueerror():
    """The field that actually matters (#1049's "why"): `arena` and
    `requires["sram_kib"]` are exactly the figures
    `src/backends/inference/alp_model_select.c`'s fit gate reads."""
    good = {"backend": "cpu", "silicon_ref": "*", "blob_format": "tflite",
            "accel_config": "", "requires": {"sram_kib": 0}, "blob": 0}
    with pytest.raises(ValueError, match=r"field 'arena' must be an int, got str in targets\[0\]"):
        Manifest.from_cbor(cbor2.dumps({
            "name": "x", "src_sha": b"\x00",
            "targets": [{**good, "arena": "big"}],
        }))


def test_json_manifest_wrong_typed_target_arena_raises_a_curated_valueerror():
    with pytest.raises(ValueError, match=r"field 'arena' must be an int, got str in targets\[0\]"):
        Manifest.from_json(
            '{"name": "x", "src_sha": "00", "targets": '
            '[{"backend": "cpu", "silicon_ref": "*", "blob_format": "tflite", '
            '"accel_config": "", "arena": "big", "requires": {"sram_kib": 0}, "blob": 0}]}'
        )


def test_cbor_manifest_wrong_typed_target_requires_raises_a_curated_valueerror():
    """`requires` itself must be a mapping -- a list slips `sram_kib` past
    every other guard and would reach the fit gate as an un-indexable
    sequence instead of `{"sram_kib": int}`."""
    good = {"backend": "cpu", "silicon_ref": "*", "blob_format": "tflite",
            "accel_config": "", "arena": 4096, "blob": 0}
    with pytest.raises(ValueError, match=r"field 'requires' must be a mapping, got list in targets\[0\]"):
        Manifest.from_cbor(cbor2.dumps({
            "name": "x", "src_sha": b"\x00",
            "targets": [{**good, "requires": ["sram_kib", 512]}],
        }))


def test_cbor_manifest_wrong_typed_coverage_field_raises_a_curated_valueerror():
    with pytest.raises(ValueError, match=r"field 'status' must be a string, got int in coverage\[0\]"):
        Manifest.from_cbor(cbor2.dumps({
            "name": "x", "src_sha": b"\x00",
            "coverage": [{"backend": "cpu", "accel_config": "", "status": 1, "reason": "x"}],
        }))


def test_a_whole_number_int_scale_is_still_accepted_not_just_float():
    """Non-vacuity in the other direction: the new type guard must not
    reject well-formed content that happens to use `int` where the dataclass
    annotates `float` -- `1` is as valid JSON/CBOR content as `1.0`, and the
    annotation is not an enforced constraint. This must NOT raise."""
    good = {"dtype": "int8", "rank": 1, "shape": [1], "scale": 1, "zp": 0}
    m = Manifest.from_cbor(cbor2.dumps({"name": "x", "src_sha": b"\x00", "inputs": [good]}))
    assert m.inputs[0].scale == 1


def test_a_complete_tensor_element_still_decodes_on_both_readers():
    good = {"dtype": "int8", "rank": 4, "shape": [1, 224, 224, 3], "scale": 0.0078, "zp": -1}
    m = Manifest.from_cbor(cbor2.dumps({"name": "x", "src_sha": b"\x00", "inputs": [good]}))
    assert m.inputs[0].dtype == "int8"
    assert m.inputs[0].shape == [1, 224, 224, 3]

    doc_json = ('{"name": "x", "src_sha": "00", "inputs": '
                '[{"dtype": "int8", "rank": 4, "shape": [1, 224, 224, 3], '
                '"scale": 0.0078, "zp": -1}]}')
    m2 = Manifest.from_json(doc_json)
    assert m2.inputs[0].dtype == "int8"
    assert m2.inputs[0].shape == [1, 224, 224, 3]
