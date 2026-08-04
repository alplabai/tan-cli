# SPDX-License-Identifier: Apache-2.0
"""The ONE frozen-Rust-oracle resolver for every module under
``tests/commands/`` (tan-cli#393).

``test_pinmux_command.py`` and ``test_diff_command.py`` each carried a
private, byte-identical ``_oracle_binary()`` that tried ``target/release``
before ``target/debug`` UNCONDITIONALLY, and asserted nothing about what it
found. So a ``target/release/tan`` left over from an earlier build beat a
freshly rebuilt ``target/debug/tan`` every time, and both files'
``..._is_a_known_divergence_from_the_oracle`` cases -- the only reason either
file spawns a binary at all -- silently measured this port against a
year-old ``tan 0.3.1`` and PASSED. A parity test that passes against the
wrong oracle is worse than no parity test: it certifies a divergence it
never looked at.

Resolution is NOT re-implemented here. ``tests/parity/oracle.py`` already
fixed this exact bug for the parity suite -- newest-by-``st_mtime`` rather
than release-first, an mtime TIE refused outright, and a set-but-missing
``TAN_RUST_BINARY`` raised rather than silently falling back -- and
duplicating that rule a third time is how the two copies this module deletes
came to exist. This is a thin adapter over :func:`oracle.rust_binary`, so the
rule and the pin have exactly one home.

Importable as ``tests.parity.oracle`` because ``python/pyproject.toml``'s
``[tool.pytest.ini_options] pythonpath = ["."]`` puts ``python/`` on
``sys.path`` for every invocation (verified from both ``python/`` and the
repo root -- pytest resolves rootdir to ``python/`` either way). pytest's own
prepend import mode ALSO imports that file as ``parity.oracle`` when it
collects ``tests/parity/``, so this import creates a second, inert module
object: nothing here touches ``oracle_fixtures``' mutable capture/replay
state (``_counters``), and no capture run ever routes through this module.
Do not add a call to ``oracle.compare``/``oracle.rust_run`` here without
resolving that first -- those DO drive the fixture store.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.parity.oracle import PINNED_ORACLE_VERSION, rust_binary

#: Resolved once, at import. :func:`oracle.rust_binary` RAISES rather than
#: returning ``None`` for the two ways an operator can name an oracle wrongly
#: (a ``TAN_RUST_BINARY`` that does not exist; an mtime tie between the two
#: profiles), and letting that raise reach collection is deliberate -- both
#: are configuration errors whose message names the cause, and this file
#: existing at all is a response to those errors being SILENT before. ``None``
#: means only "nobody built one and nobody named one", which is the ordinary
#: developer case and stays a skip.
ORACLE: str | None = rust_binary()

#: Absence -> skip. Wrongness -> fail, but that verdict belongs in
#: :func:`run_oracle` and not here: a `skipif` cannot fail, and spawning the
#: binary at import time would turn a wrong-version box into a collection
#: ERROR that also takes the ~80 tests in these files that never touch the
#: oracle down with it. Same absence/wrongness split
#: ``tests/parity/conftest.py``'s ``pinned_oracle`` fixture draws.
ORACLE_REQUIRED = pytest.mark.skipif(
    ORACLE is None,
    reason="needs a built Rust tan (cargo build --bin tan) to measure the divergence",
)

#: Set on the first :func:`run_oracle` call, so the ``--version`` probe costs
#: one subprocess per session rather than one per oracle-backed test.
_verified: str | None = None


def _verify_pinned_version(oracle: str) -> None:
    """FAIL loudly when the resolved binary is not the pinned oracle.

    This is the half neither hand-copy had. Resolution alone answers "which
    file", never "is that file the thing this suite's expected values were
    measured against" -- and the whole tan-cli#393 defect was a resolved
    binary that ran fine, emitted well-formed JSON, and answered for a
    different version of tan. An assertion, not a skip, for the reason
    ``tests/parity/conftest.py`` gives at length: a quiet skip on wrongness
    hides exactly the gap this check exists to surface.
    """
    global _verified
    if _verified == oracle:
        return
    proc = subprocess.run(
        [oracle, "--version"], capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, f"{oracle} is not a working tan binary"
    reported = proc.stdout.strip()
    assert reported == PINNED_ORACLE_VERSION, (
        f"resolved oracle {oracle!r} reports {reported!r}, not the pinned "
        f"{PINNED_ORACLE_VERSION!r} these divergence cases were measured "
        "against. rust_binary() picks the MOST RECENTLY BUILT of "
        "target/{release,debug}/tan, so a resolved-but-wrong binary means "
        "either an explicit TAN_RUST_BINARY naming the wrong one or a stale "
        "profile that was rebuilt more recently than the one you meant. "
        "Rebuild or remove the stale profile, or set TAN_RUST_BINARY=<path> "
        "explicitly."
    )
    _verified = oracle


def run_oracle(argv: list[str], cwd: Path) -> tuple[int, dict]:
    """Spawn the pinned oracle on ``argv`` and return ``(exit code, envelope)``.

    The version check runs HERE rather than at import so it is impossible to
    reach a divergence assertion without it having run -- every oracle-backed
    case in this package goes through this function.
    """
    assert ORACLE is not None, "run_oracle() called with no oracle resolved"
    _verify_pinned_version(ORACLE)
    proc = subprocess.run(
        [ORACLE, *argv], capture_output=True, text=True, encoding="utf-8", cwd=cwd
    )
    return proc.returncode, json.loads(proc.stdout)
