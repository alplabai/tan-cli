# SPDX-License-Identifier: Apache-2.0
"""tan installs both pinned Zephyr SDK archives itself, hashing each against
alp-sdk's pin while it streams:

- the minimal bundle (tan-cli#1556), extracted into a scratch dir and its
  single top-level `zephyr-sdk-<v>/` moved into place -- the job `west sdk
  install`'s `download_and_extract` did, minus the SDK discovery that made it
  reuse another copy and the `setup -c` registry write;
- the `arm-zephyr-eabi` toolchain (tan-cli#1496), into `<sdk>/gnu/`, the
  layout the SDK's own `setup.sh -t` produces.

See `tan.core.toolchain_pin` for what the pin check does and does not prove."""
from __future__ import annotations

import hashlib
import os
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from tan.core.proxy import select_https_proxy
from tan.core.subprocess_env import spawn_env
from tan.core.toolchain_provision import (
    TMP_SUFFIX_PREFIX,
    TOOLCHAIN_COMPONENT,
    ToolchainArtifact,
)

#: The artifact row whose archive west's `setup.sh` would have fetched.
TOOLCHAIN_ARTIFACT_COMPONENT = f"{TOOLCHAIN_COMPONENT}-toolchain"

#: The artifact row for the SDK's minimal bundle (`sdk_version`, `cmake/`,
#: `setup.sh`), which `west sdk install` used to fetch.
MINIMAL_SDK_COMPONENT = "minimal-sdk"

#: A whole download is abandoned past this -- the read timeout alone lets a
#: trickling stream run forever. The bound `Runner.run` put on the `west sdk
#: install` child this replaces (`bootstrap_cmd.INSTALL_TIMEOUT_S`).
DOWNLOAD_DEADLINE_S = 3600

#: HTTP statuses that mean the pinned release asset is not there at all (a
#: wrong pin, a deleted release) -- not worth a retry, and not a proxy problem.
_MISSING_STATUSES = frozenset({404, 410})

#: Windows: a tree 7-Zip just wrote can be held open by an on-access scanner,
#: and renaming it fails with WinError 5 for a moment. West's `shutil.move`
#: fell back to a copy; this retries the rename instead.
_RENAME_ATTEMPTS = 5
_RENAME_BACKOFF_S = 0.5

#: Seams, so tests replace these here rather than patching process-global
#: `time`/`os`/`subprocess` for everything else running in the same test.
_now = time.monotonic
_sleep = time.sleep
_replace = os.replace
_run = subprocess.run
SEVEN_ZIP_EXTRACTORS = ("7z", "7za", "7zr", "7zz", "7zzs")
_CHUNK = 1 << 20


@dataclass(frozen=True)
class FetchOutcome:
    #: "ok" | "mismatch" (hash differs from the pin) | "unverified" (could not download)
    #: | "missing" (the release asset is not there: HTTP 404/410)
    #: | "install" (downloaded and matched, but could not be extracted/placed).
    kind: str
    message: str = ""
    #: The sha256 actually downloaded, on a hash "mismatch" (not on an
    #: over-size one) -- what `toolchain_pin.mismatch_diagnosis` compares the
    #: release sum against.
    got_sha256: str = ""


def _single(artifacts: tuple[ToolchainArtifact, ...], component: str) -> ToolchainArtifact | None:
    found = [a for a in artifacts if a.component == component]
    return found[0] if len(found) == 1 else None


def toolchain_artifact(artifacts: tuple[ToolchainArtifact, ...]) -> ToolchainArtifact | None:
    return _single(artifacts, TOOLCHAIN_ARTIFACT_COMPONENT)


def minimal_sdk_artifact(artifacts: tuple[ToolchainArtifact, ...]) -> ToolchainArtifact | None:
    return _single(artifacts, MINIMAL_SDK_COMPONENT)


