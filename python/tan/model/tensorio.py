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
# numeric codes: the codes are non-contiguous with the SDK dtype vocabulary
# (six wanted types out of eleven declared), so a hand-maintained literal
# would need to track schema.fbs by hand for no benefit over reflection.
_DTYPE_WANTED = {"FLOAT32": "f32", "FLOAT16": "f16", "INT32": "int32",
                  "UINT8": "uint8", "INT16": "int16", "INT8": "int8"}


def tflite_reader_available() -> bool:
    """True when the `tflite` reader (the `model-io` extra) is importable on
    this host. `extract_io`/`extract_ops` deliberately swallow a missing
    reader into an empty best-effort result -- the right contract for
    `build_model`'s "compile what's available" stance -- but a caller that
    needs to tell "the reader isn't installed" apart from "this model
    genuinely has no operators" (both look like `[]` from `extract_ops`,
    `tan.model.check`'s MAJOR 2 review) reads this directly instead of
    re-deriving it from an empty list, which the second case returns too."""
    try:
        import tflite  # noqa: F401
    except ImportError:
        return False
    return True


def _dtype_names(tflite) -> dict[int, str]:
    tt = tflite.TensorType
    return {getattr(tt, k): v for k, v in _DTYPE_WANTED.items()}


def _dtype_name(dtype_names: dict[int, str], code: int) -> str:
    return dtype_names.get(code, f"tflite:{code}")


def extract_io(source: Path, *, raw: bytes | None = None) -> tuple[list[Tensor], list[Tensor]]:
    if source.suffix.lower() != ".tflite":
        return [], []                       # ONNX I/O extraction is a later follow-up
    # Reuse the caller's already-read bytes when provided (build_model reads the
    # source once for the manifest sha); an OSError on our own read is a real
    # failure, not a parse problem -- read (or take the caller's bytes) BEFORE
    # the `tflite` import check below, so "the source is unreadable" is never
    # masked by "the reader isn't installed". Both would otherwise collapse to
    # the identical `[], []`, which silently hides a real missing/unreadable
    # source on exactly the bare-install (no `model-io` extra) host this
    # degrade path exists for.
    if raw is None:
        raw = source.read_bytes()
    try:
        import tflite
    except ImportError:
        return [], []                       # parser not installed -> skip
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
    # The vocabulary `op` is spelled in -- "tflite" (the only extractor that
    # exists today) or "onnx" once ONNX extraction lands. `tan.model.analyze`
    # compares THIS against a support table's own `op_namespace`, never the
    # caller-supplied `src_format`, so a mislabelled format cannot make two
    # incomparable op vocabularies look like a match. Required kw_only, no
    # default: a silent "tflite" default let a caller build an ONNX-spelled
    # OpDesc that still claimed the TFLite vocabulary, which the namespace
    # guard in analyze.py then trusted -- every construction site (extractor
    # or test) must say which vocabulary it means.
    op_namespace: str = field(kw_only=True)


def extract_ops(source: Path, *, raw: bytes | None = None) -> list[OpDesc]:
    """Walk a TFLite flatbuffer's first subgraph into `OpDesc`s for the static
    NPU-eligibility screen. Same best-effort contract as `extract_io`: `[]` for
    a non-.tflite source, a missing `tflite` reader, or unparseable bytes --
    never raises. ONNX operator extraction is a follow-on; a `.onnx` source
    always returns `[]` here, so `tan.model.analyze` reports `undetermined`
    for the ONNX-ingesting backends rather than guessing."""
    if source.suffix.lower() != ".tflite":
        return []
    # Same ordering fix as extract_io above, for the same reason: read (or
    # take the caller's bytes) BEFORE checking whether `tflite` is importable,
    # so an unreadable/missing source still raises rather than being masked
    # by "the reader isn't installed" on a bare-install host.
    if raw is None:
        raw = source.read_bytes()
    try:
        import tflite
    except ImportError:
        return []
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
                               macs=_estimate_macs(name, in_tensors, out_tensors),
                               op_namespace="tflite"))
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


