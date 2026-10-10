# SPDX-License-Identifier: Apache-2.0
"""Host reference run + A/B compare (tan-cli#1287, host tier only).

Ported from alp-sdk `integration/model-edge-ai` (`scripts/alp_model/measure.py`,
alp-sdk#933). The host run (`backend="cpu-host"`) is a FUNCTIONAL + host-latency
reference, NOT the target SoM's performance. The on-device tier (target
latency, peak arena SRAM, per-rail energy via the on-board monitor IC) is the
bench-gated follow-on that fills the SAME `RunResult` schema with real numbers;
the host run leaves `power_mj`/`peak_sram_kib` `None` and nothing here
fabricates them; the device tier (`tan.core.model_device`) fills `peak_sram_kib`
from the app's ENERGY-CFG `sram_peak_bytes` and energy from its capture. The energy-integration math (`integrate_energy`, `windowed_delta`,
`EnergyMeasurement`) is the on-device tier's, ported unchanged and PURE; the
estimate-vs-measured feedback loop is not ported yet.

`numpy`/`onnxruntime` (the optional `model` extra) are imported inside the
functions that use them.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any, Sequence


class MeasureError(Exception):
    """The model could not be loaded or run for measurement."""


# (t_seconds, power_watts) — power_watts is an on-board monitor Power-register (3h) read.
PowerSample = tuple[float, float]


def integrate_energy(samples: Sequence[PowerSample]) -> float:
    """Trapezoidal time-integration of power samples into millijoules:
    E = Σ 0.5*(P_i + P_i+1)*Δt over consecutive samples (Δt in seconds,
    P in watts -> joules, *1000 -> mJ). Samples must be in ascending t order
    (the on-board monitor has no on-chip energy accumulator, so this is the whole
    point of taking repeated Power-register samples host-side).
    <2 samples -> 0.0 (no interval to integrate; not an error — a single
    reading carries no energy information, so returning 0.0 lets a caller
    that hasn't collected a real window yet compose without special-casing)."""
    if len(samples) < 2:
        return 0.0
    energy_j = 0.0
    for (t0, p0), (t1, p1) in zip(samples, samples[1:]):
        energy_j += 0.5 * (p0 + p1) * (t1 - t0)
    return energy_j * 1000.0


def windowed_delta(active_samples: Sequence[PowerSample],
                    idle_samples: Sequence[PowerSample], n_inferences: int) -> float:
    """Idle-baseline-subtracted energy per inference, in mJ: the delta between
    an active (inferring) window and an idle window, divided by how many
    inferences ran in the active window.

    Precondition: active_samples and idle_samples MUST cover equal wall-clock
    duration (same integration window length). This is a subtraction of two
    integrals, not a rate comparison -- unequal durations bias the baseline
    (e.g. a longer idle window subtracts more idle energy than the active
    window actually contains). The caller/firmware is responsible for taking
    symmetric windows; this function does not (and should not) assert it,
    since real hardware timing has jitter that would make a hard equality
    check false-trip."""
    if n_inferences < 1:
        raise ValueError(f"n_inferences must be >= 1, got {n_inferences}")
    return (integrate_energy(active_samples) - integrate_energy(idle_samples)) / n_inferences


@dataclass(frozen=True)
class EnergyMeasurement:
    """A real bench-measured energy result — never fabricate one; `run_host()`
    leaves `RunResult.energy` as None instead. `source`/`scope` default to (and
    are validated as) the honest labels: this is a board-level carrier-rail
    delta, NEVER an isolated NPU/U85/U55/M55 silicon energy figure."""
    value_mj_per_inference: float
    rails: list[str]
    n_inferences: int
    window_ms: float
    sample_count: int
    spread_mj: float | None = None      # std/spread across M repeated windows;
                                         # None for a single window
    source: str = "measured"
    scope: str = "carrier-rail-delta"

    def __post_init__(self) -> None:
        if self.source != "measured" or self.scope != "carrier-rail-delta":
            raise ValueError(
                "EnergyMeasurement is board-level-delta only: source must be "
                "'measured' and scope 'carrier-rail-delta' — never label this "
                "NPU/U85/U55/M55 silicon energy.")


@dataclass(frozen=True)
class RunResult:
    backend: str                 # "cpu-host" (reference) | on-device backend later
    latency_ms: float            # median wall-clock per inference
    output_argmax: int | None
    peak_sram_kib: float | None    # None on host — on-device only
    power_mj: float | None       # None on host — on-board monitor read (HW-gated)
    runs: int
    energy: EnergyMeasurement | None = None  # None until a real bench run populates it


_ORT_DTYPES = {
    "float": "float32", "float16": "float16", "double": "float64",
    "int64": "int64", "int32": "int32", "int16": "int16", "int8": "int8",
    "uint64": "uint64", "uint32": "uint32", "uint16": "uint16", "uint8": "uint8",
    "bool": "bool",
}


def _np_dtype(ort_type: str) -> str:
    """`tensor(int64)` -> `int64`; anything unrecognised (sequences, strings)
    falls back to float32, the previous behaviour (tan-cli#1486)."""
    inner = ort_type[ort_type.find("(") + 1:ort_type.rfind(")")] if "(" in ort_type else ort_type
    return _ORT_DTYPES.get(inner, "float32")


def _sample(shape: list, ort_type: str, seed: int) -> Any:
    import numpy as np
    dtype = _np_dtype(ort_type)
    dims = [(1 if not isinstance(d, int) else d) for d in shape]
    if np.dtype(dtype).kind == "f":
        return np.random.default_rng(seed).standard_normal(dims).astype(dtype)
    return np.zeros(dims, dtype=dtype)   # token ids / pixels / flags: a valid, deterministic value


def default_input(onnx_path: Path, *, seed: int = 0) -> Any:
    """Deterministic random sample matching the model's first input shape.
    Dynamic dims collapse to 1 (intent: a dynamic BATCH dim; a symbolic H/W
    would also become 1). Wrapped in MeasureError so a bad model fails clean."""
    import numpy as np
    import onnxruntime as ort
    try:
        sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
        first = sess.get_inputs()[0]
        return _sample(first.shape, first.type, seed)
    except Exception as exc:
        raise MeasureError(f"could not load model {Path(onnx_path).name}: {exc}") from exc


def run_host(onnx_path: Path, input_array: Any, *, runs: int = 20) -> RunResult:
    import numpy as np
    import onnxruntime as ort
    try:
        sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
        inputs = sess.get_inputs()
        # The sample feeds the FIRST input in that input's own dtype; any further
        # inputs get a deterministic default sample (tan-cli#1486).
        feed = {inputs[0].name: np.asarray(input_array).astype(_np_dtype(inputs[0].type))}
        for extra in inputs[1:]:
            feed[extra.name] = _sample(extra.shape, extra.type, 0)
        out = sess.run(None, feed)[0]          # warm-up + a real output
        times = []
        for _ in range(max(1, runs)):
            t0 = perf_counter()
            sess.run(None, feed)
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