def _stream(resp, dest: Path, art: ToolchainArtifact, url: str):
    """Copy `resp` to `dest`, hashing as it goes. `(FetchOutcome, None, n)` when
    the stream is refused mid-way (over the pinned size, past the deadline),
    else `(None, hexdigest, n)`."""
    digest = hashlib.sha256()
    total = 0
    deadline = _now() + DOWNLOAD_DEADLINE_S
    with open(dest, "wb") as out:
        while True:
            if _now() > deadline:
                return FetchOutcome(
                    "unverified",
                    f"{art.filename} did not finish within {DOWNLOAD_DEADLINE_S}s "
                    f"({total} of {art.size_bytes} bytes arrived)",
                ), None, total
            chunk = resp.read(_CHUNK)
            if not chunk:
                return None, digest.hexdigest(), total
            total += len(chunk)
            if total > art.size_bytes:
                return FetchOutcome(
                    "mismatch",
                    f"{url} is larger than the {art.size_bytes} bytes alp-sdk pins for {art.filename}",
                ), None, total
            digest.update(chunk)
            out.write(chunk)


def _download(url: str, dest: Path, art: ToolchainArtifact) -> FetchOutcome:
    from tan.commands.sdk_cmd import (  # noqa: PLC0415
        NETWORK_TIMEOUT_SECONDS,
        _releases_opener,
        _unroutable_proxy_refusal,
    )

    proxy = select_https_proxy(url)
    refusal = _unroutable_proxy_refusal(proxy, url)
    if refusal is not None:
        return FetchOutcome("unverified", refusal)
    request = urllib.request.Request(url, headers={"User-Agent": "tan-cli/0"})  # noqa: S310
    try:
        with _releases_opener(proxy).open(request, timeout=NETWORK_TIMEOUT_SECONDS) as resp:
            refused, got, total = _stream(resp, dest, art, url)
    except urllib.error.HTTPError as err:
        kind = "missing" if err.code in _MISSING_STATUSES else "unverified"
        return FetchOutcome(kind, f"HTTP {err.code} {err.reason} for {url}")
    except Exception as err:  # noqa: BLE001 -- every transport failure is a message
        return FetchOutcome("unverified", f"{type(err).__name__}: {err}")
    if refused is not None:
        return refused
    if total < art.size_bytes:
        return FetchOutcome(
            "unverified",
            f"incomplete download: {total} of the pinned {art.size_bytes} bytes of "
            f"{art.filename} arrived before the stream ended",
        )
    if got != art.sha256.strip().lower():
        return FetchOutcome(
            "mismatch",
            f"{art.filename} downloaded from {url} has sha256 {got}, but alp-sdk pins "
            f"{art.sha256}",
            got_sha256=got,
        )
    return FetchOutcome("ok")


def _find_seven_zip() -> str | None:
    """Absolute path of the first 7-Zip on PATH that is not inside the current
    directory. The bare name is never spawned: on Windows CreateProcess searches the
    parent's cwd first, so a planted `7z.exe` in the project would run (cf. #1483)."""
    from tan.core.tool_lookup import resolve_tool  # noqa: PLC0415

    cwd = Path(os.getcwd()).resolve()
    for name in SEVEN_ZIP_EXTRACTORS:
        found = resolve_tool(name, os.environ).resolved
        if found is None or not Path(found).is_absolute():
            continue
        real = Path(found).resolve()
        if real == cwd or cwd in real.parents:
            continue
        return str(real)
    return None


