# SPDX-License-Identifier: Apache-2.0
"""`tan.commands.bootstrap_cmd.toolchain_phase` -- ADR 0021 Lane 1 P1
(issue #474), the IO layer around `tan.core.toolchain_provision`'s pure
decisions.

Hermetic throughout. tan installs both pinned archives itself (the SDK minimal
bundle since tan-cli#1556, the `arm-zephyr-eabi` toolchain since tan-cli#1496),
so every case here stubs `bootstrap_toolchain_fetch.install_pinned_minimal_sdk`
and `install_pinned_toolchain` (the `installs` fixture) with fakes that write
what a real install leaves on disk, or return a failure outcome. Their own
download/hash/extract logic is covered in test_bootstrap_toolchain_fetch.py and
test_bootstrap_minimal_sdk_fetch_1556.py. `_probe_toolchain_compiler`'s spawn
is stubbed too (`bootstrap_cmd.probe_status`), so no test needs a real
`arm-zephyr-eabi-gcc`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tan.commands import bootstrap_cmd
from tan.commands import bootstrap_toolchain_fetch as fetch
from tan.core import toolchain_provision as tp
from tan.core.bootstrap import fallback_facts

REAL_TOOLCHAINS_MANIFEST = (
    Path(__file__).resolve().parents[3] / "contract" / "fixtures" / "toolchains" / "toolchains.json"
).read_text(encoding="utf-8")


def _small_manifest(*, version="1.0.1", host="linux-x86_64", extracted=4096) -> str:
    """A manifest scoped to ONE host with a tiny footprint, so the disk
    preflight in every test but the dedicated disk-refusal one passes
    trivially regardless of the runner's real free space."""
    return json.dumps(
        {
            "zephyrSdk": {
                "version": version,
                "baseUrl": "https://example.invalid/",
                "artifacts": [
                    {
                        "host": host, "component": "minimal-sdk",
                        "filename": "x.tar.xz", "sizeBytes": 1, "sha256": "a" * 64,
                    },
                    {
                        "host": host, "component": "arm-zephyr-eabi-toolchain",
                        "filename": "y.tar.xz", "sizeBytes": 1, "sha256": "b" * 64,
                    },
                ],
            },
            "measuredFootprint": {"extractedBytes": {"wholeSdk": extracted}},
        }
    )


def _workspace(tmp_path: Path, *, is_windows: bool = False) -> bootstrap_cmd.Workspace:
    facts = fallback_facts((3, 10))
    return bootstrap_cmd.Workspace(
        is_windows=is_windows,
        facts=facts,
        repo_root=tmp_path / "ws" / "alp-sdk",
        workspace_dir=tmp_path / "ws",
        venv_dir=tmp_path / "ws" / ".venv",
    )


def _make_sdk_with_toolchains(tmp_path: Path, manifest_text: str) -> str:
    sdk = tmp_path / "ws" / "alp-sdk"
    (sdk / "metadata").mkdir(parents=True, exist_ok=True)
    (sdk / "metadata" / "toolchains.json").write_text(manifest_text, encoding="utf-8")
    return str(sdk)


