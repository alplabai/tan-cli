# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1133: the THIRD shape of the tan-cli#1116 class -- no `try` at all.

`tests/gates/test_never_raises_contract_holds.py` now seeds
`template._load_som_doc` and `template._board_route_entries` and drives each
directly. This file drives the same defect the way a user meets it: through
the real `emit_scaffold`, which `tan/planner/cli.py:187-190` wraps with
`except TemplateError` **and nothing else**, so anything else reaches the
user as a bare traceback.

WHAT WAS MEASURED, on this tree, before the fix (3.12.3, 3.13.15 and 3.14.7
alike, non-root, one line per cell):

    site                                    shape             escaped as
    _load_som_doc          (template.py)    non-UTF-8 byte    UnicodeDecodeError
    _load_som_doc          (template.py)    malformed YAML    yaml.parser.ParserError
    _load_som_doc          (template.py)    chmod 000 file    PermissionError
    _board_route_entries   (template.py)    non-UTF-8 byte    UnicodeDecodeError
    _board_route_entries   (template.py)    malformed YAML    yaml.parser.ParserError
    _board_route_entries   (template.py)    chmod 000 file    PermissionError
    render_to_envelope     (template.py)    malformed YAML    yaml.parser.ParserError

The last row is the one the issue did not name and this file found by
sweeping the module rather than fixing only the two sites reported.
tan-cli#1116 fixed `render_to_envelope`'s READ (`board_yaml_path.read_text`)
and left the `yaml.safe_load` one line below it bare, so a template example
`board.yaml` that decoded as UTF-8 perfectly well but did not parse still
escaped -- the same "proximity to a fixed defect is not coverage" the issue
makes about the two sites next door, one line further in. Fixed with them.

THE #1127 CELL, and why the `is_file()` pre-flight is deleted rather than
supplemented. Against a `chmod 000` PARENT directory the two seeded sites
diverged BY INTERPRETER before the fix -- raw `PermissionError` out of
`Path.is_file()` itself on 3.12.3 and 3.13.15, and on 3.14.7 (where
`is_file()` swallows every `OSError` and answers `False`) the curated but
FALSE message `no metadata/e1m_modules/<sku>.yaml for sku '<sku>'` about a
file that is right there. A pre-flight cannot tell absent from unreadable on
any interpreter; it only differs in which wrong answer it gives. Classifying
on the real exception gives one answer on all three, asserted below.

The permission cases are `@_skip_as_root` for the reason
`tests/gates/test_never_raises_contract_holds.py`'s own ROOT/CI CAVEAT
gives: `chmod 000` does nothing for root. CI's `ubuntu-latest` legs have no
`container:` and run as the unprivileged `runner`, so they execute there.
"""
from __future__ import annotations

import contextlib
import json
import os

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

_skip_as_root = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX-only, non-root: chmod 0o000 has no effect for root and "
           "Windows ACLs don't honour POSIX mode bits.",
)


def _tmpl():
    """Imported inside the call so the module is not imported before
    `bind_sdk_root` has run (collection order)."""
    import tan.planner.template as m
    return m


_TEMPLATE = "fake1133"
_SKU = "E1M-FAKE1133"
_EXAMPLE = "examples/peripheral-io/fake1133"

#: `preset:` must DIFFER from the sku's own `default_board:` lower-cased, or
#: `_resolve_pin_target` short-circuits to `None` before it ever reads a
#: board document -- and then the `board` site below would be unreachable and
#: every one of its cases would pass vacuously.
_SOURCE_PRESET = "source-board"

_GOOD_BOARD_YAML = f"""\
som:
  sku: {_SKU}

preset: {_SOURCE_PRESET}

pins:
  - E1M_GPIO_IO4

cores:
  m33_sm:
    app: ./src
