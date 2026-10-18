# SPDX-License-Identifier: Apache-2.0
"""Keep `west sdk install` from adopting an SDK it did not install (tan-cli#1552).

Zephyr's `west sdk install` ignores `--install-dir` when its own discovery
(`FindZephyr-sdk.cmake`) already lists the requested version, and then runs
`setup.sh` on THAT directory. Discovery reads the CMake user package registry
and the fixed prefixes `/usr /usr/local /opt $HOME $HOME/.local
$HOME/.local/opt $HOME/bin`. tan then finds its scratch directory without
`sdk_version`, and the old "remove that directory and re-run" remedy looped
forever. Separately, when west DID honour `--install-dir`, its `setup.sh -c`
registered the scratch path in the user's real `~/.cmake/packages`, and tan
then moved the directory away, leaving a dangling entry.

Two halves:

* [`isolated_env`] -- run west with a scratch `HOME`/`USERPROFILE`, which hides
  the `$HOME` prefixes and the file-based registry and redirects registration
  into a directory tan deletes afterwards.
* [`reuse_warning`] -- what is left (`/opt`, `/usr/local`, `/usr`, and the
  Windows HKCU registry, which no env var can hide) is detected after the fact
  and named, with a remedy that actually breaks the loop.

`tan build` is unaffected by the registry: it exports `ZEPHYR_SDK_INSTALL_DIR`
from tan's verified store itself (`tan.core.zephyr_env`), so nothing is
registered for the final store path.
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tan.core import toolchain_provision

#: Where a scratch home lives, under the toolchain root. Matches
#: `toolchain_provision.wreckage_glob_pattern(leaf)` (`<leaf>.tmp-*`), so a
#: crash that skips the `finally` is swept by the next run's wreckage reclaim.
SCRATCH_HOME_INFIX = "home-"

#: The prefixes `FindZephyr-sdk.cmake` scans, relative to HOME or absolute.
_FIXED_PREFIXES = ("/usr", "/usr/local", "/opt")
_HOME_PREFIXES = ("", ".local", ".local/opt", "bin")


def scratch_home_path(root: Path, leaf: str, pid: int) -> Path:
    return root / f"{leaf}{toolchain_provision.TMP_SUFFIX_PREFIX}{SCRATCH_HOME_INFIX}{pid}"


#: Variables CMake/west read to locate an SDK directly. Blanked (empty is
#: falsy for west's `os.environ.get` and ignored by `find_package`) rather than
#: removed, because `Runner` only overlays.
_SDK_POINTER_VARS = ("ZEPHYR_SDK_INSTALL_DIR", "Zephyr-sdk_DIR", "Zephyr-sdk_ROOT")

#: List-valued variables CMake derives search prefixes from.
_SDK_PREFIX_LIST_VARS = ("PATH", "CMAKE_PREFIX_PATH")


def _names_a_zephyr_sdk(entry: str, *, is_bin: bool) -> bool:
    """Whether a PATH-style entry would let CMake find a Zephyr SDK package:
    a `<sdk>/bin` on PATH (CMake searches the parent prefix) or an SDK root
    on CMAKE_PREFIX_PATH."""
    if not entry:
        return False
    base = Path(entry)
    root = base.parent if is_bin and base.name in ("bin", "sbin") else base
    try:
        return (root / "cmake" / "Zephyr-sdkConfig.cmake").is_file()
    except OSError:
        return False


def _without_sdk_entries(value: str, *, is_bin: bool) -> str:
    return os.pathsep.join(
        e for e in value.split(os.pathsep) if not _names_a_zephyr_sdk(e, is_bin=is_bin)
    )


@contextmanager
def scratch_home(root: Path, leaf: str, pid: int, *, enabled: bool) -> Iterator[Path | None]:
    """Create the scratch home and delete it on every exit. Yields `None` when
    disabled (`--dry-run`: nothing is spawned) or when it cannot be created --
    west then runs un-isolated, exactly as before tan-cli#1552, and the
    post-hoc [`reuse_warning`] still catches a reused SDK."""
    home = scratch_home_path(root, leaf, pid)
    created = False
    if enabled:
        try:
            home.mkdir(parents=True, exist_ok=True)
            created = True
        except OSError:
            pass
    try:
        yield home if created else None
    finally:
        if created:
            shutil.rmtree(home, ignore_errors=True)


def _real_netrc(real_home: str | None) -> str | None:
    if not real_home:
        return None
    for name in (".netrc", "_netrc"):
        candidate = Path(real_home) / name
        if candidate.is_file():
            return str(candidate)
    return None


def isolated_env(
    home: Path | None,
    base_extra: dict[str, str] | None,
    *,
    real_home: str | None,
    environ: dict[str, str] | os._Environ,
) -> dict[str, str] | None:
    """`extra_env` for the west call: the caller's own overlay (the staged
    `NETRC`) plus a scratch home.

    `HOME` is what CMake's `$ENV{HOME}` reads; `USERPROFILE` is what Python's
    `expanduser` and `setup.sh`-style tooling read on Windows, where `HOME` is
    usually unset. Both are set on every platform -- harmless where unused.

    A staged credential keeps working unchanged: `requests` reads `$NETRC`, not
    `~/.netrc`. When none is staged, the user's own netrc (hidden by the
    scratch home) is pointed at explicitly so an authenticated setup that
    worked before still does.
    """
    if home is None:
        return base_extra
    env = dict(base_extra or {})
    env["HOME"] = env["USERPROFILE"] = str(home)
    # The scratch home hides `$HOME` prefixes and the file registry, but an
    # SDK exported in the environment (or put on PATH, as the SDK's own
    # installer advises) is found just the same -- measured with `west sdk
    # list`: either one alone still lists the SDK.
    for name in _SDK_POINTER_VARS:
        if environ.get(name):
            env[name] = ""
    for name in _SDK_PREFIX_LIST_VARS:
        current = environ.get(name)
        if current:
            cleaned = _without_sdk_entries(current, is_bin=name == "PATH")
            if cleaned != current:
                env[name] = cleaned
    if "NETRC" not in env and not environ.get("NETRC"):
        netrc = _real_netrc(real_home)
        if netrc is not None:
            env["NETRC"] = netrc
    return env


def reused_sdk_candidates(version: str, real_home: str | None) -> list[str]:
    """Existing `zephyr-sdk-<version>` directories in the prefixes CMake scans,
    for naming the likely culprit. Registry-only (HKCU / file registry)
    reuse cannot be located from here."""
    prefixes = [Path(p) for p in _FIXED_PREFIXES]
    if real_home:
        prefixes += [Path(real_home) / rel if rel else Path(real_home) for rel in _HOME_PREFIXES]
    found: list[str] = []
    for prefix in prefixes:
        candidate = prefix / f"zephyr-sdk-{version}"
        try:
            if (candidate / "sdk_version").is_file() and str(candidate) not in found:
                found.append(str(candidate))
        except OSError:
            continue
    return found


def reuse_warning(version: str, tmp_dir: Path, real_home: str | None) -> str:
    """The `toolchain-install` text for "west exited 0 but installed nothing
    into `tmp_dir`" -- west very likely reused an SDK it already knew about."""
    found = reused_sdk_candidates(version, real_home)
    named = (
        "; the likely one is " + ", ".join(found)
        if found
        else " (no zephyr-sdk-" + version + " directory was found in the scanned "
        "prefixes, so it is probably known only through the CMake package registry)"
    )
    return (
        f"west sdk install exited 0 but wrote no sdk_version into {tmp_dir}: it most "
        f"likely printed \"Zephyr SDK version {version} is already installed at <path>. "
        f"Using it.\" and ignored --install-dir, because another Zephyr SDK {version} "
        f"is visible to CMake's package discovery{named}. Removing the scratch "
        f"directory will not help. Move or rename that SDK out of /usr, /usr/local, "
        f"/opt, $HOME, $HOME/.local, $HOME/.local/opt and $HOME/bin, or delete its "
        f"entry from the CMake user package registry (~/.cmake/packages/Zephyr-sdk/, "
        f"or HKEY_CURRENT_USER\\Software\\Kitware\\CMake\\Packages\\Zephyr-sdk on "
        f"Windows), then re-run `tan bootstrap`."
    )
