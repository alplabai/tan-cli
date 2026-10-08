# SPDX-License-Identifier: Apache-2.0
"""`tan model run --device` / `ab --device` (tan-cli#1287, on-device tier).

The target runs a benchmark app (alp-sdk's `examples/aen/aen-inference-energy`
speaks the console protocol `tan.core.model_device` parses) and prints
per-window timing and, when the board has the monitor IC, power samples. This
module turns that console text into the same `result` envelope the host tier
fills, with `tier: device`; deployment and capture belong to tan's own flash and
monitor paths, so they enter through ONE seam, `LIVE_FLOW`:

* `--capture FILE` ingests a console capture taken by whoever ran the board
  (the bench's `ram-run.sh`, `tan monitor`, a `ram_console_buf` dump). It needs
  no hardware, no extra and no onnxruntime -- parsing is stdlib.
* Without `--capture`, `--device` calls `LIVE_FLOW(...)`, which RAM-runs the
  already-built `diagnostics.link: itcm` project through `tan flash --ram`'s own
  Flow C and returns the RAM console (`tan.commands.model_device_live`); the row
  then carries `source: live`. `capture_via` composes any deploy/read pair with
  the same coded failures.

powerMj is always `null` on this tier (energy is reported in `energy`, with its
scope label, from usable active+idle sample pairs only). peakSramKib is
ENERGY-CFG `sram_peak_bytes` / 1024 and `model` falls back to ENERGY-CFG `model`
when no model file is named (tan-cli#1404); both are `null` if the app omits them.
Refusals are plain `Issue(...)` returns so the registry gate sees the codes.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tan.commands.build_output import ProjectContext, resolve_project_context
from tan.commands.model_device_live import LiveOptions, LiveRefusal, LiveRun, live_console
from tan.commands.model_host_cmd import (
    ab_empty_data,
    resolve_model_path,
    run_empty_data,
)
from tan.core.model_device import (
    DeviceCaptureError,
    parse_console,
    run_result_from_capture,
)
from tan.envelope import Issue, Project, SdkInfo
from tan.exit_codes import ExitCode
from tan.model.measure import compare

Result = tuple[Project, SdkInfo | None, dict, list[Issue], ExitCode]

#: Largest console capture read from disk (a 64 KiB RAM console is the app's own
#: ceiling; this only stops a wrong file being slurped).
MAX_CAPTURE_BYTES = 8 * 1024 * 1024

#: Longest `consoleText` carried in a live row (the app's RAM console is 64 KiB at
#: most, so this never trims a real capture; the saved file always holds all of it).
#: Over the cap the TAIL is kept -- `LATENCY-RESULT` is printed last.
MAX_CONSOLE_TEXT_CHARS = 64 * 1024

#: `(context, model_label, options) -> console text | Issue`: RAM-run the project
#: through Flow C and return its console (`model_device_live.live_console`). A seam
#: so a test can substitute a deploy that needs no probe.
LIVE_FLOW: Callable[[ProjectContext, str | None, LiveOptions], LiveRun | LiveRefusal] = live_console


def capture_via(
    deploy: Callable[[], Issue | None], read_console: Callable[[], str | Issue]
) -> str | Issue:
    """Run `deploy()` (an `Issue` means it failed) then `read_console()`;
    the first failure is returned verbatim, so flash/monitor refusals keep their
    own codes."""
    failed = deploy()
    if failed is not None:
        return failed
    return read_console()


def _read_capture(context: ProjectContext, raw: str, role: str) -> str | Issue:
    """The capture file's text. Only a regular file is read -- never a FIFO,
    device or /proc entry -- and at most `MAX_CAPTURE_BYTES + 1` bytes of it, so
    a file that grows or lies about its size cannot be slurped."""
    path = Path(raw)
    if not path.is_absolute():
        path = Path(context.workspace_root) / path
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return Issue("model.device-capture-invalid", "error", f"{role} {path} is not a regular file.")
            data = handle.read(MAX_CAPTURE_BYTES + 1)
    except OSError as err:
        return Issue("model.device-capture-missing", "error", f"Cannot read {role} {path}: {err}")
    if len(data) > MAX_CAPTURE_BYTES:
        return Issue("model.device-capture-invalid", "error", f"{role} {path} is larger than {MAX_CAPTURE_BYTES} bytes.")
    return data.decode("utf-8", errors="replace")


def _console_for(
    context: ProjectContext, capture: str | None, label: str | None, role: str, live: LiveOptions
) -> LiveRun | LiveRefusal:
    if capture:
        text = _read_capture(context, capture, role)
        return LiveRefusal([text]) if isinstance(text, Issue) else LiveRun(text, [], None)
    return LIVE_FLOW(context, label, live)


def _row(
    label: str | None, text: str, run: LiveRun | None = None, project_path: str | None = None
) -> tuple[dict, Any] | Issue:
    """The `result` row for one console capture, or the `model.device-capture-invalid` refusal."""
    try:
        result, energy, diag = run_result_from_capture(parse_console(text))
    except (DeviceCaptureError, KeyError, ValueError, TypeError, ArithmeticError, AttributeError,
            RecursionError) as err:
        return Issue(
            "model.device-capture-invalid", "error",
            f"Unusable device capture: {err}; if the app had not finished, raise --wait.",
        )
    size = None
    if label:
        try:
            size = os.stat(label).st_size
        except OSError:
            size = None
    live = run is not None and run.flash is not None
    extra = {"source": "capture"}
    if live:
        extra = {
            "source": "live", "project": project_path, "flash": run.flash,
            "consoleText": text[-MAX_CONSOLE_TEXT_CHARS:],
            "consoleTruncated": len(text) > MAX_CONSOLE_TEXT_CHARS,
        }
    return {
        **extra,
        "model": label or diag.get("model"),
        "backend": result.backend,
        "tier": "device",
        # The app's own LATENCY-RESULT ms_per_inference when printed (scope
        # "device-latency-result"), else the median over active windows of
        # (window span / inferences completed); the latter always stays in
        # diagnostics.windowLatencyMs.
        "latencyMs": result.latency_ms,
        "latencyScope": diag["latencyScope"],
        "outputArgmax": None,
        "peakSramKib": result.peak_sram_kib,
        # Always null on the device tier: energy is reported in `energy`, with
        # its own scope label, never as a bare number.
        "powerMj": None,
        # Inferences timed across the active windows (NOT a repeat count).
        "runs": result.runs,
        "sizeBytes": size,
        "energy": None if energy is None else {
            "valueMjPerInference": energy.value_mj_per_inference,
            "spreadMj": energy.spread_mj,
            "rails": list(energy.rails),
            "nInferences": energy.n_inferences,
            "windowMs": energy.window_ms,
            "sampleCount": energy.sample_count,
            "source": energy.source,
            "scope": energy.scope,
        },
        "diagnostics": diag,
    }, result


def _degraded(row: dict, label: str) -> list[Issue]:
    diag = row["diagnostics"]
    notes = [*diag["werrLines"], *diag["warnLines"], *diag["skippedPairs"]]
    if diag.get("energyNote"):
        notes.append(diag["energyNote"])
    if not diag["npuDispatched"]:
        notes.append("the app reported npu_dispatched=false (NPU did not run the model)")
    if not notes:
        return []
    return [Issue("model.device-capture-degraded", "warning", f"{label}: " + "; ".join(notes[:5]))]


def _device_label(context: ProjectContext, source: str | None) -> str | None | Issue:
    """The model label: the file given (it must exist), or `None`."""
    if not source:
        return None
    resolved = resolve_model_path(context, source, "run")
    return resolved if isinstance(resolved, Issue) else resolved.as_posix()


def run_device_run(
    *, context: ProjectContext, source: str | None, capture: str | None, live: LiveOptions | None = None
) -> Result:
    project, sdk = context.project(), context.sdk
    data = run_empty_data()

    def refuse(issue: Issue) -> Result:
        return project, sdk, data, [issue], ExitCode.VALIDATION_FAILURE

    label = _device_label(context, source)
    if isinstance(label, Issue):
        return refuse(label)
    live = live or LiveOptions()
    run = _console_for(context, capture, label, "the run", live)
    if isinstance(run, LiveRefusal):
        if run.flash is not None:
            data["flash"] = run.flash
        return project, sdk, data, run.issues, run.exit_code
    built = _row(label, run.text, run, context.workspace_root)
    if isinstance(built, Issue):
        if run.flash is not None:
            data["flash"] = run.flash  # the board WAS loaded and reset
        return project, sdk, data, [*run.issues, built], ExitCode.VALIDATION_FAILURE
    row, _ = built
    data["model"] = row["model"]
    data["result"] = row
    return project, sdk, data, [*run.issues, *_degraded(row, "device run")], ExitCode.SUCCESS


def _note_loaded(data: dict, role: str, flash: dict | None) -> None:
    """Record, per side, that a refused `ab` run had already RAM-loaded and reset the board."""
    if flash is not None:
        data.setdefault("flash", {})[role.lower()] = flash


def run_device_ab(
    *,
    context: ProjectContext,
    source: str | None,
    against: str | None,
    capture: str | None,
    against_capture: str | None,
    live: LiveOptions | None = None,
) -> Result:
    project, sdk = context.project(), context.sdk
    data = ab_empty_data()
    rows: list[tuple[dict, Any]] = []
    issues: list[Issue] = []
    live = live or LiveOptions()
    if bool(capture) != bool(against_capture):
        return project, sdk, data, [Issue(
            "model.device-ab-mixed-sources", "error",
            "`ab --device` takes both sides live (--against-project, --confirm) or both from "
            "captures (--capture and --against-capture), not one of each; nothing was run.",
        )], ExitCode.VALIDATION_FAILURE
    context_b = context
    if not against_capture and live.against_project:
        context_b = resolve_project_context(live.against_project, None, live.sdk_root)
    if not against_capture and context_b is context:
        return project, sdk, data, [Issue(
            "model.device-live-needs-two-projects", "error",
            "`ab --device` without --against-capture runs each project in turn: pass the second "
            "project with --against-project (nothing was run).",
        )], ExitCode.VALIDATION_FAILURE
    for role, raw, cap, ctx in (
        ("A", source, capture, context), ("B", against, against_capture, context_b)
    ):
        label = _device_label(ctx, raw)
        if isinstance(label, Issue):
            return project, sdk, data, [label], ExitCode.VALIDATION_FAILURE
        run = _console_for(ctx, cap, label, f"model {role}", live)
        if isinstance(run, LiveRefusal):
            _note_loaded(data, role, run.flash)
            return project, sdk, data, [*issues, *run.issues], run.exit_code
        built = _row(label, run.text, run, ctx.workspace_root)
        if isinstance(built, Issue):
            _note_loaded(data, role, run.flash)
            return project, sdk, data, [*issues, *run.issues, built], ExitCode.VALIDATION_FAILURE
        rows.append(built)
        issues.extend(run.issues)
        issues.extend(_degraded(built[0], f"device run {role}"))
    (a_row, a_res), (b_row, b_res) = rows
    cmp = compare(a_res, b_res)
    data["a"], data["b"] = a_row, b_row
    data["comparison"] = {
        "faster": cmp.faster,
        "latencyRatio": cmp.latency_ratio,
        "aLatencyMs": cmp.a_latency_ms,
        "bLatencyMs": cmp.b_latency_ms,
        "sizeDeltaBytes": None,
    }
    return project, sdk, data, issues, ExitCode.SUCCESS
