# SPDX-License-Identifier: Apache-2.0
"""`tan.core.atomic_write.atomic_write_text`, and its byte-exact sibling
`atomic_write_bytes` (tan-cli#904 third round) -- tan-cli#516.

`bootstrap_cmd.reconcile_west_manifest_path` used to write its temp sibling
with `Path.write_text` + a bare `os.replace`, with no `fsync` anywhere -- the
same gap `debug_config_cmd._atomic_write_launch_json` had until tan-cli#489.
These tests exercise the extracted helper directly; `test_bootstrap_command.py`
covers the one real call site."""

from __future__ import annotations

import os

import pytest

from tan.core import atomic_write as atomic_write_mod
from tan.core.atomic_write import atomic_write_bytes, atomic_write_text


def test_fsyncs_the_temp_file_before_renaming_it_into_place(tmp_path, monkeypatch):
    """The core of tan-cli#516: `os.replace`'s atomicity guarantee covers the
    RENAME only, not whether the renamed-to content has reached stable
    storage. FAILS against a bare `write` + `os.replace` implementation --
    that shape never calls `os.fsync` at all."""
    target = tmp_path / "config"
    real_fsync = os.fsync
    calls: list[int] = []

    def spy_fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    # `monkeypatch.setattr`, not a raw `atomic_write_mod.os.fsync = ...`
    # assignment -- `os` is one shared module object, so a bare assignment
    # patches EVERY module's `os.fsync` process-wide (including a parallel
    # test's) and only gets restored if this test's own `finally` runs;
    # `monkeypatch` restores it unconditionally, even on a failure inside
    # `atomic_write_text` itself, and scopes the patch to this test alone.
    monkeypatch.setattr(atomic_write_mod.os, "fsync", spy_fsync)
    atomic_write_text(str(target), "hello\n")

    assert calls, "atomic_write_text must fsync the temp file before the rename"
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_fsyncs_the_directory_after_a_successful_rename(tmp_path, monkeypatch):
    """Covers the rename ENTRY surviving a crash, on top of the content fsync
    above -- POSIX only; Windows has no directory handle to fsync and
    `os.replace`/`MoveFileExW` already journal the rename itself."""
    if os.name == "nt":
        pytest.skip("POSIX directory fsync only")
    target = tmp_path / "config"
    real_open = os.open
    dir_fds_opened: list[str] = []

    def spy_open(path, flags, *args, **kwargs):
        if path == str(tmp_path) and flags == os.O_RDONLY:
            dir_fds_opened.append(path)
        return real_open(path, flags, *args, **kwargs)

    # See the fsync test above: `monkeypatch.setattr`, not a raw module
    # attribute assignment, so the patch cannot leak past this test.
    monkeypatch.setattr(atomic_write_mod.os, "open", spy_open)
    atomic_write_text(str(target), "hello\n")

    assert dir_fds_opened, "atomic_write_text must open+fsync the containing directory"


def test_writes_through_a_symlink_rather_than_replacing_it(tmp_path):
    """`os.replace` on a symlink replaces the LINK itself with a regular file
    unless the real target is resolved first -- `os.path.realpath` up front is
    what makes this write-through instead of link-destroying."""
    canonical = tmp_path / "real" / "config"
    canonical.parent.mkdir()
    canonical.write_text("old\n", encoding="utf-8")
    link = tmp_path / "config"
    try:
        link.symlink_to(canonical)
    except OSError:
        pytest.skip("cannot create a file symlink on this host")

    atomic_write_text(str(link), "new\n")

    assert link.is_symlink(), "the symlink itself must survive the write"
    assert os.path.realpath(link) == os.path.realpath(canonical)
    assert canonical.read_text(encoding="utf-8") == "new\n"


def test_cleans_up_the_temp_file_when_the_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / "config"
    target.write_text("old\n", encoding="utf-8")

    def boom_replace(_src, _dst):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(atomic_write_mod.os, "replace", boom_replace)

    with pytest.raises(OSError):
        atomic_write_text(str(target), "new\n")

    assert target.read_text(encoding="utf-8") == "old\n"
    leftovers = list(tmp_path.glob("*.tan-tmp"))
    assert leftovers == [], leftovers


