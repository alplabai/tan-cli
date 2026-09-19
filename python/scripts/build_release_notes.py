#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Assemble the GitHub Release body: the CHANGELOG slice plus the assets block.

tan-cli#1276. `release.yml`'s "Slice CHANGELOG section for the release notes"
step used to build `release_notes.md` in two unconditional parts: the
CHANGELOG's `## [X.Y.Z]` section, verbatim, then the `## Release assets` block
(the four archive names, the measured glibc floor, the no-arm64/no-`-musl`
caveat, the attestation line) appended after it.

`softprops/action-gh-release@efb35369` (v3.0.3) silently truncates any body to
at most 124999 characters before publishing (`truncateReleaseNotes`,
`src/github.ts:254-257`: `input.substring(0, githubNotesMaxCharLength - 1)`
where `githubNotesMaxCharLength = 125000` -- the `- 1` means the action's own
ceiling is 124999, not 125000) -- and that count is JavaScript string length,
i.e. UTF-16 CODE UNITS, not Python's `len()` (which counts code points; an
astral character such as most emoji is one Python code point but a surrogate
PAIR, two UTF-16 units). Once the CHANGELOG section alone passes that limit,
the assets block -- appended after it -- is cut away completely and the job
still goes green. 0.7.0's section measured 770642 characters; its page would
have shipped with no install section, no glibc floor, no attestation
instructions.

This module fixes that by budgeting for the assets block FIRST: if the slice
plus block would land too close to the limit, the slice is replaced with a
short, honest pointer to the full CHANGELOG entry on GitHub (an ABSOLUTE URL --
a relative link in a release body resolves under `/blob/` and 404s). The
assets block itself is always present in the result. As a last resort, the
final assembly refuses (non-zero exit) rather than ever relying on the
action's own silent truncation.

The #212 refusals (a `## [X.Y.Z]` section that is missing, or present but
empty) and the glibc-floor-missing refusal are unchanged in spirit -- this
module keeps both, verbatim in message text, alongside the new size logic.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: `softprops/action-gh-release`'s own hard ceiling on a release body
#: (`truncateReleaseNotes`, v3.0.3 `src/github.ts:254-257`):
#: `return input.substring(0, githubNotesMaxCharLength - 1)` where
#: `githubNotesMaxCharLength = 125000` -- i.e. the action keeps at most
#: `125000 - 1 = 124999` UTF-16 code units, not 125000. A body of EXACTLY
#: 125000 units is not caught by `length > RELEASE_BODY_UTF16_HARD_LIMIT`
#: below and would silently lose its last character to the action's own
#: truncation -- the exact hazard this module exists to never rely on. The
#: refusal in `assemble_body` therefore compares with `>=`, not `>`.
RELEASE_BODY_UTF16_HARD_LIMIT = 125_000

#: The budget the CHANGELOG slice must fit under, ALONGSIDE the assets block
#: and the separator between them, before this module will publish it
#: verbatim -- safely below `RELEASE_BODY_UTF16_HARD_LIMIT` with margin. The
#: assets block itself runs at most a couple thousand UTF-16 units (it's a
#: fixed template plus one short glibc-floor string), so this leaves well
#: over ten thousand units of headroom against the hard limit for the
#: separator and any future growth of the block, while still publishing the
#: overwhelming majority of real sections verbatim (0.6.0's 105213-character
#: section fits; 0.7.0's 770642-character one does not).
SLICE_BUDGET_UTF16 = 110_000

#: `owner/repo`, for the pointer notice's absolute URL.
REPO = "alplabai/tan-cli"


def utf16_len(text: str) -> int:
    """Count `text` the way `action-gh-release`'s Node runtime does.

    :param text: any string destined for the release body.
    :return: the number of UTF-16 code units in `text`. This is NOT
        `len(text)`: Python's `len()` counts Unicode code points, and any
        character outside the Basic Multilingual Plane (most emoji, some CJK
        extension blocks) is one Python code point but a UTF-16 surrogate
        PAIR -- two units. Undercounting those is exactly the gap that let a
        body clear a Python-`len()`-based budget while still overflowing the
        JavaScript-`string.length`-based one the action actually enforces.
    """
    return len(text.encode("utf-16-le")) // 2


