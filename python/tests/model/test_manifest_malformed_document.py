# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1023: `Manifest.from_json` (manifest.py:163-166) and
`Manifest.from_cbor` (manifest.py:171-181, reached via BOTH
`package.read_manifest_file` and `package.read_package`) bare-subscripted a
decoded `.alpmodel` manifest document with no `isinstance(d, dict)` guard --
the same defect class tan-cli#1018 fixed for `resolve_targets()`'s
`preset["silicon"]` read.

Measured on the unguarded code, verbatim (the issue's own repro table):

    Manifest.from_json("[1,2,3]")        -> TypeError: list indices must be
                                             integers or slices, not str
    Manifest.from_json('"just a string"') -> TypeError: string indices must
                                             be integers, not 'str'
    Manifest.from_cbor(cbor2.dumps([1,2,3]))       -> TypeError: list
                                             indices must be integers or
                                             slices, not str
    Manifest.from_cbor(cbor2.dumps("just a string")) -> TypeError: string
                                             indices must be integers, not
                                             'str'

Mirrors `test_targets_malformed_preset.py`'s assertion style for the sibling
`preset` guard -- same fixture shape (a bare list, a bare scalar), same
"clean ValueError naming the actual type, not a raw TypeError" bar."""
import struct

import cbor2
import pytest

from tan.model.manifest import Manifest
from tan.model.package import read_manifest_file, read_package

_HEADER = struct.Struct("<4sHHIIII")   # magic, ver, flags, mft_off, mft_len, tbl_off, blob_count


def _raw_container(mft_bytes: bytes) -> bytes:
    """A syntactically valid `.alpmodel` container (correct magic, version,
    offsets) whose manifest region is @mft_bytes VERBATIM -- for exercising a
    manifest that decodes but doesn't parse to a CBOR map, the shape neither
    `read_manifest_file` nor `read_package` rejects before reaching
    `Manifest.from_cbor`."""
    mft_off = _HEADER.size
    header = _HEADER.pack(b"ALPM", 1, 0, mft_off, len(mft_bytes), mft_off + len(mft_bytes), 0)
    return header + mft_bytes


# ---------------------------------------------------------------------------
# Manifest.from_json
# ---------------------------------------------------------------------------


def test_a_bare_list_json_manifest_raises_a_clean_valueerror_not_a_typeerror():
    """A JSON document that decodes but is a bare list (legal JSON, illegal
    manifest) must raise a curated error, not an uncaught `TypeError: list
    indices must be integers or slices, not str` from the bare
    `d["src_sha"]` subscript at manifest.py:165."""
    with pytest.raises(ValueError, match="expected a JSON object"):
        Manifest.from_json("[1, 2, 3]")


def test_a_bare_scalar_json_manifest_raises_a_clean_valueerror_not_a_typeerror():
    with pytest.raises(ValueError, match="expected a JSON object"):
        Manifest.from_json('"just a string"')


def test_the_json_valueerror_names_the_actual_type():
    with pytest.raises(ValueError, match=r"expected a JSON object, got list"):
        Manifest.from_json("[1, 2, 3]")
    with pytest.raises(ValueError, match=r"expected a JSON object, got str"):
        Manifest.from_json('"just a string"')


# ---------------------------------------------------------------------------
# Manifest.from_cbor -- direct
# ---------------------------------------------------------------------------


def test_a_bare_list_cbor_manifest_raises_a_clean_valueerror_not_a_typeerror():
    """Same guard, the CBOR entry point: a bare list must not reach the
    bare `d["name"]` subscript at manifest.py:175."""
    with pytest.raises(ValueError, match="expected a CBOR map"):
        Manifest.from_cbor(cbor2.dumps([1, 2, 3]))


def test_a_bare_scalar_cbor_manifest_raises_a_clean_valueerror_not_a_typeerror():
    with pytest.raises(ValueError, match="expected a CBOR map"):
        Manifest.from_cbor(cbor2.dumps("just a string"))


def test_the_cbor_valueerror_names_the_actual_type():
    with pytest.raises(ValueError, match=r"expected a CBOR map, got list"):
        Manifest.from_cbor(cbor2.dumps([1, 2, 3]))
    with pytest.raises(ValueError, match=r"expected a CBOR map, got str"):
        Manifest.from_cbor(cbor2.dumps("just a string"))


# ---------------------------------------------------------------------------
# Manifest.from_cbor -- reached through package.py's two call sites
# (package.py:88 `read_manifest_file`, package.py:93 `read_package`), the
# sites the issue names alongside manifest.py itself.
# ---------------------------------------------------------------------------


def test_read_manifest_file_surfaces_the_curated_error_not_a_typeerror(tmp_path):
    raw = _raw_container(cbor2.dumps([1, 2, 3]))
    path = tmp_path / "corrupt.alpmodel"
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="expected a CBOR map"):
        read_manifest_file(path)


def test_read_package_surfaces_the_curated_error_not_a_typeerror():
    raw = _raw_container(cbor2.dumps("just a string"))
    with pytest.raises(ValueError, match="expected a CBOR map"):
        read_package(raw)
