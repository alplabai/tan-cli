# SPDX-License-Identifier: Apache-2.0
"""The glibc floor scan must REFUSE the payload that shipped `v0.5.0` empty.

tan-cli#450 is explicit that a PR-time exercise which would have gone green on
the broken input adds nothing. So the load-bearing test here is not "a good
tree resolves a floor" -- it is that the exact shape the broken scan saw, two
native files carrying three `GLIBC_` versions, exits non-zero with the numbers
it counted in the message. The issue records both sides of that measurement:

    OLD (.build/tan/PKG-00.toc):  payload scan found 2 native files / 3 GLIBC_
                                  versions -- refusing to guess a floor  (exit 1)
    NEW (walk dist/tan/):         payload floor over 63 native files: GLIBC_2.30

Asserting the MESSAGE, not just the exit code: a refusal that reported the
wrong count, or that fired on the other guard, would still exit 1 and still
look green to a test that only checked the code.

Most tests here inject a fake version reader. That is deliberate, and it is not
a mock standing in for the thing under test -- the defect was never in ELF
parsing, it was in WHICH TREE gets walked and whether a thin result is trusted,
which is exactly what these drive. Injecting keeps them running on all three
required legs; `test_the_real_reader_resolves_a_floor_from_host_libraries`
covers the pyelftools half on a host that has both ELFs and the wheel. The real
coverage of that half is the container step itself, in `release.yml` and
`clean-host.yml`, which runs the scan over an actual `dist/tan/`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "glibc_floor_scan.py"
_spec = importlib.util.spec_from_file_location("glibc_floor_scan", _SCRIPT)
assert _spec and _spec.loader
gfs = importlib.util.module_from_spec(_spec)
sys.modules["glibc_floor_scan"] = gfs
_spec.loader.exec_module(gfs)


def _elf(path: Path, body: bytes = b"\x00" * 64) -> Path:
    """Write a file the magic-byte filter accepts as native."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gfs.ELF_MAGIC + body)
    return path


def _reader(versions: set[str]):
    """A version reader that reports the same needs for every file."""
    return lambda _path: set(versions)


def test_the_broken_two_file_payload_is_refused_with_the_counts_it_saw(tmp_path):
    # Arrange -- exactly what the pre-fix scan saw off .build/tan/PKG-00.toc.
    root = tmp_path / "dist" / "tan"
    _elf(root / "tan")
    _elf(root / "_internal" / "libpython3.12.so.1.0")

    # Act
    with pytest.raises(SystemExit) as excinfo:
        gfs.scan(root, glibc_versions=_reader({"GLIBC_2.17", "GLIBC_2.29", "GLIBC_2.30"}))

    # Assert -- the numbers, not merely a non-zero exit.
    assert str(excinfo.value) == (
        "payload scan found 2 native files / 3 GLIBC_ versions under "
        f"{root}/ -- refusing to guess a floor"
    )


def test_a_full_payload_resolves_a_floor(tmp_path):
    root = tmp_path / "dist" / "tan"
    _elf(root / "tan")
    for n in range(5):
        _elf(root / "_internal" / f"lib{n}.so")

    floor, paths, versions = gfs.scan(
        root, glibc_versions=_reader({"GLIBC_2.17", "GLIBC_2.30"})
    )

    assert floor == "GLIBC_2.30"
    assert len(paths) == 6
    assert versions == {"GLIBC_2.17", "GLIBC_2.30"}


def test_the_floor_is_the_numeric_maximum_not_the_lexicographic_one():
    # "GLIBC_2.30" < "GLIBC_2.4" as strings. Publishing 2.4 as the floor of a
    # 2.30 binary is the too-low direction that breaks a customer.
    assert gfs.resolve_floor({"GLIBC_2.4", "GLIBC_2.30", "GLIBC_2.9"}) == "GLIBC_2.30"


def test_a_payload_with_enough_files_but_no_versions_is_refused(tmp_path):
    root = tmp_path / "dist" / "tan"
    for n in range(6):
        _elf(root / f"lib{n}.so")

    with pytest.raises(SystemExit) as excinfo:
        gfs.scan(root, glibc_versions=_reader(set()))

    assert str(excinfo.value) == (
        "payload scan found 6 native files / 0 GLIBC_ versions under "
        f"{root}/ -- refusing to guess a floor"
    )


def test_a_missing_root_names_the_freeze_rather_than_the_scan(tmp_path):
    root = tmp_path / "dist" / "tan"

    with pytest.raises(SystemExit) as excinfo:
        gfs.scan(root, glibc_versions=_reader({"GLIBC_2.30"}))

    assert str(excinfo.value) == (
        f"{root}/ is missing -- the onedir freeze did not produce "
        "the tree this floor scan measures"
    )


def test_non_elf_files_are_not_counted(tmp_path):
    # A tree of the archive, the toc and the metadata would otherwise clear the
    # count guard while carrying no payload at all.
    root = tmp_path / "dist" / "tan"
    (root / "_internal").mkdir(parents=True, exist_ok=True)
    for n in range(20):
        (root / "_internal" / f"data{n}.json").write_text("{}", encoding="utf-8")
    _elf(root / "tan")

    with pytest.raises(SystemExit) as excinfo:
        gfs.scan(root, glibc_versions=_reader({"GLIBC_2.30"}))

    assert "found 1 native files" in str(excinfo.value)


