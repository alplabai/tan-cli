# SPDX-License-Identifier: Apache-2.0
"""`tan.planner.template._derive_pin_doc_renames` (tan-cli#494 defect 9).

Companion to `_derive_pin_renames`/`_derive_pin_macro_renames`, both of which
raise `TemplateError` when two `pins:` entries re-derive to two DIFFERENT
targets -- `_derive_pin_doc_renames` did not: it is keyed by `doc:` TEXT
rather than by pad, and `renames[old_doc] = new_doc` overwrote without a
collision check, so two entries sharing one `doc:` string but resolving to
different target routes collapsed onto whichever resolved LAST.
`_substitute_board_yaml_pin_docs`'s replace-ALL `subn` then stamps that one
doc onto BOTH entries -- one pad documented with the other pad's electricals.

Requires a bound alp-sdk checkout for the same reason as every other
`tan.planner`-importing test in this tree: the package's `__init__` eagerly
reads real `metadata/registries/*` at import time. `_resolve_pin_target` is
monkeypatched here rather than driven through real board metadata -- this is
a unit test of the collision guard itself, not of pin resolution.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _sdk_root() -> Path | None:
    for var in ("ALP_SDK_PARITY_ROOT", "ALP_SDK_ROOT"):
        raw = os.environ.get(var)
        if raw and (Path(raw) / "scripts" / "alp_project.py").is_file():
            return Path(raw).resolve()
    return None


SDK = _sdk_root()

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="set ALP_SDK_ROOT to an alp-sdk checkout so tan.planner can bind "
    "a root and import (same requirement as the parity suite)",
)


@pytest.fixture(scope="module")
def template_mod():
    from tan.planner_root import bind_sdk_root

    assert SDK is not None
    bind_sdk_root(SDK)
    from tan.planner import template

    return template


def test_two_pins_sharing_one_doc_but_different_targets_raise(template_mod, monkeypatch):
    targets = {
        "pad-a": {"e1m": "E1M_GPIO_IO4", "doc": "new doc A"},
        "pad-b": {"e1m": "E1M_GPIO_IO5", "doc": "new doc B"},
    }

    def _fake_resolve(item, sku, source_preset, metadata_root):  # noqa: ARG001
        return targets[item["_id"]]

    monkeypatch.setattr(template_mod, "_resolve_pin_target", _fake_resolve)

    pins = [
        {"_id": "pad-a", "e1m": "PAD_A", "doc": "same shared doc"},
        {"_id": "pad-b", "e1m": "PAD_B", "doc": "same shared doc"},
    ]

    with pytest.raises(template_mod.TemplateError, match="ambiguous"):
        template_mod._derive_pin_doc_renames(pins, "E1M-V2N101", "e1m-evk", Path("/x"))


def test_two_pins_sharing_one_doc_and_the_same_target_do_not_raise(template_mod, monkeypatch):
    targets = {
        "pad-a": {"e1m": "E1M_GPIO_IO4", "doc": "new doc"},
        "pad-b": {"e1m": "E1M_GPIO_IO5", "doc": "new doc"},
    }

    def _fake_resolve(item, sku, source_preset, metadata_root):  # noqa: ARG001
        return targets[item["_id"]]

    monkeypatch.setattr(template_mod, "_resolve_pin_target", _fake_resolve)

    pins = [
        {"_id": "pad-a", "e1m": "PAD_A", "doc": "same shared doc"},
        {"_id": "pad-b", "e1m": "PAD_B", "doc": "same shared doc"},
    ]

    renames = template_mod._derive_pin_doc_renames(pins, "E1M-V2N101", "e1m-evk", Path("/x"))
    assert renames == {"same shared doc": "new doc"}


def test_a_single_pin_still_renames_its_doc(template_mod, monkeypatch):
    def _fake_resolve(item, sku, source_preset, metadata_root):  # noqa: ARG001
        return {"e1m": "E1M_GPIO_IO4", "doc": "new doc"}

    monkeypatch.setattr(template_mod, "_resolve_pin_target", _fake_resolve)

    pins = [{"e1m": "PAD_A", "doc": "old doc"}]
    renames = template_mod._derive_pin_doc_renames(pins, "E1M-V2N101", "e1m-evk", Path("/x"))
    assert renames == {"old doc": "new doc"}