def _extract(archive: Path, dest: Path) -> str | None:
    """Error text, or None. `.tar.xz` via tarfile's `data` filter (no absolute paths,
    no escapes, no device nodes); `.7z` via a 7-Zip binary (Windows' archive format)."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if archive.name.endswith(".7z"):
            program = _find_seven_zip()
            if program is None:
                return f"no usable 7-Zip on PATH to extract {archive.name}"
            proc = _run(  # noqa: S603
                [program, "x", "-y", f"-o{dest}", "--", str(archive)],
                capture_output=True, text=True, check=False, env=spawn_env(),
            )
            return None if proc.returncode == 0 else f"{program} exited {proc.returncode}"
        with tarfile.open(archive, "r:*") as tar:
            tar.extractall(dest, filter="data")
    except (OSError, tarfile.TarError, subprocess.SubprocessError) as err:
        return f"{type(err).__name__}: {err}"
    return None


def install_pinned_toolchain(
    base_url: str, art: ToolchainArtifact, tmp_dir: Path, scratch_parent: Path, leaf: str
) -> FetchOutcome:
    """Download + hash + extract `art` into `<tmp_dir>/gnu/` (the layout `setup.sh`
    gives). The scratch dir is a swept `<leaf>.tmp-*` sibling, deleted on every path."""
    url = base_url.rstrip("/") + "/" + art.filename
    with tempfile.TemporaryDirectory(
        dir=scratch_parent, prefix=f"{leaf}{TMP_SUFFIX_PREFIX}dl-",
        # A scanner holding a just-written file must not turn cleanup into a
        # traceback; the `<leaf>.tmp-*` name is swept on the next run anyway.
        ignore_cleanup_errors=True,
    ) as scratch:
        archive = Path(scratch) / art.filename
        got = _download(url, archive, art)
        if got.kind != "ok":
            return got
        err = _extract(archive, tmp_dir / "gnu")
        if err is not None:
            return FetchOutcome("install", f"extracting {art.filename} failed: {err}")
    if not (tmp_dir / "gnu" / TOOLCHAIN_COMPONENT).is_dir():
        return FetchOutcome(
            "install", f"{art.filename} did not extract a gnu/{TOOLCHAIN_COMPONENT} directory"
        )
    return FetchOutcome("ok")


def _rename_with_retry(src: Path, dst: Path) -> None:
    """`os.replace` (via `_replace`), retried on `PermissionError` only (see `_RENAME_ATTEMPTS`)."""
    for attempt in range(1, _RENAME_ATTEMPTS + 1):
        try:
            _replace(src, dst)
            return
        except PermissionError:
            if attempt == _RENAME_ATTEMPTS:
                raise
            _sleep(_RENAME_BACKOFF_S * attempt)


def install_pinned_minimal_sdk(
    base_url: str, art: ToolchainArtifact, tmp_dir: Path, scratch_parent: Path, leaf: str
) -> FetchOutcome:
    """Download + hash + extract the minimal bundle, then move its single
    top-level `zephyr-sdk-<v>/` directory to `tmp_dir`, which must not exist
    (a move onto an existing directory would nest or merge). The scratch dir is
    a swept `<leaf>.tmp-*` sibling, deleted on every path."""
    if tmp_dir.exists():
        return FetchOutcome("install", f"{tmp_dir} already exists; refusing to merge into it")
    url = base_url.rstrip("/") + "/" + art.filename
    with tempfile.TemporaryDirectory(
        dir=scratch_parent, prefix=f"{leaf}{TMP_SUFFIX_PREFIX}dl-",
        # A scanner holding a just-written file must not turn cleanup into a
        # traceback; the `<leaf>.tmp-*` name is swept on the next run anyway.
        ignore_cleanup_errors=True,
    ) as scratch:
        archive = Path(scratch) / art.filename
        got = _download(url, archive, art)
        if got.kind != "ok":
            return got
        unpacked = Path(scratch) / "unpacked"
        err = _extract(archive, unpacked)
        if err is not None:
            return FetchOutcome("install", f"extracting {art.filename} failed: {err}")
        entries = list(unpacked.iterdir())
        if len(entries) != 1 or not entries[0].is_dir() or entries[0].is_symlink():
            names = sorted(e.name for e in entries)
            return FetchOutcome(
                "install",
                f"{art.filename} must hold exactly one top-level directory, found {names}",
            )
        try:
            _rename_with_retry(entries[0], tmp_dir)
        except OSError as err:
            return FetchOutcome("install", f"cannot move the SDK into {tmp_dir}: {err}")
    return FetchOutcome("ok")
