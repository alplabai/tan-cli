# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1496: alp-sdk's per-artifact sha256 pin is compared with the release
`sha256.sum` BEFORE `west sdk install`. Hermetic: a fake sum file, no network,
west never spawned."""
from __future__ import annotations

import pytest

from tan.commands import bootstrap_cmd, bootstrap_toolchain_pin
from tan.core import toolchain_pin
from tests.commands.test_bootstrap_toolchain_phase import (
    _make_sdk_with_toolchains,
    _point_home_at,
    _small_manifest,
    _stub_compiler_probe,  # noqa: F401 -- autouse fixture
    _workspace,
)

A = "a" * 64
B = "b" * 64
GOOD = f"{A}  x.tar.xz\n{B}  y.tar.xz\n"


def _run(tmp_path, monkeypatch, fetch):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    spawned: list[list[str]] = []

    def record(self, argv, *a, **kw):
        spawned.append(list(argv))

    monkeypatch.setattr(bootstrap_cmd.Runner, "run", record)
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap_toolchain_pin, "fetch_sum_text", fetch)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(
        _workspace(tmp_path), log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False
    )
    return log, spawned


def test_a_matching_sum_lets_the_install_proceed(tmp_path, monkeypatch):
    urls = []
    log, spawned = _run(tmp_path, monkeypatch, lambda url: (urls.append(url), (GOOD, None))[1])
    assert not [c for c, _ in log.warnings if c.startswith("toolchain-pin")]
    assert any("install" in a for a in spawned)
    assert urls == ["https://example.invalid/sha256.sum"]


def test_a_differing_sum_refuses_before_west_runs(tmp_path, monkeypatch):
    swapped = f"{'c' * 64}  x.tar.xz\n{B}  y.tar.xz\n"
    log, spawned = _run(tmp_path, monkeypatch, lambda url: (swapped, None))
    assert spawned == []
    assert log.blocking() == ["toolchain-pin-mismatch"]
    msg = log.warnings[0][1]
    assert "x.tar.xz" in msg and A in msg and "c" * 64 in msg
    assert "y.tar.xz" not in msg


def test_a_missing_entry_refuses(tmp_path, monkeypatch):
    log, spawned = _run(tmp_path, monkeypatch, lambda url: (f"{A}  x.tar.xz\n", None))
    assert spawned == []
    assert log.blocking() == ["toolchain-pin-mismatch"]
    assert "no entry for this file" in log.warnings[0][1]


def test_a_fetch_failure_is_a_coded_refusal_never_a_silent_pass(tmp_path, monkeypatch):
    log, spawned = _run(tmp_path, monkeypatch, lambda url: (None, "URLError: offline"))
    assert spawned == []
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
    bootstrap_cmd.toolchain_phase(
        _workspace(tmp_path), log, bootstrap_cmd.Runner(json=True, dry_run=True), sdk_root, None,
        is_windows=False,
    )
    assert log.blocking() == []


def test_parse_sum_file_handles_binary_marker_paths_and_noise():
    text = f"# c\n{A} *dir/x.tar.xz\nnot a line\n{B.upper()}  y.7z\n"
    assert toolchain_pin.parse_sum_file(text) == {"x.tar.xz": A, "y.7z": B}


def test_fetch_sum_text_reports_transport_errors_as_a_message(monkeypatch):
    from tan.commands import sdk_cmd

    class Boom:
        def open(self, *a, **k):
            raise OSError("no route")

    monkeypatch.setattr(sdk_cmd, "_releases_opener", lambda proxy: Boom())
    monkeypatch.delenv("ALL_PROXY", raising=False)
    text, why = bootstrap_toolchain_pin.fetch_sum_text("https://example.invalid/sha256.sum")
    assert text is None and "no route" in why


def test_fetch_sum_text_refuses_an_unroutable_proxy(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1")
    text, why = bootstrap_toolchain_pin.fetch_sum_text("https://example.invalid/sha256.sum")
    assert text is None and why
