# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1133: the THIRD shape of the tan-cli#1116 class -- no `try` at all.

`tests/gates/test_never_raises_contract_holds.py` now seeds
`template._load_som_doc` and `template._board_route_entries` and drives each
directly. This file drives the same defect the way a user meets it: through
the real `emit_scaffold`, which `tan/planner/cli.py:187-190` wraps with
`except TemplateError` **and nothing else**, so anything else reaches the
user as a bare traceback.

WHAT WAS MEASURED, on this tree, before the fix. Non-root, one line per
cell, and the interpreter column matters -- three of these differ by it:

    site                    shape                escaped as
    _load_som_doc           non-UTF-8 byte       UnicodeDecodeError
    _load_som_doc           malformed YAML       yaml.parser.ParserError
    _load_som_doc           chmod 000 file       PermissionError
    _load_som_doc           chmod 000 PARENT     PermissionError on 3.12.3
                                                 and 3.13.15; on 3.14.7 a
                                                 curated but FALSE
                                                 `no metadata/...`
    _board_route_entries    (the same four, identical, one document over)
    render_to_envelope      malformed YAML       yaml.parser.ParserError
    _rendered_bytes         chmod 000 file       PermissionError
    _rendered_bytes         deleted source       FileNotFoundError
    _rendered_bytes         directory in place   IsADirectoryError
    _rendered_bytes         non-UTF-8 + subst.   UnicodeDecodeError
    _safe_join              symlink loop         RuntimeError on 3.12.3
                                                 only; 3.13.15 and 3.14.7
                                                 resolve it and fail at the
                                                 read instead

Over the twelve cells the first three sites share (3 sites x 4 shapes):
**9 of 12 raw on 3.12.3 and on 3.13.15**, and **7 of 12 raw plus 2
curated-but-FALSE on 3.14.7**. After the fix: 12 of 12 curated, with
byte-identical messages across all three interpreters.

`render_to_envelope`'s row is the one the issue did not name and this file
found by sweeping the module: tan-cli#1116 fixed its READ
(`board_yaml_path.read_text`) and left the `yaml.safe_load` one line below
it bare, so a template example `board.yaml` that decoded as UTF-8 perfectly
well but did not parse still escaped -- the same "proximity to a fixed
defect is not coverage" the issue makes about the two sites next door, one
line further in.

`_rendered_bytes` and `_safe_join` came from PR #1160's review, which found
the first of them after this file's first version had already filed it as
"a candidate to read" rather than driving it. `_rendered_bytes` is the
highest-traffic read of the six: every catalog template lists 5-8
`files.user_owned` entries and 4-7 of them come through it per scaffold.
`_safe_join`'s is the same interpreter-divergence family as the #1127 cell
below, one `pathlib` method over.

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


#: The template source file. Every shipped catalog template lists 5-8
#: `files.user_owned` entries, so this is the ordinary case, not an exotic
#: one -- `board.yaml` was merely the only entry that happened to be shielded
#: by a curated read of the same file upstream.
_SOURCE_REL = "src/main.c"
_GOOD_SOURCE = "/* @@NAME@@ */\nint main(void) { return 0; }\n"


def _tree(tmp_path, *, substitute=False):
    """A synthetic (catalog, base_dir, metadata_root) triple that renders
    cleanly -- every case below breaks exactly one file in it.

    @substitute declares a `substitute:` parameter against `src/main.c`.
    `_substitutions_for` only applies one when the RESOLVED value differs
    from the declared default, so the caller must also pass `{"name":
    "other"}`; `_emit_source` below does. That branch is what reaches
    `_rendered_bytes`' own `.decode("utf-8")`, as opposed to the envelope's.
    """
    root = tmp_path / "scaffold"
    base = root / "sdk"
    example = base / _EXAMPLE
    (example / "src").mkdir(parents=True)
    (example / "board.yaml").write_text(_GOOD_BOARD_YAML, encoding="utf-8")
    (example / _SOURCE_REL).write_text(_GOOD_SOURCE, encoding="utf-8")

    record = {
        "id": _TEMPLATE,
        "example": _EXAMPLE,
        "supported": {"som_skus": [_SKU]},
        "files": {"user_owned": ["board.yaml", _SOURCE_REL]},
        "cores": [],
    }
    if substitute:
        record["parameters"] = [{
            "name": "name", "type": "string", "default": "demo",
            "substitute": {"file": _SOURCE_REL, "literal": "@@NAME@@"},
        }]
    catalog = root / "catalog-v1.json"
    catalog.write_text(json.dumps({"templates": [record]}), encoding="utf-8")

    metadata = root / "metadata"
    (metadata / "e1m_modules").mkdir(parents=True)
    (metadata / "e1m_modules" / f"{_SKU}.yaml").write_text(
        _GOOD_SOM, encoding="utf-8")
    (metadata / "boards").mkdir(parents=True)
    for name in (_SOURCE_PRESET, "target-board"):
        (metadata / "boards" / f"{name}.yaml").write_text(
            _GOOD_ROUTES, encoding="utf-8")
    return catalog, base, metadata


