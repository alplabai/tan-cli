# SPDX-License-Identifier: Apache-2.0
"""`flash_cmd._spawn_pipeline` unit tests -- **tan-cli#401**.

`_spawn_pipeline` is the only spawn path in `tan flash` that runs TWO processes,
and until #401 it was the only one with no test at all. It is not exotic:
`flash_plan.plan_yocto_wic` builds `gunzip -c <artefact> | dd of=<target> bs=4M
conv=fsync status=progress` (or `gzip -dc` / `xz -dc`) for every compressed
Yocto `.wic.gz` / `.wic.xz`, so this is the code that writes an SD card or eMMC.

What #401 measured, and what the cases below pin down: the decompressor's stderr
was drained on a background thread and then DROPPED -- the `_Outcome` was built
out of `dd`'s stderr and `dd`'s return code alone. A truncated `.wic.gz` was
therefore reported to the extension as `0+61 records in | 0+1 records out |
3997696 bytes transferred in 0.003888 secs`, with `returncode=0`, while the one
string that explains it (`gunzip: <image>.wic.gz: unexpected end of file`) was
read by `tan` and thrown away. Not a false pass -- `left_ok` was already folded
into `success` -- a false CAUSE, which sends a user re-running a flash that
writes a partial image onto a real block device again.

**No case here touches hardware or spawns a real flash tool.** Both halves of
every pipeline are this same interpreter running an inline script, so the cases
behave identically on macOS, WSL and Windows CI and cannot reach a device.
"""
import os
import sys

import pytest

from tan.commands import flash_cmd

#: Generous, and only ever hit by a REGRESSION: every child below exits in
#: milliseconds, so a case that reaches this ceiling means the drain deadlocked
#: again rather than that a machine was slow.
_TIMEOUT_S = 30.0

#: Both halves are this interpreter, so both attribute to its own basename.
#: `_program_label`'s handling of a real `gunzip`/`dd` name is pinned by the
#: pure cases at the bottom of this module instead.
_INTERPRETER = os.path.basename(sys.executable)


def _py(script: str) -> list[str]:
    """One half of a test pipeline: this interpreter running `script` inline."""
    return [sys.executable, "-c", script]


#: The left half of #401's measured failure -- a decompressor that hit the end of
#: a truncated archive after emitting some plaintext. Both stderr lines and the
#: `1` are `gunzip`'s own, verbatim from the issue.
_TRUNCATED_GZ = (
    "import sys\n"
    "sys.stdout.write('partial')\n"
    "sys.stdout.flush()\n"
    "sys.stderr.write('gunzip: core-image.wic.gz: unexpected end of file\\n')\n"
    "sys.stderr.write('gunzip: core-image.wic.gz: uncompress failed\\n')\n"
    "sys.exit(1)\n"
)

#: The right half of that same failure: `dd status=progress` reports its three
#: lines and exits 0 whatever it was fed. Verbatim from the issue's measurement.
_DD_OK = (
    "import sys\n"
    "sys.stdin.buffer.read()\n"
    "sys.stderr.write('\\n0+61 records in\\n0+1 records out\\n')\n"
    "sys.stderr.write("
    "'3997696 bytes transferred in 0.003888 secs (1028213992 bytes/sec)\\n')\n"
)

#: A decompressor that produced its whole image cleanly.
_GUNZIP_OK = "import sys\nsys.stdout.write('image')\n"

#: `dd` refused by the target device. rc `3`, not `1`, so a case asserting on it
#: proves the reported code is this half's OWN and not a generic failure marker.
_DD_FAIL = (
    "import sys\n"
    "sys.stdin.buffer.read()\n"
    "sys.stderr.write('dd: /dev/sdx: Permission denied\\n')\n"
    "sys.exit(3)\n"
)

#: A decompressor killed by something that never reached stderr (OOM, a signal).
_SILENT_FAIL = "import sys\nsys.exit(9)\n"

#: A decompressor that reported something and then wedged -- the shape of a
#: `.wic.gz` read off a stalled network mount. It holds its stdout open, so `dd`
#: blocks in `read()` and the whole pipeline has to be killed on the timeout.
_HANGS_AFTER_SPEAKING = (
    "import sys, time\n"
    "sys.stderr.write('gunzip: core-image.wic.gz: decompression stalled\\n')\n"
    "sys.stderr.flush()\n"
    "time.sleep(30)\n"
)

