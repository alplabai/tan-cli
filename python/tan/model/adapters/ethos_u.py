# SPDX-License-Identifier: Apache-2.0
"""Arm Ethos-U (Vela) compiler adapter.

Wraps the `vela` CLI from `ethos-u-vela` (the `model-compile` optional
dependency). is_available() is True when `vela` is on PATH; compile() shells out
for the given accelerator-config and reads back `<stem>_vela.tflite`. The
arena/peak-SRAM footprint is parsed best-effort from vela's summary CSV (column
names drift across vela versions, so matching is tolerant; 0 when unavailable).

compile() NEVER raises on a clean vela exit, including a full CPU fallback --
vela's own exit code is 0 whether it placed every operator on the NPU or none
of them (measured: `vela float_fc.tflite --accelerator-config ethos-u85-256`
on a float32 FULLY_CONNECTED model prints "NPU operators = 0 (0.0%)" and still
exits 0). `_parse_vela_placement` reads vela's own per-run placement summary
(always printed to stdout, `ethosu.vela.stats_writer.print_performance_metrics_
common`'s "CPU/NPU operators = N (P%)" lines) so a caller never has to infer
placement from the mere absence of an exception -- see `tan.model.check`'s
`_report_from_vela_compile`, the actual consumer of `Blob.npu_op_count`/
`cpu_op_count`."""
from __future__ import annotations
import csv
import math
import re
import shutil
import subprocess
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from . import CompilerAdapter, Blob

_VELA_TIMEOUT_S = 600        # vela compiles are minutes at most; never unbounded in CI

# ethosu.vela.stats_writer.print_performance_metrics_common (always printed to
# stdout, f=sys.stdout is its own default) emits exactly one "CPU operators ="
# and one "NPU operators =" line per run, e.g. "NPU operators = 0 (0.0%)". The
# regex intentionally ignores the printed percentage -- _parse_vela_placement
# recomputes it from the two integer counts so it never inherits vela's own
# text-formatting rounding.
_PLACEMENT_RE = re.compile(r"^(CPU|NPU) operators = (\d+)", re.MULTILINE)


def _vela_version() -> str:
    try:
        return f"vela {version('ethos-u-vela')}"
    except PackageNotFoundError:
        return "vela"


def _parse_vela_summary(out_dir: Path, stem: str) -> tuple[int, int]:
    """Best-effort (arena_bytes, req_sram_kib) from vela's <stem>_summary_*.csv.

    Every `<mem_area>_memory_used` column in vela's summary CSV is already in
    KiB, not bytes (`memory_used[...] / 1024.0`,
    ethosu/vela/stats_writer.py:123 in write_summary_metrics_csv_common) --
    despite the raw CSV values looking byte-scale at a glance (e.g.
    `72.734375`). The model's actual arena requirement is its SRAM working
    set, `sram_memory_used`: the scratch region alp-sdk's `alp_model_open()`
    `arena`/`arena_bytes` sizes at runtime for the default Shared/Dedicated
    SRAM memory modes. `arena_cache_size` (`arch.arena_cache_size / 1024`,
    stats_writer.py:107) is a DIFFERENT column -- the accelerator config's
    configured cache capacity, a build-time knob unrelated to any one
    model's footprint -- and must never be read here.

    req_sram_kib is rounded UP (ceil, never floor/truncate): the device-side
    fit gate (`t->req_sram_kib <= e->arena_sram_kib`,
    src/backends/inference/alp_model_select.c) must never under-report a
    model's requirement, or a model that doesn't actually fit could pass the
    gate as if it did.

    Returns (0, 0) when the summary is missing, unparseable, or reports no
    SRAM usage (e.g. a full CPU-fallback compile)."""
    matches = sorted(out_dir.glob(f"{stem}_summary_*.csv"))
    if not matches:
        return 0, 0
    with open(matches[0], newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return 0, 0
    row = rows[0]

    def _num(pred: Callable[[str], bool]) -> float:
        for key, val in row.items():
            if key and pred(key.lower()):
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
        return 0.0

    sram_kib = _num(lambda k: "sram" in k and "used" in k)
    if sram_kib <= 0:
        return 0, 0
    return round(sram_kib * 1024), math.ceil(sram_kib)


def _parse_vela_placement(stdout: str) -> tuple[int, int] | None:
    """(cpu_op_count, npu_op_count) from vela's own "CPU/NPU operators = N
    (P%)" summary lines -- vela's REAL per-run placement verdict, not
    inferred from its exit code (0 either way). None when either line is
    absent (an unexpected vela output shape, e.g. a future version that
    changes this text): a caller must not fabricate a placement it can't
    actually read."""
    counts = {k: int(v) for k, v in _PLACEMENT_RE.findall(stdout)}
    if "CPU" not in counts or "NPU" not in counts:
        return None
    return counts["CPU"], counts["NPU"]


class VelaAdapter(CompilerAdapter):
    backend = "ethos_u"

    def is_available(self) -> bool:
        return shutil.which("vela") is not None

    def accepts(self, src_format: str) -> bool:
        return src_format == "tflite"

    def compile(self, source: Path, *, accel_config: str, out_dir: Path, opts: dict | None = None) -> Blob:
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = ["vela", str(source), "--accelerator-config", accel_config,
               "--output-dir", str(out_dir)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_VELA_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"vela timed out after {exc.timeout}s for {accel_config}") from exc
        if proc.returncode != 0:
            raise RuntimeError(f"vela failed for {accel_config}: {proc.stderr.strip()}")
        produced = out_dir / f"{source.stem}_vela.tflite"
        if not produced.is_file():
            raise RuntimeError(f"vela produced no output at {produced}")
        arena, sram_kib = _parse_vela_summary(out_dir, source.stem)
        placement = _parse_vela_placement(proc.stdout)
        cpu_ops, npu_ops = placement if placement is not None else (None, None)
        return Blob(format="vela_tflite", payload=produced.read_bytes(),
                    arena_bytes=arena, compiler_version=_vela_version(),
                    req_sram_kib=sram_kib, cpu_op_count=cpu_ops, npu_op_count=npu_ops)
