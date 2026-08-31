# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1037: `tan/planner/template.py::_board_route_entries` had no
`isinstance(dict)` guard at EITHER level between `yaml.safe_load(...)` and
the bare `.get(...)` chain it performs on a decoded `metadata/boards/
<board_name>.yaml` document.

Two levels, per the issue's own amendment (filed after PR #1034's round-two
review found the issue as originally written covered only the first):

1. The OUTER document -- `metadata/boards/<board_name>.yaml` itself parsing
   to something other than a mapping (e.g. a bare list). Measured, verbatim
   from the issue:

       metadata/boards/<board>.yaml = "- one\\n- two\\n"
       _board_route_entries("<board>", root) -> AttributeError: 'list'
                                                  object has no attribute
                                                  'get'

2. The NESTED `e1m_routes:` field -- the outer document IS a mapping, but
   `e1m_routes:` itself is not. Measured (PR #1034 round-two review, the
   amendment's own citation):

       e1m_routes: [gpio]   -> routes.get(section) bare ->
                                AttributeError: 'list' object has no
                                attribute 'get'
       e1m_routes: a string -> AttributeError: 'str' object has no
                                attribute 'get'

Guarding only level 1 leaves level 2 reachable one document deeper -- the
same "outer guarded, nested missed" shape tan-cli#1025 round one left behind
for `_load_som_doc`'s own nested reads (fixed in PR #1034 round two by
`_topology_for_sku`). This fix mirrors that shape: outer `isinstance(dict)`
guard, then a guard on the specific nested field this function itself reads
(`e1m_routes:`), each raising `TemplateError` naming the board path and the
actual type instead of letting a raw `AttributeError` escape.

`_board_alias_to_entry` (`_board_route_entries`'s only caller inside this
module, reached by the exact same CLI path via `_resolve_pin_target`) is
covered by construction -- it has no board-document read of its own, only
`_board_route_entries(...)`'s already-guarded result.

Importing `tan.planner.template` needs SOME bound alp-sdk root (its package
`__init__` reads `metadata/registries/*` at import time) -- same requirement
as `test_load_som_doc_malformed_preset.py` -- even though these cases never
touch that checkout's own metadata content (a synthetic `metadata_root` is
passed explicitly to every call under test).
"""
from __future__ import annotations

import pytest

from tan.planner_root import bind_sdk_root
from tests.conftest import sdk_root

SDK = sdk_root()

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.template requires SOME bound "
           "root (tan/planner_root.py). A SKIP about the missing root, not a "
           "pass.",
)


@pytest.fixture(autouse=True)
def _bound_sdk():
    bind_sdk_root(SDK)
    yield


def _tmpl():
    """Imported inside the call so the module is not imported before
    `bind_sdk_root` has run (collection order)."""
    import tan.planner.template as m
    return m


_BOARD = "e1m-fake1037"


def _metadata_root_with_raw_board(tmp_path, board_yaml_text: str):
    """A metadata/ tree whose board doc is @board_yaml_text VERBATIM -- for
    exercising a board document that doesn't even parse to a mapping, or
    whose `e1m_routes:` field doesn't."""
    root = tmp_path / "metadata"
    (root / "boards").mkdir(parents=True)
    (root / "boards" / f"{_BOARD}.yaml").write_text(
        board_yaml_text, encoding="utf-8")
    return root


# ---------------------------------------------------------------------
# Level 1: the outer board document.
# ---------------------------------------------------------------------

def test_a_bare_list_board_doc_raises_a_curated_error_not_an_attributeerror(tmp_path):
    """The issue's own repro shape: a board YAML that parses to a bare
    list (legal YAML, illegal board schema) must raise `TemplateError`,
    not an uncaught `AttributeError: 'list' object has no attribute
    'get'` from the outer `.get("e1m_routes")`."""
    tmpl = _tmpl()
    root = _metadata_root_with_raw_board(tmp_path, "- one\n- two\n")
    with pytest.raises(tmpl.TemplateError, match="expected a YAML mapping"):
        tmpl._board_route_entries(_BOARD, root)


def test_a_bare_scalar_board_doc_raises_a_curated_error_not_an_attributeerror(tmp_path):
    """Same guard, the other bare shape: a board YAML that parses to a
    bare scalar string -- `AttributeError: 'str' object has no
    attribute 'get'` on the unguarded code."""
    tmpl = _tmpl()
    root = _metadata_root_with_raw_board(tmp_path, "just a scalar string\n")
    with pytest.raises(tmpl.TemplateError, match="expected a YAML mapping"):
        tmpl._board_route_entries(_BOARD, root)


