# Copyright 2026 Alp Lab AB
# SPDX-License-Identifier: Apache-2.0
"""No tracked file may contain an unresolved git conflict marker.

On 2026-08-13 `python/tests/gates/MODULE_SIZE_BUDGET_LOG.md` reached `dev`
carrying `<<<<<<< HEAD` / `=======` / `>>>>>>> origin/dev` at lines 53-65, via
the squash of PR #702. Three separate merges that day committed markers; this
was the one that landed on a protected branch.

Nothing caught it. `test_module_size_budget.py` parses the `.json`, not the
`.md` ledger beside it, so its gate passed on the very PR that broke the file.
Every other gate reads a specific format and never looks for this.

The check is deliberately repo-wide and format-agnostic: a marker is a marker
whether it lands in Python, Markdown, JSON, CMake or YAML, and the three files
that carried one this day were a `.md`, a `.json` and a `CMakeLists.txt`.

A marker at the START OF A LINE is the signal. Prose that merely mentions the
strings inline -- this docstring, `docs/` explaining a merge -- is not matched,
which is why the patterns are anchored.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

#: Anchored: only a real marker sits at column 0.
_MARKERS = ("<<<<<<< ", ">>>>>>> ")
_SEPARATOR = "======="

#: Files that legitimately contain anchored marker text as DATA, not as an
#: unresolved conflict. Keep this list short and justified; an entry here is a
#: hole in the check.
_ALLOWED = {
    # This file's own docstring quotes the markers, but indented -- so it needs
    # no exemption. Left empty deliberately: if you are adding to it, ask first
    # whether the file could indent its examples instead.
}


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO,
        capture_output=True, text=True, check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


def test_the_repo_has_tracked_files():
    """Guard against `git ls-files` returning nothing and this gate passing
    vacuously -- the exact failure mode it exists to prevent."""
    assert len(_tracked_files()) > 100


def test_no_tracked_file_carries_an_unresolved_conflict_marker():
    offenders: list[str] = []
    for rel in _tracked_files():
        if rel in _ALLOWED:
            continue
        path = REPO / rel
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: no textual marker to find
        for n, line in enumerate(text.splitlines(), 1):
            if line.startswith(_MARKERS) or line == _SEPARATOR:
                offenders.append(f"{rel}:{n}: {line[:60]}")
                break

    assert not offenders, (
        "unresolved git conflict marker in a tracked file -- this reached `dev` "
        "once already (PR #702, MODULE_SIZE_BUDGET_LOG.md:53-65) because the "
        "gate beside it parses the .json, not the .md:\n  "
        + "\n  ".join(offenders)
    )
