# SPDX-License-Identifier: Apache-2.0
"""`tan.commands.build_cmd`'s output/streaming helpers -- `_stream` and the
tan-cli#287 `_Heartbeat` it is wrapped in for a real `tan build` invocation.

Three levels, because the hard constraints sit at three levels:

* **Unit, `io.StringIO`** -- `_Heartbeat`'s mechanism: a disabled instance is
  a pure passthrough (no thread, no output of its own -- the state a non-TTY
  host or `--format json` run must always get), and an enabled one only
  speaks after real silence, then blanks itself the moment output resumes.
  `StringIO` has no concept of a column or of `\\r` moving the cursor, so
  this level cannot see a wrap or a too-short erase pad -- see the next one.
* **Unit, `_FakeTerminal`** -- the actual tan-cli#287 blocker: a message and
  its erase pad both fit the REPORTED terminal width, on a terminal narrower
  than the old hardcoded 88-column pad. `_FakeTerminal` models `\\r`-to-
  column-0 and fixed-width autowrap, which is what a real tty does and
  `StringIO` does not -- a `StringIO`-only suite passed identically whether
  the pad was 1 space or 88, which is how the mismatch reached review.
* **CLI, subprocess** -- `tan build --format json` end to end, same shape as
  `test_build_command.py`'s own envelope tests. Proves the WIRING: arming a
  `_Heartbeat` inside `_dispatch`/`_build` did not put one stray byte on
  stdout, the channel with the actual contract (`envelope_of`'s `json.loads`
  on the FULL string already fails on any trailing data; the extra
  assertions here just name what that would have been). The heartbeat
  itself is not exercised at this level -- `run_tan`'s `capture_output=True`
  makes `sys.stderr.isatty()` False regardless of `--format`, same as any
  non-interactive caller -- so this level proves ONLY the wiring, not the
  mechanism; the two unit levels above cover that.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

from tan.commands import build_cmd
from tests.commands.test_build_command import (
    ALL_ARTEFACTS,
    envelope_of,
    project,  # noqa: F401 -- pytest fixture, imported for its side effect
    run_tan,
    two_slice_plan,
    write_plan,
)


class _FakeTerminal:
    """Minimal terminal model: `\\r` returns to column 0 of the CURRENT row,
    a write past `width` columns wraps onto a NEW row -- the two behaviours
    `io.StringIO` cannot model (it treats `\\r` as an inert character and has
    no concept of a column at all) and whose absence is exactly what let
    tan-cli#287's 88-vs-114 pad mismatch through the first review pass:
    every assertion against a `StringIO` buffer passes identically whether
    the pad is 1 space or 88."""

    def __init__(self, width: int) -> None:
        self.width = width
        self.rows: list[str] = [""]

    def write(self, text: str) -> int:
        for ch in text:
            if ch == "\r":
                self.rows[-1] = ""
            elif ch == "\n":
                self.rows.append("")
            else:
                if len(self.rows[-1]) >= self.width:
                    self.rows.append("")
                self.rows[-1] += ch
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


# --- unit: _Heartbeat ---------------------------------------------------


def test_disabled_heartbeat_only_relays_lines_like_stream(monkeypatch):
    """`enabled=False` (the non-TTY / `--format json` case) must behave
    exactly like passing `_stream` straight through: every line relayed,
    nothing else ever printed, no thread started."""
    fake_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    heartbeat = build_cmd._Heartbeat(enabled=False)
    with heartbeat:
        assert heartbeat._thread is None
        heartbeat("first line")
        heartbeat("second line")

    assert fake_stderr.getvalue() == "first line\nsecond line\n"
    assert "\r" not in fake_stderr.getvalue()


def test_enabled_heartbeat_ticks_after_silence_then_clears_on_output(monkeypatch):
    """Armed (TTY, text mode): silent for longer than the threshold ->
    prints a `still building` line; a real line arriving afterwards blanks
    it (a `\\r`-clear) before relaying, so it never mixes into the stream it
    was standing in for.

    `shutil.get_terminal_size` is pinned wide (see
    `test_heartbeat_line_never_wraps_on_a_narrow_terminal` for the narrow
    case this test does NOT cover): unpinned, it reads the REAL terminal --
    `COLUMNS`, on a host/CI runner that exports one, or the actual tty width
    otherwise -- and a value narrower than the ~110-char message below would
    truncate `"still building"` itself out of the line before this test's own
    content assertion ever runs, failing for a reason unrelated to the code
    under test."""
    monkeypatch.setattr(build_cmd, "_HEARTBEAT_SILENCE_THRESHOLD_S", 0.05)
    monkeypatch.setattr(build_cmd, "_HEARTBEAT_TICK_S", 0.02)
    monkeypatch.setattr(
        build_cmd.shutil,
        "get_terminal_size",
        lambda fallback=(80, 24): os.terminal_size((120, 24)),
    )
    fake_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    heartbeat = build_cmd._Heartbeat(enabled=True)
    with heartbeat:
        assert heartbeat._thread is not None
        time.sleep(0.3)  # several ticks past the (monkeypatched) threshold
        armed = fake_stderr.getvalue()
        assert "still building" in armed, armed

        heartbeat("real output resumed")
        after_output = fake_stderr.getvalue()[len(armed) :]
        # The clear comes first (a carriage return back to column 0 and
        # blanks), THEN the relayed line -- never interleaved.
        assert after_output.startswith("\r")
        assert after_output.rstrip().endswith("real output resumed")

    # Nothing left ticking after `__exit__`: no further growth once the
    # `with` block ends.
    final = fake_stderr.getvalue()
    time.sleep(0.1)
    assert fake_stderr.getvalue() == final


def test_heartbeat_line_never_wraps_on_a_narrow_terminal(monkeypatch):
    """tan-cli#287 blocker, reproduced and pinned: the erase pad used to be
    a hardcoded 88 columns against a message that runs 114+ chars, so on
    any terminal narrower than that (80 columns -- the common case -- among
    them) `\\r` rewound only the CURRENT row and every tick grew a fresh
    one. Driven through `_FakeTerminal`, the pre-fix arithmetic produces 8
    rows in 7 ticks on an 80-column terminal (reproduced standalone: `msg =
    f"...{elapsed}...{idle}..."; term.write("\\r" + msg)` with no width
    clamp); this pins the fixed behaviour -- one row, never more, on a
    terminal narrower still."""
    monkeypatch.setattr(build_cmd, "_HEARTBEAT_SILENCE_THRESHOLD_S", 0.02)
    monkeypatch.setattr(build_cmd, "_HEARTBEAT_TICK_S", 0.01)
    width = 40  # narrower than the old hardcoded 88-column pad
    monkeypatch.setattr(
        build_cmd.shutil,
        "get_terminal_size",
        lambda fallback=(80, 24): os.terminal_size((width, 24)),
    )
    term = _FakeTerminal(width)
    monkeypatch.setattr(sys, "stderr", term)

    heartbeat = build_cmd._Heartbeat(enabled=True)
    with heartbeat:
        time.sleep(0.3)  # several ticks, elapsed/idle digits growing

    assert len(term.rows) == 1, term.rows
    assert all(len(row) <= width for row in term.rows), term.rows


# --- CLI, subprocess: the JSON path is untouched -------------------------


def test_json_format_stdout_carries_no_heartbeat_bytes(project):
    """`tan build --execute --format json`: stdout must still be EXACTLY one
    JSON document (the actual, pre-existing contract `envelope_of` checks)
    and, spelled out for this ticket, none of the heartbeat's own vocabulary
    or control bytes -- proving `on_output=_Heartbeat(...)` never reaches
    stdout regardless of what the (disabled, non-TTY-here) heartbeat would
    have printed on a real terminal. `two_slice_plan` is already `baremetal`
    (tan-cli#309: a `zephyr` backend here would trip the Zephyr-boilerplate
    guard, since the probe command never produces real Zephyr CMake evidence),
    which is all this test needs -- it asserts stdout framing only, and
    Zephyr-ness is incidental to that."""
    plan = write_plan(project, two_slice_plan(ALL_ARTEFACTS))
    proc = run_tan(
        "build", "--plan-from", str(plan), "--execute", "--format", "json", cwd=project
    )

    env = envelope_of(proc)  # json.loads on the WHOLE of stdout
    assert proc.returncode == 0, env
    assert "\r" not in proc.stdout
    assert "still building" not in proc.stdout
    # `Envelope.to_json` is compact (`separators=(",", ":")`, no indent) plus
    # the one trailing newline `print()` adds -- exactly one line, byte for
    # byte, nothing appended or interleaved around it.
    #
    # `ensure_ascii=False` mirrors `Envelope.to_json`: the oracle emits raw
    # UTF-8 (an em dash goes out as `e2 80 94`), so the port stopped escaping
    # it to `—`. This expectation has to be built the way production
    # builds it or the test measures `json.dumps`'s DEFAULT rather than what
    # tan actually wrote -- which is what it was doing, and why it reddened on
    # a message carrying an em dash rather than on any framing change.
    assert proc.stdout.count("\n") == 1
    assert proc.stdout == json.dumps(env, separators=(",", ":"), ensure_ascii=False) + "\n"