def _point_home_at(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("ALP_TOOLCHAIN_ROOT", raising=False)


def _linux(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")


def _store_dir(tmp_path: Path, version: str = "1.0.1") -> Path:
    return tmp_path / "home" / ".alp" / "toolchains" / tp.store_dir_name(version)


def _run_phase(tmp_path, sdk_root, *, json_mode=True, dry_run=False, is_windows=False):
    log = bootstrap_cmd.Log(json_mode=json_mode)
    runner = bootstrap_cmd.Runner(json=json_mode, dry_run=dry_run)
    bootstrap_cmd.toolchain_phase(
        _workspace(tmp_path, is_windows=is_windows), log, runner, sdk_root, None,
        is_windows=is_windows,
    )
    return log, runner


@dataclass
class _Installs:
    """What the stubbed pinned installs do. `minimal_outcomes` /
    `toolchain_outcomes` are consumed one per call; when empty, the install
    succeeds and writes what a real one leaves on disk."""

    version: str = "1.0.1"
    minimal_outcomes: list = field(default_factory=list)
    toolchain_outcomes: list = field(default_factory=list)
    minimal_calls: int = 0
    toolchain_calls: int = 0


@pytest.fixture(autouse=True)
def installs(monkeypatch) -> _Installs:
    state = _Installs()

    def minimal(base_url, art, tmp_dir, scratch_parent, leaf):  # noqa: ARG001
        state.minimal_calls += 1
        if state.minimal_outcomes:
            return state.minimal_outcomes.pop(0)
        tmp_dir.mkdir(parents=True)
        (tmp_dir / "sdk_version").write_text(f"{state.version}\n", encoding="utf-8")
        return fetch.FetchOutcome("ok")

    def toolchain(base_url, art, tmp_dir, scratch_parent, leaf):  # noqa: ARG001
        state.toolchain_calls += 1
        if state.toolchain_outcomes:
            return state.toolchain_outcomes.pop(0)
        gcc_dir = tmp_dir / "gnu" / "arm-zephyr-eabi" / "bin"
        gcc_dir.mkdir(parents=True, exist_ok=True)
        (gcc_dir / "arm-zephyr-eabi-gcc").write_text("#!/bin/sh\necho stub\n", encoding="utf-8")
        return fetch.FetchOutcome("ok")

    monkeypatch.setattr(fetch, "install_pinned_minimal_sdk", minimal)
    monkeypatch.setattr(fetch, "install_pinned_toolchain", toolchain)
    return state


@pytest.fixture(autouse=True)
def _stub_compiler_probe(monkeypatch):
    """Every test in this file: `arm-zephyr-eabi-gcc --version` "succeeds"
    without a real subprocess spawn -- portable to every CI shard, and keeps
    this suite testing tan's OWN verification sequencing, not a real GCC
    build's banner text."""
    monkeypatch.setattr(bootstrap_cmd, "probe_status", lambda argv, *a, **kw: (True, "arm-zephyr-eabi-gcc (Zephyr SDK 1.0.1) 14.3.0\n"))


@pytest.fixture(autouse=True)
def _stub_release_sum_fetch(monkeypatch):
    """tan-cli#1496: the pre-download pin check fetches the release
    `sha256.sum`. Hermetic here: a sum that agrees with `_small_manifest`'s
    pins (the dedicated cases live in test_bootstrap_toolchain_pin.py)."""
    from tan.commands import bootstrap_toolchain_pin

    monkeypatch.setattr(
        bootstrap_toolchain_pin,
        "fetch_sum_text",
        lambda url: (f"{'a' * 64}  x.tar.xz\n{'b' * 64}  y.tar.xz\n", None),
    )


@pytest.fixture(autouse=True)
def _no_spawns(monkeypatch):
    """Nothing in this phase spawns a child process any more (tan-cli#1556:
    no `west sdk install`); any `Runner.run` call is a regression."""

    def refuse(self, argv, *a, **kw):  # noqa: ARG001
        raise AssertionError(f"the toolchain phase must not spawn anything: {argv}")

    monkeypatch.setattr(bootstrap_cmd.Runner, "run", refuse)


# ---------------------------------------------------------------------------
# The happy path + the skip
# ---------------------------------------------------------------------------


def test_a_clean_install_is_verified_and_stamped(tmp_path, monkeypatch, installs):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    _linux(monkeypatch)

    log, _ = _run_phase(tmp_path, sdk_root)

    assert log.blocking() == []  # never blocked -- a clean install
    manifest, _ = bootstrap_cmd.load_toolchain_manifest(sdk_root)
    store_dir = _store_dir(tmp_path)
    stamp = bootstrap_cmd._read_toolchain_stamp(store_dir)
    assert tp.stamp_matches_pin(stamp, manifest) is True
    assert stamp.pin_checked and stamp.bundle_pin_checked
    assert (store_dir / "sdk_version").is_file()
    assert (store_dir / "gnu" / "arm-zephyr-eabi" / "bin" / "arm-zephyr-eabi-gcc").is_file()
    assert (installs.minimal_calls, installs.toolchain_calls) == (1, 1)


def test_a_second_bootstrap_skips_without_downloading_again(tmp_path, monkeypatch, installs):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    _linux(monkeypatch)
    first_log, _ = _run_phase(tmp_path, sdk_root)
    assert first_log.blocking() == []

    second_log, _ = _run_phase(tmp_path, sdk_root)

    assert second_log.blocking() == []
    assert second_log.warnings == []
    assert (installs.minimal_calls, installs.toolchain_calls) == (1, 1)


def test_a_stamped_store_whose_compiler_was_deleted_is_repaired_not_skipped(
    tmp_path, monkeypatch, installs
):
    """tan-cli#1483: the stamp alone used to short-circuit the phase, so a
    store whose compiler subtree had been removed was never reinstalled."""
    import shutil

    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    _linux(monkeypatch)
    _run_phase(tmp_path, sdk_root)
    store_dir = _store_dir(tmp_path)
    shutil.rmtree(store_dir / "gnu")
    assert (store_dir / tp.STAMP_FILENAME).is_file()

    _run_phase(tmp_path, sdk_root)

    assert installs.minimal_calls == 2, "stamp over a deleted compiler was skipped"
    assert (store_dir / "gnu" / "arm-zephyr-eabi" / "bin" / "arm-zephyr-eabi-gcc").is_file()


def test_the_install_log_says_both_archives_were_hashed_but_not_that_files_are_verified(
    tmp_path, monkeypatch, capsys
):
    """tan-cli#1483/#1496/#1556: the bytes are hashed while downloading and
    never re-hashed from disk; the log says exactly that."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    _linux(monkeypatch)

    _run_phase(tmp_path, sdk_root, json_mode=False)

    out = capsys.readouterr()
    text = out.out + out.err
    assert "minimal bundle and the toolchain archive were both hashed against alp-sdk's pin" in text
    assert "west" not in text
    assert "verified and stamped" not in text


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_missing_manifest_blocks_with_a_named_reason(tmp_path, monkeypatch, installs):
    _point_home_at(monkeypatch, tmp_path)
    sdk = tmp_path / "ws" / "alp-sdk"
    (sdk / "metadata").mkdir(parents=True)  # no toolchains.json written

    log, _ = _run_phase(tmp_path, str(sdk))

    assert log.blocking() == ["toolchain-install"]
    assert "toolchains.json" in log.warnings[0][1]
    assert installs.minimal_calls == 0


def test_insufficient_disk_refuses_with_both_numbers_named_and_never_downloads(
    tmp_path, monkeypatch, installs
):
    _point_home_at(monkeypatch, tmp_path)
    huge = 999 * (1 << 40)  # 999 TiB -- no real CI runner has this free
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest(extracted=huge))
    _linux(monkeypatch)

    log, _ = _run_phase(tmp_path, sdk_root)

    assert log.blocking() == ["toolchain-install"]
    msg = log.warnings[0][1]
    assert "GiB" in msg
    assert "--no-toolchain" in msg
    assert installs.minimal_calls == 0


def test_an_unsupported_host_is_a_clean_skip_never_a_warning(tmp_path, monkeypatch, installs):
    """The REAL manifest, and a real absent row: `macos-x86_64` -- ADR
    0021's own named case, and the general manifest-driven mechanism behind
    it."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, REAL_TOOLCHAINS_MANIFEST)
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "darwin")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")

    log, _ = _run_phase(tmp_path, sdk_root)

    assert log.warnings == []  # "never a failure" -- ADR 0021's own words
    assert log.blocking() == []
    assert installs.minimal_calls == 0


def test_windows_with_no_7zip_refuses_before_downloading(tmp_path, monkeypatch, installs):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest(host="windows-x86_64"))
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "win32")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(fetch, "_find_seven_zip", lambda: None)
    monkeypatch.setattr(bootstrap_cmd, "on_path", lambda _name: None)

    log, _ = _run_phase(tmp_path, sdk_root, is_windows=True)

    assert log.blocking() == ["toolchain-install"]
    assert "7-zip" in log.warnings[0][1].lower()
    assert installs.minimal_calls == 0


