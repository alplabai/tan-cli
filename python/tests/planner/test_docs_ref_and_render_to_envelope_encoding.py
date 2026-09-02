# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1116 review round 2/3: two live defects the review found by
hand-driving `tan/planner/template.py` against real broken files, neither
covered by a committed test until now (review round 3 minor).

`_docs_ref` read `metadata/sdk_version.yaml` through `except OSError`
alone, missing BOTH `UnicodeDecodeError` (a `ValueError`, not an
`OSError`) and `yaml.YAMLError` (its own exception hierarchy, not a
`ValueError` either) -- a non-UTF-8 or syntactically-invalid
`sdk_version.yaml` raised raw past this function's own documented
"cost a stale-but-safe link, not the whole scaffold" contract.

`render_to_envelope`'s example `board.yaml` read caught `except OSError`
alone around `board_yaml_path.read_text(encoding="utf-8")`, missing
`UnicodeDecodeError` -- a non-UTF-8 example `board.yaml` raised raw past
this function's own curated-raise `TemplateError` contract instead of
being folded into the same "cannot read template example board.yaml at
<path>" message a genuinely absent file already produces.

Both are fixed in the same change as the twelve (now fifteen) functions
`tests/gates/test_never_raises_contract_holds.py` seeds directly; neither
is seeded there itself, because seeding either would need `bind_sdk_root`
-- global mutable state none of that gate's other fifteen seeds touch, and
this repo's `tan.planner` import-order constraint (`tan/planner_root.py`)
means the gate would either have to bind at import time (risking a
conflict with whatever a DIFFERENT already-collected test module bound
first in the same pytest session) or defer every planner import inside
each test body, the dance `_bound_sdk_fixture.py`'s own docstring explains
at length. This file is the SDK-gated home the fixes to `tan/planner/`
already use instead (`test_render_to_envelope_malformed_example_board.py`,
imported and skipped the identical way), not a duplicate of that mechanism.
"""
from __future__ import annotations

import json

import pytest

# `_bound_sdk` is a pytest fixture, imported for its side effect -- the
# same idiom `_baremetal_support`'s consumers use for `bound_sdk_root`.
from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.template requires SOME bound "
           "root (tan/planner_root.py). A SKIP about the missing root, not a "
           "pass.",
)


def _tmpl():
    """Imported inside the call so the module is not imported before
    `bind_sdk_root` has run (collection order)."""
    import tan.planner.template as m
    return m


# ---------------------------------------------------------------------------
# _docs_ref
# ---------------------------------------------------------------------------


def test_docs_ref_degrades_to_main_on_a_non_utf8_sdk_version_yaml(tmp_path):
    """Measured escaping raw before this fix: a non-UTF-8
    `metadata/sdk_version.yaml` raised `UnicodeDecodeError` straight out of
    `_docs_ref`, past its own "a malformed version file should cost a
    stale-but-safe link, not the whole scaffold" contract."""
    m = _tmpl()
    base = tmp_path / "sdk"
    metadata = base / "metadata"
    metadata.mkdir(parents=True)
    (metadata / "sdk_version.yaml").write_bytes(b"\xff\xfe\x00not-utf8")

    assert m._docs_ref(base) == "main"


def test_docs_ref_degrades_to_main_on_syntactically_invalid_yaml(tmp_path):
    """The other half of the same fix: `yaml.YAMLError` was equally
    uncaught (it is neither an `OSError` nor a `ValueError`) -- a
    `sdk_version.yaml` that does not even parse as YAML raised raw too,
    distinct from the ALREADY-guarded "parses but is not a mapping" shape
    this function's own docstring already covers."""
    m = _tmpl()
    base = tmp_path / "sdk"
    metadata = base / "metadata"
    metadata.mkdir(parents=True)
    (metadata / "sdk_version.yaml").write_text("a: [1, 2", encoding="utf-8")

    assert m._docs_ref(base) == "main"


def test_docs_ref_still_degrades_to_main_on_a_missing_file(tmp_path):
    """Unchanged behaviour, pinned so a future edit cannot silently narrow
    the except clause back down without a test noticing: absent
    `sdk_version.yaml` (the ordinary case for most checkouts) still
    degrades quietly, no exception of any kind."""
    m = _tmpl()
    base = tmp_path / "sdk"
    (base / "metadata").mkdir(parents=True)

    assert m._docs_ref(base) == "main"


# ---------------------------------------------------------------------------
# render_to_envelope's example board.yaml read
# ---------------------------------------------------------------------------

_TEMPLATE = "encodingtest1116"
_SKU = "E1M-ENCODINGTEST"
_EXAMPLE = "examples/peripheral-io/encodingtest1116"


def _non_utf8_tree(tmp_path):
    """The same synthetic (catalog, base_dir, metadata_root) shape
    `test_render_to_envelope_malformed_example_board.py::_tree` builds, but
    with the example `board.yaml` written as raw NON-UTF-8 BYTES -- a shape
    that module's own `_tree(tmp_path, board_yaml_text: str)` cannot
    represent at all, since `str.write_text` requires a valid `str`."""
    root = tmp_path / "render"
    base = root / "sdk"
    example = base / _EXAMPLE
    example.mkdir(parents=True)
    (example / "board.yaml").write_bytes(b"\xff\xfe\x00not-utf8")

    catalog = root / "catalog-v1.json"
    catalog.write_text(json.dumps({"templates": [{
        "id": _TEMPLATE,
        "example": _EXAMPLE,
        "supported": {"som_skus": [_SKU]},
        "files": {"user_owned": ["board.yaml"]},
        "cores": [],
    }]}), encoding="utf-8")

    metadata = root / "metadata"
    (metadata / "e1m_modules").mkdir(parents=True)
    (metadata / "e1m_modules" / f"{_SKU}.yaml").write_text(
        "default_board: FAKE-BOARD\n"
        "topology:\n"
        "  m33_sm:\n"
        "    board: fake/soc/m33\n",
        encoding="utf-8")
    return catalog, base, metadata


def test_a_non_utf8_example_board_yaml_is_a_curated_template_error(tmp_path):
    """Measured escaping raw before this fix: `except OSError` alone did
    not catch `UnicodeDecodeError` (a `ValueError`, not an `OSError`), so a
    non-UTF-8 example `board.yaml` raised past `render_to_envelope`'s own
    curated-raise contract instead of becoming a `TemplateError` naming the
    path -- the same shape the existing `FileNotFoundError` case in this
    module's sibling test file already covers."""
    m = _tmpl()
    catalog, base, metadata = _non_utf8_tree(tmp_path)

    with pytest.raises(m.TemplateError) as excinfo:
        m.render_to_envelope(
            _TEMPLATE, _SKU,
            catalog_path=catalog, base_dir=base, metadata_root=metadata)

    message = str(excinfo.value)
    assert "cannot read template example board.yaml at" in message
    assert "codec can't decode" in message
