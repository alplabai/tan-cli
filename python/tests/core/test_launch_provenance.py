# SPDX-License-Identifier: Apache-2.0
"""`tan.core.launch_provenance` -- the `.alp/` content-hash sidecar
(tan-cli#518) that lets `debug_launch._merge_list_by_identity` tell "tan
wrote this" apart from "the customer's, leave it alone" without relying on
position. `tests/commands/test_debug_config_command.py` covers the sidecar
wired into the real merge/CLI; this file is the pure-logic half: hashing,
parsing, and the immutable-update contract, exercised directly.
"""
from __future__ import annotations

import json

import pytest

from tan.core import launch_provenance


def test_content_hash_ignores_key_order():
    """The issue's own requirement, verbatim: "re-serialisation with
    different key order ... must not read as an edit". A `setupCommands`
    entry an external formatter (or a hand-edit that only reordered fields)
    re-serialised with `text`/`ignoreFailures` swapped must hash IDENTICAL
    to the original, or routine formatting would orphan every entry the
    sidecar ever recorded."""
    a = {"text": "-enable-pretty-printing", "ignoreFailures": True}
    b = {"ignoreFailures": True, "text": "-enable-pretty-printing"}
    assert launch_provenance.content_hash(a) == launch_provenance.content_hash(b)


def test_content_hash_is_indifferent_to_source_whitespace():
    """The other half of the same requirement: `content_hash` hashes the
    PARSED value, never raw text, so two representations of the identical
    value that merely differ in incidental whitespace during their own
    round-trip through `json.dumps`/`json.loads` still agree."""
    value = ["board/renesas_rzv2n.cfg"]
    reloaded = json.loads(json.dumps(value, indent=4))
    assert launch_provenance.content_hash(value) == launch_provenance.content_hash(reloaded)


def test_content_hash_changes_with_the_actual_value():
    """The hash must not be a constant -- the property above is worthless if
    every input collapses to the same digest. A changed field value (the
    ordinary case: a customer edited the command, or a new build resolved a
    different one) must hash differently."""
    a = {"text": "-enable-pretty-printing"}
    b = {"text": "-enable-pretty-printing", "ignoreFailures": True}
    assert launch_provenance.content_hash(a) != launch_provenance.content_hash(b)
    assert launch_provenance.content_hash("board/a.cfg") != launch_provenance.content_hash(
        "board/b.cfg"
    )


@pytest.mark.parametrize(
    "content",
    [
        None,
        "",
        "not json at all {{{",
        "[]",  # valid JSON, but not an object
        '{"schemaVersion": 2, "configurations": {}}',  # unrecognised schema
        '{"configurations": {}}',  # schemaVersion missing entirely
        '{"schemaVersion": 1, "configurations": "not-an-object"}',
        '{"schemaVersion": 1, "configurations": {"Alp: X": "not-an-object"}}',
        # a field whose recorded hashes are not a list of strings -- must not
        # crash, and must not be trusted either.
        '{"schemaVersion": 1, "configurations": {"Alp: X": {"configFiles": [1, 2]}}}',
        '{"schemaVersion": 1, "configurations": {"Alp: X": {"configFiles": "nope"}}}',
    ],
)
def test_load_of_anything_unreadable_degrades_to_empty(content):
    """tan-cli#518's own asymmetry, stated as a test: EVERY shape `load`
    cannot confidently parse -- absent, empty, malformed JSON, the wrong
    top-level shape, a schema version this build does not recognise, or a
    per-field value that is not a list of strings -- must degrade to the
    record that owns NOTHING, never raise, and never be silently trusted for
    any (configuration, field) pair. This is the test that would catch a
    `load` that swallowed an exception and returned a HALF-populated record
    (some fields real, some coincidentally empty) instead of the fully empty
    one the design demands."""
    record = launch_provenance.load(content)
    assert record.hashes_for("Alp: X", "configFiles") == frozenset()
    assert record.hashes_for("anything", "setupCommands") == frozenset()


def test_a_missing_sidecar_is_indistinguishable_from_an_unreadable_one():
    """The CLI passes `None` for both "the file does not exist" and "the
    file could not be read" (`debug_config_cmd.py`'s own best-effort read) --
    this pins that `load(None)` really is `empty()`, not merely "close
    enough", so a future refactor that special-cased one of the two callers
    would be caught here rather than discovered as a stray still-referenced
    entry the FIRST time it desyncs from the real one."""
    assert launch_provenance.load(None) == launch_provenance.empty()


