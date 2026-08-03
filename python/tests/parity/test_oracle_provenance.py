# SPDX-License-Identifier: Apache-2.0
"""The oracle pin's OTHER half: which `crates/` commit the resolved binary was
built from (tan-cli#406), and whether the frozen fixtures were captured from
that same tree (tan-cli#409's `PROVENANCE.txt` claim).

`PINNED_ORACLE_VERSION` alone cannot answer either question. `tan --version`
renders `Cargo.toml`'s `[workspace.package]` version, which moves once per
release, so every build made between two releases prints the identical string:
measured, 15 commits changed the binary's build inputs across `c5dedc1..
ac79d4c` -- including `bb13283` (`project.boardYaml`) and `a30adaf` (the flash
TBD placeholder), both envelope-visible -- and `diff <(old --version)
<(new --version)` was EMPTY while `cmp` called the binaries different.

Reproduced before the guard existed, with the smallest possible oracle:

    $ printf '#!/bin/sh\\necho "tan 0.4.1"\\n' > fake-tan && chmod +x fake-tan
    $ TAN_RUST_BINARY=$PWD/fake-tan python -m pytest tests/core/test_planner_root.py -q
    5 passed

A two-line shell script, built from no commit at all, was certified by
`pinned_oracle` and the suite went on to measure the port against it.

Every case here varies the COMMIT, not the version string: a test that varies
the version string passes against the old code, which is exactly why the gap
survived.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.parity import oracle, oracle_provenance
from tests.parity.oracle import (
    PINNED_ORACLE_CRATES_COMMIT,
    PINNED_ORACLE_VERSION,
    oracle_provenance_drift,
    rust_binary,
)
from tests.parity.oracle_provenance import (
    ORACLE_COMMIT_ENV,
    ORACLE_COMMIT_SIDECAR_SUFFIX,
)

#: The commit the fixtures and the pin were LAST re-verified against, and the
#: one `c5dedc1` the issue built its stale oracle from. Spelled here so the
#: cases below name a real commit rather than a made-up hex string that
#: `describe_commit` could not resolve.
_STALE_COMMIT = "c5dedc1c457065147f5b739e2b8175e4b554ac5b"


def _describe(sha: str) -> str:
    """`<abbrev> <subject>` for a SHA, resolved against THIS checkout -- so an
    assertion message names a commit a reader recognises."""
    return oracle_provenance.describe_commit(oracle.REPO_ROOT, sha)


def _stub_tan(path: Path, version: str) -> Path:
    """A runnable `tan` stand-in that answers `--version` and nothing else.

    Deliberately a near-copy of `tests/gates/test_one_oracle_resolver.py`'s
    helper of the same name: sharing it would mean putting a test-stub factory
    inside `oracle.py`, which is the harness the whole suite imports, to save
    six lines.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        path.write_text(f"@echo off\r\necho {version}\r\n", encoding="utf-8")
    else:
        path.write_text(f"#!/bin/sh\necho '{version}'\n", encoding="utf-8")
        path.chmod(0o755)
    return path


def _declare(binary: Path, commit: str) -> None:
    """Stamp *binary* with the `crates/` commit it was built from -- the
    sidecar half of the seam, so a build made outside this checkout can still
    be certified (or refused) for what it is."""
    Path(str(binary) + ORACLE_COMMIT_SIDECAR_SUFFIX).write_text(commit, encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_ambient_declaration(monkeypatch):
    """A developer or CI job that legitimately exports the declaration must
    not silently decide what these cases measure."""
    monkeypatch.delenv(ORACLE_COMMIT_ENV, raising=False)


# ---------------------------------------------------------------------------
# The regression tan-cli#406 asks for: same version, different commit.
# ---------------------------------------------------------------------------


def test_two_oracles_reporting_the_same_version_are_told_apart_by_commit(tmp_path, monkeypatch):
    """The acceptance criterion. Both stubs answer `tan 0.4.1` -- so the
    version pin passes both, exactly as it passed the real `c5dedc1` build --
    and only the one built from the pinned commit is certified.
    """
    current = _stub_tan(tmp_path / "current-tan", PINNED_ORACLE_VERSION)
    stale = _stub_tan(tmp_path / "stale-tan", PINNED_ORACLE_VERSION)
    _declare(current, PINNED_ORACLE_CRATES_COMMIT)
    _declare(stale, _STALE_COMMIT)

    for binary in (current, stale):
        proc = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, encoding="utf-8"
        )
        assert proc.stdout.strip() == PINNED_ORACLE_VERSION

    assert oracle_provenance_drift(str(current)) is None
    drift = oracle_provenance_drift(str(stale))
    assert drift is not None
    assert _STALE_COMMIT[:7] in drift
    assert PINNED_ORACLE_CRATES_COMMIT[:7] in drift


