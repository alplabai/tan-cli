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

**`run MODEL.onnx [--runs N] [--input X.npy]`** is a host reference run
(onnxruntime CPU, `backend: cpu-host`): median latency over N runs and the
output argmax. **`ab MODEL.onnx --against OTHER.onnx`** runs both with the same
input and compares latency and file size. Both are FUNCTIONAL + host-latency
references, never SoM performance -- `peakSramKib`/`powerMj` stay `null` until
the bench-gated on-device tier fills the same schema. The input defaults to a
deterministic seeded sample shaped like the model's first input.

Every refusal is a plain `Issue(...)` return (never an exception) so the codes
stay visible to the issue-code registry gate.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from tan.commands.build_output import ProjectContext
from tan.core.model_host import broken_extra_message, extra_missing_message, missing_extra_modules
from tan.core.publish import publish_exclusive
from tan.envelope import Issue, Project, SdkInfo
from tan.exit_codes import ExitCode

PREP_DATA_SCHEMA_VERSION = "1"
RUN_DATA_SCHEMA_VERSION = "1"
AB_DATA_SCHEMA_VERSION = "1"

Result = tuple[Project, SdkInfo | None, dict, list[Issue], ExitCode]


def prep_empty_data() -> dict[str, Any]:
    return {
        "schemaVersion": PREP_DATA_SCHEMA_VERSION,
        "source": None,
        "output": None,
        "calibration": None,
        "accuracy": None,
    }


def run_empty_data() -> dict[str, Any]:
    return {"schemaVersion": RUN_DATA_SCHEMA_VERSION, "model": None, "result": None}


def ab_empty_data() -> dict[str, Any]:
    return {"schemaVersion": AB_DATA_SCHEMA_VERSION, "a": None, "b": None, "comparison": None}


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
    bad_count = _bad_count("--min-samples", min_samples)
    if bad_count is not None:
        return project, sdk, data, [bad_count], ExitCode.VALIDATION_FAILURE

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
        code = ExitCode.VALIDATION_FAILURE
        if done.code in ("model.prep-failed", "model.model-extra-missing"):
            code = ExitCode.RUNTIME_FAILURE
        return refuse(done, code)
    info, quantized, report = done
    data["calibration"] = _calibration_row(info)
    data["output"] = quantized.as_posix()
    data["accuracy"] = _accuracy_row(report)
    issues: list[Issue] = []
    if report.verdict != "good":
        issues.append(Issue("model.prep-accuracy-degraded", "warning", report.guidance or "INT8 accuracy dropped."))
    return project, sdk, data, issues, ExitCode.SUCCESS


def _bad_count(flag: str, value: int) -> Issue | None:
    if value >= 1:
        return None
    return Issue("model.unexpected-argument", "error", f"{flag} must be at least 1 (got {value}).")


def _under(context: ProjectContext, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else Path(context.workspace_root) / path


def _missing_ancestors(path: Path) -> list[Path]:
    """`path` and its parents that do not exist yet, outermost first -- the
    directories a `mkdir(parents=True)` would create."""
    missing = []
    for p in (path, *path.parents):
        if p.exists():
            break
        missing.append(p)
    return missing[::-1]


def _quantize(
    path: Path, cal_dir: Path, out_dir: Path, per_channel: bool, min_samples: int
) -> tuple[Any, Path, Any] | Issue:
    """Validate calibration, quantize, measure -- or the refusing `Issue`.
    Everything is built in a private staging directory inside `out_dir` and
    published (never overwriting) only after the accuracy run succeeds; a
    failure removes the staging directory and every directory this run created."""
    final = out_dir / f"{path.stem}.int8.onnx"
    if os.path.lexists(final):
        return Issue("model.prep-output-exists", "error", f"{final} already exists; remove it or use --out.")
    created = _missing_ancestors(out_dir)
    staging: Path | None = None
    done = False
    try:
        from tan.model.prep import PrepError, accuracy_delta, model_input, quantize, validate_calibration  # noqa: PLC0415

        try:
            model_input(path)
        except PrepError as err:
            return Issue("model.prep-failed", "error", str(err))
        try:
            info = validate_calibration(cal_dir, path, min_samples=min_samples)
        except PrepError as err:
            return Issue("model.prep-calibration-invalid", "error", str(err))
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(dir=out_dir, prefix=".tan-prep-"))
            staged = quantize(path, staging / "q.onnx", cal_dir, per_channel=per_channel)
            report = accuracy_delta(path, staged, cal_dir)
        except (PrepError, OSError) as err:
            return Issue("model.prep-failed", "error", str(err))
        try:
            publish_exclusive(staged, final)
        except FileExistsError:
            return Issue("model.prep-output-exists", "error", f"{final} already exists; nothing was overwritten.")
        except OSError as err:
            return Issue("model.prep-failed", "error", f"could not write {final}: {err}")
        done = True
        return info, final, report
    except ImportError as err:
        return Issue("model.model-extra-missing", "error", broken_extra_message("prep", str(err)))
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if not done:
            for directory in reversed(created):
                try:
                    directory.rmdir()
                except OSError:
                    break


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


def _result_row(result: Any, path: Path) -> dict[str, Any]:
    return {
        "model": path.as_posix(),
        "backend": result.backend,
        "tier": "host",
        "latencyMs": result.latency_ms,
        "outputArgmax": result.output_argmax,
        "peakSramKib": result.peak_sram_kib,
        "powerMj": result.power_mj,
        "runs": result.runs,
        "sizeBytes": path.stat().st_size,
    }


