# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1133 (PR #1160 review): the YAML half of the malformed-document
register, and the import-closure property that let it move here.

`require_yaml_mapping_doc` / `read_yaml_mapping` / `require_readable_bytes`
were written as module-level functions in `tan/planner/template.py` and moved
into `tan/core/document_guards.py` on the format-symmetry condition that
module's docstring now states. Two things had to be true for the move to be
safe, and neither was asserted anywhere before this file:

1. **PyYAML stays out of this module's import closure.** The register is
   imported by `tan/core/example_catalog.py`, which `tan init`'s SDK-free
   path (invariant I-32) runs with no alp-sdk checkout bound. `import yaml`
   is therefore FUNCTION-LOCAL, the same idiom seven other `tan/core/**`
   modules use. That is now a test rather than a convention: a future edit
   that hoists the import to module scope reds here, where before it would
   have been invisible until something else broke.
2. **The curated messages are the ones the planner already produced.** The
   move must not have changed a single byte a user sees, so the message
   shapes are asserted here against the register directly, with no planner
   and no bound SDK in the way.

Driven, not reasoned: every case below writes a real file (or a real
directory, or a real unreadable mode) and reads it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tan.core.document_guards import DocumentGuards


class _Curated(Exception):
    """The injected class, standing in for `TemplateError` /
    `MalformedCatalogError`. `pytest.raises`' strict type match is the
    assertion: a raw `OSError`/`UnicodeDecodeError`/`yaml.YAMLError`
    reaching the caller fails every case here."""


@pytest.fixture
def guards() -> DocumentGuards:
    return DocumentGuards(_Curated)


_skip_as_root = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX-only, non-root: chmod 0o000 has no effect for root and "
           "Windows ACLs don't honour POSIX mode bits.",
)


# ---------------------------------------------------------------------------
# The import-closure property the move depends on.
# ---------------------------------------------------------------------------


def test_importing_the_register_does_not_import_pyyaml():
    """A SUBPROCESS, deliberately: this pytest process has `yaml` imported
    many times over, so asserting `'yaml' not in sys.modules` in-process
    would be vacuous -- it would pass whatever this module does."""
    tan_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys\n"
         "import tan.core.document_guards\n"
         "print('yaml' in sys.modules)\n"],
        capture_output=True, text=True, cwd=tan_root, check=True,
    )
    assert proc.stdout.strip() == "False", (
        "`import yaml` has been hoisted to module scope in "
        "tan/core/document_guards.py. That module is on `tan init`'s "
        "SDK-free path through tan/core/example_catalog.py; keep the import "
        "inside require_yaml_mapping_doc, the idiom som_buildability.py:109 "
        "documents."
    )


def test_the_yaml_guards_still_work_through_the_deferred_import(guards, tmp_path):
    """The other half of the same property: deferring the import must not
    have broken the methods that need it."""
    path = tmp_path / "doc.yaml"
    path.write_text("a: 1\nb: [2, 3]\n", encoding="utf-8")
    assert guards.read_yaml_mapping(path, what="doc") == {"a": 1, "b": [2, 3]}


# ---------------------------------------------------------------------------
# require_yaml_mapping_doc -- the parse + shape half.
# ---------------------------------------------------------------------------


def test_an_unparseable_document_names_the_format_and_the_position(guards):
    with pytest.raises(_Curated) as excinfo:
        guards.require_yaml_mapping_doc(
            "a: [1, 2\nb: }{\n", path="/x/doc.yaml", what="doc")
    msg = str(excinfo.value)
    assert msg.startswith("malformed doc at /x/doc.yaml: not valid YAML (")
    assert "line 2 column 2" in msg


def test_a_yaml_error_with_no_mark_degrades_to_one_line(guards):
    """A `MarkedYAMLError` carries `problem`/`problem_mark`; a plain
    `yaml.YAMLError` (e.g. an unsupported tag through a constructor) carries
    neither, and its `str()` is multi-line. The curated message must stay
    one line either way -- asserted on the real thing, not a mock."""
    with pytest.raises(_Curated) as excinfo:
        guards.require_yaml_mapping_doc(
            "!!python/object:os.system {}\n", path="/x/doc.yaml", what="doc")
    assert "\n" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("text", "typename"),
    [("- one\n- two\n", "list"), ("just-a-string\n", "str"), ("3\n", "int")],
)
def test_a_parsed_non_mapping_is_refused_by_type(guards, text, typename):
    with pytest.raises(_Curated) as excinfo:
        guards.require_yaml_mapping_doc(text, path="/x/doc.yaml", what="doc")
    assert f"expected a YAML mapping, got {typename}" in str(excinfo.value)


