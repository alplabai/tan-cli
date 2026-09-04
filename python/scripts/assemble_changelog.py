#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fold `changelog.d/` fragments into CHANGELOG.md's Unreleased section.

WHY THIS EXISTS
---------------
`CHANGELOG.md` has one insertion point: the `###` lists under the
`## [X.Y.Z] — Unreleased` header. Every open PR appends there, so any two PRs
conflict on it by construction, and the conflict re-fires on every merge.
Measured 2026-08-11 across the seven conflicted tan-cli PRs open at the time,
`CHANGELOG.md` was conflicted in SIX, and was the ONLY conflicted file in
THREE -- PRs otherwise ready to merge, blocked purely by list contention.

One file per change makes that class of conflict impossible: disjoint files
cannot conflict. This script is the other half -- it puts the fragments back
into the one document the release actually slices, so `release.yml`'s
`## [X.Y.Z]` extraction is untouched and the release contract does not change.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not reformat, rewrap, summarise, or reorder the text INSIDE a
fragment. The house style here is deliberately long and carries verbatim
technical strings -- register names, error codes, flags, paths -- where a
"helpful" rewrap can corrupt meaning. Fragment content is copied byte-for-byte.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# The full Keep a Changelog set, in its canonical order. All six are here
# deliberately: `### Security` and `### Deprecated` both already appear in
# CHANGELOG.md, so a shorter list would REJECT a legitimate security fragment
# at release time -- the worst possible moment to discover it.
#
# A fragment category outside this set is a hard error rather than a
# silently-dropped entry. A changelog entry that vanishes without a word is
# the exact "reports success while doing something else" failure this repo
# keeps paying for, and for a security note it is worse than an inconvenience.
CATEGORIES = ("added", "changed", "deprecated", "removed", "fixed", "security")

# `## [0.5.2] — Unreleased` (em-dash U+2014, as the file actually uses) or
# `## [Unreleased]`. Both spellings appear across Alp Lab repos; accept either
# rather than silently matching nothing.
UNRELEASED_RE = re.compile(
    r"^##\s*\[(?P<version>[^\]]+)\]\s*(?:[—-]\s*Unreleased\s*)?$",
    re.IGNORECASE,
)


# A fence marker line: 3+ backticks, optionally followed by an info string
# (only meaningful on an opener -- see fragment_shape_errors). Matched against
# an already-`.strip()`ped line, so leading indentation is not part of this.
_FENCE_RE = re.compile(r"^(`{3,})(.*)$")


class AssembleError(RuntimeError):
    """A condition that must stop the release, not be worked around."""


def repo_root(start: Path) -> Path:
    """Walk up to the directory holding both CHANGELOG.md and changelog.d/."""
    for candidate in (start, *start.parents):
        if (candidate / "CHANGELOG.md").is_file() and (candidate / "changelog.d").is_dir():
            return candidate
    raise AssembleError(
        "could not locate a directory containing both CHANGELOG.md and "
        f"changelog.d/, starting from {start}"
    )