def test_the_drift_reaches_a_caller_as_a_raise_from_the_resolver(tmp_path, monkeypatch):
    """Named through `TAN_RUST_BINARY`, a drifted oracle must fail the run
    rather than be handed back -- `pinned_oracle` calls `rust_binary()`, so
    this is what turns three `project.boardYaml` assertion diffs (which read
    as a port regression, and whose apparent fix re-introduces the bug
    `bb13283` removed) into one message naming the stale commit.
    """
    stale = _stub_tan(tmp_path / "stale-tan", PINNED_ORACLE_VERSION)
    _declare(stale, _STALE_COMMIT)
    monkeypatch.setenv("TAN_RUST_BINARY", str(stale))

    with pytest.raises(RuntimeError, match="declares crates/ commit"):
        rust_binary()


def test_an_environment_declaration_wins_over_a_sidecar(tmp_path, monkeypatch):
    """An operator naming a commit for THIS run means it: a stale sidecar
    left beside a rebuilt binary must not out-vote them."""
    binary = _stub_tan(tmp_path / "tan", PINNED_ORACLE_VERSION)
    _declare(binary, _STALE_COMMIT)
    monkeypatch.setenv(ORACLE_COMMIT_ENV, PINNED_ORACLE_CRATES_COMMIT)

    assert oracle_provenance_drift(str(binary)) is None


def test_an_abbreviated_declaration_matches_but_a_truncated_one_does_not(tmp_path):
    """Humans paste `ac79d4c`, not forty characters. Two characters is not an
    abbreviation, it is a coincidence waiting to happen, so the floor is
    git's own seven."""
    binary = _stub_tan(tmp_path / "tan", PINNED_ORACLE_VERSION)

    _declare(binary, PINNED_ORACLE_CRATES_COMMIT[:7])
    assert oracle_provenance_drift(str(binary)) is None

    _declare(binary, PINNED_ORACLE_CRATES_COMMIT[:2])
    assert oracle_provenance_drift(str(binary)) is not None


# ---------------------------------------------------------------------------
# The undeclared cases -- what a binary's own age and location can prove.
# ---------------------------------------------------------------------------


def test_an_undeclared_oracle_from_outside_this_checkout_is_refused(tmp_path):
    """The issue's own repro path. `TAN_RUST_BINARY=<a build from another
    worktree>` wins the mtime rule (it is freshly built), answers the pinned
    version (that string is release-scoped), and so used to be certified with
    nothing tying it to a source tree at all.
    """
    binary = _stub_tan(tmp_path / "tan", PINNED_ORACLE_VERSION)

    drift = oracle_provenance_drift(str(binary))

    assert drift is not None
    assert "outside this checkout's build output" in drift
    assert ORACLE_COMMIT_ENV in drift  # and how to answer it


def test_a_build_older_than_a_source_it_compiles_is_refused(tmp_path, monkeypatch):
    """The contributor-returning-to-a-branch case, and the CI one: `tar`
    preserves a cached `target/`'s mtime while a fresh checkout stamps the
    sources with today's, so the restored binary is provably older than the
    code it claims to be. No declaration needed for this one -- the
    filesystem already answered.

    Staged against a synthetic root rather than the repo's own `target/` so
    the case does not depend on how recently anyone ran `cargo build` here.
    """
    root = tmp_path / "checkout"
    source = root / "crates" / "tan-cli" / "src" / "main.rs"
    source.parent.mkdir(parents=True)
    source.write_text("fn main() {}\n", encoding="utf-8")
    monkeypatch.setattr(oracle, "REPO_ROOT", root)
    binary = _stub_tan(oracle.build_output_root() / "debug" / "tan", PINNED_ORACLE_VERSION)
    os.utime(binary, (1_000_000, 1_000_000))
    os.utime(source, (2_000_000, 2_000_000))

    drift = oracle_provenance_drift(str(binary))

    assert drift is not None
    assert "was built BEFORE" in drift
    assert "crates/tan-cli/src/main.rs" in drift.replace("\\", "/")


def test_a_current_build_inside_this_checkout_needs_no_declaration(tmp_path, monkeypatch):
    """The ordinary `cargo build && pytest` loop must stay silent -- a guard
    that demanded a stamp from every developer would be turned off."""
    root = tmp_path / "checkout"
    source = root / "crates" / "tan-cli" / "src" / "main.rs"
    source.parent.mkdir(parents=True)
    source.write_text("fn main() {}\n", encoding="utf-8")
    monkeypatch.setattr(oracle, "REPO_ROOT", root)
    binary = _stub_tan(oracle.build_output_root() / "debug" / "tan", PINNED_ORACLE_VERSION)
    os.utime(source, (1_000_000, 1_000_000))
    os.utime(binary, (2_000_000, 2_000_000))

    assert oracle_provenance_drift(str(binary)) is None


# ---------------------------------------------------------------------------
# The three deliberate no-ops.
# ---------------------------------------------------------------------------


def test_an_inter_release_mismatch_still_fails_with_the_version_pin(tmp_path):
    """tan-cli#393 must keep its message. A `tan 0.3.1` binary is wrong for a
    reason `pinned_oracle` already states precisely, one step later; a
    provenance complaint on top would bury it, so this check stands down for
    a version that already disagrees.
    """
    binary = _stub_tan(tmp_path / "old-tan", "tan 0.3.1")

    assert oracle_provenance_drift(str(binary)) is None
    proc = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.stdout.strip() != PINNED_ORACLE_VERSION