def test_unencodable_content_does_not_leak_the_temp_sibling(tmp_path):
    """tan-cli#516 review round, finding 3: `handle.write(content)` raises
    `UnicodeEncodeError` -- a `ValueError`, not an `OSError` -- when `content`
    carries a codepoint `encoding` cannot represent (a lone surrogate under
    the default `utf-8`, here). FAILS against an implementation that only
    catches `OSError` around the write/rename: the exception would propagate
    past the cleanup entirely and the `*.tan-tmp` sibling `mkstemp` already
    created would leak into the caller's own directory, unremoved."""
    target = tmp_path / "config"

    with pytest.raises(UnicodeEncodeError):
        atomic_write_text(str(target), "\udc80", encoding="utf-8")

    assert not target.exists()
    leftovers = list(tmp_path.glob("*.tan-tmp"))
    assert leftovers == [], leftovers


def test_an_unknown_encoding_does_not_leak_the_temp_sibling(tmp_path):
    """tan-cli#516 review round, finding 3: an unrecognised `encoding=` makes
    `os.fdopen` itself raise `LookupError`, before any file object takes
    ownership of the raw descriptor `mkstemp` opened. FAILS against an
    implementation whose inner `except OSError:` (guarding the `os.close(fd)`
    that would otherwise leak the descriptor) does not also list
    `LookupError` -- the exception passes both that guard and the outer
    `except OSError:` untouched, leaking the temp file on disk."""
    target = tmp_path / "config"

    with pytest.raises(LookupError):
        atomic_write_text(str(target), "hello\n", encoding="not-a-real-encoding")

    assert not target.exists()
    leftovers = list(tmp_path.glob("*.tan-tmp"))
    assert leftovers == [], leftovers


def test_a_fsync_failure_leaves_the_original_untouched_and_no_temp_leftover(tmp_path, monkeypatch):
    """A failure INSIDE the durability sequence -- after `write` has already
    put bytes in the temp's own buffer but before the temp is durable or the
    rename happens -- must leave the real file byte-identical (the write never
    opens it) and must not leak the temp sibling."""
    target = tmp_path / "config"
    target.write_text("old\n", encoding="utf-8")

    def boom_fsync(_fd):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(atomic_write_mod.os, "fsync", boom_fsync)

    with pytest.raises(OSError):
        atomic_write_text(str(target), "new\n")

    assert target.read_text(encoding="utf-8") == "old\n"
    leftovers = list(tmp_path.glob("*.tan-tmp"))
    assert leftovers == [], leftovers


# ── atomic_write_bytes: the byte-exact sibling, #904 third round nit ────────


def test_atomic_write_bytes_round_trips_content_atomic_write_text_could_not(tmp_path):
    """The one property that justifies a separate function instead of a
    decode-then-`atomic_write_text` call at the caller: bytes that are not
    valid UTF-8 -- exactly the shape a hand-edited or malformed
    `~/.alp/sdk-defaults.json` snapshot can legitimately carry -- must round
    trip unchanged."""
    target = tmp_path / "sdk-defaults.json"
    payload = b'{"/proj": {"sdkPath": "/sdk"}}\xff\xfe'
    with pytest.raises(UnicodeDecodeError):
        payload.decode("utf-8")  # the precondition this test exists to cover

    atomic_write_bytes(str(target), payload)

    assert target.read_bytes() == payload


def test_atomic_write_bytes_leaves_the_original_untouched_on_a_failed_replace(
    tmp_path, monkeypatch
):
    """Same crash-safety contract as `atomic_write_text`'s own
    `test_cleans_up_the_temp_file_when_the_replace_fails`, proven against the
    bytes sibling directly rather than only inherited through the shared
    `_atomic_write_bytes` internals -- a future split of the two functions
    must not silently drop this for the bytes half."""
    target = tmp_path / "sdk-defaults.json"
    target.write_bytes(b"old-bytes")

    def boom_replace(_src, _dst):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(atomic_write_mod.os, "replace", boom_replace)

    with pytest.raises(OSError):
        atomic_write_bytes(str(target), b"new-bytes")

    assert target.read_bytes() == b"old-bytes"
    leftovers = list(tmp_path.glob("*.tan-tmp"))
    assert leftovers == [], leftovers
