# SPDX-License-Identifier: Apache-2.0
"""On-device tier of `tan model run`/`ab` (tan-cli#1287): parse the console a
benchmark app printed on the target and turn it into a `RunResult` row.

Ported from alp-sdk `integration/model-edge-ai` (`scripts/alp_model/ondevice.py`,
alp-sdk#933), parser and math only, then hardened: the console protocol is the one
alp-sdk's `examples/aen/aen-inference-energy` prints (`ENERGY-CFG` / `ENERGY-S` /
`ENERGY-W` / `ENERGY-WERR` / `ENERGY-WARN` / `ENERGY-PAIR` / `ENERGY-RESULT`; that
app's `printk` calls are the source of truth). The reference's labgrid /
`build.sh` / `ram-run.sh` driver is NOT ported -- tan's own flash and monitor paths
own deployment and capture.

**Honest by construction.** A capture is untrusted text: every value is type- and
range-checked (finite, positive spans and clock, non-negative counts) and anything
else raises `DeviceCaptureError`, never a plausible number.

* `latencyMs` is the active window's measured span divided by the inferences it
  completed. The span is wall time of the app's WHOLE window loop, so it is an
  upper bound on pure inference time (the energy app also polls the monitor IC
  inside it; the latency-only app does not) -- hence the app-neutral
  `LATENCY_SCOPE`.
* `peakSramKib` stays `None` (the app does not report it).
* `RunResult.power_mj` stays `None`; energy rides in `RunResult.energy`, a
  labelled board-level carrier-rail delta (`EnergyMeasurement`), only when the
  capture holds usable active+idle sample pairs. Pairs the app itself marked
  `ENERGY-PAIR n SKIPPED` and windows that timed out are excluded from energy
  (timed-out windows from latency too) and listed as degraded evidence.
* A capture with `ENERGY-W` lines but no `ENERGY-S` stream is a valid
  latency-only capture.
* `ENERGY-S` cycle stamps are a uint32 counter: one wrap per window is handled
  by the modular delta; a counter that steps BACKWARDS is indistinguishable from
  a wrap, so non-monotonic samples are not detected (a limit of the protocol).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from statistics import mean, median, stdev
from typing import Any

from tan.model.measure import EnergyMeasurement, RunResult, windowed_delta

#: +-5% agreement band between the ENERGY-W-measured clock and the DT constant.
_CYCLES_PER_S_TOLERANCE = 0.05

#: ENERGY-CFG `timestamp_source` whose spans share `cycles_per_s`'s own clock.
_KERNEL_CLOCK = "k-cycle-get-32"

#: What `latencyMs` is, carried in every device row.
LATENCY_SCOPE = "window-span-per-inference"

_U32 = 1 << 32
_WERR_RE = re.compile(r"^ENERGY-WERR (\d+) (active|idle) timed_out=(\d)")
_WARN_WRAP_RE = re.compile(r"^ENERGY-WARN (active|idle) window (\d+) ms exceeds the cycle-counter wrap")
_PAIR_SKIP_RE = re.compile(r"^ENERGY-PAIR (\d+) SKIPPED")


class DeviceCaptureError(Exception):
    """The console capture is missing pieces, malformed or from an older app."""


@dataclass(frozen=True)
class ParsedCapture:
    """A validated console capture. `samples` / `uptime_spans` are keyed
    [window][phase] ("active"/"idle"); spans are `(n_samples, span_cycles,
    span_ms, inferences)`. `werr`/`warn`/`pair_skips` keep the raw evidence
    lines in capture order; `timed_out` is the (window, phase) set the app said
    missed its deadline, `skipped` the windows it excluded itself, `wrapped`
    the (window, phase) spans whose cycle count the app flagged as wrapped."""
    cfg: dict[str, Any]
    samples: dict[int, dict[str, list[tuple[int, int]]]]
    uptime_spans: dict[int, dict[str, tuple[int, int, float, int]]]
    device_result: dict[str, Any] | None
    werr: list[str]
    warn: list[str]
    pair_skips: list[str] = field(default_factory=list)
    timed_out: frozenset = frozenset()
    skipped: frozenset = frozenset()
    wrapped: frozenset = frozenset()


def _json_object(text: str, what: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (ValueError, RecursionError) as exc:  # JSONDecodeError, int-digit limit, deep nesting
        raise DeviceCaptureError(f"malformed {what} line") from exc
    if not isinstance(value, dict):
        raise DeviceCaptureError(f"{what} must be a JSON object, got {type(value).__name__}")
    return value


def _number(value: Any, what: str, *, positive: bool = True) -> float:
    """`value` as a finite float > 0 (or >= 0), else `DeviceCaptureError`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DeviceCaptureError(f"{what} must be a number, got {value!r}")
    try:
        number = float(value)
    except OverflowError as exc:
        raise DeviceCaptureError(f"{what} is out of range") from exc
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        raise DeviceCaptureError(f"{what} must be finite and {'> 0' if positive else '>= 0'}, got {value!r}")
    return number


