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
