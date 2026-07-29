# SPDX-License-Identifier: Apache-2.0
"""The packaged artifact must satisfy the extension's own probe: a single file
whose ``--version`` first line matches /^tan \\d+\\.\\d+\\.\\d+/, answering inside
the extension's 3 s budget (alp-sdk-vscode/src/alpCli/vscodeAdapter.ts:288-290).

Skips when ``dist/tan[.exe]`` is absent so the normal suite is unaffected; run
``scripts/build_binary.sh`` to produce it.
"""
import json
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

# Keep in step with MAX_ARTIFACT_BYTES in scripts/build_binary.sh.
MAX_ARTIFACT_BYTES = 15_000_000


def test_artifact_is_a_single_file():
    # --onedir would hand the extension a directory it has no unpack step for
    # (alp-sdk-vscode/src/alpCli/download.ts:159-162 writes the body to ONE path).
    assert BINARY.is_file(), "must be --onefile: the extension cannot unpack a directory"


def test_artifact_was_built_from_a_clean_interpreter():
    # The 3 s probe below does NOT catch a dirty build: an artifact built off an
    # interpreter carrying numpy/Pillow/pywin32 measured 34349423 B and ~1.00 s
    # -- 3x the size and 2x the startup, still comfortably green. Size is the
    # only signal separating the two. Clean build is 10237542 B; the ceiling
    # sits clear of both.
    size = BINARY.stat().st_size
    assert size < MAX_ARTIFACT_BYTES, (
        f"{BINARY} is {size} B -- likely built from a dirty interpreter that "
        f"pulled in modules tan never imports; see scripts/build_binary.sh"
    )


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


def test_the_artifact_carries_its_scaffold_templates(tmp_path):
    """`tan init`'s vendored scaffold trees are DATA, so PyInstaller's static
    import graph does not reach them -- they ship only because
    `scripts/build_binary.sh` passes `--add-data`. Nothing in the source-run
    suite can notice that flag going missing, and the symptom in the field is a
    customer's FIRST command failing.

    `zephyr-app` deliberately: it is the non-interactive default AND vendored, so
    this is the literal `tan init` path. `--preview` keeps it read-only.
    """
    out = subprocess.run(
        [str(BINARY), "init", "--template", "zephyr-app", "--preview", "--format", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(tmp_path),
    )
    envelope = json.loads(out.stdout)
    codes = [issue["code"] for issue in envelope.get("issues", [])]

    # `dist/` is gitignored build output, so it can easily be an artefact built
    # before this command existed. That is a STALE BINARY, not a packaging
    # regression, and it has its own signature (Click rejects the unknown
    # subcommand). The failure this test is for looks nothing like it:
    # `init.template-unreadable`, exit 5, from a build that dropped --add-data.
    if out.returncode == 2 and "cli.parse-error" in codes:
        pytest.skip("dist/ predates `tan init` -- rerun scripts/build_binary.sh")

    assert out.returncode == 0, envelope.get("issues")
    assert len(envelope["data"]["fileChanges"]) >= 6
    assert list(tmp_path.iterdir()) == [], "--preview must not touch disk"