def test_updated_round_trips_through_render_and_load():
    """A record built via `updated`, rendered to JSON, and reloaded must
    answer `hashes_for` identically to the in-memory record it came from --
    the sidecar's whole reason to exist is surviving exactly that
    write-then-read cycle across two separate `tan debug-config` processes."""
    record = launch_provenance.empty().updated(
        "Alp: Zephyr Debug (OpenOCD)",
        {
            "configFiles": ["board/renesas_rzv2n.cfg", "interface/jlink.cfg"],
            "setupCommands": [{"text": "-enable-pretty-printing"}],
        },
    )
    reloaded = launch_provenance.load(launch_provenance.render(record))

    for field, entries in (
        ("configFiles", ["board/renesas_rzv2n.cfg", "interface/jlink.cfg"]),
        ("setupCommands", [{"text": "-enable-pretty-printing"}]),
    ):
        expected = frozenset(launch_provenance.content_hash(e) for e in entries)
        assert reloaded.hashes_for("Alp: Zephyr Debug (OpenOCD)", field) == expected


def test_updated_never_mutates_the_record_it_was_called_on():
    """`updated`'s own contract, load-bearing for `create_launch_json_write_
    plan`: it must return a NEW record, never mutate `self` -- a caller still
    holding the ORIGINAL `provenance` object (as `debug_config_cmd.py` does,
    reusing it for `sdk_identity_overwrites` before the merge that produces
    the updated one) must keep seeing the pre-update hashes on it. A `record`
    call leaking through would make this FALSE."""
    original = launch_provenance.empty().updated(
        "Alp: X", {"configFiles": ["board/a.cfg"]}
    )
    original_hashes = original.hashes_for("Alp: X", "configFiles")

    updated = original.updated("Alp: X", {"configFiles": ["board/b.cfg"]})

    assert original.hashes_for("Alp: X", "configFiles") == original_hashes
    assert original.hashes_for("Alp: X", "configFiles") != updated.hashes_for(
        "Alp: X", "configFiles"
    )


def test_updated_leaves_a_field_it_does_not_touch_exactly_as_it_was():
    """`LaunchProvenance.updated`'s own documented contract: a field ABSENT
    from `owned_entries` keeps whatever the record already had -- this is
    what lets one run touch only `configFiles` (say, `setupCommands` never
    changed) without wiping `setupCommands`' own, still-valid, provenance."""
    record = launch_provenance.empty().updated(
        "Alp: Yocto Remote Debug",
        {"setupCommands": [{"text": "-enable-pretty-printing"}]},
    )
    before = record.hashes_for("Alp: Yocto Remote Debug", "setupCommands")

    touched_only_config_files = record.updated(
        "Alp: Yocto Remote Debug", {"configFiles": ["irrelevant.cfg"]}
    )

    assert touched_only_config_files.hashes_for(
        "Alp: Yocto Remote Debug", "setupCommands"
    ) == before


def test_updated_fully_replaces_a_touched_field_rather_than_merging_into_it():
    """The other half of the same contract: a field that DOES appear in
    `owned_entries` is REPLACED wholesale, not unioned with its own stale
    hashes -- an entry that vanished from this run's own resolution must not
    leave a phantom hash behind for something unrelated to accidentally
    match later."""
    record = launch_provenance.empty().updated(
        "Alp: Zephyr Debug (OpenOCD)", {"configFiles": ["board/stale.cfg"]}
    )

    replaced = record.updated(
        "Alp: Zephyr Debug (OpenOCD)", {"configFiles": ["board/fresh.cfg"]}
    )

    hashes = replaced.hashes_for("Alp: Zephyr Debug (OpenOCD)", "configFiles")
    assert hashes == frozenset({launch_provenance.content_hash("board/fresh.cfg")})


def test_hashes_for_an_unrecorded_configuration_or_field_is_the_empty_set():
    """The default every lookup falls back to -- a configuration the sidecar
    has never seen, or a field within a KNOWN configuration that was never
    recorded (e.g. `setupCommands` on a target that only ever resolves
    `configFiles`), both read as "nothing is ours", not an error."""
    record = launch_provenance.empty().updated(
        "Alp: Zephyr Debug (OpenOCD)", {"configFiles": ["board/a.cfg"]}
    )
    assert record.hashes_for("Alp: Zephyr Debug (OpenOCD)", "setupCommands") == frozenset()
    assert record.hashes_for("Alp: Baremetal Debug (J-Link)", "configFiles") == frozenset()


def test_render_emits_no_marker_into_a_separate_document():
    """Byte-cleanliness of `launch.json` is enforced elsewhere (the merge
    never writes anything but the SDK-recognised keys); this pins the OTHER
    half -- the sidecar itself is plain, parseable, schema-tagged JSON with
    a `configurations` map keyed by name, not some bespoke format that only
    `load` can make sense of."""
    record = launch_provenance.empty().updated(
        "Alp: X", {"configFiles": ["a.cfg"]}
    )
    document = json.loads(launch_provenance.render(record))
    assert document["schemaVersion"] == launch_provenance.SCHEMA_VERSION
    assert list(document["configurations"]["Alp: X"]["configFiles"]) == [
        launch_provenance.content_hash("a.cfg")
    ]
