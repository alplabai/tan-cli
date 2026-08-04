# SPDX-License-Identifier: Apache-2.0
"""Which `crates/` commit an oracle binary was built from (tan-cli#406).

`oracle.PINNED_ORACLE_VERSION` pins the other half, and cannot pin this one.
`tan --version` renders `Cargo.toml`'s `[workspace.package]` version, which
moves once per release, so it is BLIND to every change within a release:
measured, 15 commits changed the binary's build inputs between `c5dedc1`
("release: v0.4.1") and `ac79d4c` without touching that line -- among them
`bb13283` (`project.boardYaml`) and `a30adaf` (the flash TBD placeholder),
both envelope-visible -- and binaries built from the two ends printed the
IDENTICAL `tan 0.4.1` while `cmp` called them different files.

Both directions of that hurt, and the version pin certifies the oracle in
both. The stale end fails three cases with a `project.boardYaml` assertion
diff that reads as a port regression, whose apparent fix is to re-introduce
the exact bug `bb13283` removed. The converse -- an envelope regression
measured against a baseline that already contains it -- passes, and that one
ships: `tests/parity/` is where the envelope alp-sdk-vscode consumes is
measured.

Its own module rather than more of `oracle.py`, which the whole suite imports
and which was already 650 lines. Nothing here reads a module global: every
entry point takes the checkout's roots and pins as arguments, so `oracle.py`
stays the one place that owns those values and a test can repoint them
without this file caring (and so importing this cannot cycle back into it).
"""
from __future__ import annotations

import functools
import hashlib
import os
import subprocess
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

#: How an oracle built somewhere this checkout cannot inspect declares its
#: provenance: the env var, or a sidecar file beside the binary (e.g.
#: ``target/debug/tan.crates-commit``). Either may carry an abbreviated SHA.
ORACLE_COMMIT_ENV = "TAN_ORACLE_CRATES_COMMIT"
ORACLE_COMMIT_SIDECAR_SUFFIX = ".crates-commit"

#: What the compiled `tan` is built FROM, as a git pathspec and -- in
#: :func:`_build_input_files` -- as the matching filesystem walk. Every
#: crate's ``src/**`` (which carries the wizard's vendored template payloads
#: and the completion scripts, so not just ``.rs``), every crate manifest, and
#: the workspace manifest + lockfile.
#:
#: Deliberately EXCLUDES ``crates/*/tests/**`` (compiled into `cargo test`,
#: never into the shipped binary) and the crate-level ``README.md``s. That
#: exclusion is measured, not tidiness: on the tree this was written against,
#: ``crates/tan-cli/README.md`` is NEWER than ``target/debug/tan``, so
#: counting it would call the oracle stale for a docs commit -- a false red
#: this guard cannot afford, because it fails the whole session.
BUILD_INPUT_PATHSPEC = (
    "crates/*/src/*",
    "crates/*/Cargo.toml",
    "Cargo.toml",
    "Cargo.lock",
)


