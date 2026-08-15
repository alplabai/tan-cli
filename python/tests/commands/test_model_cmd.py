# SPDX-License-Identifier: Apache-2.0
"""`_resolve_compile` -- the `models[].compile.<backend>` path resolver.

Pins alp-sdk#1271: only `config`/`calibration`/`images`/`spec` name paths.
`_resolve_compile` used to resolve EVERY string value in a compile block to an
absolute filesystem path, so DRP-AI's `input_shape` ("1,3,224,224"),
`input_name` ("images") and `product` ("V2N") -- opaque strings the adapter
must receive unchanged -- were corrupted into filesystem paths before ever
reaching the adapter, which then made the adapter's own shape check misfire.
alp-sdk fixed this as issue #1271; tan's hand-ported copy never received it.
"""
from pathlib import Path

from tan.commands.model_cmd import _resolve_compile


def test_resolve_compile_leaves_non_path_options_unchanged(tmp_path):
    """alp-sdk#1271: only `config`/`calibration`/`images`/`spec` name paths.
    Resolving a shape string turned "1,3,224,224" into a filesystem path and
    made the DRP-AI adapter's own shape check misfire."""
    out = _resolve_compile(
        {"drpai": {"input_shape": "1,3,224,224", "input_name": "images",
                   "product": "V2N", "config": "cfg.json"}},
        tmp_path,
    )
    assert out["drpai"]["input_shape"] == "1,3,224,224"
    assert out["drpai"]["input_name"] == "images"
    assert out["drpai"]["product"] == "V2N"
    # the one genuine path key IS resolved, absolute, against board.yaml's dir
    assert out["drpai"]["config"] == str((tmp_path / "cfg.json").resolve())


def test_resolve_compile_passes_through_none_and_empty():
    """An absent `compile:` block and an empty one both fall through the
    `if not block:` guard to `None` -- true of both the pre-fix tan code and
    the upstream alp-sdk#1271 fix (`if not block: return None`, unchanged by
    that fix); this is pre-existing, unrelated behaviour, not part of the
    path-key drift this test file otherwise pins."""
    assert _resolve_compile(None, Path(".")) is None
    assert _resolve_compile({}, Path(".")) is None
