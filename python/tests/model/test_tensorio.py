# SPDX-License-Identifier: Apache-2.0
"""TFLite tensor-I/O + operator-walk extraction (Stage-1a Tensor records for
the manifest; OpDesc/MAC records for tan.model.analyze's static NPU screen)."""
import sys
from pathlib import Path

import pytest

from tan.model.tensorio import extract_io, extract_ops, tflite_reader_available
from tan.model.manifest import Tensor

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _ROOT / "tests/fixtures/models/tiny_int8.tflite"


def test_extract_io_non_tflite_returns_empty(tmp_path):
    src = tmp_path / "m.onnx"
    src.write_bytes(b"ONNX-BYTES")
    assert extract_io(src) == ([], [])


def test_extract_io_missing_onnx_source_raises_not_swallowed(tmp_path):
    # The .onnx twin of the .tflite read-before-import fix: a source that
    # cannot be read at all is a real failure regardless of extension -- it
    # must never collapse to the identical `[], []` a genuinely readable but
    # non-.tflite source returns above (tan.model.check's MAJOR-1 review: this
    # silently backed `model check` reporting a verdict-shaped "nothing to
    # score" note for a nonexistent .onnx source on the ONNX-ingesting
    # backends -- drpai/deepx_dxm1 -- with model.check-failed never firing).
    with pytest.raises(OSError):
        extract_io(tmp_path / "missing.onnx")


def test_extract_io_malformed_tflite_returns_empty(tmp_path):
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-NOT-REALLY")
    # Contract: an unparseable .tflite always yields no I/O metadata and never raises
    # (parse-exception path when tflite is installed; ImportError path when it isn't).
    assert extract_io(src) == ([], [])


def test_extract_io_parses_tiny_fixture():
    pytest.importorskip("tflite")
    ins, outs = extract_io(_FIXTURE)
    # scale is stored as float32 in the flatbuffer; compare with approx to tolerate
    # the float32 -> float64 precision artifact (e.g. 0.004 -> 0.004000000189989805).
    assert len(ins) == 1
    assert ins[0].dtype == "int8" and ins[0].rank == 2 and ins[0].shape == [1, 4]
    assert ins[0].zp == -1
    assert ins[0].scale == pytest.approx(0.0078125, rel=1e-5)
    assert len(outs) == 1
    assert outs[0].dtype == "int8" and outs[0].rank == 2 and outs[0].shape == [1, 2]
    assert outs[0].zp == 2
    assert outs[0].scale == pytest.approx(0.004, rel=1e-5)


def test_extract_io_honors_raw_bytes_without_reading_file(tmp_path):
    # raw= lets build_model pass already-read bytes (read source once). The .tflite
    # path here does NOT exist on disk, so a non-empty result proves raw was used.
    pytest.importorskip("tflite")
    raw = _FIXTURE.read_bytes()
    ins, outs = extract_io(tmp_path / "does-not-exist.tflite", raw=raw)
    assert len(ins) == 1 and ins[0].shape == [1, 4]
    assert len(outs) == 1 and outs[0].shape == [1, 2]


# ---------------------------------------------------------------------------
# extract_ops: the operator walk + MAC weighting (tan.model.analyze's input)
# ---------------------------------------------------------------------------

def test_extract_ops_non_tflite_returns_empty(tmp_path):
    src = tmp_path / "m.onnx"
    src.write_bytes(b"ONNX-BYTES")
    # ONNX operator extraction is a follow-on; a .onnx source always yields []
    # here, never raises, so the ONNX-ingesting backends report `undetermined`
    # rather than a guess (tan.model.analyze).
    assert extract_ops(src) == []


def test_extract_ops_missing_onnx_source_raises_not_swallowed(tmp_path):
    # Same .onnx twin as test_extract_io_missing_onnx_source_raises_not_swallowed,
    # for the sibling extractor tan.model.check actually calls.
    with pytest.raises(OSError):
        extract_ops(tmp_path / "missing.onnx")


def test_extract_ops_malformed_tflite_returns_empty(tmp_path):
    src = tmp_path / "m.tflite"
    src.write_bytes(b"TFL3-NOT-REALLY-GARBAGE-BYTES")
    assert extract_ops(src) == []


def test_extract_ops_missing_tflite_reader_returns_empty(tmp_path, monkeypatch):
    # Force the ImportError branch even on a machine where `tflite` (the
    # `model-io` extra) IS installed: `sys.modules[name] = None` makes a bare
    # `import tflite` raise ImportError, same as it not being installed at
    # all -- this is the "missing tflite reader" case, distinct from the
    # malformed-bytes case above.
    monkeypatch.setitem(sys.modules, "tflite", None)
    assert extract_ops(_FIXTURE) == []


