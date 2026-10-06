# SPDX-License-Identifier: Apache-2.0
"""Per-run scratch overlay of an Alif SETOOLS install, the DEVICE-entry source
and the ATOC report parser -- the side-effect-free half of the Flow D sign step
(tan-cli#1325, tan-cli#1322, tan-cli#1318).

**Why a scratch tree (tan-cli#1325).** `app-gen-toc` writes its inputs and
outputs into the install it runs from: `build/images/`, `build/config/`,
`build/AppTocPackage.bin`, an APPEND-mode `build/app-package-map.txt` and
`build/logs/SBContent.log`. Running it inside the customer's shared install
overwrote a package another user of that install (a raw bench recipe, another
tan run, another slice) had left there. So every sign now runs in a private
overlay and the shared install is only ever READ.

**What the overlay is, measured against SETOOLS SE_FW_1.110.00.** The tool
`chdir`s into one of its own subdirectories and then addresses the output tree
as `../build/...` (its log line reads `Logging to ../build/logs/SBContent.log`),
so a symlinked SUBDIRECTORY would resolve `..` back into the shared install and
write there. Therefore:

* every top-level FILE (the `app-gen-*`/`app-write-mram` executables,
  `version.txt`, ...) is symlinked -- read-only and cwd-independent;
* every top-level DIRECTORY at or under [`_COPY_DIR_LIMIT_BYTES`] is COPIED
  (`utils/`, `cert/`, `bin/`: ~2.5 MiB measured), so its `..` is the scratch
  root; a directory over the limit (`alif/`, ~74 MiB of System Package images the
  signing step does not read) is symlinked, because copying it per run would
  cost more than the isolation of a directory the tool does not enter;
* `build/` is a FRESH real directory holding only what this one sign needs
  (`config/`, `images/`, `logs/`), so `app-package-map.txt` is never shared
  history and the post-sign report belongs to exactly this run.

Where a symlink cannot be created (Windows without the privilege) the file is
copied instead.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass

from tan.core.flash_plan import FlashPlanError

#: A top-level directory of the SETOOLS install larger than this is symlinked
#: instead of copied (see the module docstring).
_COPY_DIR_LIMIT_BYTES = 16 * 1024 * 1024

#: The stock device configuration SETOOLS ships, relative to the install root.
STOCK_DEVICE_CONFIG_REL = os.path.join("build", "config", "app-device-config.json")

#: The DEVICE entry's `version` in the bench recipe
#: (alp-sdk `scripts/bench/aen/flash-run.sh`).
DEVICE_ENTRY_VERSION = "0.5.00"

#: Registered issue code (`contract/issue-codes.json`) for the refusal below.
DEVICE_CONFIG_MISSING_CODE = "flash.device-config-missing"

_DEVICE_BASENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class DeviceConfigMissingError(FlashPlanError):
    """No device configuration could be found for the DEVICE ATOC entry.
    `code` is the registered issue code `tan flash` reports for it."""

    code = DEVICE_CONFIG_MISSING_CODE


@dataclass(frozen=True)
class DeviceConfig:
    """The resolved DEVICE-entry source: an absolute `path` (read-only) and a
    human `source` naming where it came from."""

    path: str
    source: str

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


def resolve_device_config(
    explicit: str | None, setools_dir: str, *, entry_id: str
) -> DeviceConfig:
    """`flash_args.setools_device_config` (already resolved to an absolute path
    by the caller) when set -- refusing, never falling through to the stock
    file, if it does not exist -- else SETOOLS' own stock
    `build/config/app-device-config.json`. Refuses with
    [`DeviceConfigMissingError`] when neither exists: the DEVICE entry carries the
    firewall/clock/boot configuration a table-replacing ATOC write would
    otherwise delete (tan-cli#1322), so dropping it silently is not an option.
    `--no-device-config` never reaches here."""
    if explicit:
        if not os.path.isfile(explicit):
            raise DeviceConfigMissingError(
                f"alif_mram_jlink[{entry_id}]: flash_args.setools_device_config is "
                f"'{explicit}' but no such file exists -- the DEVICE entry's source "
                "is never silently swapped for the stock config. Fix the path, or "
                "pass --no-device-config to sign an app-only ATOC (a table-replacing "
                "write then deletes the resident DEVICE entry)."
            )
        config = DeviceConfig(explicit, "flash_args.setools_device_config")
    else:
        stock = os.path.join(setools_dir, STOCK_DEVICE_CONFIG_REL)
        if not os.path.isfile(stock):
            raise DeviceConfigMissingError(
                f"alif_mram_jlink[{entry_id}]: no device configuration for the DEVICE "
                f"ATOC entry -- flash_args.setools_device_config is unset and SETOOLS "
                f"ships no '{stock}'. The ATOC write REPLACES the whole table, so a "
                "missing DEVICE entry deletes the resident one (firewall regions, HFXO "
                "trims, SE_BOOT_INFO). Point flash_args.setools_device_config at an "
                "Alif device-config JSON, or pass --no-device-config to knowingly sign "
                "an app-only ATOC."
            )
        config = DeviceConfig(stock, "the stock SETOOLS build/config/app-device-config.json")
    if _DEVICE_BASENAME_RE.match(config.name) is None:
        raise DeviceConfigMissingError(
            f"alif_mram_jlink[{entry_id}]: the device configuration file name "
            f"'{config.name}' is not a plain file name (letters, digits, '.', '_', '-')"
        )
    return config


def _dir_size(path: str, limit: int) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
            if total > limit:
                return total
    return total


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.symlink(src, dst, target_is_directory=os.path.isdir(src))
    except (OSError, NotImplementedError):
        if os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)


def make_scratch(setools_dir: str, parent: str | None = None) -> str:
    """Create a private overlay of `setools_dir` (see the module docstring) and
    return its root. The shared install is only read. Raises `OSError` on a
    filesystem failure; the half-built directory is removed first."""
    root = tempfile.mkdtemp(prefix="tan-setools-", dir=parent)
    try:
        for entry in os.scandir(setools_dir):
            if entry.name == "build":
                continue
            dst = os.path.join(root, entry.name)
            if entry.is_dir(follow_symlinks=False) and (
                _dir_size(entry.path, _COPY_DIR_LIMIT_BYTES) <= _COPY_DIR_LIMIT_BYTES
            ):
                shutil.copytree(entry.path, dst, symlinks=True)
            else:
                _link_or_copy(entry.path, dst)
        for sub in ("config", "images", "logs"):
            os.makedirs(os.path.join(root, "build", sub))
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root


def cleanup_scratch(path: str | None) -> bool:
    """Remove a scratch overlay made by [`make_scratch`] -- ONLY the overlay:
    symlinks are unlinked, never followed. `True` when nothing is left."""
    if not path:
        return True
    shutil.rmtree(path, ignore_errors=True)
    return not os.path.exists(path)


# ── the ATOC report ─────────────────────────────────────────────────────────

_TOC_ENTRY_RE = re.compile(
    r"APP TOC entry for\s+(\S+)\s+obj_address\s+(0x[0-9A-Fa-f]+)"
)
_PACKAGE_SIZE_RE = re.compile(r"APP Package total size:\s*(\d+)\s*bytes")


@dataclass(frozen=True)
class AtocReport:
    """What `app-package-map.txt` says about the package this run built:
    `entries` are `(name, obj_address)` per `APP TOC entry for` line (empty for
    a report that carries none), `package_size` the reported total in bytes."""

    entries: tuple[tuple[str, str], ...] = ()
    package_size: int | None = None


def parse_atoc_report(text: str) -> AtocReport:
    """Parse the entry list and total size out of an `app-package-map.txt`. The
    scratch report is fresh (one block), so every match belongs to this run."""
    entries = tuple((m.group(1), m.group(2)) for m in _TOC_ENTRY_RE.finditer(text))
    sizes = _PACKAGE_SIZE_RE.findall(text)
    return AtocReport(entries=entries, package_size=int(sizes[-1]) if sizes else None)