"""

_GOOD_SOM = (
    "default_board: TARGET-BOARD\n"
    "topology:\n"
    "  m33_sm:\n"
    "    board: fake/soc/m33\n"
)

_GOOD_ROUTES = (
    "e1m_routes:\n"
    "  gpio:\n"
    "    - e1m: E1M_GPIO_IO4\n"
    "      board_alias: BOARD_BUTTON\n"
)


def _tree(tmp_path):
    """A synthetic (catalog, base_dir, metadata_root) triple that renders
    cleanly -- every case below breaks exactly one file in it."""
    root = tmp_path / "scaffold"
    base = root / "sdk"
    example = base / _EXAMPLE
    example.mkdir(parents=True)
    (example / "board.yaml").write_text(_GOOD_BOARD_YAML, encoding="utf-8")

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
        _GOOD_SOM, encoding="utf-8")
    (metadata / "boards").mkdir(parents=True)
    for name in (_SOURCE_PRESET, "target-board"):
        (metadata / "boards" / f"{name}.yaml").write_text(
            _GOOD_ROUTES, encoding="utf-8")
    return catalog, base, metadata


#: `{site: how to reach its one document from (base, metadata)}`. All three
#: are read by ONE `emit_scaffold` call, which is the whole point: the issue's
#: "one call away from the read #1116 fixed, in the same invocation".
_SITES = {
    "som-preset": lambda base, metadata: (
        metadata / "e1m_modules" / f"{_SKU}.yaml"),
    "board-metadata": lambda base, metadata: (
        metadata / "boards" / f"{_SOURCE_PRESET}.yaml"),
    "template-example-board": lambda base, metadata: (
        base / _EXAMPLE / "board.yaml"),
}


def _emit(tmp_path, site: str, break_it):
    """Render the synthetic tree with @site's document broken by @break_it,
    and return the raised exception (or `None` when it rendered)."""
    m = _tmpl()
    catalog, base, metadata = _tree(tmp_path)
    with break_it(_SITES[site](base, metadata)):
        try:
            m.emit_scaffold(_TEMPLATE, _SKU, catalog_path=catalog,
                            base_dir=base, metadata_root=metadata)
        except BaseException as exc:  # noqa: BLE001 -- the class IS the assertion
            return exc
    return None


@contextlib.contextmanager
def _non_utf8(path):
    path.write_bytes(b"\xff\xfe\x00not-utf8")
    yield


@contextlib.contextmanager
def _unparseable_yaml(path):
    path.write_text("a: [1, 2\nb: }{\n", encoding="utf-8")
    yield


@contextlib.contextmanager
def _chmod_000_file(path):
    original = path.stat().st_mode
    path.chmod(0o000)
    try:
        yield
    finally:
        path.chmod(original)


@contextlib.contextmanager
def _chmod_000_parent(path):
    original = path.parent.stat().st_mode
    path.parent.chmod(0o000)
    try:
        yield
    finally:
        path.parent.chmod(original)


def test_the_wellformed_tree_still_renders(tmp_path):
    """The control. Every refusal below has to be a refusal of a BROKEN
    document, not of the shape this harness itself builds."""
    m = _tmpl()
    catalog, base, metadata = _tree(tmp_path)
    out = m.emit_scaffold(_TEMPLATE, _SKU, catalog_path=catalog,
                          base_dir=base, metadata_root=metadata)
    assert [entry["path"] for entry in json.loads(out)] == ["board.yaml"]


@pytest.mark.parametrize("site", sorted(_SITES))
def test_a_non_utf8_document_is_a_curated_template_error(tmp_path, site):
    """Measured pre-fix: raw `UnicodeDecodeError` out of `emit_scaffold` at
    the two `metadata/**` sites (the example `board.yaml` was already fixed
    by tan-cli#1116, and is asserted here so a regression there reds too)."""
    m = _tmpl()
    exc = _emit(tmp_path, site, _non_utf8)
    assert isinstance(exc, m.TemplateError), f"raw {type(exc).__name__}"
    assert "cannot read" in str(exc)


@pytest.mark.parametrize("site", sorted(_SITES))
def test_an_unparseable_yaml_document_is_a_curated_template_error(
        tmp_path, site):
    """Measured pre-fix: raw `yaml.parser.ParserError` at ALL THREE sites --
    `yaml.YAMLError` is neither an `OSError` nor a `ValueError`, so no
    `except` clause anywhere on this path had ever covered it."""
    m = _tmpl()
    exc = _emit(tmp_path, site, _unparseable_yaml)
    assert isinstance(exc, m.TemplateError), f"raw {type(exc).__name__}"
    assert "not valid YAML" in str(exc)


@_skip_as_root
@pytest.mark.parametrize("site", sorted(_SITES))
def test_an_unreadable_document_is_a_curated_template_error(tmp_path, site):
    """Measured pre-fix: raw `PermissionError` at the two `metadata/**`
    sites, on all three interpreters (a `chmod 000` FILE is past
    `is_file()`, which only needs the containing directory searchable)."""
    m = _tmpl()
    exc = _emit(tmp_path, site, _chmod_000_file)
    assert isinstance(exc, m.TemplateError), f"raw {type(exc).__name__}"
    assert "cannot read" in str(exc)
    assert "Permission denied" in str(exc)


@_skip_as_root
@pytest.mark.parametrize("site", sorted(_SITES))
def test_an_unreadable_parent_directory_names_the_read_not_an_absence(
        tmp_path, site):
    """THE tan-cli#1127 CELL. Pre-fix, at the two pre-flight sites, this
    shape was a raw `PermissionError` on 3.12.3/3.13.15 and the curated but
    FALSE `no metadata/...` on 3.14.7 -- two different wrong answers from one
    `Path.is_file()`. Asserting `cannot read` rather than merely "raises
    `TemplateError`" is what pins the 3.14.7 half: the old message satisfied
    the weaker assertion while claiming a file that exists does not."""
    m = _tmpl()
    exc = _emit(tmp_path, site, _chmod_000_parent)
    assert isinstance(exc, m.TemplateError), f"raw {type(exc).__name__}"
    assert "cannot read" in str(exc)
    assert "no metadata/" not in str(exc)


def test_a_genuinely_absent_som_preset_keeps_its_own_message(tmp_path):
    """The one answer the deleted pre-flight got right, preserved byte for
    byte: it names the SKU being resolved, which a generic `cannot read`
    would not. `require_readable_text`'s `absent=` argument exists for this,
    and only `FileNotFoundError` reaches it."""
    m = _tmpl()
    catalog, base, metadata = _tree(tmp_path)
    (metadata / "e1m_modules" / f"{_SKU}.yaml").unlink()
    with pytest.raises(m.TemplateError) as excinfo:
        m.emit_scaffold(_TEMPLATE, _SKU, catalog_path=catalog,
                        base_dir=base, metadata_root=metadata)
    assert str(excinfo.value) == (
        f"no metadata/e1m_modules/{_SKU}.yaml for sku '{_SKU}'")


def test_a_genuinely_absent_board_document_keeps_its_own_message(tmp_path):
    """Same for the board document, which names the BOARD."""
    m = _tmpl()
    catalog, base, metadata = _tree(tmp_path)
    (metadata / "boards" / f"{_SOURCE_PRESET}.yaml").unlink()
    with pytest.raises(m.TemplateError) as excinfo:
        m.emit_scaffold(_TEMPLATE, _SKU, catalog_path=catalog,
                        base_dir=base, metadata_root=metadata)
    assert str(excinfo.value) == (
        f"no metadata/boards/{_SOURCE_PRESET}.yaml for board "
        f"'{_SOURCE_PRESET}'")
