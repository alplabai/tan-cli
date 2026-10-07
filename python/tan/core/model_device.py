# SPDX-License-Identifier: Apache-2.0
"""On-device tier of `tan model run`/`ab` (tan-cli#1287): parse the console a
benchmark app printed on the target and turn it into a `RunResult` row.

Ported from alp-sdk `integration/model-edge-ai` (`scripts/alp_model/ondevice.py`,
alp-sdk#933), PURE half only: the console protocol is the one alp-sdk's
`examples/aen/aen-inference-energy` prints (`ENERGY-CFG` / `ENERGY-S` /
`ENERGY-W` / `ENERGY-RESULT`; that app's `printk` calls are the source of
truth). The reference's labgrid/`build.sh`/`ram-run.sh` driver is NOT ported --
tan's own flash and monitor paths own deployment and capture.

Honest by construction: nothing is invented. `latencyMs` is the active window's
measured span divided by the inferences it completed; `peakSramKib` stays
`None` (the app does not report it); `powerMj` is filled only when the capture
carries a full active+idle sample stream, and then as a labelled board-level
carrier-rail delta (`EnergyMeasurement`), never NPU/silicon energy. A malformed,
partial or truncated capture raises `DeviceCaptureError` instead of returning a
plausible-looking number.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from statistics import mean, median, stdev
from typing import Any

from tan.model.measure import EnergyMeasurement, RunResult, windowed_delta

#: +-5% agreement band between the DT `cycles_per_s` constant and the
#: ENERGY-W-measured rate (reference value, unchanged).
_CYCLES_PER_S_TOLERANCE = 0.05


class DeviceCaptureError(Exception):
    """The console capture is missing pieces, malformed or from an older app."""


@dataclass(frozen=True)
class ParsedCapture:
    """A tolerantly-parsed on-target console capture. `samples` and
    `uptime_spans` are keyed [window_index][phase] ("active"/"idle").
    `werr`/`warn` are the raw ENERGY-WERR/ENERGY-WARN lines seen, in capture
    order -- surfaced verbatim in capture_diagnostics() so a degraded run
    (I2C errors, a timed-out window, a too-long span) stays visible instead
    of silently vanishing into a clean-looking energy figure."""
    cfg: dict[str, Any]
    samples: dict[int, dict[str, list[tuple[int, int]]]]
    uptime_spans: dict[int, dict[str, tuple[int, int, float, int]]]  # (n, span_cycles, span_ms, inferences)
    device_result: dict[str, Any] | None
    werr: list[str]
    warn: list[str]


def parse_console(text: str) -> ParsedCapture:
    """Tolerant of interleaved noise (Zephyr banner, other printk lines,
    ENERGY-SCAN) -- only the recognised `ENERGY-*` prefixes are acted on.
    Raises DeviceCaptureError on a missing/malformed header, zero windows, any
    window/phase with fewer than 2 samples (no interval to integrate), an
    ENERGY-W line from an older 5-field firmware image (no per-window
    inference count), or an ENERGY-WPART line (the emitted sample stream is
    shorter than what the device integrated, so host re-integration of that
    window would be invalid)."""
    cfg: dict[str, Any] | None = None
    samples: dict[int, dict[str, list[tuple[int, int]]]] = {}
    uptime_spans: dict[int, dict[str, tuple[int, int, float, int]]] = {}
    device_result: dict[str, Any] | None = None
    werr: list[str] = []
    warn: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("ENERGY-CFG "):
            try:
                cfg = json.loads(line[len("ENERGY-CFG "):])
            except json.JSONDecodeError as exc:
                raise DeviceCaptureError(f"malformed ENERGY-CFG line: {line!r}") from exc
        elif line.startswith("ENERGY-S "):
            parts = line.split()
            if len(parts) != 5 or parts[2] not in ("active", "idle"):
                raise DeviceCaptureError(f"malformed ENERGY-S line: {line!r}")
            try:
                w_i, cycles, power_raw = int(parts[1]), int(parts[3]), int(parts[4])
            except ValueError as exc:
                raise DeviceCaptureError(f"malformed ENERGY-S line: {line!r}") from exc
            samples.setdefault(w_i, {}).setdefault(parts[2], []).append((cycles, power_raw))
        elif line.startswith("ENERGY-W "):
            parts = line.split()
            if len(parts) == 6 and parts[2] in ("active", "idle"):
                raise DeviceCaptureError(
                    f"ENERGY-W line has 5 fields, no per-window inference count: {line!r} -- "
                    "this capture is from an older firmware image that predates per-window "
                    "ENERGY-W inference reporting; re-flash the current "
                    "aen-inference-energy build")
            if len(parts) != 7 or parts[2] not in ("active", "idle"):
                raise DeviceCaptureError(f"malformed ENERGY-W line: {line!r}")
            try:
                w_i = int(parts[1])
                span = (int(parts[3]), int(parts[4]), float(parts[5]), int(parts[6]))
            except ValueError as exc:
                raise DeviceCaptureError(f"malformed ENERGY-W line: {line!r}") from exc
            uptime_spans.setdefault(w_i, {})[parts[2]] = span
        elif line.startswith("ENERGY-WPART "):
            parts = line.split()
            if len(parts) < 3 or parts[2] not in ("active", "idle"):
                raise DeviceCaptureError(f"malformed ENERGY-WPART line: {line!r}")
            raise DeviceCaptureError(
                f"window {parts[1]} phase {parts[2]!r} emitted a partial sample stream "
                f"({line!r}) -- the device integral is authoritative for this build and "
                "`--device` cannot re-derive energy from a partial window")
        elif line.startswith("ENERGY-WERR "):
            werr.append(line)
        elif line.startswith("ENERGY-WARN "):
            warn.append(line)
        elif line.startswith("ENERGY-RESULT "):
            try:
                device_result = json.loads(line[len("ENERGY-RESULT "):])
            except json.JSONDecodeError as exc:
                raise DeviceCaptureError(f"malformed ENERGY-RESULT line: {line!r}") from exc
        # else: not a recognised prefix -- banner/printk noise (incl. ENERGY-SCAN), ignored.

    if cfg is None:
        raise DeviceCaptureError("no ENERGY-CFG header line found in capture")
    if not samples:
        raise DeviceCaptureError("no ENERGY-S sample lines found in capture (zero windows)")
    for w_i, phases in samples.items():
        for phase, points in phases.items():
            if len(points) < 2:
                raise DeviceCaptureError(
                    f"window {w_i} phase {phase!r} has {len(points)} sample(s); need >= 2")

    return ParsedCapture(cfg=cfg, samples=samples, uptime_spans=uptime_spans,
                         device_result=device_result, werr=werr, warn=warn)


# ---------------------------------------------------------------------------
# Capture -> EnergyMeasurement (still pure: math only, delegates to measure.py)
# ---------------------------------------------------------------------------

def _samples_to_power(points: list[tuple[int, int]], cycles_per_s: float,
                       power_lsb_w: float) -> list[tuple[float, float]]:
    """(cycles, power_raw) -> (t_seconds, power_watts). t is the unsigned
    delta from the window's FIRST sample's cycle count, mod 2**32 -- the
    DWT/k_cycle counter is a uint32 that may wrap once within a window, and
    an unsigned mod-2**32 difference is correct across exactly one such
    wrap. power_watts is a straight INA236 Power-register count * its LSB
    (hardware-computed V*I; no software multiply here, per measure.py)."""
    if not points:
        return []
    t0_cycles = points[0][0]
    return [(((cycles - t0_cycles) % (1 << 32)) / cycles_per_s, power_raw * power_lsb_w)
            for cycles, power_raw in points]


def _measured_cycles_per_s(parsed: ParsedCapture) -> float | None:
    """Median of span_cycles/span_ms*1000 across every ENERGY-W span (every
    window, both phases) -- the kernel-uptime cross-check against the DT
    cycles_per_s constant. None when there are no ENERGY-W lines at all."""
    rates = [span_cycles / span_ms * 1000.0
             for phases in parsed.uptime_spans.values()
             for (_n, span_cycles, span_ms, _inf) in phases.values() if span_ms > 0]
    return median(rates) if rates else None


def _effective_cycles_per_s(parsed: ParsedCapture) -> float:
    """The measured rate when it exists and every ENERGY-W span agrees with
    it within `_CYCLES_PER_S_TOLERANCE`; otherwise the DT constant."""
    dt = float(parsed.cfg["cycles_per_s"])
    measured = _measured_cycles_per_s(parsed)
    if measured is None:
        return dt
    rates = [span_cycles / span_ms * 1000.0
             for phases in parsed.uptime_spans.values()
             for (_n, span_cycles, span_ms, _inf) in phases.values() if span_ms > 0]
    if (max(rates) - min(rates)) / measured > _CYCLES_PER_S_TOLERANCE:
        return dt
    return measured


def measurement_from_capture(parsed: ParsedCapture) -> EnergyMeasurement:
    """Per (active, idle) window pair: convert samples to (t, watts), call
    the PURE `windowed_delta()` once, dividing by THAT WINDOW's own active
    inference count (ENERGY-W's 6th field -- inference count is measured
    per window, not a single config-wide constant; there is no
    `cfg["n_inferences"]`, ENERGY-CFG never carries one). `value_mj_per_inference`
    is the mean across windows, `spread_mj` the sample stdev (None for a
    single window). `window_ms` is the mean active-window duration;
    `sample_count` totals every sample (both phases) across every window
    used; `EnergyMeasurement.n_inferences` is the sum of every window's
    active-phase inference count actually used."""
    cfg = parsed.cfg
    cycles_per_s = _effective_cycles_per_s(parsed)
    power_lsb_w = float(cfg["power_lsb_w"])

    per_window_mj: list[float] = []
    window_ms_list: list[float] = []
    total_samples = 0
    total_inferences = 0
    for w_i in sorted(parsed.samples.keys()):
        phases = parsed.samples[w_i]
        if "active" not in phases or "idle" not in phases:
            raise DeviceCaptureError(f"window {w_i} is missing an active or idle phase")
        active_span = parsed.uptime_spans.get(w_i, {}).get("active")
        if active_span is None:
            raise DeviceCaptureError(
                f"window {w_i} has no ENERGY-W line for phase 'active' -- cannot recover "
                "its per-inference divisor")
        n_inferences = active_span[3]
        if n_inferences < 1:
            raise DeviceCaptureError(
                f"window {w_i} active phase completed 0 inferences (ENERGY-W reports "
                f"inferences={n_inferences}) -- no valid mJ/inference divisor for this pair")
        active = _samples_to_power(phases["active"], cycles_per_s, power_lsb_w)
        idle = _samples_to_power(phases["idle"], cycles_per_s, power_lsb_w)
        per_window_mj.append(windowed_delta(active, idle, n_inferences))
        window_ms_list.append((active[-1][0] - active[0][0]) * 1000.0)
        total_samples += len(active) + len(idle)
        total_inferences += n_inferences

    value = per_window_mj[0] if len(per_window_mj) == 1 else mean(per_window_mj)
    spread = None if len(per_window_mj) < 2 else stdev(per_window_mj)
    return EnergyMeasurement(
        value_mj_per_inference=value,
        rails=[cfg["rail"]],
        n_inferences=total_inferences,
        window_ms=mean(window_ms_list),
        sample_count=total_samples,
        spread_mj=spread,
    )


def capture_diagnostics(parsed: ParsedCapture) -> dict:
    """Everything `EnergyMeasurement` (frozen, fixed field set) can't carry:
    which cycles_per_s won the DT-vs-measured reconciliation (and both raw
    values), the device's own self-reported result as a cross-check, the
    host/device ratio, and any degraded-run evidence (ENERGY-WERR/ENERGY-WARN
    lines, `npu_dispatched: false`) so a degraded capture stays visible
    rather than looking identical to a clean one."""
    dt = float(parsed.cfg["cycles_per_s"])
    measured = _measured_cycles_per_s(parsed)
    used = _effective_cycles_per_s(parsed)
    device_value = (parsed.device_result.get("value_mj_per_inference")
                    if parsed.device_result is not None else None)
    host_value = measurement_from_capture(parsed).value_mj_per_inference
    ratio = round(host_value / device_value, 4) if device_value else None
    return {
        "cycles_per_s_used": used,
        "cycles_per_s_dt": dt,
        "cycles_per_s_measured": measured,
        "device_value_mj_per_inference": device_value,
        "host_vs_device_ratio": ratio,
        "npu_dispatched": bool(parsed.cfg.get("npu_dispatched", False)),
        "windows": sorted(parsed.samples.keys()),
        "werr_lines": list(parsed.werr),
        "warn_lines": list(parsed.warn),
    }


def latency_from_capture(parsed: ParsedCapture) -> tuple[float, int, float]:
    """`(median ms per inference, total active inferences, median cycles per
    inference)` from the ACTIVE ENERGY-W spans. Raises when no active window
    completed an inference -- there is nothing to time."""
    per_ms: list[float] = []
    per_cycles: list[float] = []
    total = 0
    for phases in parsed.uptime_spans.values():
        span = phases.get("active")
        if span is None or span[3] < 1:
            continue
        _n, span_cycles, span_ms, inferences = span
        per_ms.append(span_ms / inferences)
        per_cycles.append(span_cycles / inferences)
        total += inferences
    if not per_ms:
        raise DeviceCaptureError("no active window completed an inference; nothing to time")
    return median(per_ms), total, median(per_cycles)


def run_result_from_capture(parsed: ParsedCapture) -> tuple[RunResult, EnergyMeasurement | None, dict]:
    """The `RunResult` (tier-device schema), the labelled energy measurement
    when the capture supports one, and `capture_diagnostics`."""
    latency_ms, inferences, _cycles = latency_from_capture(parsed)
    energy: EnergyMeasurement | None = None
    if all({"active", "idle"} <= set(p) for p in parsed.samples.values()):
        try:
            energy = measurement_from_capture(parsed)
        except DeviceCaptureError:
            energy = None  # a window without a usable divisor: report latency only
    backend = "ethos-u" if parsed.cfg.get("npu_dispatched") else "cpu-device"
    result = RunResult(
        backend=backend,
        latency_ms=round(latency_ms, 6),
        output_argmax=None,
        peak_sram_kib=None,
        power_mj=None if energy is None else energy.value_mj_per_inference,
        runs=inferences,
        energy=energy,
    )
    return result, energy, capture_diagnostics(parsed)