def test_the_outer_error_names_the_offending_path_and_the_actual_type(tmp_path):
    """Diagnostic, not generic -- names the board path and the real
    Python type, mirroring `_load_som_doc`'s message."""
    tmpl = _tmpl()
    root = _metadata_root_with_raw_board(tmp_path, "- one\n- two\n")
    board_path = root / "boards" / f"{_BOARD}.yaml"
    with pytest.raises(tmpl.TemplateError) as exc_info:
        tmpl._board_route_entries(_BOARD, root)
    assert "got list" in str(exc_info.value)
    assert str(board_path) in str(exc_info.value)


# ---------------------------------------------------------------------
# Level 2: the outer document IS a mapping, but `e1m_routes:` itself is
# not -- the amendment's own gap, one document deeper.
# ---------------------------------------------------------------------

def test_a_non_mapping_e1m_routes_raises_a_curated_error(tmp_path):
    """`e1m_routes:` present but a bare list, not a mapping -- must not
    reach the per-section `routes.get(section)` bare -- measured on the
    unguarded code: `AttributeError: 'list' object has no attribute
    'get'`."""
    tmpl = _tmpl()
    root = _metadata_root_with_raw_board(
        tmp_path, "e1m_routes:\n  - gpio\n")
    with pytest.raises(tmpl.TemplateError, match="e1m_routes must be a mapping"):
        tmpl._board_route_entries(_BOARD, root)


def test_a_scalar_e1m_routes_raises_a_curated_error(tmp_path):
    """Same guard, the other bare shape: `e1m_routes:` a scalar string --
    `AttributeError: 'str' object has no attribute 'get'` on the
    unguarded code."""
    tmpl = _tmpl()
    root = _metadata_root_with_raw_board(
        tmp_path, "e1m_routes: just-a-string\n")
    with pytest.raises(tmpl.TemplateError, match="e1m_routes must be a mapping"):
        tmpl._board_route_entries(_BOARD, root)


def test_the_nested_error_names_the_offending_path_and_the_actual_type(tmp_path):
    tmpl = _tmpl()
    root = _metadata_root_with_raw_board(
        tmp_path, "e1m_routes:\n  - gpio\n")
    board_path = root / "boards" / f"{_BOARD}.yaml"
    with pytest.raises(tmpl.TemplateError) as exc_info:
        tmpl._board_route_entries(_BOARD, root)
    assert "got list" in str(exc_info.value)
    assert str(board_path) in str(exc_info.value)


def test_board_alias_to_entry_also_raises_not_a_bare_attributeerror(tmp_path):
    """`_board_alias_to_entry` reaches the same malformed document
    through `_board_route_entries` -- the guard lives in the shared
    function, so it must cover this caller too."""
    tmpl = _tmpl()
    root = _metadata_root_with_raw_board(tmp_path, "- one\n- two\n")
    with pytest.raises(tmpl.TemplateError, match="expected a YAML mapping"):
        tmpl._board_alias_to_entry(_BOARD, root)


# ---------------------------------------------------------------------
# Vacuity checks: legitimate documents must still resolve normally.
# ---------------------------------------------------------------------

def test_a_well_formed_board_doc_still_resolves_normally(tmp_path):
    """Vacuity check for BOTH guards: a legitimate board doc with a
    legitimate `e1m_routes:` mapping must not be rejected."""
    tmpl = _tmpl()
    root = _metadata_root_with_raw_board(
        tmp_path,
        "e1m_routes:\n"
        "  gpio:\n"
        "    - e1m: E1M_GPIO_IO0\n"
        "      board_alias: BOARD_LED0\n")
    entries = tmpl._board_route_entries(_BOARD, root)
    assert entries == [{"e1m": "E1M_GPIO_IO0", "board_alias": "BOARD_LED0"}]
    assert tmpl._board_alias_to_entry(_BOARD, root) == {
        "BOARD_LED0": {"e1m": "E1M_GPIO_IO0", "board_alias": "BOARD_LED0"},
    }


def test_a_board_doc_with_no_e1m_routes_at_all_still_resolves_to_empty(tmp_path):
    """Vacuity check for the `or {}` fallback: a board doc that simply
    omits `e1m_routes:` is not malformed -- empty routes, not an
    error."""
    tmpl = _tmpl()
    root = _metadata_root_with_raw_board(tmp_path, "some_other_field: 1\n")
    assert tmpl._board_route_entries(_BOARD, root) == []
