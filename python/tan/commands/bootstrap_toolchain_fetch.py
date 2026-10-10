# SPDX-License-Identifier: Apache-2.0
"""IO half of tan-cli#1496's toolchain-archive check: download the pinned
`arm-zephyr-eabi` archive ourselves, hash it against alp-sdk's pin while it
streams, and extract it into the layout the SDK's own `setup.sh` produces
(`<sdk>/gnu/arm-zephyr-eabi`). West verifies only the minimal bundle and its
`setup.sh` downloads this archive unchecked, so west is told
`--no-gnu-toolchains` and this module does the job (see `tan.core.toolchain_pin`)."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import tarfile
import tempfile
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
SEVEN_ZIP_EXTRACTORS = ("7z", "7za", "7zr", "7zz", "7zzs")
_CHUNK = 1 << 20


@dataclass(frozen=True)
class FetchOutcome:
    #: "ok" | "mismatch" (hash differs from the pin) | "unverified" (could not download)
    #: | "install" (downloaded and matched, but could not be extracted/placed).
    kind: str
    message: str = ""


def toolchain_artifact(artifacts: tuple[ToolchainArtifact, ...]) -> ToolchainArtifact | None:
    found = [a for a in artifacts if a.component == TOOLCHAIN_ARTIFACT_COMPONENT]
    return found[0] if len(found) == 1 else None


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
    import urllib.request  # noqa: PLC0415

    request = urllib.request.Request(url, headers={"User-Agent": "tan-cli/0"})  # noqa: S310
    digest = hashlib.sha256()
    total = 0
    try:
        with _releases_opener(proxy).open(request, timeout=NETWORK_TIMEOUT_SECONDS) as resp:
            with open(dest, "wb") as out:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > art.size_bytes:
                        return FetchOutcome(
                            "mismatch",
                            f"{url} is larger than the {art.size_bytes} bytes alp-sdk pins "
                            f"for {art.filename}",
                        )
                    digest.update(chunk)
                    out.write(chunk)
    except Exception as err:  # noqa: BLE001 -- every transport failure is a message
        return FetchOutcome("unverified", f"{type(err).__name__}: {err}")
    if total < art.size_bytes:
        return FetchOutcome(
            "unverified",
            f"incomplete download: {total} of the pinned {art.size_bytes} bytes of "
            f"{art.filename} arrived before the stream ended",
        )
    got = digest.hexdigest()
    if got != art.sha256.strip().lower():
        return FetchOutcome(
            "mismatch",
            f"{art.filename} downloaded from {url} has sha256 {got}, but alp-sdk pins "
            f"{art.sha256} (a proxy rewriting the download gives the same symptom)",
        )
    return FetchOutcome("ok")


def _extract(archive: Path, dest: Path) -> str | None:
    """Error text, or None. `.tar.xz` via tarfile's `data` filter (no absolute paths,
    no escapes, no device nodes); `.7z` via a 7-Zip binary (Windows' archive format)."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if archive.name.endswith(".7z"):
            program = next((p for p in SEVEN_ZIP_EXTRACTORS if shutil.which(p)), None)
            if program is None:
                return "no 7-Zip on PATH to extract the .7z toolchain archive"
            proc = subprocess.run(  # noqa: S603
                [program, "x", "-y", f"-o{dest}", str(archive)],
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
        dir=scratch_parent, prefix=f"{leaf}{TMP_SUFFIX_PREFIX}dl-"
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
