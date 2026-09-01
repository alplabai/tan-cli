# SPDX-License-Identifier: Apache-2.0
"""`tan.core.sdk_removal` -- tan-cli#790's target resolution, sizing, and
failure classification, at the unit layer (pure functions and small real
filesystem fixtures via `tmp_path`, no subprocess). Command-level envelope
behaviour (refusals, idempotence, registry pruning) lives in
`tests/commands/test_sdk_command.py`, alongside every other `sdk` verb.
"""
from __future__ import annotations

import errno
import ntpath
import os
import posixpath
import stat
from pathlib import Path

import pytest

from tan.core.sdk_removal import (
    RemovalOutcome,
    classify_removal_error,
    compute_tree_bytes,
    is_outside_cache_root,
    looks_like_path,
    removal_would_take_out,
    remove_sdk_tree,
    resolve_removal_target,
)

WINDOWS = os.name == "nt"


# ── looks_like_path / resolve_removal_target ────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("v0.15.0", False),
        ("0.15.0-rc1", False),
        ("bare-name", False),
        (".", True),
        ("..", True),
        ("./sdk-cache/v0.15.0", True),
        ("../sibling", True),
        ("/abs/sdk-cache/v0.15.0", True),
    ],
)
def test_looks_like_path_distinguishes_a_version_name_from_an_explicit_path(raw, expected):
    assert looks_like_path(raw) is expected


@pytest.mark.skipif(not WINDOWS, reason="backslash-as-separator is Windows-only in practice")
def test_looks_like_path_recognises_a_backslash_path_even_off_windows():
    assert looks_like_path("sdk-cache\\v0.15.0") is True


def test_resolve_removal_target_joins_a_bare_version_onto_destination(tmp_path):
    destination = tmp_path / "sdk-cache"
    result = resolve_removal_target("v0.15.0", destination)
    assert result.target == destination / "v0.15.0"
    assert result.is_named_version is True
    assert result.destination == destination


def test_resolve_removal_target_takes_an_explicit_path_as_is(tmp_path):
    destination = tmp_path / "sdk-cache"
    explicit = tmp_path / "elsewhere" / "checkout"
    result = resolve_removal_target(str(explicit), destination)
    assert result.target == explicit
    assert result.is_named_version is False


# ── is_outside_cache_root ────────────────────────────────────────────────────


def test_is_outside_cache_root_false_for_a_child_of_the_root(tmp_path):
    root = tmp_path / "sdk-cache"
    assert is_outside_cache_root(root / "v0.15.0", root) is False


def test_is_outside_cache_root_false_for_the_root_itself(tmp_path):
    root = tmp_path / "sdk-cache"
    assert is_outside_cache_root(root, root) is False


def test_is_outside_cache_root_true_for_a_sibling_directory(tmp_path):
    """The exact footgun the tan-cli#790 issue names: a path that merely
    SHARES A PREFIX with the cache root (`sdk-cache-old` starts with
    `sdk-cache`) must not be treated as contained -- only a real path
    SEPARATOR boundary counts."""
    root = tmp_path / "sdk-cache"
    sibling = tmp_path / "sdk-cache-old" / "v0.1.0"
    assert is_outside_cache_root(sibling, root) is True


def test_is_outside_cache_root_true_for_an_unrelated_absolute_path(tmp_path):
    root = tmp_path / "sdk-cache"
    unrelated = tmp_path / "not-the-cache" / "v0.1.0"
    assert is_outside_cache_root(unrelated, root) is True


# ── compute_tree_bytes ───────────────────────────────────────────────────────


def test_compute_tree_bytes_sums_every_file_in_the_tree(tmp_path):
    root = tmp_path / "install"
    (root / "a").mkdir(parents=True)
    (root / "a" / "one.bin").write_bytes(b"x" * 100)
    (root / "b").mkdir()
    (root / "b" / "two.bin").write_bytes(b"y" * 50)
    assert compute_tree_bytes(root) == 150


def test_compute_tree_bytes_a_single_file_is_its_own_size(tmp_path):
    f = tmp_path / "solo.bin"
    f.write_bytes(b"z" * 37)
    assert compute_tree_bytes(f) == 37