def slice_changelog_section(changelog_text: str, version: str) -> str:
    """Return the body of `CHANGELOG.md`'s `## [version]` section.

    :param changelog_text: the full contents of `CHANGELOG.md`.
    :param version: the tag's version, with no leading `v`
        (`${GITHUB_REF_NAME#v}`).
    :return: the section body, stripped of leading/trailing blank lines.
    :raises SystemExit: (#212) if the section is missing, or present but
        empty. A release with no notes is a broken release, not a degraded
        one, and the tag is immutable once pushed -- fail before publishing,
        not after.
    """
    header = re.compile(rf"^## \[{re.escape(version)}\]")
    out: list[str] = []
    capturing = found = False
    for line in changelog_text.splitlines(keepends=True):
        if header.match(line):
            capturing = found = True
            continue
        if capturing and line.startswith("## ["):
            break
        if capturing:
            out.append(line)
    body = "".join(out).strip()
    if not found:
        raise SystemExit(
            f"::error::CHANGELOG.md has no `## [{version}]` section, so this "
            f"release would publish with an empty body. The version bump "
            f"renames `## [Unreleased]` to `## [{version}] -- <date>`; that "
            f"edit is missing. Fix CHANGELOG.md on the release branch, then "
            f"re-tag."
        )
    if not body:
        raise SystemExit(
            f"::error::CHANGELOG.md's `## [{version}]` section is empty. A "
            f"release body has to say what changed; write the section, then "
            f"re-tag."
        )
    return body


def read_glibc_floor(path: Path) -> str:
    """Return the measured glibc floor, refusing to publish an unmeasured one.

    :param path: `meta/glibc-floor.txt`, written by the Linux build leg from
        the container-run `glibc_floor_scan.py`.
    :return: the floor string, e.g. `"GLIBC_2.30"`.
    :raises SystemExit: if the file is missing or empty -- the Linux build
        leg did not report a measured floor, and the notes must not state one
        nothing measured.
    """
    try:
        floor = path.read_text(encoding="utf-8").strip()
    except OSError:
        floor = ""
    if not floor:
        raise SystemExit(
            f"::error::{path} is missing or empty -- the Linux build leg did "
            f"not report a measured floor, and the notes must not state one "
            f"nothing measured."
        )
    return floor


def assets_block(glibc_floor: str) -> str:
    """Return the `## Release assets` block, with the measured floor filled in.

    :param glibc_floor: e.g. `"GLIBC_2.30"`, from `read_glibc_floor`.
    :return: the block, always the same shape -- this function's output is
        what `assemble_body` guarantees survives into the final body.
    """
    return f"""## Release assets

Four archives, each a PyInstaller --onedir freeze of the Python
`tan` (tan-cli#349 -- was a single-file --onefile freeze; --onedir
fixes a 13-19s macOS startup regression caused by --onefile
re-extracting its runtime on every invocation). Unpack the archive
and run the `tan`/`tan.exe` inside; `install.sh`/`install.ps1` do
this for you.

- `tan-x86_64-pc-windows-msvc.zip` -- Windows x64
- `tan-x86_64-apple-darwin.tar.gz` / `tan-aarch64-apple-darwin.tar.gz` -- macOS
- `tan-x86_64-unknown-linux-gnu.tar.gz` -- Linux x64, frozen on Debian 11.
  It requires **{glibc_floor}** or newer -- measured from the
  binary's own bundled payload at build time, not assumed from the
  build image. Debian 11+ / Ubuntu 20.04+ / RHEL 9+ are comfortably
  above it.

There is no arm64 Windows and no arm64 Linux asset in this release,
and no `-musl` asset. A frozen binary has to be built on the
architecture it runs on, and this release builds on four runners; if
you need an arm64 Linux or arm64 Windows `tan`, install from source
(`pip install ./python`) and say so on the issue tracker.

- Every archive + `checksums.txt` carries a GitHub build-provenance
  attestation. Verify with:
  `gh attestation verify <downloaded-file> --repo alplabai/tan-cli`"""


