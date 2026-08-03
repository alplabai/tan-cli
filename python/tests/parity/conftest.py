# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for every test module under ``tests/parity/``.

Home of the one check that used to live inside ``test_oracle_parity.py``
alone, opted into by a handful of cases there and inherited by nobody else:
proof that whatever ``target/{release,debug}/tan`` :func:`oracle.rust_binary`
resolved is actually the pinned oracle this whole suite is measured against,
not a stale profile silently standing in for it.

``test_flash_oracle_parity.py``, ``test_image_size_oracle.py``,
``test_clean_parity.py`` and ``test_run_oracle_parity.py`` each bind
``RUST = rust_binary()`` at import time and gate their live cases on
``missing_for_live`` (an ABSENCE check only) with no version assertion of
their own. Under a live run (``TAN_PARITY_LIVE=1``) against a resolved-but-
wrong binary, every one of those files' failures reads as a wall of PORT
bugs, with nothing naming the oracle itself as the actual problem -- measured:
``TAN_PARITY_LIVE=1 TAN_RUST_BINARY=<the stale 0.1.1 build>`` over just
``test_image_size_oracle.py`` + ``test_clean_parity.py`` produced "108
failed, 8 passed, 9 skipped, 2 xfailed", eight of those passes measured
against the wrong oracle entirely.

Fixing this per-file (each module opting into its own copy of the check, the
way ``test_oracle_parity.py``'s old ``pinned_oracle`` did) would need to land
in four places every time the pin changes. A session-scoped, autouse fixture
here is inherited by every module in this directory for free, including
``test_oracle_parity.py`` itself, which no longer defines its own copy.
"""
import subprocess

import pytest

from .oracle import PINNED_ORACLE_VERSION, rust_binary

@pytest.fixture(scope="session", autouse=True)
def pinned_oracle() -> None:
    """FAIL the session -- never skip -- when a resolved oracle binary is
    present but does not report :data:`oracle.PINNED_ORACLE_VERSION`.

    Absence stays a skip: that is what each file's own ``missing_for_live``/
    ``_ORACLE_REQUIRED`` gate already decides, and this fixture defers to it
    entirely by doing nothing when ``rust_binary()`` returns ``None``. Only WRONGNESS
    fails here -- a resolved binary that answers the wrong ``--version`` --
    because a quiet skip in that case would hide exactly the gap this
    harness exists to surface (``missing_for_live``'s own docstring makes the
    identical argument for the absence case; tan-cli#272 is where that rule
    was first written down, and it applies just as hard to wrongness as to
    absence).

    Session-scoped so this runs the check ONCE per test process rather than
    once per test case; the resolution cannot change mid-session, so
    re-checking it per-test bought nothing but 17 extra ``--version``
    subprocesses a run.

    ``rust_binary()`` is called HERE, in the fixture body, and deliberately
    NOT at this conftest's import. It can raise -- a ``TAN_RUST_BINARY`` that
    is set but missing, or the mtime TIE between target/{release,debug} that
    it now refuses to break silently -- and a raise at conftest import is an
    ``ImportError while loading conftest``, which aborts the WHOLE pytest
    session rather than this directory. Measured: with a bogus
    ``TAN_RUST_BINARY``, ``pytest tests/parity tests/core`` collected zero
    tests and exited rc=4, so the repo's own ``python -m pytest tests -q``
    gate would have reported nothing at all. Resolving in the fixture keeps
    the blast radius on ``tests/parity``, which is what it is about.
    """
    rust = rust_binary()
    if rust is None:
        return
    proc = subprocess.run([rust, "--version"], capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 0, f"{rust} is not a working tan binary"
    stdout = proc.stdout.strip()
    assert stdout == PINNED_ORACLE_VERSION, (
        f"resolved oracle {rust!r} reports {stdout!r}, not the pinned "
        f"{PINNED_ORACLE_VERSION!r} this whole parity suite is measured "
        "against. oracle.rust_binary() picks the MOST RECENTLY BUILT of "
        "target/{release,debug}/tan, so a resolved-but-wrong binary means "
        "either an inverted or TIED mtime between the two profiles (a tie "
        "raises inside rust_binary() itself -- see that function) or an "
        "explicit TAN_RUST_BINARY naming the wrong one. Rebuild or remove "
        "the stale profile, or set TAN_RUST_BINARY=<path to the correct "
        "build> explicitly."
    )