def test_a_manifest_with_no_minimal_sdk_row_refuses_by_name(tmp_path, monkeypatch, installs):
    """tan-cli#1556 review: the schema does not require a `minimal-sdk` row,
    and west used to find the bundle by itself. tan now needs the pin."""
    _point_home_at(monkeypatch, tmp_path)
    doc = json.loads(_small_manifest())
    doc["zephyrSdk"]["artifacts"] = [
        a for a in doc["zephyrSdk"]["artifacts"] if a["component"] != "minimal-sdk"
    ]
    sdk_root = _make_sdk_with_toolchains(tmp_path, json.dumps(doc))
    _linux(monkeypatch)

    log, _ = _run_phase(tmp_path, sdk_root)

    assert log.blocking() == ["toolchain-install"]
    assert "minimal-sdk" in log.warnings[0][1]
    assert installs.minimal_calls == 0


def test_a_transport_failure_is_retried_then_reported_as_unverified(
    tmp_path, monkeypatch, installs
):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    _linux(monkeypatch)
    monkeypatch.setattr(bootstrap_cmd.time, "sleep", lambda _seconds: None)
    installs.minimal_outcomes = [fetch.FetchOutcome("unverified", "reset by peer")] * 3

    log, _ = _run_phase(tmp_path, sdk_root)

    # Retried the documented number of times -- not zero, not unbounded.
    assert installs.minimal_calls == 3  # literal, not TOOLCHAIN_INSTALL_ATTEMPTS
    assert log.blocking() == ["toolchain-pin-unverified"]
    msg = log.warnings[0][1]
    assert "reset by peer" in msg and "x.tar.xz" in msg
    assert installs.toolchain_calls == 0