def test_tflite_reader_available_follows_the_real_import(monkeypatch):
    pytest.importorskip("tflite")
    assert tflite_reader_available() is True
    monkeypatch.setitem(sys.modules, "tflite", None)
    assert tflite_reader_available() is False


def test_extract_ops_parses_tiny_fixture_with_macs():
    pytest.importorskip("tflite")
    ops = extract_ops(_FIXTURE)
    assert len(ops) == 1
    op = ops[0]
    assert op.op == "FULLY_CONNECTED"
    # weights [2, 4] (const), bias [2] (const), input [1, 4], output [1, 2] --
    # MACs = out_elems(1*2) * in_features(4) = 8.
    assert op.macs == 8
    assert len(op.inputs) == 3 and len(op.outputs) == 1
    weight = next(t for t in op.inputs if t.is_const and len(t.shape) == 2)
    assert weight.shape == [2, 4]
    assert weight.dtype == "int8"
    activation = next(t for t in op.inputs if not t.is_const)
    assert activation.shape == [1, 4]
    assert op.outputs[0].shape == [1, 2] and not op.outputs[0].is_const


def test_extract_ops_honors_raw_bytes_without_reading_file(tmp_path):
    pytest.importorskip("tflite")
    raw = _FIXTURE.read_bytes()
    ops = extract_ops(tmp_path / "does-not-exist.tflite", raw=raw)
    assert len(ops) == 1 and ops[0].op == "FULLY_CONNECTED" and ops[0].macs == 8


# ---------------------------------------------------------------------------
# _estimate_macs: real per-op arithmetic, extracted (not hand-fed) via a
# minimal in-memory single-operator TFLite flatbuffer -- CONV_2D,
# TRANSPOSE_CONV and DEPTHWISE_CONV_2D previously had ZERO coverage (the only
# fixture, tiny_int8.tflite, is a single FULLY_CONNECTED); a wrong shape
# unpack in any of the three would ship green. The low-level flatbuffers
# calls here mirror tests/fixtures/models/gen_tiny_model.py's own builder
# (kept local rather than imported: that script is a stand-alone fixture
# regenerator, not a test helper module).
# ---------------------------------------------------------------------------

def _fb_buffer(t, b, data: bytes):
    if not data:
        t.BufferStart(b); return t.BufferEnd(b)
    d = b.CreateByteVector(data)
    t.BufferStart(b); t.BufferAddData(b, d); return t.BufferEnd(b)


def _fb_tensor(t, b, shape, ttype, buf_idx, name):
    nm = b.CreateString(name)
    t.TensorStartShapeVector(b, len(shape))
    for d in reversed(shape):
        b.PrependInt32(d)
    sh = b.EndVector()
    t.TensorStart(b)
    t.TensorAddShape(b, sh); t.TensorAddType(b, ttype); t.TensorAddBuffer(b, buf_idx)
    t.TensorAddName(b, nm)
    return t.TensorEnd(b)


def _fb_ivec(b, start_fn, vals):
    start_fn(b, len(vals))
    for v in reversed(vals):
        b.PrependInt32(v)
    return b.EndVector()


def _fb_offvec(b, start_fn, offs):
    start_fn(b, len(offs))
    for off in reversed(offs):
        b.PrependUOffsetTRelative(off)
    return b.EndVector()


