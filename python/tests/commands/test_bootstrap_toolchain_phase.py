# SPDX-License-Identifier: Apache-2.0
"""`tan.commands.bootstrap_cmd.toolchain_phase` -- ADR 0021 Lane 1 P1
(issue #474), the IO layer around `tan.core.toolchain_provision`'s pure
decisions.

Hermetic throughout: `west sdk install` is never really spawned. Every case
here monkeypatches `bootstrap_cmd.Runner.run` (the SAME seam
`test_bootstrap_command.py`'s own pip-phase tests use) to a fake that either
simulates what a real `west sdk install --install-dir <dir>` would have
written to disk, or reports a failure string -- so this file exercises the
decision logic, the refusals and the post-install verification step against
a STUBBED fetch, never a real download. `_probe_toolchain_compiler`'s own
subprocess spawn is stubbed too (`bootstrap_cmd.probe`), so no test depends
on a real `arm-zephyr-eabi-gcc` existing on the runner, on any platform.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tan.commands import bootstrap_cmd
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


def _fake_west_sdk_install_writes(install_dir_arg_index: int, *, version="1.0.1"):
    """A `Runner.run` stand-in that, on seeing a `west sdk install
    --install-dir <X>` argv, writes the exact files a REAL run would have
    left there (`sdk_version`, the compiler binary path) and returns success
    -- the "stubbed fetch" the task calls for, never a real network call."""

    def fake_run(self, argv, cwd=None, extra_env=None):  # noqa: ARG001
        if "install" in argv and "sdk" in argv:
            install_dir = Path(argv[install_dir_arg_index])
            (install_dir / "gnu" / "arm-zephyr-eabi" / "bin").mkdir(parents=True, exist_ok=True)
            (install_dir / "gnu" / "arm-zephyr-eabi" / "bin" / "arm-zephyr-eabi-gcc").write_text(
                "#!/bin/sh\necho stub\n", encoding="utf-8"
            )
            (install_dir / "sdk_version").write_text(f"{version}\n", encoding="utf-8")
        return None

    return fake_run


@pytest.fixture(autouse=True)
def _stub_compiler_probe(monkeypatch):
    """Every test in this file: `arm-zephyr-eabi-gcc --version` "succeeds"
    without a real subprocess spawn -- portable to every CI shard, and keeps
    this suite testing tan's OWN verification sequencing, not a real GCC
    build's banner text."""
    monkeypatch.setattr(bootstrap_cmd, "probe", lambda argv, *a, **kw: (True, "arm-zephyr-eabi-gcc (Zephyr SDK 1.0.1) 14.3.0\n"))


def _argv_index_of_install_dir(argv_from_west_sdk_install_argv) -> int:
    return argv_from_west_sdk_install_argv.index("--install-dir") + 1


def _install_dir_index() -> int:
    sample = tp.west_sdk_install_argv("west", version="1.0.1", install_dir="/PLACEHOLDER")
    return _argv_index_of_install_dir(sample)


# ---------------------------------------------------------------------------
# The happy path + the skip
# ---------------------------------------------------------------------------


def test_a_clean_install_is_verified_and_stamped(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.Runner, "run", _fake_west_sdk_install_writes(_install_dir_index()))
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")

    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    runner = bootstrap_cmd.Runner(json=True)
    bootstrap_cmd.toolchain_phase(ws, log, runner, sdk_root, None, is_windows=False)

    assert log.blocking() == []  # never blocked -- a clean install
    manifest, _ = bootstrap_cmd.load_toolchain_manifest(sdk_root)
    store_dir = tmp_path / "home" / ".alp" / "toolchains" / tp.store_dir_name(manifest.version)
    stamp = bootstrap_cmd._read_toolchain_stamp(store_dir)
    assert tp.stamp_matches_pin(stamp, manifest) is True
    assert (store_dir / "gnu" / "arm-zephyr-eabi" / "bin" / "arm-zephyr-eabi-gcc").is_file()


def test_a_second_bootstrap_skips_without_spawning_west_again(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.Runner, "run", _fake_west_sdk_install_writes(_install_dir_index()))
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")

    ws = _workspace(tmp_path)
    first_log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(
        ws, first_log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False
    )
    assert first_log.blocking() == []

    def refuse_to_run_again(self, argv, cwd=None, extra_env=None):  # noqa: ARG001
        raise AssertionError(f"west sdk install must not be spawned again: {argv}")

    monkeypatch.setattr(bootstrap_cmd.Runner, "run", refuse_to_run_again)
    second_log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(
        ws, second_log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False
    )
    assert second_log.blocking() == []
    assert second_log.warnings == []


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_missing_manifest_blocks_with_a_named_reason(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    sdk = tmp_path / "ws" / "alp-sdk"
    (sdk / "metadata").mkdir(parents=True)  # no toolchains.json written

    def refuse_any_spawn(self, argv, cwd=None, extra_env=None):  # noqa: ARG001
        raise AssertionError("must refuse before spawning anything")

    monkeypatch.setattr(bootstrap_cmd.Runner, "run", refuse_any_spawn)
    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(ws, log, bootstrap_cmd.Runner(json=True), str(sdk), None, is_windows=False)

    assert log.blocking() == ["toolchain-install"]
    assert "toolchains.json" in log.warnings[0][1]


def test_insufficient_disk_refuses_with_both_numbers_named_and_never_spawns_west(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    huge = 999 * (1 << 40)  # 999 TiB -- no real CI runner has this free
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest(extracted=huge))
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")

    def refuse_any_spawn(self, argv, cwd=None, extra_env=None):  # noqa: ARG001
        raise AssertionError("must refuse before spawning `west sdk install`")

    monkeypatch.setattr(bootstrap_cmd.Runner, "run", refuse_any_spawn)
    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False)

    assert log.blocking() == ["toolchain-install"]
    msg = log.warnings[0][1]
    assert "GiB" in msg
    assert "--no-toolchain" in msg