def test_a_transient_failure_followed_by_a_success_still_verifies_and_stamps(
    tmp_path, monkeypatch, installs
):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    _linux(monkeypatch)
    monkeypatch.setattr(bootstrap_cmd.time, "sleep", lambda _seconds: None)
    installs.minimal_outcomes = [fetch.FetchOutcome("unverified", "timed out")] * 2

    log, _ = _run_phase(tmp_path, sdk_root)

    assert installs.minimal_calls == 3
    assert log.blocking() == []
    manifest, _ = bootstrap_cmd.load_toolchain_manifest(sdk_root)
    assert tp.stamp_matches_pin(bootstrap_cmd._read_toolchain_stamp(_store_dir(tmp_path)), manifest)


def test_a_missing_release_asset_is_not_retried_and_does_not_blame_the_proxy(
    tmp_path, monkeypatch, installs
):
    """tan-cli#1556 review: a 404 used to read as a transport failure ("check
    ALL_PROXY") after three retries; it means the pin names an asset upstream
    does not serve, which no retry or proxy change fixes."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    _linux(monkeypatch)
    monkeypatch.setattr(bootstrap_cmd.time, "sleep", lambda _seconds: None)
    installs.minimal_outcomes = [fetch.FetchOutcome("missing", "HTTP 404 Not Found for u")]

    log, _ = _run_phase(tmp_path, sdk_root)

    assert installs.minimal_calls == 1
    assert log.blocking() == ["toolchain-install"]
    msg = log.warnings[0][1]
    assert "HTTP 404" in msg and "Re-running will not help" in msg
    assert "ALL_PROXY" not in msg


def test_a_hash_mismatch_is_never_retried(tmp_path, monkeypatch, installs):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    _linux(monkeypatch)
    monkeypatch.setattr(bootstrap_cmd.time, "sleep", lambda _seconds: None)
    installs.minimal_outcomes = [fetch.FetchOutcome("mismatch", "x.tar.xz has sha256 cc")]

    log, _ = _run_phase(tmp_path, sdk_root)

    assert installs.minimal_calls == 1
    assert log.blocking() == ["toolchain-pin-mismatch"]
    assert not _store_dir(tmp_path).exists()


def test_an_extraction_failure_on_a_critically_low_volume_gets_the_disk_note(
    tmp_path, monkeypatch, installs
):
    """The low-disk note used to ride only on `west sdk install`'s failure
    path; tan's own extraction failure is where it belongs now."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    _linux(monkeypatch)
    monkeypatch.setattr(bootstrap_cmd, "_free_disk_bytes", lambda _root: 10 * (1 << 20))
    installs.minimal_outcomes = [fetch.FetchOutcome("install", "extracting x.tar.xz failed")]

    log, _ = _run_phase(tmp_path, sdk_root)

    assert log.blocking() == ["toolchain-install"]
    msg = log.warnings[0][1]
    assert "0.01 GiB" in msg
    assert "low disk space" in msg


