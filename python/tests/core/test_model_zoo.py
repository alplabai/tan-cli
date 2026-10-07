# SPDX-License-Identifier: Apache-2.0
"""`tan.core.model_zoo` (tan-cli#1286)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tan.core.model_zoo import (
    ZooFetchError,
    ZooUnavailable,
    entry_as_dict,
    fetch_source,
    filter_by_sku,
    load_zoo,
)

URL_BYTES = b"upstream-model"
URL_SHA = hashlib.sha256(URL_BYTES).hexdigest()


def manifest(id_: str, *, kind="model", source=None, soms=("E1M-AEN801",), extra="") -> str:
    source = source or f"  url: https://example.com/{id_}.tflite\n  sha256: {URL_SHA}\n"
    soms_s = "[" + ", ".join(soms) + "]"
    task = "smoke" if kind == "fixture" else "object-detection"
    return (
        f"schema_version: 1\nkind: {kind}\nid: {id_}\ntask: {task}\ndescription: d {id_}\n"
        f"license: MIT\nsource:\n{source}validated_soms: {soms_s}\n{extra}"
    )


@pytest.fixture
def zoo(tmp_path: Path) -> Path:
    d = tmp_path / "model_zoo"
    (d / "starters").mkdir(parents=True)
    (d / "starters" / "t.tflite").write_bytes(b"starter")
    (d / "b-model.yaml").write_text(manifest("b-model"), encoding="utf-8")
    (d / "a-tiny.yaml").write_text(
        manifest("a-tiny", kind="fixture", source="  bundled: starters/t.tflite\n", soms=()),
        encoding="utf-8",
    )
    return d


def test_load_sorted_and_filter_by_sku(zoo):
    entries, problems = load_zoo(zoo)
    assert problems == []
    assert [e.id for e in entries] == ["a-tiny", "b-model"]
    assert [e.id for e in filter_by_sku(entries, "E1M-AEN801")] == ["b-model"]
    assert filter_by_sku(entries, "E1M-V2N101") == []  # empty validated_soms never matches


def test_unknown_fields_are_tolerated_and_row_shape(zoo):
    (zoo / "c-x.yaml").write_text(manifest("c-x", extra="future_field: 1\n"), encoding="utf-8")
    entries, problems = load_zoo(zoo)
    assert problems == []
    row = entry_as_dict(next(e for e in entries if e.id == "c-x"))
    assert row["source"] == {"url": "https://example.com/c-x.tflite", "sha256": URL_SHA}
    assert row["validatedSoms"] == ["E1M-AEN801"]


@pytest.mark.parametrize(
    "name,text",
    [
        ("bad-id.yaml", manifest("other")),
        ("http.yaml", manifest("http", source=f"  url: http://x.io/m.tflite\n  sha256: {URL_SHA}\n")),
        ("nohash.yaml", manifest("nohash", source="  url: https://x.io/m.tflite\n")),
        ("esc.yaml", manifest("esc", source="  bundled: starters/../x.tflite\n")),
        ("ext.yaml", manifest("ext", source=f"  url: https://x.io/m.zip\n  sha256: {URL_SHA}\n")),
        ("ver.yaml", manifest("ver").replace("schema_version: 1", "schema_version: 2")),
        ("junk.yaml", "- not a mapping\n"),
    ],
)
def test_invalid_manifest_is_skipped_with_a_problem(zoo, name, text):
    (zoo / name).write_text(text, encoding="utf-8")
    entries, problems = load_zoo(zoo)
    assert [e.id for e in entries] == ["a-tiny", "b-model"]
    assert [p[0] for p in problems] == [name]


def test_missing_directory_is_unavailable(tmp_path):
    with pytest.raises(ZooUnavailable):
        load_zoo(tmp_path / "nope")


def test_fetch_bundled_and_url_verified(zoo):
    entries, _ = load_zoo(zoo)
    by = {e.id: e for e in entries}
    assert fetch_source(by["a-tiny"], zoo) == b"starter"
    assert fetch_source(by["b-model"], zoo, reader=lambda url: URL_BYTES) == URL_BYTES


def test_fetch_hash_mismatch_is_flagged(zoo):
    entries, _ = load_zoo(zoo)
    entry = next(e for e in entries if e.id == "b-model")
    with pytest.raises(ZooFetchError) as err:
        fetch_source(entry, zoo, reader=lambda url: b"tampered")
    assert err.value.mismatch


def test_bundled_symlink_escape_is_refused(zoo, tmp_path):
    outside = tmp_path / "secret.tflite"
    outside.write_bytes(b"x")
    (zoo / "starters" / "t.tflite").unlink()
    try:
        (zoo / "starters" / "t.tflite").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    entries, _ = load_zoo(zoo)
    with pytest.raises(ZooFetchError):
        fetch_source(next(e for e in entries if e.id == "a-tiny"), zoo)
