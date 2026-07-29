# SPDX-License-Identifier: Apache-2.0
"""The packaged artifact must satisfy the extension's own probe: a single file
whose ``--version`` first line matches /^tan \\d+\\.\\d+\\.\\d+/, answering inside
the extension's 3 s budget (alp-sdk-vscode/src/alpCli/vscodeAdapter.ts:288-290).

Skips when ``dist/tan[.exe]`` is absent so the normal suite is unaffected; run
``scripts/build_binary.sh`` to produce it.
"""
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

BINARY = (
    Path(__file__).resolve().parents[2]
    / "dist"
    / ("tan.exe" if sys.platform == "win32" else "tan")
)
pytestmark = pytest.mark.skipif(
    not BINARY.exists(), reason="run scripts/build_binary.sh first"
)


def test_artifact_is_a_single_file():
    # --onedir would hand the extension a directory it has no unpack step for
    # (alp-sdk-vscode/src/alpCli/download.ts:159-162 writes the body to ONE path).
    assert BINARY.is_file(), "must be --onefile: the extension cannot unpack a directory"


def test_version_line_matches_the_extension_regex():
    out = subprocess.run(
        [str(BINARY), "--version"], capture_output=True, text=True, encoding="utf-8"
    )
    first = out.stdout.splitlines()[0]
    assert re.match(r"^tan \d+\.\d+\.\d+", first), f"got: {first!r}"


def test_version_probe_completes_within_the_3s_budget():
    start = time.monotonic()
    subprocess.run(
        [str(BINARY), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=3,
    )
    elapsed = time.monotonic() - start
    assert elapsed < 3.0, f"--version took {elapsed:.2f}s; extension probe timeout is 3s"
    print(f"\nstartup: {elapsed:.3f}s")
