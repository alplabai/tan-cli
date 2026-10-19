# SPDX-License-Identifier: Apache-2.0
"""Pure half of tan-cli#1552: the environment `west sdk install` runs under,
and the refusal when it reuses an SDK tan did not install.

`west sdk install` honours `--install-dir` ONLY when `fetch_sdk_info` finds no
SDK of the requested version (`scripts/west_commands/sdk.py`, `install_sdk`:
`if len(installed_info) == 0: ... download_and_extract(...)` else "is already
installed at ... Using it."). `fetch_sdk_info` runs `listsdk.cmake`, i.e.
`FindZephyr-sdk.cmake`, whose `find_package(Zephyr-sdk ... CONFIG PATHS ...)`
carries no `NO_DEFAULT_PATH`, so it searches:

- the fixed `PATHS` `/usr`, `/usr/local`, `/opt`, `$HOME`, `$HOME/.local`,
  `$HOME/.local/opt`, `$HOME/bin`;
- the CMake user package registry (`$HOME/.cmake/packages/Zephyr-sdk/` on
  POSIX, HKCU on Windows);
- `$Zephyr-sdk_DIR`, `$Zephyr-sdk_ROOT` / `$ZEPHYR-SDK_ROOT`,
  `$CMAKE_PREFIX_PATH`, cmake's own install prefix;
- every `PATH` entry as a prefix, an entry ending in `bin`/`sbin` standing for
  its parent -- so `~/zephyr-sdk-1.0.1/bin` on `PATH`, as the SDK's docs
  suggest, finds that SDK (measured with `cmake --debug-find-pkg=Zephyr-sdk`).

West then appends `$ZEPHYR_SDK_INSTALL_DIR` when it is non-empty. A reused SDK
leaves the `.tmp-*` install dir without `sdk_version`, and the run used to
fail as a "version mismatch" whose remedy (delete it, re-run) hit the same
branch forever.

The child therefore gets ([`isolated_west_sdk_env`]): a scratch `HOME`
(hiding the POSIX registry and every `$HOME` prefix), the SDK variables above
blanked, and `PATH` / `CMAKE_PREFIX_PATH` without entries that sit inside an
SDK root. On POSIX the scratch `HOME` also catches the registry entry west's
`setup.sh -c` writes for the `.tmp-*` path (`zephyr_sdk_export.cmake`:
`$ENV{HOME}/.cmake/packages`), which otherwise survived as a dead entry after
tan moved that directory, replacing whatever the user had registered for that
version. tan's own builds never need the entry: they set
`ZEPHYR_SDK_INSTALL_DIR` from the store (`tan.core.zephyr_env`).

What stays visible: the system prefixes, a `PATH` prefix that merely CONTAINS
a `zephyr-sdk*` directory (`~/bin` for `~/zephyr-sdk-*`; dropping
`~/.local/bin` or `/usr/local/bin` would drop `cmake` and `python`), cmake's
own install prefix, and on Windows the HKCU registry, which no environment
variable redirects -- `setup.cmd -c` still writes its `.tmp-*` entry there,
and `tan.core.sdk_registry` (tan-cli#1553) prunes it once the install
returns. A reuse through those is reported by [`reused_sdk_message`].

The scratch `HOME` also hides west's global config (`~/.westconfig`) and moves
Python's user site; the workspace venv's `west` reads neither for this command.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence

HOME_ENV_VAR = "HOME"

PATH_ENV_VAR = "PATH"

CMAKE_PREFIX_PATH_ENV_VAR = "CMAKE_PREFIX_PATH"

#: Every variable that hands west or `find_package` an SDK location directly;
#: each is blanked for the child. West adds `ZEPHYR_SDK_INSTALL_DIR` only when
#: it is non-empty (`fetch_sdk_info`); CMake reads the package `_DIR` and both
#: spellings of `_ROOT` (CMP0074, and the upper-case form since 3.27).
SDK_LOCATION_ENV_VARS: tuple[str, ...] = (
    "ZEPHYR_SDK_INSTALL_DIR",
    "Zephyr-sdk_DIR",
    "Zephyr-sdk_ROOT",
    "ZEPHYR-SDK_ROOT",
)

#: CMake strips one of these trailing components off a `PATH` entry to get the
#: search prefix.
_BIN_DIR_NAMES = frozenset({"bin", "sbin"})

_BARE_DRIVE = re.compile(r"[A-Za-z]:")

#: `requests` reads `$NETRC`, else `~/.netrc` then `~/_netrc` -- the same name
#: `toolchain_provision.NETRC_ENV_VAR` carries; repeated here so this module
#: stays importable on its own.
NETRC_ENV_VAR = "NETRC"
NETRC_FILENAMES: tuple[str, ...] = (".netrc", "_netrc")

#: Infix of the scratch home's directory name, after the store leaf and
#: `toolchain_provision.TMP_SUFFIX_PREFIX`, so the wreckage sweep that already
#: reclaims a killed install's `.tmp-*` sibling reclaims this one too.
SCRATCH_HOME_INFIX = "home-"

#: `FindZephyr-sdk.cmake`'s fixed search prefixes that a scratch `HOME` does
#: not hide.
SYSTEM_SDK_PREFIXES: tuple[str, ...] = ("/usr", "/usr/local", "/opt")


def path_prefix_of(entry: str) -> str:
    """The search prefix CMake derives from one `PATH` entry: its parent when
    the last component is `bin`/`sbin`, else the entry itself. A bare drive
    keeps its separator (`C:\\bin` -> `C:\\`, never the drive-relative `C:`)."""
    trimmed = entry.rstrip("/\\") or entry
    head, _, last = trimmed.replace("\\", "/").rpartition("/")
    if last.lower() not in _BIN_DIR_NAMES or not head:
        return trimmed
    prefix = trimmed[: len(head)]
    if _BARE_DRIVE.fullmatch(prefix):
        return trimmed[: len(head) + 1]
    return prefix


def without_sdk_roots(
    value: str, *, sep: str, is_sdk_root: Callable[[str], bool], strip_bin: bool
) -> str:
    """`value` (a `sep`-joined list) minus every entry whose search prefix is
    itself an SDK root, order kept. `strip_bin` for `PATH`, whose entries
    CMake turns into prefixes with [`path_prefix_of`]; `CMAKE_PREFIX_PATH`
    entries are prefixes as they stand."""

    def prefix(entry: str) -> str:
        return path_prefix_of(entry) if strip_bin else entry

    kept = [entry for entry in value.split(sep) if not (entry and is_sdk_root(prefix(entry)))]
    return sep.join(kept)


def isolated_west_sdk_env(
    scratch_home: str,
    *,
    user_netrc: str | None,
    filtered_lists: dict[str, str] | None = None,
) -> dict[str, str]:
    """The `extra_env` for the `west sdk install` child. `user_netrc` is the
    caller's real netrc when it has one and `$NETRC` is unset: moving `HOME`
    must not take a credential away from an unauthenticated run. A staged tan
    credential is merged over this and wins. `filtered_lists` carries the
    `PATH` / `CMAKE_PREFIX_PATH` values [`without_sdk_roots`] changed."""
    env = {HOME_ENV_VAR: scratch_home, **{var: "" for var in SDK_LOCATION_ENV_VARS}}
    if user_netrc is not None:
        env[NETRC_ENV_VAR] = user_netrc
    env.update(filtered_lists or {})
    return env


def reused_sdk_message(version: str, install_dir: str, candidates: Sequence[str]) -> str:
    """The `toolchain-install` refusal when west exited 0 but installed
    nothing into `install_dir`. Hedged: tan does not see west's own "already
    installed at" line in every mode, so `candidates` are what tan can see
    west still reaching, not proof of which one it used."""
    if candidates:
        where = f"Zephyr SDK {version} copies west can still reach: {', '.join(candidates)}."
    else:
        where = (
            f"tan cannot see which copy: west still searches "
            f"{', '.join(SYSTEM_SDK_PREFIXES)}, a PATH entry's prefix, cmake's own "
            f"install prefix and, on Windows, the CMake package registry "
            f"(HKEY_CURRENT_USER\\Software\\Kitware\\CMake\\Packages\\Zephyr-sdk)."
        )
    return (
        f"`west sdk install` exited 0 but installed nothing into {install_dir} -- "
        f"what it does when it finds Zephyr SDK {version} already installed and "
        f"reuses that copy (its own log names it: \"is already installed at ...\"). "
        f"{where} tan does not adopt an SDK it did not check against alp-sdk's "
        f"pins, and re-running alone will not change this: move that SDK out of "
        f"west's search path (or remove its registry entry) for one `tan bootstrap`, "
        f"then put it back."
    )
