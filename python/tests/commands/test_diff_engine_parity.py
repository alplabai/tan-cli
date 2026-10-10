# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1484: `tan diff` selects the validator engine exactly as `tan validate`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from tan.cli import app
from tan.core.board_validator_run import ValidatorRun
from tan.core import board_validator_run

runner = CliRunner()


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    sdk = tmp_path / "alp-sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (sdk / "metadata").mkdir()
    # A spawnable script that REJECTS: proves the default engine never uses it.
    (sdk / "scripts" / "validate_board_yaml.py").write_text(
        "import sys\nsys.stderr.write('FAIL board.yaml: spawned\\n')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "board.yaml").write_text("som:\n  sku: E1M-AEN701\n", encoding="utf-8")
    return sdk, proj


def _diff(proj: Path, sdk: Path):
    result = runner.invoke(
        app, ["diff", "--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    return result.exit_code, json.loads(result.stdout)


def test_default_engine_is_in_process_and_ignores_the_sdk_script(tmp_path, monkeypatch):
    monkeypatch.delenv("TAN_VALIDATE_ENGINE", raising=False)
    sdk, proj = _setup(tmp_path)

    def refuse(*a, **k):
        raise AssertionError("the default engine must not spawn")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(
        board_validator_run, "run_board_validator", lambda *_a: ValidatorRun(0, "clean\n", "")
    )
    code, env = _diff(proj, sdk)
    assert code == 0, env["issues"]


def test_default_engine_rejection_refuses_the_diff(tmp_path, monkeypatch):
    monkeypatch.delenv("TAN_VALIDATE_ENGINE", raising=False)
    sdk, proj = _setup(tmp_path)
    monkeypatch.setattr(
        board_validator_run,
        "run_board_validator",
        lambda *_a: ValidatorRun(1, "", "FAIL board.yaml: bad sku\n"),
    )
    code, env = _diff(proj, sdk)
    assert code == 2
    assert env["issues"][0]["code"] == "diff.schema-violation"


def test_subprocess_env_pins_the_spawn_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TAN_VALIDATE_ENGINE", "subprocess")
    sdk, proj = _setup(tmp_path)
    monkeypatch.setattr(
        board_validator_run,
        "run_board_validator",
        lambda *_a: (_ for _ in ()).throw(AssertionError("in-process engine used")),
    )
    code, env = _diff(proj, sdk)
    assert code == 2
    assert "spawned" in env["issues"][0]["message"]
