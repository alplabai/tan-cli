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
subprocess spawn is stubbed too (`bootstrap_cmd.probe_status`), so no test
depends on a real `arm-zephyr-eabi-gcc` existing on the runner, on any
platform.
"""
from __future__ import annotations

import json
import os
import time
from netrc import netrc
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

    def fake_run(self, argv, cwd=None, extra_env=None, tail_lines=4):  # noqa: ARG001
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
    monkeypatch.setattr(bootstrap_cmd, "probe_status", lambda argv, *a, **kw: (True, "arm-zephyr-eabi-gcc (Zephyr SDK 1.0.1) 14.3.0\n"))


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

    def refuse_to_run_again(self, argv, cwd=None, extra_env=None, tail_lines=4):  # noqa: ARG001
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

    def refuse_any_spawn(self, argv, cwd=None, extra_env=None, tail_lines=4):  # noqa: ARG001
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

    def refuse_any_spawn(self, argv, cwd=None, extra_env=None, tail_lines=4):  # noqa: ARG001
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

    def refuse_any_spawn(self, argv, cwd=None, extra_env=None, tail_lines=4):  # noqa: ARG001
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

    def refuse_any_spawn(self, argv, cwd=None, extra_env=None, tail_lines=4):  # noqa: ARG001
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

    def fake_fail(self, argv, cwd=None, extra_env=None, tail_lines=4):  # noqa: ARG001
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


def test_a_permanent_west_sdk_install_failure_names_the_manual_remedy_command(
    tmp_path, monkeypatch
):
    """tan-cli#990 review MAJOR: this is the phase's MOST LIKELY failure in
    practice (the real first CI run of this phase hit exactly it, after all
    3 retries), and it was the ONE refusal in this function that named no
    remedy at all -- `Log.warn` has no `fix` channel, so unlike doctor's
    `Check.fix`, the command has to be IN the message text, the same way
    the 7-Zip/disk/adopted-root refusals already do it."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest(version="1.0.1"))
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap_cmd.time, "sleep", lambda _seconds: None)

    def fake_fail(self, argv, cwd=None, extra_env=None, tail_lines=4):  # noqa: ARG001
        return "tar: Unexpected EOF"

    monkeypatch.setattr(bootstrap_cmd.Runner, "run", fake_fail)
    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False)

    assert log.blocking() == ["toolchain-install"]
    msg = log.warnings[0][1]
    # The exact command a customer can copy-paste, naming THIS run's actual
    # resolved pin (never a possibly-stale `doctor_cmd` constant).
    assert "west sdk install --version 1.0.1 -t arm-zephyr-eabi" in msg
    assert "tan bootstrap" in msg


