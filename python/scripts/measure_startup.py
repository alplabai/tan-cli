#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""tan-cli#349: measure how long a FROZEN `tan` takes to run `--version`.

`--version` does no I/O, no network and no SDK resolution, so its wall time is
process startup and nothing else. Under the old PyInstaller `--onefile` build
that startup re-extracted ~14 MB of interpreter and shared libraries into a
fresh temp directory on EVERY invocation, and every subcommand paid it:

    macOS arm64   13.25 / 19.35 / 19.35 / 18.58 / 19.74 s
    Windows x64    1.19 /  1.07 /  1.08 /  1.07 /  1.05 s
    Linux   x64    0.51 /  0.43 /  0.44 /  0.37 /  0.36 s

against ~0.01 s for `git --version` on the same hosts. macOS was ~40x worse
than Linux on top of that because each extracted `.dylib` falls outside the
parent binary's ad-hoc signature and is inspected individually on load.

`--onedir` moves that extraction to install time. This script is what turns
that claim into a measurement on a runner the maintainer's laptop cannot
reach -- macOS arm64 in particular, the platform #349 was filed against and
the one platform the fix could not be verified on by hand.

It is a REGRESSION gate, not a benchmark. The ceiling is deliberately far
above a healthy onedir startup and far below the onefile numbers above: the
question it answers is "did the freeze silently go back to re-extracting per
invocation", which is a 30x signal, not a 20% one. A tight ceiling here would
buy nothing and flake on a loaded runner.

Reports the MEDIAN of N runs. A mean would let one scheduling stall on a
shared runner decide the verdict; the median needs half the runs to be slow
before it moves, which is the shape of a real regression rather than noise.
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import sys
import time
from pathlib import Path

# Healthy onedir startup measured at ~0.32-0.51 s (Linux/Windows). The onefile
# regression this guards against was 13-19 s on macOS and ~1.1 s on Windows.
# 5 s sits an order of magnitude above the former and below the worst of the
# latter, so it cannot be tripped by runner noise and cannot miss the fault.
DEFAULT_CEILING_S = 5.0
DEFAULT_RUNS = 5


def measure(tan: Path, runs: int) -> list[float]:
    timings: list[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        proc = subprocess.run(
            [str(tan), "--version"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        timings.append(time.perf_counter() - start)
        if proc.returncode != 0:
            raise SystemExit(
                f"`{tan} --version` exited {proc.returncode}, so its wall time "
                f"measures a failure path, not startup.\n"
                f"stdout: {proc.stdout.strip()!r}\nstderr: {proc.stderr.strip()!r}"
            )
        # A `--version` that printed nothing did not do the work being timed.
        if not proc.stdout.strip():
            raise SystemExit(
                f"`{tan} --version` exited 0 but printed nothing to stdout. "
                f"The timing below would measure a no-op."
            )
    return timings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tan", required=True, type=Path, help="the frozen binary")
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--ceiling", type=float, default=DEFAULT_CEILING_S)
    args = ap.parse_args()

    if not args.tan.is_file():
        raise SystemExit(f"no frozen binary at {args.tan}")

    timings = measure(args.tan, args.runs)
    median = statistics.median(timings)

    print(f"tan --version startup, {args.runs} runs on {sys.platform}:")
    for i, t in enumerate(timings, 1):
        print(f"  run {i}: {t:.3f} s")
    print(f"  median: {median:.3f} s (ceiling {args.ceiling:.1f} s)")

    if median > args.ceiling:
        print(
            f"::error::tan --version median startup {median:.3f} s exceeds the "
            f"{args.ceiling:.1f} s ceiling. The freeze is re-extracting per "
            f"invocation again -- check that build_binary.sh still passes "
            f"--onedir (tan-cli#349).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
