# SPDX-License-Identifier: Apache-2.0
"""Gate: the frozen-oracle capture store says what it is, and the label stays
true (tan-cli#617).

tan-cli#269 deleted `crates/` and the ~13 parity modules that replayed
`tests/fixtures/oracle_captures/`. Five of the seven `test_*_oracle_parity.
json` files lost their only reader in that change and were left behind, which
is how `test_command_surface_oracle_parity.json` came to hold a byte-for-byte
copy of the completion script tan-cli#614 has since rewritten -- a stored
answer contradicting the shipped one, with nothing in the tree to say which is
authoritative.

## The decision this gate enforces

The store is KEPT, not deleted. It is deliberately unrecapturable (the binary
is gone, `.github/workflows/capture-platform-fixtures.yml` is gone,
`TAN_PARITY_LIVE`/`TAN_PARITY_CAPTURE` are gone), and it is cited as the
authoritative record of oracle behaviour by `docs/ROADMAP.md`,
`docs/ux-polish-sweep-plan.md`, `README.md` and four modules under `tan/`.
Deleting evidence that cannot be regenerated, to fix what is a LABELLING
problem, is the wrong trade. So the fix is a label -- `README.md` at the top
of the directory -- plus this gate, so the label cannot rot into the same
silent-staleness the captures themselves fell into.

Three things are checked, each of which has gone wrong at least once in this
area of the tree:

1. **Every file is named in the README.** A capture added or renamed without
   a row is exactly how the last five became orphans unnoticed.
2. **The live-reader column is MEASURED, not asserted.** The README's claim
   about which captures still feed a test is re-derived from the tree on every
   run. A capture that gains or loses a reader reds here rather than leaving a
   second stale table behind the first.
3. **Each declared supersession is still a real one.** A superseded substring
   must still be IN the capture (a capture edited to agree with shipped
   behaviour is a fabricated oracle answer -- tan-cli#511's failure mode) and
   still ABSENT from the shipped source (otherwise the README's claim has gone
   stale and the note must be revised, not quietly kept).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests import oracle_captures

_CAPTURES = oracle_captures.CAPTURES_DIR
_README = _CAPTURES / "README.md"
_TESTS_ROOT = Path(__file__).resolve().parents[1]
_TAN_ROOT = _TESTS_ROOT.parent / "tan"
_THIS_MODULE = Path(__file__).resolve().relative_to(_TESTS_ROOT.parent).as_posix()

#: `<capture stem>` -> the substrings that capture froze and the shipped
#: source has since moved past, paired with the module that must no longer
#: contain them. Every row is measured in both directions by
#: `test_each_declared_supersession_is_still_real` below.
_SUPERSEDED: dict[str, tuple[str, tuple[str, ...]]] = {
    "test_command_surface_oracle_parity": (
        "commands/completion_cmd.py",
        (
            'case "${COMP_WORDS[1]}"',
            "--ci --help --version",
            "complete -c tan -l version",
        ),
    ),
}


def _walk_strings(payload: object):
    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_strings(item)
    elif isinstance(payload, dict):
        for value in payload.values():
            yield from _walk_strings(value)


#: `CAPTURES_DIR / "<name>.json"` -- a reader that builds its own path.
_PATTERN_CAPTURES_DIR = re.compile(r'CAPTURES_DIR\s*/\s*[\'"]([^\'"]+)[\'"]')
#: A call to `oracle_captures`'s `load` accessor with a literal capture name,
#: qualified (`oracle_captures.load`) or bare (reachable via `from tests.
#: oracle_captures import load`) -- documented in `tests/oracle_captures.py`'s
#: own docstring and, until this pattern existed, a SECOND way into this
#: file's measurement it could not see at all: a module reading a capture
#: through that accessor -- one the README still labelled history-only --
#: read here as having no reader, and the drift check passed vacuously
#: (measured directly against a throwaway repro module: reverting this
#: pattern turns that case green again). `\b` ahead of the accessor name so a
#: `reload`/`preload` call is not mistaken for it -- there is a word boundary
#: before a bare call or right after the `oracle_captures.` qualifier, never
#: inside a longer identifier that merely ends the same way.
#:
#: Spelled with a run-time join rather than as one literal string: written
#: out whole, this pattern's own OWN example would itself be a textbook
#: instance of the shape it matches, and self-match against this file during
#: collection.
_ACCESSOR_NAME = "".join(["l", "o", "a", "d"])
_PATTERN_LOAD_CALL = re.compile(
    r'\b(?:oracle_captures\.)?' + _ACCESSOR_NAME + r'\(\s*[\'"]([^\'"]+)[\'"]'
)


def _measured_live_readers() -> dict[str, set[str]]:
    """Capture filename -> the test modules that read it, derived from the
    tree rather than from the README.

    Two call shapes are matched textually -- `CAPTURES_DIR / "<name>"` and
    `oracle_captures.load("<name>")` -- both documented in
    `tests/oracle_captures.py`. A THIRD shape exists and cannot be matched
    textually at all: `test_each_declared_supersession_is_still_real` below
    reads `_CAPTURES / f"{stem}.json"` for every key of `_SUPERSEDED`, an
    f-string built from a loop variable rather than a literal. That one is
    measured directly off the `_SUPERSEDED` mapping instead of regexed."""
    readers: dict[str, set[str]] = {}

    def record(name: str, module_rel: str) -> None:
        if not name.endswith((".json", ".txt")):
            name = f"{name}.json"
        readers.setdefault(name, set()).add(module_rel)

    for module in sorted(_TESTS_ROOT.rglob("*.py")):
        if module.name == "oracle_captures.py":
            continue
        text = module.read_text(encoding="utf-8")
        module_rel = module.relative_to(_TESTS_ROOT.parent).as_posix()
        for name in _PATTERN_CAPTURES_DIR.findall(text):
            record(name, module_rel)
        for name in _PATTERN_LOAD_CALL.findall(text):
            record(name, module_rel)

    for stem in _SUPERSEDED:
        record(stem, _THIS_MODULE)

    return readers


def test_the_store_carries_a_readme_at_the_top_of_the_directory():
    """The one thing tan-cli#617 asks for outright: a reader who opens this
    directory learns, before anything else, that these bytes are a frozen
    `tan 0.4.1` record and not a claim about what ships."""
    assert _README.is_file(), f"no README.md in {_CAPTURES}"
    text = _README.read_text(encoding="utf-8")
    assert "tan 0.4.1" in text, "the README must name the binary these captures came from"
    assert "Nothing in this directory describes what `tan` does today." in text, (
        "the README's lead sentence is what stops a reader taking a capture for a "
        "statement about shipped behaviour -- it must stay, and stay first"
    )


def test_every_file_in_the_store_is_named_in_the_readme():
    """A capture added, renamed or left behind without a row is how the last
    five orphans went unnoticed through tan-cli#269.

    Matched backtick-wrapped (`` `<name>` ``, the table's own row format,
    `test_the_readme_live_reader_table_matches_the_measured_tree` below reads
    the same way) rather than as a bare substring: an unanchored `p.name not
    in text` would be satisfied by the filename appearing anywhere at all --
    prose, a `PROVENANCE.txt` mention, a coincidental substring of another
    row's own reader path -- none of which is the row this test exists to
    require."""
    text = _README.read_text(encoding="utf-8")
    unlisted = sorted(
        p.name
        for p in _CAPTURES.iterdir()
        if p.name != "README.md" and f"`{p.name}`" not in text
    )
    assert unlisted == [], (
        f"{unlisted} sit in {_CAPTURES} with no row in README.md. Add one saying "
        "whether the file still has a live reader or is history only -- an "
        "unlabelled capture is the tan-cli#617 defect reappearing."
    )


def test_the_readme_live_reader_table_matches_the_measured_tree():
    """The README's live-reader claims are re-derived from the tree here, so
    the table cannot become a second stale record sitting on top of the first."""
    measured = _measured_live_readers()
    text = _README.read_text(encoding="utf-8")
    problems = []
    for name in sorted(p.name for p in _CAPTURES.iterdir() if p.name != "README.md"):
        row = next((line for line in text.splitlines() if line.startswith(f"| `{name}`")), None)
        if row is None:
            continue  # covered by the previous test, with a better message
        # The reader CELL specifically, not the row as a whole: `"none" in
        # row` is satisfied by a path or note containing "none" anywhere --
        # including in the FILE column, or a future reader path that happens
        # to contain the substring -- and would silently flip that row to
        # "history only" without the table actually saying so.
        cells = [c.strip() for c in row.split("|")]
        reader_cell = cells[2] if len(cells) > 2 else ""
        claims_none = reader_cell.lower().startswith("none")
        if measured.get(name) and claims_none:
            problems.append(f"{name}: README says history-only, but {sorted(measured[name])} read it")
        if not measured.get(name) and not claims_none:
            problems.append(f"{name}: README claims a live reader, but nothing in tests/ reads it")
    assert problems == [], (
        "README.md's live-reader table has drifted from the tree:\n  " + "\n  ".join(problems)
    )


@pytest.mark.parametrize("stem", sorted(_SUPERSEDED))
def test_each_declared_supersession_is_still_real(stem):
    """Both halves, because either one going false is a defect with a
    different fix.

    Substring still in the capture: if it is not, someone hand-edited a frozen
    oracle answer to agree with shipped behaviour -- the fabrication failure
    mode of tan-cli#511, unrecoverable because there is no binary left to
    re-capture from.

    Substring no longer in the shipped source: if it is back, the README's
    supersession note has gone stale and must be REVISED, not left standing as
    a claim the tree contradicts. That is the whole shape tan-cli#617 is
    about, just pointing the other way."""
    module_rel, needles = _SUPERSEDED[stem]
    store = json.loads((_CAPTURES / f"{stem}.json").read_text(encoding="utf-8"))
    recorded = list(_walk_strings(store))
    shipped = (_TAN_ROOT / module_rel).read_text(encoding="utf-8")

    for needle in needles:
        assert any(needle in text for text in recorded), (
            f"{needle!r} is no longer in {stem}.json. A frozen oracle capture "
            "must never be edited to match what ships -- restore it from git "
            "history (tan-cli#511, tan-cli#617)."
        )
        assert needle not in shipped, (
            f"{needle!r} is back in tan/{module_rel}, so README.md's "
            f"supersession note for {stem}.json is now false. Revise the note "
            "and this table together -- do not leave a claim standing that the "
            "shipped source contradicts."
        )
