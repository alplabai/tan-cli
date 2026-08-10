# SPDX-License-Identifier: Apache-2.0
"""Seeds one real, on-disk file per [`PROBE_TOOLS`] identity onto `PATH`, at
pytest COLLECTION time -- strictly before `tests/conftest.py`'s
session-scoped `_probe_tools_are_a_property_of_the_test` fixture ever runs
(tan-cli#603/#617 follow-up review, the `test_probe_tool_inventory.py`
minor).

## The gap this closes

Every runner this suite has ever executed on genuinely lacks JLinkExe /
openocd / pyocd / west / etc -- that is the entire premise of tan-cli#603.
Which means `tests/conftest.py::_probe_free_path` is a documented NO-OP on
every one of them: nothing on the real PATH ever matches, so nothing is ever
farmed. `test_probe_tool_inventory.py`'s fourteen `test_no_probe_tool_
resolves_from_the_inherited_path` cases (one per identity) therefore passed
identically whether the farming logic worked, was broken, or did not exist at
all -- there was never a real probe tool on PATH for it to have farmed away.
A gate that cannot fail is not a gate (the exact tan-cli#485 lesson, aimed at
this file instead of `test_planner_relocation_freshness.py`).

This module makes the farming logic ACTUALLY RUN, for real, every time this
directory's tests execute: a canary file for every [`PROBE_TOOLS`] identity,
written to a scratch directory and prepended to `PATH` during COLLECTION --
which pytest always finishes, for every conftest.py and test module in the
run, before any fixture SETUP begins. That ordering is what makes this
reliable without depending on file-name alphabetics or fixture-declaration
order: `_probe_tools_are_a_property_of_the_test` re-reads `os.environ["PATH"]`
fresh when IT runs (first test execution, after collection), so it always
sees this canary directory already sitting on PATH, farms it like any other
dirty entry, and by the time this directory's own tests run the canary is
gone -- a REAL, falsifiable answer instead of a vacuous one.

## What this does NOT fix

`.github/workflows/parity.yml`'s `python-tests` matrix -- the only job that
runs windows-latest/macos-latest at all -- passes `--ignore=tests/gates` on
every OS; `.github/workflows/ci.yml`'s `python` job runs the full suite
(gates included) on `ubuntu-latest` only. So this file, seed included, still
has exactly ONE runner that ever executes it. That is a CI-topology gap, not
a fixture gap, and is disclosed rather than silently worked around here --
see `test_probe_tool_inventory.py`'s own module docstring.
"""
from __future__ import annotations

import atexit
import os
import shutil
import stat
import tempfile

from tests.conftest import PROBE_TOOLS

_seed_dir = tempfile.mkdtemp(prefix="tan-gates-probe-seed-")
atexit.register(shutil.rmtree, _seed_dir, ignore_errors=True)

for _name in sorted(PROBE_TOOLS):
    _file = os.path.join(_seed_dir, f"{_name}.exe" if os.name == "nt" else _name)
    with open(_file, "w", encoding="utf-8"):
        pass
    if os.name != "nt":
        os.chmod(_file, stat.S_IRWXU | stat.S_IRGRP | stat.S_IROTH)

os.environ["PATH"] = _seed_dir + os.pathsep + os.environ.get("PATH", "")
