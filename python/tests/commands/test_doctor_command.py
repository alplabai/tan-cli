# SPDX-License-Identifier: Apache-2.0
"""``tan doctor`` -- the host-readiness probe, and the Python-floor gap it exists
to close.

Two halves, deliberately:

* **Pure verdicts, called directly.** Every ``*_check`` in ``doctor_cmd`` takes
  already-probed facts and returns a ``Check``. That is where the arithmetic
  lives (which floor wins, which issue code a verdict carries), and it is
  testable without a host that happens to be misconfigured. Driving these
  through a subprocess would mean asserting against whatever ``python3``/
  ``west``/``JLinkExe`` this particular developer machine has, which certifies
  nothing.
* **Framing, driven as a real subprocess.** One JSON document on stdout, the
  exit code, and the guarantee that probing a hostile environment cannot escape
  as a traceback. An in-process call exercises none of those -- same reasoning as
  ``test_build_command.py``.

The load-bearing case is ``test_python_3_10_fails_although_the_manifest_allows
_it``: ``metadata/bootstrap.json`` declares ``pythonMinVersion: "3.10"``,
``zephyr/cmake/modules/python.cmake`` sets ``PYTHON_MINIMUM_REQUIRED 3.12``, and
tan's own POSIX bootstrap branch "cannot fail on version". Ubuntu 22.04 ships
``python3`` = 3.10, so bootstrap succeeds, doctor said Pass, and the customer's
first build died inside Zephyr's CMake configure pointing at Zephyr.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tan.commands import doctor_cmd

#: ``python/`` -- pinned onto the child's PYTHONPATH so ``python -m tan``
#: resolves from a scratch cwd without a ``pip install``.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

#: The worktree root -- one level above ``python/`` -- so ``contract/`` fixtures
#: can be read without re-typing a repo-relative path in every test.
REPO_ROOT = Path(__file__).resolve().parents[3]


def _plant_zephyr_sdk(root: Path) -> None:
    """Create the one file ``_zephyr_sdk_root_valid`` actually probes, so a
    test SDK root is genuine rather than merely present -- the distinction
    finding 1 (tan-cli#286 second pass) exists to enforce.

    Builds the path from ``doctor_cmd.ZEPHYR_SDK_TOOLCHAIN_DIR`` -- the SAME
    constant the production probe reads -- rather than a second, independently
    spelled literal here: tan-cli#286 third pass's blocker was exactly that,
    this fixture and the probe each hardcoding the layout by hand and
    silently agreeing on the WRONG one, so 77 tests passed over a broken
    probe. One constant, two readers, cannot drift apart the same way again.

    The exe suffix reads ``doctor_cmd.os.name``, never this test module's own
    (real) ``os`` -- ``doctor_cmd.os`` is the name a test rebinds to flip the
    production platform branch (see ``_FixedOsName`` below), and reading the
    real module here is finding 2 (tan-cli#286 third pass): it planted the
    POSIX name while a faked-``nt`` probe looked for the ``.exe`` suffix,
    failing 2 of 3 CI legs.
    """
    exe = "arm-zephyr-eabi-gcc.exe" if doctor_cmd.os.name == "nt" else "arm-zephyr-eabi-gcc"
    bin_dir = root.joinpath(*doctor_cmd.ZEPHYR_SDK_TOOLCHAIN_DIR)
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / exe).write_text("", encoding="utf-8")


def run_tan(*argv, cwd, scrub_path=False, env_extra=None):
    """Spawn the port. ``scrub_path`` empties ``PATH`` so not one probe can
    resolve -- the hostile environment doctor is supposed to survive."""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
        **(env_extra or {}),
    }
    if scrub_path:
        env["PATH"] = ""
        # Both are read by the SETOOLS check; a developer machine that exports
        # them must not change what this case observes.
        env.pop("SETOOLS_DIR", None)
        env.pop("SE_UART", None)
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=env,
    )


# --------------------------------------------------------------------------
# The bug: the effective floor, not the declared one
# --------------------------------------------------------------------------


def test_python_3_10_fails_although_the_manifest_allows_it():
    """The exact Ubuntu 22.04 shape. 3.10 clears `pythonMinVersion` and dies at
    Zephyr's CMake configure, so doctor must refuse it here."""
    check = doctor_cmd.python_check(
        found=("python3", (3, 10)), floor=(3, 12), floor_source="zephyr"
    )
    assert check.status == "fail"
    assert check.code == "bootstrap.python-too-old"
    assert "3.10" in check.detail and "3.12" in check.detail


def test_python_3_12_passes():
    check = doctor_cmd.python_check(
        found=("python3", (3, 12)), floor=(3, 12), floor_source="zephyr"
    )
    assert check.status == "pass"


def test_no_runnable_interpreter_is_its_own_frozen_code():
    check = doctor_cmd.python_check(found=None, floor=(3, 12), floor_source="zephyr")
    assert check.status == "fail"
    assert check.code == "bootstrap.python-not-runnable"


def test_zephyr_floor_is_read_from_the_real_cmake_when_the_workspace_resolves(tmp_path):
    modules = tmp_path / "cmake" / "modules"
    modules.mkdir(parents=True)
    (modules / "python.cmake").write_text(
        "include_guard(GLOBAL)\nset(PYTHON_MINIMUM_REQUIRED 3.13)\n", encoding="utf-8"
    )
    floor, source = doctor_cmd.zephyr_python_floor(str(tmp_path))
    assert floor == (3, 13)
    assert "python.cmake" in source


def test_zephyr_floor_falls_back_to_the_pinned_constant_with_no_workspace():
    floor, source = doctor_cmd.zephyr_python_floor(None)
    assert floor == doctor_cmd.ZEPHYR_PYTHON_FLOOR == (3, 12)
    assert "built-in" in source


def test_zephyr_floor_survives_an_unreadable_cmake_file(tmp_path):
    """A directory where a file is expected, undecodable bytes, no match at all
    -- every one is a fallback, never an exception."""
    modules = tmp_path / "cmake" / "modules"
    modules.mkdir(parents=True)
    (modules / "python.cmake").mkdir()
    assert doctor_cmd.zephyr_python_floor(str(tmp_path))[0] == doctor_cmd.ZEPHYR_PYTHON_FLOOR


def test_the_manifest_declaring_a_lower_floor_is_itself_reported():
    """`doctor` must not silently paper over the skew: it says which floor is
    which, so the fix lands in the manifest rather than in the customer."""
    check = doctor_cmd.python_floor_skew_check(
        manifest_floor=(3, 10), effective_floor=(3, 12), effective_source="zephyr python.cmake"
    )
    assert check is not None
    assert check.status == "warn"
    assert "3.10" in check.detail and "3.12" in check.detail
    assert "metadata/bootstrap.json" in check.detail


def test_no_skew_check_when_the_two_floors_agree():
    assert (
        doctor_cmd.python_floor_skew_check(
            manifest_floor=(3, 12), effective_floor=(3, 12), effective_source="x"
        )
        is None
    )


# --------------------------------------------------------------------------
# _load_manifest -- the provenance verdict, as DATA
#
# `manifest_is_real` used to be re-derived by `_collect` sniffing
# `source.startswith("facts from alp-sdk")` -- a prefix match against
# `_load_manifest`'s own f-string, silently flippable by a future reword with
# nothing to catch it. `ManifestLoad.is_real` is now set once, at the read,
# and these pin the three provenances a caller can see.
# --------------------------------------------------------------------------


def _write_bootstrap_json(root: Path, prerequisites: dict) -> Path:
    path = root / "metadata" / "bootstrap.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"prerequisites": prerequisites}), encoding="utf-8")
    return path


def test_load_manifest_resolves_and_declares_the_python_floor(tmp_path):
    _write_bootstrap_json(
        tmp_path,
        {"posix": ["git", "cmake", "python3", "ninja"], "pythonMinVersion": "3.10"},
    )
    loaded = doctor_cmd._load_manifest(str(tmp_path))
    assert loaded.is_real is True
    assert loaded.error is None
    assert loaded.source.startswith("facts from alp-sdk")
    assert loaded.facts["pythonMinVersion"] == "3.10"


def test_load_manifest_resolves_but_omits_the_python_floor(tmp_path):
    """The manifest itself is real -- `is_real` must still say so -- even though
    `pythonMinVersion` is absent and `_manifest_floor_from_facts` falls back to
    `FALLBACK_PYTHON_FLOOR` for the NUMBER. Provenance and the floor value are
    two different questions."""
    _write_bootstrap_json(tmp_path, {"posix": ["git", "cmake", "python3", "ninja"]})
    loaded = doctor_cmd._load_manifest(str(tmp_path))
    assert loaded.is_real is True
    assert loaded.error is None
    assert "pythonMinVersion" not in loaded.facts
    assert doctor_cmd._manifest_floor_from_facts(loaded.facts) == doctor_cmd.FALLBACK_PYTHON_FLOOR


def test_load_manifest_with_no_sdk_resolved_is_not_real():
    loaded = doctor_cmd._load_manifest(None)
    assert loaded.is_real is False
    assert loaded.error is None
    assert "fallback" in loaded.source
    assert loaded.facts["pythonMinVersion"] == "3.10"


def test_collect_reports_no_manifest_read_when_none_resolves(tmp_path, monkeypatch):
    """The end-to-end wire: with no `metadata/bootstrap.json` under `sdk_root`,
    `pythonFloor` (when it fires) must say the manifest was never consulted --
    the exact case `manifest_is_real=False` exists for. `ZEPHYR_BASE` is cleared
    so the built-in Zephyr floor (3.12), which already outranks the fallback
    manifest floor (3.10), is what makes the skew fire deterministically."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    checks = doctor_cmd._collect(str(tmp_path))
    skew = next((c for c in checks if c.name == "pythonFloor"), None)
    assert skew is not None
    assert "no alp-sdk metadata/bootstrap.json was read" in skew.detail


# --------------------------------------------------------------------------
# Prerequisites, west
# --------------------------------------------------------------------------


def test_missing_prerequisites_carry_the_frozen_code_and_the_install_commands():
    check = doctor_cmd.prerequisites_check(
        checked=["git", "cmake", "python3", "ninja"],
        missing=["ninja"],
        install={"ninja": "sudo apt-get install -y ninja-build"},
        source="alp-sdk metadata/bootstrap.json",
    )
    assert check.status == "fail"
    assert check.code == "bootstrap.prerequisites-missing"
    assert "sudo apt-get install -y ninja-build" in (check.fix or "")


def test_west_below_the_manifest_floor_warns_and_names_both_versions():
    check = doctor_cmd.west_check(found="west", version=(0, 13), floor=(0, 14))
    assert check.status == "warn"
    assert "0.13" in check.detail and "0.14" in check.detail


def test_west_absent_fails():
    assert doctor_cmd.west_check(found=None, version=None, floor=(0, 14)).status == "fail"


def test_west_present_but_unparseable_version_is_a_warning_not_a_crash():
    check = doctor_cmd.west_check(found="west", version=None, floor=(0, 14))
    assert check.status == "warn"


# --------------------------------------------------------------------------
# zephyrSdk (tan-cli#286) -- the port had NO such check at all; the Rust
# oracle's `zephyrSdk` (crates/tan-cli/src/commands/doctor.rs::
# append_zephyr_sdk_toolchain, tan-cli#160) is unconditional in plain
# `tan doctor`, so this must be too.
# --------------------------------------------------------------------------


def test_zephyr_sdk_detected_passes():
    check = doctor_cmd.zephyr_sdk_check(True)
    assert check.status == "pass"
    assert check.fix is None


def test_zephyr_sdk_not_detected_fails_and_names_the_exact_install_command():
    check = doctor_cmd.zephyr_sdk_check(False)
    assert check.status == "fail"
    command = "west sdk install --version 1.0.1 -t arm-zephyr-eabi"
    assert command in check.detail
    assert command in (check.fix or "")


def test_zephyr_sdk_check_names_the_env_var_when_it_points_at_a_bad_directory():
    """Finding: the fail detail used to hardcode "(ZEPHYR_SDK_INSTALL_DIR
    unset)" even when the variable WAS set and simply named a directory with
    no working toolchain in it -- exactly the stale-var case the guard in
    `_zephyr_sdk_detected` exists for. It must say the var is set and wrong,
    not unset."""
    check = doctor_cmd.zephyr_sdk_check(False, env_dir="/opt/zephyr-sdk-0.16.5")
    assert "ZEPHYR_SDK_INSTALL_DIR=" in check.detail
    assert "/opt/zephyr-sdk-0.16.5" in check.detail
    assert "unset" not in check.detail


def test_zephyr_sdk_check_says_unset_only_when_it_really_is():
    check = doctor_cmd.zephyr_sdk_check(False, env_dir=None)
    assert "ZEPHYR_SDK_INSTALL_DIR unset" in check.detail


def test_zephyr_sdk_install_dir_env_wins_when_the_directory_actually_has_the_toolchain(
    tmp_path, monkeypatch
):
    _plant_zephyr_sdk(tmp_path)
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(tmp_path))
    assert doctor_cmd._zephyr_sdk_detected() is True


def test_zephyr_sdk_install_dir_pointing_at_an_empty_directory_is_not_trusted(
    tmp_path, monkeypatch
):
    """Finding 1, tan-cli#286 second pass: `Path(env_dir).is_dir()` alone
    passes on ANY directory, so an empty one named by ZEPHYR_SDK_INSTALL_DIR
    used to report a false Pass. The scan roots are pinned to an empty
    stand-in too (finding 3, third pass) -- `/opt` is one of
    `_zephyr_sdk_scan_roots`'s roots UNCONDITIONALLY, not only via
    `HOME`/`USERPROFILE`/`Path.home()`, so pinning only those three (as this
    test used to) still let the assertion flip on a host that genuinely has a
    Zephyr SDK under `/opt` -- a documented `west sdk install` default."""
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(tmp_path))
    monkeypatch.setattr(
        doctor_cmd, "_zephyr_sdk_scan_roots", lambda: [tmp_path / "not-a-real-home"]
    )
    assert doctor_cmd._zephyr_sdk_detected() is False


def test_zephyr_sdk_install_dir_env_pointing_nowhere_is_not_trusted(tmp_path, monkeypatch):
    """A stale `ZEPHYR_SDK_INSTALL_DIR` (exported once, the SDK since removed)
    must not report a false Pass -- mirrors
    `crate::toolchain::env_dir_still_exists`. The scan roots are pinned for
    the same reason, including `/opt`, as the empty-directory case above."""
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(tmp_path / "gone"))
    monkeypatch.setattr(
        doctor_cmd, "_zephyr_sdk_scan_roots", lambda: [tmp_path / "not-a-real-home"]
    )
    assert doctor_cmd._zephyr_sdk_detected() is False


def test_zephyr_sdk_detected_by_scanning_home_with_no_env_var_set(tmp_path, monkeypatch):
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
    _plant_zephyr_sdk(tmp_path / "zephyr-sdk-1.0.1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert doctor_cmd._zephyr_sdk_detected() is True


def test_zephyr_sdk_detected_via_msys_home_split_from_windows_userprofile(tmp_path, monkeypatch):
    """Finding 2, tan-cli#286 second pass -- reproduced on a real host: Git
    Bash/MSYS sets `HOME` to a POSIX-translated path (`/c/Users/caner`) that is
    real but has no SDK under it, while the actual Zephyr SDK sits under the
    native `%USERPROFILE%` (`C:\\Users\\caner\\zephyr-sdk-1.0.1`). The old
    `HOME or USERPROFILE` picked `HOME` (set first) and never scanned
    `USERPROFILE` at all, so a host that HAS the SDK reported `False`. Both
    must be scanned."""
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
    posix_home = tmp_path / "msys-home"  # stands in for e.g. /c/Users/dev: real, empty
    posix_home.mkdir()
    windows_profile = tmp_path / "win-profile"  # stands in for C:\Users\dev
    _plant_zephyr_sdk(windows_profile / "zephyr-sdk-1.0.1")
    monkeypatch.setenv("HOME", str(posix_home))
    monkeypatch.setenv("USERPROFILE", str(windows_profile))
    assert doctor_cmd._zephyr_sdk_detected() is True


def test_zephyr_sdk_root_valid_rejects_a_directory_with_no_compiler_in_it(tmp_path):
    assert doctor_cmd._zephyr_sdk_root_valid(tmp_path) is False


def test_zephyr_sdk_root_valid_accepts_a_real_layout(tmp_path):
    _plant_zephyr_sdk(tmp_path)
    assert doctor_cmd._zephyr_sdk_root_valid(tmp_path) is True


def test_zephyr_sdk_install_version_matches_the_real_toolchain_lock():
    """tan-cli#172's Python-side half. `contract/fixtures/toolchains/
    toolchains.json`'s own `_comment` states the rule verbatim: "A NEW
    consumer of this pin needs its own parity assertion; widening this scan
    will not reach it." Mirrors `crates/tan-core/src/host_env.rs`'s
    `zephyr_sdk_install_version_matches_the_real_toolchain_lock` -- so a bump
    that updates the Rust constant but not this one fails HERE, instead of
    `tan doctor` silently naming a stale `west sdk install --version`."""
    fixture = REPO_ROOT / "contract" / "fixtures" / "toolchains" / "toolchains.json"
    doc = json.loads(fixture.read_text(encoding="utf-8"))
    assert doctor_cmd.ZEPHYR_SDK_INSTALL_VERSION == doc["zephyrSdk"]["version"]


# --------------------------------------------------------------------------
# sevenZip (tan-cli#286 second pass, finding 3) -- the `zephyrSdk` Fail names
# `west sdk install` as the whole remedy, but on native Windows that command
# cannot complete without 7-Zip on PATH (`tan.core.bootstrap`'s
# `manual_install_windows` prose). Mirrors `crate::build_readiness`'s
# `sevenZip` sibling, gated exactly `probe.is_windows && !probe.zephyr_sdk`
# (tan-cli#204).
# --------------------------------------------------------------------------


def test_seven_zip_check_passes_clean_with_no_fix_when_found():
    check = doctor_cmd.seven_zip_check(True)
    assert check.status == "pass"
    assert check.fix is None


def test_seven_zip_check_names_the_pinned_install_command_when_absent():
    check = doctor_cmd.seven_zip_check(False)
    assert check.status == "warn"
    command = "winget install -e --id 7zip.7zip"
    assert command in check.detail
    assert command in (check.fix or "")
    for program in doctor_cmd.SEVEN_ZIP_PROGRAMS:
        assert program in check.detail


class _FixedOsName:
    """A stand-in for the `os` module that reports a FIXED `os.name`, proxying
    every other attribute to the real module.

    Rebinding `doctor_cmd.os` to one of these (rather than mutating
    `os.name` on the real, process-wide module object `import os` hands back
    everywhere) is the only safe way to flip an `os.name`-gated branch in a
    test: mutating the shared module crashes pytest's OWN failure-reporting on
    a real failure (`pathlib.Path.__new__` re-picks `WindowsPath`/`PosixPath`
    from `os.name` on every call, including ones pytest itself makes) --
    caught while writing this test, not theoretical.
    """

    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        return getattr(os, attr)

    @property
    def name(self):
        return self._name


def test_collect_adds_seven_zip_only_on_windows_while_the_sdk_is_absent(tmp_path, monkeypatch):
    """Finding 3 (tan-cli#286 third pass): the scan roots are stubbed outright
    -- `/opt` is one of `_zephyr_sdk_scan_roots`'s roots unconditionally, so a
    developer host with a real `/opt/zephyr-sdk-*` would otherwise flip this
    to `zephyrSdk` detected and drop `sevenZip` from the check list."""
    monkeypatch.setattr(doctor_cmd, "os", _FixedOsName("nt"))
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
    monkeypatch.setattr(
        doctor_cmd, "_zephyr_sdk_scan_roots", lambda: [tmp_path / "not-a-real-home"]
    )
    checks = doctor_cmd._collect(None)
    assert "sevenZip" in {c.name for c in checks}


def test_collect_omits_seven_zip_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor_cmd, "os", _FixedOsName("posix"))
    monkeypatch.delenv("ZEPHYR_SDK_INSTALL_DIR", raising=False)
    monkeypatch.setattr(
        doctor_cmd, "_zephyr_sdk_scan_roots", lambda: [tmp_path / "not-a-real-home"]
    )
    checks = doctor_cmd._collect(None)
    assert "sevenZip" not in {c.name for c in checks}


def test_collect_omits_seven_zip_on_windows_once_the_sdk_is_detected(tmp_path, monkeypatch):
    """The permanent-noise case the gate exists to avoid: once the SDK is
    present, the extractor is irrelevant, so `sevenZip` must not linger."""
    monkeypatch.setattr(doctor_cmd, "os", _FixedOsName("nt"))
    _plant_zephyr_sdk(tmp_path)
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(tmp_path))
    checks = doctor_cmd._collect(None)
    assert "sevenZip" not in {c.name for c in checks}


def test_collect_reports_zephyr_sdk_unconditionally_with_no_board_or_sdk_resolved():
    """The load-bearing regression case: before this check existed, plain
    `tan doctor` with no SDK and no `--build` (the exact fresh-host,
    ADR-0021-Lane-1-P0a call) never mentioned a Zephyr toolchain at all --
    reverting the `_collect` wiring must fail this."""
    checks = doctor_cmd._collect(None, build=False)
    assert "zephyrSdk" in {c.name for c in checks}


def test_collect_reports_zephyr_sdk_under_build_too():
    checks = doctor_cmd._collect(None, build=True)
    assert "zephyrSdk" in {c.name for c in checks}


# --------------------------------------------------------------------------
# --build: zephyrWorkspace
# --------------------------------------------------------------------------


def test_zephyr_workspace_warns_with_no_zephyr_base():
    check = doctor_cmd.zephyr_workspace_check(None, None, None)
    assert check.status == "warn"
    assert "ZEPHYR_BASE" in check.detail


def test_zephyr_workspace_warns_when_the_dir_is_not_a_zephyr_checkout():
    check = doctor_cmd.zephyr_workspace_check("/nope", None, None)
    assert check.status == "warn"
    assert "VERSION" in check.detail


def test_zephyr_workspace_passes_with_no_sdk_pin_to_compare():
    check = doctor_cmd.zephyr_workspace_check("/zephyrproject/zephyr", "4.4.0", None)
    assert check.status == "pass"
    assert "4.4.0" in check.detail


def test_zephyr_workspace_warns_on_a_pin_mismatch():
    check = doctor_cmd.zephyr_workspace_check("/zephyrproject/zephyr", "4.3.0", "4.4.1")
    assert check.status == "warn"
    assert "4.3.0" in check.detail and "4.4.1" in check.detail


def test_zephyr_workspace_passes_on_a_matching_pin():
    check = doctor_cmd.zephyr_workspace_check("/zephyrproject/zephyr", "4.4.1", "4.4.1")
    assert check.status == "pass"


def test_build_flag_adds_zephyr_workspace_to_the_check_list_plain_doctor_lacks(tmp_path):
    plain = run_tan("doctor", "--format", "json", cwd=tmp_path, scrub_path=True)
    built = run_tan("doctor", "--build", "--format", "json", cwd=tmp_path, scrub_path=True)
    plain_env = json.loads(plain.stdout)
    built_env = json.loads(built.stdout)

    plain_names = {c["name"] for c in plain_env["data"]["checks"]}
    built_names = {c["name"] for c in built_env["data"]["checks"]}
    assert "zephyrWorkspace" not in plain_names
    assert "zephyrWorkspace" in built_names
    assert built_names - plain_names == {"zephyrWorkspace"}


def test_build_flag_reads_a_real_zephyr_base(tmp_path):
    zephyr = tmp_path / "zephyr"
    zephyr.mkdir()
    (zephyr / "VERSION").write_text(
        "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = 1\n", encoding="utf-8"
    )
    proc = run_tan(
        "doctor", "--build", "--format", "json", cwd=tmp_path, scrub_path=True,
        env_extra={"ZEPHYR_BASE": str(zephyr)},
    )
    env = json.loads(proc.stdout)
    check = next(c for c in env["data"]["checks"] if c["name"] == "zephyrWorkspace")
    assert check["status"] == "pass"
    assert "4.4.1" in check["detail"]


# --------------------------------------------------------------------------
# SETOOLS -- the second silent gap
# --------------------------------------------------------------------------


def test_setools_check_names_both_env_vars_the_fdt_package_and_the_alif_download():
    check = doctor_cmd.setools_check(
        setools_dir=None, se_uart=None, has_fdt=False, is_linux=True
    )
    assert check.status == "warn"
    blob = f"{check.detail} {check.fix}"
    for token in ("SETOOLS_DIR", "SE_UART", "fdt", "app-release-exec-linux"):
        assert token in blob, f"the SETOOLS check never mentions {token}"


def test_setools_dir_pointing_somewhere_without_app_gen_toc_is_reported(tmp_path):
    check = doctor_cmd.setools_check(
        setools_dir=str(tmp_path), se_uart="/dev/ttyUSB0", has_fdt=True, is_linux=True
    )
    assert check.status == "warn"
    assert "app-gen-toc" in check.detail


def test_a_fully_provisioned_setools_host_passes(tmp_path):
    (tmp_path / "app-gen-toc").write_text("", encoding="utf-8")
    (tmp_path / "app-write-mram").write_text("", encoding="utf-8")
    check = doctor_cmd.setools_check(
        setools_dir=str(tmp_path), se_uart="/dev/ttyUSB0", has_fdt=True, is_linux=True
    )
    assert check.status == "pass"


def test_setools_is_unknown_not_warn_off_linux():
    """`alif_flash.py` hard-codes `app-release-exec-linux`; there is no verdict
    to give a Windows/macOS host, and `unknown` counts in no summary bucket."""
    check = doctor_cmd.setools_check(
        setools_dir=None, se_uart=None, has_fdt=False, is_linux=False
    )
    assert check.status == "unknown"
    assert "app-release-exec-linux" in check.detail


# --------------------------------------------------------------------------
# J-Link / Flow D
# --------------------------------------------------------------------------


def test_jlink_names_the_part_number_device_profile_and_the_dll_floor():
    check = doctor_cmd.jlink_check(found="/usr/bin/JLinkExe", version=(9, 50))
    assert check.status == "pass"
    blob = f"{check.detail} {check.fix or ''}"
    assert "AE822FA0E5597LS0_M55_HE" in blob
    assert "V13" in blob


def test_jlink_below_v9_46_warns_because_flow_d_has_no_mram_loader():
    check = doctor_cmd.jlink_check(found="/usr/bin/JLinkExe", version=(9, 40))
    assert check.status == "warn"
    assert "9.46" in check.detail


def test_jlink_absent_is_a_warning_not_a_failure():
    assert doctor_cmd.jlink_check(found=None, version=None).status == "warn"


def test_jlink_present_with_an_unreadable_version_still_reports_the_requirements():
    check = doctor_cmd.jlink_check(found="/usr/bin/JLinkExe", version=None)
    assert check.status == "warn"
    assert "AE822FA0E5597LS0_M55_HE" in f"{check.detail} {check.fix or ''}"


def test_jlink_check_names_a_caller_supplied_device_not_only_the_fallback():
    """`_collect` passes the metadata-resolved profile through; a stand-in value
    proves the parameter is actually used, not shadowed by the constant."""
    check = doctor_cmd.jlink_check(
        found="/usr/bin/JLinkExe", version=(9, 50), device="SOME_OTHER_PROFILE"
    )
    assert "SOME_OTHER_PROFILE" in check.detail
    assert "AE822FA0E5597LS0_M55_HE" not in check.detail


# --------------------------------------------------------------------------
# jlink_flash_device -- resolving the AE822 profile from metadata
# --------------------------------------------------------------------------


def _write_e8_json(root: Path, variants: list[dict]) -> None:
    e8 = root / "metadata" / "socs" / "alif" / "ensemble"
    e8.mkdir(parents=True)
    (e8 / "e8.json").write_text(json.dumps({"variants": variants}), encoding="utf-8")


def test_jlink_flash_device_is_read_from_e8_json_when_an_sdk_resolves(tmp_path):
    """The real shape: two AE822 package variants, only the second carrying a
    `jlink_flash_device` -- the one variant with an MRAM loader profile."""
    _write_e8_json(
        tmp_path,
        [
            {"debug": {"jlink_device": {"m55_he": "Cortex-M55"}}},
            {"debug": {"jlink_device": {"m55_he": "Cortex-M55"},
                       "jlink_flash_device": "AE822FA0E5597LS0_M55_HE"}},
        ],
    )
    device, source = doctor_cmd.jlink_flash_device(str(tmp_path))
    assert device == "AE822FA0E5597LS0_M55_HE"
    assert "e8.json" in source


def test_jlink_flash_device_falls_back_with_no_sdk_root():
    device, source = doctor_cmd.jlink_flash_device(None)
    assert device == doctor_cmd.JLINK_AEN_DEVICE
    assert "built-in fallback" in source


def test_jlink_flash_device_falls_back_when_no_variant_carries_the_key(tmp_path):
    _write_e8_json(tmp_path, [{"debug": {"jlink_device": {"m55_he": "Cortex-M55"}}}])
    device, source = doctor_cmd.jlink_flash_device(str(tmp_path))
    assert device == doctor_cmd.JLINK_AEN_DEVICE
    assert "built-in fallback" in source


def test_jlink_flash_device_survives_a_missing_or_malformed_e8_json(tmp_path):
    """No file, and a directory where a file is expected: both fall back rather
    than raising -- doctor's whole job is to run on a host where things are
    wrong."""
    assert doctor_cmd.jlink_flash_device(str(tmp_path))[0] == doctor_cmd.JLINK_AEN_DEVICE

    (tmp_path / "metadata" / "socs" / "alif" / "ensemble").mkdir(parents=True)
    (tmp_path / "metadata" / "socs" / "alif" / "ensemble" / "e8.json").mkdir()
    assert doctor_cmd.jlink_flash_device(str(tmp_path))[0] == doctor_cmd.JLINK_AEN_DEVICE


def test_jlink_flash_device_falls_back_when_variants_disagree(tmp_path):
    """Two variants carrying DIFFERENT `jlink_flash_device` values is
    ambiguous, not resolved: picking whichever serialises first would silently
    name the wrong part with nothing to catch it, so this must fall back and
    say why -- not pick either one."""
    _write_e8_json(
        tmp_path,
        [
            {"debug": {"jlink_flash_device": "AE822FA0E5597LS0_M55_HE"}},
            {"debug": {"jlink_flash_device": "SOME_OTHER_PART_M55_HE"}},
        ],
    )
    device, source = doctor_cmd.jlink_flash_device(str(tmp_path))
    assert device == doctor_cmd.JLINK_AEN_DEVICE
    assert "ambiguous" in source


# --------------------------------------------------------------------------
# _collect -- the production call site, not just the helpers in isolation
# --------------------------------------------------------------------------


def test_collect_wires_the_resolved_jlink_device_and_its_source_into_the_check(tmp_path):
    """Reverting `_collect` to the old hardcoded `jlink_check(jlink_exe,
    jlink_version)` call must fail THIS test: it is the only coverage of the
    production call site, not just `jlink_flash_device`/`jlink_check` in
    isolation. Also proves the source travels into the envelope (Finding 5):
    the check text must differ depending on where the profile came from."""
    _write_e8_json(tmp_path, [{"debug": {"jlink_flash_device": "STAND-IN-PROFILE"}}])
    checks = doctor_cmd._collect(str(tmp_path))
    jlink = next(c for c in checks if c.name == "jlink")
    assert "STAND-IN-PROFILE" in jlink.detail
    assert doctor_cmd.JLINK_AEN_DEVICE not in jlink.detail
    assert "e8.json" in jlink.detail


# --------------------------------------------------------------------------
# Aggregation and issue codes
# --------------------------------------------------------------------------


def test_a_single_failing_check_exits_4_never_0():
    checks = [
        doctor_cmd.Check("a", "pass", "fine"),
        doctor_cmd.Check("b", "fail", "broken"),
    ]
    assert doctor_cmd.exit_code_for(checks) == 4


def test_warnings_alone_do_not_fail_the_host():
    checks = [doctor_cmd.Check("a", "warn", "meh"), doctor_cmd.Check("b", "unknown", "?")]
    assert doctor_cmd.exit_code_for(checks) == 0


def test_unknown_is_counted_in_no_summary_bucket():
    summary = doctor_cmd.summarise(
        [
            doctor_cmd.Check("a", "pass", ""),
            doctor_cmd.Check("b", "warn", ""),
            doctor_cmd.Check("c", "fail", ""),
            doctor_cmd.Check("d", "unknown", ""),
        ]
    )
    assert summary == {"pass": 1, "warn": 1, "fail": 1}


def test_issues_default_to_doctor_dot_check_name_and_skip_passing_checks():
    issues = doctor_cmd.checks_to_issues(
        [
            doctor_cmd.Check("west", "warn", "old"),
            doctor_cmd.Check("jlink", "pass", "fine"),
            doctor_cmd.Check("setools", "unknown", "not askable"),
        ]
    )
    assert [(i.code, i.severity) for i in issues] == [("doctor.west", "warning")]


def test_the_retired_windows_unsupported_code_is_never_reused():
    source = Path(doctor_cmd.__file__).read_text(encoding="utf-8")
    assert "windows-unsupported" not in source


def test_the_three_frozen_codes_are_spelled_exactly():
    source = Path(doctor_cmd.__file__).read_text(encoding="utf-8")
    for code in (
        "bootstrap.python-too-old",
        "bootstrap.python-not-runnable",
        "bootstrap.prerequisites-missing",
    ):
        assert f'"{code}"' in source


# --------------------------------------------------------------------------
# Hostile probes -- the recurring Critical in this port
# --------------------------------------------------------------------------


def test_a_probe_that_hangs_is_killed_and_returns_none():
    assert doctor_cmd.probe([sys.executable, "-c", "import time; time.sleep(30)"], timeout=2) is None


def test_a_probe_of_a_missing_binary_returns_none():
    assert doctor_cmd.probe(["tan-doctor-no-such-binary-8f3a"], timeout=5) is None


def test_a_probe_that_exits_non_zero_returns_none():
    assert doctor_cmd.probe([sys.executable, "-c", "raise SystemExit(3)"], timeout=10) is None


def test_a_probe_emitting_undecodable_bytes_does_not_raise():
    out = doctor_cmd.probe(
        [sys.executable, "-c", r"import sys; sys.stdout.buffer.write(b'\xff\xfe ok')"],
        timeout=10,
    )
    assert out is not None and "ok" in out


def test_path_lookup_never_consults_the_current_directory(tmp_path, monkeypatch):
    """Mirrors `command_on_path`: `shutil.which` inserts `os.curdir` ahead of
    PATH on Windows, so a checkout shipping its own `west.exe` would be
    reported as the host's tooling."""
    planted = tmp_path / ("west.exe" if os.name == "nt" else "west")
    planted.write_text("", encoding="utf-8")
    planted.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")
    assert doctor_cmd.on_path("west") is None


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def test_a_scrubbed_host_exits_4_with_exactly_one_envelope_and_no_traceback(tmp_path):
    proc = run_tan("doctor", "--format", "json", cwd=tmp_path, scrub_path=True)
    assert "Traceback" not in proc.stderr, proc.stderr
    assert proc.returncode == 4, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    envelope = json.loads(proc.stdout)
    assert envelope["command"] == "doctor"
    assert envelope["ok"] is False
    assert envelope["exitCode"] == 4
    # `sdk` is OMITTED when absent, never null.
    assert "sdk" not in envelope
    codes = {i["code"] for i in envelope["issues"]}
    # `west`, `cmake` and `ninja` cannot resolve with no PATH, so this one is
    # certain on every host.
    assert "bootstrap.prerequisites-missing" in codes
    # The Python verdict is host-dependent even with PATH scrubbed, in all
    # THREE directions -- so it is the CHECK's presence and vocabulary that is
    # asserted, never a particular outcome. Windows resolves `py.exe` from the
    # Windows directory regardless of PATH (CreateProcess searches it before
    # PATH), so the launcher's default interpreter decides: below the effective
    # floor gives `fail` (`bootstrap.python-too-old`), at or above it gives
    # `pass` and NO issue at all, and a POSIX host with no PATH gives `fail`
    # (`bootstrap.python-not-runnable`). Pinning any one of those makes this
    # case flip on a host change that is not a defect -- installing Python 3.12
    # beside a 3.11 was enough to move it from the second to the third.
    host_python = next(c for c in envelope["data"]["checks"] if c["name"] == "hostPython")
    assert host_python["status"] in ("pass", "fail")
    if host_python["status"] == "fail":
        assert codes & {"bootstrap.python-not-runnable", "bootstrap.python-too-old"}
    assert {"summary", "checks", "generatedAt", "nextSteps"} <= set(envelope["data"])


@pytest.mark.parametrize(
    "epoch", ["1700000000000", "99999999999", "-99999999999", "253402300799"]
)
def test_an_out_of_range_source_date_epoch_still_reports_the_host(epoch, tmp_path):
    """`data.generatedAt` is rendered INSIDE doctor's own try/except, so a
    timestamp helper that throws does not produce a traceback here -- it produces
    a WRONG VERDICT: `doctor.internal-failure` at exit 5, `data: null`, on a host
    that was diagnosed fine. The quiet half of the same defect
    `test_debug_config_command.py` covers loudly.

    Milliseconds is the realistic trigger (1700000000000 -> year 55838), and CI
    and reproducible-build environments are what set this variable. `PATH` is
    scrubbed so the exit code is the deterministic 4 of the case above rather
    than whatever this developer machine happens to have installed.
    """
    proc = run_tan(
        "doctor", "--format", "json", cwd=tmp_path, scrub_path=True,
        env_extra={"SOURCE_DATE_EPOCH": epoch},
    )
    assert "Traceback" not in proc.stderr, proc.stderr
    envelope = json.loads(proc.stdout)

    codes = {i["code"] for i in envelope["issues"]}
    assert "doctor.internal-failure" not in codes, envelope["issues"]
    assert proc.returncode == 4, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert envelope["data"] is not None
    # Shape, not value: an out-of-range epoch falls back to the wall clock.
    time.strptime(envelope["data"]["generatedAt"], "%Y-%m-%dT%H:%M:%SZ")


def test_text_mode_writes_nothing_to_stdout(tmp_path):
    proc = run_tan("doctor", cwd=tmp_path, scrub_path=True)
    assert proc.stdout == ""
    assert proc.stderr.strip() != ""


def test_an_invalid_format_is_rejected(tmp_path):
    proc = run_tan("doctor", "--format", "yaml", cwd=tmp_path)
    assert proc.returncode == 2


# --------------------------------------------------------------------------
# `project` envelope field -- posix separators, anchored on `--project`
# --------------------------------------------------------------------------


def test_project_envelope_uses_posix_separators_not_native(tmp_path):
    """`root`/`boardYaml` must be forward-slash on every platform, matching the
    oracle -- verified: `tan --project app doctor --format json` from a scratch
    tree reports `"root":"C:/Users/.../app"`. `Project(root=str(workspace_root),
    board_yaml=board_yaml)` used to emit the native `C:\\Users\\...\\app`
    (`os.path.join`/`Path` on Windows) instead."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "board.yaml").write_text("", encoding="utf-8")
    proc = run_tan(
        "--project", "app", "doctor", "--format", "json", cwd=tmp_path, scrub_path=True
    )
    envelope = json.loads(proc.stdout)
    assert envelope["project"] == {
        "root": app.as_posix(),
        "boardYaml": (app / "board.yaml").as_posix(),
    }


def test_explicit_relative_board_yaml_is_anchored_on_project_not_cwd(tmp_path):
    """`--project app doctor --board-yaml board.yaml` must resolve onto
    `<tmp_path>/app/board.yaml`, not the real cwd -- verified against the
    oracle. Before the fix, `--board-yaml` was anchored only inside the
    discovery branch (`if board_yaml is None and ...`); an EXPLICIT relative
    `--board-yaml` skipped that branch entirely and was reported verbatim
    (`'board.yaml'`, resolving against the real cwd downstream) -- the Critical
    wrong-project defect class `build_cmd.build` already guards against,
    surviving here."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "board.yaml").write_text("app board\n", encoding="utf-8")
    # A DIFFERENT board.yaml sitting in the real cwd -- the one that would
    # (wrongly) win if the anchor is missing.
    (tmp_path / "board.yaml").write_text("cwd board\n", encoding="utf-8")
    proc = run_tan(
        "--project", "app", "doctor", "--board-yaml", "board.yaml", "--format", "json",
        cwd=tmp_path, scrub_path=True,
    )
    envelope = json.loads(proc.stdout)
    assert envelope["project"] == {
        "root": app.as_posix(),
        "boardYaml": (app / "board.yaml").as_posix(),
    }
