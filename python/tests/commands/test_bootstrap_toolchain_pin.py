# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1496/#1559: alp-sdk's per-artifact sha256 pin is what every archive
tan downloads is hashed against; the release `sha256.sum` is fetched only after
a mismatch, to explain it. Hermetic: a fake sum file, no network; the pinned
installs are the phase suite's `installs` stubs."""
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


def _never_fetched(url):
    raise AssertionError("the release sum must not be fetched unless a hash mismatches")


def test_a_successful_install_never_fetches_the_sum(tmp_path, monkeypatch, installs):
    """tan-cli#1559: both archives are hashed against the pin; the sum adds
    nothing to a successful install, so it is not even fetched."""
    log = _run(tmp_path, monkeypatch, _never_fetched)
    assert log.blocking() == []
    assert installs.minimal_calls == 1 and installs.toolchain_calls == 1


def test_a_sum_that_disagrees_with_the_pin_does_not_block_a_matching_download(
    tmp_path, monkeypatch, installs
):
    """The pin decides. A sum that differs is never consulted when the bytes
    tan downloaded hash to the pin."""
    swapped = f"{'c' * 64}  x.tar.xz\n{B}  y.tar.xz\n"
    log = _run(tmp_path, monkeypatch, lambda url: (swapped, None))
    assert log.blocking() == []


def test_a_mismatch_fetches_the_sum_once_and_says_upstream_republished(
    tmp_path, monkeypatch, installs
):
    got = "d" * 64
    installs.minimal_outcomes = [
        fetch.FetchOutcome("mismatch", f"x.tar.xz has sha256 {got}", got_sha256=got)
    ]
    urls = []
    log = _run(
        tmp_path, monkeypatch,
        lambda url: (urls.append(url), (f"{got}  x.tar.xz\n{B}  y.tar.xz\n", None))[1],
    )
    assert log.blocking() == ["toolchain-pin-mismatch"]
    assert "consistent with upstream republishing" in log.warnings[0][1]
    assert urls == ["https://example.invalid/sha256.sum"]
    assert installs.minimal_calls == 1  # a mismatch is never retried


def test_a_mismatch_with_the_pin_still_published_is_blamed_on_the_transit(
    tmp_path, monkeypatch, installs
):
    got = "d" * 64
    installs.minimal_outcomes = [
        fetch.FetchOutcome("mismatch", f"x.tar.xz has sha256 {got}", got_sha256=got)
    ]
    log = _run(tmp_path, monkeypatch, lambda url: (GOOD, None))
    assert "changing in transit" in log.warnings[0][1]


def test_an_unreachable_sum_leaves_a_mismatch_refused(tmp_path, monkeypatch, installs):
    got = "d" * 64
    installs.minimal_outcomes = [
        fetch.FetchOutcome("mismatch", f"x.tar.xz has sha256 {got}", got_sha256=got)
    ]
    log = _run(tmp_path, monkeypatch, lambda url: (None, "URLError: offline"))
    assert log.blocking() == ["toolchain-pin-mismatch"]
    assert "could not be fetched" in log.warnings[0][1] and "offline" in log.warnings[0][1]


def test_dry_run_plans_both_archives_and_no_sum(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap_toolchain_pin, "fetch_sum_text", _never_fetched)
    log = bootstrap_cmd.Log(json_mode=True)
    runner = bootstrap_cmd.Runner(json=True, dry_run=True)
    bootstrap_cmd.toolchain_phase(
        _workspace(tmp_path), log, runner, sdk_root, None, is_windows=False,
    )
    assert log.blocking() == []
    lines = [" ".join(a) for a in runner.planned]
    assert [ln.split()[3] for ln in lines if ln.startswith("# tan downloads")] == [
        "https://example.invalid/x.tar.xz",
        "https://example.invalid/y.tar.xz",
    ]


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


# ---------------------------------------------------------------------------
# tan-cli#1559: the sum explains a mismatch; it never causes or excuses one
# ---------------------------------------------------------------------------