def _build_single_op_model(*, builtin_op: int,
                            tensors: list[tuple[list[int], int, bytes]],
                            op_inputs: list[int], op_outputs: list[int]) -> bytes:
    """One-operator TFLite flatbuffer. @tensors is [(shape, ttype, data)] --
    data=b"" marks a non-const (activation) tensor (buffer 0, TFLite's shared
    empty buffer); non-empty data marks a const (weight/bias/output_shape)
    tensor, each getting its own buffer. Just enough structure for
    `extract_ops` to walk (tensors + one operator); no subgraph-IO metadata,
    since extract_ops never reads SubGraph.Inputs/Outputs."""
    import flatbuffers
    import tflite as t

    b = flatbuffers.Builder(1024)
    bufs = [_fb_buffer(t, b, b"")]
    buf_idx = []
    for _shape, _ttype, data in tensors:
        if data:
            bufs.append(_fb_buffer(t, b, data))
            buf_idx.append(len(bufs) - 1)
        else:
            buf_idx.append(0)
    buffers = _fb_offvec(b, t.ModelStartBuffersVector, bufs)
    tensor_offs = [_fb_tensor(t, b, shape, ttype, buf_idx[i], f"t{i}")
                   for i, (shape, ttype, _data) in enumerate(tensors)]
    tensors_vec = _fb_offvec(b, t.SubGraphStartTensorsVector, tensor_offs)
    sg_in = _fb_ivec(b, t.SubGraphStartInputsVector, op_inputs)
    sg_out = _fb_ivec(b, t.SubGraphStartOutputsVector, op_outputs)
    op_in = _fb_ivec(b, t.OperatorStartInputsVector, op_inputs)
    op_out = _fb_ivec(b, t.OperatorStartOutputsVector, op_outputs)
    t.OperatorStart(b)
    t.OperatorAddOpcodeIndex(b, 0)
    t.OperatorAddInputs(b, op_in)
    t.OperatorAddOutputs(b, op_out)
    op = t.OperatorEnd(b)
    operators = _fb_offvec(b, t.SubGraphStartOperatorsVector, [op])
    sg_name = b.CreateString("main")
    t.SubGraphStart(b)
    t.SubGraphAddTensors(b, tensors_vec)
    t.SubGraphAddInputs(b, sg_in)
    t.SubGraphAddOutputs(b, sg_out)
    t.SubGraphAddOperators(b, operators)
    t.SubGraphAddName(b, sg_name)
    sg = t.SubGraphEnd(b)
    subgraphs = _fb_offvec(b, t.ModelStartSubgraphsVector, [sg])
    t.OperatorCodeStart(b)
    t.OperatorCodeAddDeprecatedBuiltinCode(b, builtin_op)
    t.OperatorCodeAddBuiltinCode(b, builtin_op)
    t.OperatorCodeAddVersion(b, 1)
    oc = t.OperatorCodeEnd(b)
    opcodes = _fb_offvec(b, t.ModelStartOperatorCodesVector, [oc])
    t.ModelStart(b)
    t.ModelAddVersion(b, 3)
    t.ModelAddOperatorCodes(b, opcodes)
    t.ModelAddSubgraphs(b, subgraphs)
    t.ModelAddBuffers(b, buffers)
    b.Finish(t.ModelEnd(b), b"TFL3")
    return bytes(b.Output())


def test_conv_2d_macs_match_the_hand_computed_value(tmp_path):
    pytest.importorskip("tflite")
    import tflite as t
    raw = _build_single_op_model(
        builtin_op=t.BuiltinOperator.CONV_2D,
        tensors=[
            ([1, 8, 8, 3], t.TensorType.INT8, b""),                      # activation input
            ([16, 3, 3, 3], t.TensorType.INT8, b"\x01" * (16 * 3 * 3 * 3)),  # filter (OHWI)
            ([1, 8, 8, 16], t.TensorType.INT8, b""),                     # output
        ],
        op_inputs=[0, 1], op_outputs=[2],
    )
    ops = extract_ops(tmp_path / "conv.tflite", raw=raw)
    assert len(ops) == 1 and ops[0].op == "CONV_2D"
    # out_elems(1*8*8*16=1024) * kh(3) * kw(3) * in_c(3) = 27648.
    assert ops[0].macs == 27648


def test_conv_2d_filter_is_selected_positionally_not_by_first_matching_const(tmp_path):
    """A constant-folded input ahead of the real filter (same rank, same
    kind of tensor a const-folding pass would leave behind) must not shadow
    it -- the filter is TFLite's kFilterTensor, always input[1], never
    'whichever const input matches the expected rank first'."""
    pytest.importorskip("tflite")
    import tflite as t
    raw = _build_single_op_model(
        builtin_op=t.BuiltinOperator.CONV_2D,
        tensors=[
            ([1, 8, 8, 3], t.TensorType.INT8, b"\x01" * (1 * 8 * 8 * 3)),     # shadowing const
            ([16, 3, 3, 3], t.TensorType.INT8, b"\x01" * (16 * 3 * 3 * 3)),   # the REAL filter (input[1])
            ([1, 8, 8, 16], t.TensorType.INT8, b""),                         # output
        ],
        op_inputs=[0, 1], op_outputs=[2],
    )
    ops = extract_ops(tmp_path / "shadow.tflite", raw=raw)
    assert len(ops) == 1
    # Correct (input[1], the real filter) -> 27648. The shadow at input[0]
    # (misread as an [out_c=1, kh=8, kw=8, in_c=3] filter) would give
    # 1024*8*8*3 = 196608 -- 7x too high.
    assert ops[0].macs == 27648


