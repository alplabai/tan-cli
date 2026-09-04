<!-- SPDX-License-Identifier: Apache-2.0 -->
# `changelog.d/` — one file per change

Add your changelog entry as a **new file in this directory**, not by editing
`CHANGELOG.md`.

## Why

`CHANGELOG.md` has exactly one insertion point — the `### Added` / `### Changed`
/ `### Fixed` lists under the `## [X.Y.Z] — Unreleased` header. Every open PR
appends there, so **any two PRs conflict on it by construction**, and the
conflict re-fires on every merge: merge one PR and the rest go dirty again.

That is not a hypothetical. Measured on 2026-08-11 across the seven conflicted
PRs open at the time, `CHANGELOG.md` was a conflicted file in **six of them**,
and in **three it was the *only* conflicted file** — those PRs were otherwise
ready to merge and were blocked purely by contention over one list.

Disjoint files cannot conflict. One file per change removes the entire class.

## How

Create `changelog.d/<issue>.<category>.md`:

```
changelog.d/665.fixed.md
changelog.d/651.added.md
changelog.d/612.changed.md
```

- `<issue>` — the GitHub **issue** number this entry belongs to, never a PR
  number. The parser does not enforce this (it only has to make the filename
  unique; the number itself is not parsed for meaning), but a fragment named
  after the PR that implements the work sends a released `CHANGELOG.md`
  reader to a diff instead of a problem statement. This has cost real review
  rounds — #786, #787, #788, #882, #907, #913, #1012, #1016, #1017, #1024 are
  all instances of exactly this fix. If the work has no covering issue yet,
  file one first; don't borrow the PR's own number.
- `<category>` — one of `added`, `changed`, `deprecated`, `removed`, `fixed`,
  `security`. It selects the `###` heading the entry lands under. All six are
  live in `python/scripts/assemble_changelog.py`'s `CATEGORIES`, and a category
  outside that set is a hard error rather than a dropped entry. `security` is
  the one worth knowing about: file a security note as `<n>.fixed.md` and the
  assembler takes it without complaint, landing it under `### Fixed` where it
  loses the heading that made it findable.

The file contains **the markdown bullet(s) exactly as they should appear** in
`CHANGELOG.md` — same voice, same depth, same verbatim technical strings. The
assembler does not reformat, rewrap, or summarise; what you write is what ships.

```markdown
- **`tan flash` refuses a `jlink_serial` it cannot honour instead of silently
  ignoring it.** The J-Link arm accepted the key and dropped it, so a two-probe
  bench flashed whichever probe enumerated first. Now `flash.serial-unsupported`.
```

Multi-paragraph entries and sub-bullets are fine — keep the existing house
style, which runs long deliberately (the CHANGELOG is the exhaustive record;
the release page is the summary — see `alp-lab:writing-release-notes`).

## Release time

`python/scripts/assemble_changelog.py --write` folds every fragment into
`CHANGELOG.md` under the current `Unreleased` header, in canonical section
order, then deletes the fragments.

**`--write` is required, and that is deliberate (tan-cli#1172).** The fold used
to be the default, so typing the script's name rewrote `CHANGELOG.md` and
deleted every fragment — no prompt, exit 0, and a summary that reads like
success. It cost 157 fragments once. A fragment you have written but not yet
`git add`ed is unrecoverable that way, and drafting is exactly when you would
run this to see how it renders.

```sh
python3 python/scripts/assemble_changelog.py           # render to stdout, change NOTHING
python3 python/scripts/assemble_changelog.py --write    # fold + delete fragments (release only)
python3 python/scripts/assemble_changelog.py --check    # report what is pending, change nothing
python3 python/scripts/assemble_changelog.py --dry-run  # same render as a bare run, without the reminder
python3 python/scripts/assemble_changelog.py --require-empty  # exit 1 if any fragment is still unfolded
```

One flag at a time: `--write` is REFUSED (exit 2) alongside `--check`,
`--dry-run`, or `--require-empty` (tan-cli#1181). `--dry-run --write` used to
perform the whole fold silently, exit 0 — the destructive half won over the
flag documented as "write nothing", from a command line that asked for both.
There is no safe way to guess which half was meant, so it names the flags it
saw and what to run instead.

`--check` never fails merely because fragments are pending — that is why it is
not the gate. Like every mode it refuses (exit 1) an unusable fragment: a
category outside the six, an empty or whitespace-only body, or — since
tan-cli#930 — a body whose top-level lines are not shaped as a Markdown
bullet list, e.g. a fragment whose only line is `not a bullet at all`. That
shape check is narrower than "any valid Markdown list": only a literal `- `
(a dash, then a space) marks a bullet — `*`, `+`, and numbered (`1.`) markers
are refused, as is a column-0 continuation line with no `- ` bullet above it
(CommonMark would still parse that as part of the list; this checker does
not) — and a ` ``` ` fence must close before the fragment ends. Before
tan-cli#930 the check was only a filename lint; it now also refuses content
`splice()` would otherwise insert into `CHANGELOG.md` as bare text under a
bullet-list heading. It still does not, and never will, judge whether a
fragment's sentences are *true* — a wrong count or a false claim reads clean;
that stays a review problem.
`--require-empty` is the gate, and since tan-cli#813 it is a live one:
`.github/workflows/release.yml:828` runs it, so a tag cut with fragments still
sitting here fails the release rather than shipping a CHANGELOG missing them.

This runs **before** the version bump and tag, so `release.yml`'s existing
`## [X.Y.Z]` slice sees a fully-populated section and nothing about the release
contract changes.

## Editing `CHANGELOG.md` directly

Still correct for: fixing a typo in a shipped entry, correcting a released
section, or any edit that is not "a new entry for unreleased work". The
fragment rule exists to stop N PRs racing one insertion point — it is not a
ban on ever touching the file.
