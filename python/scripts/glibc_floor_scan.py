#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Measure the glibc floor of a PyInstaller ``--onedir`` payload, and refuse
rather than guess when the tree it is pointed at is not that payload.

tan-cli#450. This scan used to be a heredoc inside `release.yml`'s container
leg -- a job that runs on a `v*` tag and on nothing else. That placement is
what turned one defect into a spent tag rather than a red PR: the scan read
`.build/tan/PKG-00.toc`, tan-cli#349 switched the freeze to `--onedir` so that
file stopped enumerating the payload, and the first thing that noticed was the
`v0.5.0` GA tag publishing zero assets. The rc tags did not catch it either --
they predated `--onedir`, so the scan's input had not degraded yet.

Two properties follow from this being a FILE rather than a heredoc, and both
are the point of the move:

  * `clean-host.yml`'s `freeze-and-smoke` job already freezes this exact
    artifact in this exact container on every pull request. It can now run the
    scan BY NAME, which is the PR-time exercise tan-cli#450 asks for -- without
    paying for a second, differently-built freeze that could drift from the
    shipping one.
  * The refusal is unit-testable off a runner. While the logic was a heredoc
    nested in a `docker run` inside a tag-only job, the only way to prove it
    goes red on the broken input was to push a tag, and a tag cannot be
    un-pushed. `tests/scripts/test_glibc_floor_scan.py` drives this code with a
    2-file tree and asserts the exact refusal string.

WHAT "REFUSING TO GUESS" BUYS
-----------------------------

The floor is the MAXIMUM `GLIBC_` version any shipped ELF asks its loader for:
a host whose glibc is older cannot run the binary at all, so the number is a
hard compatibility claim published beside the asset. Deriving it from a partial
payload does not produce a slightly-wrong number -- it produces a confidently
wrong one that is too LOW, the direction that breaks a customer rather than
inconveniencing one. Hence the two-sided guard: a plausible file count AND at
least one version found, or exit non-zero.

`_MIN_NATIVE_FILES = 5` is a floor on the count, not a measurement of it. The
current tree measures 63 native ELF files; the broken `.toc` path saw 2. Five
is far enough below 63 to survive ordinary churn (a dropped extension module, a
slimmer CPython) and far enough above 2 to catch a payload that is not the
payload.

WHY THE VERSION READER IS INJECTABLE
-------------------------------------

`glibc_versions` is a parameter of `scan`, resolved at call time (NOT bound as
a default argument, which is evaluated once at import and would make a
`monkeypatch.setattr` on the module silently ineffective -- the test would then
exercise the real reader while believing it had swapped it, and pass for the
wrong reason). The defect this file exists for was never in ELF parsing -- it was in
WHICH TREE gets walked and whether a thin result is trusted. Injecting the
reader lets the non-vacuity test drive the walk, the count and the refusal with
synthetic ``\\x7fELF`` payloads on ANY host, rather than skipping everywhere a
real versioned ELF is unavailable -- macOS and Windows are both required legs,
and a test that only runs on one of three is not the gate it looks like.
`test_glibc_floor_scan.py` keeps a separate, ELF-host-only arm that runs the
real pyelftools reader over the host's own libraries, so the parsing half is
still exercised where there are ELFs to parse.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Iterable

#: An ELF file's first four bytes. The scan filters on the magic rather than on
#: a suffix because a PyInstaller payload's shared objects carry every naming
#: convention CPython and its wheels use (`.so`, `.so.6`, `.cpython-312-*.so`)
#: alongside the extension-less main binary.
ELF_MAGIC = b"\x7fELF"

#: Below this many native files the tree is not a `--onedir` payload, whatever
#: else it is. See the module docstring for how the bound was chosen.
_MIN_NATIVE_FILES = 5


class UnreadableElf(Exception):
    """Raised by a version reader when a magic-matched file is not a real ELF.

    The magic-byte filter in ``iter_native_files`` only proves the first four
    bytes are ``\\x7fELF``; it says nothing about whether the rest of the file
    is a parseable ELF. The heredoc this scan replaced counted a file toward
    ``_MIN_NATIVE_FILES`` only after ``ELFFile(f)`` had actually constructed
    successfully -- counting on magic bytes alone, as an earlier revision of
    this module did, makes the guard strictly weaker than that: a directory of
    truncated or corrupted magic-only files could clear the count guard while
    carrying no real payload, which is exactly the kind of thin result the
    guard exists to refuse. ``scan`` catches this and excludes the file from
    both the count and the version set, rather than silently treating an
    unreadable file as one that needs nothing.
    """


