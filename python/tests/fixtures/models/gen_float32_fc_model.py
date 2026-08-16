# tests/fixtures/models/gen_float32_fc_model.py
"""Generate a tiny FLOAT32 (deliberately UNQUANTIZED) FULLY_CONNECTED
.tflite fixture (no TensorFlow).

Structurally the same single-op graph as `gen_tiny_model.py`'s
`tiny_int8.tflite`, but FLOAT32 tensors throughout instead of int8 --
FULLY_CONNECTED is a real Ethos-U op vela's own supported-ops table lists by
name, but vela's `tflite_supported_operators.py` also enforces a Generic
constraint that every feature-map tensor's dtype be one of
{BOOL, INT16, INT32, INT64, INT8, UINT8} -- FLOAT32 is not in that set, so
vela rejects this graph's only operator outright and falls the WHOLE model
back to the CPU. Measured (`ethos-u-vela` 5.1.0):

    $ vela float32_fc.tflite --accelerator-config ethos-u85-256 ...
    Warning (supported operators) operator: FULLY_CONNECTED, ofm: 'output'
    Reason: Operation has tensor with unsupported DataType Float32
    CPU operators = 1 (100.0%)
    NPU operators = 0 (0.0%)
    (exit code 0)

This is the exact "vela exits 0 on a full CPU fallback" scenario
`tan.model.check._report_from_vela_compile` exists to read honestly instead
of reporting `"fits"` for (tan-cli#782 review BLOCKER) -- a REAL compile of
this fixture is the only way to prove that fix; no amount of monkeypatching
`VelaAdapter.compile` can.

Run to (re)create float32_fc.tflite:
    py -3.14 tests/fixtures/models/gen_float32_fc_model.py
Requires the `model-compile` extra (tflite + flatbuffers)."""
from __future__ import annotations
import struct
from pathlib import Path

import flatbuffers
import tflite as t

K, N = 4, 2


def _buffer(b, data: bytes):
    if not data:
        t.BufferStart(b); return t.BufferEnd(b)
    d = b.CreateByteVector(data)
    t.BufferStart(b); t.BufferAddData(b, d); return t.BufferEnd(b)


def _tensor(b, shape, ttype, buf_idx, name):
    nm = b.CreateString(name)
    t.TensorStartShapeVector(b, len(shape))
    for d in reversed(shape):
        b.PrependInt32(d)
    sh = b.EndVector()
    t.TensorStart(b)
    t.TensorAddShape(b, sh); t.TensorAddType(b, ttype); t.TensorAddBuffer(b, buf_idx)
    t.TensorAddName(b, nm)
    return t.TensorEnd(b)


# flatbuffers vectors are written back-to-front: open with the element count, then
# Prepend each element in REVERSE so they end up in forward order. (_offvec below
# does the same for vectors of table offsets.)
def _ivec(b, start_fn, vals):
    start_fn(b, len(vals))
    for v in reversed(vals):
        b.PrependInt32(v)
    return b.EndVector()


def _offvec(b, start_fn, offs):
    start_fn(b, len(offs))
    for off in reversed(offs):
        b.PrependUOffsetTRelative(off)
    return b.EndVector()


def build() -> bytes:
    b = flatbuffers.Builder(1024)
    weights = struct.pack(f"<{N * K}f", *([0.1] * (N * K)))
    bias = struct.pack(f"<{N}f", *([0.0] * N))
    bufs = [_buffer(b, b""), _buffer(b, weights), _buffer(b, bias)]
    buffers = _offvec(b, t.ModelStartBuffersVector, bufs)
    t_in = _tensor(b, [1, K], t.TensorType.FLOAT32, 0, "input")
    t_w = _tensor(b, [N, K], t.TensorType.FLOAT32, 1, "weights")
    t_b = _tensor(b, [N], t.TensorType.FLOAT32, 2, "bias")
    t_out = _tensor(b, [1, N], t.TensorType.FLOAT32, 0, "output")
    tensors = _offvec(b, t.SubGraphStartTensorsVector, [t_in, t_w, t_b, t_out])
    sg_in = _ivec(b, t.SubGraphStartInputsVector, [0])
    sg_out = _ivec(b, t.SubGraphStartOutputsVector, [3])
    op_in = _ivec(b, t.OperatorStartInputsVector, [0, 1, 2])
    op_out = _ivec(b, t.OperatorStartOutputsVector, [3])
    t.FullyConnectedOptionsStart(b); fc = t.FullyConnectedOptionsEnd(b)
    t.OperatorStart(b)
    t.OperatorAddOpcodeIndex(b, 0); t.OperatorAddInputs(b, op_in); t.OperatorAddOutputs(b, op_out)
    t.OperatorAddBuiltinOptionsType(b, t.BuiltinOptions.FullyConnectedOptions)
    t.OperatorAddBuiltinOptions(b, fc)
    op = t.OperatorEnd(b)
    operators = _offvec(b, t.SubGraphStartOperatorsVector, [op])
    sg_name = b.CreateString("main")
    t.SubGraphStart(b)
    t.SubGraphAddTensors(b, tensors); t.SubGraphAddInputs(b, sg_in)
    t.SubGraphAddOutputs(b, sg_out); t.SubGraphAddOperators(b, operators); t.SubGraphAddName(b, sg_name)
    sg = t.SubGraphEnd(b)
    subgraphs = _offvec(b, t.ModelStartSubgraphsVector, [sg])
    t.OperatorCodeStart(b)
    t.OperatorCodeAddDeprecatedBuiltinCode(b, t.BuiltinOperator.FULLY_CONNECTED)
    t.OperatorCodeAddBuiltinCode(b, t.BuiltinOperator.FULLY_CONNECTED)
    t.OperatorCodeAddVersion(b, 1)
    oc = t.OperatorCodeEnd(b)
    opcodes = _offvec(b, t.ModelStartOperatorCodesVector, [oc])
    desc = b.CreateString("alp float32 fc fixture (vela-rejected, unquantized)")
    t.ModelStart(b)
    t.ModelAddVersion(b, 3); t.ModelAddOperatorCodes(b, opcodes)
    t.ModelAddSubgraphs(b, subgraphs); t.ModelAddBuffers(b, buffers); t.ModelAddDescription(b, desc)
    b.Finish(t.ModelEnd(b), b"TFL3")
    return bytes(b.Output())


if __name__ == "__main__":
    out = Path(__file__).with_name("float32_fc.tflite")
    out.write_bytes(build())
    print(f"wrote {out} ({out.stat().st_size} bytes)")
