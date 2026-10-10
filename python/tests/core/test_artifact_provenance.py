# SPDX-License-Identifier: Apache-2.0
"""`tan.core.artifact_provenance` (tan-cli#1066): alp-sdk's
`metadata/bootstrap.json` `artifactProvenance` block, normalised for
`missingPrerequisites[]`.

Two things are being pinned here, and they are different in kind.

1. The HAPPY path, against the vendored real producer output
   (`contract/fixtures/bootstrap/manifest.json`, which tracks `parity.yml`'s
   `PINNED_SDK_TAG`) -- never a hand-typed copy of alp-sdk's values, for the
   reason `test_bootstrap_command.REAL_MANIFEST` already states: a manifest
   fact re-spelled in a test is a fact with two owners.

2. The MALFORMED matrix. `doctor`'s whole job is to report on a broken
   environment, so a bad `bootstrap.json` must not take the diagnosis down
   with it -- and this block is advisory display metadata, the least
   load-bearing thing in the file. Every shape below therefore DEGRADES to the
   same `null` an absent key yields, per field, rather than raising the way
   the sibling curated-error register does (`planner.template.
   _require_mapping_doc` / `_require_field`, tan-cli#1073/#1082, which guard a
   document the command cannot proceed without). That choice is what these
   tests exist to hold; see the module's own docstring for the argument.
"""
import json
from pathlib import Path

from tan.core import artifact_provenance
from tan.core.artifact_provenance import ArtifactProvenance

#: The real producer output, read -- never re-typed. Same file the bootstrap
#: suite's `REAL_MANIFEST` reads, and it tracks `parity.yml`'s `PINNED_SDK_TAG`.
REAL_MANIFEST = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "contract" / "fixtures" / "bootstrap" / "manifest.json"
    ).read_text(encoding="utf-8")
)


def test_the_pinned_manifests_block_parses_to_the_values_alp_sdk_publishes():
    """alp-sdk#1574's block, through the real reader, compared against the same
    file's own bytes -- so this cannot pass by agreeing with a stale copy."""
    raw = REAL_MANIFEST["artifactProvenance"]
    table = artifact_provenance.parse_table(raw)

    assert set(table) == set(raw), "every declared tool must survive the parse"
    for tool, doc in raw.items():
        entry = table[tool]
        assert entry.tier == doc["tier"]
        assert entry.licence == doc["licence"]
        # THE rename: alp-sdk's `source` is the wire's `sourceUrl`.
        assert entry.source_url == doc["source"]
        assert entry.size_bytes == doc["sizeBytes"]

    # The keys are the prerequisite tool vocabulary, which is what makes the
    # join onto `missingPrerequisites[].tool` a join on an existing identity.
    assert "cmake" in table and "ninja" in table


def test_the_wire_form_is_four_keys_and_renames_source_to_source_url():
    entry = ArtifactProvenance(
        tier="A", licence="BSD-3-Clause", source_url="https://cmake.org/", size_bytes=12
    )
    assert entry.as_dict() == {
        "tier": "A",
        "licence": "BSD-3-Clause",
        "sourceUrl": "https://cmake.org/",
        "sizeBytes": 12,
    }
    # `source` is alp-sdk's spelling and must NOT appear on the wire; `licence`
    # is alp-sdk's spelling and MUST (see the module docstring for why the two
    # decisions differ).
    assert "source" not in entry.as_dict()
    assert "license" not in entry.as_dict()


def test_absence_is_null_on_every_field_never_an_omitted_key():
    """An SDK predating the block, or a tool with no entry, reports the same
    four `null`s -- present, so a consumer renders "not reported" rather than
    feature-detecting a key."""
    assert artifact_provenance.UNKNOWN.as_dict() == {
        "tier": None,
        "licence": None,
        "sourceUrl": None,
        "sizeBytes": None,
    }
    # No table at all (an SDK with no `artifactProvenance`) and a table with no
    # entry for this tool must be indistinguishable downstream.
    assert artifact_provenance.for_tool(None, "cmake") is artifact_provenance.UNKNOWN
    assert artifact_provenance.for_tool({}, "cmake") is artifact_provenance.UNKNOWN
    table = artifact_provenance.parse_table(REAL_MANIFEST["artifactProvenance"])
    # Real, and permanent: alp-sdk v0.16.0's block covers HOST prerequisites
    # only, so `west`/`zephyrSdk`/`setools`/`jlink` and the Debian package name
    # `python3-venv` have no entry. That gap is alp-sdk#1574's, not tan's.
    for absent in ("west", "zephyrSdk", "setools", "jlink", "python3-venv"):
        assert artifact_provenance.for_tool(table, absent) is artifact_provenance.UNKNOWN