#: `{site: how to reach its one document from (base, metadata)}`. All FOUR
#: are read by ONE `emit_scaffold` call, which is the whole point: the issue's
#: "one call away from the read #1116 fixed, in the same invocation".
#:
#: `template-source` (PR #1160 review, MAJOR 1) is the fourth, and it is not
#: a DOCUMENT read -- `_rendered_bytes` copies bytes and parses nothing, so
#: it takes part in the read-failure cases below and not the YAML ones. Which
#: cases a site belongs to is spelled out per test rather than assumed.
_SITES = {
    "som-preset": lambda base, metadata: (
        metadata / "e1m_modules" / f"{_SKU}.yaml"),
    "board-metadata": lambda base, metadata: (
        metadata / "boards" / f"{_SOURCE_PRESET}.yaml"),
    "template-example-board": lambda base, metadata: (
        base / _EXAMPLE / "board.yaml"),
    "template-source": lambda base, metadata: base / _EXAMPLE / _SOURCE_REL,
}

#: The three that are parsed as YAML. `template-source` is not one of them.
_DOCUMENT_SITES = tuple(s for s in _SITES if s != "template-source")


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
    assert [entry["path"] for entry in json.loads(out)] == [
        "board.yaml", _SOURCE_REL]


@pytest.mark.parametrize("site", sorted(_DOCUMENT_SITES))
def test_a_non_utf8_document_is_a_curated_template_error(tmp_path, site):
    """Measured pre-fix: raw `UnicodeDecodeError` out of `emit_scaffold` at
    the two `metadata/**` sites (the example `board.yaml` was already fixed
    by tan-cli#1116, and is asserted here so a regression there reds too).

    `template-source` is excluded on purpose and covered separately below:
    non-UTF-8 bytes are LEGAL there."""
    m = _tmpl()
    exc = _emit(tmp_path, site, _non_utf8)
    assert isinstance(exc, m.TemplateError), f"raw {type(exc).__name__}"
    assert "cannot read" in str(exc)


@pytest.mark.parametrize("site", sorted(_DOCUMENT_SITES))
def test_an_unparseable_yaml_document_is_a_curated_template_error(
        tmp_path, site):
    """Measured pre-fix: raw `yaml.parser.ParserError` at ALL THREE document
    sites -- `yaml.YAMLError` is neither an `OSError` nor a `ValueError`, so
    no `except` clause anywhere on this path had ever covered it."""
    m = _tmpl()
    exc = _emit(tmp_path, site, _unparseable_yaml)
    assert isinstance(exc, m.TemplateError), f"raw {type(exc).__name__}"
    assert "not valid YAML" in str(exc)


@_skip_as_root
@pytest.mark.parametrize("site", sorted(_SITES))
def test_an_unreadable_document_is_a_curated_template_error(tmp_path, site):
    """Measured pre-fix: raw `PermissionError` at the two `metadata/**`
    sites and at `template-source`, on all three interpreters (a `chmod 000`
    FILE is past `is_file()`, which only needs the containing directory
    searchable)."""
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


# ---------------------------------------------------------------------
# `template-source`'s own shapes (PR #1160 review, MAJOR 1). Its contract
# differs from the three document sites in both directions, so neither is
# assumed from the other.
# ---------------------------------------------------------------------


