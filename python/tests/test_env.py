# SPDX-License-Identifier: Apache-2.0
import os
import shutil

import pytest

from tan.env import TEXT_WRAP_MIN_WIDTH, no_color_requested, wrap_width


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
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda **_: os.terminal_size((132, 43)))
    assert wrap_width() == 132


def test_wrap_width_floors_a_narrow_terminal(monkeypatch):
    """Mirrors `doctor_cmd`'s own floor (now the shared `TEXT_WRAP_MIN_WIDTH`
    both modules import): a real terminal narrower than this must not
    degrade to wrapping one word per line."""
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda **_: os.terminal_size((20, 24)))
    assert wrap_width() == TEXT_WRAP_MIN_WIDTH


def test_wrap_width_ignores_no_color(monkeypatch):
    """`--no-color`/`NO_COLOR` must NOT disable wrapping -- a user who wants
    plain (uncoloured) text on a real terminal still wants readable line
    breaks. `wrap_width` takes no `no_color`/`ci` argument at all, unlike
    `use_color` above -- this pins that by construction: `NO_COLOR` set,
    stream genuinely a tty, still resolves a real width rather than `None`."""
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(shutil, "get_terminal_size", lambda **_: os.terminal_size((100, 24)))
    assert wrap_width() == 100