def test_an_empty_document_is_an_empty_mapping_not_an_error(guards):
    """`yaml.safe_load("") is None`, and every caller's pre-existing
    `... or {}` normalised that to an empty mapping. Folding the `or {}` into
    this method must not have turned a document that rendered into one that
    refuses."""
    assert guards.require_yaml_mapping_doc(
        "", path="/x/doc.yaml", what="doc") == {}
    assert guards.require_yaml_mapping_doc(
        "# just a comment\n", path="/x/doc.yaml", what="doc") == {}


# ---------------------------------------------------------------------------
# read_yaml_mapping -- the read half, and `absent`.
# ---------------------------------------------------------------------------


def test_an_absent_document_uses_the_callers_own_message(guards, tmp_path):
    with pytest.raises(_Curated) as excinfo:
        guards.read_yaml_mapping(
            tmp_path / "gone.yaml", what="doc", absent="no doc for 'X'")
    assert str(excinfo.value) == "no doc for 'X'"


def test_without_absent_a_missing_file_is_still_curated(guards, tmp_path):
    """`absent` is optional; omitting it must not put the raw
    `FileNotFoundError` back."""
    with pytest.raises(_Curated) as excinfo:
        guards.read_yaml_mapping(tmp_path / "gone.yaml", what="doc")
    assert "cannot read doc at" in str(excinfo.value)


def test_a_present_but_unreadable_document_is_not_reported_as_absent(
        guards, tmp_path):
    """The distinction `absent` exists to draw, and the one an `is_file()`
    pre-flight could not: a directory in the file's place is not "no such
    file"."""
    (tmp_path / "doc.yaml").mkdir()
    with pytest.raises(_Curated) as excinfo:
        guards.read_yaml_mapping(
            tmp_path / "doc.yaml", what="doc", absent="no doc for 'X'")
    assert "cannot read doc at" in str(excinfo.value)


def test_a_non_utf8_document_is_curated(guards, tmp_path):
    path = tmp_path / "doc.yaml"
    path.write_bytes(b"\xff\xfe\x00not-utf8")
    with pytest.raises(_Curated) as excinfo:
        guards.read_yaml_mapping(path, what="doc", absent="no doc for 'X'")
    assert "cannot read doc at" in str(excinfo.value)


# ---------------------------------------------------------------------------
# require_readable_bytes -- and why it is not the text half in disguise.
# ---------------------------------------------------------------------------


def test_crlf_survives_the_bytes_read_and_does_not_survive_the_text_read(
        guards, tmp_path):
    """The measurement behind keeping these two methods separate rather than
    defining the text half as `require_readable_bytes(...).decode()`.

    `Path.read_text` opens in TEXT mode and applies universal-newline
    translation; `read_bytes` does not. Folding one into the other would have
    silently changed what every existing text caller sees on a CRLF
    checkout."""
    path = tmp_path / "asset.bin"
    path.write_bytes(b"one\r\ntwo\r\n")
    assert guards.require_readable_bytes(path, what="asset") == b"one\r\ntwo\r\n"
    assert guards.require_readable_text(path, what="asset") == "one\ntwo\n"


def test_bytes_reads_do_not_decode_at_all(guards, tmp_path):
    """A template asset is not required to be text. The bytes half must hand
    back arbitrary bytes rather than refusing them the way the text half
    correctly does."""
    path = tmp_path / "asset.bin"
    path.write_bytes(b"\xff\xfe\x00\x01")
    assert guards.require_readable_bytes(path, what="asset") == b"\xff\xfe\x00\x01"
    with pytest.raises(_Curated):
        guards.require_readable_text(path, what="asset")


@pytest.mark.parametrize("what_breaks", ["absent", "directory"])
def test_a_bytes_read_failure_is_curated(guards, tmp_path, what_breaks):
    path = tmp_path / "asset.bin"
    if what_breaks == "directory":
        path.mkdir()
    with pytest.raises(_Curated) as excinfo:
        guards.require_readable_bytes(path, what="template source file")
    assert "cannot read template source file at" in str(excinfo.value)


@_skip_as_root
def test_an_unreadable_bytes_read_is_curated(guards, tmp_path):
    path = tmp_path / "asset.bin"
    path.write_bytes(b"data")
    original = path.stat().st_mode
    path.chmod(0o000)
    try:
        with pytest.raises(_Curated) as excinfo:
            guards.require_readable_bytes(path, what="template source file")
    finally:
        path.chmod(original)
    assert "Permission denied" in str(excinfo.value)
