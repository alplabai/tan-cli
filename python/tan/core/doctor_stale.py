# SPDX-License-Identifier: Apache-2.0
"""`tan doctor`'s stale-install verdict: is the `tan` that is RUNNING behind
the source it was installed from?

Motivating case: a `pipx install git+https://github.com/alplabai/tan-cli.git@dev#subdirectory=python`
taken on one day keeps answering `tan 0.7.0` after `dev` has moved on, and
nothing says so -- agents kept using it unknowingly.

The question is answered from the install's own provenance record, PEP 610's
`direct_url.json` in the `tan-cli` dist-info, in order of cost:

* **path / editable install** (`file://` url): offline. Read the source
  tree's `tan/version.py` `TAN_VERSION` and compare with the running one.
* **VCS install** (`vcs_info`): ONE `git ls-remote <url> <branch>` with a short
  timeout, compared with the installed `commit_id`. Pinned installs (a tag or
  a sha, which `refs/heads/<rev>` does not resolve) are intentional and pass.
* anything else (wheel/PyPI/frozen binary): no provenance, no verdict.

Never raises and never fails doctor: offline/timeout/no-git degrades to
`None` ("unable to check"), which the caller renders as a pass.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from tan.core.probe import probe

#: Network ceiling for the one `ls-remote`. Doctor must stay snappy offline.
LS_REMOTE_TIMEOUT_S = 4

_VERSION_RE = re.compile(r'^TAN_VERSION\s*=\s*"([^"]+)"', re.MULTILINE)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIST_NAMES = ("tan-cli", "tan_cli", "tan")


@dataclass(frozen=True)
class InstallOrigin:
    kind: str  # "path" | "vcs"
    url: str
    commit: str | None = None  # vcs: installed commit
    revision: str | None = None  # vcs: requested branch/tag/sha
    subdirectory: str | None = None


@dataclass(frozen=True)
class StaleVerdict:
    detail: str
    fix: str


def read_install_origin(direct_url_text: str | None) -> InstallOrigin | None:
    """Parse a PEP 610 `direct_url.json` body; `None` if absent/unusable."""
    if not direct_url_text:
        return None
    try:
        data = json.loads(direct_url_text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("url"), str):
        return None
    url = data["url"]
    sub = data.get("subdirectory")
    sub = sub if isinstance(sub, str) else None
    vcs = data.get("vcs_info")
    if isinstance(vcs, dict) and vcs.get("vcs") == "git":
        commit = vcs.get("commit_id")
        rev = vcs.get("requested_revision")
        return InstallOrigin(
            "vcs",
            url,
            commit if isinstance(commit, str) else None,
            rev if isinstance(rev, str) else None,
            sub,
        )
    if url.startswith("file:"):
        return InstallOrigin("path", url, subdirectory=sub)
    return None


def installed_direct_url_text() -> str | None:
    """The `direct_url.json` of the running tan's dist, or `None`."""
    from importlib import metadata

    for name in DIST_NAMES:
        try:
            return metadata.distribution(name).read_text("direct_url.json")
        except metadata.PackageNotFoundError:
            continue
        except OSError:
            return None
    return None


def parse_tan_version(version_py_text: str) -> str | None:
    m = _VERSION_RE.search(version_py_text)
    return m.group(1) if m else None


def source_version(origin: InstallOrigin) -> str | None:
    """`TAN_VERSION` of a `file://` source tree (offline); `None` if unreadable."""
    root = Path(urllib.request.url2pathname(urllib.parse.urlparse(origin.url).path))
    for rel in (origin.subdirectory or "", "python", ""):
        try:
            text = (root / rel / "tan" / "version.py").read_text(encoding="utf-8")
        except OSError:
            continue
        return parse_tan_version(text)
    return None


def remote_head(origin: InstallOrigin, git_exe: str | None) -> str | None:
    """The commit `origin.revision` (default `HEAD`) points at on the remote;
    `None` when it cannot be learned or the revision is not a branch (a tag/sha
    pin) -- either way there is no "newer" to report."""
    if git_exe is None or (origin.revision and _SHA_RE.match(origin.revision)):
        return None
    ref = f"refs/heads/{origin.revision}" if origin.revision else "HEAD"
    out = probe([git_exe, "ls-remote", origin.url, ref], timeout=LS_REMOTE_TIMEOUT_S)
    if not out:
        return None
    sha = out.split(None, 1)[0].strip().lower()
    return sha if _SHA_RE.match(sha) else None


def reinstall_command(origin: InstallOrigin) -> str:
    spec = origin.url if origin.url.startswith("git+") else f"git+{origin.url}"
    if origin.revision:
        spec += f"@{origin.revision}"
    if origin.subdirectory:
        spec += f"#subdirectory={origin.subdirectory}"
    return f'pipx install --force "{spec}"'


def stale_verdict(
    running_version: str,
    origin: InstallOrigin | None,
    *,
    source_ver: str | None = None,
    remote_commit: str | None = None,
) -> StaleVerdict | None:
    """Pure verdict. `None` = not behind, or unable to tell."""
    if origin is None:
        return None
    if origin.kind == "path":
        if source_ver is None or source_ver == running_version:
            return None
        return StaleVerdict(
            f"installed tan is {running_version} but the source it was installed from "
            f"({origin.url}) is {source_ver} -- reinstall to pick it up",
            "reinstall from that source (pipx install --force <source>/python)",
        )
    if origin.commit and remote_commit and origin.commit != remote_commit:
        rev = origin.revision or "HEAD"
        return StaleVerdict(
            f"installed tan {running_version} was built from {origin.commit[:8]} but "
            f"{rev} is now {remote_commit[:8]} -- this install is behind",
            reinstall_command(origin),
        )
    return None


def running_tan_verdict(version: str, git_exe: str | None, offline: bool) -> StaleVerdict | None:
    """IO half: read the running tan's provenance and judge it. `offline`
    skips the one `git ls-remote`. Cannot raise."""
    try:
        origin = read_install_origin(installed_direct_url_text())
        if origin is None:
            return None
        if origin.kind == "path":
            return stale_verdict(version, origin, source_ver=source_version(origin))
        remote = None if offline else remote_head(origin, git_exe)
        return stale_verdict(version, origin, remote_commit=remote)
    except Exception:  # noqa: BLE001 -- a doctor probe must never traceback
        return None