def _emit_source(tmp_path, break_it, *, substitute=False):
    m = _tmpl()
    catalog, base, metadata = _tree(tmp_path, substitute=substitute)
    with break_it(base / _EXAMPLE / _SOURCE_REL):
        try:
            m.emit_scaffold(
                _TEMPLATE, _SKU, {"name": "other"} if substitute else None,
                catalog_path=catalog, base_dir=base, metadata_root=metadata)
        except BaseException as exc:  # noqa: BLE001 -- the class IS the assertion
            return exc
    return None


@contextlib.contextmanager
def _deleted(path):
    path.unlink()
    yield


@contextlib.contextmanager
def _replaced_by_a_directory(path):
    path.unlink()
    path.mkdir()
    yield


@pytest.mark.parametrize(
    ("shape", "break_it"),
    [("deleted", _deleted), ("directory", _replaced_by_a_directory)],
)
def test_a_broken_template_source_is_a_curated_template_error(
        tmp_path, shape, break_it):
    """Measured pre-fix on 3.12.3, 3.13.15 and 3.14.7 alike: raw
    `FileNotFoundError` and raw `IsADirectoryError` respectively, straight
    out of `emit_scaffold`."""
    m = _tmpl()
    exc = _emit_source(tmp_path, break_it)
    assert isinstance(exc, m.TemplateError), f"raw {type(exc).__name__}"
    assert "cannot read template source file at" in str(exc)


def test_a_non_utf8_template_source_is_refused_by_the_envelope_not_the_read(
        tmp_path):
    """The read must NOT have narrowed what a template may ship. A template
    asset is not required to be text -- `render()` writes back whatever it
    read -- so the refusal here belongs to the JSON envelope, which cannot
    encode arbitrary bytes, and it says so. This is the case that stops the
    #1160 fix quietly turning a byte-copy into a UTF-8 requirement."""
    m = _tmpl()
    exc = _emit_source(tmp_path, _non_utf8)
    assert isinstance(exc, m.TemplateError), f"raw {type(exc).__name__}"
    assert "cannot be JSON-encoded" in str(exc)
    assert "cannot read" not in str(exc)


def test_a_non_utf8_source_with_a_substitution_is_curated_too(tmp_path):
    """The FIFTH escape, one line inside the fourth: `_rendered_bytes`' own
    `.decode("utf-8")`, reached only when a `substitute:` applies. Measured
    pre-fix as a raw `UnicodeDecodeError` on all three interpreters -- and
    note the ASYMMETRY it removed: the identical bytes were curated when no
    substitution applied (the case above) and raw when one did.

    LATENT, not live, and the distinction is worth stating as precisely as
    the fourth site's "4-7 files per scaffold": measured against alp-sdk
    `ff27f179`, **0 of the 8 parameters** across the catalog's 9 templates
    carry `substitute:`, and `$defs/parameter` in
    `template-catalog-v1.schema.json` is `additionalProperties: false` over
    exactly `constraints`/`default`/`description`/`name`/`type`, so a
    catalog declaring one does not validate. This case therefore builds its
    own catalog rather than using a shipped one. Guarded anyway: a
    hand-edited catalog is the same input class every other guard in this
    module exists for, and an asymmetry like this outlives the reason for
    it."""
    m = _tmpl()
    exc = _emit_source(tmp_path, _non_utf8, substitute=True)
    assert isinstance(exc, m.TemplateError), f"raw {type(exc).__name__}"
    assert "cannot have substitutions applied" in str(exc)


def test_the_substituting_control_still_renders(tmp_path):
    """The control for the case above: with a readable UTF-8 source, the
    substitution really does apply -- otherwise that test would pass
    vacuously against a branch it never entered."""
    m = _tmpl()
    catalog, base, metadata = _tree(tmp_path, substitute=True)
    out = json.loads(m.emit_scaffold(
        _TEMPLATE, _SKU, {"name": "other"}, catalog_path=catalog,
        base_dir=base, metadata_root=metadata))
    source = next(e for e in out if e["path"] == _SOURCE_REL)
    assert "/* other */" in source["contents"]


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
