# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1496: tan downloads the toolchain archive itself and hashes it against
alp-sdk's pin (west's setup.sh would fetch it unchecked). Hermetic: a real tar.xz
built in tmp_path, served by a fake opener."""
from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

from tan.commands import bootstrap_toolchain_fetch as fetch
from tan.commands import sdk_cmd
from tan.core.toolchain_provision import ToolchainArtifact


def _archive(tmp_path: Path, member="arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        data = b"stub"
        info = tarfile.TarInfo(member)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _serve(monkeypatch, payload: bytes):
    class Opener:
        def open(self, req, timeout=None):
            return _Resp(payload)

    monkeypatch.setattr(sdk_cmd, "_releases_opener", lambda proxy: Opener())
    monkeypatch.setattr(fetch, "select_https_proxy", lambda url: None)


def _art(payload: bytes, sha=None, size=None) -> ToolchainArtifact:
    return ToolchainArtifact(
        "linux-x86_64", fetch.TOOLCHAIN_ARTIFACT_COMPONENT, "t.tar.xz",
        size if size is not None else len(payload),
        sha or hashlib.sha256(payload).hexdigest(),
    )


def test_a_matching_archive_is_extracted_into_the_setup_sh_layout(tmp_path, monkeypatch):
    payload = _archive(tmp_path)
    _serve(monkeypatch, payload)
    tmp_dir = tmp_path / "root" / "leaf.tmp-1"
    (tmp_path / "root").mkdir()
    out = fetch.install_pinned_toolchain(
        "https://example.invalid/", _art(payload), tmp_dir, tmp_path / "root", "leaf"
    )
    assert out.kind == "ok", out
    assert (tmp_dir / "gnu" / "arm-zephyr-eabi" / "bin" / "arm-zephyr-eabi-gcc").is_file()
    assert [p.name for p in (tmp_path / "root").iterdir()] == ["leaf.tmp-1"]  # scratch gone


def test_a_swapped_archive_is_a_mismatch_and_nothing_is_extracted(tmp_path, monkeypatch):
    good = _archive(tmp_path)
    evil = _archive(tmp_path, member="arm-zephyr-eabi/evil")
    _serve(monkeypatch, evil)
    (tmp_path / "root").mkdir()
    tmp_dir = tmp_path / "root" / "leaf.tmp-1"
    out = fetch.install_pinned_toolchain(
        "https://example.invalid/", _art(good), tmp_dir, tmp_path / "root", "leaf"
    )
    assert out.kind == "mismatch"
    assert not (tmp_dir / "gnu").exists()


def test_an_oversized_body_is_refused_without_buffering_it_all(tmp_path, monkeypatch):
    payload = _archive(tmp_path)
    _serve(monkeypatch, payload)
    (tmp_path / "root").mkdir()
    out = fetch.install_pinned_toolchain(
        "https://example.invalid/", _art(payload, size=3), tmp_path / "root" / "t", tmp_path / "root", "leaf"
    )
    assert out.kind == "mismatch" and "larger" in out.message


def test_a_transport_error_is_unverified(tmp_path, monkeypatch):
    class Boom:
        def open(self, *a, **k):
            raise OSError("no route")

    monkeypatch.setattr(sdk_cmd, "_releases_opener", lambda proxy: Boom())
    monkeypatch.setattr(fetch, "select_https_proxy", lambda url: None)
    (tmp_path / "root").mkdir()
    out = fetch.install_pinned_toolchain(
        "https://example.invalid/", _art(b"x"), tmp_path / "root" / "t", tmp_path / "root", "leaf"
    )
    assert out.kind == "unverified" and "no route" in out.message


def test_an_archive_without_the_toolchain_directory_is_an_install_failure(tmp_path, monkeypatch):
    payload = _archive(tmp_path, member="other/file")
    _serve(monkeypatch, payload)
    (tmp_path / "root").mkdir()
    out = fetch.install_pinned_toolchain(
        "https://example.invalid/", _art(payload), tmp_path / "root" / "t", tmp_path / "root", "leaf"
    )
    assert out.kind == "install"