def test_a_toolchain_failure_after_the_bundle_installed_is_reported_and_not_stamped(
    tmp_path, monkeypatch, installs
):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    _linux(monkeypatch)
    installs.toolchain_outcomes = [fetch.FetchOutcome("mismatch", "y.tar.xz has sha256 dd")]

    log, _ = _run_phase(tmp_path, sdk_root)

    assert log.blocking() == ["toolchain-pin-mismatch"]
    assert not _store_dir(tmp_path).exists()


# ---------------------------------------------------------------------------
# Interrupted/partial installs must never look present
# ---------------------------------------------------------------------------


def test_a_version_mismatch_after_install_is_never_trusted_or_stamped(
    tmp_path, monkeypatch, installs
):
    """The installed tree names a DIFFERENT version than the pin -- a hash
    match on the archive is not the predicate; only the re-read fact is."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest(version="1.0.1"))
    _linux(monkeypatch)
    installs.version = "0.16.8"

    log, _ = _run_phase(tmp_path, sdk_root)

    assert log.blocking() == ["toolchain-install"]
    assert "Remove that directory" not in log.warnings[0][1]
    assert not _store_dir(tmp_path).exists()  # never moved into place


def test_a_compiler_that_does_not_run_is_never_stamped(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    _linux(monkeypatch)
    monkeypatch.setattr(bootstrap_cmd, "probe_status", lambda argv, *a, **kw: (False, None))

    log, _ = _run_phase(tmp_path, sdk_root)

    assert log.blocking() == ["toolchain-install"]
    # tan-cli#990 review MINOR: the probe runs BEFORE the move (on `tmp_dir`),
    # so a failed probe never reaches `store_dir` and stays auto-reclaimable.
    assert not _store_dir(tmp_path).exists()


def test_a_failed_probe_under_an_adopted_root_still_lets_a_retry_recover(tmp_path, monkeypatch):
    """tan-cli#990 review MINOR: probing `tmp_dir` first means a failed probe
    never creates `store_dir`, so a second attempt under the same adopted
    `$ALP_TOOLCHAIN_ROOT` can still succeed with no manual cleanup."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    adopted_root = tmp_path / "bench-cache"
    monkeypatch.setenv("ALP_TOOLCHAIN_ROOT", str(adopted_root))
    _linux(monkeypatch)
    monkeypatch.setattr(bootstrap_cmd, "probe_status", lambda argv, *a, **kw: (False, None))

    log, _ = _run_phase(tmp_path, sdk_root)

    manifest, _ = bootstrap_cmd.load_toolchain_manifest(sdk_root)
    store_dir = adopted_root / tp.store_dir_name(manifest.version)
    assert log.blocking() == ["toolchain-install"]
    assert "did not run" in log.warnings[0][1]
    assert not store_dir.exists()

    monkeypatch.setattr(
        bootstrap_cmd, "probe_status",
        lambda argv, *a, **kw: (True, "arm-zephyr-eabi-gcc (Zephyr SDK) 14.3.0\n"),
    )
    second_log, _ = _run_phase(tmp_path, sdk_root)
    assert second_log.blocking() == []
    assert tp.stamp_matches_pin(bootstrap_cmd._read_toolchain_stamp(store_dir), manifest) is True


def test_wreckage_from_a_killed_prior_attempt_is_reclaimed_before_a_new_one_starts(
    tmp_path, monkeypatch
):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    root = tmp_path / "home" / ".alp" / "toolchains"
    root.mkdir(parents=True)
    wreckage = root / f"{tp.store_dir_name('1.0.1')}{tp.TMP_SUFFIX_PREFIX}99999"
    (wreckage / "partial").mkdir(parents=True)
    (wreckage / "partial" / "junk.bin").write_bytes(b"\x00" * 32)
    _linux(monkeypatch)

    log, _ = _run_phase(tmp_path, sdk_root)

    assert log.blocking() == []
    assert not wreckage.exists()


