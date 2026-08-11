# SPDX-License-Identifier: Apache-2.0
"""`tan.core.doctor_libraries` + `doctor_cmd.libraries_check` -- the ADR-0018
curated-library check ported out of alp-sdk's `scripts/alp_cli/doctor.py`.

Both halves live here rather than split across `tests/commands/
test_doctor_command.py` (3659 lines already) because `libraries_check` is a
pure outcome->`Check` mapping with no CLI dispatch in it; the resolution it
maps is the thing worth testing and it is in this module's subject.

## The case this file exists for

alp-sdk's original read the RAW `libraries:` list out of the YAML document and
passed each entry to `load_manifest()`. board.yaml's entries are
`{name, cores?}` OBJECTS as well as bare strings, and on the object shape the
original's SUCCESS path -- `resolve_selection` having already passed, using
the loader's normalised names -- then re-read the raw list to build its labels
and raised straight out of `_all_checks()`:

    $ cd examples/peripheral-io/fmt-formatting
    $ PYTHONPATH=<alp-sdk>/scripts python3 -m alp_cli doctor
    ...
      File ".../alp_cli/doctor.py", line 734, in _check_libraries
        man = load_manifest(name, metadata_root)
    alp_orchestrate.models.OrchestratorError: unknown library
    `{'name': 'fmt', 'cores': ['m55_hp']}` in `libraries:` -- no manifest at
    metadata/libraries/{'name': 'fmt', 'cores': ['m55_hp']}.yaml.

Measured, not reasoned: exit 1, whole command down, in a shipped example
directory. **All 42 in-tree alp-sdk examples that select libraries use that
object shape** -- there is no shipped project the original works on at all.
Its three tests (alp-sdk `tests/scripts/test_library_layer.py`) all wrote a
bare-string `libraries:` fixture, which is the only shape that survived.

So `test_the_object_entry_shape_resolves` below is not a nicety: it is the
regression case for a defect that made the check being ported unusable.
`test_one_library_declared_on_both_channels_is_reported_once` covers the
other reason the port takes its names from `scoped_names(project)` -- the
loader's own normalised, deduplicated, both-channel view -- rather than from
the raw document.

Not covered, because it is unreachable: `metadata/library-aliases-v1.json`
maps legacy underscore tokens (`cmsis_dsp` -> `cmsis-dsp`) and the loader
applies that table, but `board.schema.json` pins every `libraries:` name to
`^[a-z][a-z0-9-]*$`, so no underscore spelling survives validation to reach
it. Measured while writing this file, after an earlier draft asserted the
alias path as a second fixed defect and failed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tan.commands.doctor_cmd import libraries_check
from tan.commands.sdk_cmd import NO_SDK_NEXT_STEPS
from tan.core.doctor_libraries import LibraryReport, inspect_selection
from tests.conftest import sdk_root

SDK = sdk_root()

_SKIP_REASON = (
    "set ALP_SDK_ROOT to an alp-sdk checkout so tan.planner can bind a root "
    "and read metadata/libraries/* (same requirement as the parity suite)"
)

#: A minimal, self-authored project -- not a path into an alp-sdk example, so
#: this suite's coverage does not depend on a file inside a second repo that
#: moves independently (the convention `test_project_loader.py` records).
#: `E1M-AEN801` + `e1m-evk` + one Cortex-M55 core is the same shape those
#: fixtures use; `fmt` is header-only C++ with no `requires:` narrowing it off
#: an M-class core, so it resolves on this target.
_BOARD = """\
som:
  sku: E1M-AEN801
preset: e1m-evk
libraries:
{selection}
cores:
  m55_hp:
    app: ./src
