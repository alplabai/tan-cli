# SPDX-License-Identifier: Apache-2.0
"""tan-cli#415: `pinmux`'s table read caught `OSError` only, so a non-UTF-8
`metadata/pinmux/<family>.yaml` escaped `_resolve` as a raw `UnicodeDecodeError`
(a `ValueError`, not an `OSError`). `pinmux()`'s own generic backstop still
turned that into SOME envelope rather than a traceback, but the WRONG one:
`pinmux.internal-failure` at `error` severity / a non-zero exit, instead of the
SAME `warning`-severity `pinmux.table-not-found` at exit 0 every other
unreadable table already gets.

A separate file from `test_pinmux_command.py` on purpose -- that file is owned
by another unit working this branch concurrently (see the task brief); this
one is scoped to exactly the decode-guard regression and mirrors its fixture
shape (`_sdk_root`/`_project`, mounting `pinmux` on a throwaway `typer.Typer()`)
rather than duplicating its broader CLI-surface coverage.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from typer.testing import CliRunner

from tan.commands.pinmux_cmd import pinmux as pinmux_command

app = typer.Typer()
app.command("pinmux")(pinmux_command)

runner = CliRunner()


def _sdk_root(tmp_path: Path) -> Path:
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    pinmux_dir = sdk / "metadata" / "pinmux"
    pinmux_dir.mkdir(parents=True)
    # A stray 0xFF byte is enough to make this non-UTF-8 -- the same minimal
    # repro `test_kconfig_command.py`'s tan-cli#396 regression test uses.
    (pinmux_dir / "aen.yaml").write_bytes(b"schemaVersion: pinmux-capability-v1\n# \xff\n")
    return sdk


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    return proj


def test_a_non_utf8_pinmux_table_is_a_warning_at_exit_zero_not_an_internal_failure(
    tmp_path: Path,
) -> None:
    sdk = _sdk_root(tmp_path)
    proj = _project(tmp_path)
    result = runner.invoke(
        app,
        [
            "--project", str(proj),
            "--sdk-root", str(sdk),
            "--family", "aen",
            "--format", "json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is True
    assert envelope["issues"] == [
        {
            "code": "pinmux.table-not-found",
            "severity": "warning",
            "message": "No pinmux capability table for family 'aen' "
            "(metadata/pinmux/aen.yaml).",
        }
    ]
    assert envelope["data"]["pads"] == []
