# SPDX-License-Identifier: Apache-2.0
"""Extract model tensor + operator descriptors from a TFLite flatbuffer.

`extract_io` feeds the `.alpmodel` manifest (Stage-1a `Tensor` records,
dtype/rank/shape/scale/zp -> mirrors alp_inference_tensor_t). `extract_ops`
feeds `tan.model.analyze`'s static NPU-eligibility screen (ADR-0028 amendment):
per-operator name + shape/const-ness + a best-effort MAC estimate, so the
screen can weight coverage by compute rather than by op count.

Both are best-effort with the SAME contract: return an empty result when the
source isn't a .tflite, the `tflite` reader (from the `model-io` extra) isn't
installed, or the bytes don't parse -- never raise. The model still packages
(or scores as `undetermined`), just without this metadata
(compile-what's-available)."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from .manifest import Tensor

# Shared TFLite TensorType -> SDK dtype-string mapping. Honest about the gap:
# an unmapped type (e.g. INT64/BOOL) reads as "tflite:<code>" rather than a
# silently wrong "f32" -- the SDK Tensor.dtype vocabulary has no such types.
# Built by reflecting on tflite.TensorType rather than hand-transcribing the
# numeric codes (INT16 is 7, INT8 is 9 -- NOT 5/7 as a naive reading of
# schema.fbs declaration order would suggest; a hardcoded literal here is
# exactly the transcription trap this sidesteps).
_DTYPE_WANTED = {"FLOAT32": "f32", "FLOAT16": "f16", "INT32": "int32",
                  "UINT8": "uint8", "INT16": "int16", "INT8": "int8"}


def _dtype_names(tflite) -> dict[int, str]:
    tt = tflite.TensorType
    return {getattr(tt, k): v for k, v in _DTYPE_WANTED.items()}


def _dtype_name(dtype_names: dict[int, str], code: int) -> str:
    return dtype_names.get(code, f"tflite:{code}")


def extract_io(source: Path, *, raw: bytes | None = None) -> tuple[list[Tensor], list[Tensor]]:
    if source.suffix.lower() != ".tflite":
        return [], []                       # ONNX I/O extraction is a later follow-up
    try:
        import tflite
    except ImportError:
        return [], []                       # parser not installed -> skip
    # Reuse the caller's already-read bytes when provided (build_model reads the
    # source once for the manifest sha); an OSError on our own read is a real
    # failure, not a parse problem.
    if raw is None:
        raw = source.read_bytes()
    try:
        model = tflite.Model.GetRootAs(raw, 0)
        if model.SubgraphsLength() == 0:
            return [], []
        g = model.Subgraphs(0)
        dtype_names = _dtype_names(tflite)
        ins = [_describe(g, g.Inputs(i), dtype_names) for i in range(g.InputsLength())]
        outs = [_describe(g, g.Outputs(i), dtype_names) for i in range(g.OutputsLength())]
        return ins, outs
    except Exception:
        return [], []                       # malformed / unexpected schema -> no metadata


def _describe(g, idx: int, dtype_names: dict[int, str]) -> Tensor:
    ten = g.Tensors(idx)
    shape = [int(ten.Shape(i)) for i in range(ten.ShapeLength())]
    qp = ten.Quantization()
    scale = float(qp.Scale(0)) if qp is not None and qp.ScaleLength() else 0.0
    zp = int(qp.ZeroPoint(0)) if qp is not None and qp.ZeroPointLength() else 0
    return Tensor(dtype=_dtype_name(dtype_names, ten.Type()), rank=len(shape), shape=shape,
                  scale=scale, zp=zp)


@dataclass(frozen=True)
class TensorDesc:
    """One operator's input/output tensor, static-shape only (no runtime data)."""

    shape: list[int]
    dtype: str
    nbytes: int          # 0 for a non-const (activation) tensor
    is_const: bool        # True when the flatbuffer carries baked-in data (weights/bias)


@dataclass(frozen=True)
class OpDesc:
    """One model operator, as spelled in its own source vocabulary (TFLite
    builtin-op name today; an ONNX op-type string once ONNX extraction lands)."""

    op: str
    inputs: list[TensorDesc] = field(default_factory=list)
    outputs: list[TensorDesc] = field(default_factory=list)
    macs: int = 0          # best-effort static estimate; 0 when not computable