# Positional index of the filter/weights input tensor per TFLite operator
# kernel (kFilterTensor / kWeightsTensor in the TFLite C++ kernels), keyed by
# builtin-op name. NOT "the first const input of the expected rank" -- that
# heuristic silently grabs whichever constant happens to come first once a
# graph has been const-folded, which shadows the real filter and can inflate
# the MAC estimate by many times (see test_conv_2d_filter_is_selected_
# positionally_not_by_first_matching_const). Every op below carries a
# MANDATORY (never optional) tensor at index 0 -- the activation for
# FULLY_CONNECTED/CONV_2D/DEPTHWISE_CONV_2D, kOutputShapeTensor for
# TRANSPOSE_CONV -- so the filter index below never shifts under an
# absent-optional-input reindex; only the trailing bias tensor is ever
# optional (see _ACTIVATION_INPUT_INDEX below for TRANSPOSE_CONV's one
# further wrinkle: its activation isn't at index 0 the way it is here).
_FILTER_INPUT_INDEX = {
    "FULLY_CONNECTED": 1,     # kInputTensor=0, kWeightsTensor=1, kBiasTensor=2 (optional)
    "CONV_2D": 1,             # kInputTensor=0, kFilterTensor=1, kBiasTensor=2 (optional)
    "DEPTHWISE_CONV_2D": 1,   # kInputTensor=0, kFilterTensor=1, kBiasTensor=2 (optional)
    "TRANSPOSE_CONV": 1,      # kOutputShapeTensor=0, kWeightsTensor=1, kDataInputTensor=2, kBiasTensor=3 (optional)
}

# TRANSPOSE_CONV alone needs the INPUT activation positionally too (its
# filter is applied per input position, not per output position -- see the
# formula below), and that activation is not at index 0 the way it is for
# every other op here.
_ACTIVATION_INPUT_INDEX = {
    "TRANSPOSE_CONV": 2,      # kDataInputTensor
}


def _filter_weight(op_name: str, inputs: list[TensorDesc], rank: int) -> TensorDesc | None:
    idx = _FILTER_INPUT_INDEX.get(op_name)
    if idx is None or idx >= len(inputs):
        return None
    w = inputs[idx]
    return w if w.is_const and len(w.shape) == rank else None


def _elem_count(shape: list[int]) -> int:
    n = 1
    for d in shape:
        n *= max(int(d), 0)
    return n


def _estimate_macs(op_name: str, inputs: list[TensorDesc], outputs: list[TensorDesc]) -> int:
    """Best-effort MAC count from static shapes for the compute-dominant op
    kinds (conv/dense); 0 for everything else. MAC weighting exists to stop
    op-count coverage from hiding a conv backbone behind a wall of cheap
    elementwise ops, not to model every op's cost exactly -- an upper-bound
    screening heuristic, same stance as the rest of this module."""
    if not outputs:
        return 0

    if op_name == "TRANSPOSE_CONV":
        # Unlike CONV_2D, the filter slides over the INPUT (the op upsamples:
        # output is stride-larger than input), so costing off the output
        # element count -- as CONV_2D correctly does -- over-counts by
        # stride^2. Cost off the actual input-activation tensor's element
        # count instead -- this branch never reads out_elems, so it must sit
        # ahead of (not behind) the out_elems==0 guard below: an output shape
        # that happens to compute to 0 elements (e.g. a dynamic dim) must not
        # zero out a fully-computable in_elems * kh * kw * out_c estimate.
        w = _filter_weight(op_name, inputs, 4)              # [out_c, kh, kw, in_c] (OHWI)
        if w is None:
            return 0
        out_c, kh, kw, _ = w.shape
        act_idx = _ACTIVATION_INPUT_INDEX[op_name]
        if act_idx >= len(inputs):
            return 0
        in_elems = _elem_count(inputs[act_idx].shape)
        if in_elems == 0:
            return 0
        return in_elems * kh * kw * out_c

    out_elems = _elem_count(outputs[0].shape)
    if out_elems == 0:
        return 0

    if op_name == "FULLY_CONNECTED":
        w = _filter_weight(op_name, inputs, 2)             # [out_features, in_features]
        return out_elems * w.shape[-1] if w else 0
    if op_name == "CONV_2D":
        w = _filter_weight(op_name, inputs, 4)              # [out_c, kh, kw, in_c] (OHWI)
        if w is None:
            return 0
        _, kh, kw, in_c = w.shape
        return out_elems * kh * kw * in_c
    if op_name == "DEPTHWISE_CONV_2D":
        w = _filter_weight(op_name, inputs, 4)              # [1, kh, kw, out_c]
        if w is None:
            return 0
        _, kh, kw, _ = w.shape
        return out_elems * kh * kw
    return 0