#: Comfortably past any platform's pipe buffer (64 KiB on Linux/macOS): ~475 KiB
#: over 8192 lines. This is the anti-hang property -- a decompressor whose stderr
#: is never read blocks in `write()` forever, never closes its stdout, and the
#: pipeline's `wait()` never returns.
_SPEW_LINES = 8192
_SPEW = (
    "import sys\n"
    f"for i in range({_SPEW_LINES}):\n"
    "    sys.stderr.write('DRAIN-' + str(i) + '-' + 'x' * 48 + chr(10))\n"
    "sys.stdout.write('image')\n"
)


# ── the #401 regression ─────────────────────────────────────────────────────


def test_a_failed_decompressor_reaches_the_outcome_instead_of_only_dds_bytes():
    """THE case #401 was filed over, and the one that would have caught it."""
    outcome = flash_cmd._spawn_pipeline(_py(_TRUNCATED_GZ), _py(_DD_OK), True, _TIMEOUT_S)

    assert outcome.success is False
    # `dd`'s own 0, reported for a failure that happened upstream of it, was the
    # defect. The first non-zero of the two halves is `gunzip`'s 1.
    assert outcome.returncode == 1
    assert "gunzip: core-image.wic.gz: unexpected end of file" in outcome.stderr
    assert "gunzip: core-image.wic.gz: uncompress failed" in outcome.stderr


def test_the_entry_message_carries_the_decompressors_diagnosis_and_names_it():
    """`_capture_tail` keeps only the LAST four non-empty lines, and `dd
    status=progress` fills three of them on every run whether it succeeded or
    not. So it is not enough for the decompressor's stderr to be folded in
    somewhere -- it has to survive that window, which is why the failing half is
    ordered last."""
    outcome = flash_cmd._spawn_pipeline(_py(_TRUNCATED_GZ), _py(_DD_OK), True, _TIMEOUT_S)

    message = flash_cmd._execute_message(outcome, "yocto_wic", "a55")

    assert "gunzip: core-image.wic.gz: unexpected end of file" in message
    assert f"{_INTERPRETER} exited rc=1:" in message


def test_a_failed_dd_is_reported_with_its_own_return_code():
    outcome = flash_cmd._spawn_pipeline(_py(_GUNZIP_OK), _py(_DD_FAIL), True, _TIMEOUT_S)

    assert outcome.success is False
    assert outcome.returncode == 3
    assert "dd: /dev/sdx: Permission denied" in outcome.stderr


def test_a_silent_failing_decompressor_still_reports_its_return_code():
    """A half that printed nothing keeps its attributed header: "it exited 9 and
    said nothing" is the whole diagnosis available, and dropping it would leave
    the message reading as a clean `dd`."""
    outcome = flash_cmd._spawn_pipeline(_py(_SILENT_FAIL), _py(_DD_OK), True, _TIMEOUT_S)

    assert outcome.success is False
    assert outcome.returncode == 9
    assert f"{_INTERPRETER} exited rc=9:" in outcome.stderr


def test_both_halves_succeeding_is_a_successful_outcome():
    outcome = flash_cmd._spawn_pipeline(_py(_GUNZIP_OK), _py(_DD_OK), True, _TIMEOUT_S)

    assert outcome.success is True
    assert outcome.returncode == 0
    assert outcome.captured is True
    # Success short-circuits the tail, so no message is built out of any of this.
    assert flash_cmd._capture_tail(outcome) is None


def test_the_drain_consumes_more_than_one_pipe_buffer_without_blocking():
    """The pre-#401 anti-hang property, kept: the decompressor's stderr is read
    concurrently with the write. If the drain regresses this case does not fail
    fast, it stalls until `_TIMEOUT_S` and comes back as a kill -- which is
    exactly what a real `.wic.gz` flash would do to a bench."""
    outcome = flash_cmd._spawn_pipeline(_py(_SPEW), _py(_DD_OK), True, _TIMEOUT_S)

    assert outcome.success is True
    assert "timed out" not in outcome.stderr
    assert outcome.stderr.count("DRAIN-") == _SPEW_LINES


