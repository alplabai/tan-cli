# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1109 fault 1, proved against the REAL incident range.

`test_planner_resync.py`'s own module docstring explains why its tests build a
miniature alp-sdk repo rather than bind a real checkout -- exactly the
coupling-to-whatever-alp-sdk-is-doing-today the freshness gate exists to make
explicit. This file is the deliberate exception: it does not test the
classifier in general, it proves ONE fixed, historical claim tan-cli#1109
makes about ONE fixed alp-sdk range --

    $ gh api repos/alplabai/alp-sdk/compare/0914da38...5c33ef04 \\
        --jq '.files[]?|select(.filename|startswith("scripts/alp_orchestrate/"))'
    (no output)

-- the exact range that produced PRs #1106, #1107 and #1108. Anchored to that
range's alp-sdk SHAs (never `origin/dev`, never "whatever HEAD is now") and to
the `tan/planner/` gate file EXACTLY as it read right after PR #1103 merged
(`git show <commit>:...`, not this repo's own live, ever-advancing
`PINNED_SDK_COMMIT`) so the proof stays meaningful no matter how many
legitimate re-syncs land on `dev` after this file does.

Skipped, not failed, without a bound alp-sdk checkout -- same convention as
every other real-SDK-gated test in this suite (`tests.conftest.sdk_root()`).
Bind `ALP_SDK_ROOT` at a checkout with `0914da38ebbecac3c1546064dd506f7fafe0bfa7`
and `5c33ef046670029e59f013b65e4aaae8f03fc5be` (and their shared history back to
`26b0040e9a762c16aff5c7c53b2e19cc7583b2a4`, `STRICT_LOADERS_PINNED_SDK_COMMIT`
at that same historical gate revision) both reachable to run it for real.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pytest

from tests.conftest import sdk_root

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "python" / "scripts" / "planner_resync.py"
GATE_REL = "python/tests/gates/test_planner_relocation_freshness.py"

#: The tan-cli commit whose `test_planner_relocation_freshness.py` pins
#: `PINNED_SDK_COMMIT` at `0914da38ebbecac3c1546064dd506f7fafe0bfa7` -- the
#: state of `dev` right after PR #1103 merged, and the exact base tan-cli#1109
#: measures "zero mirrored files changed" against. A fixed historical commit,
#: not this repo's live gate file: the live one moves every time a real
#: re-sync lands, and this proof must not start silently failing (or, worse,
#: silently proving a DIFFERENT range) when that happens.
_TAN_CLI_GATE_COMMIT = "0d6f13611f1b7978943727a5ac13cf7c6edb3278"
_MIRROR_BASE = "0914da38ebbecac3c1546064dd506f7fafe0bfa7"
_TARGET = "5c33ef046670029e59f013b65e4aaae8f03fc5be"

SDK = sdk_root()


def _load():
    spec = importlib.util.spec_from_file_location("planner_resync", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pr = _load()


@pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT (or ALP_SDK_PARITY_ROOT) is not set -- no bound "
    "alp-sdk checkout to prove tan-cli#1109's own reproduction range against.",
)
def test_the_0914da38_to_5c33ef04_range_proves_zero_mirror_changes_and_no_pr():
    """The literal case that produced #1106/#1107/#1108: with the gate file
    as it read right after PR #1103 merged, re-syncing to `5c33ef04` must
    show every `scripts/alp_orchestrate/` module unchanged, must not move the
    mirror pin, and `apply()` must write nothing -- the shape
    `planner-resync.yml`'s "Open or refresh the proposal PR" step reads as
    "Nothing to propose" and therefore never pushes a branch or opens a PR."""
    gate_proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{_TAN_CLI_GATE_COMMIT}:{GATE_REL}"],
        capture_output=True, check=True, text=True,
    )
    gate = pr.parse_gate(gate_proc.stdout)
    assert gate.pinned_sdk_commit == _MIRROR_BASE, (
        "this fixture pin drifted from the historical commit it names -- "
        "re-derive _TAN_CLI_GATE_COMMIT rather than silently proving a "
        "different range than tan-cli#1109's own reproduction"
    )

    rep = pr.classify(SDK, REPO_ROOT, gate, _TARGET)

    changed_mirror = [v for v in rep.mirror if v.status != "unchanged"]
    assert changed_mirror == [], (
        "expected ZERO changed scripts/alp_orchestrate/ files in this exact "
        f"range (the `gh api compare` measurement tan-cli#1109 cites) -- got "
        f"{[(v.path, v.status) for v in changed_mirror]}"
    )
    assert rep.mirror_moves is False

    # apply() is safe to call against the real REPO_ROOT here: it only ever
    # writes a mirror file when `v.status == "merged"` (none are, above) and
    # only ever rewrites the gate file when `mirror_moves or hand_port_moves`
    # (mirror_moves is False, and hand_port_moves requires it) -- so this
    # call is a documented no-op, not a real write against this checkout.
    touched = pr.apply(REPO_ROOT, gate, rep)
    assert touched == [], (
        "no file may be written for this range -- a non-empty `touched` is "
        "exactly what `git status --porcelain -- python/` being non-empty "
        "means to `planner-resync.yml`'s PR step, which is what tan-cli#1109 "
        "says must never happen when zero mirrored files changed"
    )
