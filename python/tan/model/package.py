# SPDX-License-Identifier: Apache-2.0
"""Assemble/parse the .alpmodel binary container (header + CBOR manifest + blobs).

`CONTAINER_VERSION` describes the BINARY FRAME below -- the 24-byte header's
field layout and the 8-byte blob-table entries -- and nothing about which keys
the CBOR manifest carries. Adding a manifest key is therefore NOT a container
change: alp-sdk's on-device reader skips keys it does not know
(`src/common/alp_model.c`, see `tan.model.manifest`'s module docstring for the
measured evidence), while a CONTAINER_VERSION it does not know is a hard
ALP_ERR_VERSION refusal for every fielded device. Do not move this constant to
announce a manifest field.
"""
from __future__ import annotations
import os
import struct
from pathlib import Path
from .manifest import Manifest

MAGIC = b"ALPM"
CONTAINER_VERSION = 1
_HEADER = struct.Struct("<4sHHIIII")   # magic, ver, flags, mft_off, mft_len, tbl_off, blob_count
_HEADER_SIZE = _HEADER.size            # 24
_TBL_ENTRY = struct.Struct("<II")      # blob off, blob len


def write_package(mft: Manifest, blobs: list[bytes]) -> bytes:
    mft_bytes = mft.to_cbor()
    mft_off = _HEADER_SIZE
    tbl_off = mft_off + len(mft_bytes)
    blobs_off = tbl_off + _TBL_ENTRY.size * len(blobs)

    table, blob_region, cursor = b"", b"", blobs_off
    for b in blobs:
        table += _TBL_ENTRY.pack(cursor, len(b))
        blob_region += b
        cursor += len(b)

    header = _HEADER.pack(MAGIC, CONTAINER_VERSION, 0,
                          mft_off, len(mft_bytes), tbl_off, len(blobs))
    return header + mft_bytes + table + blob_region


def _unpack_header(head: bytes) -> tuple[int, int, int, int]:
    """(mft_off, mft_len, tbl_off, blob_count) from a validated 24-byte header."""
    magic, ver, _flags, mft_off, mft_len, tbl_off, n = _HEADER.unpack_from(head, 0)
    if magic != MAGIC:
        raise ValueError(f"bad magic {magic!r}")
    if ver != CONTAINER_VERSION:
        raise ValueError(f"unsupported container version {ver}")
    return mft_off, mft_len, tbl_off, n


def read_manifest_file(path: Path) -> Manifest:
    """The manifest of an on-disk package, WITHOUT reading its blobs.

    `read_package` needs the whole container in memory; a caller that only
    wants to read back what a package says about itself (`tan model build`
    reporting a shipped target's caveats) would then pay a full copy of every
    compiled blob -- megabytes for a real vision model -- to read a few
    hundred bytes of CBOR. This seeks to the manifest region instead, so the
    cost is O(manifest), not O(package).

    Reading it back off disk, rather than reporting the in-memory object that
    was just written, is deliberate: it reports what the ARTIFACT says. A
    caveat lost by the encoder is then visibly missing from the build output
    instead of being narrated from a manifest that never reached the file."""
    with open(path, "rb") as f:
        head = f.read(_HEADER_SIZE)
        if len(head) < _HEADER_SIZE:
            raise ValueError(f"truncated container: {len(head)} bytes, "
                             f"header is {_HEADER_SIZE}")
        mft_off, mft_len, _tbl_off, _n = _unpack_header(head)
        # Bound the read against the real file size BEFORE issuing it.
        # `mft_off`/`mft_len` are untrusted u32s off the wire, and unlike
        # `read_package`'s slice of an already-loaded buffer, `f.read(n)`
        # allocates n bytes -- an `mft_len` of 0xFFFFFFFF would ask for 4 GiB
        # from a 5 KiB file. Compared subtraction-style, the same shape
        # alp-sdk's reader uses for the same fields (`src/common/alp_model.c`:
        # "adding the untrusted u32 offset and length first can wrap").
        size = os.fstat(f.fileno()).st_size
        if mft_off > size or mft_len > size - mft_off:
            raise ValueError(f"manifest region {mft_off}+{mft_len} runs past "
                             f"the end of a {size}-byte container")
        f.seek(mft_off)
        blob = f.read(mft_len)
    if len(blob) != mft_len:
        raise ValueError(f"truncated manifest: {len(blob)} of {mft_len} bytes")
    return Manifest.from_cbor(blob)


def read_package(raw: bytes) -> tuple[Manifest, list[bytes]]:
    mft_off, mft_len, tbl_off, n = _unpack_header(raw)
    mft = Manifest.from_cbor(raw[mft_off:mft_off + mft_len])
    blobs = []
    for i in range(n):
        off, length = _TBL_ENTRY.unpack_from(raw, tbl_off + i * _TBL_ENTRY.size)
        blobs.append(raw[off:off + length])
    return mft, blobs
