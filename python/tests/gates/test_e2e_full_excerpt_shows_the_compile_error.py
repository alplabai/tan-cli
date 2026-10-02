# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1296: `scripts/e2e-full.sh`'s `excerpt()` must surface the ninja
`FAILED:` block and compiler `error:` lines even when they are far from the
end of the capture -- the last 400 bytes of a failed build are only ninja's
trailer, which left the nightly failure undiagnosable from the CI log.

The function is cut out of the script (not copied) and run under `bash`, so
the test breaks if the real one regresses."""
from __future__ import annotations

import functools
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "e2e-full.sh"
HEADER = "LINES MATCHING ^FAILED:|error:"


@functools.lru_cache(maxsize=1)
def _bash_available() -> bool:
    """Presence on PATH is not proof: on windows-latest `bash` can resolve to
    the System32 WSL launcher stub, which ignores `-c` and prints a UTF-16
    banner. Require a real bash to echo a known token (the guard
    `test_completion_command._bash_available` documents, with its cold-start
    retry)."""
    if shutil.which("bash") is None:
        return False
    for _attempt in range(2):
        try:
            proc = subprocess.run(
                ["bash", "-c", "echo tan-bash-ok"], capture_output=True, text=True, timeout=30
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        return proc.returncode == 0 and proc.stdout.strip() == "tan-bash-ok"
    return False


pytestmark = pytest.mark.skipif(not _bash_available(), reason="needs a real bash")


def _excerpt_of(tmp_path: Path, capture: str) -> str:
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    start = lines.index("excerpt() {")
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    func = "\n".join(lines[start : end + 1])
    cap = tmp_path / "capture.txt"
    cap.write_text(capture, encoding="utf-8")
    out = subprocess.run(
        ["bash", "-c", f'set -u\n{func}\nexcerpt "$1"', "bash", cap.as_posix()],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def test_a_failed_block_far_from_the_tail_is_printed(tmp_path):
    capture = (
        "[1/9] Building C object ok.c.obj\n"
        "FAILED: zephyr/drivers/ipm/ipm_arm_mhuv2.c.obj\n"
        "/opt/gcc " + "-DX " * 300 + "-c ipm_arm_mhuv2.c\n"
        "ipm_arm_mhuv2.c:42:5: error: 'struct ipm_driver_api' has no member named 'poll_out'\n"
        + "unrelated progress line\n" * 200
        + "ninja: build stopped: subcommand failed.\n"
    )

    shown = _excerpt_of(tmp_path, capture)

    assert HEADER in shown
    assert "no member named 'poll_out'" in shown
    assert "FAILED: zephyr/drivers/ipm/ipm_arm_mhuv2.c.obj" in shown
    assert "ninja: build stopped" in shown  # the existing tail is kept
    assert max(len(line) for line in shown.splitlines()) <= 400  # the gcc line is bounded


def test_matches_are_capped_at_twenty_lines(tmp_path):
    shown = _excerpt_of(tmp_path, "".join(f"f{i}.c: error: boom {i}\n" for i in range(50)))

    assert "boom 19" in shown and "boom 20" not in shown.split(HEADER)[1]


def test_a_clean_capture_gets_no_failure_header(tmp_path):
    assert HEADER not in _excerpt_of(tmp_path, "all good\n")


def test_the_traceback_branch_still_works(tmp_path):
    shown = _excerpt_of(tmp_path, "Traceback (most recent call last):\n  boom\n")
    assert "TRACEBACK PRESENT" in shown