def test_a_netrc_an_older_tan_left_behind_is_swept(tmp_path, monkeypatch):
    """Before tan-cli#1556 tan staged a private netrc for `west sdk install`;
    one a crash left behind holds a secret, so the phase still sweeps it."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    root = tmp_path / "home" / ".alp" / "toolchains"
    old = root / f"{tp.NETRC_SCRATCH_PREFIX}abc123"
    old.mkdir(parents=True)
    (old / "netrc").write_text("machine api.github.com login x password y\n", encoding="utf-8")
    _linux(monkeypatch)

    _run_phase(tmp_path, sdk_root)

    assert not old.exists()


# ---------------------------------------------------------------------------
# $ALP_TOOLCHAIN_ROOT -- the adopted-root exception
# ---------------------------------------------------------------------------


def test_an_unstamped_directory_under_an_adopted_root_is_never_deleted(
    tmp_path, monkeypatch, installs
):
    _point_home_at(monkeypatch, tmp_path)
    adopted_root = tmp_path / "bench-cache"
    adopted_root.mkdir()
    monkeypatch.setenv("ALP_TOOLCHAIN_ROOT", str(adopted_root))
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    preexisting = adopted_root / tp.store_dir_name("1.0.1")
    preexisting.mkdir()
    marker = preexisting / "not-tans.txt"
    marker.write_text("customer-owned cache content", encoding="utf-8")
    _linux(monkeypatch)

    log, _ = _run_phase(tmp_path, sdk_root)

    assert log.blocking() == ["toolchain-install"]
    assert marker.read_text(encoding="utf-8") == "customer-owned cache content"  # untouched


def test_an_unstamped_dir_under_an_adopted_root_refuses_before_any_download(
    tmp_path, monkeypatch, installs
):
    """tan-cli#1498: the adopted-root refusal used to fire only AFTER the
    multi-minute download, with a message blaming the stamp."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    adopted_root = tmp_path / "bench-cache"
    monkeypatch.setenv("ALP_TOOLCHAIN_ROOT", str(adopted_root))
    _linux(monkeypatch)
    manifest, _ = bootstrap_cmd.load_toolchain_manifest(sdk_root)
    store_dir = adopted_root / tp.store_dir_name(manifest.version)
    store_dir.mkdir(parents=True)
    (store_dir / "keep.txt").write_text("mine", encoding="utf-8")

    log, _ = _run_phase(tmp_path, sdk_root)

    assert installs.minimal_calls == 0, "nothing may be downloaded"
    assert log.blocking() == ["toolchain-install"]
    message = log.warnings[0][1]
    assert "nothing was downloaded" in message
    assert "ALP_TOOLCHAIN_ROOT" in message
    assert (store_dir / "keep.txt").read_text(encoding="utf-8") == "mine"


# ---------------------------------------------------------------------------
# `--dry-run` honesty
# ---------------------------------------------------------------------------


def test_dry_run_plans_every_download_and_writes_nothing(tmp_path, monkeypatch, installs):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    _linux(monkeypatch)

    def refuse_to_sleep(_seconds):
        raise AssertionError("a dry run must never sleep between retries")

    monkeypatch.setattr(bootstrap_cmd.time, "sleep", refuse_to_sleep)

    log, runner = _run_phase(tmp_path, sdk_root, dry_run=True)

    assert log.blocking() == []
    planned = [" ".join(argv) for argv in runner.planned]
    assert any("sha256.sum" in line for line in planned)
    assert any("x.tar.xz" in line and "a" * 64 in line for line in planned)
    assert any("y.tar.xz" in line and "b" * 64 in line for line in planned)
    assert not any("west" in line for line in planned)
    assert installs.minimal_calls == 0
    assert not (tmp_path / "home" / ".alp" / "toolchains").exists()