def extract_ops(source: Path, *, raw: bytes | None = None) -> list[OpDesc]:
    """Walk a TFLite flatbuffer's first subgraph into `OpDesc`s for the static
    NPU-eligibility screen. Same best-effort contract as `extract_io`: `[]` for
    a non-.tflite source, a missing `tflite` reader, or unparseable bytes --
    never raises. ONNX operator extraction is a follow-on; a `.onnx` source
    always returns `[]` here, so `tan.model.analyze` reports `undetermined`
    for the ONNX-ingesting backends rather than guessing."""
    if source.suffix.lower() != ".tflite":
        return []
    try:
        import tflite
    except ImportError:
        return []
    if raw is None:
        raw = source.read_bytes()
    try:
        model = tflite.Model.GetRootAs(raw, 0)
        if model.SubgraphsLength() == 0:
            return []
        g = model.Subgraphs(0)
        opcodes = [model.OperatorCodes(i) for i in range(model.OperatorCodesLength())]
        names = _builtin_op_names(tflite)
        dtype_names = _dtype_names(tflite)
        custom_code = tflite.BuiltinOperator.CUSTOM
        ops: list[OpDesc] = []
        for i in range(g.OperatorsLength()):
            op = g.Operators(i)
            oc = opcodes[op.OpcodeIndex()]
            code = oc.BuiltinCode() or oc.DeprecatedBuiltinCode()
            if code == custom_code:
                raw_name = oc.CustomCode()
                name = raw_name.decode("utf-8", "replace") if raw_name else "CUSTOM"
            else:
                name = names.get(code, f"tflite:{code}")
            in_tensors = [_tensor_desc(model, g, op.Inputs(j), dtype_names)
                          for j in range(op.InputsLength()) if op.Inputs(j) >= 0]
            # -1 marks an absent optional input (e.g. a bias-less CONV_2D).
            out_tensors = [_tensor_desc(model, g, op.Outputs(j), dtype_names)
                           for j in range(op.OutputsLength()) if op.Outputs(j) >= 0]
            ops.append(OpDesc(op=name, inputs=in_tensors, outputs=out_tensors,
                               macs=_estimate_macs(name, in_tensors, out_tensors)))
        return ops
    except Exception:
        return []                       # malformed / unexpected schema -> no metadata


def _builtin_op_names(tflite) -> dict[int, str]:
    bo = tflite.BuiltinOperator
    return {getattr(bo, n): n for n in dir(bo) if n.isupper() and isinstance(getattr(bo, n), int)}


def _tensor_desc(model, g, idx: int, dtype_names: dict[int, str]) -> TensorDesc:
    ten = g.Tensors(idx)
    shape = [int(ten.Shape(i)) for i in range(ten.ShapeLength())]
    buf = model.Buffers(ten.Buffer())
    nbytes = buf.DataLength() if buf is not None else 0
    return TensorDesc(shape=shape, dtype=_dtype_name(dtype_names, ten.Type()), nbytes=nbytes,
                       is_const=nbytes > 0)


def _estimate_macs(op_name: str, inputs: list[TensorDesc], outputs: list[TensorDesc]) -> int:
    """Best-effort MAC count from static shapes for the compute-dominant op
    kinds (conv/dense); 0 for everything else. MAC weighting exists to stop
    op-count coverage from hiding a conv backbone behind a wall of cheap
    elementwise ops, not to model every op's cost exactly -- an upper-bound
    screening heuristic, same stance as the rest of this module."""
    if not outputs:
        return 0
    out_elems = 1
    for d in outputs[0].shape:
        out_elems *= max(int(d), 0)
    if out_elems == 0:
        return 0

    def _const_weight(rank: int) -> TensorDesc | None:
        return next((t for t in inputs if t.is_const and len(t.shape) == rank), None)

    if op_name == "FULLY_CONNECTED":
        w = _const_weight(2)               # [out_features, in_features]
        return out_elems * w.shape[-1] if w else 0
    if op_name in ("CONV_2D", "TRANSPOSE_CONV"):
        w = _const_weight(4)               # [out_c, kh, kw, in_c] (OHWI)
        if w is None:
            return 0
        _, kh, kw, in_c = w.shape
        return out_elems * kh * kw * in_c
    if op_name == "DEPTHWISE_CONV_2D":
        w = _const_weight(4)               # [1, kh, kw, out_c]
        if w is None:
            return 0
        _, kh, kw, _ = w.shape
        return out_elems * kh * kw
    return 0
