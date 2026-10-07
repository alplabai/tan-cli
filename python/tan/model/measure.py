# SPDX-License-Identifier: Apache-2.0
"""Host reference run + A/B compare (tan-cli#1287, host tier only).

Ported from alp-sdk `integration/model-edge-ai` (`scripts/alp_model/measure.py`,
alp-sdk#933). The host run (`backend="cpu-host"`) is a FUNCTIONAL + host-latency
reference, NOT the target SoM's performance. The on-device tier (target
latency, peak arena SRAM, per-rail energy via the on-board monitor IC) is the
bench-gated follow-on that fills the SAME `RunResult` schema with real numbers;
until then `power_mj`/`peak_sram_kib` stay `None` and nothing here fabricates
them. The energy-integration math and estimate-vs-measured feedback of the
reference belong to that tier and are not ported yet.

`numpy`/`onnxruntime` (the optional `model` extra) are imported inside the
functions that use them.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any


class MeasureError(Exception):
    """The model could not be loaded or run for measurement."""


@dataclass(frozen=True)
class RunResult:
    backend: str                 # "cpu-host" (reference) | on-device backend later
    latency_ms: float            # median wall-clock per inference
    output_argmax: int | None
    peak_sram_kib: int | None    # None on host — on-device only
    power_mj: float | None       # None on host — on-board monitor read (HW-gated)
    runs: int


def default_input(onnx_path: Path, *, seed: int = 0) -> Any:
    """Deterministic random sample matching the model's first input shape.
    Dynamic dims collapse to 1 (intent: a dynamic BATCH dim; a symbolic H/W
    would also become 1). Wrapped in MeasureError so a bad model fails clean."""
    import numpy as np
    import onnxruntime as ort
    try:
        sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
        shape = [(1 if not isinstance(d, int) else d) for d in sess.get_inputs()[0].shape]
    except Exception as exc:
        raise MeasureError(f"could not load model {Path(onnx_path).name}: {exc}") from exc
    return np.random.default_rng(seed).standard_normal(shape).astype(np.float32)


def run_host(onnx_path: Path, input_array: Any, *, runs: int = 20) -> RunResult:
    import numpy as np
    import onnxruntime as ort
    try:
        sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
        name = sess.get_inputs()[0].name
        x = input_array.astype(np.float32)
        out = sess.run(None, {name: x})[0]          # warm-up + a real output
        times = []
        for _ in range(max(1, runs)):
            t0 = perf_counter()
            sess.run(None, {name: x})
            times.append((perf_counter() - t0) * 1000.0)
    except Exception as exc:
        raise MeasureError(f"host run failed: {exc}") from exc
    return RunResult(backend="cpu-host", latency_ms=round(median(times), 3),
                     output_argmax=int(np.asarray(out).ravel().argmax()),
                     peak_sram_kib=None, power_mj=None, runs=len(times))


@dataclass(frozen=True)
class ABComparison:
    faster: str                  # "a" | "b" | "tie"
    latency_ratio: float | None  # b.latency_ms / a.latency_ms; None if a.latency_ms == 0
                                  # (avoids emitting non-finite JSON `Infinity`)
    a_latency_ms: float
    b_latency_ms: float
    size_delta_bytes: int | None  # size_b - size_a


def compare(a: RunResult, b: RunResult, *, size_a: int | None = None,
            size_b: int | None = None) -> ABComparison:
    if a.latency_ms < b.latency_ms:
        faster = "a"
    elif b.latency_ms < a.latency_ms:
        faster = "b"
    else:
        faster = "tie"
    ratio = (b.latency_ms / a.latency_ms) if a.latency_ms else None
    delta = (size_b - size_a) if (size_a is not None and size_b is not None) else None
    return ABComparison(faster=faster,
                        latency_ratio=round(ratio, 4) if ratio is not None else None,
                        a_latency_ms=a.latency_ms, b_latency_ms=b.latency_ms,
                        size_delta_bytes=delta)
