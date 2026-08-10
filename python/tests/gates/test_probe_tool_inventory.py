# SPDX-License-Identifier: Apache-2.0
"""Gate: the debug/flash probe inventory is a property of the TEST, not of
the host running it (tan-cli#603).

A test that probes for `JLinkExe`/`openocd`/`pyocd`/`west` resolves the REAL
binary on a bench host that genuinely has them installed, and takes a
different branch than the same test takes on a CI runner that does not. A
local green bar then does not mean what it appears to mean.

That is not hypothetical. tan-cli#600 turned `python -m pytest` red on
ubuntu-latest, windows-latest AND macos-latest -- seven `test_flow_d_
preflight_*` cases in `tests/commands/test_flash_command.py`, every one of
them reporting that the J-Link binary "could not be resolved to a real
location on PATH or in the workspace venv; refusing to write MRAM without
confirming which board is attached" -- while the identical suite on the
bench host passed, because that host has `/usr/bin/JLinkExe`,
`/usr/bin/openocd` and `~/.local/bin/pyocd` for real. Three platforms red,
one developer green, and the green one is the machine the change was written
on.

## The measurement this gate was built from

Instrumenting tan's three resolution seams (`doctor_cmd.on_path`,
`tool_lookup.resolve_tool`, `shutil.which`) across a full suite run on the
bench host's own PATH, and again across a run whose PATH held everything
EXCEPT the probe tooling, found **34 tests taking a different branch** --
every `_collect` case in `tests/commands/test_doctor_command.py`, three in
`tests/gates/test_doctor_check_scope.py`, and `tests/commands/
test_support_bundle_command.py::test_the_real_collect_produces_the_oracle_
shaped_check_list`. Their assertions happen not to cover the check whose
answer changed, so the OUTCOME was identical (3710 passed both ways) while
the MEANING was not. Those tests still spawn `/usr/bin/JLinkExe -?` and
`west --version` for real on that host, which is the same defect seen from
the other side.

`conftest.py`'s `_probe_tools_are_a_property_of_the_test` fixture is the fix:
it rebuilds PATH once per session so no probe identity resolves, matching what
CI already exercises. This module is what stops that fixture rotting into a
no-op.

## Why this is not "just empty the PATH"

`tests/conftest.py::empty_tool_inventory` builds a PATH under which
EVERYTHING is absent, and it is the right tool for a single test that wants
that answer. It is the wrong tool for the whole suite: measured on this same
tip, running the full suite under a PATH holding only `sh bash git which env
ls cat uname` produces **36 failures** that have nothing to do with probe
tooling -- `tests/installers/test_installer_release_layout.py` (22) needs
real `mktemp`/`sed`/`curl`/`tar`/`sha256sum` for the installer shell scripts
it executes, `tests/commands/test_generate_command.py` and
`tests/commands/test_doctor_command.py` need a real `python3`, and
`test_spawn_timeout_path_returns_promptly_despite_an_orphaned_pipe_holder`
needs a real `sleep`. A CI runner HAS all of those. So the honest
neutralisation is surgical -- remove the probe tooling a runner lacks, keep
the ordinary host tooling a runner has -- and `test_ordinary_host_tooling_
is_untouched` below is what holds it to that.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from tan.commands import doctor_cmd
from tests.conftest import PROBE_TOOLS


@pytest.mark.parametrize("tool", sorted(PROBE_TOOLS))
def test_no_probe_tool_resolves_from_the_inherited_path(tool):
    """Neither of the two resolvers tan actually probes through finds a
    debug/flash probe identity on the PATH this suite inherited.

    `on_path` AND `shutil.which` both, because they are different walks --
    `on_path` excludes the cwd by hand, `shutil.which` does not -- and code
    under test uses each in different places."""
    assert doctor_cmd.on_path(tool) is None, (
        f"`{tool}` resolves to {doctor_cmd.on_path(tool)} from this suite's PATH. "
        "Every probe-gated test on this host is therefore taking the "
        "tool-PRESENT branch while CI takes the tool-ABSENT one, silently "
        "(tan-cli#603). Nothing about the host is wrong -- the fixture in "
        "tests/conftest.py that neutralises this has stopped working."
    )
    assert shutil.which(tool) is None, f"`{tool}` resolves via shutil.which: {shutil.which(tool)}"


def test_a_spawned_child_sees_the_same_absent_inventory():
    """Covers the OTHER way a probe reaches the host: 26 test modules spawn
    `[sys.executable, "-m", "tan", ...]`, and a child resolves through its
    own inherited `PATH`, not through anything patched in this process. A
    fix applied only in-process would leave every one of those spawns
    host-dependent."""
    probe = "; ".join(
        [
            "import shutil, json, sys",
            f"names = {sorted(PROBE_TOOLS)!r}",
            "print(json.dumps({n: shutil.which(n) for n in names}))",
        ]
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    import json

    found = {k: v for k, v in json.loads(out.stdout).items() if v is not None}
    assert found == {}, (
        f"a spawned child still resolves probe tooling: {found}. The PATH the "
        "fixture rebuilt is not reaching subprocesses (tan-cli#603)."
    )


def test_ordinary_host_tooling_is_untouched():
    """The neutralisation is surgical, not a blanket empty PATH.

    Measured: a suite run under a PATH stripped to `sh bash git which env ls
    cat uname` fails 36 tests that need `python3`, `sleep`, `mktemp`, `sed`,
    `curl`, `tar` and `sha256sum` -- all of which a CI runner has. Stripping
    those would not make the suite honest, it would make it wrong in a new
    direction. This is the pin that says so."""
    required = ["git", "python3"] if os.name != "nt" else ["git"]
    missing = [t for t in required if doctor_cmd.on_path(t) is None]
    assert missing == [], (
        f"ordinary host tooling {missing} no longer resolves. The probe-tool "
        "neutralisation has over-reached into tools a CI runner genuinely has "
        "(tan-cli#603) -- it must remove the probe identities only."
    )


def test_a_test_that_wants_a_probe_tool_seeds_its_own(tmp_path, monkeypatch):
    """The point of the fix, stated as a test: a case that NEEDS a probe tool
    present says so itself, by seeding one and pointing PATH at it -- the
    shape `test_flash_command.py::_stub_flow_d_probe` already uses. The
    inventory is then a property of the test, readable in the test, and
    identical on every host."""
    tools = tmp_path / "seeded"
    tools.mkdir()
    seeded = tools / ("openocd.exe" if os.name == "nt" else "openocd")
    seeded.write_text("", encoding="utf-8")
    if os.name != "nt":
        os.chmod(seeded, 0o755)

    assert doctor_cmd.on_path("openocd") is None  # absent until the test says otherwise
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    assert doctor_cmd.on_path("openocd") == str(seeded)


def test_the_probe_tool_list_covers_what_the_600_regression_named():
    """The four identities tan-cli#603 names verbatim stay in the list. A
    future edit that trims it back is free to argue for the trim, but not to
    make it silently -- these are the ones a real regression escaped on."""
    for tool in ("JLinkExe", "openocd", "pyocd", "west"):
        assert tool in PROBE_TOOLS, (
            f"`{tool}` dropped out of PROBE_TOOLS -- tan-cli#603's own "
            "reproduction turned on exactly this identity resolving locally "
            "and not on CI."
        )
