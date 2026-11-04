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


# -- tan platform list ----------------------------------------------------------

RELEASES = [
    {"tag_name": "platform-v26.11.0", "published_at": "2026-11-20T10:00:00Z"},
    {"tag_name": "platform-v26.11.2", "published_at": "2026-12-01T10:00:00Z"},
    {"tag_name": "platform-v27.1.0-rc1", "published_at": "2027-01-10T10:00:00Z"},
    {"tag_name": "platform-v27.1.0", "published_at": "2027-01-20T10:00:00Z"},
    {"tag_name": "platform-v27.01.0", "published_at": "2027-01-21T10:00:00Z"},
    {"tag_name": "v0.1.0", "published_at": "2026-01-01T10:00:00Z"},
]


def _list(ws, monkeypatch, *args, releases=RELEASES, err=None):
    monkeypatch.setattr(platform_fetch, "list_releases", lambda: (None, err) if err else (releases, None))
    return run(ws, "list", *args)


def test_list_newest_first_without_rc_and_skips_unparsable_tags(ws, monkeypatch):
    code, env = _list(ws, monkeypatch)
    assert code == 0
    assert env["data"]["releases"] == [
        {"version": "27.1.0", "line": "27.1", "prerelease": False, "published": "2027-01-20"},
        {"version": "26.11.2", "line": "26.11", "prerelease": False, "published": "2026-12-01"},
        {"version": "26.11.0", "line": "26.11", "prerelease": False, "published": "2026-11-20"},
    ]
    assert env["data"]["pinned"] is None


def test_list_include_rc_orders_rc_before_its_ga(ws, monkeypatch):
    _, env = _list(ws, monkeypatch, "--include-rc")
    assert [r["version"] for r in env["data"]["releases"]][:2] == ["27.1.0", "27.1.0-rc1"]
    assert env["data"]["releases"][1]["prerelease"] is True


def test_list_marks_the_pinned_version(ws, monkeypatch):
    local = ws / "m.yaml"
    local.write_bytes(_manifest_bytes(sdk="0.18.0"))
    run(ws, "use", "--manifest", str(local))
    _, env = _list(ws, monkeypatch)
    assert env["data"]["pinned"] == "27.2.0"


def test_list_network_error_is_an_envelope(ws, monkeypatch):
    code, env = _list(ws, monkeypatch, err="offline")
    assert code == 1 and env["issues"][0]["code"] == "platform.list-failed"
    assert env["data"] == {"releases": [], "pinned": None}


def test_fetch_403_404_mention_github_token(monkeypatch):
    import urllib.error

    class Opener:
        def open(self, *a, **k):
            raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr("tan.commands.sdk_cmd._releases_opener", lambda proxy: Opener())
    releases, err = platform_fetch.list_releases()
    assert releases is None and "GITHUB_TOKEN" in err


# -- review follow-ups ---------------------------------------------------------


def test_use_line_refuses_a_manifest_naming_another_version(ws, monkeypatch):
    monkeypatch.setattr(platform_fetch, "list_release_tags", lambda: (["platform-v27.2.1"], None))
    monkeypatch.setattr(
        platform_fetch, "fetch_manifest", lambda url: (_manifest_bytes(sdk="0.18.0"), None)
    )  # names 27.2.0, not the resolved 27.2.1
    code, env = run(ws, "use", "27.2")
    assert code == 2 and env["issues"][0]["code"] == "platform.manifest-invalid"
    assert not (ws / ".alp" / "platform.yaml").exists()


def test_use_full_version_refuses_a_manifest_naming_another_version(ws, monkeypatch):
    monkeypatch.setattr(
        platform_fetch, "fetch_manifest", lambda url: (_manifest_bytes(sdk="0.18.0"), None)
    )
    code, env = run(ws, "use", "27.2.5")
    assert code == 2 and env["issues"][0]["code"] == "platform.manifest-invalid"


def test_show_and_doctor_report_an_unreadable_pin_with_the_real_error(ws):
    pin = ws / ".alp" / "platform.yaml"
    pin.parent.mkdir()
    pin.write_bytes(_manifest_bytes())
    pin.chmod(0)
    try:
        if _readable(pin):
            pytest.skip("cannot make the pin unreadable here (root or Windows)")
        code, env = run(ws, "show")
        assert code == 2 and env["issues"][0]["code"] == "platform.pin-invalid"
        assert "cannot be read" in env["issues"][0]["message"]
        data, check = _doctor(ws)
        assert check["status"] == "fail" and "cannot be read" in check["detail"]
        assert data["platform"] is None
    finally:
        pin.chmod(0o600)


def _readable(path) -> bool:
    try:
        path.read_bytes()
    except OSError:
        return False
    return True


class _Capture:
    def __init__(self):
        self.request = None

    def open(self, request, timeout=None):
        import io

        self.request = request

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return Resp(b"{}")


@pytest.mark.parametrize(
    ("url", "expect_token"),
    [
        ("https://github.com/alplabai/alp-platform/releases/download/x/platform.yaml", True),
        ("https://api.github.com/repos/alplabai/alp-platform/releases", True),
        ("https://objects.githubusercontent.com/x", False),
    ],
)
def test_github_token_is_unredirected_and_host_scoped(monkeypatch, url, expect_token):
    cap = _Capture()
    monkeypatch.setattr("tan.commands.sdk_cmd._releases_opener", lambda proxy: cap)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    platform_fetch._get(url, "x/y", 1000)
    # `headers` is what urllib copies onto a redirect; the token must not be there.
    assert "Authorization" not in cap.request.headers
    has = cap.request.unredirected_hdrs.get("Authorization") == "Bearer tok"
    assert has is expect_token


def test_oversize_response_is_refused(monkeypatch):
    class Big(_Capture):
        def open(self, request, timeout=None):
            resp = super().open(request, timeout)
            resp.seek(0)
            resp.truncate()
            resp.write(b"x" * 50)
            resp.seek(0)
            return resp

    monkeypatch.setattr("tan.commands.sdk_cmd._releases_opener", lambda proxy: Big())
    body, err = platform_fetch._get("https://api.github.com/x", "x/y", 10)
    assert body is None and "larger than 10 bytes" in err
