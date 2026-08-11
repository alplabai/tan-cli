# SPDX-License-Identifier: Apache-2.0
"""Unit tests for `tests.conftest._probe_free_path` itself, host-independent
(tan-cli#603 follow-up review, blocker 3).

Every existing exercise of this function is INDIRECT: the session-scoped
`_probe_tools_are_a_property_of_the_test` fixture calls it once, against
whatever this host's real PATH happens to contain, and nothing asserts
anything about the REBUILT PATH's shape -- only about identities being
unresolvable afterwards (`tests/gates/test_probe_tool_inventory.py`). On a
host with no probe tooling at all (every CI runner, by design) that indirect
exercise is a no-op and proves nothing about the farming logic itself:
ordering, shadowing, duplicate entries, non-probe survivors, or the
lazy-scratch contract. These tests build synthetic PATHs by hand instead, so
they run -- and can fail -- on any host, CI included.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.conftest import PROBE_TOOLS, _probe_free_path


def _mkfile(path, executable: bool = True):
    path.write_text("", encoding="utf-8")
    if executable and os.name != "nt":
        os.chmod(path, 0o755)
    return path


def _one_probe_tool() -> str:
    """Any single [`PROBE_TOOLS`] identity -- which one is irrelevant to
    every test here, only that it IS one."""
    return sorted(PROBE_TOOLS)[0]


class _CountedFactory:
    """A `scratch_factory` that counts its own calls and always answers the
    SAME directory, so the lazy-call contract (`_probe_free_path` must call
    it at most once, and only when farming is actually needed) is directly
    assertable rather than inferred from filesystem side effects."""

    def __init__(self, root):
        self._root = root
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self._root


def test_a_path_with_no_probe_tool_is_untouched_and_never_touches_disk(tmp_path):
    """The documented no-op path: a PATH entry that carries no [`PROBE_TOOLS`]
    identity is returned unrebuilt (`None`), and the scratch factory -- the
    ONLY thing that would do filesystem work -- is never even called."""
    clean = tmp_path / "clean"
    clean.mkdir()
    _mkfile(clean / "sh")
    _mkfile(clean / "git")

    factory = _CountedFactory(tmp_path / "unused-scratch")
    result = _probe_free_path(str(clean), factory)

    assert result is None
    assert factory.calls == 0
    assert not factory._root.exists(), (
        "the scratch directory must not be created on the no-match path -- "
        "that is the whole point of the lazy factory"
    )


def test_the_scratch_factory_is_called_at_most_once(tmp_path):
    """Two DIFFERENT PATH entries each carrying a probe tool must still only
    request the scratch root ONCE -- every farm lives under that one root, at
    its own `probe-free-<index>` subdirectory."""
    tool = _one_probe_tool()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _mkfile(first / tool)
    _mkfile(second / tool)

    factory = _CountedFactory(tmp_path / "scratch")
    result = _probe_free_path(os.pathsep.join([str(first), str(second)]), factory)

    assert result is not None
    assert factory.calls == 1


def test_ordering_is_preserved_and_only_matching_entries_are_rebuilt(tmp_path):
    """A PATH of [clean, dirty, clean] must come back [clean, farm, clean] --
    same length, same positions, the untouched entries returned VERBATIM
    (not even copied), and only the dirty one replaced."""
    tool = _one_probe_tool()
    clean_a = tmp_path / "clean_a"
    dirty = tmp_path / "dirty"
    clean_b = tmp_path / "clean_b"
    for d in (clean_a, dirty, clean_b):
        d.mkdir()
    _mkfile(clean_a / "sh")
    _mkfile(dirty / tool)
    _mkfile(dirty / "sh")
    _mkfile(clean_b / "git")

    original = os.pathsep.join([str(clean_a), str(dirty), str(clean_b)])
    result = _probe_free_path(original, lambda: tmp_path / "scratch")
    assert result is not None
    entries = result.split(os.pathsep)

    assert len(entries) == 3
    assert entries[0] == str(clean_a)
    assert entries[2] == str(clean_b)
    assert entries[1] != str(dirty), "the dirty entry must be replaced, not passed through"


def test_duplicate_path_entries_are_farmed_independently(tmp_path):
    """The same directory listed twice in PATH (a routine misconfiguration,
    not a hostile fixture) must not collide: each occurrence gets its own
    farm, keyed by its own POSITION, and the original directory is left
    completely alone (a farm sharing a name across the two would either
    collide on `mkdir` or silently alias one occurrence's tools onto the
    other's)."""
    tool = _one_probe_tool()
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    _mkfile(dirty / tool)
    _mkfile(dirty / "sh")

    original = os.pathsep.join([str(dirty), str(dirty)])
    result = _probe_free_path(original, lambda: tmp_path / "scratch")
    assert result is not None
    entries = result.split(os.pathsep)

    assert len(entries) == 2
    assert entries[0] != entries[1], "each occurrence must get its own farm directory"
    assert (dirty / tool).is_file(), "the ORIGINAL directory must be left untouched"


def test_non_probe_tools_survive_in_the_farm(tmp_path):
    """A directory's non-probe contents must all still resolve from the farm
    -- this is the whole reason a farm exists rather than just dropping the
    directory: `tests/gates/test_probe_tool_inventory.py::
    test_ordinary_host_tooling_is_untouched` depends on exactly this."""
    tool = _one_probe_tool()
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    _mkfile(dirty / tool)
    _mkfile(dirty / "git")
    _mkfile(dirty / "python3")
    (dirty / "some-subdir").mkdir()
    _mkfile(dirty / "some-subdir" / "nested")

    result = _probe_free_path(str(dirty), lambda: tmp_path / "scratch")
    assert result is not None
    farm = Path(result)

    assert (farm / "git").is_file()
    assert (farm / "python3").is_file()
    assert (farm / "some-subdir").is_dir(), (
        "a subdirectory (not itself a probe tool) must survive too -- "
        "os.link refuses directories outright, so this exercises the "
        "copy/symlink-with-target_is_directory path"
    )
    assert (farm / "some-subdir" / "nested").is_file()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs elevation/dev mode on Windows CI")
def test_a_symlink_to_a_directory_is_still_target_is_directory_true(tmp_path, monkeypatch):
    """A PATH entry can hold a symlink whose TARGET is a directory (a stray
    `__pycache__`-adjacent link, a vendored tool's own symlinked support
    tree). `os.path.isdir(source)` already follows that symlink -- an
    earlier draft additionally excluded `os.path.islink(source)` sources from
    the directory check, which handed `target_is_directory=False` to exactly
    this case: the broken file-type symlink the directory branch exists to
    prevent, one level of indirection later (tan-cli#625 review). Verified by
    spying on the real `os.symlink` call rather than by the resulting
    behaviour, because `target_is_directory` is a silent no-op on POSIX --
    only the ARGUMENT proves the fix, not what the symlink does once made."""
    real_dir = tmp_path / "real_subdir"
    real_dir.mkdir()
    _mkfile(real_dir / "inner")
    linked_dir = tmp_path / "linked_subdir"
    os.symlink(real_dir, linked_dir, target_is_directory=True)

    dirty = tmp_path / "dirty"
    dirty.mkdir()
    tool = _one_probe_tool()
    _mkfile(dirty / tool)
    # The entry under test: a SYMLINK, sitting in a PATH directory that also
    # needs farming (the probe tool forces that), whose target is a
    # directory.
    os.symlink(linked_dir, dirty / "link_to_a_dir", target_is_directory=True)

    calls: dict[str, bool] = {}
    real_symlink = os.symlink

    def spy_symlink(source, link, *, target_is_directory=False):
        calls[Path(link).name] = target_is_directory
        return real_symlink(source, link, target_is_directory=target_is_directory)

    monkeypatch.setattr(os, "symlink", spy_symlink)

    result = _probe_free_path(str(dirty), lambda: tmp_path / "scratch")
    assert result is not None
    assert calls.get("link_to_a_dir") is True, (
        f"a symlink whose target is a directory must be recreated with "
        f"target_is_directory=True; recorded calls: {calls}"
    )


@pytest.mark.parametrize("tool", sorted(PROBE_TOOLS))
def test_every_probe_tool_becomes_unresolvable_in_the_farm(tmp_path, tool):
    """Direct, per-identity proof that farming actually REMOVES the probe
    tool -- not just that it changes the PATH string. Every stem in
    [`PROBE_TOOLS`], one at a time, so a single identity silently surviving a
    future refactor cannot hide behind the other nine."""
    dirty = tmp_path / f"dirty-{tool}"
    dirty.mkdir()
    _mkfile(dirty / tool)
    _mkfile(dirty / "sh")

    result = _probe_free_path(str(dirty), lambda: tmp_path / "scratch")
    assert result is not None
    farm = Path(result)

    assert not (farm / tool).exists(), f"{tool!r} must not survive into the farm"
    assert (farm / "sh").is_file()


def test_a_windows_style_extension_still_matches_by_stem(tmp_path):
    """`_probe_free_path` matches by extension-stripped stem, case-insensitive
    -- `JLinkExe.exe`/`West.CMD` are the same identity as their bare POSIX
    names, and that match must fire regardless of which host runs the test."""
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    tool = _one_probe_tool()
    _mkfile(dirty / f"{tool}.EXE")
    _mkfile(dirty / "sh")

    result = _probe_free_path(str(dirty), lambda: tmp_path / "scratch")
    assert result is not None
    farm = Path(result)
    assert not any(p.stem.lower() == tool.lower() for p in farm.iterdir())
    assert (farm / "sh").is_file()


def test_the_temurin_jlink_collision_is_caught_by_a_lowercase_stem(tmp_path):
    """The literal windows-latest sighting (tan-cli#625 review), not a
    hypothetical: the pre-installed Temurin JDK ships `jlink.exe` -- its own
    module-linker tool, nothing to do with a debug probe -- ALL LOWERCASE,
    which collides with the bare `"JLink"` identity only because the match
    lowercases BOTH sides (`os.path.splitext(n)[0].lower() in lowered`,
    `lowered` itself built from `t.lower() for t in PROBE_TOOLS`).

    `test_a_windows_style_extension_still_matches_by_stem` above varies only
    the EXTENSION's case on a stem that already matches `"JLink"`'s own
    casing (`JLink` + `.EXE`); it does not exercise the STEM casing at all,
    which is what the real collision needed. A mutant that drops the
    `.lower()` normalisation (`os.path.splitext(...)[0] in PROBE_TOOLS`
    verbatim) passes every other test in this file and in
    `test_probe_tool_inventory.py` -- this is the one that catches it."""
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    _mkfile(dirty / "jlink.exe")
    _mkfile(dirty / "sh")

    result = _probe_free_path(str(dirty), lambda: tmp_path / "scratch")
    assert result is not None, (
        "a lowercase 'jlink.exe' must still be recognised as the bare "
        "'JLink' PROBE_TOOLS identity -- this is the Temurin JDK collision "
        "measured directly on windows-latest"
    )
    farm = Path(result)
    assert not (farm / "jlink.exe").exists()
    assert (farm / "sh").is_file()


def test_empty_and_missing_path_entries_pass_through(tmp_path):
    """A doubled separator (`":/nope::/x"`) or a directory that does not
    exist must be preserved verbatim, not raise -- both are routine PATH
    hygiene issues this function has no business rejecting."""
    missing = str(tmp_path / "does-not-exist")
    original = os.pathsep.join(["", missing])
    factory = _CountedFactory(tmp_path / "scratch")
    result = _probe_free_path(original, factory)
    assert result is None
    assert factory.calls == 0
