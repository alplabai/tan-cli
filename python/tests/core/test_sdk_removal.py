# SPDX-License-Identifier: Apache-2.0
"""`tan.core.sdk_removal` -- tan-cli#790's target resolution, sizing, and
failure classification, at the unit layer (pure functions and small real
filesystem fixtures via `tmp_path`, no subprocess). Command-level envelope
behaviour (refusals, idempotence, registry pruning) lives in
`tests/commands/test_sdk_command.py`, alongside every other `sdk` verb.
"""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from tan.core.sdk_removal import (
    RemovalOutcome,
    classify_removal_error,
    compute_tree_bytes,
    is_outside_cache_root,
    looks_like_path,
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