def git(repo_root: Path, *args: str) -> str | None:
    """``git`` in *repo_root*: its stdout (possibly EMPTY) on success,
    ``None`` when it could not answer at all.

    The two are kept apart deliberately -- an empty ``git log`` is the answer
    "nothing changed", which is the whole verdict of
    :func:`crates_commits_since`, while ``None`` means a source tarball with
    no ``.git``, or a host with no ``git``. Collapsing them turns "I cannot
    tell" into "all clear", which is the silent-green shape this area keeps
    reproducing. ``None`` is not swallowed error handling: every caller says
    in its own message that it had no second opinion, rather than inventing a
    verdict from it.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def describe_commit(repo_root: Path, sha: str) -> str:
    """``<abbrev> <subject>``, or the SHA alone when git cannot resolve it --
    so a drift message names a commit a reader recognises instead of forty hex
    characters they have to go look up."""
    return git(repo_root, "log", "-1", "--format=%h %s", sha) or sha


def head_build_input_commit(repo_root: Path) -> str | None:
    """The newest commit at HEAD touching a build input. Equals the pin on a
    checkout the pin is current for; when it does not, the pin -- and the
    frozen fixtures behind it -- are what moved on."""
    return git(repo_root, "log", "-1", "--format=%H", "--", *BUILD_INPUT_PATHSPEC) or None


def build_input_digest(repo_root: Path) -> str | None:
    """A content digest of every tracked build input, or ``None`` without git.

    This, not a commit SHA, is what pins the oracle. The SHA version of this
    check (`git log -1 --format=%H -- <BUILD_INPUT_PATHSPEC>`) could never pass
    on a pull request: GitHub builds a synthetic MERGE ref, and a path-limited
    `git log -1` there answers with the merge commit itself. Measured on
    tan-cli#414 -- "crates/ build inputs moved to 7ab4c99 Merge <head> into
    <base>; the parity suite is still pinned to ac79d4c" -- on a branch whose
    diff touches no build input at all. A gate that fails by construction on
    every PR is worse than no gate: it trains a reader to wave it through.

    Rebases, cherry-picks and squash-merges rewrite SHAs without changing one
    byte the compiler sees, and all three are normal here. Content answers the
    question the fixtures actually ask -- "were you captured from THIS crates/
    state?" -- and is immune to every one of them.

    `git ls-files -s` is the whole implementation on purpose: the blob SHAs it
    prints ARE git's own content hashes, already computed, so this neither
    re-reads the files nor invents a second hashing rule that could disagree
    with git about line endings. Same reasoning
    `tests/gates/test_planner_relocation_freshness.py` records for its own
    upstream comparison -- "deliberately a content hash".
    """
    listing = git(repo_root, "ls-files", "-s", "--", *BUILD_INPUT_PATHSPEC)
    if not listing:
        return None
    return hashlib.sha256(listing.encode("utf-8")).hexdigest()


def crates_commits_since(repo_root: Path, sha: str) -> str | None:
    """Commits that changed ``crates/`` CONTENT since ``sha``: empty string
    when nothing has moved, ``None`` when git could not answer.

    Scoped to the WHOLE of ``crates/``, unlike :data:`BUILD_INPUT_PATHSPEC`:
    this one backs the frozen fixtures' ``PROVENANCE.txt`` claim
    (tan-cli#409), and a capture is a claim about the tree that produced it,
    not only about the compiler's inputs.

    The TREE HASHES are compared first, and equality short-circuits to "no
    movement" before the log is consulted at all. `git log -- crates` answers
    about COMMIT REACHABILITY, not content: a squash merge, a rebase or a
    revert that leaves `crates/` byte-identical still produces a new commit
    object whose path-filtered log entry looks exactly like real drift.
    Measured on the tan-cli#418 squash -- `git log` named it, while
    `git rev-parse <sha>:crates` and `git rev-parse HEAD:crates` were the
    SAME hash (205e15ac5291db72e590c40bcd317cb911ab6cdd) and
    `git diff --numstat` reported zero files.

    That false fire is the expensive kind: it demands a re-validation nobody
    needs, on every merge, until someone learns to advance the pin without
    measuring -- at which point the check is worse than absent, because it
    still reads as evidence. Comparing content makes the answer mean what the
    caller assumes it means.
    """
    before = git(repo_root, "rev-parse", f"{sha}:crates")
    after = git(repo_root, "rev-parse", "HEAD:crates")
    if before is not None and after is not None and before == after:
        return ""
    return git(repo_root, "log", "--oneline", f"{sha}..HEAD", "--", "crates")


def declared_commit(binary: str) -> tuple[str, str] | None:
    """``(sha, where it was declared)`` for an oracle that carries its own
    provenance, else ``None``. Env var first, then the sidecar: an operator
    naming one on the command line means it for this run, whatever a stale
    file left beside the binary says."""
    if declared := os.environ.get(ORACLE_COMMIT_ENV):
        return declared.strip(), f"${ORACLE_COMMIT_ENV}"
    sidecar = Path(str(binary) + ORACLE_COMMIT_SIDECAR_SUFFIX)
    if sidecar.is_file():
        return sidecar.read_text(encoding="utf-8").strip(), str(sidecar)
    return None


def same_commit(one: str, other: str) -> bool:
    """Abbreviation-tolerant SHA compare -- a human declaring a provenance
    pastes `ac79d4c`, not all forty. Below git's own seven-character floor
    nothing matches, so a truncated or empty declaration cannot pass by
    prefix."""
    one, other = one.strip().lower(), other.strip().lower()
    if min(len(one), len(other)) < 7:
        return False
    return one.startswith(other) or other.startswith(one)


@functools.lru_cache(maxsize=None)
def reported_version(binary: str) -> str | None:
    """``tan --version`` for *binary*, or ``None`` when it will not run.

    Cached for the process: `pinned_oracle` is session-scoped for the same
    reason, and the resolution cannot change mid-session. A rebuild DURING a
    session is therefore not observed -- which is the existing contract, not
    a new one.
    """
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, encoding="utf-8", timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _build_input_files(repo_root: Path) -> Iterator[Path]:
    """:data:`BUILD_INPUT_PATHSPEC`, walked. The same scope expressed twice
    because git answers the COMMIT question and the filesystem answers the
    "was this binary built before that edit" one."""
    for name in ("Cargo.toml", "Cargo.lock"):
        if (path := repo_root / name).is_file():
            yield path
    for crate in sorted((repo_root / "crates").iterdir()):
        if (manifest := crate / "Cargo.toml").is_file():
            yield manifest
        if (src := crate / "src").is_dir():
            yield from (p for p in src.rglob("*") if p.is_file())


def _undeclared_drift(binary: str, repo_root: Path, build_output: Path, pinned: str) -> str | None:
    """The undeclared case: infer from mtimes, then from location.

    A binary older than an input it is compiled from is stale, provably and
    with nobody's cooperation -- which is the shape both measured exposures
    take: a contributor returning to a branch whose `target/` predates the
    commits on it, and a CI job restoring a cached `target/` over a fresh
    checkout (`tar` preserves the cached mtime; the checkout's sources get
    today's).

    Newer than every input is NOT proof of the converse -- a fresh build of
    an OLD checkout wins that comparison too, which is exactly how the issue
    reproduced it -- so a binary from outside this checkout's own build
    output is refused unless it declares where it came from.
    """
    newest = max(((p.stat().st_mtime, p) for p in _build_input_files(repo_root)), default=None)
    if newest is not None and Path(binary).stat().st_mtime < newest[0]:
        when = datetime.fromtimestamp(newest[0], timezone.utc).isoformat()
        return (
            f"resolved oracle {binary!r} was built BEFORE "
            f"{newest[1].relative_to(repo_root)} was last modified ({when}), so it cannot "
            f"have been built from the pinned crates/ commit "
            f"{describe_commit(repo_root, pinned)}. Its --version answers the pinned string "
            "anyway -- that line is bumped only at release time and is blind to every change "
            "within one (tan-cli#406), which is why this is checked separately. Rebuild it: "
            "`cargo build --locked --bin tan`. If it really was built from that commit and "
            f"only its mtime moved, say so: {ORACLE_COMMIT_ENV}=<sha>."
        )
    if build_output.resolve() not in Path(binary).resolve().parents:
        return (
            f"resolved oracle {binary!r} is outside this checkout's build output "
            f"({build_output}), so nothing here ties it to a crates/ commit: it reports the "
            "pinned version string exactly like every other build made since that line was "
            "last bumped, however old its source tree was (tan-cli#406). Declare it -- "
            f"{ORACLE_COMMIT_ENV}=$(git -C <its checkout> rev-parse HEAD), or write that SHA "
            f"to {binary}{ORACLE_COMMIT_SIDECAR_SUFFIX} -- or unset TAN_RUST_BINARY and let "
            "this checkout's own build be the oracle."
        )
    return None


def drift(
    binary: str,
    *,
    repo_root: Path,
    build_output: Path,
    pinned_commit: str,
    pinned_version: str,
) -> str | None:
    """Why *binary* cannot be certified as the pinned oracle, or ``None``.

    Three deliberate no-ops, each of which would otherwise be a false red:

    * no `crates/` under *repo_root* -- nothing to be stale against. That is
      the post-tan-cli#269 tree, and any test that repoints the root.
    * a binary whose `--version` already disagrees with *pinned_version*.
      `pinned_oracle`'s own assertion is the more specific message for an
      inter-release mismatch (tan-cli#393) and it runs one step later;
      pre-empting it with a provenance complaint would bury it.
    * a declaration that MATCHES the pin -- the whole point of the seam.
    """
    if not (repo_root / "crates").is_dir():
        return None
    reported = reported_version(binary)
    if reported is not None and reported != pinned_version:
        return None
    declared = declared_commit(binary)
    if declared is None:
        return _undeclared_drift(binary, repo_root, build_output, pinned_commit)
    sha, where = declared
    if same_commit(sha, pinned_commit):
        return None
    return (
        f"resolved oracle {binary!r} declares crates/ commit "
        f"{describe_commit(repo_root, sha)} ({where}), not the pinned "
        f"{describe_commit(repo_root, pinned_commit)} this whole parity suite is measured "
        f"against. Both answer {pinned_version}: the version line moves only at release "
        "time, so it cannot see drift within a release (tan-cli#406). Rebuild the oracle "
        "from this checkout (`cargo build --locked --bin tan`), or -- if the move is "
        "intended -- advance PINNED_ORACLE_CRATES_COMMIT in tests/parity/oracle.py and "
        "RE-CAPTURE the frozen fixtures against the new oracle, because they are the other "
        "half of the same measurement."
    )
