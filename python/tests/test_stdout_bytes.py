# SPDX-License-Identifier: Apache-2.0
"""Byte-level pin on the process-boundary stdout fix in `tan.cli.main`
(`_reconfigure_stdio`) and the `ensure_ascii=False` fix in `tan.envelope`.

Every other test in this suite drives commands through `typer.testing.
CliRunner`, whose `invoke()` captures output into an in-memory
`io.BytesIO`-backed stream that Click itself constructs and never runs
through a platform `TextIOWrapper` -- it applies NO newline translation and
is always UTF-8, regardless of host OS or console code page. That harness
structurally CANNOT see a Windows-console-only defect: a real Windows
`sys.stdout` is a `TextIOWrapper` that translates a written `"\\n"` to
`"\\r\\n"` and encodes with the process's locale code page unless told
otherwise, and CliRunner's fake stream never exercises that path at all. Only
a real subprocess, read back as RAW BYTES (not `text=True`, which would
silently undo the very translation this file exists to catch), can show it.

Measured before the fix (`tan.cli.main` had no `_reconfigure_stdio`, and
`envelope.py`'s `json.dumps` had no `ensure_ascii=False`): `tan completion
--shell bash` was 3975 bytes with 108 `\\r` where the built oracle
(`target/debug/tan.exe`) was 3867 bytes with zero, and the emitted script was
a hard syntax error when sourced in a strict bash (WSL Ubuntu-22.04:
``syntax error near unexpected token `$'{\\r''``); a non-ASCII `scaffold
--name` value shipped as `\\uXXXX` escapes instead of the oracle's raw UTF-8.
Confirmed to go RED against the pre-fix source (reverting `_reconfigure_
stdio`'s call site reproduces the 108-`\\r` count and the WSL syntax error
above verbatim).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

#: `python/` -- `python -m tan` resolves the package off `os.getcwd()`, not
#: this file's own location, so a child process needs it pinned onto
#: `PYTHONPATH` (mirrors `test_cli_skeleton.py`'s `PACKAGE_ROOT`).
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _run_bytes(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run `python -m tan <argv>` in a real child process and return RAW
    bytes -- deliberately no `text=True`/`encoding=`, which would have
    `subprocess` itself perform universal-newline decoding and mask exactly
    the `\\r\\n` translation this file must observe on the wire.
    """
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True,
        cwd=cwd,
        env=env,
    )


def test_text_command_stdout_has_no_cr():
    """`tan completion --shell bash`: a plain text-mode command. Pre-fix this
    was 3975 bytes with 108 `\\r` (measured); the oracle is 3867 bytes with
    zero. A `\\r` in this output is not cosmetic -- WSL's bash refuses to
    source the script at all (`syntax error near unexpected token
    $'{\\r''`)."""
    result = _run_bytes("completion", "--shell", "bash")
    assert result.returncode == 0, result.stderr
    assert b"\r" not in result.stdout, result.stdout


def test_json_envelope_stdout_has_no_cr():
    """`tan clean --format json`: not completion-specific -- ANY `--format
    json` envelope ended `\\r\\n` pre-fix, because the defect is at the
    process-wide stdout stream, not in any one command."""
    result = _run_bytes("clean", "--format", "json")
    assert result.stdout.endswith(b"\n")
    assert not result.stdout.endswith(b"\r\n")
    assert b"\r" not in result.stdout, result.stdout


def test_bare_format_json_stdout_has_no_cr():
    """`tan --format json` alone (a Click-level usage error, routed through
    `main`'s own `_usage_error_envelope` fallback) -- the shortest possible
    repro that the fix lives at the process boundary, not inside any one
    command's own success path."""
    result = _run_bytes("--format", "json")
    assert b"\r" not in result.stdout, result.stdout


def test_nonascii_value_round_trips_as_raw_utf8_not_escaped(tmp_path):
    """`scaffold --name "Sensör Ölçüm" --format json --preview`: pre-fix,
    `envelope.py`'s bare `json.dumps` (`ensure_ascii` defaults to `True`)
    shipped `"Sens\\u00f6r \\u00d6l\\u00e7\\u00fcm"`; the oracle's
    `serde_json::to_string` writes the raw UTF-8 bytes verbatim. `--preview`
    so nothing is actually written to `tmp_path`."""
    destination = tmp_path / "sensor-driver"
    result = _run_bytes(
        "scaffold",
        "--name",
        "Sensör Ölçüm",
        "--template",
        "sensor-driver",
        "--destination",
        str(destination),
        "--format",
        "json",
        "--preview",
    )
    assert result.returncode == 0, result.stderr
    # Raw UTF-8 for "ö"/"Ö"/"ç"/"ü" on the wire, not a `\uXXXX` escape.
    assert "Sensör Ölçüm".encode("utf-8") in result.stdout, result.stdout
    assert b"\\u00f6" not in result.stdout, result.stdout
    assert b"\\u00d6" not in result.stdout, result.stdout


def test_stderr_also_has_no_cr():
    """`_reconfigure_stdio` reconfigures stderr too (the fix's own docstring
    names both streams) -- `tan build --bogus --format json` is a Click usage
    error that tees its message onto the real stderr live (`_TeeStderr`),
    which is exactly the path that would still show `\\r\\n` if only stdout
    had been fixed."""
    result = _run_bytes("build", "--bogus", "--format", "json")
    assert b"\r" not in result.stderr, result.stderr