"""


def _board(tmp_path: Path, selection: str) -> Path:
    path = tmp_path / "board.yaml"
    path.write_text(_BOARD.format(selection=selection), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Absence: the two cases that must produce no row at all.
# ---------------------------------------------------------------------------


def test_no_board_yaml_produces_no_report(tmp_path):
    assert inspect_selection(None, tmp_path) is None
    assert inspect_selection(tmp_path / "board.yaml", tmp_path) is None


def test_a_project_selecting_nothing_produces_no_report(tmp_path):
    """A "0 selected" row in front of every customer who does not use the
    feature is the noise this `None` exists to avoid. Cheap, too: this path
    never reaches the planner at all, which is why the peek at the raw
    document survives the port."""
    path = tmp_path / "board.yaml"
    path.write_text("som:\n  sku: E1M-AEN801\npreset: e1m-evk\n", encoding="utf-8")
    assert inspect_selection(path, tmp_path) is None
    assert libraries_check(None) is None


def test_an_empty_libraries_list_produces_no_report(tmp_path):
    path = _board(tmp_path, "  []")
    assert inspect_selection(path, tmp_path) is None


# ---------------------------------------------------------------------------
# The two shapes, read without any SDK -- `unverifiable`, but NAMED.
# ---------------------------------------------------------------------------


def test_both_entry_shapes_are_read_from_the_raw_document(tmp_path):
    """The raw peek must see the object shape as well as the string shape.
    Reading only `isinstance(entry, str)` is the same blindness in the other
    direction as the defect this port fixes: a project selecting only
    core-scoped libraries would look like a project selecting none, and the
    check would go silent on exactly the shape every shipped example uses."""
    path = _board(tmp_path, "  - fmt\n  - name: etl\n    cores: [m55_hp]")
    report = inspect_selection(path, None)
    assert report is not None
    assert report.outcome == "unverifiable"
    assert report.names == ("fmt", "etl")


def test_selected_but_unverifiable_still_reports_a_warn(tmp_path):
    """No SDK checkout resolved. The customer HAS selected libraries, so
    silence would be wrong -- but nothing was checked, so `pass` would be a
    lie. `warn`, naming the count, and pointing at how to fix the report.

    The remedy is `sdk_cmd.NO_SDK_NEXT_STEPS` verbatim, not prose of its own:
    an earlier draft wrote "run `tan sdk switch <path>`" and
    `test_sdk_onboarding_dead_end.py` reded it -- `switch` is a subcommand
    this build REFUSES, so that fix line was a dead end. Asserting on the
    shared constant is what keeps this row correct if the onboarding route
    changes again."""
    path = _board(tmp_path, "  - name: fmt\n    cores: [m55_hp]")
    check = libraries_check(inspect_selection(path, None))
    assert check is not None
    assert check.status == "warn"
    assert check.scope == "project"
    assert "1 selected" in check.detail
    assert "none verified" in check.detail
    assert check.fix is not None and check.fix.startswith(NO_SDK_NEXT_STEPS)


def test_an_unparseable_board_yaml_warns_rather_than_raising(tmp_path):
    """`_collect`'s stated invariant is that no probe may raise. `boardYaml`
    owns the verdict on the file itself and runs immediately above this, so
    the wording here stays scoped to why the LIBRARY line could not be
    produced."""
    path = tmp_path / "board.yaml"
    path.write_text("libraries: [fmt\n  broken: ][\n", encoding="utf-8")
    report = inspect_selection(path, tmp_path)
    assert report is not None
    assert report.outcome == "unreadable"
    check = libraries_check(report)
    assert check is not None and check.status == "warn" and check.scope == "project"


def test_an_unknown_outcome_is_refused_at_construction():
    """`outcome` drives a status mapping, so a typo'd value must not fall
    through `libraries_check`'s final `else` and be rendered as INCOMPATIBLE.
    Same posture as `Check.__post_init__`'s scope guard."""
    with pytest.raises(ValueError, match="unknown library-report outcome"):
        LibraryReport("Ok", ("fmt",))


# ---------------------------------------------------------------------------
# The real resolution. Needs a bound alp-sdk checkout for metadata/libraries/*.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(SDK is None, reason=_SKIP_REASON)
def test_the_object_entry_shape_resolves(tmp_path):
    """THE regression case -- see the module docstring. `{name, cores}` is the
    shape all 42 shipped alp-sdk examples use and the shape the original
    crashed on; here it must resolve, be labelled with its real tier and
    licence out of `metadata/libraries/fmt.yaml`, and pass."""
    path = _board(tmp_path, "  - name: fmt\n    cores: [m55_hp]")
    report = inspect_selection(path, SDK)
    assert report is not None
    assert report.outcome == "ok", report
    assert report.names == ("fmt",)
    assert len(report.labels) == 1
    assert report.labels[0].startswith("fmt (tier ")
    check = libraries_check(report)
    assert check is not None
    assert check.status == "pass"
    assert check.scope == "project"
    assert "1 selected, all compatible" in check.detail
    assert "fmt (tier " in check.detail