def test_an_unsupported_host_is_a_clean_skip_never_a_warning(tmp_path, monkeypatch):
    """The REAL manifest, and a real absent row: `macos-x86_64` -- ADR
    0021's own named case, and the general manifest-driven mechanism behind
    it."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, REAL_TOOLCHAINS_MANIFEST)
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "darwin")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")

    def refuse_any_spawn(self, argv, cwd=None, extra_env=None):  # noqa: ARG001
        raise AssertionError("an unsupported host must never spawn `west`")

    monkeypatch.setattr(bootstrap_cmd.Runner, "run", refuse_any_spawn)
    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False)

    assert log.warnings == []  # "never a failure" -- ADR 0021's own words
    assert log.blocking() == []


def test_windows_with_no_7zip_refuses_before_spawning_west(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest(host="windows-x86_64"))
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "win32")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(bootstrap_cmd, "on_path", lambda _name: None)

    def refuse_any_spawn(self, argv, cwd=None, extra_env=None):  # noqa: ARG001
        raise AssertionError("must gate on 7-Zip before spawning `west sdk install`")

    monkeypatch.setattr(bootstrap_cmd.Runner, "run", refuse_any_spawn)
    ws = _workspace(tmp_path, is_windows=True)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=True)

    assert log.blocking() == ["toolchain-install"]
    assert "7-Zip" in log.warnings[0][1] or "7-zip" in log.warnings[0][1].lower()


def test_a_west_sdk_install_failure_is_retried_then_reported_with_the_checksum_hint(
    tmp_path, monkeypatch
):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap_cmd.time, "sleep", lambda _seconds: None)

    calls = {"n": 0}

    def fake_fail(self, argv, cwd=None, extra_env=None):  # noqa: ARG001
        calls["n"] += 1
        return "sha256 mismatched: aaaa:bbbb"

    monkeypatch.setattr(bootstrap_cmd.Runner, "run", fake_fail)
    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False)

    # Retried the documented number of times -- not zero (no retry at all)
    # and not unbounded (a permanent failure must still give up).
    assert calls["n"] == 3  # literal, not bootstrap_cmd.TOOLCHAIN_INSTALL_ATTEMPTS -- a test that reads the constant it is pinning proves nothing against a mutant that changes it
    assert log.blocking() == ["toolchain-install"]
    msg = log.warnings[0][1]
    assert "sha256 mismatched" in msg
    assert "proxy" in msg.lower()


def test_a_transient_failure_followed_by_a_success_still_verifies_and_stamps(tmp_path, monkeypatch):
    """The exact shape `getting-started.yml`'s own retry loop exists for:
    the FIRST `west sdk install` attempt fails (a dropped connection, a rate
    limit, a flaky `tar --xz` extraction), and a LATER attempt succeeds --
    the phase must not give up on the first failure, and must still run its
    full version/compiler verification on whichever attempt landed."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap_cmd.time, "sleep", lambda _seconds: None)

    real_fake = _fake_west_sdk_install_writes(_install_dir_index())
    calls = {"n": 0}

    def flaky(self, argv, cwd=None, extra_env=None):
        calls["n"] += 1
        if calls["n"] < 3:  # literal -- see the assertion below
            return "west: fetch_releases API rate limit exceeded"
        return real_fake(self, argv, cwd=cwd, extra_env=extra_env)

    monkeypatch.setattr(bootstrap_cmd.Runner, "run", flaky)
    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False)

    assert calls["n"] == 3  # literal, not bootstrap_cmd.TOOLCHAIN_INSTALL_ATTEMPTS -- a test that reads the constant it is pinning proves nothing against a mutant that changes it
    assert log.blocking() == []
    manifest, _ = bootstrap_cmd.load_toolchain_manifest(sdk_root)
    store_dir = tmp_path / "home" / ".alp" / "toolchains" / tp.store_dir_name(manifest.version)
    stamp = bootstrap_cmd._read_toolchain_stamp(store_dir)
    assert tp.stamp_matches_pin(stamp, manifest) is True


