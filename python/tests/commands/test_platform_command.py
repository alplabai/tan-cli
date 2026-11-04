# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1571: `tan platform use|show|clear` and `tan doctor`'s `data.platform`.
No real network: the two fetch functions are faked."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tan.cli import app
from tan.commands import platform_fetch
from tests.core.test_platform_manifest import GOOD

runner = CliRunner()


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A west workspace with a minimal alp-sdk at 0.18.0, and a local manifest."""
    (tmp_path / ".west").mkdir()
    sdk = tmp_path / "alp-sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "metadata").mkdir()
    (sdk / "scripts" / "alp_project.py").write_text("")
    (sdk / "VERSION").write_text("0.18.0\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    return tmp_path


def _manifest_bytes(**sdk_ver) -> bytes:
    data = copy.deepcopy(GOOD)
    if sdk_ver:
        data["components"]["alp-sdk"]["version"] = sdk_ver["sdk"]
    return b"# comment kept byte-for-byte\n" + yaml.safe_dump(data).encode()


def run(ws, *args):
    res = runner.invoke(app, ["platform", *args, "--sdk-root", str(ws / "alp-sdk"), "--format", "json"])
    return res.exit_code, json.loads(res.stdout)


def test_use_manifest_writes_pin_byte_for_byte_and_show_clear(ws):
    local = ws / "m.yaml"
    local.write_bytes(_manifest_bytes(sdk="0.18.0"))
    code, env = run(ws, "use", "--manifest", str(local))
    assert code == 0 and env["ok"] is True
    pin = ws / ".alp" / "platform.yaml"
    assert pin.read_bytes() == local.read_bytes()
    assert env["data"]["platform"]["version"] == "27.2.0"
    assert env["data"]["platform"]["components"][0]["status"] == "match"
    assert env["data"]["nextSteps"] == []

    code, env = run(ws, "show")
    assert code == 0 and env["data"]["platform"]["line"] == "27.2"

    code, env = run(ws, "clear")
    assert code == 0 and env["data"]["cleared"] is True and not pin.exists()
    assert run(ws, "show")[1]["data"]["platform"] is None
    assert run(ws, "clear")[1]["data"]["cleared"] is False


def test_use_reports_sdk_mismatch_as_next_step_not_failure(ws):
    local = ws / "m.yaml"
    local.write_bytes(_manifest_bytes(sdk="0.19.0"))
    code, env = run(ws, "use", "--manifest", str(local))
    assert code == 0
    assert "v0.19.0" in env["data"]["nextSteps"][0]
    assert any(i["code"] == "platform.sdk-off-manifest" for i in env["issues"])
    # tan itself is reported (0.8.0 pinned vs this build), never updated.
    assert any(i["code"] == "platform.tan-off-manifest" for i in env["issues"])


def test_use_invalid_manifest_refused_and_nothing_written(ws):
    bad = copy.deepcopy(GOOD)
    bad["components"]["cc3501e-bridge-firmware"]["gpe_stamp"] = "0.254.42.0"
    local = ws / "m.yaml"
    local.write_text(yaml.safe_dump(bad))
    code, env = run(ws, "use", "--manifest", str(local))
    assert code == 2 and env["issues"][0]["code"] == "platform.manifest-invalid"
    assert "permanently bricks" in env["issues"][0]["message"]
    assert not (ws / ".alp" / "platform.yaml").exists()


def test_use_padded_version_refused_without_network(ws, monkeypatch):
    monkeypatch.setattr(platform_fetch, "fetch_manifest", lambda url: pytest.fail("no network"))
    code, env = run(ws, "use", "27.02.0")
    assert code == 2 and "27.2.0" in env["issues"][0]["message"]


def test_use_line_resolves_latest_ga_via_faked_releases(ws, monkeypatch):
    urls = []
    monkeypatch.setattr(
        platform_fetch, "list_release_tags",
        lambda: (["platform-v27.2.0", "platform-v27.2.1", "platform-v27.2.2-rc1"], None),
    )
    body = _manifest_bytes(sdk="0.18.0").replace(b"platform: 27.2.0", b"platform: 27.2.1")
    monkeypatch.setattr(platform_fetch, "fetch_manifest", lambda url: (urls.append(url), (body, None))[1])
    code, env = run(ws, "use", "27.2")
    assert code == 0 and env["data"]["platform"]["version"] == "27.2.1"
    assert urls == ["https://github.com/alplabai/alp-platform/releases/download/platform-v27.2.1/platform.yaml"]


def test_use_fetch_failure_is_an_envelope(ws, monkeypatch):
    monkeypatch.setattr(platform_fetch, "list_release_tags", lambda: (None, "offline"))
    code, env = run(ws, "use", "27.2")
    assert code == 1 and env["issues"][0]["code"] == "platform.fetch-failed"


def test_use_needs_a_west_workspace(ws):
    (ws / ".west").rmdir()
    local = ws / "m.yaml"
    local.write_bytes(_manifest_bytes())
    code, env = run(ws, "use", "--manifest", str(local))
    assert code == 1 and env["issues"][0]["code"] == "platform.no-workspace"


def test_unknown_subcommand(ws):
    code, env = run(ws, "bogus")
    assert code == 1 and env["issues"][0]["code"] == "platform.unknown-subcommand"


# -- tan doctor data.platform -------------------------------------------------


def _doctor(ws):
    res = runner.invoke(app, ["doctor", "--sdk-root", str(ws / "alp-sdk"), "--format", "json"])
    data = json.loads(res.stdout)["data"]
    return data, next(c for c in data["checks"] if c["name"] == "platform")


def test_doctor_no_pin(ws):
    data, check = _doctor(ws)
    assert data["platform"] is None and check["status"] == "pass"


def test_doctor_pin_match_mismatch_and_invalid(ws):
    from tan.version import TAN_VERSION

    pin = ws / ".alp" / "platform.yaml"
    pin.parent.mkdir()
    raw = yaml.safe_load(_manifest_bytes(sdk="0.18.0"))
    raw["components"]["tan-cli"]["version"] = TAN_VERSION
    pin.write_text(yaml.safe_dump(raw))
    data, check = _doctor(ws)
    assert check["status"] == "pass"
    plat = data["platform"]
    assert [c["status"] for c in plat["components"]] == ["match", "match", "unknown", "unknown", "unknown"]
    # The published key set (contract/doctor-data-keys.json) pins this shape.
    contract = json.loads((Path(__file__).resolve().parents[3] / "contract" / "doctor-data-keys.json").read_text("utf-8"))
    decl = contract["dataKeys"]["platform"]["items"]
    assert set(plat) == set(decl)
    assert set(plat["components"][0]) == set(decl["components"][0])

    raw["components"]["alp-sdk"]["version"] = "0.19.0"
    pin.write_text(yaml.safe_dump(raw))
    data, check = _doctor(ws)
    assert check["status"] == "warn" and data["platform"]["components"][0]["status"] == "mismatch"

    pin.write_text("schema: bogus\n")
    data, check = _doctor(ws)
    assert check["status"] == "fail" and data["platform"] is None
