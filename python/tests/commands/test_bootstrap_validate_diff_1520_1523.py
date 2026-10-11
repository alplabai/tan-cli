# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1520 (doctor host-adoption pass carries the integrity caveat) and
tan-cli#1523 (skew direction, Windows 7-Zip preflight, diff crash message)."""
from __future__ import annotations

import json
import types

import pytest

from tan.commands import bootstrap_cmd, diff_cmd, validate_cmd
from tan.core.bootstrap import fallback_facts


@pytest.fixture
def sdk(tmp_path):
    root = tmp_path / "sdk"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "validate_board_yaml.py").write_text("# differs\n", encoding="utf-8")
    return root


def test_an_older_bound_sdk_is_not_called_newer(sdk, monkeypatch):
    monkeypatch.setattr(validate_cmd, "_sdk_direction", lambda _r: "older")
    msg = validate_cmd.skew_warning(sdk)
    assert "newer than" not in msg and "Rules added since are not applied" not in msg
    assert "older" in msg and "can reject a board this SDK accepts" in msg


def test_a_newer_bound_sdk_keeps_the_newer_wording(sdk, monkeypatch):
    monkeypatch.setattr(validate_cmd, "_sdk_direction", lambda _r: "newer")
    assert "newer than" in validate_cmd.skew_warning(sdk)


def test_an_unordered_checkout_gets_a_neutral_message(sdk):
    # tmp dir is not a git checkout -> direction unknown.
    msg = validate_cmd.skew_warning(sdk)
    assert "newer than" not in msg and "differ from" in msg
    assert "scripts/validate_board_yaml.py" in msg and "TAN_VALIDATE_ENGINE=subprocess" in msg


def test_windows_preflight_rejects_unar_only(tmp_path, monkeypatch):
    """tan-cli#1523: unar is not enough for tan's own .7z extractor, so the
    preflight must refuse before the download."""
    monkeypatch.setattr(bootstrap_cmd, "on_path", lambda program: program == "unar")
    log = bootstrap_cmd.Log(json_mode=True)
    ws = bootstrap_cmd.Workspace(
        is_windows=True,
        facts=fallback_facts((3, 10)),
        repo_root=tmp_path / "ws" / "alp-sdk",
        workspace_dir=tmp_path / "ws",
        venv_dir=tmp_path / "ws" / ".venv",
    )
    sdk = tmp_path / "ws" / "alp-sdk"
    (sdk / "metadata").mkdir(parents=True)
    (sdk / "metadata" / "toolchains.json").write_text(
        json.dumps(
            {
                "zephyrSdk": {"version": "1.0.1", "baseUrl": "https://example.invalid/", "artifacts": [
                    {"host": "windows-x86_64", "component": "minimal-sdk", "filename": "x.7z", "sizeBytes": 1, "sha256": "a" * 64}
                ]},
                "measuredFootprint": {"extractedBytes": {"wholeSdk": 1}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "win32")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("ALP_TOOLCHAIN_ROOT", raising=False)
    bootstrap_cmd.toolchain_phase(ws, log, bootstrap_cmd.Runner(json=True), str(sdk), None, is_windows=True)
    msgs = [m for _c, m in log.warnings]
    assert any("No 7-Zip" in m and "`unar` alone is not enough" in m for m in msgs), msgs


def test_diff_crash_message_does_not_call_the_board_invalid(tmp_path, monkeypatch):
    crash = validate_cmd._Result(
        validate_cmd.OUTCOME_FAILED,
        (validate_cmd._Finding(
            "error",
            f"{validate_cmd.VALIDATOR_CRASH_PREFIX} (bound alp-sdk v1). This is not a verdict on board.yaml",
        ),),
    )
    monkeypatch.setattr(diff_cmd, "run_in_process_engine", lambda *_a: (1, "", crash))
    monkeypatch.setattr(diff_cmd, "subprocess_engine_requested", lambda: False)
    (tmp_path / "metadata").mkdir()
    sdk_info = types.SimpleNamespace(root=str(tmp_path))
    with pytest.raises(diff_cmd.ParseFailure) as err:
        diff_cmd._reject_if_sdk_validator_disagrees(sdk_info, str(tmp_path), str(tmp_path / "board.yaml"))
    assert "board.yaml is not valid" not in err.value.message
    assert "not a verdict on board.yaml" in err.value.message


def test_diff_genuine_rejection_keeps_the_invalid_wording(tmp_path, monkeypatch):
    bad = validate_cmd._Result(
        "schema-violation", (validate_cmd._Finding("error", "som.sku missing"),)
    )
    monkeypatch.setattr(diff_cmd, "run_in_process_engine", lambda *_a: (1, "", bad))
    monkeypatch.setattr(diff_cmd, "subprocess_engine_requested", lambda: False)
    (tmp_path / "metadata").mkdir()
    sdk_info = types.SimpleNamespace(root=str(tmp_path))
    with pytest.raises(diff_cmd.ParseFailure) as err:
        diff_cmd._reject_if_sdk_validator_disagrees(sdk_info, str(tmp_path), "b.yaml")
    assert err.value.message.startswith("board.yaml is not valid")
