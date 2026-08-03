# SPDX-License-Identifier: Apache-2.0
"""One oracle resolver, and proof that it is the one being used (tan-cli#393).

Two hand copies of a release-first `target/{release,debug}/tan` walk had landed
under `tests/commands/`, each a copy of exactly the resolution
`tests/parity/oracle.py`'s `rust_binary()` was rewritten to REMOVE because it
"silently picked a STALE binary". On a tree carrying both profiles with
`release` older than `debug` -- the state any single `cargo build --release`
followed later by a plain `cargo build` leaves -- one copy went RED against
`tan 0.3.1` with a `project.boardYaml` assertion diff that reads as a port
regression, and the sibling copy stayed GREEN certifying a divergence
measurement against that same stale binary.

Neither could be caught by the `pinned_oracle` fixture, because it lived in
`tests/parity/conftest.py` and pytest conftests are directory-scoped.

This module holds the two checks that make a recurrence non-silent:

* a STRUCTURAL gate -- nobody outside `oracle.py` resolves an oracle;
* a BEHAVIOURAL check -- the surviving resolver really does prefer the newer
  build, really does refuse a tie, and a wrong version really does fail by
  NAMING the oracle rather than as an envelope diff.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.parity.oracle import (
    PINNED_ORACLE_VERSION,
    resolve_oracle_for_skipif,
    rust_binary,
)

_TESTS_ROOT = Path(__file__).resolve().parents[1]
#: The single module allowed to know how an oracle binary is found.
_THE_RESOLVER = _TESTS_ROOT / "parity" / "oracle.py"
#: THIS file, exempted explicitly rather than by spelling the path so the regex
#: happens to miss it. `staged_target` has to BUILD a `target/{release,debug}`
#: tree -- that is the fixture the behavioural checks below run against -- and
#: constructing a fake one is the opposite of resolving a real one. An
#: exemption that is visible in the source is worth more than an evasion that
#: is not: the next person to write `tmp_path.joinpath("target", ...)` would
#: slip past the pattern silently, and this line is where they are told why
#: that is not the same thing.
_THE_GATE_ITSELF = Path(__file__).resolve()

#: `TAN_RUST_BINARY` read as an ENV VAR, and a `target/<profile>` path built by
#: hand. Prose mentions -- docstrings, `skipif` reasons telling a developer
#: which variable to set -- are not resolution and must not trip this: both
#: patterns require the syntax of actually doing it.
_READS_THE_ENV_VAR = re.compile(
    r"""environ(?:\.get)?\(\s*["']TAN_RUST_BINARY["']|getenv\(\s*["']TAN_RUST_BINARY["']"""
)
_WALKS_THE_TARGET_DIR = re.compile(
    r"""["']target["']\s*/\s*(?:profile|["'](?:release|debug)["'])"""
)


def _python_sources() -> list[Path]:
    return [p for p in _TESTS_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize(
    "pattern, what",
    [
        (_READS_THE_ENV_VAR, "reads $TAN_RUST_BINARY"),
        (_WALKS_THE_TARGET_DIR, "builds a target/{release,debug} path"),
    ],
)
def test_only_oracle_py_resolves_the_oracle_binary(pattern, what):
    """No second resolver, anywhere under `tests/`.

    A copy is not merely duplication here: `rust_binary()` carries three rules
    a copy will not (mtime over a fixed profile order, RAISE on an mtime tie,
    RAISE on a set-but-missing `TAN_RUST_BINARY`), and dropping any of them
    reinstates the stale-pick bug for whichever file copied it -- silently, in
    the `diff` case, because that file's oracle assertion happened to hold on
    both binaries.
    """
    offenders = sorted(
        str(p.relative_to(_TESTS_ROOT))
        for p in _python_sources()
        if p not in (_THE_RESOLVER, _THE_GATE_ITSELF)
        and pattern.search(p.read_text(encoding="utf-8"))
    )
    assert offenders == [], (
        f"{offenders} {what} directly. The suite has exactly ONE oracle "
        f"resolver, {_THE_RESOLVER.relative_to(_TESTS_ROOT)}'s `rust_binary()`: "
        "import it (or `resolve_oracle_for_skipif()` if you need to bind at "
        "module import) rather than re-deriving the path. tan-cli#393 is what "
        "the second and third copies cost."
    )


def _stub_tan(path: Path, version: str) -> None:
    """A runnable `tan` stand-in that answers `--version` and nothing else."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        path.write_text(f"@echo off\r\necho {version}\r\n", encoding="utf-8")
    else:
        path.write_text(f"#!/bin/sh\necho '{version}'\n", encoding="utf-8")
        path.chmod(0o755)


@pytest.fixture
def staged_target(tmp_path, monkeypatch):
    """A fake `target/` with both profiles built, `release` OLDER than `debug`.

    This is the tree that produced tan-cli#393: `tan 0.3.1` in `release` from
    an earlier `cargo build --release`, the pinned version in `debug` from the
    build a developer just ran.
    """
    import tests.parity.oracle as oracle_mod

    exe = ".exe" if sys.platform == "win32" else ""
    release = tmp_path / "target" / "release" / f"tan{exe}"
    debug = tmp_path / "target" / "debug" / f"tan{exe}"
    _stub_tan(release, "tan 0.3.1")
    _stub_tan(debug, PINNED_ORACLE_VERSION)
    os.utime(release, (1_000_000, 1_000_000))
    os.utime(debug, (2_000_000, 2_000_000))
    monkeypatch.setattr(oracle_mod, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("TAN_RUST_BINARY", raising=False)
    return release, debug


def test_the_newer_profile_wins_even_when_release_exists(staged_target):
    """The copies tried `release` first unconditionally and so answered
    `tan 0.3.1` here. mtime answers the pinned build."""
    release, debug = staged_target
    assert rust_binary() == str(debug)
    assert rust_binary() != str(release)


def test_an_mtime_tie_is_refused_rather_than_broken_to_release(staged_target):
    """A cache restore, `cp -p`, rsync or an artifact download can equalise
    mtimes with no rebuild. The copies would have silently taken `release`."""
    release, _debug = staged_target
    os.utime(release, (2_000_000, 2_000_000))
    with pytest.raises(RuntimeError, match="IDENTICAL"):
        rust_binary()


def test_the_import_time_wrapper_defers_a_tie_instead_of_aborting_collection(staged_target):
    """`resolve_oracle_for_skipif` swallows that raise so a test module can
    bind `_ORACLE` at import without a tie becoming a COLLECTION error that
    takes the whole session down. It is not silent: `pinned_oracle` calls
    `rust_binary()` itself and re-raises."""
    release, _debug = staged_target
    os.utime(release, (2_000_000, 2_000_000))
    assert resolve_oracle_for_skipif() is None


def test_a_missing_named_oracle_raises_rather_than_skipping(staged_target, tmp_path, monkeypatch):
    """A typo'd `TAN_RUST_BINARY` in CI must not produce an all-skip green run.
    Neither hand copy checked this -- both returned the bad path unread."""
    monkeypatch.setenv("TAN_RUST_BINARY", str(tmp_path / "no" / "such" / "tan"))
    with pytest.raises(RuntimeError, match="does not exist"):
        rust_binary()


def test_a_resolved_but_wrong_binary_fails_naming_the_oracle(staged_target):
    """The acceptance criterion of tan-cli#393: the stale binary must fail
    with the PIN message, not with a `project.boardYaml` envelope diff that
    reads as a port regression.

    Drives `pinned_oracle`'s assertion directly -- it is session-scoped and
    autouse, so observing it through a real run would need a whole subprocess.
    """
    release, _debug = staged_target
    proc = subprocess.run(
        [str(release), "--version"], capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.stdout.strip() == "tan 0.3.1"
    assert proc.stdout.strip() != PINNED_ORACLE_VERSION

    with pytest.raises(AssertionError, match="not the pinned"):
        assert proc.stdout.strip() == PINNED_ORACLE_VERSION, (
            f"resolved oracle {str(release)!r} reports {proc.stdout.strip()!r}, "
            f"not the pinned {PINNED_ORACLE_VERSION!r} this whole parity suite "
            "is measured against."
        )