def test_a_missing_or_absent_block_is_an_empty_table_not_an_error():
    assert artifact_provenance.parse_table(None) == {}
    assert artifact_provenance.parse_table({}) == {}


def test_a_block_that_is_not_a_mapping_degrades_instead_of_raising():
    """`doctor` must not crash on a bad `bootstrap.json` -- the file it reads to
    diagnose a broken host is itself part of what can be broken."""
    for malformed in ([], "cmake", 7, True, [{"tool": "cmake"}]):
        assert artifact_provenance.parse_table(malformed) == {}, malformed


def test_an_entry_that_is_not_a_mapping_degrades_to_unknown():
    table = artifact_provenance.parse_table(
        {"cmake": "https://cmake.org/", "ninja": ["A"], "git": None, "xz": 3}
    )
    assert set(table) == {"cmake", "ninja", "git", "xz"}
    for tool in table:
        assert table[tool] == artifact_provenance.UNKNOWN, tool
        assert table[tool].as_dict()["licence"] is None


def test_one_malformed_entry_does_not_cost_the_others_their_provenance():
    """Per-ENTRY degradation, not per-block: an upstream typo in one row must
    not blank the seven rows that are fine."""
    table = artifact_provenance.parse_table(
        {
            "cmake": {"tier": "A", "source": "https://cmake.org/", "licence": "BSD-3-Clause"},
            "ninja": "not-an-object",
        }
    )
    assert table["cmake"].licence == "BSD-3-Clause"
    assert table["ninja"] == artifact_provenance.UNKNOWN


def test_a_missing_field_is_null_and_the_rest_of_the_entry_survives():
    """Per-FIELD degradation too. `xz` and `7zip` really do declare no licence
    upstream, so this is the normal shape, not only the malformed one."""
    table = artifact_provenance.parse_table(
        {"xz": {"tier": "A", "source": "https://tukaani.org/xz/"}}
    )
    assert table["xz"].as_dict() == {
        "tier": "A",
        "licence": None,
        "sourceUrl": "https://tukaani.org/xz/",
        "sizeBytes": None,
    }


def test_a_non_string_field_is_null_and_is_never_coerced():
    """No `str()` anywhere: rendering `{'spdx': 'MIT'}` as a licence, or `1` as
    a tier, would be tan INVENTING a licensing claim -- the one thing the
    `null` spelling exists to prevent."""
    table = artifact_provenance.parse_table(
        {
            "cmake": {
                "tier": 1,
                "licence": {"spdx": "MIT"},
                "source": ["https://cmake.org/"],
                "sizeBytes": "12",
            }
        }
    )
    assert table["cmake"] == artifact_provenance.UNKNOWN
    assert table["cmake"].as_dict() == {
        "tier": None, "licence": None, "sourceUrl": None, "sizeBytes": None,
    }


def test_size_bytes_takes_an_int_and_refuses_bool_float_and_negative():
    """`True == 1` in Python -- the same trap `parse_bootstrap_manifest` and
    `_load_manifest` already exclude `bool` for on `schemaVersion`. A float is
    refused rather than truncated: alp-sdk emits `null` or an integer, and
    truncating would claim a precision the manifest never did. A NEGATIVE int
    is refused for the same reason (tan-cli#1066 review nit): `-4096` is not a
    download size, and "-4 KB" on a consent screen is the same nonsense a
    truncated float would be. `0` survives -- silly but meaningful, and `null`
    already spells "not reported"."""
    def size(value):
        return artifact_provenance.parse_table({"x": {"sizeBytes": value}})["x"].size_bytes

    assert size(1024) == 1024
    assert size(0) == 0
    assert size(None) is None
    assert size(True) is None
    assert size(False) is None
    assert size(1024.0) is None
    assert size("1024") is None
    assert size(-1) is None
    assert size(-4096) is None


def test_a_non_string_key_is_dropped_rather_than_widening_the_table():
    """`missingPrerequisites[].tool` is a string, so a key that could never
    match one carries no information. (JSON cannot produce this; a caller
    handing `parse_table` an already-built dict can.)"""
    table = artifact_provenance.parse_table({7: {"tier": "A"}, "cmake": {"tier": "A"}})
    assert set(table) == {"cmake"}