def test_depthwise_conv_2d_macs_match_the_hand_computed_value(tmp_path):
    pytest.importorskip("tflite")
    import tflite as t
    raw = _build_single_op_model(
        builtin_op=t.BuiltinOperator.DEPTHWISE_CONV_2D,
        tensors=[
            ([1, 8, 8, 16], t.TensorType.INT8, b""),                        # activation input
            ([1, 3, 3, 16], t.TensorType.INT8, b"\x01" * (1 * 3 * 3 * 16)),  # filter [1, kh, kw, out_c]
            ([1, 8, 8, 16], t.TensorType.INT8, b""),                        # output
        ],
        op_inputs=[0, 1], op_outputs=[2],
    )
    ops = extract_ops(tmp_path / "dw.tflite", raw=raw)
    assert len(ops) == 1 and ops[0].op == "DEPTHWISE_CONV_2D"
    # out_elems(1*8*8*16=1024) * kh(3) * kw(3) = 9216.
    assert ops[0].macs == 9216


def test_transpose_conv_costs_off_input_elements_not_output(tmp_path):
    """TRANSPOSE_CONV upsamples (output is stride-larger than input), so the
    filter slides over the INPUT, not the output -- costing off out_elems the
    way CONV_2D correctly does over-counts by stride^2."""
    pytest.importorskip("tflite")
    import tflite as t
    raw = _build_single_op_model(
        builtin_op=t.BuiltinOperator.TRANSPOSE_CONV,
        tensors=[
            ([4], t.TensorType.INT32, b"\x00" * 16),                        # output_shape (kOutputShapeTensor=0)
            ([16, 3, 3, 3], t.TensorType.INT8, b"\x01" * (16 * 3 * 3 * 3)),  # filter (kWeightsTensor=1, OHWI)
            ([1, 8, 8, 3], t.TensorType.INT8, b""),                         # input activation (kDataInputTensor=2)
            ([1, 16, 16, 16], t.TensorType.INT8, b""),                      # output, stride-2 upsample
        ],
        op_inputs=[0, 1, 2], op_outputs=[3],
    )
    ops = extract_ops(tmp_path / "tconv.tflite", raw=raw)
    assert len(ops) == 1 and ops[0].op == "TRANSPOSE_CONV"
    # Correct: in_elems(1*8*8*3=192) * kh(3) * kw(3) * out_c(16) = 27648.
    # The old (CONV_2D-shared, output-elements) formula would have scored
    # out_elems(1*16*16*16=4096) * kh*kw*in_c(3*3*3=27) = 110592 -- exactly
    # a stride^2 (2^2 = 4) over-count of the correct value.
    assert ops[0].macs == 27648


def test_transpose_conv_is_not_zeroed_by_a_zero_element_output_shape(tmp_path):
    """TRANSPOSE_CONV's formula never reads out_elems (see the test above),
    so a declared output shape that happens to compute to 0 elements (a
    dynamic/-1 dim, or a genuinely empty output tensor) must not zero out an
    otherwise fully-computable in_elems * kh * kw * out_c estimate -- the
    out_elems==0 early-return exists for the OTHER three ops
    (FULLY_CONNECTED/CONV_2D/DEPTHWISE_CONV_2D), which do cost off out_elems,
    and must not vestigially short-circuit this one too."""
    pytest.importorskip("tflite")
    import tflite as t
    raw = _build_single_op_model(
        builtin_op=t.BuiltinOperator.TRANSPOSE_CONV,
        tensors=[
            ([4], t.TensorType.INT32, b"\x00" * 16),                        # output_shape (kOutputShapeTensor=0)
            ([16, 3, 3, 3], t.TensorType.INT8, b"\x01" * (16 * 3 * 3 * 3)),  # filter (kWeightsTensor=1, OHWI)
            ([1, 8, 8, 3], t.TensorType.INT8, b""),                         # input activation (kDataInputTensor=2)
            ([1, 0, 16, 16], t.TensorType.INT8, b""),                       # output: a 0 dim -> 0 elements
        ],
        op_inputs=[0, 1, 2], op_outputs=[3],
    )
    ops = extract_ops(tmp_path / "tconv-zero-out.tflite", raw=raw)
    assert len(ops) == 1 and ops[0].op == "TRANSPOSE_CONV"
    # Same in_elems(192) * kh(3) * kw(3) * out_c(16) = 27648 as the healthy
    # case above -- the (irrelevant) zero-element output shape must not
    # collapse this to 0.
    assert ops[0].macs == 27648