def _uint(token: str, what: str, line: str, *, limit: int | None = None) -> int:
    try:
        value = int(token)
    except ValueError as exc:
        raise DeviceCaptureError(f"malformed {what} in {line!r}") from exc
    if value < 0 or (limit is not None and value >= limit):
        raise DeviceCaptureError(f"{what} out of range in {line!r}")
    return value


def _parse_w(parts: list[str], line: str) -> tuple[int, str, tuple[int, int, float, int]]:
    if len(parts) == 6 and parts[2] in ("active", "idle"):
        raise DeviceCaptureError(
            f"ENERGY-W line has 5 fields, no per-window inference count: {line!r} -- "
            "this capture is from an older firmware image that predates per-window "
            "ENERGY-W inference reporting; re-flash the current aen-inference-energy build")
    if len(parts) != 7 or parts[2] not in ("active", "idle"):
        raise DeviceCaptureError(f"malformed ENERGY-W line: {line!r}")
    window = _uint(parts[1], "window", line)
    n_samples = _uint(parts[3], "sample count", line)
    span_cycles = _uint(parts[4], "span_cycles", line, limit=_U32)
    try:
        span_ms = float(parts[5])
    except ValueError as exc:
        raise DeviceCaptureError(f"malformed span_ms in {line!r}") from exc
    if not math.isfinite(span_ms) or span_ms <= 0 or span_cycles <= 0:
        raise DeviceCaptureError(f"span must be finite and > 0 in {line!r}")
    return window, parts[2], (n_samples, span_cycles, span_ms, _uint(parts[6], "inferences", line))


def parse_console(text: str) -> ParsedCapture:
    """Tolerant of interleaved noise (Zephyr banner, other printk, ENERGY-SCAN);
    strict about every recognised line. Raises `DeviceCaptureError` on a
    missing/malformed/invalid header, a malformed or out-of-range line, a
    duplicate ENERGY-W, a window/phase with fewer than 2 samples, an
    old-firmware ENERGY-W, an ENERGY-WPART partial window, or no ENERGY-W at all."""
    cfg: dict[str, Any] | None = None
    samples: dict[int, dict[str, list[tuple[int, int]]]] = {}
    spans: dict[int, dict[str, tuple[int, int, float, int]]] = {}
    device_result: dict[str, Any] | None = None
    werr: list[str] = []
    warn: list[str] = []
    pair_skips: list[str] = []
    timed_out: set[tuple[int, str]] = set()
    skipped: set[int] = set()
    wrapped: set[tuple[int, str]] = set()

    for raw in text.splitlines():
        line = raw.strip()
        parts = line.split()
        if line.startswith("ENERGY-CFG "):
            cfg = _json_object(line[len("ENERGY-CFG "):], "ENERGY-CFG")
        elif line.startswith("ENERGY-S "):
            if len(parts) != 5 or parts[2] not in ("active", "idle"):
                raise DeviceCaptureError(f"malformed ENERGY-S line: {line!r}")
            window = _uint(parts[1], "window", line)
            cycles = _uint(parts[3], "cycle stamp", line, limit=_U32)
            power = _uint(parts[4], "power count", line, limit=_U32)
            samples.setdefault(window, {}).setdefault(parts[2], []).append((cycles, power))
        elif line.startswith("ENERGY-W "):
            window, phase, span = _parse_w(parts, line)
            if phase in spans.get(window, {}):
                raise DeviceCaptureError(f"duplicate ENERGY-W for window {window} {phase}: {line!r}")
            spans.setdefault(window, {})[phase] = span
        elif line.startswith("ENERGY-WPART "):
            raise DeviceCaptureError(
                f"a window emitted a partial sample stream ({line!r}) -- the device integral "
                "is authoritative for this build and `--device` cannot re-derive energy from "
                "a partial window")
        elif line.startswith("ENERGY-WERR "):
            werr.append(line)
            m = _WERR_RE.match(line)
            if m and m.group(3) == "1":
                timed_out.add((int(m.group(1)), m.group(2)))
        elif line.startswith("ENERGY-WARN "):
            warn.append(line)
            m = _WARN_WRAP_RE.match(line)
            if m:
                wrapped.add((int(m.group(2)), m.group(1)))
        elif line.startswith("LATENCY-WARN "):
            warn.append(line)
        elif line.startswith("RESULT ") and " WARN: " in line:
            warn.append("WARN: " + line.split(" WARN: ", 1)[1])
        elif line.startswith("ENERGY-PAIR "):
            m = _PAIR_SKIP_RE.match(line)
            if m:
                pair_skips.append(line)
                skipped.add(int(m.group(1)))
        elif line.startswith("ENERGY-RESULT "):
            device_result = _json_object(line[len("ENERGY-RESULT "):], "ENERGY-RESULT")
        # else: banner / printk noise (incl. ENERGY-SCAN) -- ignored.

    if cfg is None:
        raise DeviceCaptureError("no ENERGY-CFG header line found in capture")
    _number(cfg.get("cycles_per_s"), "ENERGY-CFG cycles_per_s")
    if "power_lsb_w" in cfg:
        _number(cfg["power_lsb_w"], "ENERGY-CFG power_lsb_w")
    if "rail" in cfg and not isinstance(cfg["rail"], str):
        raise DeviceCaptureError("ENERGY-CFG rail must be a string")
    if not spans:
        raise DeviceCaptureError("no ENERGY-W lines found in capture")
    for window, phases in samples.items():
        for phase, points in phases.items():
            if len(points) < 2:
                raise DeviceCaptureError(f"window {window} phase {phase!r} has {len(points)} sample(s); need >= 2")
    return ParsedCapture(cfg, samples, spans, device_result, werr, warn, pair_skips,
                         frozenset(timed_out), frozenset(skipped), frozenset(wrapped))