# --------------------------------------------------------------------------
# tan-cli#1066 review: the degrade must be VISIBLE, not just correct.
# --------------------------------------------------------------------------


def test_the_pinned_manifest_reports_no_problems_at_all():
    """The positive control, and the one that keeps this from crying wolf: the
    real block at `PINNED_SDK_TAG` -- eight entries, two with `licence: null`,
    every `sizeBytes` null -- must be completely silent. A diagnostic that
    fires on the SDK tan actually ships against trains its own reader to
    ignore it."""
    assert artifact_provenance.problems_in(REAL_MANIFEST) is None


def test_an_absent_block_is_silent_but_a_declared_one_is_not():
    """The whole distinction, in one test. No `artifactProvenance` KEY is every
    SDK before alp-sdk v0.16.0 -- normal, silent. A key that IS present and
    unreadable is a producer defect, and before the review finding it produced
    output byte-identical to the silent case, which made an alp-sdk generator
    regression undetectable from the consumer side."""
    assert artifact_provenance.problems_in({"schemaVersion": 1}) is None
    # `null` is NOT absence here: no alp-sdk has ever emitted a null block, so
    # a key carrying one is a defect, not an older SDK.
    assert artifact_provenance.problems_in({"artifactProvenance": None}) == (
        "`artifactProvenance` is null, not an object"
    )


def test_a_non_mapping_block_is_named_by_its_actual_type():
    assert artifact_provenance.problems_in({"artifactProvenance": []}) == (
        "`artifactProvenance` is list, not an object"
    )
    assert artifact_provenance.problems_in({"artifactProvenance": "cmake"}) == (
        "`artifactProvenance` is str, not an object"
    )


def test_a_non_mapping_entry_names_the_tool_it_belongs_to():
    """`doctor` prints this line to a human who then has to file it against
    alp-sdk -- "something is wrong" is not a bug report."""
    assert artifact_provenance.problems_in(
        {"artifactProvenance": {"cmake": "https://cmake.org/"}}
    ) == "`cmake` is str, not an object"
    assert artifact_provenance.problems_in(
        {"artifactProvenance": {7: {"tier": "A"}}}
    ) == "`artifactProvenance` has a non-string key (7)"


def test_a_declared_but_unreadable_field_is_named_with_the_manifests_own_key():
    """`source`, not `sourceUrl`: this line describes what is wrong in
    alp-sdk's FILE, and pointing a reader at a key their file does not contain
    would send them looking for the wrong thing. The rename lives on the wire
    only."""
    assert artifact_provenance.problems_in(
        {"artifactProvenance": {"cmake": {"source": 7}}}
    ) == "`cmake.source` is not readable (7)"


def test_an_absent_or_null_field_is_not_a_problem():
    """A field the manifest never declared, or declared as `null`, is the
    normal shape (`xz`/`7zip` licence, every `sizeBytes` today) -- reported as
    `null` on the wire and silent here. Only a value tan could not READ is a
    problem."""
    assert artifact_provenance.problems_in({"artifactProvenance": {"xz": {}}}) is None
    assert artifact_provenance.problems_in(
        {"artifactProvenance": {"xz": {"tier": "A", "licence": None, "sizeBytes": None}}}
    ) is None


def test_the_problem_report_is_derived_from_the_parser_not_a_second_opinion():
    """The anti-drift property, asserted rather than described: a field is
    reported unreadable EXACTLY when the manifest declared a value and the real
    reader still answered `None`. `-4096` is the live proof -- it is a
    perfectly good int that only `_count`'s own (newer) negative refusal
    rejects, so a hand-written duplicate of the type rules here would have
    missed it."""
    doc = {"artifactProvenance": {"cmake": {"sizeBytes": -4096}}}
    assert artifact_provenance.parse_table(doc["artifactProvenance"])["cmake"].size_bytes is None
    assert artifact_provenance.problems_in(doc) == "`cmake.sizeBytes` is not readable (-4096)"


def test_many_problems_are_bounded_but_the_count_stays_exact():
    """A wholly corrupt block would otherwise put dozens of clauses into one
    `doctor` line. Truncating without a count would hide how bad it is."""
    line = artifact_provenance.problems_in(
        {
            "artifactProvenance": {
                "cmake": {"tier": 1, "licence": [], "source": 2, "sizeBytes": "x"},
                "ninja": {"tier": 3},
            }
        }
    )
    assert line.startswith("`cmake.tier` is not readable (1); ")
    assert line.endswith("; and 2 more")
    assert line.count(";") == 3