def fragment_shape_errors(name: str, body: str) -> list[str]:
    """Return structural defects in a fragment body, or `[]` if it is sound.

    tan-cli#930: `--check` validated the FILENAME contract and nothing about
    what was inside it -- a fragment whose entire body was the single line
    `not a bullet at all` passed clean. This is deliberately NOT a prose
    checker: whether a claim is true, a count is right, or a sentence reads
    well stays a human review problem forever (see the issue). What this
    checks is the one thing `splice()` (below) assumes and never verifies
    itself: `splice()` applies ZERO transformation to a fragment's text -- it
    joins bodies nose-to-tail with a blank line between them and inserts the
    result verbatim under a `### <Category>` heading. So a fragment that is
    not already a valid Markdown bullet list on its own is not one after
    splicing either; it lands in `CHANGELOG.md` as bare prose sitting under a
    bullet-list heading, which is exactly the measured defect this closes.
    Because `splice()` never reformats, there is no separate "assembler rule"
    for this check to drift from -- the fragment's own shape IS the shape it
    ships in.
    """
    errors: list[str] = []
    lines = body.split("\n")
    seen_bullet = False
    open_fence_len: int | None = None

    for line in lines:
        stripped = line.strip()
        fence_match = _FENCE_RE.match(stripped)
        if fence_match:
            ticks, rest = fence_match.group(1), fence_match.group(2)
            if open_fence_len is None:
                # Opens a fence. An info string (` ```python`) is allowed only
                # on the opener, so this is unconditionally an open.
                open_fence_len = len(ticks)
            elif len(ticks) >= open_fence_len and not rest.strip():
                # CommonMark: a fence only CLOSES on a marker with at least
                # as many backticks as the opener and nothing else on the
                # line. A shorter or text-trailing run of backticks (e.g. a
                # nested ``` inside an outer ```` fence) is fence CONTENT,
                # not a close -- counting every ``` regardless of length
                # falsely flagged that as unbalanced.
                open_fence_len = None
        if not line.strip():
            continue
        if line.startswith("- "):
            if not line[2:].strip():
                errors.append(
                    f"{name}: line {line!r} is a bullet marker with no "
                    "content -- it would land in CHANGELOG.md as a bare "
                    "`- ` with nothing readable after it"
                )
                continue
            seen_bullet = True
            continue
        if line[:1] in (" ", "\t"):
            if not seen_bullet:
                errors.append(
                    f"{name}: line {line!r} is indented as if continuing a "
                    "bullet, but no `- ` bullet precedes it"
                )
            continue
        errors.append(
            f"{name}: line {line!r} is not a Markdown bullet (`- ...`) and "
            "is not indented under one -- it would land in CHANGELOG.md as "
            "bare text under a bullet-list heading, not a list item"
        )

    if open_fence_len is not None:
        errors.append(
            f"{name}: unterminated ``` fence (opened with {open_fence_len} "
            "backtick(s), never closed) -- this would swallow whatever is "
            "spliced in after it into an unterminated code block"
        )

    return errors


