# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1496: alp-sdk's per-artifact sha256 pin is compared with the release
`sha256.sum` before tan downloads anything. Hermetic: a fake sum file, no
network; the pinned installs are the phase suite's `installs` stubs."""
from __future__ import annotations

import pytest

from tan.commands import bootstrap_cmd, bootstrap_toolchain_pin
from tan.commands import bootstrap_toolchain_fetch as fetch
from tan.core import toolchain_pin
from tan.core.toolchain_provision import ToolchainArtifact
from tests.commands.test_bootstrap_toolchain_phase import (
    _make_sdk_with_toolchains,
    _no_spawns,  # noqa: F401 -- autouse fixture
    _point_home_at,
    _small_manifest,
    _stub_compiler_probe,  # noqa: F401 -- autouse fixture
    _stub_release_sum_fetch,  # noqa: F401 -- autouse fixture
    _workspace,
    installs,  # noqa: F401 -- autouse fixture
)

_REAL_FETCH = bootstrap_toolchain_pin.fetch_sum_text  # before the autouse stub replaces it
A = "a" * 64
B = "b" * 64
GOOD = f"{A}  x.tar.xz\n{B}  y.tar.xz\n"


def _run(tmp_path, monkeypatch, fetch_sum):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap_toolchain_pin, "fetch_sum_text", fetch_sum)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(
        _workspace(tmp_path), log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False
    )
    return log


def test_a_matching_sum_lets_the_install_proceed(tmp_path, monkeypatch, installs):
    urls = []
    log = _run(tmp_path, monkeypatch, lambda url: (urls.append(url), (GOOD, None))[1])
    assert not [c for c, _ in log.warnings if c.startswith("toolchain-pin")]
    assert installs.minimal_calls == 1 and installs.toolchain_calls == 1
    # Once, before downloading: tan hashes both archives itself, so there is
    # no west run in between to re-check against (tan-cli#1556).
    assert urls == ["https://example.invalid/sha256.sum"]