def test_a_tree_with_no_crates_directory_is_not_drift(tmp_path, monkeypatch):
    """After tan-cli#269 deletes `crates/` there is nothing to be stale
    against, and the frozen fixtures are the whole measurement. The guard
    must go quiet then, not fail every run."""
    root = tmp_path / "checkout-without-crates"
    root.mkdir()
    monkeypatch.setattr(oracle, "REPO_ROOT", root)
    binary = _stub_tan(tmp_path / "tan", PINNED_ORACLE_VERSION)

    assert oracle_provenance_drift(str(binary)) is None


# ---------------------------------------------------------------------------
# The pin, and the fixtures behind it, against THIS checkout.
# ---------------------------------------------------------------------------


def test_the_pinned_commit_is_this_checkouts_newest_build_input_commit():
    """A pin nobody re-checks is the failure this issue is about, one level
    up: a `crates/` change with no matching pin bump means the fixtures and
    the binary they were captured from have parted company. Bump
    `PINNED_ORACLE_CRATES_COMMIT` and re-verify the fixtures against a rebuilt
    oracle (`TAN_PARITY_LIVE=1`), or re-capture them.
    """
    head = oracle_provenance.head_build_input_commit(oracle.REPO_ROOT)
    if head is None:
        pytest.skip("no git in this environment, or no crates/ left to pin (tan-cli#269)")
    assert head == PINNED_ORACLE_CRATES_COMMIT, (
        f"crates/ build inputs moved to {_describe(head)}; the parity suite is still "
        f"pinned to {_describe(PINNED_ORACLE_CRATES_COMMIT)}. Re-verify the frozen "
        "fixtures against a rebuilt oracle and advance the pin (both live in "
        "tests/parity/), or re-capture them."
    )


#: The two machine-read lines in `oracle_fixtures/PROVENANCE.txt`. They are
#: separate facts and must not collapse into one: the CAPTURE SHA is where
#: the committed answers physically came from and never changes without a
#: re-capture, while the VERIFIED SHA is how far anyone has since checked
#: that those answers still hold. Recording only the first makes a
#: re-verification unrecordable; recording only the second erases where the
#: fixtures came from.
_PROVENANCE_KEYS = ("captured-from-crates-commit", "verified-against-crates-commit")


def _provenance_field(key: str) -> str:
    path = Path(__file__).resolve().parent / "oracle_fixtures" / "PROVENANCE.txt"
    values = [
        line.split(f"{key}:", 1)[1].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith(f"{key}:")
    ]
    assert len(values) == 1, (
        f"PROVENANCE.txt must carry exactly one machine-readable `{key}: <sha>` "
        f"line; found {values}"
    )
    return values[0]


@pytest.mark.parametrize("key", _PROVENANCE_KEYS)
def test_the_recorded_provenance_shas_resolve_to_real_commits(key):
    """A SHA nobody can resolve is not a record of anything. Cheap, and it is
    what keeps the drift check below from passing on a typo."""
    sha = _provenance_field(key)
    if oracle_provenance.crates_commits_since(oracle.REPO_ROOT, sha) is None:
        pytest.skip("no git in this environment, so a recorded SHA cannot be resolved")
    assert _describe(sha) != sha, f"PROVENANCE.txt's {key} {sha} is unresolvable"


def test_the_frozen_fixtures_record_a_provenance_that_still_holds():
    """`PROVENANCE.txt` claims the committed fixtures still describe the
    oracle. Nothing checked it, and it had gone stale by three commits -- one
    of them a 4654-line rewrite of `debug_config.rs` -- while the file itself
    still read "crates/ is frozen and will not change again for this reason"
    (tan-cli#409).

    Keyed on the VERIFIED sha, not the capture one: a maintainer who
    re-validated the frozen answers against a newer oracle (`TAN_PARITY_LIVE=1`
    over the capture recipe's modules) has done the work this check is asking
    for without re-writing a single fixture, and that has to be recordable or
    the check becomes noise someone deletes.

    Scoped to the WHOLE of `crates/`, unlike the build-input pathspec the
    oracle-staleness guard uses: a capture is a claim about the tree that
    produced it.
    """
    verified = _provenance_field("verified-against-crates-commit")
    since = oracle_provenance.crates_commits_since(oracle.REPO_ROOT, verified)
    if since is None:
        pytest.skip("no git in this environment, so the recorded SHA cannot be resolved")
    assert since == "", (
        f"crates/ has moved since the frozen fixtures were last verified at "
        f"{_describe(verified)}:\n{since}\n"
        "Re-validate them against a rebuilt oracle (TAN_PARITY_LIVE=1 over the modules "
        "in PROVENANCE.txt's capture recipe) and advance verified-against-crates-commit "
        "with what you measured, or re-capture (TAN_PARITY_CAPTURE=1) and advance both "
        "recorded SHAs."
    )