def load_fragments(frag_dir: Path) -> dict[str, list[tuple[str, str]]]:
    """Return {category: [(filename, body), ...]} sorted by filename.

    Sorting by filename makes assembly DETERMINISTIC. Without it the entry
    order would follow directory iteration order, so the same fragments could
    produce a different CHANGELOG on two machines and the diff would look like
    a real change.
    """
    buckets: dict[str, list[tuple[str, str]]] = {c: [] for c in CATEGORIES}
    bad: list[str] = []
    shape_bad: list[str] = []

    for path in sorted(frag_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        parts = path.name[: -len(".md")].rsplit(".", 1)
        if len(parts) != 2 or parts[1].lower() not in CATEGORIES:
            bad.append(path.name)
            continue
        body = path.read_text(encoding="utf-8").strip("\n")
        if not body.strip():
            bad.append(f"{path.name} (empty)")
            continue
        shape_bad.extend(fragment_shape_errors(path.name, body))
        buckets[parts[1].lower()].append((path.name, body))

    if bad:
        raise AssembleError(
            "unusable fragment filename(s): "
            + ", ".join(sorted(bad))
            + f"\nexpected `<issue>.<category>.md` with category in {CATEGORIES}, "
            "and a non-empty body. Refusing to continue rather than dropping "
            "the entry silently."
        )
    if shape_bad:
        raise AssembleError(
            "malformed fragment content (tan-cli#930 -- `--check` now reads "
            "the body, not only the filename):\n  "
            + "\n  ".join(shape_bad)
        )
    return buckets


def find_unreleased(lines: list[str]) -> tuple[int, int]:
    """Return (header_index, end_index) for the Unreleased section.

    end_index is the index of the next `## ` header, or len(lines).
    """
    start = None
    for i, line in enumerate(lines):
        if UNRELEASED_RE.match(line.rstrip()):
            start = i
            break
    if start is None:
        raise AssembleError(
            "CHANGELOG.md has no `## [<version>] — Unreleased` (or "
            "`## [Unreleased]`) header. Refusing to guess where entries belong."
        )
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            return start, j
    return start, len(lines)


def splice(section: list[str], buckets: dict[str, list[tuple[str, str]]]) -> list[str]:
    """Insert each bucket's entries under its `###` heading, creating it if absent.

    Existing entries are KEPT and new ones appended after them: a fragment
    never replaces hand-written text already in the section.
    """
    out = list(section)

    for category in CATEGORIES:
        entries = buckets.get(category) or []
        if not entries:
            continue
        heading = f"### {category.capitalize()}"
        block = "\n\n".join(body for _, body in entries)

        idx = next(
            (i for i, l in enumerate(out) if l.strip().lower() == heading.lower()),
            None,
        )
        if idx is None:
            # No such heading yet -- append one at the end of the section,
            # preserving canonical order relative to headings that do exist is
            # not attempted here; the assembler runs once at release time and
            # the result is reviewed in the release PR.
            while out and not out[-1].strip():
                out.pop()
            out.extend(["", heading, "", *block.split("\n")])
            continue

        # Find the end of this heading's body: the next `###`/`##` or section end.
        end = len(out)
        for j in range(idx + 1, len(out)):
            if out[j].startswith("### ") or out[j].startswith("## "):
                end = j
                break
        tail = end
        while tail > idx + 1 and not out[tail - 1].strip():
            tail -= 1
        out[tail:tail] = ["", *block.split("\n")]

    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="report what would be folded; change nothing; refuses "
                         "(exit 1) a fragment with a bad filename or malformed "
                         "content (tan-cli#930), but never merely because "
                         "fragments are pending -- for that, see --require-empty")
    ap.add_argument("--require-empty", action="store_true",
                    help="exit 1 if any fragment remains unfolded (for a release gate)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the resulting CHANGELOG.md to stdout, write "
                         "nothing (this is also what a bare invocation does)")
    ap.add_argument("--write", action="store_true",
                    help="PERFORM THE FOLD: rewrite CHANGELOG.md and DELETE "
                         "every file in changelog.d/. Irreversible for any "
                         "fragment that is untracked or staged-but-uncommitted "
                         "(tan-cli#1172). Without it, this script only reports.")
    ap.add_argument("--root", type=Path, default=None,
                    help="repo root (default: discovered from this script's location)")
    args = ap.parse_args(argv)

    try:
        root = args.root or repo_root(Path(__file__).resolve().parent)
        frag_dir = root / "changelog.d"
        changelog = root / "CHANGELOG.md"

        buckets = load_fragments(frag_dir)
        total = sum(len(v) for v in buckets.values())

        if args.check or args.require_empty:
            for category in CATEGORIES:
                for name, _ in buckets[category]:
                    print(f"{category:8} {name}")
            print(f"{total} fragment(s) pending")
            if args.require_empty and total:
                print(
                    "::error::unfolded changelog fragments remain -- run "
                    "`python3 python/scripts/assemble_changelog.py --write` and "
                    "commit the result before tagging",
                    file=sys.stderr,
                )
                return 1
            return 0

        if not total:
            print("no fragments to fold")
            return 0

        lines = changelog.read_text(encoding="utf-8").splitlines()
        start, end = find_unreleased(lines)
        merged = lines[:start + 1] + splice(lines[start + 1:end], buckets) + lines[end:]
        text = "\n".join(merged).rstrip("\n") + "\n"

        if not args.write:
            # tan-cli#1172: the fold used to be the DEFAULT. A bare invocation
            # rewrote CHANGELOG.md and deleted every fragment, with no prompt,
            # exit 0, and a summary that reads like success -- and the next
            # `--check` then reported `0 fragment(s) pending`, which reads as
            # "nothing to do" rather than "everything is gone". It cost 157
            # fragments once, recovered only because they happened to be
            # tracked with a clean index at that moment.
            #
            # The safe intent is the common one: almost every interactive
            # reason to run this script is to SEE the rendered result. So that
            # is the default now, and the irreversible half has to be asked
            # for by name. The release flow says the name once, in one place.
            sys.stdout.write(text)
            if not args.dry_run:
                print(
                    f"\n-- {total} fragment(s) rendered above, NOTHING written. "
                    f"Pass --write to fold them into "
                    f"{changelog.relative_to(root)} and delete "
                    f"{frag_dir.relative_to(root)}/*.md.",
                    file=sys.stderr,
                )
            return 0

        changelog.write_text(text, encoding="utf-8")
        for category in CATEGORIES:
            for name, _ in buckets[category]:
                (frag_dir / name).unlink()
        print(f"folded {total} fragment(s) into {changelog.relative_to(root)}")
        return 0

    except AssembleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
