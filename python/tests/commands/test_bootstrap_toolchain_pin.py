# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1496: alp-sdk's per-artifact sha256 pin is compared with the release
`sha256.sum` BEFORE `west sdk install`. Hermetic: a fake sum file, no network,
west never spawned."""
from __future__ import annotations

import pytest

from tan.commands import bootstrap_cmd, bootstrap_toolchain_pin
from tan.core import toolchain_pin
from tan.core.toolchain_provision import ToolchainArtifact  # noqa: F401
from tests.commands.test_bootstrap_toolchain_phase import (
    _stub_release_sum_fetch,  # noqa: F401 -- autouse fixture
    _stub_toolchain_download,  # noqa: F401 -- autouse fixture
    _make_sdk_with_toolchains,
    _point_home_at,
    _small_manifest,
    _stub_compiler_probe,  # noqa: F401 -- autouse fixture
    _workspace,
)

_REAL_FETCH = bootstrap_toolchain_pin.fetch_sum_text  # before the autouse stub replaces it
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
    # once before west, once after (check-then-use re-fetch)
    assert urls == ["https://example.invalid/sha256.sum"] * 2


def test_a_sum_that_changes_while_west_runs_is_refused(tmp_path, monkeypatch):
    texts = iter([GOOD, GOOD + "# republished\n"])
    log, spawned = _run(tmp_path, monkeypatch, lambda url: (next(texts), None))
    assert spawned
    assert log.blocking() == ["toolchain-pin-mismatch"]
    assert "changed while" in log.warnings[0][1]
    assert not list((tmp_path / "home" / ".alp" / "toolchains").glob("*/.alp-toolchain-stamp.json"))


def test_a_sum_that_goes_bad_after_west_ran_is_refused(tmp_path, monkeypatch):
    texts = iter([(GOOD, None), (None, "URLError: gone")])
    log, _ = _run(tmp_path, monkeypatch, lambda url: next(texts))
    assert log.blocking() == ["toolchain-pin-unverified"]


def test_a_transport_failure_of_the_archive_is_retried_but_a_mismatch_is_not(tmp_path, monkeypatch):
    from tan.commands import bootstrap_toolchain_fetch as fetch

    sleeps = []
    monkeypatch.setattr(bootstrap_cmd.time, "sleep", sleeps.append)
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        return fetch.FetchOutcome("ok") if len(calls) == 3 else fetch.FetchOutcome("unverified", "reset")

    monkeypatch.setattr(fetch, "install_pinned_toolchain", flaky)
    log, _ = _run(tmp_path, monkeypatch, lambda url: (GOOD, None))
    assert len(calls) == 3 and not [c for c, _ in log.warnings if c.startswith("toolchain-pin")]
    assert sleeps and sleeps[0] > 0

    calls.clear()
    monkeypatch.setattr(
        fetch, "install_pinned_toolchain",
        lambda *a, **k: (calls.append(1), fetch.FetchOutcome("mismatch", "bad"))[1],
    )
    log, _ = _run(tmp_path, monkeypatch, lambda url: (GOOD, None))
    assert len(calls) == 1 and log.blocking() == ["toolchain-pin-mismatch"]


@pytest.mark.parametrize("kind,code", [
    ("mismatch", "toolchain-pin-mismatch"),
    ("unverified", "toolchain-pin-unverified"),
    ("install", "toolchain-install"),
])
def test_a_toolchain_download_failure_is_a_coded_refusal_and_not_stamped(
    tmp_path, monkeypatch, kind, code
):
    from tan.commands import bootstrap_toolchain_fetch as fetch

    monkeypatch.setattr(
        fetch, "install_pinned_toolchain", lambda *a, **k: fetch.FetchOutcome(kind, "boom")
    )
    monkeypatch.setattr(bootstrap_cmd.time, "sleep", lambda s: None)
    log, _ = _run(tmp_path, monkeypatch, lambda url: (GOOD, None))
    assert log.blocking() == [code]
    assert "boom" in log.warnings[0][1]


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
    runner = bootstrap_cmd.Runner(json=True, dry_run=True)
    bootstrap_cmd.toolchain_phase(
        _workspace(tmp_path), log, runner, sdk_root, None, is_windows=False,
    )
    assert log.blocking() == []
    lines = [" ".join(a) for a in runner.planned]
    downloads = [ln for ln in lines if ln.startswith("# tan downloads")]
    assert [ln.split()[3] for ln in downloads] == [
        "https://example.invalid/sha256.sum",
        "https://example.invalid/y.tar.xz",
        "https://example.invalid/sha256.sum",
    ]
    # order: sum, then west, then archive + sum re-fetch
    west_at = next(i for i, ln in enumerate(lines) if ln.startswith("west sdk install"))
    assert lines[west_at - 1].startswith("# tan downloads") and "sha256.sum" in lines[west_at - 1]
    assert "y.tar.xz" in lines[west_at + 1]


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
    new = tp.parse_stamp(tp.render_stamp(tp.ToolchainStamp("1", "d", "t", True)))
    assert new is not None and new.pin_checked is True


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
