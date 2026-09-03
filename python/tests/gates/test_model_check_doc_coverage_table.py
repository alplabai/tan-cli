# SPDX-License-Identifier: Apache-2.0
"""`docs/model-check-static-screen.md`'s `(npuCoverage, basis)` table must
enumerate exactly the pairs `tan.model.analyze.LEGITIMATE_COVERAGE_BY_BASIS`
declares -- no more, no fewer.

WHAT THIS GATE ENFORCES, EXACTLY: the SET OF PAIRS in that one table, in both
directions. A pair reachable in code but absent from the table fails, naming
the pair; a pair documented in the table but no longer legitimate in code also
fails, naming it, so the table cannot accumulate dead rows. That is the whole
claim.

WHAT IT DOES NOT ENFORCE, DELIBERATELY: every `Means` cell, the surrounding
prose, the `corroborated`/`uncorroborated` qualifiers on the two `bench` rows,
the "Five distinct values ... in seven rows" sentence under it, and the
`### Where fits may and may not appear` section. Those explain MEANING -- that
`undetermined` at `basis: "bench"` says "SRAM/latency are measured, placement
is not" while the same word at `basis: "static-screen"` says "nothing was
screened at all" -- and meaning is not mechanically derivable from a dict of
frozensets. A gate that pretended otherwise would be pinning prose by keyword
match, which is the duplication tan-cli#1135 exists to avoid, not remove.

WHY IT EXISTS. tan-cli#1115 changed WHEN coverage is carried onto a bench
point, and this page went false at five lines (`:73`, `:76`, `:100`, `:189`,
`:257`) -- including a claim that both `fits` surfaces "derive it from one
function (`tan.model.perf.coverage_from_placement`)", which the bench path no
longer does. PR #1130 corrected it by hand and filed tan-cli#1135, because
nothing stopped it drifting again. The blast radius is not cosmetic: the page
tells consumers to match exhaustively on `(npuCoverage, basis)` and calls the
same word "Not a placement claim" at one basis and "A real placement" at
another, so a stale row actively misleads about what a number means.

WHY IT CAN BE HONEST NOW. Before tan-cli#1135 there was no source of truth to
compare against -- the legitimate combinations were implied by control flow in
`_perf_point_report` and its neighbours and enumerated nowhere, so any gate
would have had to hand-copy the pairs and become a second thing to drift.
`LEGITIMATE_COVERAGE_BY_BASIS` is that source of truth, and it is not a
test-only mirror: `BackendReport.__post_init__` validates every construction
against it, so production refuses an illegitimate pair before this gate ever
reads a byte of Markdown.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

from tan.model.analyze import LEGITIMATE_COVERAGE_BY_BASIS

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC = REPO_ROOT / "docs" / "model-check-static-screen.md"

#: The table's own header row, verbatim. Used to LOCATE the table rather than
#: to validate it: the document carries a second three-column table (the
#: per-operator `status` vocabulary a dozen lines above) and a keyword scan
#: would happily read that one instead. A missing header is a hard failure,
#: never a vacuous pass -- deleting the table must turn this gate red.
_HEADER = "| `npuCoverage` | At which `basis` | Means |"

#: A backtick-quoted token, e.g. `` `partial` ``. Column 1 legitimately holds
#: several (`` `partial` / `fits` / `cpu-only` ``), one row per BASIS rather
#: than one per pair; column 2 holds the basis first and may qualify it in
#: prose after a comma (`` `bench`, corroborated ``), which is meaning and is
#: not read here.
_TICKED = re.compile(r"`([^`]+)`")


@functools.cache
def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


@functools.cache
def _table_rows() -> tuple[str, ...]:
    """The table's body rows -- the lines between its `| --- |` separator and
    the first line that is not a table row."""
    lines = _doc().splitlines()
    assert _HEADER in lines, (
        f"{DOC.name}: the `(npuCoverage, basis)` table header is gone.\n"
        f"Expected, verbatim:\n    {_HEADER}\n"
        f"This gate cannot check a table it cannot find, and passing anyway "
        f"would make it vacuous -- restore the table or delete this gate "
        f"deliberately.")
    start = lines.index(_HEADER) + 1
    assert lines[start].startswith("| ---"), (
        f"{DOC.name}:{start + 1}: expected the Markdown separator row "
        f"directly under the table header, found {lines[start]!r}")
    rows = []
    for line in lines[start + 1:]:
        if not line.startswith("|"):
            break
        rows.append(line)
    assert rows, f"{DOC.name}: the `(npuCoverage, basis)` table has no rows"
    return tuple(rows)


@functools.cache
def _documented_pairs() -> frozenset[tuple[str, str]]:
    """Every `(npuCoverage, basis)` pair the table states.

    Column 1 contributes each of its backticked coverage words; column 2
    contributes its FIRST backticked token as the basis, so a row qualified in
    prose (`` `bench`, corroborated ``) is read as `bench` and its qualifier
    is left to the reader, where it belongs."""
    pairs: set[tuple[str, str]] = set()
    for row in _table_rows():
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        assert len(cells) == 3, (
            f"{DOC.name}: expected 3 columns in {row!r}, found {len(cells)}")
        coverages = _TICKED.findall(cells[0])
        bases = _TICKED.findall(cells[1])
        assert coverages, f"{DOC.name}: no backticked npuCoverage in {row!r}"
        assert bases, f"{DOC.name}: no backticked basis in {row!r}"
        pairs.update((c, bases[0]) for c in coverages)
    return frozenset(pairs)


def _legitimate_pairs() -> frozenset[tuple[str, str]]:
    return frozenset((coverage, basis)
                     for basis, coverages in LEGITIMATE_COVERAGE_BY_BASIS.items()
                     for coverage in coverages)


def _fmt(pairs) -> str:
    return ", ".join(f"({c!r} @ {b!r})" for c, b in sorted(pairs)) or "(none)"


def test_the_doc_documents_every_legitimate_coverage_basis_pair():
    """Adding a reachable pair in code without documenting it fails here."""
    missing = _legitimate_pairs() - _documented_pairs()
    assert not missing, (
        f"{DOC.name}'s `(npuCoverage, basis)` table is missing "
        f"{len(missing)} pair(s) that `LEGITIMATE_COVERAGE_BY_BASIS` declares "
        f"legitimate: {_fmt(missing)}.\n"
        f"A pair a report can really carry must be documented before it can "
        f"reach a customer matching exhaustively on the pair -- add a row (or "
        f"extend an existing basis's row) to {DOC.relative_to(REPO_ROOT)}, "
        f"saying what the word MEANS at that basis.")


def test_the_doc_documents_no_pair_that_is_no_longer_legitimate():
    """Removing a pair from the code without removing its row fails here, so
    the table cannot accumulate dead rows."""
    stale = _documented_pairs() - _legitimate_pairs()
    assert not stale, (
        f"{DOC.name}'s `(npuCoverage, basis)` table documents "
        f"{len(stale)} pair(s) `LEGITIMATE_COVERAGE_BY_BASIS` no longer "
        f"declares legitimate: {_fmt(stale)}.\n"
        f"`BackendReport.__post_init__` now raises on these, so no report can "
        f"carry one -- remove the row from "
        f"{DOC.relative_to(REPO_ROOT)} rather than leaving a reader a "
        f"combination tan will never emit.")


def test_the_table_locator_reads_the_coverage_table_and_not_its_neighbour():
    """Positive control on `_table_rows`' locator. The document carries a
    second three-column table (the per-operator `status` vocabulary), and a
    gate that silently read that one would pass for the wrong reason
    forever."""
    rows = _table_rows()
    assert all("`static-screen`" in r or "`compiled`" in r or "`bench`" in r
               for r in rows), (
        f"{DOC.name}: `_table_rows` returned rows with no basis in them -- "
        f"the locator has drifted onto a different table: {rows!r}")
    neighbour = next(line for line in _doc().splitlines()
                     if line.startswith("| `npu-eligible` |"))
    assert neighbour not in rows, (
        f"{DOC.name}: `_table_rows` picked up the operator-`status` table -- "
        f"its row {neighbour!r} is in the result")
