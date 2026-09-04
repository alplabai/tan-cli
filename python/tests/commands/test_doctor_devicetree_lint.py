# SPDX-License-Identifier: Apache-2.0
"""`tan doctor`'s `devicetreeLint` check (tan-cli#1192), driven against a REAL
seeded host rather than a mock of one.

Every case below seeds an actual Zephyr-SDK-shaped directory and (where it
wants one) an actual executable `dtc` on `PATH`, then runs the real
`doctor_cmd._collect` / the real `tan support-bundle` over it. Nothing here
monkeypatches `_resolve_dtc`, `devicetree_lint_check` or `on_path`: the
predicate under test is precisely the resolution, so stubbing it out would
leave the thing this issue is about untested -- the same discipline
tan-cli#1179's own note tests were held to.

`ZEPHYR_SDK_INSTALL_DIR` is set in EVERY case, including the "SDK has no
hosttools" ones. `_zephyr_sdk_detected_root` honours that variable first and
only falls back to scanning `/opt` and `$HOME`; a bench or developer machine
with a real `/opt/zephyr-sdk-1.0.1` would otherwise decide these tests'
answers for them (tan-cli#603's own lesson, applied to a new probe). `dtc`
itself joined `tests/conftest.PROBE_TOOLS` in this change for the same reason.

## The two host shapes that must be SILENT

1. A `tan bootstrap` install: the SDK carries no `hosttools/` at all
   (tan-cli#1178's `--no-hosttools`) and the host has no distro `dtc`. This is
   what a correctly-provisioned host looks like TODAY, and a check that warned
   here would warn on every one of them -- the anti-pattern `west_check`'s
   docstring records.
2. A host that DOES have a usable `dtc`, from either source. The lint runs;
   there is nothing to say.

Both report `pass`, raise no `issues[]` entry, add no `nextSteps` line, and
leave `exit_code_for` at 0 for that check.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tan.cli import app
from tan.commands import doctor_cmd
from tan.core import devicetree_lint
from tan.exit_codes import ExitCode

runner = CliRunner()


def _sdk_without_hosttools(root: Path) -> Path:
    """A Zephyr SDK exactly as `tan bootstrap` leaves one: the cross toolchain
    `_zephyr_sdk_root_valid` probes for, its `sdk_version` marker, and NO
    `hosttools/` tree (`tan.core.toolchain_provision.NO_HOSTTOOLS_FLAG`)."""
    exe = "arm-zephyr-eabi-gcc.exe" if os.name == "nt" else "arm-zephyr-eabi-gcc"
    binaries = root.joinpath(*doctor_cmd.ZEPHYR_SDK_TOOLCHAIN_DIR)
    binaries.mkdir(parents=True, exist_ok=True)
    (binaries / exe).write_text("", encoding="utf-8")
    (root / "sdk_version").write_text("1.0.1\n", encoding="utf-8")
    assert not (root / "hosttools").exists()
    return root


def _seed_dtc(directory: Path, version_line: str | None) -> Path:
    """An executable `dtc` in `directory` that answers `version_line` on
    `--version`, or prints nothing at all when `version_line` is `None`.

    `.bat` on Windows, `#!/bin/sh` elsewhere -- the same cross-platform shape
    `test_size_command.py` already uses for its fake `size`."""
    directory.mkdir(parents=True, exist_ok=True)
    body = "" if version_line is None else version_line
    script = directory / ("dtc.bat" if os.name == "nt" else "dtc")
    if os.name == "nt":
        script.write_text(
            "@echo off\r\n" + (f"echo {body}\r\n" if version_line else ""), encoding="utf-8"
        )
    else:
        script.write_text(
            "#!/bin/sh\n" + (f'echo "{body}"\n' if version_line else ""), encoding="utf-8"
        )
        script.chmod(0o755)
    return script


def _hosttools_bin(sdk_root: Path) -> Path:
    """Where this HOST's SDK would keep its `dtc` -- the directory
    `host-tools.cmake` puts on `CMAKE_PREFIX_PATH`."""
    host_os, host_arch = doctor_cmd._host_os_arch_tags()
    resolved = devicetree_lint.hosttools_bin_dir(sdk_root, host_os, host_arch)
    assert resolved is not None, "caller must skip this case on Windows"
    return resolved


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A deterministic host: HOME repointed at a scratch dir, `PATH` holding
    exactly one seeded directory, and `ZEPHYR_SDK_INSTALL_DIR` naming a seeded
    SDK. Returns `(sdk_root, path_dir)`."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    path_dir = tmp_path / "bin"
    path_dir.mkdir()
    monkeypatch.setenv("PATH", str(path_dir))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    sdk_root = _sdk_without_hosttools(tmp_path / "zephyr-sdk-1.0.1")
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(sdk_root))
    return sdk_root, path_dir


def _check(workspace: Path) -> doctor_cmd.Check:
    checks = doctor_cmd._collect(str(workspace))
    found = next((c for c in checks if c.name == "devicetreeLint"), None)
    assert found is not None, [c.name for c in checks]
    return found


# ---------------------------------------------------------------------------
# SILENT on a correctly-provisioned host
# ---------------------------------------------------------------------------


def test_a_no_hosttools_bootstrap_install_with_no_dtc_is_silent(tmp_path, host):
    """The headline requirement. Since tan-cli#1178 THIS is what a correct,
    working, documented install looks like -- and the check must not warn on
    it, or it warns on everybody."""
    check = _check(tmp_path)
    assert check.status == "pass", check.detail
    assert check.scope == "host"
    assert check.fix is None
    # It still SAYS the lint is not running -- that is the visibility #1192
    # asks for -- while costing nobody a warning.
    assert "SKIPPED" in check.detail
    assert "--no-hosttools" in check.detail


def test_a_silent_pass_raises_no_issue_adds_no_next_step_and_never_exits_4(tmp_path, host):
    check = _check(tmp_path)
    assert doctor_cmd.checks_to_issues([check]) == []
    assert doctor_cmd.next_steps([check]) == []
    assert doctor_cmd.summarise([check]) == {"pass": 1, "warn": 0, "fail": 0}
    assert doctor_cmd.exit_code_for([check]) is ExitCode.SUCCESS


def test_a_usable_dtc_on_path_is_silent(tmp_path, host):
    """The other correct shape: somebody installed the distro package. The
    lint runs, so there is nothing to report."""
    _sdk_root, path_dir = host
    _seed_dtc(path_dir, "Version: DTC v1.7.0")
    check = _check(tmp_path)
    assert check.status == "pass", check.detail
    assert "1.7.0" in check.detail
    assert "this host's PATH" in check.detail
    assert doctor_cmd.checks_to_issues([check]) == []


@pytest.mark.skipif(os.name == "nt", reason="the Windows SDK ships no hosttools dtc")
def test_an_sdk_that_still_carries_hosttools_is_silent(tmp_path, host):
    """A pre-#1178 or hand-`west sdk install`ed SDK still has its own `dtc`,
    reached through `host-tools.cmake`'s `CMAKE_PREFIX_PATH` append. Silent,
    and it must name the SDK rather than PATH."""
    sdk_root, _path_dir = host
    _seed_dtc(_hosttools_bin(sdk_root), "Version: DTC v1.7.0+")
    check = _check(tmp_path)
    assert check.status == "pass", check.detail
    # `v1.7.0+` -- the real SDK binary's own answer, whose `+` CMake drops too.
    assert "dtc 1.7.0" in check.detail
    assert "hosttools" in check.detail
    assert doctor_cmd.checks_to_issues([check]) == []


@pytest.mark.skipif(os.name == "nt", reason="the Windows SDK ships no hosttools dtc")
def test_the_sdk_copy_wins_over_path_because_cmake_searches_it_first(tmp_path, host):
    """`find_program` searches `CMAKE_PREFIX_PATH` before `PATH`, so the
    version this check reports must be attributed to the binary CMake would
    actually pick -- the `west`-shaped "which binary answered" defect
    (tan-cli#123/#488) applied to `dtc`."""
    sdk_root, path_dir = host
    sdk_dtc = _seed_dtc(_hosttools_bin(sdk_root), "Version: DTC v1.7.0")
    _seed_dtc(path_dir, "Version: DTC v1.6.1")
    check = _check(tmp_path)
    assert check.status == "pass", check.detail
    assert str(sdk_dtc) in check.detail
    assert "1.7.0" in check.detail and "1.6.1" not in check.detail


# ---------------------------------------------------------------------------
# The ONE thing worth speaking about
# ---------------------------------------------------------------------------


def test_a_dtc_below_zephyrs_own_floor_warns(tmp_path, host):
    """`FindDtc.cmake` resets a too-old `dtc` to `DTC-NOTFOUND` and prints
    nothing, so the operator installed a devicetree compiler and got no lint.
    That is the actionable divergence, and it is unreachable on a host
    provisioned per this repo's onramp."""
    _sdk_root, path_dir = host
    _seed_dtc(path_dir, "Version: DTC v1.4.5")
    check = _check(tmp_path)
    assert check.status == "warn", check.detail
    assert check.scope == "host"
    assert "1.4.5" in check.detail and "1.4.6" in check.detail
    assert "DTC-NOTFOUND" in check.detail
    assert check.fix is not None


def test_a_dtc_whose_version_cannot_be_parsed_warns(tmp_path, host):
    """The other half of `FindDtc.cmake`'s reset: `dtc_status` non-zero, or
    output the regex does not match, leaves `DTC_VERSION_STRING` empty and
    `find_package_handle_standard_args` fails the same way."""
    _sdk_root, path_dir = host
    _seed_dtc(path_dir, "not a dtc at all")
    check = _check(tmp_path)
    assert check.status == "warn", check.detail
    assert "did not answer `dtc --version`" in check.detail


def test_the_warn_is_a_warning_issue_a_next_step_and_still_exit_0(tmp_path, host):
    """The severity, asserted on both axes it can move. `warn` puts the check
    in `summary.warn`, raises ONE `warning` issue under the registered code,
    and contributes its fix to `nextSteps` -- and `exit_code_for` still
    answers 0, because a host with an unusable `dtc` builds fine."""
    _sdk_root, path_dir = host
    _seed_dtc(path_dir, "Version: DTC v1.4.5")
    check = _check(tmp_path)
    issues = doctor_cmd.checks_to_issues([check])
    assert [(i.code, i.severity) for i in issues] == [
        ("doctor.devicetree-lint", "warning")
    ]
    assert doctor_cmd.next_steps([check]) == [check.fix]
    assert doctor_cmd.summarise([check]) == {"pass": 0, "warn": 1, "fail": 0}
    assert doctor_cmd.exit_code_for([check]) is ExitCode.SUCCESS


def test_the_check_never_moves_the_real_doctor_exit_code(tmp_path, host, monkeypatch):
    """End to end, through the real command: seeding the WARN condition must
    not turn a doctor that would otherwise exit 0 into an exit 4. Asserted
    against the full check list, not the one check in isolation."""
    _sdk_root, path_dir = host
    _seed_dtc(path_dir, "Version: DTC v1.4.5")
    checks = doctor_cmd._collect(str(tmp_path))
    lint = next(c for c in checks if c.name == "devicetreeLint")
    assert lint.status == "warn"
    without = [c for c in checks if c.name != "devicetreeLint"]
    assert doctor_cmd.exit_code_for(checks) is doctor_cmd.exit_code_for(without)


# ---------------------------------------------------------------------------
# `tan support-bundle` -- the SECOND surface, which must not move at all
# ---------------------------------------------------------------------------


def _bundle(workspace: Path) -> tuple[dict, dict]:
    """`(stdout envelope, the WRITTEN bundle)`. The doctor report this command
    folds in rides in the file, not on stdout -- only `outputPath` does."""
    result = runner.invoke(app, ["support-bundle", "--format", "json"])
    assert result.stdout, result.output
    envelope = json.loads(result.stdout)
    written = json.loads(
        Path(envelope["data"]["outputPath"]).read_text(encoding="utf-8")
    )
    return envelope, written


@pytest.mark.parametrize(
    "version_line", [None, "Version: DTC v1.4.5", "Version: DTC v1.7.0", "garbage"]
)
def test_support_bundle_reports_no_devicetree_lint_row_in_any_state(
    tmp_path, host, monkeypatch, version_line
):
    """`support_bundle_cmd` harvests only the five names `_HOST_CHECK_ORDER`
    lists, out of `doctor_cmd.host_environment_checks` -- which this change
    deliberately does NOT add the check to. So every one of the four host
    shapes above leaves that command's report, its `doctor.summary` and its
    verdict byte-for-byte unmoved, and no severity chosen here can leak into
    it (the `longPaths` demotion, tan-cli#374 finding 1, is what that seam
    costs when a check DOES cross it)."""
    _sdk_root, path_dir = host
    if version_line is not None:
        _seed_dtc(path_dir, version_line)
    monkeypatch.chdir(tmp_path)
    envelope, written = _bundle(tmp_path)
    names = [c["name"] for c in written["doctor"]["checks"]]
    assert "devicetreeLint" not in names, names
    assert not any("devicetree" in i["code"] for i in envelope["issues"]), envelope["issues"]
    assert "devicetreeLint" not in json.dumps(written)


def test_support_bundles_verdict_is_identical_with_and_without_the_warn(
    tmp_path, host, monkeypatch
):
    """The severity question stated as a measurement: run the command on the
    silent host and on the WARN host and compare the two verdicts."""
    _sdk_root, path_dir = host
    monkeypatch.chdir(tmp_path)
    silent_envelope, silent = _bundle(tmp_path)
    _seed_dtc(path_dir, "Version: DTC v1.4.5")
    warned_envelope, warned = _bundle(tmp_path)
    assert silent_envelope["exitCode"] == warned_envelope["exitCode"]
    assert silent_envelope["ok"] == warned_envelope["ok"]
    assert silent_envelope["issues"] == warned_envelope["issues"]
    assert silent["doctor"]["summary"] == warned["doctor"]["summary"]
    assert silent["doctor"]["checks"] == warned["doctor"]["checks"]
    assert silent["doctor"]["nextSteps"] == warned["doctor"]["nextSteps"]


def test_host_environment_checks_does_not_carry_the_new_check(tmp_path, host):
    """The mechanism behind the two tests above, pinned directly: if a later
    change adds `devicetreeLint` to this seam it lands in `tan
    support-bundle` too, and that is a decision to make deliberately."""
    names = [c.name for c in doctor_cmd.host_environment_checks(None, str(tmp_path))]
    assert "devicetreeLint" not in names, names


# ---------------------------------------------------------------------------
# The pure half (`tan.core.devicetree_lint`)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Version: DTC v1.7.0+\n", (1, 7, 0)),
        ("Version: DTC v1.4.6", (1, 4, 6)),
        # `FindDtc.cmake`'s regex makes the `v` optional.
        ("Version: DTC 1.6.1", (1, 6, 1)),
        ("dtc version 1.6.1", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_dtc_version_matches_find_dtcs_own_scrape(text, expected):
    assert devicetree_lint.parse_dtc_version(text) == expected


@pytest.mark.parametrize(
    "version,runs",
    [((1, 4, 5), False), ((1, 4, 6), True), ((1, 7, 0), True), (None, False)],
)
def test_lint_will_run_is_find_package_dtc_1_4_6(version, runs):
    resolution = devicetree_lint.DtcResolution(
        "/somewhere/dtc", devicetree_lint.ORIGIN_PATH, version
    )
    assert devicetree_lint.lint_will_run(resolution) is runs


def test_lint_never_runs_without_a_binary():
    resolution = devicetree_lint.DtcResolution(
        None, devicetree_lint.ORIGIN_ABSENT, (1, 7, 0)
    )
    assert devicetree_lint.lint_will_run(resolution) is False


def test_hosttools_bin_dir_mirrors_host_tools_cmake_per_platform():
    root = Path("/sdk")
    assert devicetree_lint.hosttools_bin_dir(root, "linux", "x86_64") == Path(
        "/sdk/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin"
    )
    assert devicetree_lint.hosttools_bin_dir(root, "linux", "aarch64") == Path(
        "/sdk/hosttools/sysroots/aarch64-pokysdk-linux/usr/bin"
    )
    assert devicetree_lint.hosttools_bin_dir(root, "macos", "aarch64") == Path(
        "/sdk/hosttools/usr/bin"
    )
    # Windows: `host-tools.cmake` appends only qemu/qemu-arc/openocd prefixes,
    # and the Windows hosttools archive ships no `dtc` at all -- so there is
    # no SDK path to look in, only PATH.
    assert devicetree_lint.hosttools_bin_dir(root, "windows", "x86_64") is None
