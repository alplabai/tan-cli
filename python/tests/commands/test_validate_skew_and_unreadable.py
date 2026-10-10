# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1484: validator skew warning, --help text, undecodable board.yaml."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from tan.cli import app
from tan.core import board_validator_run, board_validator_skew
from tan.core.board_validator_run import ValidatorRun

runner = CliRunner()


def _sdk(tmp_path: Path) -> Path:
    sdk = tmp_path / "alp-sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (sdk / "metadata").mkdir()
    return sdk


def _project(tmp_path: Path, board: bytes) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "board.yaml").write_bytes(board)
    return proj


def _validate(proj: Path, sdk: Path):
    result = runner.invoke(
        app, ["validate", "--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    return result.exit_code, json.loads(result.stdout)


def _clean(monkeypatch):
    monkeypatch.delenv("TAN_VALIDATE_ENGINE", raising=False)
    monkeypatch.setattr(
        board_validator_run, "run_board_validator", lambda *_a: ValidatorRun(0, "clean\n", "")
    )


def test_a_checkout_with_the_audited_sources_gets_no_warning(tmp_path, monkeypatch):
    _clean(monkeypatch)
    sdk = _sdk(tmp_path)
    code, env = _validate(_project(tmp_path, b"som:\n  sku: X\n"), sdk)
    assert code == 0
    assert not any(i["code"] == "validate.sdk-validator-newer" for i in env["issues"])


def test_a_newer_validator_source_warns_but_stays_clean(tmp_path, monkeypatch):
    _clean(monkeypatch)
    sdk = _sdk(tmp_path)
    (sdk / "scripts" / "validate_board_yaml.py").write_text("# newer\n", encoding="utf-8")
    code, env = _validate(_project(tmp_path, b"som:\n  sku: X\n"), sdk)
    assert code == 0 and env["ok"] is True
    (warn,) = [i for i in env["issues"] if i["code"] == "validate.sdk-validator-newer"]
    assert warn["severity"] == "warning"
    assert "scripts/validate_board_yaml.py" in warn["message"]
    assert "TAN_VALIDATE_ENGINE=subprocess" in warn["message"]
    assert env["data"]["outcome"] == "clean"


def test_skewed_sources_ignores_missing_files_and_crlf(tmp_path):
    assert board_validator_skew.skewed_sources(tmp_path) == ()


def test_help_documents_the_engine_variable():
    result = runner.invoke(app, ["validate", "--help"])
    assert "TAN_VALIDATE_ENGINE" in result.output


def test_an_undecodable_board_is_the_users_file_not_an_sdk_gap(tmp_path, monkeypatch):
    monkeypatch.delenv("TAN_VALIDATE_ENGINE", raising=False)
    sdk = _sdk(tmp_path)
    proj = _project(tmp_path, b"som:\n  sku: E1M-AEN801 # caf\xe9\ncores: {}\n")
    code, env = _validate(proj, sdk)
    assert code == 2
    assert env["issues"][0]["code"] == "validate.board-yaml-unreadable"
    assert "could not read this SDK" not in env["issues"][0]["message"]
