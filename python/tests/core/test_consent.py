# SPDX-License-Identifier: Apache-2.0
"""tan-cli#91: `tan.core.consent.can_prompt` — the one gate standing between
`doctor --fix` and unattended host mutation.

This suite exists because the hand-written copy of this check inside
`doctor_cmd.py` shipped with **three of its five conditions**, omitting both
`isatty()` calls, and the entire `doctor --fix` test suite stayed green through
three independent mutations of it — including deleting the guard outright.

So every test below is written to FAIL against a specific way of getting this
wrong, and the truth-table test is exhaustive over all 32 combinations rather
than sampling the ones that happen to be convenient.
"""
from __future__ import annotations

import itertools
import sys

import pytest

from tan.core.consent import can_prompt


class _FakeStream:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def ttys(monkeypatch):
    """Set `sys.stdin`/`sys.stderr` tty-ness independently.

    `monkeypatch.setattr` on `sys.stdin` is the only honest way to drive this:
    under pytest both handles are already replaced by capture objects whose
    `isatty()` is `False`, so a test that did NOT patch them would pass the
    all-flags-clear case for the wrong reason — it would be measuring pytest's
    capture, not the function.
    """

    def _set(*, stdin: bool, stderr: bool) -> None:
        monkeypatch.setattr(sys, "stdin", _FakeStream(stdin))
        monkeypatch.setattr(sys, "stderr", _FakeStream(stderr))

    return _set


def test_all_conditions_met_is_the_only_true_case(ttys):
    ttys(stdin=True, stderr=True)
    assert can_prompt(non_interactive=False, ci=False, json_mode=False) is True


@pytest.mark.parametrize(
    "non_interactive,ci,json_mode,stdin_tty,stderr_tty",
    [c for c in itertools.product([False, True], repeat=5) if c != (False, False, False, True, True)],
)
def test_every_other_combination_is_false(
    ttys, non_interactive, ci, json_mode, stdin_tty, stderr_tty
):
    """Exhaustive over all 32 combinations: exactly one is `True`.

    A sampled test lets a wrong implementation through — the shipped
    `doctor --fix` bug was invisible precisely because no case in its suite
    combined "flags all clear" with "stdio is not a terminal", which is the
    single most common shape of an automated run.
    """
    ttys(stdin=stdin_tty, stderr=stderr_tty)
    assert (
        can_prompt(non_interactive=non_interactive, ci=ci, json_mode=json_mode) is False
    )


def test_a_redirected_stderr_alone_withholds_consent(ttys):
    """`tan doctor --fix 2>log`: stdin can carry the answer, but the question
    would go to a file the user never reads, so tan would block on a prompt
    nobody saw. This is the half a stdin-only check misses."""
    ttys(stdin=True, stderr=False)
    assert can_prompt(non_interactive=False, ci=False, json_mode=False) is False


def test_a_redirected_stdin_alone_withholds_consent(ttys):
    """`tan doctor --fix < /dev/null`: the question can be asked, but no
    answer can ever arrive."""
    ttys(stdin=False, stderr=True)
    assert can_prompt(non_interactive=False, ci=False, json_mode=False) is False


def test_fully_captured_pipes_withhold_consent_even_with_no_flags(ttys):
    """The exact shape that caused tan-cli#91's live incident: a CI runner
    that captures both streams and does NOT pass `--ci` or
    `--non-interactive`. The shipped guard returned "yes, prompt" here and
    spawned four real `winget install` runs."""
    ttys(stdin=False, stderr=False)
    assert can_prompt(non_interactive=False, ci=False, json_mode=False) is False


def test_stdout_tty_ness_is_deliberately_not_consulted(monkeypatch, ttys):
    """`tan doctor --fix | tee log` from a real terminal is a normal,
    fully-interactive invocation. Consulting `stdout` would refuse consent
    with the human sitting right there, so the result must not move when
    `stdout` changes."""
    ttys(stdin=True, stderr=True)
    monkeypatch.setattr(sys, "stdout", _FakeStream(False))
    assert can_prompt(non_interactive=False, ci=False, json_mode=False) is True
    monkeypatch.setattr(sys, "stdout", _FakeStream(True))
    assert can_prompt(non_interactive=False, ci=False, json_mode=False) is True


class _NoIsattyStream:
    """Shaped like `cli._TeeStderr` under `--format json`: a real object with
    `write`/`flush`/`getvalue`, but no `.isatty()` at all -- EXISTS, so an
    `is not None` guard does not catch it, unlike `sys.stdin`/`sys.stderr`
    being `None` outright (fd 0/2 closed)."""


def test_a_tee_stderr_shaped_stream_withholds_consent_instead_of_crashing(monkeypatch):
    """tan-cli#503 round 4: an EARLIER version of this fix guarded
    `sys.stdin`/`sys.stderr` with `is not None` before `.isatty()` -- which
    stops a `None` stream (fd closed) but does nothing for a stream that
    EXISTS yet still lacks `.isatty()`, exactly `cli._TeeStderr`'s own shape
    under `--format json`. `json_mode` is expected to short-circuit this
    function before it reaches such an object on the path that passes it
    correctly -- but a caller that forgets to thread `json_mode` through
    (the exact bug `build_cmd._dispatch` had, tan-cli#488) reaches the bare
    `.isatty()` anyway. `json_mode=False` here stands in for exactly that
    caller bug: `can_prompt` must still answer `False`, not raise
    `AttributeError: '_NoIsattyStream' object has no attribute 'isatty'`."""
    monkeypatch.setattr(sys, "stdin", _FakeStream(True))
    monkeypatch.setattr(sys, "stderr", _NoIsattyStream())
    assert can_prompt(non_interactive=False, ci=False, json_mode=False) is False


def test_a_tee_stderr_shaped_stdin_withholds_consent_instead_of_crashing(monkeypatch):
    """The stdin-side twin of the test above -- `.isatty()` missing on
    `sys.stdin` itself, not just `sys.stderr`, must be caught by the same
    guard rather than only the stderr one."""
    monkeypatch.setattr(sys, "stdin", _NoIsattyStream())
    monkeypatch.setattr(sys, "stderr", _FakeStream(True))
    assert can_prompt(non_interactive=False, ci=False, json_mode=False) is False
