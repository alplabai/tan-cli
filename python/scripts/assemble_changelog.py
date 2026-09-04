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
import os
import re
import sys
from pathlib import Path

# `tan.core.atomic_write` is this repo's ONE durable atomic-write
# implementation (tan-cli#516): it resolves a symlinked target before writing,
# `fsync`s the temp file's own descriptor before the rename AND the parent
# directory after it, and names its temp `tmpXXXXXXXX.tan-tmp`, which
# `.gitignore:70` covers. Imported rather than re-derived because its own
# docstring says why a third hand-rolled copy must not exist: two independent
# copies of exactly this sequence already drifted apart once (tan-cli#510,
# tan-cli#489/#516), which is what got it extracted in the first place.
#
# This script is not part of the installed package and is run as
# `python3 python/scripts/assemble_changelog.py` from the repo root, so it puts
# `python/` on the path itself instead of relying on `pip install -e ./python`
# having happened -- the same shape `bump_dev_version.py` and
# `regen_module_size_budget.py` already use. `sys.path` is searched by
# `PathFinder`, which precedes an editable install's own meta-path finder, so
# this deliberately binds THIS checkout's helper: the durability of a fold run
# from this tree must not depend on which other checkout happens to be
# pip-installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tan.core.atomic_write import atomic_write_text  # noqa: E402

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


