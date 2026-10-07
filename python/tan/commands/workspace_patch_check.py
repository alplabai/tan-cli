# SPDX-License-Identifier: Apache-2.0
"""Is alp-sdk's `zephyr/patches.yml` applied in the resolved west workspace?
(tan-cli#1376)

A customer hit `alp_camera_open FAILED: ALP_ERR_NOSUPPORT` because the
workspace lacked patch 0001 (Alif clock `set_rate`) while `tan build` said
`ok: true`. `tan bootstrap` applies the patches (`bootstrap_patches.py`); this
module only ASKS, for `tan doctor` and `tan build`, and never writes to the
workspace.

The SDK's own `scripts/verify_west_patches.py` is the source of truth and is
run unchanged; this module reads its exit code (0 applied / 1 not applied /
2 uninspectable / 3 module not checked out) and its failure report. It never
raises: an absent verifier (older SDK), a missing interpreter, a timeout or an
unreadable report all become `unchecked`, because a check that cannot run must
not fail an offline doctor or a build.

Caching (`tan build` only): a positive verdict is stored in the build dir keyed
by (every workspace module's HEAD, `patches.yml` bytes). Only `applied` is
cached: applying patches edits the working tree, not HEAD, so a cached
"missing" would go stale the moment the user ran `tan bootstrap`, whereas an
applied tree stays applied until a HEAD moves (`west update`) or `patches.yml`
changes -- both of which change the key.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from tan.core.host_python import probe_host_python
from tan.core.subprocess_env import spawn_env
from tan.core.venv import venv_python, west_program
from tan.core.west_patches import (
    UnappliedPatch,
    cache_key,
    classify_verify,
    parse_unapplied,
    parse_unapplied_patches,
)

APPLIED = "applied"
MISSING = "missing"
UNCHECKED = "unchecked"

CACHE_FILE = ".tan-workspace-patches.json"
_VERIFIER = Path("scripts") / "verify_west_patches.py"
_PATCHES_YML = Path("zephyr") / "patches.yml"
_TIMEOUT_S = 120
_PYTHON_FLOOR = (3, 10)


@dataclass(frozen=True)
class PatchCheck:
    state: str  # APPLIED | MISSING | UNCHECKED
    patches: list[UnappliedPatch] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    #: Why `unchecked`, or the applied count line; empty for `missing`.
    note: str = ""
    cached: bool = False


def _run(argv: list[str], cwd: str) -> tuple[int | None, str, str]:
    try:
        out = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL, timeout=_TIMEOUT_S,
            env=spawn_env(), check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, "", ""
    return out.returncode, out.stdout, out.stderr


def _head(module_dir: Path) -> str:
    """A cheap, spawn-free identity of a checkout's HEAD. Not a verdict: it
    only has to CHANGE when the checkout moves."""
    try:
        git = module_dir / ".git"
        gitdir = git
        if git.is_file():
            text = git.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                gitdir = (module_dir / text[len("gitdir:"):].strip()).resolve()
        head_file = gitdir / "HEAD"
        head = head_file.read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head[4:].strip()
            loose = gitdir / ref
            if loose.is_file():
                return loose.read_text(encoding="utf-8").strip()
            packed = gitdir / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.endswith(" " + ref):
                        return line.split(" ", 1)[0]
            return f"{ref}@{head_file.stat().st_mtime_ns}"
        return head
    except (OSError, UnicodeDecodeError):
        return "unreadable"


def _workspace_heads(west: str, workspace: Path) -> dict[str, str] | None:
    code, out, _ = _run([west, "list", "-f", "{abspath}"], str(workspace))
    if code != 0:
        return None
    return {p: _head(Path(p)) for p in (ln.strip() for ln in out.splitlines()) if p}


def _interpreter(workspace: Path, sdk_root: str) -> str | None:
    # The workspace venv carries west + pyyaml (Zephyr's requirements), which
    # the verifier imports; the resolved host Python is the fallback.
    py = venv_python(str(workspace), sdk_root)
    if py is not None:
        return py
    host = probe_host_python(_PYTHON_FLOOR, need_west=True)
    return host.interpreter if host is not None else None


def _read_cache(path: Path, key: str) -> bool:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and doc.get("key") == key and doc.get("state") == APPLIED


def _write_cache(path: Path, key: str) -> None:
    try:
        path.write_text(json.dumps({"key": key, "state": APPLIED}), encoding="utf-8")
    except OSError:
        pass  # the cache is an optimisation; a read-only build dir just re-checks


def check_workspace_patches(
    workspace: Path, sdk_root: str | None, *, cache_dir: Path | None = None
) -> PatchCheck:
    """Verify `<sdk_root>/zephyr/patches.yml` against `workspace`. Read-only."""
    if sdk_root is None:
        return PatchCheck(UNCHECKED, note="no alp-sdk checkout resolved")
    sdk = Path(sdk_root)
    verifier, patches_yml = sdk / _VERIFIER, sdk / _PATCHES_YML
    if not verifier.is_file() or not patches_yml.is_file():
        return PatchCheck(
            UNCHECKED,
            note="this alp-sdk checkout carries no patch verifier "
            "(scripts/verify_west_patches.py), so patches cannot be checked",
        )
    west = west_program(str(workspace), sdk_root)
    key = None
    if cache_dir is not None:
        heads = _workspace_heads(west, workspace)
        if heads is not None:
            try:
                key = cache_key(patches_yml.read_bytes(), heads)
            except OSError:
                key = None
        if key is not None and _read_cache(cache_dir / CACHE_FILE, key):
            return PatchCheck(APPLIED, note="verified applied (cached)", cached=True)

    python = _interpreter(workspace, sdk_root)
    if python is None:
        return PatchCheck(UNCHECKED, note="no Python able to run the patch verifier was found")
    argv = [
        python, str(verifier), "--repo", str(sdk),
        "--topdir", str(workspace), "--west", west,
    ]
    with tempfile.TemporaryDirectory(prefix="tan-patches-") as scratch:
        code, _out, err = _run(argv, scratch)
        verdict = classify_verify(code)
        if verdict == "applied":
            if key is not None and cache_dir is not None:
                _write_cache(cache_dir / CACHE_FILE, key)
            return PatchCheck(APPLIED, note="verified applied")
        if verdict == "unchecked":
            return PatchCheck(
                UNCHECKED,
                note="some zephyr/patches.yml modules are not checked out in this workspace; "
                "their patches could not be checked",
            )
        patches = parse_unapplied_patches(err) if verdict == "unapplied" else []
        if not patches:
            return PatchCheck(
                UNCHECKED,
                note="the patch verifier could not inspect this workspace "
                "(run scripts/verify_west_patches.py directly to see why)",
            )
        lcode, listing, _ = _run([*argv, "--list-unapplied"], scratch)
        modules = parse_unapplied(listing) if lcode == 0 else []
    return PatchCheck(MISSING, patches=patches, modules=modules)
