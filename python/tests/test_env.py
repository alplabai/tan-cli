# SPDX-License-Identifier: Apache-2.0
import errno
import os
import sys

import pytest

from tan import env
from tan.env import TEXT_WRAP_MIN_WIDTH, no_color_requested, terminal_width, wrap_width


def test_unset_does_not_request_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert no_color_requested() is False


@pytest.mark.parametrize("value", ["", "0", "false", "1", "true", "anything"])
def test_any_set_value_including_empty_requests_no_color(monkeypatch, value):
    """PRESENCE, not truthiness -- `NO_COLOR=` (set, empty) is the exact case
    that drifted between the two hand-rolled copies this module replaces."""
    monkeypatch.setenv("NO_COLOR", value)
    assert no_color_requested() is True


# --------------------------------------------------------------------------
# `wrap_width` -- the seam `explain`/`sdk current` hard-wrap prose through.
# --------------------------------------------------------------------------


def test_wrap_width_is_none_off_a_terminal(monkeypatch):
    """The one that matters: `wrap_width()` must answer `None`, not some
    small fallback number, when stderr is piped or redirected -- `tan
    explain | grep` (or any pipe) wants one record per line, and a hard
    newline inserted into that stream corrupts it in a way a real
    terminal's own non-destructive soft-wrap never would. pytest's captured
    stderr is already not a tty, so `sys.stderr.isatty` is forced False
    explicitly rather than relying on that being merely incidental."""
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    assert wrap_width() is None


def test_wrap_width_resolves_a_real_terminal(monkeypatch):
    """Pins `env.terminal_width`, not `shutil.get_terminal_size`
    (tan-cli#564): the measurement now reads stderr's OWN fd, so under
    `pytest -s` -- capture off, `sys.stderr` the developer's real terminal --
    a `shutil` pin would be bypassed entirely and this test would assert
    against whatever that terminal happens to be."""
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr(env, "terminal_width", lambda fallback: 132)
    assert wrap_width() == 132


def test_wrap_width_floors_a_narrow_terminal(monkeypatch):
    """Mirrors `doctor_cmd`'s own floor (now the shared `TEXT_WRAP_MIN_WIDTH`
    both modules import): a real terminal narrower than this must not
    degrade to wrapping one word per line."""
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr(env, "terminal_width", lambda fallback: 20)
    assert wrap_width() == TEXT_WRAP_MIN_WIDTH


def test_wrap_width_ignores_no_color(monkeypatch):
    """`--no-color`/`NO_COLOR` must NOT disable wrapping -- a user who wants
    plain (uncoloured) text on a real terminal still wants readable line
    breaks. `wrap_width` takes no `no_color`/`ci` argument at all, unlike
    `use_color` above -- this pins that by construction: `NO_COLOR` set,
    stream genuinely a tty, still resolves a real width rather than `None`."""
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(env, "terminal_width", lambda fallback: 100)
    assert wrap_width() == 100


# --------------------------------------------------------------------------
# `terminal_width` -- the one place that MEASURES a terminal (tan-cli#564).
# --------------------------------------------------------------------------