def already_folded(
    section: list[str], buckets: dict[str, list[tuple[str, str]]]
) -> list[str]:
    """Return the names of fragments whose body is ALREADY in `section`.

    tan-cli#1181. There is one state this script can leave behind that it
    cannot undo: `CHANGELOG.md` folded with fragments still on disk, which is
    what a fold whose unlinks partly failed produces (see the survivor report
    in `main`). Folding again on that tree used to splice every survivor a
    SECOND time and exit 0 -- measured on this repo's own corpus: fold 162,
    restore one fragment, re-run, and the run printed
    `folded 1 fragment(s) into CHANGELOG.md` while that entry's lead sentence
    went 1 -> 2. A success-shaped output over a non-success state is the exact
    class tan-cli#1172 exists to eliminate.

    The test is EXACT, not fuzzy, and that is what makes it cheap enough to
    run unconditionally: `splice()` applies zero transformation -- it copies a
    fragment body byte-for-byte into the section -- so `body in section_text`
    is precisely "this fragment has already been folded here", not a
    similarity heuristic. Measured against the real 162-fragment corpus and
    the real pristine `CHANGELOG.md`: 0 false positives, and the survivor of a
    forced partial unlink is the only hit.

    Only the Unreleased section is searched. A body that also appears in a
    RELEASED section is a fragment being re-added deliberately, which is the
    author's business, not this script's.
    """
    section_text = "\n".join(section)
    return [
        name
        for category in CATEGORIES
        for name, body in (buckets.get(category) or [])
        if body in section_text
    ]


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
                         "(tan-cli#1172). Without it, this script only reports. "
                         "Refused (exit 2) alongside --check, --dry-run, or "
                         "--require-empty, which all promise to change nothing "
                         "(tan-cli#1181).")
    ap.add_argument("--root", type=Path, default=None,
                    help="repo root (default: discovered from this script's location)")
    args = ap.parse_args(argv)

    # tan-cli#1181: `--write` is the irreversible half; the other three flags
    # all promise, in their own help text, to change nothing. Combined, the
    # destructive one used to win silently -- `--dry-run --write` folded 162
    # fragments and deleted them, exit 0, from an invocation whose documented
    # meaning is "print the resulting CHANGELOG.md to stdout, write nothing",
    # and which `changelog.d/README.md` advertises as the safe look-first
    # form. Worse, the precedence was INCONSISTENT in exactly the direction
    # that loses data: `--check --write` was safe only because `--check` is
    # handled first, so the same operator learned two opposite lessons about
    # what a safe flag does when paired with `--write`.
    #
    # There is no reading of such a command line that is safe to guess: the
    # operator asked for both halves, so honouring either one silently does
    # something they also asked not to do -- and one of the two answers is
    # unrecoverable. Refuse, name the flags, and name what to run instead.
    # Exit 2 (not 1) marks it as a USAGE error, distinct from the exit 1 a
    # malformed fragment or a failed fold returns.
    conflicting = [
        name
        for name, requested in (
            ("--check", args.check),
            ("--require-empty", args.require_empty),
            ("--dry-run", args.dry_run),
        )
        if requested
    ]
    if args.write and conflicting:
        joined = ", ".join(conflicting)
        print(
            f"error: --write cannot be combined with {joined}. --write "
            "DELETES every file in changelog.d/, and "
            + ("those flags promise" if len(conflicting) > 1 else "that flag promises")
            + " to change nothing -- refusing rather than guessing which "
            "half you meant (tan-cli#1181).\n"
            f"       To look first:  assemble_changelog.py {conflicting[0]}\n"
            "       To fold:         assemble_changelog.py --write",
            file=sys.stderr,
        )
        return 2

    try:
        if args.root is not None:
            # `repo_root` settles only on a directory that holds BOTH
            # CHANGELOG.md and changelog.d/; `--root` bypassed that check
            # entirely, so `--write --root <dir with changelog.d but no
            # CHANGELOG.md>` died with a raw FileNotFoundError traceback and
            # exit 1 rather than this script's own `error: ...` contract.
            # Same predicate, same message shape, one code path's worth of
            # difference between the two ways of choosing a root.
            root = args.root
            if not (root / "CHANGELOG.md").is_file() or not (
                root / "changelog.d"
            ).is_dir():
                raise AssembleError(
                    f"--root {root} does not contain both CHANGELOG.md and "
                    "changelog.d/. Refusing rather than failing partway "
                    "through with a traceback."
                )
        else:
            root = repo_root(Path(__file__).resolve().parent)
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

        # tan-cli#1181: refuse the folded-plus-survivors tree rather than
        # folding its survivors a second time. Checked here, before the
        # splice, so it covers the RENDER too -- a bare run or `--dry-run` on
        # such a tree would otherwise print a CHANGELOG with the entry twice
        # and call it the result. Refusing (rather than skipping and deleting)
        # is the conservative half: a skip would delete a fragment on the
        # strength of an inference, and the operator who has this tree needs
        # to know it exists.
        duplicated = already_folded(lines[start + 1:end], buckets)
        if duplicated:
            raise AssembleError(
                f"{len(duplicated)} fragment(s) are ALREADY present in "
                f"{changelog.relative_to(root)}'s Unreleased section:\n  "
                + "\n  ".join(duplicated)
                + "\nThis is the folded-plus-survivors state a fold whose "
                "unlinks failed leaves behind. Folding again would splice "
                "each of them a second time and exit 0 with a line that "
                "reads like success. Delete those fragment file(s) -- their "
                "text is already in the changelog -- and re-run."
            )

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

        # tan-cli#1181: temp file + os.replace, not truncate-then-write. The
        # fragments are unlinked immediately below, so a crash or ENOSPC
        # partway THROUGH the write would leave CHANGELOG.md truncated with
        # the text that was supposed to replace it already deleted. os.replace
        # is atomic within a filesystem: CHANGELOG.md is either wholly the old
        # file or wholly the new one, never a prefix of either.
        #
        # `atomic_write_text` rather than a hand-rolled sequence here, because
        # the first hand-rolled one got three things wrong that it already has
        # right:
        #
        #   * A SYMLINKED CHANGELOG.md was CLOBBERED instead of written
        #     through. `os.replace(tmp, changelog)` puts a regular file where
        #     the link was, so the fold landed on the wrong inode. Measured on
        #     a copy of this repo with `CHANGELOG.md -> real/CHANGELOG.md`:
        #     exit 0, stdout `folded 162 fragment(s) into CHANGELOG.md`,
        #     `real/CHANGELOG.md` still md5 4cc00bd3446d5718b8eabb27b50d1744
        #     and still unfolded, the repo-root path now a new 1261455-byte
        #     regular file with the symlink gone, and all 162 fragments
        #     deleted. The `Path.write_text` this replaced FOLLOWED the link;
        #     `atomic_write_text` resolves the real target first, and creates
        #     its temp beside THAT (same filesystem, or `os.replace` fails
        #     EXDEV).
        #   * `os.fsync` on the temp makes the BYTES durable, not the RENAME.
        #     A power cut in between leaves precisely the state the survivor
        #     report below says is impossible: CHANGELOG.md at its old content
        #     with the fragments already gone. `atomic_write_text` also fsyncs
        #     the parent directory after the replace (POSIX only; Windows
        #     journals the rename itself).
        #   * The temp was named `CHANGELOG.md.tmp`, which no `.gitignore`
        #     rule matched -- a SIGKILL mid-fsync left a 1261455-byte
        #     untracked, committable file in the repo root, and the next
        #     successful run truncated it without a word. `atomic_write_text`
        #     uses `tempfile.mkstemp(dir=..., suffix=".tan-tmp")`, which
        #     `.gitignore:70` covers, matching the repo's two other
        #     atomic-write producers documented at `.gitignore:53-69`.
        try:
            # `atomic_write_text` writes the encoded bytes verbatim -- its
            # other callers (`.vscode/launch.json`, `.west/config`) want no
            # newline translation. This script does: the `Path.write_text` it
            # replaced opened in text mode with newline=None, which translates
            # "\n" to `os.linesep` on write, so on Windows CHANGELOG.md is
            # CRLF and must stay CRLF. `text` is built by `"\n".join(...)` and
            # holds no "\r", so this reproduces that translation exactly, and
            # is a no-op wherever `os.linesep` is already "\n".
            atomic_write_text(str(changelog), text.replace("\n", os.linesep))
        except OSError as exc:
            raise AssembleError(
                f"failed to write {changelog.relative_to(root)}: {exc}. "
                f"Nothing was deleted -- {frag_dir.relative_to(root)}/ is "
                "untouched and the fold can simply be re-run."
            ) from exc

        # Unlink AFTER the replace, so there is no state where the fragments
        # are gone and CHANGELOG.md is unwritten. The reverse window does
        # survive -- CHANGELOG.md folded with fragments still on disk -- so a
        # failed unlink is reported and exits nonzero, naming each survivor
        # and its errno, instead of printing a success line over a tree that
        # is in neither state. A PARTIAL failure is the interesting one: the
        # named set is exactly the set still on disk, so the operator can act
        # on the message alone. `already_folded` above is the second half --
        # a re-run on that tree is refused rather than folding the survivors
        # again.
        survivors: list[str] = []
        for category in CATEGORIES:
            for name, _ in buckets[category]:
                try:
                    (frag_dir / name).unlink()
                except OSError as exc:
                    survivors.append(f"{name}: {exc}")
        if survivors:
            print(
                f"error: {changelog.relative_to(root)} was folded, but "
                f"{len(survivors)} of {total} fragment(s) could not be "
                "deleted:\n  " + "\n  ".join(sorted(survivors)) + "\n"
                "Delete them by hand -- their text is ALREADY in "
                f"{changelog.relative_to(root)}. Re-running --write on this "
                "tree is refused rather than folding each of them a second "
                "time (tan-cli#1181), so deleting them is the way forward.",
                file=sys.stderr,
            )
            return 1

        print(f"folded {total} fragment(s) into {changelog.relative_to(root)}")
        return 0

    except AssembleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
