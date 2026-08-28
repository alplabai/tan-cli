# SPDX-License-Identifier: Apache-2.0
"""`tan.core.example_facets` -- the `metadata/catalog.json` reader that feeds
`tan examples`' additive `category`/`som`/`board`/`cores`/`coreCount`/
`osSet`/`declares` facets (tan-cli#484).

Unit level: the lookup by `sourceDir`, the type-guarding of every optional
field, and the "never raises, absence means nothing to report" contract for
every way the catalogue can fail to read. The command-level surface (the
envelope, discovery composing the lookup) is
`tests/commands/test_examples_command.py`.
"""

from __future__ import annotations

import json

from tan.core.example_facets import ExampleFacets, load_example_facets


def _write_catalog(tmp_path, examples_by_category):
    (tmp_path / "metadata").mkdir(exist_ok=True)
    (tmp_path / "metadata" / "catalog.json").write_text(
        json.dumps({"examples": examples_by_category}), encoding="utf-8"
    )
    return tmp_path


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_loads_every_facet_keyed_by_source_dir(tmp_path):
    _write_catalog(
        tmp_path,
        {
            "multicore": [
                {
                    "name": "rpmsg-v2n",
                    "path": "examples/multicore/rpmsg-v2n",
                    "som": "E1M-V2N101",
                    "board": "e1m-evk",
                    "cores": [
                        {"id": "a55_cluster", "os": "yocto", "app": "./linux"},
                        {"id": "m33_sm", "os": "zephyr"},
                    ],
                    "coreCount": 2,
                    "osSet": ["yocto", "zephyr"],
                    "declares": {
                        "chips": True,
                        "ipc": True,
                        "models": False,
                        "peripherals": True,
                    },
                }
            ]
        },
    )

    facets = load_example_facets(tmp_path)
    assert set(facets) == {"multicore/rpmsg-v2n"}
    entry = facets["multicore/rpmsg-v2n"]
    assert entry == ExampleFacets(
        category="multicore",
        som="E1M-V2N101",
        board="e1m-evk",
        cores=(
            {"id": "a55_cluster", "os": "yocto", "app": "./linux"},
            {"id": "m33_sm", "os": "zephyr"},
        ),
        core_count=2,
        os_set=("yocto", "zephyr"),
        declares={"chips": True, "ipc": True, "models": False, "peripherals": True},
    )


def test_two_categories_both_load(tmp_path):
    _write_catalog(
        tmp_path,
        {
            "audio": [{"name": "i2s-tone", "declares": {}}],
            "aen": [{"name": "aen-analog-validate", "declares": {}}],
        },
    )
    facets = load_example_facets(tmp_path)
    assert set(facets) == {"audio/i2s-tone", "aen/aen-analog-validate"}
    assert facets["audio/i2s-tone"].category == "audio"
    assert facets["aen/aen-analog-validate"].category == "aen"


# --------------------------------------------------------------------------
# Optional fields genuinely absent (gen_catalog.py omits, never nulls)
# --------------------------------------------------------------------------


def test_entry_with_only_the_always_present_fields(tmp_path):
    """`gen_catalog.py` omits a key it has nothing to say for -- a portable
    example with no `som:`, no resolvable topology, carries only `name` and
    `declares`."""
    _write_catalog(tmp_path, {"testing": [{"name": "minimal", "declares": {}}]})
    entry = load_example_facets(tmp_path)["testing/minimal"]
    assert entry.category == "testing"
    assert entry.som is None
    assert entry.board is None
    assert entry.cores is None
    assert entry.core_count is None
    assert entry.os_set is None
    assert entry.declares == {}


# --------------------------------------------------------------------------
# Never raises -- every way the source can fail to read
# --------------------------------------------------------------------------


def test_missing_catalog_is_empty_not_an_error(tmp_path):
    assert load_example_facets(tmp_path) == {}


def test_unreadable_json_is_empty_not_an_error(tmp_path):
    (tmp_path / "metadata").mkdir()
    (tmp_path / "metadata" / "catalog.json").write_text("{not json", encoding="utf-8")
    assert load_example_facets(tmp_path) == {}


def test_non_object_top_level_is_empty_not_an_error(tmp_path):
    (tmp_path / "metadata").mkdir()
    (tmp_path / "metadata" / "catalog.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert load_example_facets(tmp_path) == {}


def test_examples_key_wrong_shape_is_empty_not_an_error(tmp_path):
    _write_catalog(tmp_path, [])
    assert load_example_facets(tmp_path) == {}


def test_entries_list_wrong_shape_is_skipped_not_fatal(tmp_path):
    """One category's `entries` being malformed does not lose the others."""
    (tmp_path / "metadata").mkdir()
    (tmp_path / "metadata" / "catalog.json").write_text(
        json.dumps(
            {
                "examples": {
                    "broken": "not a list",
                    "fine": [{"name": "ok", "declares": {}}],
                }
            }
        ),
        encoding="utf-8",
    )
    assert set(load_example_facets(tmp_path)) == {"fine/ok"}


def test_a_malformed_entry_in_the_list_is_skipped_not_fatal(tmp_path):
    _write_catalog(
        tmp_path,
        {"audio": ["not a dict", {"name": "ok", "declares": {}}, {"declares": {}}]},
    )
    # `"not a dict"` is skipped; the no-`name` entry is skipped; `ok` loads.
    assert set(load_example_facets(tmp_path)) == {"audio/ok"}


def test_wrong_typed_optional_fields_degrade_to_none_not_a_crash(tmp_path):
    """A catalogue that does not match `gen_catalog.py`'s own shape (hand-edited,
    a future schema change) must not crash the reader -- every optional field is
    independently type-guarded."""
    _write_catalog(
        tmp_path,
        {
            "audio": [
                {
                    "name": "odd",
                    "som": 12345,
                    "board": ["not", "a", "string"],
                    "cores": "not-a-list",
                    "coreCount": "three",
                    "osSet": {"not": "a-list"},
                    "declares": ["not", "a", "dict"],
                }
            ]
        },
    )
    entry = load_example_facets(tmp_path)["audio/odd"]
    assert entry.som is None
    assert entry.board is None
    assert entry.cores is None
    assert entry.core_count is None
    assert entry.os_set is None
    assert entry.declares is None
