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
* every top-level DIRECTORY is COPIED (`bin/`, `cert/`, `utils/`: ~2.5 MiB
  measured -- the tool writes there, e.g. its logs), so its `..` is the scratch
  root. The ONLY exceptions are an explicit read-only allowlist,
  [`_LINK_DIRS`] (`alif/`: ~74 MiB of System Package images the signing step
  does not read, so copying it per run would cost more than it isolates), and
  `utils/key`, the signing keys, which are symlinked INTO the copied `utils/`
  rather than duplicated into the temp directory (measured: `app-gen-toc` signs
  correctly through the link and the shared install stays byte-identical);
* `build/` is a FRESH real directory holding only what this one sign needs
  (`config/`, `images/`, `logs/`), so `app-package-map.txt` is never shared
  history and the post-sign report belongs to exactly this run.

Where a symlink cannot be created (Windows without the privilege) the file is
copied instead.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass

from tan.core.flash_plan import FlashPlanError

#: Top-level directories that are symlinked, not copied: read-only to the sign
#: step (see the module docstring). Everything else is copied.
_LINK_DIRS = frozenset({"alif"})

#: Directory, relative to the install root, whose contents are key material:
#: linked into the copy instead of duplicated.
_KEY_DIR = os.path.join("utils", "key")

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
    """The resolved DEVICE-entry source: an absolute `path` (read-only), a human
    `source` naming where it came from, and the `metadata.device` the file itself
    declares (`None` when it does not parse)."""

    path: str
    source: str
    metadata_device: str | None = None

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


def read_metadata_device(path: str) -> str | None:
    """`metadata.device` out of a device-config JSON, or `None`."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        device = doc["metadata"]["device"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return device if isinstance(device, str) else None


def family_mismatch(metadata_device: str | None, flash_device: str | None) -> str | None:
    """A warning when the device configuration was written for a different Alif
    family than `flash_device` (the J-Link part profile): the part numbers share
    their first three characters within a family (`AE8...` Ensemble E8). `None`
    when they agree or either is unknown."""
    if not metadata_device or not flash_device:
        return None
    if metadata_device[:3].upper() == flash_device[:3].upper():
        return None
    return (
        f"the device configuration declares device '{metadata_device}' but this slice's J-Link "
        f"part profile is '{flash_device}' -- a different Alif family. The DEVICE entry carries "
        "firewall, clock and boot settings. This is a warning, not a refusal: the metadata may be "
        "stale (the stock SETOOLS file declares an E7 part, yet its compiled blob is byte-identical "
        "to the E8 board's original DEVICE entry). Confirm it is the right file (it is the stock "
        "SETOOLS config unless flash_args.setools_device_config names another)."
    )


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
    config = DeviceConfig(config.path, config.source, read_metadata_device(config.path))
    if _DEVICE_BASENAME_RE.match(config.name) is None:
        raise DeviceConfigMissingError(
            f"alif_mram_jlink[{entry_id}]: the device configuration file name "
            f"'{config.name}' is not a plain file name (letters, digits, '.', '_', '-')"
        )
    return config


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
        shared_key = os.path.join(setools_dir, _KEY_DIR)
        key_abs = os.path.abspath(shared_key)

        def _skip_keys(directory: str, names: list[str]) -> list[str]:
            # The signing keys are never copied: a kill between a copy and its removal would
            # leave them in the temp directory (tan-cli#1485). They are linked in below.
            return [n for n in names if os.path.abspath(os.path.join(directory, n)) == key_abs]

        for entry in os.scandir(setools_dir):
            if entry.name == "build":
                continue
            dst = os.path.join(root, entry.name)
            if entry.is_dir(follow_symlinks=False) and entry.name not in _LINK_DIRS:
                shutil.copytree(entry.path, dst, symlinks=True, ignore=_skip_keys)
            else:
                _link_or_copy(entry.path, dst)
        scratch_utils = os.path.dirname(os.path.join(root, _KEY_DIR))
        if os.path.isdir(shared_key) and os.path.isdir(scratch_utils):
            _link_or_copy(shared_key, os.path.join(root, _KEY_DIR))
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
    # `app-gen-toc` pads the name field with NULs ("DEVICE\x00\x00"); they are not
    # part of the name (bench round 6).
    entries = tuple(
        (m.group(1).replace("\x00", ""), m.group(2)) for m in _TOC_ENTRY_RE.finditer(text)
    )
    sizes = _PACKAGE_SIZE_RE.findall(text)
    return AtocReport(entries=entries, package_size=int(sizes[-1]) if sizes else None)