def test_a_west_sdk_install_call_asks_for_the_wide_capture_tail(tmp_path, monkeypatch):
    """tan-cli#990 review: the real first CI run of this phase produced a
    failure message naming NO cause -- `capture_tail`'s 4-line default
    discarded the actual `tar`/`xz` error line above the closing frames of
    `west`'s own traceback. `_run_west_sdk_install_with_retries` must ask
    `Runner.run` for `TOOLCHAIN_INSTALL_TAIL_LINES`, not the 4-line default,
    on every attempt -- recorded here directly rather than inferred from a
    message string, so a regression to the old default fails on this
    assertion, not on a fragile substring match."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap_cmd.time, "sleep", lambda _seconds: None)

    seen_tail_lines: list[int] = []

    def fake_fail(self, argv, cwd=None, extra_env=None, tail_lines=4):  # noqa: ARG001
        seen_tail_lines.append(tail_lines)
        return "tar: Unexpected EOF"

    monkeypatch.setattr(bootstrap_cmd.Runner, "run", fake_fail)
    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False)

    assert seen_tail_lines == [bootstrap_cmd.TOOLCHAIN_INSTALL_TAIL_LINES] * 3
    assert bootstrap_cmd.TOOLCHAIN_INSTALL_TAIL_LINES > 4


def test_a_west_sdk_install_failure_on_a_critically_low_volume_gets_the_disk_note(
    tmp_path, monkeypatch
):
    """`getting-started.yml`'s real first CI run of this phase hit a `tar
    --xz` extraction failure whose message named no cause at all (`tan.core.
    bootstrap.capture_tail` keeps only the last 4 lines) -- this is the
    diagnostic that failure needed: a low-disk note appended when the volume
    is critically low AT FAILURE TIME, independent of whether the preflight
    (checked once, before anything was written) passed."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap_cmd.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(bootstrap_cmd, "_free_disk_bytes", lambda _root: 10 * (1 << 20))

    def fake_fail(self, argv, cwd=None, extra_env=None, tail_lines=4):  # noqa: ARG001
        return "tar: Unexpected EOF"

    monkeypatch.setattr(bootstrap_cmd.Runner, "run", fake_fail)
    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False)

    assert log.blocking() == ["toolchain-install"]
    msg = log.warnings[0][1]
    assert "0.01 GiB" in msg
    assert "low disk space" in msg


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

    def flaky(self, argv, cwd=None, extra_env=None, tail_lines=4):
        calls["n"] += 1
        if calls["n"] < 3:  # literal -- see the assertion below
            return "west: fetch_releases API rate limit exceeded"
        return real_fake(self, argv, cwd=cwd, extra_env=extra_env, tail_lines=tail_lines)

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
    monkeypatch.setattr(bootstrap_cmd, "probe_status", lambda argv, *a, **kw: (False, None))

    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False)

    assert log.blocking() == ["toolchain-install"]
    manifest, _ = bootstrap_cmd.load_toolchain_manifest(sdk_root)
    store_dir = tmp_path / "home" / ".alp" / "toolchains" / tp.store_dir_name(manifest.version)
    # tan-cli#990 review MINOR, fixed: the probe now runs BEFORE the move
    # (on `tmp_dir`), so a failed probe never even reaches `store_dir` --
    # not "moved but unstamped". This matters beyond the stamp file: under
    # an adopted `$ALP_TOOLCHAIN_ROOT`, an unstamped `store_dir` that DOES
    # exist would permanently block every later retry (see the dedicated
    # `test_a_failed_probe_under_an_adopted_root_still_lets_a_retry_recover`
    # below); leaving the failure at `tmp_dir` keeps it auto-reclaimable.
    assert not store_dir.exists()
    assert not (store_dir / tp.STAMP_FILENAME).exists()


def test_a_failed_probe_under_an_adopted_root_still_lets_a_retry_recover(tmp_path, monkeypatch):
    """tan-cli#990 review MINOR: before the probe-before-move fix, a failed
    compiler probe left an UNSTAMPED `store_dir` in place, and under an
    ADOPTED `$ALP_TOOLCHAIN_ROOT` the very next line's own guard then
    refused to touch that path forever (`root_adopted` -> warn, never
    `rmtree`) -- a dead end only a manual `rm -rf` could clear. Probing
    `tmp_dir` first means a failed probe never creates `store_dir` at all,
    so a SECOND `tan bootstrap` attempt (this time with a working compiler)
    can still succeed under the same adopted root, no manual cleanup
    needed."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    adopted_root = tmp_path / "bench-cache"
    monkeypatch.setenv("ALP_TOOLCHAIN_ROOT", str(adopted_root))
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap_cmd, "probe_status", lambda argv, *a, **kw: (False, None))

    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    monkeypatch.setattr(bootstrap_cmd.Runner, "run", _fake_west_sdk_install_writes(_install_dir_index()))
    bootstrap_cmd.toolchain_phase(ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False)

    manifest, _ = bootstrap_cmd.load_toolchain_manifest(sdk_root)
    store_dir = adopted_root / tp.store_dir_name(manifest.version)
    assert log.blocking() == ["toolchain-install"]
    assert "did not run" in log.warnings[0][1]
    assert not store_dir.exists()  # never even created -- nothing to unblock

    # A second attempt, this time with a working compiler, under the SAME
    # adopted root: must succeed, not hit the "already exists, adopted,
    # refusing to touch" refusal the OLD move-before-probe order would have
    # produced here.
    monkeypatch.setattr(bootstrap_cmd, "probe_status", lambda argv, *a, **kw: (True, "arm-zephyr-eabi-gcc (Zephyr SDK) 14.3.0\n"))
    second_log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(
        ws, second_log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False
    )
    assert second_log.blocking() == []
    stamp = bootstrap_cmd._read_toolchain_stamp(store_dir)
    assert tp.stamp_matches_pin(stamp, manifest) is True


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


# ---------------------------------------------------------------------------
# Authenticating the SDK download (tan-cli#1143)
# ---------------------------------------------------------------------------

#: A value no real credential could ever be, chosen so a single `in` test
#: over a whole rendered envelope is conclusive. Inside
#: `toolchain_provision._SDK_TOKEN_ALLOWED` on purpose -- a sentinel the
#: character guard would REJECT would make every leak assertion below pass
#: for the wrong reason.
SENTINEL_TOKEN = "ghp_TANCLI1143SENTINELdoNotLeakThisValue"

#: A SECOND sentinel, for the cases where one variable holds an unusable
#: value and another holds a usable one -- the two must be told apart, and
#: neither may reach an envelope.
GOOD_TOKEN = "ghp_TANCLI1143SECONDsentinelAlsoDoNotLeak"

#: What `west sdk install` really prints when GitHub's API quota is
#: exhausted -- the `self.inf(...)` line from Zephyr v4.4.1
#: `scripts/west_commands/sdk.py:270` followed by the exception
#: `fetch_releases` raises on the same response. Verbatim, including the
#: `--personal-access-token` advice this issue exists because `tan` cannot
#: honour.
WEST_RATE_LIMIT_TAIL = (
    "fetch_releases API rate limit exceeded. Try executing install script with "
    "--personal-access-token argument or use a .netrc file\n"
    "Exception: Failed to fetch: 403, {\"message\": \"API rate limit exceeded for "
    "40.65.56.224.\"}"
)


@pytest.fixture(autouse=True)
def _no_ambient_github_token(monkeypatch):
    """No test in this file may inherit the developer's (or the runner's) own
    `GH_TOKEN`. Without this, whether the phase stages a credential at all
    depends on who is running the suite -- and the cases that assert the
    UNauthenticated path would silently stop testing it on any machine where
    `gh auth login` has been run."""
    for name in tp.SDK_TOKEN_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _echoing_rate_limit_failure(seen: dict):
    """A `Runner.run` stand-in that fails the way a rate-limited `west sdk
    install` fails, and -- the load-bearing part -- builds its captured tail
    OUT OF THE ARGV IT WAS GIVEN.

    That is not decoration. `capture_tail` keeps the last non-empty lines of
    a failed child's output, and a child that dies printing its own command
    line is exactly how a secret passed as an argv element reaches an
    envelope. Echoing the argv here is what makes the "not in the tail"
    assertions below able to FAIL: with a plain constant tail they would
    pass against an implementation that appended
    `--personal-access-token <token>` to the argv.
    """

    def fake_run(self, argv, cwd=None, extra_env=None, tail_lines=4):  # noqa: ARG001
        # `self.planned.append(list(argv))` is the FIRST thing the real
        # `Runner.run` does, and `planned` is `data.plannedCommands`. Without
        # it here, monkeypatching `run` wholesale leaves `runner.planned`
        # EMPTY -- and the `for argv in runner.planned` leak assertion below
        # iterates zero times and cannot fail (tan-cli#1148 review: a
        # zero-iteration loop asserting a security property reads as
        # coverage and is worse than no test). The stand-in reproduces the
        # side effect, not just the return value.
        self.planned.append(list(argv))
        seen.setdefault("argv", []).append(list(argv))
        seen.setdefault("extra_env", []).append(dict(extra_env or {}))
        netrc_path = (extra_env or {}).get(tp.NETRC_ENV_VAR)
        if netrc_path is not None:
            seen["netrc_path"] = netrc_path
            seen["netrc_text"] = Path(netrc_path).read_text(encoding="utf-8")
            seen["netrc_mode"] = oct(Path(netrc_path).stat().st_mode & 0o777)
        return f"$ {' '.join(argv)}\n{WEST_RATE_LIMIT_TAIL}"

    return fake_run


def _run_toolchain_phase_to_failure(tmp_path, monkeypatch, seen):
    """`toolchain_phase` driven to its rate-limited-failure exit, with the
    retry sleep neutered. Returns the `(log, runner)` pair the assertions
    read."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap_cmd.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(bootstrap_cmd.Runner, "run", _echoing_rate_limit_failure(seen))
    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    runner = bootstrap_cmd.Runner(json=True)
    bootstrap_cmd.toolchain_phase(ws, log, runner, sdk_root, None, is_windows=False)
    return log, runner


def _rendered_envelope(log, runner, sdk_root: str) -> str:
    """The REAL `bootstrap` JSON envelope this run would have emitted --
    `bootstrap_cmd._data(..., planned=...)` and `log.take_issues(...)` fed to
    the same `Envelope` the command builds, then `to_json()`.

    Greping a hand-rolled dict of the pieces would prove less: this is the
    literal text a customer pastes into a bug report and the extension logs.
    """
    data = bootstrap_cmd._data(
        args={
            "no_pip": False, "no_west": False, "no_toolchain": False, "print_env": False,
        },
        sdk_root=sdk_root,
        paths=None,
        facts=fallback_facts((3, 12)),
        pin="",
        planned=runner.planned,
    )
    issues = log.take_issues(escalate_blocking=True)
    return bootstrap_cmd.Envelope(
        "bootstrap",
        bootstrap_cmd.Project(root=None, board_yaml=None),
        data,
        issues,
        bootstrap_cmd.ExitCode.RUNTIME_FAILURE,
    ).to_json()


def test_a_github_token_in_the_environment_reaches_west_without_touching_any_argv(
    tmp_path, monkeypatch
):
    """tan-cli#1143, the whole point: the token MUST reach the child (or the
    feature does nothing), and MUST NOT reach `planned` argv, the JSON
    envelope, the failure tail, or any logged line (or the feature is worse
    than nothing).

    Both halves are asserted here deliberately. A "the token did not leak"
    test that never proves the token was in play at all passes trivially
    against an implementation that drops the credential on the floor -- and
    then licenses exactly the belief this issue says must be earned.
    """
    seen: dict = {}
    monkeypatch.setenv("TAN_GITHUB_TOKEN", SENTINEL_TOKEN)
    log, runner = _run_toolchain_phase_to_failure(tmp_path, monkeypatch, seen)
    sdk_root = str(tmp_path / "ws" / "alp-sdk")

    # --- half one: the credential really reached the child ---------------
    assert seen["extra_env"], "west sdk install was never spawned"
    for env in seen["extra_env"]:
        assert tp.NETRC_ENV_VAR in env, "every attempt must carry the credential"
    assert SENTINEL_TOKEN in seen["netrc_text"]
    # Parsed by the SAME stdlib parser `requests.utils.get_netrc_auth` uses,
    # not by a substring match: a file the token is merely *inside* is not a
    # file the token is *readable* from. Re-materialised from the bytes the
    # child saw, because the original is already deleted by the time the
    # phase returns (the case below asserts exactly that).
    replayed = tmp_path / "captured-netrc"
    replayed.write_text(seen["netrc_text"], encoding="utf-8")
    parsed = netrc(str(replayed)).authenticators(tp.GITHUB_API_HOST)
    assert parsed is not None and parsed[2] == SENTINEL_TOKEN
    if os.name != "nt":  # Windows has no POSIX mode bits to assert on
        assert seen["netrc_mode"] == "0o600"

    # --- half two: none of the four named surfaces carries it ------------
    # ORDER MATTERS. Each surface is asserted before the one whose failure
    # would mask it, so a single mutation reds the assertion that actually
    # guards it: planned argv first (the `--dry-run` surface, which no child
    # output can reach), then the rendered envelope, then the logged
    # messages, then the captured tail.
    #
    # The count is the anti-vacuity guard: `planned` is populated by the
    # stand-in's own `self.planned.append`, so if that ever stops happening
    # the loop below would silently iterate zero times and prove nothing.
    #
    # Snapshotted first because `_rendered_envelope` calls `take_issues`,
    # which DRAINS the log -- reading `log.warnings` after it would be the
    # same vacuity in a new place (an empty string contains no sentinel).
    blocking = log.blocking()
    logged = " ".join(msg for _code, msg in log.warnings)
    assert logged, "the failure must have been recorded at all"

    assert len(runner.planned) == bootstrap_cmd.TOOLCHAIN_INSTALL_ATTEMPTS
    for argv in runner.planned:
        assert SENTINEL_TOKEN not in " ".join(argv)
    assert SENTINEL_TOKEN not in _rendered_envelope(log, runner, sdk_root)
    assert blocking == ["toolchain-install"]
    # The variable NAME is safe to print and is what makes the run
    # explicable; the value is not.
    assert SENTINEL_TOKEN not in logged
    assert "$TAN_GITHUB_TOKEN" in logged
    tails = " ".join(f"$ {' '.join(argv)}" for argv in seen["argv"])
    assert SENTINEL_TOKEN not in tails


def test_the_staged_credential_file_is_deleted_once_the_install_returns(tmp_path, monkeypatch):
    """A secret written to disk that outlives the command is a leak with a
    longer half-life than any of the four surfaces above. The cleanup is in a
    `finally`, so this holds on the FAILURE path -- the one that skips
    straight past the success-side code."""
    seen: dict = {}
    monkeypatch.setenv("GH_TOKEN", SENTINEL_TOKEN)
    _run_toolchain_phase_to_failure(tmp_path, monkeypatch, seen)

    staged = Path(seen["netrc_path"])
    assert not staged.exists()
    assert not staged.parent.exists()


def test_a_rate_limited_failure_names_tans_own_surface_not_wests_flag(tmp_path, monkeypatch):
    """The message a customer sees today ends with west's advice to pass
    `--personal-access-token`, a flag on a command they never typed and which
    `tan bootstrap` rejects. The augmented message must name the lever that
    actually exists for them."""
    seen: dict = {}
    log, _runner = _run_toolchain_phase_to_failure(tmp_path, monkeypatch, seen)

    assert log.blocking() == ["toolchain-install"]
    msg = log.warnings[0][1]
    # West's own text is kept verbatim -- the note is APPENDED, never a
    # replacement (`_augment_with_low_disk_note`'s established shape).
    assert "--personal-access-token argument or use a .netrc file" in msg
    assert "$TAN_GITHUB_TOKEN" in msg
    assert "anonymous per-IP API quota" in msg
    assert "that is `west`'s own flag" in msg


def test_an_unauthenticated_run_stages_nothing_and_spawns_the_same_argv(tmp_path, monkeypatch):
    """tan-cli#1143 acceptance: with no token in the environment the download
    path is byte-for-byte what it was -- no netrc, no `$NETRC`, no extra env
    of any kind on the `west sdk install` spawn."""
    seen: dict = {}
    _run_toolchain_phase_to_failure(tmp_path, monkeypatch, seen)

    assert "netrc_path" not in seen
    assert seen["extra_env"] == [{}, {}, {}]
    expected = tp.west_sdk_install_argv(
        "west", version="1.0.1", install_dir=seen["argv"][0][_install_dir_index()]
    )
    assert seen["argv"][0][1:] == expected[1:]


def test_a_dry_run_stages_no_credential_even_with_a_token_in_the_environment(
    tmp_path, monkeypatch
):
    """`--dry-run` spawns nothing, so there is nothing to authenticate -- and
    writing a secret to disk to plan a command that will not run is a cost
    with no benefit. It also keeps `data.plannedCommands` identical with and
    without a token bound, which is what
    `test_bootstrap_command.py::test_a_dry_run_never_puts_the_github_token_in_the_envelope`
    then greps end to end."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")
    monkeypatch.setenv("TAN_GITHUB_TOKEN", SENTINEL_TOKEN)
    staged: list[Path] = []
    monkeypatch.setattr(
        bootstrap_cmd,
        "_stage_sdk_credential",
        lambda token: staged.append(token) or pytest.fail("staged under --dry-run"),
    )

    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    runner = bootstrap_cmd.Runner(json=True, dry_run=True)
    bootstrap_cmd.toolchain_phase(ws, log, runner, sdk_root, None, is_windows=False)

    assert staged == []
    assert log.blocking() == []
    assert any("sdk" in argv and "install" in argv for argv in runner.planned)


def _age_out(directory: Path) -> None:
    """Backdate `directory` past `SDK_CREDENTIAL_LIVE_WINDOW_S`, so the sweep
    treats it as a crashed run's residue rather than a concurrent run's live
    credential. Real residue is minutes-to-days old by the time anything
    sweeps it; a fixture created microseconds ago is not."""
    old = time.time() - bootstrap_cmd.SDK_CREDENTIAL_LIVE_WINDOW_S - 60
    os.utime(directory, (old, old))


def test_a_concurrent_runs_live_credential_is_never_swept(tmp_path, monkeypatch):
    """tan-cli#1148 round 2, FIX 3. The extension shells `tan`, so two
    concurrent `tan bootstrap` runs are ordinary. Without a liveness bound
    the second run's sweep deletes the first run's IN-USE netrc, and the
    first run silently continues anonymous after having already printed that
    it was authenticating -- the exact silent-downgrade shape the rest of
    this work exists to remove.

    An age window, not a PID probe: the lifetime of a live staged credential
    is bounded by construction (`TOOLCHAIN_INSTALL_ATTEMPTS` timeouts plus
    backoff, since `Runner.run` kills the child at `INSTALL_TIMEOUT_S`),
    whereas a PID check is racy under reuse and differs across platforms.
    """
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap_cmd.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(bootstrap_cmd.Runner, "run", _echoing_rate_limit_failure({}))

    root = tmp_path / "home" / ".alp" / "toolchains"
    root.mkdir(parents=True)
    # Another `tan bootstrap`, mid-download, right now.
    live = root / f"{tp.NETRC_SCRATCH_PREFIX}concurrent"
    live.mkdir()
    (live / "netrc").write_text(tp.netrc_text(SENTINEL_TOKEN), encoding="utf-8")
    # And a genuine crash residue beside it, so this proves DISCRIMINATION
    # rather than a sweep that simply stopped working.
    stale = root / f"{tp.NETRC_SCRATCH_PREFIX}crashed"
    stale.mkdir()
    _age_out(stale)

    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    bootstrap_cmd.toolchain_phase(
        ws, log, bootstrap_cmd.Runner(json=True), sdk_root, None, is_windows=False
    )

    assert live.exists() and (live / "netrc").exists()
    assert not stale.exists()


def test_a_credential_with_a_future_mtime_is_kept_not_swept(tmp_path):
    """The backwards-clock-skew fail-safe, which round 3 of the tan-cli#1148
    review found DOCUMENTED but UNPINNED: wrapping the subtraction in `abs()`
    left the whole suite at 95 passed.

    A negative age must read as "too young to touch". Under `abs()` a
    directory dated a day into the future measures as a day OLD and is
    swept -- which is exactly the live credential of a concurrent run on a
    box whose clock just stepped backwards, the case the window exists for.
    """
    future = tmp_path / f"{tp.NETRC_SCRATCH_PREFIX}skewed"
    future.mkdir()
    ahead = time.time() + 86400
    os.utime(future, (ahead, ahead))

    assert bootstrap_cmd._sdk_credential_is_possibly_live(future, time.time())
    # ...and end to end, through the sweep itself.
    bootstrap_cmd._reclaim_sdk_credential_wreckage(tmp_path)
    assert future.exists()


def test_the_live_window_keeps_a_real_margin_over_the_worst_case_lifetime():
    """The other undriven mutant round 3 found: `2 * (` -> `1 * (` also left
    95 passed. The doubling is a rule, not a taste -- a bound computed from
    three constants a later PR may retune must not become exact, and mtime
    granularity, a suspended laptop and a slow network filesystem all push a
    live credential's observed age above the arithmetic worst case.

    The worst case is recomputed here from the constants rather than read
    back from the module, so this cannot pass by agreeing with itself.
    """
    attempts = bootstrap_cmd.TOOLCHAIN_INSTALL_ATTEMPTS
    backoff = sum(
        bootstrap_cmd.TOOLCHAIN_RETRY_BACKOFF_S * (attempt - 1)
        for attempt in range(1, attempts + 1)
    )
    worst_case = attempts * bootstrap_cmd.INSTALL_TIMEOUT_S + backoff
    assert worst_case == 10845  # 3 x 3600 + (15 + 30), stated in the constant's comment

    assert bootstrap_cmd.SDK_CREDENTIAL_LIVE_SAFETY_FACTOR >= 2
    assert bootstrap_cmd.SDK_CREDENTIAL_LIVE_WINDOW_S >= 2 * worst_case
    # And the dwell cost the docstring quotes to the customer is real.
    assert round(bootstrap_cmd.SDK_CREDENTIAL_LIVE_WINDOW_S / 3600, 2) == 6.03


def test_an_unreadable_credential_directory_is_left_alone(tmp_path):
    """Anything the sweep cannot measure counts as LIVE. Deleting a
    credential on a failed `stat` would be guessing, in the one direction
    that cannot be undone."""
    missing = tmp_path / "gone"
    assert bootstrap_cmd._sdk_credential_is_possibly_live(missing, time.time())


def test_a_crashed_runs_leftover_credential_is_reclaimed_by_the_next_run(tmp_path, monkeypatch):
    """tan-cli#1148 review FIX 1. `_discard_sdk_credential` runs in a
    `finally`, which cannot run on SIGKILL, an OOM kill or power loss -- and
    a SIGKILLed `pytest -n 8` run left two of these behind during that
    review. Without a sweep a credential accumulates one copy per crash,
    forever.

    The sweep sits ABOVE the already-installed early return, which is the
    branch a bootstrapped machine takes every time: this case proves it by
    stamping the store so the phase returns without installing anything, and
    still expecting the residue gone.
    """
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")

    root = tmp_path / "home" / ".alp" / "toolchains"
    leaf = tp.store_dir_name("1.0.1")
    residue = root / f"{tp.NETRC_SCRATCH_PREFIX}crashed"
    residue.mkdir(parents=True)
    (residue / "netrc").write_text(tp.netrc_text(SENTINEL_TOKEN), encoding="utf-8")
    _age_out(residue)
    # A neighbour that must survive: the sweep's only proof of provenance is
    # the name, so it has to be narrow enough to leave everything else alone.
    survivor = root / "some-other-thing"
    survivor.mkdir()
    manifest = tp.parse_toolchain_manifest(_small_manifest())
    (root / leaf).mkdir()
    (root / leaf / tp.STAMP_FILENAME).write_text(
        tp.render_stamp(tp.ToolchainStamp("1.0.1", manifest.digest(), tp.TOOLCHAIN_COMPONENT)),
        encoding="utf-8",
    )

    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    runner = bootstrap_cmd.Runner(json=True)
    bootstrap_cmd.toolchain_phase(ws, log, runner, sdk_root, None, is_windows=False)

    assert runner.planned == []  # the already-installed fast path, as intended
    # NARROW first, then EFFECTIVE. A sweep that deletes the residue by
    # deleting everything would pass the second assertion and fail the
    # customer, so the bound is asserted before the effect.
    assert survivor.exists()
    assert (root / leaf).exists()
    assert not residue.exists()


def test_a_dry_run_reclaims_nothing(tmp_path, monkeypatch):
    """`--dry-run` writes nothing and deletes nothing -- the sweep is a
    mutation like any other, and the flag's whole contract is that a preview
    run leaves the machine as it found it."""
    _point_home_at(monkeypatch, tmp_path)
    sdk_root = _make_sdk_with_toolchains(tmp_path, _small_manifest())
    monkeypatch.setattr(bootstrap_cmd.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap_cmd.platform, "machine", lambda: "x86_64")
    root = tmp_path / "home" / ".alp" / "toolchains"
    residue = root / f"{tp.NETRC_SCRATCH_PREFIX}crashed"
    residue.mkdir(parents=True)

    ws = _workspace(tmp_path)
    log = bootstrap_cmd.Log(json_mode=True)
    runner = bootstrap_cmd.Runner(json=True, dry_run=True)
    bootstrap_cmd.toolchain_phase(ws, log, runner, sdk_root, None, is_windows=False)

    assert residue.exists()


def test_the_staged_credential_lives_under_the_toolchain_root_not_the_shared_tmpdir(
    tmp_path, monkeypatch
):
    """tan-cli#1148 review FIX 1, the placement half. A sweep is only safe
    over a directory tan owns: `/tmp` is world-writable, so globbing and
    `rmtree`-ing `$TMPDIR/...` would delete paths any local user can create.
    Asserting the PARENT is what stops a future refactor quietly moving the
    secret back to `$TMPDIR` and leaving the sweep pointed at nothing."""
    seen: dict = {}
    decoy = tmp_path / "decoy-tmpdir"
    decoy.mkdir()
    monkeypatch.setenv("TAN_GITHUB_TOKEN", SENTINEL_TOKEN)
    # `monkeypatch.setenv("TMPDIR", ...)` is INERT here and the round-2 review
    # caught it being used that way: `tempfile.tempdir` is already cached from
    # the first `gettempdir()` of the process, so the env var is read by
    # nobody and the decoy assertion below stayed green under a mutation that
    # really did stage into `/tmp`. Patch the resolved value instead.
    monkeypatch.setattr(bootstrap_cmd.tempfile, "tempdir", str(decoy))
    # ...and prove the redirect took, so this half cannot go quietly inert
    # again: without `dir=root`, `mkdtemp` resolves here.
    assert bootstrap_cmd.tempfile.gettempdir() == str(decoy)
    _run_toolchain_phase_to_failure(tmp_path, monkeypatch, seen)

    # `assert list(decoy.iterdir()) == []` stood here and is REMOVED, not
    # repaired (tan-cli#1148 round 2). Two things were wrong with it and only
    # one was the `TMPDIR` env var: even with the redirect made real above,
    # that assertion cannot fail BY CONSTRUCTION, because the staged
    # directory is deleted by `_discard_sdk_credential` before this test ever
    # looks -- so the decoy is empty at assertion time whichever directory
    # `mkdtemp` chose. Measured: with `dir=root` dropped, the netrc really
    # landed in the decoy and that line stayed green.
    #
    # The path CAPTURED DURING the run is what carries the property, and it
    # is a real discriminator precisely because the redirect above is live:
    # without `dir=root` this resolves under `decoy`.
    staged = Path(seen["netrc_path"])
    assert staged.parent.parent == tmp_path / "home" / ".alp" / "toolchains"
    assert staged.name == "netrc"
    assert staged.parent.name.startswith(tp.NETRC_SCRATCH_PREFIX)


def test_a_downgrade_to_unauthenticated_reaches_the_json_envelope(tmp_path, monkeypatch):
    """tan-cli#1148 review, minor 1: the downgrade used to be a `log.line`,
    which prints NOTHING in JSON mode (`Log.line`: `if not self.json`). An
    extension user got no signal at all, and was then told by the rate-limit
    remedy to set a token they could see was already set.

    Not `WORKSPACE_BLOCKING`, deliberately: an unauthenticated download
    usually still succeeds, and failing a whole workspace over a missed
    optimisation would be its own defect.
    """
    seen: dict = {}
    monkeypatch.setenv("GH_TOKEN", SENTINEL_TOKEN)
    monkeypatch.setattr(bootstrap_cmd, "_stage_sdk_credential", lambda token, root: None)
    log, runner = _run_toolchain_phase_to_failure(tmp_path, monkeypatch, seen)
    sdk_root = str(tmp_path / "ws" / "alp-sdk")

    codes = [code for code, _msg in log.warnings]
    assert "sdk-credential-unstaged" in codes
    assert "sdk-credential-unstaged" not in log.blocking()
    downgrade = next(msg for code, msg in log.warnings if code == "sdk-credential-unstaged")
    assert "$GH_TOKEN" in downgrade
    assert "unauthenticated" in downgrade
    assert SENTINEL_TOKEN not in downgrade

    # And the remedy must NOT then tell them to set the token they set.
    failure = next(msg for code, msg in log.warnings if code == "toolchain-install")
    assert "went out anonymous even though $GH_TOKEN is set" in failure
    assert "Authenticate the download by setting $TAN_GITHUB_TOKEN" not in failure
    # The whole envelope still carries no secret.
    assert SENTINEL_TOKEN not in _rendered_envelope(log, runner, sdk_root)


def test_a_token_variable_tan_will_not_use_says_so_instead_of_vanishing(tmp_path, monkeypatch):
    """tan-cli#1148 review, minor 3. A `.env` value that kept its literal
    quotes is the realistic case; silently skipping it leaves a download
    anonymous for a reason nothing on screen names."""
    seen: dict = {}
    monkeypatch.setenv("GH_TOKEN", f'"{SENTINEL_TOKEN}"')
    log, runner = _run_toolchain_phase_to_failure(tmp_path, monkeypatch, seen)
    sdk_root = str(tmp_path / "ws" / "alp-sdk")

    downgrade = next(msg for code, msg in log.warnings if code == "sdk-credential-unstaged")
    assert "$GH_TOKEN is set but is not a value tan can use" in downgrade
    assert "netrc_path" not in seen  # nothing staged from an unusable value
    # The rejected VALUE is as much a secret as an accepted one -- a quoted
    # token is still a token.
    assert SENTINEL_TOKEN not in downgrade
    assert SENTINEL_TOKEN not in _rendered_envelope(log, runner, sdk_root)


def test_a_shadowed_unusable_variable_never_claims_a_download_went_out_anonymous(
    tmp_path, monkeypatch
):
    """tan-cli#1148 round 2, FIX 1. `TAN_GITHUB_TOKEN` holding a quoted
    `.env` value, `GH_TOKEN` holding a good one: the refused-variable warning
    used to be raised BEFORE the resolve, so a registered issue code went out
    on the envelope asserting the download "will go out unauthenticated"
    while it went out authenticated on `GH_TOKEN`.

    A wire surface stating the opposite of what happened is worse than the
    silence the warning replaced, so it is now rendered from the outcome:
    the variable is still named -- it lost a race it would otherwise have
    won, which is the surprise worth reporting -- but the sentence ends with
    what actually happened.
    """
    seen: dict = {}
    monkeypatch.setenv("TAN_GITHUB_TOKEN", f'"{SENTINEL_TOKEN}"')
    monkeypatch.setenv("GH_TOKEN", GOOD_TOKEN)
    log, runner = _run_toolchain_phase_to_failure(tmp_path, monkeypatch, seen)
    sdk_root = str(tmp_path / "ws" / "alp-sdk")

    # It really did go out authenticated, on the variable behind the bad one.
    assert tp.NETRC_ENV_VAR in seen["extra_env"][0]
    assert GOOD_TOKEN in seen["netrc_text"]

    downgrade = next(msg for code, msg in log.warnings if code == "sdk-credential-unstaged")
    assert "$TAN_GITHUB_TOKEN is set but is not a value tan can use" in downgrade
    assert "tan authenticated the download with $GH_TOKEN instead." in downgrade
    assert "unauthenticated" not in downgrade
    # And the rate-limit remedy agrees with it rather than contradicting it.
    failure = next(msg for code, msg in log.warnings if code == "toolchain-install")
    assert "AUTHENTICATED quota" in failure
    assert "anonymous per-IP API quota" not in failure
    for secret in (SENTINEL_TOKEN, GOOD_TOKEN):
        assert secret not in _rendered_envelope(log, runner, sdk_root)


def test_a_variable_behind_the_one_that_won_is_not_reported_at_all(tmp_path, monkeypatch):
    """`resolve_sdk_token` takes the first usable and stops, so a broken
    value BEHIND the winner was never going to be consulted. Reporting it
    would be noise about a variable that changed nothing -- and noise on a
    registered issue code is how a code stops being read."""
    seen: dict = {}
    monkeypatch.setenv("TAN_GITHUB_TOKEN", GOOD_TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", f'"{SENTINEL_TOKEN}"')
    log, _runner = _run_toolchain_phase_to_failure(tmp_path, monkeypatch, seen)

    assert [code for code, _ in log.warnings] == ["toolchain-install"]
    assert GOOD_TOKEN in seen["netrc_text"]