_ART = ToolchainArtifact("h", "c", "x.tar.xz", 1, A)
_GOT = "d" * 64


def test_a_sum_still_listing_the_pin_means_the_bytes_changed_in_transit():
    msg = toolchain_pin.mismatch_diagnosis(_ART, _GOT, f"{A}  x.tar.xz\n", "")
    assert "consistent with the bytes changing in transit" in msg


def test_a_sum_listing_the_downloaded_hash_means_upstream_republished():
    msg = toolchain_pin.mismatch_diagnosis(_ART, _GOT, f"{_GOT}  x.tar.xz\n", "")
    assert "consistent with upstream republishing" in msg


def test_a_sum_listing_a_third_hash_says_so():
    msg = toolchain_pin.mismatch_diagnosis(_ART, _GOT, f"{'e' * 64}  x.tar.xz\n", "")
    assert "e" * 64 in msg and "not the pin and not what was downloaded" in msg


def test_a_sum_with_no_entry_or_an_ambiguous_one_is_named():
    assert "no entry" in toolchain_pin.mismatch_diagnosis(_ART, _GOT, f"{A}  other\n", "")
    ambiguous = toolchain_pin.mismatch_diagnosis(_ART, _GOT, f"{A}  x.tar.xz\n{B}  x.tar.xz\n", "")
    assert "ambiguous" in ambiguous


def test_an_unreachable_sum_leaves_the_refusal_undiagnosed_but_standing():
    msg = toolchain_pin.mismatch_diagnosis(_ART, _GOT, None, "URLError: offline")
    assert "could not be fetched" in msg and "offline" in msg


def test_every_diagnosis_that_names_a_cause_says_to_confirm_before_moving_the_pin():
    """tan-cli#1559 review: the sum comes over the channel that just delivered
    bad bytes, so a diagnosis is evidence, never permission to re-pin."""
    for sum_text in (f"{A}  x.tar.xz\n", f"{_GOT}  x.tar.xz\n", f"{'e' * 64}  x.tar.xz\n"):
        msg = toolchain_pin.mismatch_diagnosis(_ART, _GOT, sum_text, "")
        assert "Confirm on the release page before touching the pin." in msg, msg
        assert "served here" in msg


def test_an_oversized_download_is_still_diagnosed_without_a_hash():
    """A download refused for exceeding the pinned size was never hashed;
    the sum can still say whether it lists the pin."""
    assert "changing in transit" in toolchain_pin.mismatch_diagnosis(_ART, None, f"{A}  x.tar.xz\n", "")
    other = toolchain_pin.mismatch_diagnosis(_ART, None, f"{_GOT}  x.tar.xz\n", "")
    assert "not the pin." in other and "republishing" not in other


def test_an_oversized_download_still_fetches_the_sum_to_explain_itself(
    tmp_path, monkeypatch, installs
):
    installs.minimal_outcomes = [
        fetch.FetchOutcome("mismatch", "x.tar.xz is larger than the 1 bytes alp-sdk pins")
    ]
    urls = []
    log = _run(tmp_path, monkeypatch, lambda url: (urls.append(url), (GOOD, None))[1])
    assert log.blocking() == ["toolchain-pin-mismatch"]
    assert urls == ["https://example.invalid/sha256.sum"]
    assert "changing in transit" in log.warnings[0][1]


def test_a_slow_drip_sum_is_abandoned_at_its_deadline(monkeypatch):
    import io

    from tan.commands import sdk_cmd

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp(b"x" * 10_000_000)

    monkeypatch.setattr(sdk_cmd, "_releases_opener", lambda proxy: _Opener())
    monkeypatch.setattr(bootstrap_toolchain_pin, "select_https_proxy", lambda url: None)
    clock = iter([0.0] + [bootstrap_toolchain_pin.SUM_FETCH_DEADLINE_S + 1] * 1000)
    monkeypatch.setattr(bootstrap_toolchain_pin, "_now", lambda: next(clock))

    text, why = _REAL_FETCH("https://example.invalid/sha256.sum")

    assert text is None and "did not finish within" in why