def _read_glibc_versions(path: Path) -> set[str]:
    """Return every ``GLIBC_*`` symbol version ``path`` asks its loader for.

    :param path: an ELF file, already confirmed by its magic bytes.
    :return: the ``GLIBC_x.y`` names in its ``.gnu.version_r`` section; may be
        empty for a file that parsed fine but carries no versioned needs.
    :raises UnreadableElf: if pyelftools cannot parse ``path`` at all -- a
        magic-matched file that is not actually a readable ELF must not count
        toward the caller's native-file guard.
    """
    from elftools.common.exceptions import ELFError
    from elftools.elf.elffile import ELFFile
    from elftools.elf.gnuversions import GNUVerNeedSection

    versions: set[str] = set()
    try:
        with open(path, "rb") as handle:
            elf = ELFFile(handle)
            for section in elf.iter_sections():
                if isinstance(section, GNUVerNeedSection):
                    for _, auxes in section.iter_versions():
                        versions.update(
                            aux.name for aux in auxes if aux.name.startswith("GLIBC_")
                        )
    except (ELFError, OSError) as exc:
        raise UnreadableElf(str(path)) from exc
    return versions


def iter_native_files(root: Path) -> Iterable[Path]:
    """Yield every file under ``root`` whose first four bytes are the ELF magic.

    :param root: the payload directory to walk, recursively.
    :return: the matching paths, sorted, so the count and the reported order
        are stable across filesystems.
    """
    for candidate in sorted(root.rglob("*")):
        if not candidate.is_file():
            continue
        try:
            with open(candidate, "rb") as handle:
                if handle.read(4) != ELF_MAGIC:
                    continue
        except OSError:
            continue
        yield candidate


def resolve_floor(versions: set[str]) -> str:
    """Return the highest ``GLIBC_x.y`` in ``versions``, compared numerically.

    :param versions: a non-empty set of ``GLIBC_x.y`` names.
    :return: the maximum by numeric component -- NOT lexicographic, under which
        ``GLIBC_2.30`` sorts below ``GLIBC_2.4``. Publishing ``2.4`` as the
        floor of a ``2.30`` binary is the wrong-and-too-low direction the
        module docstring warns about.
    """
    return max(versions, key=lambda v: tuple(int(n) for n in v.split("_")[1].split(".")))


def scan(
    root: Path,
    glibc_versions: Callable[[Path], set[str]] | None = None,
) -> tuple[str, list[Path], set[str]]:
    """Measure ``root``'s glibc floor, or refuse with the reason.

    :param root: the onedir payload directory.
    :param glibc_versions: reader for one file's ``GLIBC_`` needs; injectable
        so the walk-and-refuse logic is testable without real versioned ELFs.
        Resolved at CALL time, not bound as a default -- a default argument is
        evaluated once at import, so ``monkeypatch.setattr(mod,
        "_read_glibc_versions", ...)`` would silently not take effect and the
        test would exercise the real reader while believing it had swapped it.
        May raise ``UnreadableElf`` for a magic-matched file it cannot parse;
        such a file is excluded from both the returned paths and the guard
        count rather than counted as one that needs nothing.
    :return: ``(floor, native_files, versions_seen)`` -- ``native_files`` only
        the ones the reader actually parsed.
    :raises SystemExit: with a message -- never a bare non-zero -- so a CI log
        says which of the two guards refused, and with what numbers.
    """
    if not root.is_dir():
        raise SystemExit(
            f"{root}/ is missing -- the onedir freeze did not produce "
            "the tree this floor scan measures"
        )

    read = glibc_versions if glibc_versions is not None else _read_glibc_versions
    paths: list[Path] = []
    versions: set[str] = set()
    for native in iter_native_files(root):
        try:
            native_versions = read(native)
        except UnreadableElf:
            continue
        paths.append(native)
        versions.update(native_versions)

    if len(paths) < _MIN_NATIVE_FILES or not versions:
        raise SystemExit(
            "payload scan found %d native files / %d GLIBC_ versions under "
            "%s/ -- refusing to guess a floor" % (len(paths), len(versions), root)
        )

    return resolve_floor(versions), paths, versions


def main(argv: list[str] | None = None) -> int:
    """Run the scan and write the resolved floor.

    :param argv: command-line arguments, defaulting to ``sys.argv[1:]``.
    :return: ``0``; every refusal leaves through ``SystemExit`` instead.
    """
    parser = argparse.ArgumentParser(
        description="Measure the glibc floor of a PyInstaller --onedir payload."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("dist/tan"),
        help="the onedir payload to measure (default: dist/tan)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("dist/glibc-floor.txt"),
        help="where to write the resolved floor (default: dist/glibc-floor.txt)",
    )
    args = parser.parse_args(argv)

    floor, paths, versions = scan(args.root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(floor + "\n", encoding="utf-8")
    print(
        "payload floor over %d native files: %s (saw: %s)"
        % (len(paths), floor, " ".join(sorted(versions)))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