def test_dry_run_never_retries_one_planned_command_only(tmp_path, monkeypatch):
    """`Runner.run` returns success-shaped `None` immediately under
    `--dry-run` without spawning -- the retry loop must not mistake that for
    3 successful attempts and must not sleep."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")

    def refuse_to_sleep(_seconds):
        raise AssertionError("a dry run must never sleep between retries")

    monkeypatch.setattr(bootstrap_cmd.time, "sleep", refuse_to_sleep)
    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    runner = bootstrap_cmd.Runner(json=True, dry_run=True)
    bootstrap_cmd.toolchain_phase(ws, log, runner, sdk_root, None, is_windows=False)

    assert log.blocking() == []
    install_argvs = [argv for argv in runner.planned if "install" in argv and "sdk" in argv]
    assert len(install_argvs) == 1


# ---------------------------------------------------------------------------
# Interrupted/partial installs must never look present
# ---------------------------------------------------------------------------


def test_a_version_mismatch_after_install_is_never_trusted_or_stamped(tmp_path, monkeypatch):
    """`west sdk install` exits 0 but the installed tree names a DIFFERENT
    version than the pin tan asked for -- directory-exists (and even
    'west said success') is never the predicate; only the re-read fact is.
    """
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest(version="1.0.1"))
    monkeypatch.setattr(
        bootstrap_cmd.Runner, "run",
        _fake_west_sdk_install_writes(_install_dir_index(), version="0.16.8"),
    )
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")

    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False)

    assert log.blocking() == ["toolchain-install"]
    manifest, _ = bootstrap_cmd.load_toolchain_manifest(sdk_root)
    store_dir = tmp_path / "home" / ".alp" / "toolchains" / tp.store_dir_name(manifest.version)
    assert not store_dir.exists()  # never moved into place


def test_a_compiler_that_does_not_run_is_never_stamped(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.Runner, "run", _fake_west_sdk_install_writes(_install_dir_index()))
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap_cmd, "probe", lambda argv, *a, **kw: (False, None))

    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False)

    assert log.blocking() == ["toolchain-install"]
    manifest, _ = bootstrap_cmd.load_toolchain_manifest(sdk_root)
    store_dir = tmp_path / "home" / ".alp" / "toolchains" / tp.store_dir_name(manifest.version)
    # Moved into place (version matched), but NOT stamped -- so a later
    # `stamp_matches_pin` check (doctor, or this same phase next run) still
    # reports it unverified rather than trusting the move alone.
    assert not (store_dir / tp.STAMP_FILENAME).exists()


def test_wreckage_from_a_killed_prior_attempt_is_reclaimed_before_a_new_one_starts(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    root = tmp_path / "home" / ".alp" / "toolchains"
    root.mkdir(parents=True)
    leaf = tp.store_dir_name("1.0.1")
    wreckage = root / f"{leaf}{tp.TMP_SUFFIX_PREFIX}99999"
    (wreckage / "partial").mkdir(parents=True)
    (wreckage / "partial" / "junk.bin").write_bytes(b"\x00" * 32)

    monkeypatch.setattr(bootstrap_cmd.Runner, "run", _fake_west_sdk_install_writes(_install_dir_index()))
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")

    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False)

    assert log.blocking() == []
    assert not wreckage.exists()


# ---------------------------------------------------------------------------
# $ALP_TOOLCHAIN_ROOT -- the adopted-root exception
# ---------------------------------------------------------------------------


def test_an_unstamped_directory_under_an_adopted_root_is_never_deleted(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    adopted_root = tmp_path / "bench-cache"
    adopted_root.mkdir()
    monkeypatch.setenv("ALP_TOOLCHAIN_ROOT", str(adopted_root))
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    leaf = tp.store_dir_name("1.0.1")
    preexisting = adopted_root / leaf
    preexisting.mkdir()
    marker = preexisting / "not-tans.txt"
    marker.write_text("customer-owned cache content", encoding="utf-8")

    monkeypatch.setattr(bootstrap_cmd.Runner, "run", _fake_west_sdk_install_writes(_install_dir_index()))
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")

    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False)

    assert log.blocking() == ["toolchain-install"]
    assert marker.read_text(encoding="utf-8") == "customer-owned cache content"  # untouched


# ---------------------------------------------------------------------------
# `--dry-run` honesty
# ---------------------------------------------------------------------------


def test_dry_run_plans_the_argv_and_writes_nothing(tmp_path, monkeypatch):
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")

    # The REAL `Runner.run` (no monkeypatch): its own `dry_run` branch is what
    # this test proves never spawns a child process at all.
    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    runner = bootstrap_cmd.Runner(json=True, dry_run=True)
    bootstrap_cmd.toolchain_phase(ws, log, runner, sdk_root, None, is_windows=False)

    assert log.blocking() == []
    assert any("sdk" in argv and "install" in argv for argv in runner.planned)
    root = tmp_path / "home" / ".alp" / "toolchains"
    assert not root.exists()  # nothing created on disk under --dry-run