def test_compute_tree_bytes_absent_path_is_zero(tmp_path):
    assert compute_tree_bytes(tmp_path / "does-not-exist") == 0


def test_compute_tree_bytes_empty_directory_is_zero(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    assert compute_tree_bytes(root) == 0


# ── classify_removal_error ───────────────────────────────────────────────────


def _err(*, errno_value: int, winerror: int | None = None) -> OSError:
    err = OSError(errno_value, os.strerror(errno_value) if errno_value else "boom")
    if winerror is not None:
        err.winerror = winerror  # type: ignore[attr-defined]
    return err


def test_classify_removal_error_ebusy_is_in_use():
    assert classify_removal_error(_err(errno_value=errno.EBUSY)) == "in-use"


def test_classify_removal_error_etxtbsy_is_in_use():
    assert classify_removal_error(_err(errno_value=errno.ETXTBSY)) == "in-use"


def test_classify_removal_error_eacces_is_permission():
    assert classify_removal_error(_err(errno_value=errno.EACCES)) == "permission"


def test_classify_removal_error_eperm_is_permission():
    assert classify_removal_error(_err(errno_value=errno.EPERM)) == "permission"


@pytest.mark.parametrize("code", [32, 33], ids=["sharing-violation", "lock-violation"])
def test_classify_removal_error_windows_in_use_winerrors(code):
    assert classify_removal_error(_err(errno_value=errno.EACCES, winerror=code)) == "in-use"


def test_classify_removal_error_windows_access_denied_winerror_is_permission():
    """winerror 5 (ERROR_ACCESS_DENIED) must NOT be classified in-use -- the
    exact distinction the tan-cli#790 issue asks for: a wrong verdict here
    sends the reader hunting a holder that does not exist."""
    assert classify_removal_error(_err(errno_value=errno.EACCES, winerror=5)) == "permission"


def test_classify_removal_error_winerror_takes_precedence_over_errno():
    """An `errno` that WOULD read as in-use (`EBUSY`) must not win once a
    `winerror` is present and says otherwise -- `winerror` is the more
    specific signal on a real Windows `OSError`."""
    err = _err(errno_value=errno.EBUSY, winerror=5)
    assert classify_removal_error(err) == "permission"


# ── remove_sdk_tree ───────────────────────────────────────────────────────────


def test_remove_sdk_tree_removes_the_directory_and_reports_freed_bytes(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    (root / "blob.bin").write_bytes(b"x" * 256)
    outcome = remove_sdk_tree(root)
    assert isinstance(outcome, RemovalOutcome)
    assert outcome.ok is True
    assert outcome.freed_bytes == 256
    assert not root.exists()


@pytest.mark.skipif(WINDOWS, reason="POSIX directory-mode semantics")
def test_remove_sdk_tree_clears_a_read_only_directory_defeating_a_childs_unlink(tmp_path):
    """The maintainer's own tan-cli#790 follow-up correction, made concrete:
    on POSIX, `unlink`/`rmdir` consult only the CONTAINING directory's write
    bit -- a read-only directory (`chmod 555`) defeats the removal of every
    child inside it, and the child's OWN mode was never the problem. This is
    the case `dir_removal.remove_dir`'s read-only retry did NOT handle before
    tan-cli#790 (it only cleared the FAILING PATH's own mode, which is a
    no-op for POSIX unlink); mutation-tested below.
    """
    root = tmp_path / "install"
    locked = root / "readonly-dir"
    locked.mkdir(parents=True)
    (locked / "child.txt").write_text("x")
    os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)  # r-x, no write
    try:
        outcome = remove_sdk_tree(root)
    finally:
        # Best-effort cleanup even on an assertion failure below, so a failed
        # run does not leave an undeletable directory behind for the next one.
        if locked.exists():
            os.chmod(locked, stat.S_IRWXU)
    assert outcome.ok is True, outcome.detail
    assert not root.exists()


def test_remove_sdk_tree_reports_a_partial_freed_amount_on_failure(tmp_path, monkeypatch):
    """A failure partway through must still report what was ACTUALLY freed,
    not zero -- `remove_sdk_tree` sizes before AND after a failed attempt."""
    import tan.core.sdk_removal as mod

    root = tmp_path / "install"
    root.mkdir()
    (root / "kept.bin").write_bytes(b"x" * 64)

    def explode(_path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(mod, "remove_dir", explode)
    outcome = remove_sdk_tree(root)
    assert outcome.ok is False
    assert outcome.kind == "permission"
    # Nothing was actually removed (the fake never touched the tree), so the
    # freed amount must be 0, not the full pre-size -- proving this is
    # measured post-failure, not just echoed from the pre-size.
    assert outcome.freed_bytes == 0
    assert root.exists()


# ── removal_would_take_out (tan-cli#1053) ───────────────────────────────────
#
# The four `sdk remove` comparisons this replaces all failed in the UNSAFE
# direction -- a miss meant the load-bearing refusal never fired and the
# install another project still pointed at was silently removed without
# `--force`. So both directions are asserted here: the matches that must now
# be found, AND the non-matches that must NOT become spurious refusals.


def test_two_spellings_of_one_directory_match_even_though_the_strings_differ(tmp_path):
    """The defect class, reproduced HOST-NATIVELY on a case-sensitive
    filesystem where the originally-reported case-insensitive spelling cannot
    be: a symlinked cache gives two string-unequal absolute paths that name
    ONE directory, exactly as `<cache>/SdkVersion` and `<cache>/sdkversion`
    do on macOS's default APFS volume. `os.path.samefile` answers True for
    both arrangements for the same reason -- one `st_dev`/`st_ino` -- which
    is why one helper covers both."""
    real = tmp_path / "cache" / "v0.19.0"
    real.mkdir(parents=True)
    try:
        (tmp_path / "cache-link").symlink_to(tmp_path / "cache", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create a symlink")
    through_link = str(tmp_path / "cache-link" / "v0.19.0")

    assert str(real) != through_link, "the fixture would be vacuous otherwise"
    assert os.path.samefile(str(real), through_link), "the fixture must be ONE directory"
    # Both directions, and both are the SAME verdict here on purpose: the link
    # is an INTERMEDIATE component (`cache-link/`), so neither side's FINAL
    # component is a link and `remove_dir` really would delete the real
    # directory whichever spelling it is handed. The asymmetric case -- a
    # target whose own final component is the link -- is the next test.
    assert removal_would_take_out(str(real), through_link)
    assert removal_would_take_out(through_link, str(real))


def test_removing_a_symlink_does_not_take_out_what_it_points_at(tmp_path):
    """The asymmetry, and the over-refusal the first version of this change
    shipped (tan-cli#1053 review, major 1).

    `dir_removal.remove_dir` UNLINKS a link it is handed and never follows
    it, so removing `<cache>/current` destroys nothing behind it -- but
    `os.path.samefile` follows links on BOTH sides and answered True, so the
    workspace pinned at the real `<cache>/v0.19.0` was reported as orphaned
    by a removal that could not touch it. Asserted in both directions,
    because getting only one of them right is exactly the defect."""
    real = tmp_path / "v0.19.0"
    real.mkdir()
    link = tmp_path / "current"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create a symlink")
    assert os.path.samefile(str(link), str(real)), "samefile alone would say yes"

    # Removing the LINK cannot destroy the real directory.
    assert not removal_would_take_out(str(real), str(link))
    # Removing the REAL directory does orphan whoever resolves through the link.
    assert removal_would_take_out(str(link), str(real))
    # ...and naming the link itself on both sides is still the same path.
    assert removal_would_take_out(str(link), str(link))


def test_removing_a_link_does_take_out_the_same_link_under_another_spelling(tmp_path):
    """The other half of the asymmetry, and the under-refusal a BLANKET
    `return False` on `islink(target)` shipped (tan-cli#1053 review, round 2).

    Removing a link takes out that link -- so a workspace pinned at the SAME
    link under a different spelling really is orphaned, and really is owed a
    refusal. Measured on the blanket version: with `cache/current ->
    v0.19.0` and `alias -> cache`, a pin at `<T>/alias/current` went
    `projectPin` -> `none` on a `tan sdk remove <cache>/current` that needed
    no `--force` at all. `os.path.samestat` over two `os.lstat`s follows
    neither final component, so two spellings of one link share an inode
    while a link and its own target do not."""
    real = tmp_path / "cache" / "v0.19.0"
    real.mkdir(parents=True)
    try:
        (tmp_path / "cache" / "current").symlink_to(real, target_is_directory=True)
        (tmp_path / "alias").symlink_to(tmp_path / "cache", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create a symlink")
    direct = str(tmp_path / "cache" / "current")
    aliased = str(tmp_path / "alias" / "current")
    assert direct != aliased, "the fixture would be vacuous otherwise"

    # One link under two spellings: removing it orphans the other spelling.
    assert removal_would_take_out(aliased, direct)
    assert removal_would_take_out(direct, aliased)
    # ...while the link and what it POINTS AT stay distinct, both ways.
    assert not removal_would_take_out(str(real), direct)
    assert removal_would_take_out(direct, str(real))


def test_two_genuinely_different_directories_still_do_not_match(tmp_path):
    """The SAFE direction, and the reason this helper cannot just answer True.
    A guard that over-refuses is its own bug: an unrelated install has to stay
    removable without `--force`. Both sides EXIST here, so the `samefile` arm
    really runs and really has to say no -- a fixture of two absent paths
    would pass on the lexical arm alone and prove nothing."""
    left = tmp_path / "v0.19.0"
    right = tmp_path / "v0.20.0"
    left.mkdir()
    right.mkdir()

    assert not removal_would_take_out(str(left), str(right))


def test_a_pair_that_does_not_exist_degrades_to_the_lexical_answer(tmp_path):
    """`os.path.samefile` RAISES for a path that is not there, and a removal
    target legitimately stops existing partway through this command -- so the
    filesystem arm must degrade, never propagate. Equal spellings still
    match; different ones answer False rather than exploding."""
    gone = str(tmp_path / "already-removed")
    assert not os.path.exists(gone)

    assert removal_would_take_out(gone, gone)
    assert not removal_would_take_out(gone, str(tmp_path / "something-else"))


@pytest.mark.skipif(WINDOWS, reason="the POSIX flavour is what is being asserted")
def test_case_is_not_folded_on_a_case_sensitive_posix_host(tmp_path):
    """The whole reason the fold does NOT live in
    `sdk_default_registry.normalized_sdk_path`: on case-sensitive POSIX
    `/home/me/sdk` and `/home/Me/sdk` really are two directories, and a
    blanket case fold would refuse a removal that is perfectly safe."""
    assert posixpath.normcase("/home/Me/sdk") == "/home/Me/sdk"
    assert not removal_would_take_out("/home/me/sdk", "/home/Me/sdk")


def test_the_case_fold_is_the_stdlib_normcase_so_windows_folds_and_posix_does_not(monkeypatch):
    """The platform half this Linux host cannot execute, asserted through the
    stdlib function that decides it. `os.path.normcase` IS `ntpath.normcase`
    on Windows and `posixpath.normcase` on POSIX, so pinning both flavours
    plus the helper's delegation to whichever one `os.path` exposes covers
    the Windows behaviour without a Windows box.

    Also the reason `normcase` alone is not the fix: it is the IDENTITY on
    darwin, whose default APFS volume is case-INSENSITIVE -- which is why the
    `samefile` arm above exists at all."""
    assert ntpath.normcase("C:/Users/Me/sdk") == ntpath.normcase("c:/users/me/sdk")
    assert posixpath.normcase("/Users/Me") != posixpath.normcase("/users/me")

    monkeypatch.setattr(os.path, "normcase", ntpath.normcase)
    assert removal_would_take_out("C:/Users/Me/sdk", "c:/users/me/sdk")


def test_a_relative_spelling_is_never_anchored_to_the_removing_processs_cwd(
    tmp_path, monkeypatch
):
    """`normalized_sdk_path`'s own invariant, preserved: a registry `sdkPath`
    that was stored RELATIVE must not be resolved against whatever directory
    the removing process happens to be sitting in, or the comparison invents
    a match its writer never wrote. The `samefile` arm is skipped unless both
    sides are absolute, so this pair -- which `samefile` alone would call one
    directory -- answers False."""
    target = tmp_path / "v0.19.0"
    target.mkdir()
    monkeypatch.chdir(tmp_path)

    assert os.path.samefile("v0.19.0", str(target)), "the hazard is real from this cwd"
    assert not removal_would_take_out("v0.19.0", str(target))
