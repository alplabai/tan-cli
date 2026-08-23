# SPDX-License-Identifier: Apache-2.0
"""`doctor_cmd.on_path` IS `tool_lookup.resolve_tool` (tan-cli#532).

`on_path` was the last of the five hand-rolled `%PATH%` walks #532 was opened
about, and `faultdecode_cmd` reaches the lookup through it, so the two retire
together. What this module pins is not the walk -- `tool_lookup` owns that and
`test_bare_argv0_spawn.py` already covers it -- but the DELEGATION, because
the failure #532 exists to prevent is somebody re-growing a second copy here
later. tan-cli#510 is what that costs: an availability check and the spawn it
was protecting drifted into disagreeing about what they were protecting
against.

A behavioural-only test cannot carry that. On POSIX both implementations agree
by construction (`exts` was `[""]`, which is what `resolve_tool` does), and the
arm where they genuinely differ -- Windows, where the old copy accepted a bare
extension-less `%PATH%` file ahead of every suffixed sibling -- is not the
question this file asks. Hence one call-through assertion plus real-filesystem
agreement, rather than a re-run of the walk.

The reason recorded here until tan-cli#811 was that the Windows branch is
"unreachable from Linux CI: `resolve_tool`'s Windows branch builds a `Path`,
which dispatches on `os.name` at construction, so patching `os.name` raises
rather than reaching it". That was false in both halves -- `Path(str)` under a
patched `os.name` constructs fine, and the stat methods answer `False` rather
than raising -- and it is now moot: the walk builds its hit with
`PureWindowsPath` and IS driven from POSIX by
`test_bare_argv0_spawn.py::test_the_windows_walk_returns_the_pathlib_spelling
_not_the_join_spelling`. The choice made here is still the right one, but it
is a choice about what THIS file pins, not a platform barrier.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tan.commands import doctor_cmd
from tan.core.tool_lookup import resolve_tool


def _executable(directory: Path, name: str) -> Path:
    """A file `resolve_tool` will actually find, on either platform.

    `.exe` on Windows, mirroring `test_bare_argv0_spawn.py::_executable` --
    and for the reason THIS module is about. `resolve_tool` never tries the
    bare, extension-less name on Windows
    (`tool_lookup.windows_candidate_names`), so a fixture written as
    `alp-probe-tool` is exactly the file the consolidation refuses to
    resolve: correct behaviour, and it made the first revision of this
    module fail on `windows-latest` while passing everywhere else. The
    lookup is still asked for the BARE name -- supplying the suffix is its
    job, not the caller's.

    On POSIX the executable bit is what makes it a hit (`os.access(...,
    os.X_OK)`); `chmod` is a no-op for that purpose on Windows, where
    `os.access` reports any existing file as executable.
    """
    path = directory / (f"{name}.exe" if os.name == "nt" else name)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _resolves_to(answer: str | None, expected: Path) -> bool:
    """Whether `answer` names the SAME FILE as `expected` -- not the same
    string.

    Second Windows lesson from this module (tan-cli#532). `resolve_tool`
    builds its candidate from `%PATHEXT%`, whose default is UPPERCASE
    (`.COM;.EXE;.BAT;.CMD`), and returns `str(Path(directory) / name)`. So
    the answer carries `alp-probe-tool.EXE` while the fixture on disk is
    `alp-probe-tool.exe`: `Path.is_file()` matches, because the filesystem is
    case-insensitive, and a `==` on the two strings does not. That is not a
    defect in the lookup -- the path it returns opens the intended file, which
    is the whole contract -- so the assertion is the thing that has to state
    identity the way the platform means it.

    `os.path.samefile` rather than a `.lower()` comparison: it answers the
    question actually being asked ("did the probe find MY fixture"), and it
    keeps working if a future `%PATHEXT%` change alters the casing again or a
    caller starts returning a resolved/short-form path.
    """
    if answer is None:
        return False
    return os.path.samefile(answer, expected)


def test_on_path_delegates_to_the_one_lookup(monkeypatch):
    """The anti-second-copy assertion: `on_path` must CALL `resolve_tool`,
    not merely happen to agree with it.

    Fails the moment anyone re-hand-rolls the walk in `doctor_cmd`, which is
    the regression #532 is about -- and which no agreement-based test can
    catch, since a fresh copy would agree on POSIX on the day it was written
    and drift later, exactly as the five copies did.
    """
    calls: list[tuple[str, str | None]] = []

    class _Answer:
        resolved = "/sentinel/west"

    def _spy(tool, env):
        calls.append((tool, env.get("PATH")))
        return _Answer()

    monkeypatch.setattr(doctor_cmd, "resolve_tool", _spy)
    monkeypatch.setenv("PATH", "/sentinel-path")

    assert doctor_cmd.on_path("west") == "/sentinel/west", (
        "on_path did not return resolve_tool's `resolved` verbatim"
    )
    assert calls == [("west", "/sentinel-path")], (
        "on_path did not route through tool_lookup.resolve_tool -- a second "
        f"hand-rolled %PATH% walk is back in doctor_cmd (calls: {calls})"
    )


def test_on_path_reads_the_live_environment_not_a_snapshot(monkeypatch, tmp_path):
    """`on_path` is the doctor's HOST probe, so it must see the PATH in force
    at CALL time. A module-scope capture of `os.environ["PATH"]` would satisfy
    the delegation test above and still report a stale host.
    """
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    # The helper's RETURN value, not a re-spelled path: on Windows the file on
    # disk is `alp-probe-tool.exe` and that is what resolves.
    tool = _executable(second, "alp-probe-tool")

    monkeypatch.setenv("PATH", str(first))
    assert doctor_cmd.on_path("alp-probe-tool") is None

    monkeypatch.setenv("PATH", os.pathsep.join([str(first), str(second)]))
    assert _resolves_to(doctor_cmd.on_path("alp-probe-tool"), tool)


def test_on_path_and_resolve_tool_cannot_answer_differently(monkeypatch, tmp_path):
    """Hit and miss, against a real directory, through both entry points.

    Weaker than the delegation test on its own -- two copies agree until they
    don't -- but it is what proves the delegation is WIRED CORRECTLY rather
    than merely present: a call-through that dropped `env`, or that returned
    `searched` instead of `resolved`, passes the spy above and fails here.
    """
    _executable(tmp_path, "alp-probe-tool")
    monkeypatch.setenv("PATH", str(tmp_path))

    assert doctor_cmd.on_path("alp-probe-tool") == resolve_tool(
        "alp-probe-tool", os.environ).resolved
    assert doctor_cmd.on_path("alp-absent-tool") is None
    assert resolve_tool("alp-absent-tool", os.environ).resolved is None


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX-only by nature, not by convenience: Windows has no execute "
           "bit, and `os.access(path, os.X_OK)` reports every existing file as "
           "executable there, so the property under test does not exist on "
           "that platform. Run unskipped it would pass for an unrelated reason "
           "-- `resolve_tool` never tries the bare extension-less name on "
           "Windows -- which is worse than a skip: a green case that measures "
           "nothing.",
)
def test_a_non_executable_file_on_path_is_not_a_hit(monkeypatch, tmp_path):
    """The property the old copy enforced with `os.access(..., os.X_OK)`, kept
    across the consolidation. Without it a `README` beside the tools would
    answer the probe.
    """
    (tmp_path / "alp-probe-tool").write_text("not executable\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path))

    assert doctor_cmd.on_path("alp-probe-tool") is None


def test_an_unreadable_path_entry_does_not_fail_the_probe(monkeypatch, tmp_path):
    """The old copy swallowed `OSError` per PATH entry ("a dead network share,
    a name too long for the filesystem: skip the entry, never fail the
    command"). A doctor that raises while REPORTING on a broken host is the
    worst possible moment to raise, so the property is pinned here rather than
    left to `tool_lookup`'s own tests.
    """
    good = tmp_path / "good"
    good.mkdir()
    tool = _executable(good, "alp-probe-tool")
    missing = tmp_path / "does-not-exist"
    long_name = tmp_path / ("x" * 300)

    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(missing), str(long_name), "", str(good)]))
    assert _resolves_to(doctor_cmd.on_path("alp-probe-tool"), tool)
