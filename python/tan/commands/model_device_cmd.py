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
* Without `--capture`, `--device` calls `LIVE_FLOW(...)` -- a callable that
  deploys the app and returns the console text. It is `None` until the
  `tan flash --ram` / monitor-capture work (tan-cli#1313) provides one, and
  `--device` then refuses with `model.device-flow-unavailable` rather than
  pretending. `capture_via` composes any deploy/read pair with the same coded
  failures and is what the hermetic tests drive with stubs.

peakSramKib is always `null` (the app does not report it); powerMj is set only
from a full active+idle sample stream, as a labelled carrier-rail delta.
Refusals are plain `Issue(...)` returns so the registry gate sees the codes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from tan.commands.build_output import ProjectContext
from tan.commands.model_host_cmd import (
    ab_empty_data,
    resolve_model_path,
    run_empty_data,
)
from tan.core.model_device import DeviceCaptureError, parse_console, run_result_from_capture
from tan.envelope import Issue, Project, SdkInfo
from tan.exit_codes import ExitCode
from tan.model.measure import compare

Result = tuple[Project, SdkInfo | None, dict, list[Issue], ExitCode]

#: Largest console capture read from disk (a 64 KiB RAM console is the app's own
#: ceiling; this only stops a wrong file being slurped).
MAX_CAPTURE_BYTES = 8 * 1024 * 1024

#: `(context, model_label) -> console text | Issue`. Set by the flash/monitor
#: integration once it exists; `None` means "no live path in this build".
LIVE_FLOW: Callable[[ProjectContext, str | None], str | Issue] | None = None


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
    path = Path(raw)
    if not path.is_absolute():
        path = Path(context.workspace_root) / path
    try:
        if path.stat().st_size > MAX_CAPTURE_BYTES:
            return Issue("model.device-capture-invalid", "error", f"{role} {path} is larger than {MAX_CAPTURE_BYTES} bytes.")
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        return Issue("model.device-capture-missing", "error", f"Cannot read {role} {path}: {err}")


def _console_for(context: ProjectContext, capture: str | None, label: str | None, role: str) -> str | Issue:
    if capture:
        return _read_capture(context, capture, role)
    if LIVE_FLOW is None:
        return Issue(
            "model.device-flow-unavailable",
            "error",
            "`--device` needs a console capture (--capture FILE from the bench run); live "
            "deploy-and-capture (`tan flash --ram` + monitor capture, tan-cli#1313) is not "
            "available in this build.",
        )
    return LIVE_FLOW(context, label)


def _row(label: str | None, text: str) -> tuple[dict, Any] | Issue:
    """The `result` row for one console capture, or the `model.device-capture-invalid` refusal."""
    try:
        result, energy, diag = run_result_from_capture(parse_console(text))
    except (DeviceCaptureError, KeyError, ValueError) as err:
        return Issue("model.device-capture-invalid", "error", f"Unusable device capture: {err}")
    return {
        "model": label,
        "backend": result.backend,
        "tier": "device",
        "latencyMs": result.latency_ms,
        "outputArgmax": None,
        "peakSramKib": None,
        "powerMj": result.power_mj,
        "runs": result.runs,
        "sizeBytes": None,
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
    notes = [*diag["werr_lines"], *diag["warn_lines"]]
    if not diag["npu_dispatched"]:
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


def run_device_run(*, context: ProjectContext, source: str | None, capture: str | None) -> Result:
    project, sdk = context.project(), context.sdk
    data = run_empty_data()

    def refuse(issue: Issue) -> Result:
        return project, sdk, data, [issue], ExitCode.VALIDATION_FAILURE

    label = _device_label(context, source)
    if isinstance(label, Issue):
        return refuse(label)
    text = _console_for(context, capture, label, "capture")
    if isinstance(text, Issue):
        return refuse(text)
    built = _row(label, text)
    if isinstance(built, Issue):
        return refuse(built)
    row, _ = built
    data["model"] = label
    data["result"] = row
    return project, sdk, data, _degraded(row, "device run"), ExitCode.SUCCESS


def run_device_ab(
    *,
    context: ProjectContext,
    source: str | None,
    against: str | None,
    capture: str | None,
    against_capture: str | None,
) -> Result:
    project, sdk = context.project(), context.sdk
    data = ab_empty_data()
    rows: list[tuple[dict, Any]] = []
    issues: list[Issue] = []
    for role, raw, cap in (("A", source, capture), ("B", against, against_capture)):
        label = _device_label(context, raw)
        if isinstance(label, Issue):
            return project, sdk, data, [label], ExitCode.VALIDATION_FAILURE
        text = _console_for(context, cap, label, f"{role} capture")
        if isinstance(text, Issue):
            return project, sdk, data, [text], ExitCode.VALIDATION_FAILURE
        built = _row(label, text)
        if isinstance(built, Issue):
            return project, sdk, data, [built], ExitCode.VALIDATION_FAILURE
        rows.append(built)
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
