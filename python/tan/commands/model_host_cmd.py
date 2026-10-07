# SPDX-License-Identifier: Apache-2.0
"""Host-tier `tan model prep` (tan-cli#1287): runners and text renderers, kept
out of `model_cmd.py` (module-size budget); that file owns the typer surface
and `finish()`.

**`prep MODEL.onnx --calibration DIR [--out DIR] [--per-channel]`** validates
the calibration set (`.npy` samples, shape-matched to the model input), INT8
quantizes with onnxruntime QDQ static to `<out>/<stem>.int8.onnx`, and reports
the fp32-vs-int8 accuracy delta -- a silent INT8 accuracy cliff is the failure
this exists to catch. No vendor toolchain and no hardware; the vendor compile
(`tan model build`) is a separate step. ONNX input only: `.tflite` conversion
(tf2onnx/tensorflow) is deferred.

Every refusal is a plain `Issue(...)` return (never an exception) so the codes
stay visible to the issue-code registry gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tan.commands.build_output import ProjectContext
from tan.core.model_host import extra_missing_message, missing_extra_modules
from tan.envelope import Issue, Project, SdkInfo
from tan.exit_codes import ExitCode

PREP_DATA_SCHEMA_VERSION = "1"

Result = tuple[Project, SdkInfo | None, dict, list[Issue], ExitCode]


def prep_empty_data() -> dict[str, Any]:
    return {
        "schemaVersion": PREP_DATA_SCHEMA_VERSION,
        "source": None,
        "output": None,
        "calibration": None,
        "accuracy": None,
    }


def resolve_model_path(context: ProjectContext, raw: str | None, role: str) -> Path | Issue:
    """`raw` as an existing file (relative to the project root), or the
    `model.model-source-missing` refusal."""
    if not raw:
        return Issue(
            "model.model-source-missing",
            "error",
            f"`tan model {role}` needs a model file (.onnx) argument.",
        )
    path = Path(raw)
    if not path.is_absolute():
        path = Path(context.workspace_root) / path
    if not path.is_file():
        return Issue("model.model-source-missing", "error", f"Model file not found: {path}")
    return path


def format_refusal(path: Path, verb: str) -> Issue | None:
    """The `model.model-format-unsupported` refusal for a non-ONNX input."""
    if path.suffix.lower() == ".onnx":
        return None
    hint = (
        " TFLite -> ONNX conversion (tf2onnx/tensorflow) is not available yet."
        if path.suffix.lower() == ".tflite"
        else ""
    )
    return Issue(
        "model.model-format-unsupported",
        "error",
        f"`tan model {verb}` takes an .onnx model; got {path.name}.{hint}",
    )


def _calibration_row(info: Any) -> dict[str, Any]:
    return {"samples": info.samples, "inputName": info.input_name, "inputShape": list(info.input_shape)}


def _accuracy_row(rep: Any) -> dict[str, Any]:
    return {
        "samples": rep.samples,
        "top1AgreementPct": rep.top1_agreement_pct,
        "meanCosine": rep.mean_cosine,
        "maxAbsErr": rep.max_abs_err,
        "verdict": rep.verdict,
        "guidance": rep.guidance,
    }


def run_prep(
    *,
    context: ProjectContext,
    source: str | None,
    calibration: str | None,
    out: str,
    per_channel: bool,
    min_samples: int,
) -> Result:
    project, sdk = context.project(), context.sdk
    data = prep_empty_data()

    def refuse(issue: Issue, exit_code: ExitCode) -> Result:
        return project, sdk, data, [issue], exit_code

    path = resolve_model_path(context, source, "prep")
    if isinstance(path, Issue):
        return refuse(path, ExitCode.VALIDATION_FAILURE)
    bad_format = format_refusal(path, "prep")
    if bad_format is not None:
        return refuse(bad_format, ExitCode.VALIDATION_FAILURE)
    data["source"] = path.as_posix()
    missing = missing_extra_modules("prep")
    if missing:
        extra = Issue("model.model-extra-missing", "error", extra_missing_message("prep", missing))
        return refuse(extra, ExitCode.RUNTIME_FAILURE)
    if not calibration:
        need = Issue(
            "model.prep-calibration-invalid",
            "error",
            "`tan model prep` needs --calibration DIR (a directory of .npy samples).",
        )
        return refuse(need, ExitCode.VALIDATION_FAILURE)

    cal_dir = _under(context, calibration)
    out_dir = _under(context, out)
    done = _quantize(path, cal_dir, out_dir, per_channel, min_samples)
    if isinstance(done, Issue):
        code = ExitCode.RUNTIME_FAILURE if done.code == "model.prep-failed" else ExitCode.VALIDATION_FAILURE
        data["calibration"] = None
        return refuse(done, code)
    info, quantized, report = done
    data["calibration"] = _calibration_row(info)
    data["output"] = quantized.as_posix()
    data["accuracy"] = _accuracy_row(report)
    issues: list[Issue] = []
    if report.verdict != "good":
        issues.append(Issue("model.prep-accuracy-degraded", "warning", report.guidance or "INT8 accuracy dropped."))
    return project, sdk, data, issues, ExitCode.SUCCESS


def _under(context: ProjectContext, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else Path(context.workspace_root) / path


def _quantize(
    path: Path, cal_dir: Path, out_dir: Path, per_channel: bool, min_samples: int
) -> tuple[Any, Path, Any] | Issue:
    """Validate calibration, quantize, measure -- or the refusing `Issue`."""
    from tan.model.prep import PrepError, accuracy_delta, quantize, validate_calibration  # noqa: PLC0415

    try:
        info = validate_calibration(cal_dir, path, min_samples=min_samples)
    except PrepError as err:
        return Issue("model.prep-calibration-invalid", "error", str(err))
    try:
        quantized = quantize(path, out_dir / f"{path.stem}.int8.onnx", cal_dir, per_channel=per_channel)
        return info, quantized, accuracy_delta(path, quantized, cal_dir)
    except PrepError as err:
        return Issue("model.prep-failed", "error", str(err))


def render_prep_text(data: dict) -> list[str]:
    if not data.get("output"):
        return []
    cal, acc = data["calibration"], data["accuracy"]
    return [
        f"calibration: {cal['samples']} samples, input {cal['inputName']} {cal['inputShape']}",
        f"quantized {data['source']} -> {data['output']}",
        f"accuracy (fp32 vs int8): {acc['verdict']}; top-1 agreement {acc['top1AgreementPct']}%, "
        f"mean cosine {acc['meanCosine']}, max abs err {acc['maxAbsErr']}",
    ]