def _samples_to_power(points: list[tuple[int, int]], cycles_per_s: float,
                      power_lsb_w: float) -> list[tuple[float, float]]:
    """(cycles, power_raw) -> (t_seconds, power_watts); t is the unsigned delta
    from the window's first stamp, mod 2**32 (one wrap per window is correct; see
    the module docstring for the backwards-counter limit)."""
    if not points:
        return []
    t0 = points[0][0]
    return [(((cycles - t0) % _U32) / cycles_per_s, power * power_lsb_w) for cycles, power in points]


def _span_rates(parsed: ParsedCapture) -> list[float]:
    """Cycles per second implied by every ENERGY-W span, unwrapped and not timed
    out (a window the app reported ENERGY-WERR timed_out=1 for ends early, so its
    span says nothing about the clock; latency already excludes it)."""
    return [
        span[1] / span[2] * 1000.0
        for window, phases in parsed.uptime_spans.items()
        for phase, span in phases.items()
        if (window, phase) not in parsed.wrapped and (window, phase) not in parsed.timed_out
    ]


def _cycles_per_s(parsed: ParsedCapture) -> tuple[float, float | None]:
    """`(used, measured)`: the median span-implied clock when every span agrees
    with the others within `_CYCLES_PER_S_TOLERANCE`, else the DT constant."""
    dt = float(parsed.cfg["cycles_per_s"])
    rates = _span_rates(parsed)
    if not rates:
        return dt, None
    measured = median(rates)
    if parsed.cfg.get("timestamp_source") == _KERNEL_CLOCK:
        # Spans and `cycles_per_s` come from the SAME kernel clock, and the span's
        # ms field is an integer tick, so span_cycles / span_ms is a rounding
        # artefact, not a second opinion on the clock: use the constant, and
        # report the ratio informationally only.
        return dt, measured
    if (max(rates) - min(rates)) / measured > _CYCLES_PER_S_TOLERANCE:
        return dt, measured
    return measured, measured


def _energy_windows(parsed: ParsedCapture) -> list[int]:
    return [
        w for w in sorted(parsed.samples)
        if {"active", "idle"} <= set(parsed.samples[w])
        and w not in parsed.skipped
        and (w, "active") not in parsed.timed_out
        and (w, "idle") not in parsed.timed_out
    ]


