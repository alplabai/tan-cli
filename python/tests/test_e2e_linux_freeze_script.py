# SPDX-License-Identifier: Apache-2.0
"""`scripts/e2e-linux-freeze.sh` must not call an unrunnable freeze OK.

tan-cli#717: the success gate was `[ -x dist/tan/tan ]` plus a version read
inside a command substitution. `-x` passes on the PyInstaller bootloader
regardless of the app inside it, and a command substitution discards the exit
status, so a tree that dies `ModuleNotFoundError: No module named 'typer'` on
every invocation printed `freeze OK:` with an empty version and returned 0.
`scripts/e2e-container.sh` then takes that tree as its input.

The script is exercised against a FAKE checkout, not a real freeze: it is
driven by `TAN_CHECKOUT`, and everything it does before the gate is a `git
log`, a `cd python`, and `bash scripts/build_binary.sh`. Standing in a stub
build script that plants a chosen `dist/tan/tan` lets both outcomes -- runnable
and not -- be produced in milliseconds, where a real PyInstaller run takes
minutes and cannot be made to fail on demand.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "e2e-linux-freeze.sh"

#: Not run on Windows. The script under test is the LINUX freeze harness: its
#: own header says "Build the Linux --onedir freeze for the cross-platform e2e,
#: INSIDE WSL" and gives its invocation as
#: `MSYS_NO_PATHCONV=1 wsl -d Ubuntu-24.04 -- bash <this file>`. Git Bash puts a
#: `bash` on PATH on windows-latest, so a presence check alone lets these cases
#: run a script that is never invoked that way -- and they did, reddening the
#: windows shards. The ubuntu and macos legs still exercise every case, so this
#: is a platform restriction, not coverage traded away.
pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="e2e-linux-freeze.sh is invoked through WSL, not Windows' own bash",
    ),
    pytest.mark.skipif(
        shutil.which("bash") is None or shutil.which("git") is None,
        reason="needs bash and git on PATH",
    ),
]

#: What `build_binary.sh` is replaced with. Writes the onedir tree the real one
#: writes -- an executable at `dist/tan/tan` -- with a body the caller chooses.
_STUB_BUILD = """#!/usr/bin/env bash
set -eu
mkdir -p dist/tan
cp "$STUB_TAN_BODY" dist/tan/tan
chmod +x dist/tan/tan
echo "OK: dist/tan.tar.gz is 1 B (ceiling 16500000, libc=default)"
"""

#: A freeze that answers `--version`, as a real one does.
_RUNNABLE = """#!/usr/bin/env bash
echo "tan 0.0.0-stub"
"""

#: A freeze that cannot import its own entry point. Mirrors the real failure:
#: PyInstaller's bootloader runs, the app raises, exit is non-zero and every
#: line of it lands on stderr.
_BROKEN = """#!/usr/bin/env bash
echo "Traceback (most recent call last):" >&2
echo "  File \\"tan/cli.py\\", line 23, in <module>" >&2
echo "    import typer" >&2
echo "ModuleNotFoundError: No module named 'typer'" >&2
exit 1
"""

#: A freeze that exits 0 and prints nothing. `$(...)` cannot tell this apart
#: from a real version by status alone, which is why the fix also rejects an
#: empty string.
_SILENT = """#!/usr/bin/env bash
exit 0
"""


def _fake_checkout(tmp_path: Path, tan_body: str) -> Path:
    """A tree shaped like this repo, far enough for the script to reach its gate."""
    root = tmp_path / "checkout"
    (root / "python" / "scripts").mkdir(parents=True)
    (root / "python" / ".venv-build" / "bin").mkdir(parents=True)

    # The script aborts unless this is executable; it never runs it itself
    # (it hands the path to build_binary.sh as $PYTHON, which is stubbed out).
    py = root / "python" / ".venv-build" / "bin" / "python"
    py.write_text("#!/usr/bin/env bash\nexit 0\n")
    py.chmod(0o755)

    body = root / "tan-body.sh"
    body.write_text(tan_body)
    body.chmod(0o755)

    build = root / "python" / "scripts" / "build_binary.sh"
    build.write_text(_STUB_BUILD)
    build.chmod(0o755)

    # `git log --oneline -1` and `git diff --quiet` run before the gate.
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=root, check=True, env=env)
    return root


def _run(root: Path):
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=root,
        env={**os.environ, "TAN_CHECKOUT": str(root),
             "STUB_TAN_BODY": str(root / "tan-body.sh")},
        capture_output=True,
        text=True,
    )


def test_a_runnable_freeze_is_reported_ok(tmp_path):
    """The positive control: without it, a gate that refuses everything passes."""
    result = _run(_fake_checkout(tmp_path, _RUNNABLE))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "freeze OK: tan 0.0.0-stub" in result.stdout


def test_a_freeze_that_cannot_import_its_entry_point_is_refused(tmp_path):
    """The #717 regression: this exited 0 printing `freeze OK:` with no version."""
    result = _run(_fake_checkout(tmp_path, _BROKEN))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "freeze OK" not in result.stdout
    assert "the freeze is not runnable" in result.stdout
    # The operator gets the cause, not just a verdict.
    assert "ModuleNotFoundError: No module named 'typer'" in result.stdout


def test_a_freeze_that_prints_no_version_is_refused(tmp_path):
    """Exit 0 with empty output is the half a status check alone cannot see."""
    result = _run(_fake_checkout(tmp_path, _SILENT))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "freeze OK" not in result.stdout
