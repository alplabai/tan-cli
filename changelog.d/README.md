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

- `<issue>` — the GitHub issue or PR number this entry belongs to. It only has
  to make the filename unique; it is not parsed for meaning.
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

`python/scripts/assemble_changelog.py` folds every fragment into
`CHANGELOG.md` under the current `Unreleased` header, in canonical section
order, then deletes the fragments:

```sh
python3 python/scripts/assemble_changelog.py          # fold + delete fragments
python3 python/scripts/assemble_changelog.py --check   # report only, change nothing
python3 python/scripts/assemble_changelog.py --dry-run # print the result to stdout
python3 python/scripts/assemble_changelog.py --require-empty  # exit 1 if any fragment is still unfolded
```

`--check` never fails merely because fragments are pending — that is why it is
not the gate. It is not unconditionally exit-0 either, whatever its `--help`
says: like every mode it refuses an unusable fragment (a category outside the
six, or an empty body) and exits 1, which makes it a usable filename lint.
`--require-empty` is the gate, and since tan-cli#813 it is a live one:
`.github/workflows/release.yml:704` runs it, so a tag cut with fragments still
sitting here fails the release rather than shipping a CHANGELOG missing them.

This runs **before** the version bump and tag, so `release.yml`'s existing
`## [X.Y.Z]` slice sees a fully-populated section and nothing about the release
contract changes.

## Editing `CHANGELOG.md` directly

Still correct for: fixing a typo in a shipped entry, correcting a released
section, or any edit that is not "a new entry for unreleased work". The
fragment rule exists to stop N PRs racing one insertion point — it is not a
ban on ever touching the file.
