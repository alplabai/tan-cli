# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1556: tan installs the pinned Zephyr SDK minimal bundle itself
instead of running `west sdk install`. `west`'s own `download_and_extract`
(`scripts/west_commands/sdk.py`) is the shape reproduced here: download,
hash -- against alp-sdk's pin, not the release sum -- extract into a scratch
dir, require exactly one top-level directory, move it into place.

Hermetic: real `.tar.xz` archives built in `tmp_path`, served by a fake opener.
"""
from __future__ import annotations

import hashlib
import io
import tarfile
import urllib.error
from pathlib import Path

from tan.commands import bootstrap_toolchain_fetch as fetch
from tan.commands import sdk_cmd
from tan.core.toolchain_provision import ToolchainArtifact

MEMBERS = (
    "zephyr-sdk-1.0.1/sdk_version",
    "zephyr-sdk-1.0.1/cmake/Zephyr-sdkConfig.cmake",
    "zephyr-sdk-1.0.1/setup.sh",
)


def _bundle(members=MEMBERS) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        for member in members:
            data = b"1.0.1\n" if member.endswith("sdk_version") else b"stub"
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


def _raise(monkeypatch, err: BaseException):
    class Opener:
        def open(self, req, timeout=None):
            raise err

    monkeypatch.setattr(sdk_cmd, "_releases_opener", lambda proxy: Opener())
    monkeypatch.setattr(fetch, "select_https_proxy", lambda url: None)


def _art(payload: bytes, sha=None) -> ToolchainArtifact:
    return ToolchainArtifact(
        "linux-x86_64", fetch.MINIMAL_SDK_COMPONENT, "zephyr-sdk-1.0.1_linux-x86_64_minimal.tar.xz",
        len(payload), sha or hashlib.sha256(payload).hexdigest(),
    )


def _install(tmp_path: Path, art: ToolchainArtifact) -> tuple[fetch.FetchOutcome, Path, Path]:
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    tmp_dir = root / "leaf.tmp-1"
    out = fetch.install_pinned_minimal_sdk("https://example.invalid/", art, tmp_dir, root, "leaf")
    return out, root, tmp_dir


def test_the_bundles_single_top_dir_becomes_the_install_dir(tmp_path, monkeypatch):
    payload = _bundle()
    _serve(monkeypatch, payload)

    out, root, tmp_dir = _install(tmp_path, _art(payload))

    assert out.kind == "ok", out
    assert (tmp_dir / "sdk_version").read_text(encoding="utf-8") == "1.0.1\n"
    assert (tmp_dir / "cmake" / "Zephyr-sdkConfig.cmake").is_file()
    assert [p.name for p in root.iterdir()] == ["leaf.tmp-1"]  # scratch gone


def test_a_swapped_bundle_is_a_mismatch_and_nothing_is_placed(tmp_path, monkeypatch):
    good = _bundle()
    _serve(monkeypatch, good[:-1] + bytes([good[-1] ^ 1]))

    out, root, tmp_dir = _install(tmp_path, _art(good))

    assert out.kind == "mismatch"
    assert not tmp_dir.exists()
    assert list(root.iterdir()) == []


def test_a_bundle_with_two_top_level_dirs_is_refused(tmp_path, monkeypatch):
    payload = _bundle(("a/sdk_version", "b/sdk_version"))
    _serve(monkeypatch, payload)

    out, _, tmp_dir = _install(tmp_path, _art(payload))

    assert out.kind == "install" and "exactly one" in out.message
    assert not tmp_dir.exists()


def test_a_bundle_with_no_top_level_dir_is_refused(tmp_path, monkeypatch):
    payload = _bundle(("sdk_version",))
    _serve(monkeypatch, payload)

    out, _, tmp_dir = _install(tmp_path, _art(payload))

    assert out.kind == "install"
    assert not tmp_dir.exists()


def test_an_existing_install_dir_is_never_merged_into(tmp_path, monkeypatch):
    payload = _bundle()
    _serve(monkeypatch, payload)
    (tmp_path / "root" / "leaf.tmp-1").mkdir(parents=True)

    out, _, _ = _install(tmp_path, _art(payload))

    assert out.kind == "install" and "already exists" in out.message


def test_a_missing_release_asset_is_reported_as_missing_not_as_a_proxy_problem(
    tmp_path, monkeypatch
):
    _raise(monkeypatch, urllib.error.HTTPError("u", 404, "Not Found", {}, None))

    out, _, _ = _install(tmp_path, _art(b"x"))

    assert out.kind == "missing"
    assert "HTTP 404" in out.message


def test_a_throttled_download_names_its_http_status(tmp_path, monkeypatch):
    _raise(monkeypatch, urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None))

    out, _, _ = _install(tmp_path, _art(b"x"))

    assert out.kind == "unverified"
    assert "HTTP 429" in out.message


def test_a_download_past_its_deadline_is_abandoned(tmp_path, monkeypatch):
    payload = _bundle()
    _serve(monkeypatch, payload)
    clock = iter([0.0] + [fetch.DOWNLOAD_DEADLINE_S + 1.0] * 1000)
    monkeypatch.setattr(fetch, "_now", lambda: next(clock))

    out, _, tmp_dir = _install(tmp_path, _art(payload))

    assert out.kind == "unverified" and "did not finish within" in out.message
    assert not tmp_dir.exists()


def test_a_rename_refused_while_a_scanner_holds_the_tree_is_retried(tmp_path, monkeypatch):
    """Windows: a just-extracted tree can be locked by Defender for a moment,
    and `os.replace` then fails with WinError 5 (PermissionError)."""
    payload = _bundle()
    _serve(monkeypatch, payload)
    real_replace = fetch._replace
    calls = []

    def flaky_replace(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(fetch, "_replace", flaky_replace)
    monkeypatch.setattr(fetch, "_sleep", lambda s: None)

    out, _, tmp_dir = _install(tmp_path, _art(payload))

    assert out.kind == "ok", out
    assert len(calls) == 3
    assert (tmp_dir / "sdk_version").is_file()


def test_the_minimal_sdk_row_is_selected_by_component():
    rows = (
        ToolchainArtifact("linux-x86_64", "minimal-sdk", "m.tar.xz", 1, "a" * 64),
        ToolchainArtifact("linux-x86_64", "arm-zephyr-eabi-toolchain", "t.tar.xz", 1, "b" * 64),
    )

    assert fetch.minimal_sdk_artifact(rows).filename == "m.tar.xz"
    assert fetch.minimal_sdk_artifact(rows[1:]) is None


def test_the_windows_7z_bundle_goes_through_7zip_and_lands_the_same_way(tmp_path, monkeypatch):
    """The Windows bundle is a `.7z`, extracted by a 7-Zip binary rather than
    `tarfile`. Hermetic: a fake 7-Zip writes the tree the real archive holds
    (measured: one `zephyr-sdk-1.0.1/` top-level directory)."""
    payload = b"7z-archive-bytes"
    _serve(monkeypatch, payload)
    art = ToolchainArtifact(
        "windows-x86_64", fetch.MINIMAL_SDK_COMPONENT, "zephyr-sdk-1.0.1_windows-x86_64_minimal.7z",
        len(payload), hashlib.sha256(payload).hexdigest(),
    )
    seen = []

    class _Done:
        returncode = 0

    def fake_7z(argv, **kw):
        seen.append(argv)
        out = Path(next(a for a in argv if a.startswith("-o"))[2:])
        (out / "zephyr-sdk-1.0.1").mkdir(parents=True)
        (out / "zephyr-sdk-1.0.1" / "sdk_version").write_text("1.0.1\n", encoding="utf-8")
        return _Done()

    monkeypatch.setattr(fetch, "_find_seven_zip", lambda: "/opt/7zip/7zz")
    monkeypatch.setattr(fetch, "_run", fake_7z)

    out, _, tmp_dir = _install(tmp_path, art)

    assert out.kind == "ok", out
    assert seen and seen[0][:3] == ["/opt/7zip/7zz", "x", "-y"]
    assert (tmp_dir / "sdk_version").read_text(encoding="utf-8") == "1.0.1\n"


def test_a_windows_host_with_no_7zip_names_the_archive_it_could_not_extract(
    tmp_path, monkeypatch
):
    payload = b"7z-archive-bytes"
    _serve(monkeypatch, payload)
    art = ToolchainArtifact(
        "windows-x86_64", fetch.MINIMAL_SDK_COMPONENT, "zephyr-sdk-1.0.1_windows-x86_64_minimal.7z",
        len(payload), hashlib.sha256(payload).hexdigest(),
    )
    monkeypatch.setattr(fetch, "_find_seven_zip", lambda: None)

    out, _, tmp_dir = _install(tmp_path, art)

    assert out.kind == "install"
    assert "zephyr-sdk-1.0.1_windows-x86_64_minimal.7z" in out.message
    assert not tmp_dir.exists()