def measurement_from_capture(parsed: ParsedCapture) -> EnergyMeasurement:
    """Per usable (active, idle) pair: `windowed_delta` over that pair's own
    active inference count (ENERGY-W's last field). Excludes pairs the app
    marked SKIPPED and windows that timed out. `value` is the mean across pairs,
    `spread_mj` the sample stdev (None for one pair)."""
    cfg = parsed.cfg
    if not isinstance(cfg.get("rail"), str) or not cfg["rail"]:
        raise DeviceCaptureError("ENERGY-CFG rail must be a non-empty string to derive energy")
    power_lsb_w = _number(cfg.get("power_lsb_w"), "ENERGY-CFG power_lsb_w")
    cycles_per_s, _ = _cycles_per_s(parsed)
    windows = _energy_windows(parsed)
    if not windows:
        raise DeviceCaptureError("no usable (active, idle) sample pair (skipped, timed-out or incomplete)")
    per_window, window_ms, total_samples, total_inferences = [], [], 0, 0
    for w in windows:
        active_span = parsed.uptime_spans.get(w, {}).get("active")
        if active_span is None:
            raise DeviceCaptureError(f"window {w} has no ENERGY-W line for phase 'active'")
        n_inferences = active_span[3]
        if n_inferences < 1:
            raise DeviceCaptureError(f"window {w} active phase completed 0 inferences; no mJ/inference divisor")
        active = _samples_to_power(parsed.samples[w]["active"], cycles_per_s, power_lsb_w)
        idle = _samples_to_power(parsed.samples[w]["idle"], cycles_per_s, power_lsb_w)
        per_window.append(windowed_delta(active, idle, n_inferences))
        window_ms.append((active[-1][0] - active[0][0]) * 1000.0)
        total_samples += len(active) + len(idle)
        total_inferences += n_inferences
    return EnergyMeasurement(
        value_mj_per_inference=per_window[0] if len(per_window) == 1 else mean(per_window),
        rails=[cfg["rail"]],
        n_inferences=total_inferences,
        window_ms=mean(window_ms),
        sample_count=total_samples,
        spread_mj=None if len(per_window) < 2 else stdev(per_window),
    )


def capture_diagnostics(parsed: ParsedCapture, energy: EnergyMeasurement | None) -> dict:
    """Everything `EnergyMeasurement` can't carry (camelCase, envelope-ready):
    the clock reconciliation, the device's own result as a cross-check against
    the ALREADY-computed `energy` (never recomputed here), and the degraded-run
    evidence."""
    used, measured = _cycles_per_s(parsed)
    device_value = None
    if parsed.device_result is not None:
        raw = parsed.device_result.get("value_mj_per_inference")
        device_value = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw) else None
    ratio = round(energy.value_mj_per_inference / device_value, 4) if energy and device_value else None
    return {
        "cyclesPerSUsed": used,
        "cyclesPerSDt": float(parsed.cfg["cycles_per_s"]),
        "cyclesPerSMeasured": measured,
        "deviceValueMjPerInference": device_value,
        "hostVsDeviceRatio": ratio,
        "npuDispatched": bool(parsed.cfg.get("npu_dispatched", False)),
        "windows": sorted(parsed.uptime_spans),
        "werrLines": list(parsed.werr),
        "warnLines": list(parsed.warn),
        "skippedPairs": list(parsed.pair_skips),
        "energyWindows": _energy_windows(parsed),
    }


def latency_from_capture(parsed: ParsedCapture) -> tuple[float, int, float]:
    """`(median ms per inference, total active inferences, median cycles per
    inference)` from the active ENERGY-W spans of windows that did not time out.
    Derived from `span_cycles` over the reconciled clock (the span's own
    millisecond field for a window the app flagged as counter-wrapped), rounded
    to what the clock can resolve. Raises when nothing completed an inference."""
    used, _ = _cycles_per_s(parsed)
    per_ms: list[float] = []
    per_cycles: list[float] = []
    total = 0
    for window, phases in parsed.uptime_spans.items():
        span = phases.get("active")
        if span is None or span[3] < 1 or (window, "active") in parsed.timed_out:
            continue
        _n, span_cycles, span_ms, inferences = span
        wrapped = (window, "active") in parsed.wrapped
        per_ms.append((span_ms if wrapped else span_cycles / used * 1000.0) / inferences)
        per_cycles.append(span_cycles / inferences)
        total += inferences
    if not per_ms:
        raise DeviceCaptureError("no active window completed an inference in time; nothing to time")
    resolution_ms = 1000.0 / (used * total)
    return round(median(per_ms), max(0, math.ceil(-math.log10(resolution_ms)))), total, median(per_cycles)


def run_result_from_capture(parsed: ParsedCapture) -> tuple[RunResult, EnergyMeasurement | None, dict]:
    """The `RunResult` (tier-device schema), the labelled energy measurement when
    the capture supports one (else `None`, with the reason in
    `diagnostics["energyNote"]` when samples existed), and the diagnostics.
    Energy is computed exactly once."""
    latency_ms, inferences, _ = latency_from_capture(parsed)
    energy: EnergyMeasurement | None = None
    note: str | None = None
    if parsed.samples:
        try:
            energy = measurement_from_capture(parsed)
        except (DeviceCaptureError, ArithmeticError) as err:
            note = f"energy not derived: {err}"
    diagnostics = capture_diagnostics(parsed, energy)
    diagnostics["energyNote"] = note
    backend = "ethos-u" if parsed.cfg.get("npu_dispatched") else "cpu-device"
    result = RunResult(backend, latency_ms, None, None, None, inferences, energy)
    return result, energy, diagnostics