class _FakeStderr:
    """A stderr whose `fileno()` answers a chosen fd, so a test can model the
    two-handle split the defect turned on without opening a pty."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._fd


def _stderr_only_terminal(monkeypatch, columns: int, stderr_fd: int = 2) -> None:
    """Model the reported run: stderr on a `columns`-wide terminal, every
    other fd (stdout among them) redirected, so its ioctl raises the exact
    `OSError [Errno 25] Inappropriate ioctl for device` the issue measured."""
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.delenv("LINES", raising=False)
    monkeypatch.setattr(sys, "stderr", _FakeStderr(stderr_fd))

    def _sized(fd: int) -> os.terminal_size:
        if fd == stderr_fd:
            return os.terminal_size((columns, 24))
        raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")

    monkeypatch.setattr(os, "get_terminal_size", _sized)


def test_terminal_width_measures_stderr_not_stdout(monkeypatch):
    """tan-cli#564, the defect itself: `tan explain > out.txt` from a
    70-column terminal. stderr's fd reads 70; `sys.__stdout__`'s raises
    `OSError [Errno 25] Inappropriate ioctl for device`, which
    `shutil.get_terminal_size` swallows itself -- so the old measurement
    silently took its hard-coded 100-column fallback and printed 84- and
    91-column lines onto a 70-column screen."""
    _stderr_only_terminal(monkeypatch, 70)
    assert terminal_width(100) == 70


def test_wrap_width_measures_stderr_not_stdout(monkeypatch):
    """The same run one seam up, where the user feels it: `wrap_width()` is
    what `explain`/`sdk current` hard-wrap through. Returned 100 before the
    fix, 70 after."""
    _stderr_only_terminal(monkeypatch, 70)
    assert wrap_width() == 70


def test_terminal_width_falls_back_to_shutil_when_stderr_is_redirected(monkeypatch):
    """The mirror image -- stderr redirected, stdout still a terminal -- must
    keep resolving exactly what it resolved before the move, not jump to the
    bare `fallback`. `shutil.get_terminal_size` is the last word, so pinning
    it here proves the fall-through is still taken."""
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(sys, "stderr", _FakeStderr(2))

    def _never_a_terminal(fd: int) -> os.terminal_size:
        raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")

    monkeypatch.setattr(os, "get_terminal_size", _never_a_terminal)
    monkeypatch.setattr(
        env.shutil,
        "get_terminal_size",
        lambda fallback: os.terminal_size(fallback),
    )
    assert terminal_width(137) == 137


class _NoFileno:
    """stderr replaced by something that never had a `.fileno()` -- the
    `AttributeError` arm (`io.StringIO` under a test harness, a logging
    shim in a wrapper script)."""

    def isatty(self) -> bool:
        return True


class _ClosedStderr:
    """stderr closed underneath the process -- the `ValueError` arm."""

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise ValueError("I/O operation on closed file")


@pytest.mark.parametrize("broken", [_NoFileno(), _ClosedStderr()])
def test_terminal_width_survives_a_stderr_that_cannot_be_measured(monkeypatch, broken):
    """Same exception pair `stderr_is_tty` above guards against: neither a
    missing `.fileno()` nor a closed stream may propagate out of a width
    probe and take a whole command down over a cosmetic number."""
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(sys, "stderr", broken)
    monkeypatch.setattr(
        env.shutil,
        "get_terminal_size",
        lambda fallback: os.terminal_size(fallback),
    )
    assert terminal_width(88) == 88


@pytest.mark.parametrize(
    ("value", "expected"),
    [("137", 137), ("0", 70), ("", 70), ("not-a-number", 70)],
)
def test_columns_env_overrides_both_handles(monkeypatch, value, expected):
    """`shutil.get_terminal_size`'s own precedence, kept exactly: an operator
    who exports `COLUMNS=137` is overriding BOTH handles on purpose, and that
    override must not be the one thing this move takes away. A non-positive,
    empty or non-numeric value is ignored and the stderr measurement wins,
    same as `shutil` ignores it."""
    _stderr_only_terminal(monkeypatch, 70)
    monkeypatch.setenv("COLUMNS", value)
    assert terminal_width(100) == expected


def test_terminal_width_ignores_a_zero_column_measurement(monkeypatch):
    """An ioctl that answers without raising but reports 0 columns is a real
    pty shape, and `shutil.get_terminal_size` discards it (`columns <= 0`)
    rather than returning it. Kept identical here: taken literally, a 0 would
    reach `build_cmd._heartbeat_line_width`'s `max(columns - 1, 1)` as a
    one-column line -- worse than the fallback it exists to avoid."""
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(sys, "stderr", _FakeStderr(2))
    monkeypatch.setattr(os, "get_terminal_size", lambda fd: os.terminal_size((0, 0)))
    monkeypatch.setattr(
        env.shutil,
        "get_terminal_size",
        lambda fallback: os.terminal_size(fallback),
    )
    assert terminal_width(100) == 100
