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


def stage(entry, zoo, tmp_path, **kw):
    d = tmp_path / "dest"
    d.mkdir(exist_ok=True)
    return fetch_source(entry, zoo, tmp_dir=d, **kw), d


def test_fetch_bundled_and_url_verified(zoo, tmp_path):
    entries, _ = load_zoo(zoo)
    by = {e.id: e for e in entries}
    staged, _ = stage(by["a-tiny"], zoo, tmp_path)
    assert staged.path.read_bytes() == b"starter" and staged.size == 7
    staged, _ = stage(by["b-model"], zoo, tmp_path, reader=lambda url: iter([URL_BYTES[:5], URL_BYTES[5:]]))
    assert staged.path.read_bytes() == URL_BYTES and staged.sha256 == URL_SHA


def test_fetch_hash_mismatch_is_flagged_and_leaves_no_temp(zoo, tmp_path):
    entries, _ = load_zoo(zoo)
    entry = next(e for e in entries if e.id == "b-model")
    with pytest.raises(ZooFetchError) as err:
        stage(entry, zoo, tmp_path, reader=lambda url: iter([b"tampered"]))
    assert err.value.mismatch
    assert list((tmp_path / "dest").iterdir()) == []


def test_fetch_enforces_size_cap_and_deadline(zoo, tmp_path, monkeypatch):
    entries, _ = load_zoo(zoo)
    entry = next(e for e in entries if e.id == "b-model")
    monkeypatch.setattr("tan.core.model_zoo.MAX_DOWNLOAD_BYTES", 4)
    with pytest.raises(ZooFetchError, match="larger than"):
        stage(entry, zoo, tmp_path, reader=lambda url: iter([b"12345678"]))
    monkeypatch.undo()
    with pytest.raises(ZooFetchError, match="not complete within"):
        stage(entry, zoo, tmp_path, reader=lambda url: iter([b"x", b"y"]), max_seconds=-1)
    assert list((tmp_path / "dest").iterdir()) == []


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
        stage(next(e for e in entries if e.id == "a-tiny"), zoo, tmp_path)


@pytest.mark.parametrize(
    "extra",
    [
        "compile: [1, 2]\n",
        "compile:\n  drpai: 3\n",
        "compile:\n  drpai:\n    input_shape: {a: 1}\n",
        "compile:\n  drpai: &a\n    x: *a\n",
        "compile:\n  drpai: {}\n",
    ],
)
def test_bad_compile_shape_is_an_invalid_entry(zoo, extra):
    (zoo / "c-bad.yaml").write_text(manifest("c-bad", extra=extra), encoding="utf-8")
    entries, problems = load_zoo(zoo)
    assert "c-bad" not in [e.id for e in entries]
    assert [p[0] for p in problems] == ["c-bad.yaml"]


def test_good_compile_shape_loads(zoo):
    extra = "compile:\n  drpai:\n    input_shape: [1, 3, 224, 224]\n    input_name: images\n"
    (zoo / "c-ok.yaml").write_text(manifest("c-ok", extra=extra), encoding="utf-8")
    entries, problems = load_zoo(zoo)
    assert problems == [] and "c-ok" in [e.id for e in entries]


# --- iter_url against a local server (the https-only rule is the manifest's;
# --- `_schemes` lets the test speak plain http to 127.0.0.1) ---------------

import gzip  # noqa: E402
import http.server  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from tan.core import model_zoo  # noqa: E402


class _Server:
    def __init__(self, handler):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/m.tflite"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def serve():
    servers = []

    def make(fn):
        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                fn(self)

            def log_message(self, *a):
                pass

        servers.append(_Server(H))
        return servers[-1].url

    yield make
    for s in servers:
        s.close()


def drain(url, **kw):
    return b"".join(model_zoo.iter_url(url, _schemes=("http://",), **kw))


def staged_via(url, zoo, tmp_path, **kw):
    entries, _ = load_zoo(zoo)
    entry = next(e for e in entries if e.id == "b-model")
    return stage(entry, zoo, tmp_path, reader=lambda u: model_zoo.iter_url(url, _schemes=("http://",)), **kw)


def test_chunked_body_without_length_is_capped(serve, zoo, tmp_path, monkeypatch):
    def handler(h):
        h.send_response(200)
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        try:
            for _ in range(200):
                h.wfile.write(b"400\r\n" + b"x" * 0x400 + b"\r\n")
            h.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass

    url = serve(handler)
    monkeypatch.setattr(model_zoo, "MAX_DOWNLOAD_BYTES", 4096)
    with pytest.raises(ZooFetchError, match="larger than"):
        staged_via(url, zoo, tmp_path)
    assert list((tmp_path / "dest").iterdir()) == []


def test_lying_content_length_cannot_exceed_the_cap(serve, zoo, tmp_path, monkeypatch):
    def handler(h):
        h.send_response(200)
        h.send_header("Content-Length", "10")  # claims small, sends a lot
        h.end_headers()
        try:
            h.wfile.write(b"y" * 100000)
        except OSError:
            pass

    url = serve(handler)
    got = drain(url)
    assert len(got) <= 10  # the client never reads past what it was told
    monkeypatch.setattr(model_zoo, "MAX_DOWNLOAD_BYTES", 5)
    with pytest.raises(ZooFetchError):
        staged_via(url, zoo, tmp_path)


def test_oversized_content_length_is_refused_before_reading(serve):
    def handler(h):
        h.send_response(200)
        h.send_header("Content-Length", str(model_zoo.MAX_DOWNLOAD_BYTES + 1))
        h.end_headers()

    with pytest.raises(ZooFetchError, match="Content-Length"):
        drain(serve(handler))


def test_gzip_encoded_response_is_refused(serve):
    body = gzip.compress(b"z" * 1000)

    def handler(h):
        h.send_response(200)
        h.send_header("Content-Encoding", "gzip")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    with pytest.raises(ZooFetchError, match="Content-Encoding"):
        drain(serve(handler))


def test_slow_drip_exceeds_the_total_deadline(serve):
    def handler(h):
        h.send_response(200)
        h.send_header("Content-Length", "1000")
        h.end_headers()
        try:
            for _ in range(1000):
                h.wfile.write(b"d")
                h.wfile.flush()
                time.sleep(0.05)
        except OSError:
            pass

    started = time.monotonic()
    with pytest.raises(ZooFetchError):
        drain(serve(handler), max_seconds=0.5)
    assert time.monotonic() - started < 5


def test_redirect_loop_is_capped(serve):
    def handler(h):
        h.send_response(302)
        h.send_header("Location", h.path + "x")
        h.send_header("Content-Length", "0")
        h.end_headers()

    with pytest.raises(ZooFetchError):
        drain(serve(handler))


def test_redirect_to_plain_http_is_refused_by_default(serve):
    def handler(h):
        h.send_response(302)
        h.send_header("Location", "http://127.0.0.1:1/x")
        h.send_header("Content-Length", "0")
        h.end_headers()

    url = serve(handler)
    with pytest.raises(ZooFetchError, match="non-https"):
        b"".join(model_zoo.iter_url(url))  # default _schemes: https only -> http start URL is fine, redirect is not
