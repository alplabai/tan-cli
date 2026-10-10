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

Caching (`tan build` only): a positive verdict (applied, or applied with some
modules not checked out) is stored in `<project>/build/` keyed by (every
workspace module's HEAD, `patches.yml` bytes, and a working-tree fingerprint:
`(path, mtime_ns, size)` of every file the patches write, looked up under every
module checkout). The fingerprint matters because `west patch clean`,
`git checkout -- .` and `git stash` revert patches WITHOUT moving HEAD; they
rewrite the patched files, so mtime/size change and the cache misses. Only
positive verdicts are cached: a cached "missing" would go stale the moment the
user ran `tan bootstrap`.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from tan.core.atomic_write import atomic_write_text
from tan.core.host_python import probe_host_python
from tan.core.patch_paths import patch_paths
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
    #: Why `tan build` re-verifies on every build: the cache key could not be
    #: built in full, so nothing is cached. Empty when caching was in play.
    cache_note: str = ""


def _run(argv: list[str], cwd: str, timeout: int = _TIMEOUT_S) -> tuple[int | None, str, str]:
    try:
        out = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL, timeout=timeout,
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


_PATCH_ENTRY = re.compile(r"^\s*-?\s*path:\s*['\"]?([^'\"#\s]+)", re.MULTILINE)


def _patched_files(sdk: Path) -> list[str] | None:
    """Repo-relative paths every `patches.yml` patch touches, or `None`.

    Every path a patch touches counts, deletions, renames and binary patches
    included (`tan.core.patch_paths`), so a revert that restores a deleted
    file moves the fingerprint.

    `None` when the list cannot be trusted to be complete: `patches.yml` or
    any named patch is unreadable, or no path was found at all. A partial
    list would silently weaken the cache key towards HEAD-only, reopening the
    revert hole for the dropped files, so the caller skips caching instead.
    """
    rels: set[str] = set()
    try:
        entries = _PATCH_ENTRY.findall((sdk / _PATCHES_YML).read_text(encoding="utf-8"))
        for entry in entries:
            text = (sdk / "zephyr" / "patches" / entry).read_text(encoding="utf-8", errors="replace")
            rels.update(patch_paths(text))
    except (OSError, ValueError):
        return None
    return sorted(rels) or None


def _tree_fingerprint(module_dirs: list[str], rels: list[str]) -> dict[str, str]:
    """`{path: "mtime_ns:size"}` for each patched file present under any module."""
    out: dict[str, str] = {}
    for d in module_dirs:
        for rel in rels:
            f = Path(d) / rel
            try:
                st = f.stat()
            except OSError:
                continue
            out[str(f)] = f"{st.st_mtime_ns}:{st.st_size}"
    return out


def _cache_key(west: str, workspace: Path, sdk: Path, why: list[str]) -> str | None:
    """The cache key, or `None` after appending the reason to `why` when it
    cannot be built in full (nothing is then cached)."""
    heads = _workspace_heads(west, workspace)
    if heads is None:
        why.append("the west workspace modules could not be listed")
        return None
    try:
        rels = _patched_files(sdk)
        if rels is None:
            why.append("zephyr/patches.yml or a patch it names could not be read in full")
            return None
        tree = _tree_fingerprint(list(heads), rels)
        return cache_key((sdk / _PATCHES_YML).read_bytes(), {**heads, **tree})
    except OSError:
        why.append("zephyr/patches.yml could not be read")
        return None


def _interpreter(workspace: Path, sdk_root: str) -> str | None:
    # The workspace venv carries west + pyyaml (Zephyr's requirements), which
    # the verifier imports; the resolved host Python is the fallback.
    py = venv_python(str(workspace), sdk_root)
    if py is not None:
        return py
    host = probe_host_python(_PYTHON_FLOOR, need_west=True)
    return host.interpreter if host is not None else None


def _read_cache(path: Path, key: str) -> str | None:
    """The cached state (`APPLIED` or `UNCHECKED`) for `key`, else `None`."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(doc, dict) and doc.get("key") == key and doc.get("state") in (APPLIED, UNCHECKED):
        return doc["state"]
    return None


def _cache_hit(cache_dir: Path, key: str | None) -> PatchCheck | None:
    cached = _read_cache(cache_dir / CACHE_FILE, key) if key is not None else None
    if cached == APPLIED:
        return PatchCheck(APPLIED, note="verified applied (cached)", cached=True)
    if cached == UNCHECKED:
        return PatchCheck(UNCHECKED, note=_PARTIAL_NOTE, cached=True)
    return None


def _write_cache(path: Path, key: str, state: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(str(path), json.dumps({"key": key, "state": state}))
    except Exception:  # noqa: BLE001 -- the cache is an optimisation
        pass


_PARTIAL_NOTE = (
    "some zephyr/patches.yml modules are not checked out in this workspace; "
    "their patches could not be checked"
)


def check_workspace_patches(
    workspace: Path,
    sdk_root: str | None,
    *,
    cache_dir: Path | None = None,
    timeout: int = _TIMEOUT_S,
) -> PatchCheck:
    """Verify `<sdk_root>/zephyr/patches.yml` against `workspace`. Read-only,
    and never raises: anything unexpected is `unchecked`."""
    why: list[str] = []
    try:
        result = _check(workspace, sdk_root, cache_dir, timeout, why)
        if why and result.state != MISSING:
            result = replace(result, cache_note=why[0])
        return result
    except Exception:  # noqa: BLE001 -- an advisory check must never break its caller
        return PatchCheck(UNCHECKED, note="the patch check failed unexpectedly")


def _check(
    workspace: Path, sdk_root: str | None, cache_dir: Path | None, timeout: int,
    why: list[str],
) -> PatchCheck:
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
        key = _cache_key(west, workspace, sdk, why)
        hit = _cache_hit(cache_dir, key)
        if hit is not None:
            return hit

    python = _interpreter(workspace, sdk_root)
    if python is None:
        return PatchCheck(UNCHECKED, note="no Python able to run the patch verifier was found")
    argv = [
        python, str(verifier), "--repo", str(sdk),
        "--topdir", str(workspace), "--west", west,
    ]
    with tempfile.TemporaryDirectory(prefix="tan-patches-") as scratch:
        code, _out, err = _run(argv, scratch, timeout)
        verdict = classify_verify(code)
        if verdict == "applied":
            if key is not None and cache_dir is not None:
                _write_cache(cache_dir / CACHE_FILE, key, APPLIED)
            return PatchCheck(APPLIED, note="verified applied")
        if verdict == "unchecked":
            # Everything inspectable is patched: as good as applied until a
            # head or patched file moves, so it is cached too.
            if key is not None and cache_dir is not None:
                _write_cache(cache_dir / CACHE_FILE, key, UNCHECKED)
            return PatchCheck(UNCHECKED, note=_PARTIAL_NOTE)
        if verdict != "unapplied" or "Traceback" in err:
            return PatchCheck(
                UNCHECKED,
                note="the patch verifier could not inspect this workspace "
                "(run scripts/verify_west_patches.py directly to see why)",
            )
        # Exit 1 is the verifier's "not applied"; if it printed no parsable
        # line, the patches are unnamed but still missing.
        patches = parse_unapplied_patches(err)
        lcode, listing, _ = _run([*argv, "--list-unapplied"], scratch, timeout)
        modules = parse_unapplied(listing) if lcode == 0 else []
    return PatchCheck(MISSING, patches=patches, modules=modules)
