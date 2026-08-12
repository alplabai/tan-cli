# SPDX-License-Identifier: Apache-2.0
"""`tan.planner.template._derive_pin_doc_renames` collision guard
(tan-cli#494 defect 9 / alp-sdk#1394).

`_derive_pin_doc_renames` built its `{old_doc: new_doc | None}` rename map
with NO collision guard, unlike both of its siblings: `_derive_pin_renames`
raises `TemplateError` (`... re-derives to two different targets ...
-- ambiguous`) when two `pins:` entries name the same source pad and
resolve to different target pads, and `_derive_pin_macro_renames` does the
same for `macro:`. The `doc:` companion had two unguarded assignment
sites -- `renames[old_doc] = new_doc` and `renames[old_doc] = None` -- and
per its own docstring `None` means DROP the field entirely.

Two `pins:` entries legitimately sharing one `doc:` string (one sentence
describing a debounce network, a bus, or a connector common to both pads)
keyed a single entry in the flat map `_substitute_board_yaml_pin_docs`
applies file-wide, so the last write won and the winner depended on
`pins:` ordering. Worst case: the entry WITH a target `doc:` was visited
first and the one WITHOUT visited second -- the map ended up `None` and
the documentation was dropped from BOTH pins, including the one whose
re-derived `doc:` was perfectly good.

Fixed in alp-sdk by PR alplabai/alp-sdk#1399 (closes alp-sdk#1394); this
ports the same guard into `tan/planner/template.py`, which is a
hand-audited mirror of `scripts/alp_template.py`
(`HAND_PORT_HASHES["scripts/alp_template.py"]` in
`tests/gates/test_planner_relocation_freshness.py`) and so cannot pick up
an upstream fix automatically.

Uses a synthetic `metadata_root` (built with `tmp_path`), the same
`_derive_pin_doc_renames(original_pins, sku, source_preset, metadata_root)`
call shape alp-sdk's own `tests/scripts/test_alp_template.py` uses --
every aliased route on the real `metadata/boards/e1m-x-evk.yaml` carries
its own `doc:`, so the real tree cannot exercise the string-vs-`None`
branch at all. `tan.planner` still needs SOME bound root to import (its
`__init__` reads `metadata/registries/*` at import time), even though
none of these probes read the bound checkout's own content -- same
posture as `tests/planner/test_sdk_capability.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tan.planner_root import bind_sdk_root
from tests.conftest import sdk_root

SDK = sdk_root()

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.template requires SOME "
           "bound root (tan/planner_root.py), even though every probe "
           "under test here reads only a tmp_path fixture, never the "
           "bound checkout's own content. A SKIP about the missing root, "
           "not a pass.",
)


@pytest.fixture(autouse=True)
def _bound_sdk():
    bind_sdk_root(SDK)
    yield


def _mod():
    """Imported inside the call so the module imports after `bind_sdk_root`
    has run -- same reason `test_sdk_capability.py`'s `_mod()` does it
    lazily."""
    import tan.planner.template as m

    return m


def _write_shared_doc_fixture(root: Path, second_target_doc: str | None) -> Path:
    """A metadata_root whose SRC board routes two pads (`E1M_A`, `E1M_B`)
    that both carry a `board_alias:` onto a DST board. The DST route for
    `BOARD_A` always has its own `doc:`; the one for `BOARD_B` gets
    `second_target_doc` (a different string, or no `doc:` at all when
    `None`). Returns the metadata_root."""
    metadata_root = root / "metadata"
    (metadata_root / "e1m_modules").mkdir(parents=True)
    (metadata_root / "e1m_modules" / "E1M-SRCTEST.yaml").write_text(
        "default_board: SRC-PRESET\n", encoding="utf-8", newline="\n")
    (metadata_root / "e1m_modules" / "E1M-DSTTEST.yaml").write_text(
        "default_board: DST-PRESET\n", encoding="utf-8", newline="\n")

    (metadata_root / "boards").mkdir(parents=True)
    (metadata_root / "boards" / "src-preset.yaml").write_text(
        "e1m_routes:\n"
        "  gpio:\n"
        "    - e1m: E1M_A\n"
        "      macro: SRC_PIN_A\n"
        "      board_alias: BOARD_A\n"
        "    - e1m: E1M_B\n"
        "      macro: SRC_PIN_B\n"
        "      board_alias: BOARD_B\n",
        encoding="utf-8", newline="\n",
    )
    (metadata_root / "boards" / "dst-preset.yaml").write_text(
        "e1m_routes:\n"
        "  gpio:\n"
        "    - e1m: E1M_X_A\n"
        "      macro: DST_PIN_A\n"
        "      board_alias: BOARD_A\n"
        "      doc: Shared debounce network, DST pad A.\n"
        "    - e1m: E1M_X_B\n"
        "      macro: DST_PIN_B\n"
        "      board_alias: BOARD_B\n"
        + (f"      doc: {second_target_doc}\n" if second_target_doc else ""),
        encoding="utf-8", newline="\n",
    )
    return metadata_root


_SHARED_DOC = "Shared debounce network (10k + 0.1uF), SRC pads A and B."

_SHARED_DOC_PINS = [
    {"e1m": "E1M_A", "macro": "SRC_PIN_A", "doc": _SHARED_DOC},
    {"e1m": "E1M_B", "macro": "SRC_PIN_B", "doc": _SHARED_DOC},
]


def test_derive_pin_doc_renames_rejects_two_pins_sharing_a_doc(tmp_path):
    """Two `pins:` entries sharing one `doc:` string that re-derive to two
    DIFFERENT target strings are ambiguous for the flat
    `{old_doc: new_doc}` map `_substitute_board_yaml_pin_docs` applies
    file-wide -- hard error, exactly as `_derive_pin_renames` and
    `_derive_pin_macro_renames` already did for their own keys."""
    m = _mod()
    metadata_root = _write_shared_doc_fixture(
        tmp_path, "Shared debounce network, DST pad B.")
    with pytest.raises(m.TemplateError, match="ambiguous"):
        m._derive_pin_doc_renames(
            _SHARED_DOC_PINS, "E1M-DSTTEST", "src-preset", metadata_root)


def test_derive_pin_doc_renames_rejects_a_shared_doc_one_side_drops(tmp_path):
    """The branch that lost data SILENTLY pre-fix: one entry re-derives the
    shared `doc:` to a target string, the other's target route has no
    `doc:` at all -- `None`, which per this function's docstring means
    DROP the field. Pre-fix the second write won unguarded, so the map
    said `None` and the documentation was dropped from BOTH pins,
    including the one with a perfectly good target `doc:`; reverse the
    `pins:` ordering and the doc survived. "Rename it" and "drop it" are
    contradictory instructions for one key, so this is ambiguous too --
    and the outcome must no longer depend on iteration order."""
    m = _mod()
    metadata_root = _write_shared_doc_fixture(tmp_path, None)
    with pytest.raises(m.TemplateError, match="ambiguous"):
        m._derive_pin_doc_renames(
            _SHARED_DOC_PINS, "E1M-DSTTEST", "src-preset", metadata_root)

    with pytest.raises(m.TemplateError, match="ambiguous"):
        m._derive_pin_doc_renames(
            list(reversed(_SHARED_DOC_PINS)), "E1M-DSTTEST", "src-preset",
            metadata_root)


def test_derive_pin_doc_renames_allows_a_shared_doc_that_agrees(tmp_path):
    """A shared `doc:` is only ambiguous when the entries DISAGREE -- two
    pins whose target routes carry the SAME `doc:` string yield one
    unambiguous rename, not an error."""
    m = _mod()
    metadata_root = _write_shared_doc_fixture(
        tmp_path, "Shared debounce network, DST pad A.")
    assert m._derive_pin_doc_renames(
        _SHARED_DOC_PINS, "E1M-DSTTEST", "src-preset", metadata_root
    ) == {_SHARED_DOC: "Shared debounce network, DST pad A."}


def test_derive_pin_doc_renames_keeps_an_unchanged_doc_out_of_the_map(tmp_path):
    """A target `doc:` byte-identical to the entry's own contributes NO
    map entry -- there is nothing to rewrite. Guarding the two assignment
    sites must not fold that case into the `None` (DROP) value, which
    would delete a `doc:` that was already correct."""
    m = _mod()
    metadata_root = _write_shared_doc_fixture(tmp_path, None)
    pins = [{
        "e1m": "E1M_A", "macro": "SRC_PIN_A",
        "doc": "Shared debounce network, DST pad A.",
    }]
    assert m._derive_pin_doc_renames(
        pins, "E1M-DSTTEST", "src-preset", metadata_root) == {}


def test_derive_pin_doc_renames_rejects_a_shared_doc_kept_then_dropped(tmp_path):
    """The third contradiction the fix closes: one entry's target `doc:`
    is byte-identical to the shared string ("keep it" -- no map entry at
    all), the other's target has none ("drop it"). Pre-fix the map said
    `None` unconditionally, so the pin whose doc was ALREADY correct lost
    it to the file-wide substitution with no diagnostic. Recording every
    resolution -- not only the ones that produce a rename -- is what
    makes this reachable."""
    m = _mod()
    metadata_root = _write_shared_doc_fixture(tmp_path, None)
    pins = [
        {"e1m": "E1M_A", "macro": "SRC_PIN_A",
         "doc": "Shared debounce network, DST pad A."},
        {"e1m": "E1M_B", "macro": "SRC_PIN_B",
         "doc": "Shared debounce network, DST pad A."},
    ]
    with pytest.raises(m.TemplateError, match="ambiguous"):
        m._derive_pin_doc_renames(
            pins, "E1M-DSTTEST", "src-preset", metadata_root)