def too_long_notice(version: str, repo: str = REPO) -> str:
    """Return the honest pointer notice that replaces an oversized slice.

    :param version: the tag's version, no leading `v`.
    :param repo: `owner/name`, for the URL.
    :return: a short paragraph -- says plainly that the section was too long
        for a release body, points at the full entry with an ABSOLUTE URL
        (a relative link in a release body resolves under `/blob/` and
        404s), and leaves room for a human summary to follow.
    """
    return (
        f"This release's `CHANGELOG.md` section is too long to include in "
        f"full in a release body (GitHub release bodies are capped at "
        f"{RELEASE_BODY_UTF16_HARD_LIMIT - 1} UTF-16 code units; see "
        f"tan-cli#1276). Read the complete, unabridged entry here:\n\n"
        f"https://github.com/{repo}/blob/v{version}/CHANGELOG.md\n\n"
        f"A human-written summary of the highlights may follow as an edit to "
        f"this release page."
    )


def assemble_body(
    slice_body: str, block: str, version: str, repo: str = REPO
) -> str:
    """Join the CHANGELOG slice (or a pointer notice) with the assets block.

    :param slice_body: from `slice_changelog_section` -- already refused if
        missing/empty.
    :param block: from `assets_block` -- guaranteed present in the result;
        this is the whole fix (tan-cli#1276): it is decided FIRST whether the
        slice fits, never appended after the fact where a silent truncation
        could still cut it away.
    :param version: the tag's version, no leading `v`; used in the notice URL
        if the slice is replaced.
    :param repo: `owner/name`, for the notice URL.
    :return: the final release body.
    :raises SystemExit: if the body is still at or over
        `RELEASE_BODY_UTF16_HARD_LIMIT` even after replacing the slice with
        the notice -- this module never relies on the action's own silent
        truncation to make a body fit. The comparison is `>=`, not `>`: the
        action keeps only `RELEASE_BODY_UTF16_HARD_LIMIT - 1` units
        (`truncateReleaseNotes`'s own `- 1`, see that constant's docstring),
        so a body of exactly the limit still loses its last character if
        this let it through.
    """
    body = f"{slice_body}\n\n{block}"
    if utf16_len(body) > SLICE_BUDGET_UTF16:
        body = f"{too_long_notice(version, repo)}\n\n{block}"

    length = utf16_len(body)
    if length >= RELEASE_BODY_UTF16_HARD_LIMIT:
        raise SystemExit(
            f"::error::release notes body is {length} UTF-16 code units, "
            f"at or over action-gh-release's {RELEASE_BODY_UTF16_HARD_LIMIT}-unit "
            f"hard limit, even after replacing the CHANGELOG slice with a "
            f"pointer notice -- the `## Release assets` block itself must "
            f"fit under the limit with room to spare. Shrink the block "
            f"before re-tagging; never let the action truncate it silently."
        )
    return body


def main(argv: list[str] | None = None) -> int:
    """Read CHANGELOG.md + the measured glibc floor, write `release_notes.md`.

    :param argv: command-line arguments, defaulting to `sys.argv[1:]`.
    :return: `0`; every refusal leaves through `SystemExit` instead.
    """
    parser = argparse.ArgumentParser(
        description="Assemble a GitHub Release body that never loses the "
        "Release assets block to the action's silent 125000-unit truncation."
    )
    parser.add_argument(
        "--changelog", type=Path, default=Path("CHANGELOG.md"), help="path to CHANGELOG.md"
    )
    parser.add_argument(
        "--version", required=True, help="the tag's version, no leading v (e.g. 0.7.0)"
    )
    parser.add_argument(
        "--glibc-floor-file",
        type=Path,
        default=Path("meta/glibc-floor.txt"),
        help="path to the measured glibc floor, written by the Linux build leg",
    )
    parser.add_argument("--repo", default=REPO, help="owner/name, for the pointer notice URL")
    parser.add_argument(
        "--out", type=Path, default=Path("release_notes.md"), help="where to write the body"
    )
    args = parser.parse_args(argv)

    glibc_floor = read_glibc_floor(args.glibc_floor_file)
    print(f"measured glibc floor of the published Linux asset: {glibc_floor}")

    changelog_text = args.changelog.read_text(encoding="utf-8")
    slice_body = slice_changelog_section(changelog_text, args.version)
    block = assets_block(glibc_floor)
    body = assemble_body(slice_body, block, args.version, args.repo)

    args.out.write_text(body + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({utf16_len(body)} UTF-16 code units)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
