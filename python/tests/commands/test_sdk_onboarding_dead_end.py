# SPDX-License-Identifier: Apache-2.0
"""tan-cli#305 -- a clean host (no alp-sdk checkout anywhere, no `~/.alp`, an
empty cwd) must never be told to run `tan sdk install`/`tan sdk switch`: both
refuse outright in this build (`sdk_cmd._run_not_ported`, `sdk.not-ported`),
so recommending either is a dead end -- the FIRST command a new customer
types (`tan doctor`) used to name a subcommand that immediately refuses, with
no other documented way to reach `tan bootstrap` at all.

The invariant this file pins is NOT "these three strings changed" -- it is
"no next-steps text names a subcommand this build refuses", checked with one
shared assertion against every site that used to get this wrong
independently (`doctor_cmd.sdk_check`, `sdk_cmd.no_active_sdk_text`,
`bootstrap_cmd`'s SDK-unresolved refusal), so a FUTURE rewording that still
points at the same broken command keeps failing this, and a fourth site
added later that makes the same mistake fails it too.

`test_the_clean_host_sequence_from_the_issue_is_no_longer_a_dead_end` is the
one that matters most: it reproduces the exact three-command sequence
tan-cli#305 measured against the published asset (`tan doctor` -> `tan sdk
install <ver>` -> `tan bootstrap --dry-run`), end to end, as real
subprocesses in a hermetically clean `HOME`/cwd -- a green `pytest` run over
unit-level checks alone is what let the original defect ship.
"""
import os
import subprocess
import sys
from pathlib import Path

from tan.commands import doctor_cmd, sdk_cmd

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def assert_no_refused_subcommand_named(text: str) -> None:
    """The shared invariant. `install`/`switch` both refuse
    (`sdk_cmd._run_not_ported`, `sdk.not-ported`) -- checked as the literal
    phrase a customer would actually type (`tan sdk install`/`tan sdk
    switch`), not a bare `"sdk install"` substring, because `west sdk
    install` (the REAL, working Zephyr SDK toolchain installer `doctor`'s
    `zephyrSdk` check legitimately recommends) would otherwise false-positive
    this check.
    """
    for verb in sdk_cmd.NOT_PORTED_SDK_SUBCOMMANDS:
        phrase = f"tan sdk {verb}"
        assert phrase not in text, f"names a refused subcommand ({phrase!r}):\n{text}"


# ── unit level: every site that used to hardcode the recommendation ─────────


def test_doctor_sdk_check_never_recommends_a_refused_subcommand():
    for project_scope in (None, "examples/uart-echo"):
        check = doctor_cmd.sdk_check(None, project_scope=project_scope)
        assert check.status == "fail"
        assert_no_refused_subcommand_named(check.detail)
        assert_no_refused_subcommand_named(check.fix or "")


def test_sdk_current_no_sdk_text_never_recommends_a_refused_subcommand():
    for cached in ([], ["0.13.0"]):
        text = "\n".join(sdk_cmd.no_active_sdk_text(cached))
        assert_no_refused_subcommand_named(text)


def test_python_floor_skew_fix_never_recommends_a_refused_subcommand():
    """The `pythonFloor` warning's `fix` text used to send an unresolved host
    to `tan sdk switch` -- the one other next-steps string tan-cli#305's own
    repro surfaced beside the three the issue named directly."""
    check = doctor_cmd.python_floor_skew_check(
        manifest_floor=doctor_cmd.FALLBACK_PYTHON_FLOOR,
        effective_floor=doctor_cmd.ZEPHYR_PYTHON_FLOOR,
        effective_source="zephyr python.cmake",
        manifest_is_real=False,
    )
    assert check is not None
    assert_no_refused_subcommand_named(check.fix or "")


# ── end to end: the real dead end, reproduced against a live subprocess ─────


def _run_tan(*argv, cwd, home):
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
        "HOME": str(home),
        "USERPROFILE": str(home),
    }
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=env,
        timeout=120,
    )


def test_the_clean_host_sequence_from_the_issue_is_no_longer_a_dead_end(tmp_path):
    """The exact repro tan-cli#305 measured on the published `v0.5.0-rc2`
    asset: no alp-sdk checkout anywhere, `~/.alp` absent, an empty cwd.

    `sdk install` keeps refusing (exit 1, `sdk.not-ported`) -- that refusal
    is correct and this test does not ask it to change (see `sdk_cmd`'s
    module docstring). What must change is that NEITHER `tan doctor` nor
    `tan bootstrap` sends the customer to it, or to `sdk switch`, leaving
    `--sdk-root <path>` (and how to get a checkout at all) as the one path
    forward that is actually named.
    """
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()

    doctor = _run_tan("doctor", "--format", "text", cwd=cwd, home=home)
    assert "Traceback" not in doctor.stderr
    assert doctor.returncode != 0, "an unresolved SDK must not read as a healthy host"
    assert_no_refused_subcommand_named(doctor.stderr)

    install = _run_tan("sdk", "install", "v0.13.0", cwd=cwd, home=home)
    assert install.returncode == 1
    assert "not available in this build of tan" in install.stderr

    bootstrap = _run_tan("bootstrap", "--dry-run", cwd=cwd, home=home)
    assert "Traceback" not in bootstrap.stderr
    assert bootstrap.returncode != 0
    assert_no_refused_subcommand_named(bootstrap.stderr)
    # The honest, working way forward must actually be named.
    assert "--sdk-root" in bootstrap.stderr
    assert "git clone" in bootstrap.stderr


def test_sdk_help_no_longer_advertises_install_and_switch_as_plain_options():
    """`tan sdk --help` used to list `install`/`switch` beside `list`/
    `current` with nothing marking that two of the four refuse."""
    result = subprocess.run(
        [sys.executable, "-m", "tan", "sdk", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
            ),
        },
        timeout=60,
    )
    assert result.returncode == 0
    help_text = result.stdout
    assert "install" in help_text and "switch" in help_text
    assert "refuse" in help_text.lower()