def _measure(path: Path, input_path: Path | None, runs: int, sample: Any) -> tuple[Any, Any] | Issue:
    """`(RunResult, input sample)` for `path`, or the `model.run-failed` refusal.
    `sample` is reused across `ab`'s two models so both see the same input."""
    from tan.model.measure import MeasureError, default_input, run_host  # noqa: PLC0415

    try:
        if sample is None:
            if input_path is not None:
                import numpy as np  # noqa: PLC0415

                sample = np.load(input_path, allow_pickle=False)
            else:
                sample = default_input(path)
        return run_host(path, sample, runs=runs), sample
    except ImportError as err:
        return Issue("model.model-extra-missing", "error", broken_extra_message("run", str(err)))
    except (MeasureError, OSError, ValueError, AttributeError, TypeError) as err:
        return Issue("model.run-failed", "error", str(err))


def _host_preflight(
    context: ProjectContext, verb: str, raws: list[str | None], data: dict
) -> tuple[list[Path], None] | tuple[None, Issue]:
    """Resolve every model path, check format and the extra; the first refusal."""
    paths: list[Path] = []
    for raw in raws:
        path = resolve_model_path(context, raw, verb)
        if isinstance(path, Issue):
            return None, path
        bad = format_refusal(path, verb)
        if bad is not None:
            return None, bad
        paths.append(path)
    missing = missing_extra_modules(verb)
    if missing:
        return None, Issue("model.model-extra-missing", "error", extra_missing_message(verb, missing))
    return paths, None


def _refusal_exit(issue: Issue) -> ExitCode:
    return ExitCode.RUNTIME_FAILURE if issue.code in ("model.model-extra-missing", "model.run-failed") else ExitCode.VALIDATION_FAILURE


def run_run(
    *, context: ProjectContext, source: str | None, runs: int, input_file: str | None
) -> Result:
    project, sdk = context.project(), context.sdk
    data = run_empty_data()
    bad_count = _bad_count("--runs", runs)
    if bad_count is not None:
        return project, sdk, data, [bad_count], ExitCode.VALIDATION_FAILURE
    paths, issue = _host_preflight(context, "run", [source], data)
    if issue is not None:
        return project, sdk, data, [issue], _refusal_exit(issue)
    assert paths is not None
    measured = _measure(paths[0], _under(context, input_file) if input_file else None, runs, None)
    if isinstance(measured, Issue):
        return project, sdk, data, [measured], _refusal_exit(measured)
    data["model"] = paths[0].as_posix()
    data["result"] = _result_row(measured[0], paths[0])
    return project, sdk, data, [], ExitCode.SUCCESS


def run_ab(
    *,
    context: ProjectContext,
    source: str | None,
    against: str | None,
    runs: int,
    input_file: str | None,
) -> Result:
    from tan.model.measure import compare  # noqa: PLC0415

    project, sdk = context.project(), context.sdk
    data = ab_empty_data()
    bad_count = _bad_count("--runs", runs)
    if bad_count is not None:
        return project, sdk, data, [bad_count], ExitCode.VALIDATION_FAILURE
    paths, issue = _host_preflight(context, "ab", [source, against], data)
    if issue is not None:
        return project, sdk, data, [issue], _refusal_exit(issue)
    assert paths is not None
    a_path, b_path = paths
    input_path = _under(context, input_file) if input_file else None
    first = _measure(a_path, input_path, runs, None)
    if isinstance(first, Issue):
        return project, sdk, data, [first], _refusal_exit(first)
    second = _measure(b_path, input_path, runs, first[1])
    if isinstance(second, Issue):
        return project, sdk, data, [second], _refusal_exit(second)
    a_res, b_res = first[0], second[0]
    cmp = compare(a_res, b_res, size_a=a_path.stat().st_size, size_b=b_path.stat().st_size)
    data["a"] = _result_row(a_res, a_path)
    data["b"] = _result_row(b_res, b_path)
    data["comparison"] = {
        "faster": cmp.faster,
        "latencyRatio": cmp.latency_ratio,
        "aLatencyMs": cmp.a_latency_ms,
        "bLatencyMs": cmp.b_latency_ms,
        "sizeDeltaBytes": cmp.size_delta_bytes,
    }
    return project, sdk, data, [], ExitCode.SUCCESS


def render_run_text(data: dict) -> list[str]:
    r = data.get("result")
    if not r:
        return []
    return [
        f"{r['model']}: {r['backend']} median {r['latencyMs']} ms over {r['runs']} runs, "
        f"output argmax {r['outputArgmax']}",
        "host reference only -- not SoM performance",
    ]


def render_ab_text(data: dict) -> list[str]:
    c = data.get("comparison")
    if not c:
        return []
    return [
        f"A {data['a']['model']}: {c['aLatencyMs']} ms, {data['a']['sizeBytes']} bytes",
        f"B {data['b']['model']}: {c['bLatencyMs']} ms, {data['b']['sizeBytes']} bytes",
        f"faster: {c['faster']}; B/A latency ratio {c['latencyRatio']}; size delta {c['sizeDeltaBytes']} bytes",
        "host reference only -- not SoM performance",
    ]
