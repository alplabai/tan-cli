# SPDX-License-Identifier: Apache-2.0
"""tan-cli#467: `contract/issue-codes.json` registers each code as a
SEPARATE `{...}` element of the `issueCodes` array, never as a repeated key
inside one JSON object -- so a plain `json.load` never sees a "duplicate
key" in the syntactic sense, even when the same `code` string is registered
twice. That is exactly what shipped: `bootstrap.adopted-venv-unusable` had
two entries (`severity` disagreeing, "warning" vs "error") and
`bootstrap.venv-recreated` had two more, and neither
`test_every_issue_code_is_registered.py` (`{e["code"] for e in ...}`, a
`set` comprehension that silently dedups) nor
`test_issue_code_registry_shape.py` (a plain list, checked only for
spelling) can see a repeat -- both read the array only AFTER it is fully
parsed, with the duplicate already sitting there twice. Anything downstream
that keys a lookup off `code` (this repo's own release step re-publishes
`issueCodes` verbatim into `envelope-contract.json`; a consumer that then
maps `code -> entry` inherits ordinary last-write-wins dict semantics) ends
up preferring whichever entry happens to sit later in the file -- reordering
the two duplicate entries would have flipped the published severity with no
code diff at all.

This gate closes that blind spot two ways, both at load time rather than
after the fact:

  1. [`load_rejecting_duplicate_keys`] parses with an `object_pairs_hook`
     ([`_reject_duplicate_object_keys`]) that raises the instant it sees the
     same key spelled twice inside one `{...}` -- the classic duplicate-key
     shape, which plain `json.load` resolves last-wins silently.
  2. [`duplicate_record_values`] is the tan-cli#467 shape itself: a second
     pass over the already-parsed `issueCodes` array (no `object_pairs_hook`
     can see this one -- each duplicate is a separate, well-formed object,
     not a repeated key) that collects EVERY `code` appearing more than
     once, so one failure names all of them rather than stopping at the
     first.

[`load_registry_rejecting_duplicates`] runs both and is what the tests below
exercise; it is also the intended entry point for a future registry that
wants the same coverage (`record_key` is a parameter, not hardcoded to
`"code"`).
"""

from __future__ import annotations

import json
import pathlib

import pytest

#: `contract/` lives at the repo root, one level above `python/` -- the same
#: resolution every sibling registry gate in this directory uses.
REGISTRY = pathlib.Path(__file__).resolve().parents[3] / "contract" / "issue-codes.json"


def _reject_duplicate_object_keys(pairs: list[tuple[str, object]]) -> dict:
    """`object_pairs_hook`: raise the moment one `{...}` object spells the
    same key twice (`dict(pairs)` alone would resolve it last-wins,
    silently)."""
    keys = [k for k, _ in pairs]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise ValueError(f"duplicate key(s) {dupes} inside one JSON object")
    return dict(pairs)


def load_rejecting_duplicate_keys(path: pathlib.Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f, object_pairs_hook=_reject_duplicate_object_keys)


def duplicate_record_values(records: list[dict], *, record_key: str) -> list[object]:
    """Every `record_key` value that appears more than once across
    `records`, in first-seen order. This is the tan-cli#467 shape itself --
    two separate, well-formed objects sharing one `code` -- which no
    `object_pairs_hook` can see, since neither object individually has a
    repeated key."""
    seen: set[object] = set()
    dupes: list[object] = []
    for record in records:
        if record_key not in record:
            continue
        value = record[record_key]
        if value in seen and value not in dupes:
            dupes.append(value)
        seen.add(value)
    return dupes


def load_registry_rejecting_duplicates(path: pathlib.Path, *, record_key: str) -> dict:
    """Parse the JSON registry at `path`, raising `ValueError` naming every
    offending value if either duplicate shape above is present."""
    data = load_rejecting_duplicate_keys(path)
    dupes = duplicate_record_values(data["issueCodes"], record_key=record_key)
    if dupes:
        raise ValueError(
            f"duplicate {record_key!r} value(s) registered more than once in {path}: {dupes}"
        )
    return data


def test_issue_codes_registry_has_no_duplicate_codes():
    """Real regression coverage: the committed registry must parse clean."""
    data = load_registry_rejecting_duplicates(REGISTRY, record_key="code")
    codes = data["issueCodes"]
    # Non-vacuity (tan-cli#275's standing lesson): an emptied-out or
    # reshaped registry would otherwise make this pass by checking nothing.
    assert len(codes) > 100, (
        f"only {len(codes)} codes found in {REGISTRY} -- the registry shape "
        f"changed and this gate is now checking almost nothing"
    )


def test_the_gate_rejects_the_tan_cli_467_shape(tmp_path: pathlib.Path):
    """Non-vacuity, the direction that matters: prove the gate can actually
    fail, reproducing the exact pre-fix pair tan-cli#467 shipped -- TWO
    separate duplicated codes, `bootstrap.adopted-venv-unusable` (disagreeing
    on `severity`) and `bootstrap.venv-recreated` -- and that both are named
    in the one failure, not just the first encountered."""
    dupe = tmp_path / "dupe-issue-codes.json"
    dupe.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "issueCodes": [
                    {"code": "bootstrap.adopted-venv-unusable", "status": "reserved", "severity": "warning"},
                    {"code": "bootstrap.venv-recreated", "status": "reserved", "severity": "warning"},
                    {"code": "bootstrap.adopted-venv-unusable", "status": "reserved", "severity": "error"},
                    {"code": "bootstrap.venv-recreated", "status": "reserved", "severity": "warning"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as excinfo:
        load_registry_rejecting_duplicates(dupe, record_key="code")
    message = str(excinfo.value)
    assert "bootstrap.adopted-venv-unusable" in message
    assert "bootstrap.venv-recreated" in message


def test_the_hook_rejects_a_literal_duplicate_json_key(tmp_path: pathlib.Path):
    """The narrower classic shape: the same key spelled twice inside one
    `{...}` object, which plain `json.load` resolves last-wins silently."""
    dupe = tmp_path / "dupe-key.json"
    dupe.write_text('{"issueCodes": [{"code": "a.b", "severity": "warning", "severity": "error"}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="severity"):
        load_rejecting_duplicate_keys(dupe)


def test_the_gate_accepts_a_clean_registry(tmp_path: pathlib.Path):
    """Positive control: the gate must not reject a registry with no repeats
    at all, so the assertions above are not merely rejecting everything."""
    clean = tmp_path / "clean-issue-codes.json"
    clean.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "issueCodes": [
                    {"code": "bootstrap.adopted-venv-unusable", "status": "reserved", "severity": "warning"},
                    {"code": "bootstrap.venv-recreated", "status": "reserved", "severity": "warning"},
                ],
            }
        ),
        encoding="utf-8",
    )
    data = load_registry_rejecting_duplicates(clean, record_key="code")
    assert len(data["issueCodes"]) == 2