def test_a_hung_pipeline_is_killed_and_still_reports_what_the_decompressor_said():
    """The kill path returns its own outcome, so it is a second place the drained
    stderr can be dropped -- and a decompressor that printed a diagnosis and THEN
    wedged is a different bench problem from one that went quiet. The sentence
    naming this failure mode is kept LAST so `_capture_tail`'s four-line window
    cannot push it out."""
    outcome = flash_cmd._spawn_pipeline(_py(_HANGS_AFTER_SPEAKING), _py(_DD_OK), True, 1.0)

    assert outcome.success is False
    assert "gunzip: core-image.wic.gz: decompression stalled" in outcome.stderr
    assert outcome.stderr.splitlines()[-1] == "timed out after 1s and was killed"


# ── the pure composition helpers ────────────────────────────────────────────


def test_pipeline_stderr_attributes_each_half_and_puts_the_failed_one_last():
    text = flash_cmd._pipeline_stderr(
        flash_cmd._Half("gunzip", 1, "gunzip: core-image.wic.gz: unexpected end of file\n"),
        flash_cmd._Half("dd", 0, "\n0+61 records in\n0+1 records out\n"),
    )

    assert text.splitlines() == [
        "dd exited rc=0:",
        "0+61 records in",
        "0+1 records out",
        "gunzip exited rc=1:",
        "gunzip: core-image.wic.gz: unexpected end of file",
    ]


def test_a_healthy_pipeline_keeps_the_natural_left_to_right_order():
    text = flash_cmd._pipeline_stderr(
        flash_cmd._Half("gunzip", 0, "gunzip: 62.1% -- replaced with core-image.wic\n"),
        flash_cmd._Half("dd", 0, "0+61 records in\n"),
    )

    assert text.splitlines() == [
        "gunzip exited rc=0:",
        "gunzip: 62.1% -- replaced with core-image.wic",
        "dd exited rc=0:",
        "0+61 records in",
    ]


def test_when_both_halves_fail_the_upstream_one_is_reported_last():
    """`dd` failing on a stream the decompressor never finished is a CONSEQUENCE,
    so the decompressor -- upstream, and the cause -- is the half that has to
    survive the tail window."""
    text = flash_cmd._pipeline_stderr(
        flash_cmd._Half("gunzip", 1, "gunzip: core-image.wic.gz: unexpected end of file\n"),
        flash_cmd._Half("dd", 1, "dd: /dev/sdx: Input/output error\n"),
    )

    assert text.splitlines() == [
        "dd exited rc=1:",
        "dd: /dev/sdx: Input/output error",
        "gunzip exited rc=1:",
        "gunzip: core-image.wic.gz: unexpected end of file",
    ]


def test_a_silent_successful_half_contributes_no_section():
    text = flash_cmd._pipeline_stderr(
        flash_cmd._Half("gunzip", 0, ""),
        flash_cmd._Half("dd", 3, "dd: /dev/sdx: Permission denied\n"),
    )

    assert text.splitlines() == ["dd exited rc=3:", "dd: /dev/sdx: Permission denied"]


def test_a_half_killed_before_it_reported_an_rc_says_so():
    text = flash_cmd._pipeline_stderr(
        flash_cmd._Half("gunzip", None, ""),
        flash_cmd._Half("dd", 0, "0+61 records in\n"),
    )

    assert text.splitlines() == ["dd exited rc=0:", "0+61 records in", "gunzip exited rc=unknown:"]


@pytest.mark.parametrize(
    ("left_rc", "right_rc", "expected"),
    [
        (1, 0, 1),  # #401's own case: the decompressor's code, not dd's 0
        (0, 3, 3),
        (1, 3, 1),  # first non-zero, and the left half is first
        (0, 0, 0),
        (None, 0, -1),  # killed before it reported one -- no honest zero exists
        (None, 3, 3),
    ],
)
def test_pipeline_returncode_reports_the_first_non_zero(left_rc, right_rc, expected):
    assert flash_cmd._pipeline_returncode(left_rc, right_rc) == expected


def test_program_label_strips_a_venv_resolved_absolute_path(tmp_path):
    """`_programs_resolved_in_venv` rewrites `argv[0]` to the venv's own copy, so
    by the time a half is attributed its program may be a long absolute path
    nobody needs in a failure message."""
    argv = [str(tmp_path / "bin" / "gunzip"), "-c", "core-image.wic.gz"]

    assert flash_cmd._program_label(argv) == "gunzip"