def test_a_transport_failure_of_the_archive_is_retried_but_a_mismatch_is_not(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(bootstrap_cmd.time, "sleep", sleeps.append)
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 3:
            gcc = a[2] / "gnu" / "arm-zephyr-eabi" / "bin"
            gcc.mkdir(parents=True)
            (gcc / "arm-zephyr-eabi-gcc").write_text("stub", encoding="utf-8")
            return fetch.FetchOutcome("ok")
        return fetch.FetchOutcome("unverified", "reset")

    monkeypatch.setattr(fetch, "install_pinned_toolchain", flaky)
    log = _run(tmp_path, monkeypatch, lambda url: (GOOD, None))
    assert len(calls) == 3 and not [c for c, _ in log.warnings if c.startswith("toolchain-pin")]
    assert sleeps and sleeps[0] > 0

    calls.clear()
    monkeypatch.setattr(
        fetch, "install_pinned_toolchain",
        lambda *a, **k: (calls.append(1), fetch.FetchOutcome("mismatch", "bad"))[1],
    )
    (tmp_path / "again").mkdir()
    log = _run(tmp_path / "again", monkeypatch, lambda url: (GOOD, None))
    assert len(calls) == 1 and log.blocking() == ["toolchain-pin-mismatch"]


@pytest.mark.parametrize("kind,code", [
    ("mismatch", "toolchain-pin-mismatch"),
    ("unverified", "toolchain-pin-unverified"),
    ("missing", "toolchain-install"),
    ("install", "toolchain-install"),
])
def test_a_toolchain_download_failure_is_a_coded_refusal_and_not_stamped(
    tmp_path, monkeypatch, kind, code
):
    monkeypatch.setattr(
        fetch, "install_pinned_toolchain", lambda *a, **k: fetch.FetchOutcome(kind, "boom")
    )
    monkeypatch.setattr(bootstrap_cmd.time, "sleep", lambda s: None)
    log = _run(tmp_path, monkeypatch, lambda url: (GOOD, None))
    assert log.blocking() == [code]
    assert "boom" in log.warnings[0][1]
    assert not list((tmp_path / "home" / ".alp" / "toolchains").glob("*/.alp-toolchain-stamp.json"))


def test_a_differing_sum_refuses_before_anything_is_downloaded(tmp_path, monkeypatch, installs):
    swapped = f"{'c' * 64}  x.tar.xz\n{B}  y.tar.xz\n"
    log = _run(tmp_path, monkeypatch, lambda url: (swapped, None))
    assert installs.minimal_calls == 0
    assert log.blocking() == ["toolchain-pin-mismatch"]
    msg = log.warnings[0][1]
    assert "x.tar.xz" in msg and A in msg and "c" * 64 in msg
    assert "y.tar.xz" not in msg
    assert "west" not in msg


def test_a_missing_entry_refuses(tmp_path, monkeypatch, installs):
    log = _run(tmp_path, monkeypatch, lambda url: (f"{A}  x.tar.xz\n", None))
    assert installs.minimal_calls == 0
    assert log.blocking() == ["toolchain-pin-mismatch"]
    assert "no entry for this file" in log.warnings[0][1]


def test_a_fetch_failure_is_a_coded_refusal_never_a_silent_pass(tmp_path, monkeypatch, installs):
    log = _run(tmp_path, monkeypatch, lambda url: (None, "URLError: offline"))
    assert installs.minimal_calls == 0
    assert log.blocking() == ["toolchain-pin-unverified"]
    assert "offline" in log.warnings[0][1]


def test_dry_run_does_not_touch_the_network(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")

    def boom(url):
        raise AssertionError("dry-run must not fetch")

    monkeypatch.setattr(bootstrap_toolchain_pin, "fetch_sum_text", boom)
    log = bootstrap_cmd.Log(json_mode=True)
    runner = bootstrap_cmd.Runner(json=True, dry_run=True)
    bootstrap_cmd.toolchain_phase(
        _workspace(tmp_path), log, runner, sdk_root, None, is_windows=False,
    )
    assert log.blocking() == []
    lines = [" ".join(a) for a in runner.planned]
    assert [ln.split()[3] for ln in lines if ln.startswith("# tan downloads")] == [
        "https://example.invalid/sha256.sum",
        "https://example.invalid/x.tar.xz",
        "https://example.invalid/y.tar.xz",
    ]


def _findings(text, *names):
    arts = tuple(
        ToolchainArtifact("h", "c", n, 1, A) for n in names
    )
    return toolchain_pin.pin_findings(arts, toolchain_pin.parse_sum_file(text))


def test_parser_matches_wests_plain_and_crlf_lines():
    assert _findings(f"{A}  x.tar.xz\r\n{B}  y\r\n", "x.tar.xz") == ()
    assert _findings(f"{A}\tx.tar.xz\n", "x.tar.xz") == ()


@pytest.mark.parametrize("line", [
    f"{A} *x.tar.xz",          # binary marker: west's key is '*x.tar.xz', never matches
    f"{A}  dist/x.tar.xz",     # path prefix: ditto
    f"{A}  .\\x.tar.xz",
    f"{A.upper()}  x.tar.xz",  # west compares to a lowercase hexdigest
    f"{A}  x.tar.xz  extra",   # three tokens
])
def test_an_entry_west_would_not_match_is_refused_not_guessed(line):
    found = _findings(line + "\n", "x.tar.xz")
    assert len(found) == 1 and found[0].problem


def test_a_duplicate_entry_with_different_hashes_is_refused_even_if_last_matches():
    found = _findings(f"{B}  x.tar.xz\n{A}  x.tar.xz\n", "x.tar.xz")
    assert len(found) == 1 and "different hashes" in found[0].problem
    assert _findings(f"{A}  x.tar.xz\n{A}  x.tar.xz\n", "x.tar.xz") == ()


def test_the_stamp_records_whether_the_pin_was_checked():
    from tan.core import toolchain_provision as tp

    old = tp.parse_stamp('{"version":"1","manifestDigest":"d","targetTriple":"t"}')
    assert old is not None and old.pin_checked is False
    assert old.bundle_pin_checked is False
    new = tp.parse_stamp(tp.render_stamp(tp.ToolchainStamp("1", "d", "t", True, True)))
    assert new is not None and new.pin_checked is True and new.bundle_pin_checked is True


def test_fetch_sum_text_reports_transport_errors_as_a_message(monkeypatch):
    from tan.commands import sdk_cmd

    class Boom:
        def open(self, *a, **k):
            raise OSError("no route")

    monkeypatch.setattr(sdk_cmd, "_releases_opener", lambda proxy: Boom())
    from tan.core.proxy import HTTPS_PROXY_ENV_VARS

    for var in (*HTTPS_PROXY_ENV_VARS, "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    text, why = _REAL_FETCH("https://example.invalid/sha256.sum")
    assert text is None and "no route" in why


def test_fetch_sum_text_refuses_an_unroutable_proxy(monkeypatch):
    from tan.core.proxy import HTTPS_PROXY_ENV_VARS

    for var in (*HTTPS_PROXY_ENV_VARS, "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1")
    text, why = _REAL_FETCH("https://example.invalid/sha256.sum")
    assert text is None and why