def test_an_unparseable_elf_magic_file_does_not_count_toward_the_guard(tmp_path):
    # A file that starts with the ELF magic but is not a real ELF -- a
    # truncated download, a corrupted extension module -- must not count
    # toward the native-file guard just because the magic bytes matched. The
    # heredoc this scan replaced only counted a file after `ELFFile(f)` had
    # actually constructed; counting on magic bytes alone (as an earlier
    # revision of this module did) makes the guard strictly weaker than that.
    root = tmp_path / "dist" / "tan"
    _elf(root / "corrupt.so")
    for n in range(5):
        _elf(root / "_internal" / f"lib{n}.so")

    def reader(path: Path) -> set[str]:
        if path.name == "corrupt.so":
            raise gfs.UnreadableElf(str(path))
        return {"GLIBC_2.30"}

    floor, paths, versions = gfs.scan(root, glibc_versions=reader)

    assert floor == "GLIBC_2.30"
    assert len(paths) == 5
    assert all(p.name != "corrupt.so" for p in paths)


def test_a_payload_of_only_unparseable_elf_magic_files_is_refused(tmp_path):
    # If EVERY magic-matched file fails to parse, the count guard must see
    # zero native files, not the number of magic-only matches -- that is the
    # precise scenario the previous count-on-magic-bytes shape got wrong.
    root = tmp_path / "dist" / "tan"
    for n in range(6):
        _elf(root / f"corrupt{n}.so")

    def reader(path: Path) -> set[str]:
        raise gfs.UnreadableElf(str(path))

    with pytest.raises(SystemExit) as excinfo:
        gfs.scan(root, glibc_versions=reader)

    assert str(excinfo.value) == (
        "payload scan found 0 native files / 0 GLIBC_ versions under "
        f"{root}/ -- refusing to guess a floor"
    )


def test_the_real_reader_raises_unreadable_elf_on_a_magic_only_file(tmp_path):
    # Proves _read_glibc_versions itself raises rather than silently
    # returning an empty set for a file that only has the magic bytes right --
    # the property test_an_unparseable_elf_magic_file_does_not_count_toward_
    # the_guard exercises through an injected reader.
    pytest.importorskip(
        "elftools", reason="pyelftools is container-only; see the comment above"
    )
    bogus = _elf(tmp_path / "bogus.so", body=b"not a real elf body, just the magic")

    with pytest.raises(gfs.UnreadableElf):
        gfs._read_glibc_versions(bogus)


def test_main_writes_the_floor_where_the_upload_step_reads_it(
    tmp_path, capsys, monkeypatch
):
    root = tmp_path / "dist" / "tan"
    for n in range(6):
        _elf(root / f"lib{n}.so")
    monkeypatch.setattr(gfs, "_read_glibc_versions", _reader({"GLIBC_2.30"}))
    out = tmp_path / "dist" / "glibc-floor.txt"

    assert gfs.main(["--root", str(root), "--out", str(out)]) == 0

    # release.yml uploads this exact path as the `glibc-floor` artifact.
    assert out.read_text(encoding="utf-8") == "GLIBC_2.30\n"
    assert "payload floor over 6 native files: GLIBC_2.30" in capsys.readouterr().out


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="needs a host with versioned ELFs"
)
def test_the_real_reader_resolves_a_floor_from_host_libraries():
    # The pyelftools half, on the one required leg that has ELFs. Reads the
    # host's own libraries rather than a checked-in fixture: a committed ELF
    # would rot, and the point is that the reader works against real symbol
    # versioning.
    #
    # `pyelftools` is deliberately NOT a declared test dependency: it is
    # installed only inside the freeze container, by the two lines in
    # release.yml and clean-host.yml that run this scan for real over a real
    # `dist/tan/`. That container step IS the load-bearing coverage of the
    # reader; this arm is belt-and-braces for a developer who happens to have
    # the package. The import guard is SEPARATE from the platform guard above
    # on purpose -- collapsing them would report "needs a host with versioned
    # ELFs" on a Linux box that simply lacks the wheel, and the next reader
    # would believe the wrong thing. (Found the hard way: the platform skip
    # hid this on macOS and the ubuntu leg then failed with
    # ModuleNotFoundError.)
    pytest.importorskip(
        "elftools", reason="pyelftools is container-only; see the comment above"
    )
    candidates = [
        p
        for p in Path("/lib/x86_64-linux-gnu").glob("lib*.so*")
        if p.is_file() and not p.is_symlink()
    ][:20]
    if not candidates:
        pytest.skip("no host libraries found to read")

    seen: set[str] = set()
    for lib in candidates:
        seen |= gfs._read_glibc_versions(lib)
    if not seen:
        pytest.skip("host libraries carry no GLIBC_ version needs")

    assert gfs.resolve_floor(seen).startswith("GLIBC_")