@pytest.mark.skipif(SDK is None, reason=_SKIP_REASON)
def test_the_bare_string_entry_shape_resolves_too(tmp_path):
    """The project-wide channel. Both channels are the same ADR-0018
    selection and `scoped_names` is what folds them into one list."""
    path = _board(tmp_path, "  - fmt")
    report = inspect_selection(path, SDK)
    assert report is not None
    assert report.outcome == "ok", report
    assert report.names == ("fmt",)


@pytest.mark.skipif(SDK is None, reason=_SKIP_REASON)
def test_one_library_declared_on_both_channels_is_reported_once(tmp_path):
    """The other half of why names come from `scoped_names(project)` rather
    than the raw document, and the case that is invisible if only the entry
    SHAPES are covered.

    `[fmt, {name: fmt, cores: [m55_hp]}]` is schema-valid -- `uniqueItems`
    compares whole entries, and these two differ -- but it is ONE selection
    declared on both channels. `scoped_names` dedupes it; a raw read reports
    `2 selected: fmt (tier B, MIT), fmt (tier B, MIT)`."""
    path = _board(tmp_path, "  - fmt\n  - name: fmt\n    cores: [m55_hp]")
    report = inspect_selection(path, SDK)
    assert report is not None
    assert report.outcome == "ok", report
    assert report.names == ("fmt",)
    check = libraries_check(report)
    assert check is not None and "1 selected" in check.detail


@pytest.mark.skipif(SDK is None, reason=_SKIP_REASON)
def test_an_unknown_library_warns_and_names_the_offender(tmp_path):
    """WARN, never FAIL: `tan build` refuses this outright, so the hard gate
    already exists and is the emit. A `fail` here would additionally cost
    exit 4 and make an unwireable selection read identically to a missing
    toolchain."""
    path = _board(tmp_path, "  - name: no-such-library\n    cores: [m55_hp]")
    report = inspect_selection(path, SDK)
    assert report is not None
    assert report.outcome == "unresolved", report
    check = libraries_check(report)
    assert check is not None
    assert check.status == "warn"
    assert check.scope == "project"
    assert "no-such-library" in check.detail
    assert "INCOMPATIBLE" in check.detail
    # The emitter's OWN message, verbatim -- the warning a customer reads here
    # is word-for-word the error `tan build` will print, not a paraphrase.
    assert "no manifest at metadata/libraries/no-such-library.yaml" in check.detail


@pytest.mark.skipif(SDK is None, reason=_SKIP_REASON)
def test_the_libraries_row_reaches_the_collected_report(tmp_path):
    """End to end through `_collect`, which is what puts the row on the wire
    (`data.checks[]` is `[c.as_dict() for c in _collect(...)]`) -- a check
    function nothing calls reports nothing."""
    from tan.commands import doctor_cmd

    path = _board(tmp_path, "  - name: fmt\n    cores: [m55_hp]")
    checks = doctor_cmd._collect(
        str(SDK), board_yaml=str(path), workspace_root=str(tmp_path)
    )
    rows = [c.as_dict() for c in checks if c.name == "libraries"]
    assert len(rows) == 1, [c.name for c in checks]
    assert rows[0]["scope"] == "project"
    assert rows[0]["status"] == "pass"


@pytest.mark.skipif(SDK is None, reason=_SKIP_REASON)
def test_no_libraries_row_when_the_project_selects_none(tmp_path):
    """The absence, proved on the wire rather than only on the helper."""
    from tan.commands import doctor_cmd

    path = tmp_path / "board.yaml"
    path.write_text("som:\n  sku: E1M-AEN801\npreset: e1m-evk\n", encoding="utf-8")
    checks = doctor_cmd._collect(
        str(SDK), board_yaml=str(path), workspace_root=str(tmp_path)
    )
    assert [c.name for c in checks if c.name == "libraries"] == []
