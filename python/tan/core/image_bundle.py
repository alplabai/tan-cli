# SPDX-License-Identifier: Apache-2.0
"""Pure model + string builders for `tan image` -- port of
`crates/tan-core/src/image_bundle.rs` plus the `path_guard::is_plain_relative`
seam it depends on.

The IO (tar/gzip/sha256/copy/mkdir) lives in `tan.commands.image_cmd`; every
*decision* that does not touch the filesystem is here so it stays unit-testable:
the bundle-manifest shape + key order, the OS-independent forward-slash artefact
paths, and the ok-slice gate.

Two deliberate contract fixes carried over from the Rust:
  - the helper entry carries `chip` (the real schema field) instead of the
    perpetually-`null` `role` the retired Python emitted;
  - artefact paths are built as forward-slash STRINGS, never from a `Path`, so
    the machine contract never leaks a Windows `\\` separator.
"""

from __future__ import annotations

import os
import re
from typing import Any

#: Bundle-manifest `schema_version` (matches the system-manifest v1 line).
BUNDLE_SCHEMA_VERSION = 1
#: `generated_by` value -- tan-branded (the retired Python emitted `west alp-image`).
GENERATED_BY = "tan image"
#: Bundle root directory name, under the build root.
BUNDLE_DIR = "image-bundle"
#: Per-slice archive subdirectory name.
SLICES_DIR = "slices"
#: Helper-MCU firmware subdirectory name.
HELPERS_DIR = "helper-mcus"
#: Output bundle-manifest filename.
BUNDLE_MANIFEST = "bundle-manifest.json"

#: The not-yet-built sentinel a SoM preset uses for a helper firmware that has no
#: release yet. A legitimate state, so it is a warning, never a failure.
PENDING_SENTINEL = "TBD"

_SEPARATORS = re.compile(r"[/\\]")


def is_plain_relative(path: str) -> bool:
    """True when `path` is safe to join onto a trusted base: relative, and built
    only from ordinary names. Port of `tan_core::path_guard::is_plain_relative`.

    `os.path.isabs()` is NOT this check, for the same reason `Path::is_absolute()`
    is not: on Windows it is false for `/x`, `\\x` and `C:x`, none of which
    contains a `..` either, yet every one of them makes `base / p` discard part
    or all of `base`. An empty path and a bare `.` are rejected too: both make
    `base / p` resolve to `base` itself, so a caller meaning "a file under base"
    would act on base.

    Platform-dependent exactly as `Path::components()` is: `C:foo` is a drive
    prefix on Windows and an ordinary filename on POSIX.
    """
    if os.name == "nt":
        drive, rest = os.path.splitdrive(path)
        if drive:
            return False  # Component::Prefix -- `C:`, `C:/`, `\\\\server\\share`
        if rest[:1] in ("/", "\\"):
            return False  # Component::RootDir -- `\\x` and `/x` both
        parts = _SEPARATORS.split(rest)
    else:
        if path.startswith("/"):
            return False  # Component::RootDir
        parts = path.split("/")
    parts = [p for p in parts if p != ""]  # repeated separators collapse
    if not parts:
        return False  # "" and "." and "/" all yield nothing usable
    # `.` is normalized away EXCEPT at the start of the path, where it is a real
    # `Component::CurDir` -- so `a/./b` passes and `./a` does not.
    for index, part in enumerate(parts):
        if part == "." and index > 0:
            continue
        if part in (".", ".."):
            return False
    return True


def slice_archive_name(core_id: str, os_name: str) -> str | None:
    """Archive filename for a slice: `<core_id>-<os>.tar.gz`.

    `None` when `core_id`/`os` are not safe to fold into a filename that will be
    joined onto the bundle's `slices/` dir. Both come straight off
    system-manifest.yaml -- a deliberately tolerant reader, not a validator --
    and this used to interpolate them unchecked: a `core_id` of
    `../../../../home/u/x` or (Windows) `C:/x` escaped `image-bundle/slices/`
    entirely, so the caller's unlink + create could clobber an arbitrary
    `*.tar.gz` outside the build tree. The guard lives HERE, not at the call
    site, because [`slice_artefact_rel`] needs the identical check for the
    manifest's `artefact` string -- a per-call-site guard is how this class of
    bug keeps recurring.
    """
    if not is_plain_relative(core_id) or not is_plain_relative(os_name):
        return None
    return f"{core_id}-{os_name}.tar.gz"


def slice_artefact_rel(core_id: str, os_name: str) -> str | None:
    """Forward-slash relative artefact path: `slices/<core_id>-<os>.tar.gz`.
    `None` for the same unsafe shapes [`slice_archive_name`] rejects."""
    name = slice_archive_name(core_id, os_name)
    return None if name is None else f"{SLICES_DIR}/{name}"


def helper_artefact_rel(basename: str) -> str:
    """Forward-slash relative artefact path: `helper-mcus/<basename>`."""
    return f"{HELPERS_DIR}/{basename}"


def helper_firmware_candidates(
    raw: str, build_root: str, sdk_root: str | None
) -> list[tuple[str, str]]:
    """Labeled `(root, absolute_candidate)` pairs for a helper's declared
    `firmware_path`, in resolution order (alp-sdk#330).

    alp-sdk's `som-preset-v1.schema.json` defines a relative `firmware_path` as
    repository-relative -- relative to the SDK checkout, not to this project's
    build tree. `build_root` is still tried FIRST, ahead of `sdk_root`,
    mirroring the precedence `flash_plan.resolve_artefact_path` already
    established for the identical two-roots ambiguity (tan-cli#301, #322): a
    helper artefact the CURRENT build actually produced under `build/` is the
    one this run just made, and must win over a same-named file the SDK
    happens to ship. `sdk_root` is the fallback that lets a genuinely
    SDK-shipped firmware (the common case) resolve at all -- build-root-only
    resolution never could, which is the whole bug.

    An absolute `raw` yields itself alone, unchanged: `sdk_root` never applies
    to it (matches `system_manifest.anchor_under`'s own absolute passthrough).

    Returned in full even when nothing resolves, so the caller can name every
    root it tried and the absolute path each one produced (issue #330: the old
    message named only the raw relative string, so the reporter had to go find
    the file by hand to prove it existed).
    """
    if os.path.isabs(raw):
        return [("absolute path", raw)]
    candidates = [("build root", os.path.join(build_root, raw))]
    if sdk_root is not None:
        candidates.append(("sdk root", os.path.join(sdk_root, raw)))
    return candidates


def slice_should_bundle(status: Any) -> bool:
    """A slice is bundled iff it built successfully (`status == "ok"`)."""
    return status == "ok"


def slice_entry(
    core_id: str, os_name: str, artefact: str, sha256: str, size: int
) -> dict[str, Any]:
    """One bundled slice's manifest entry; key order IS the on-disk order."""
    return {
        "core_id": core_id,
        "os": os_name,
        "artefact": artefact,
        "sha256": sha256,
        "size": size,
    }


def helper_entry(
    name: str | None, chip: str | None, artefact: str, sha256: str, size: int
) -> dict[str, Any]:
    """One bundled helper firmware's manifest entry. `name`/`chip` are `null`
    only if the manifest omitted them -- and `chip`, not the retired Python's
    always-`null` `role`, is the field."""
    return {
        "name": name,
        "chip": chip,
        "artefact": artefact,
        "sha256": sha256,
        "size": size,
    }


def assemble_bundle_manifest(
    hw_info: dict,
    boot_order: list,
    slices: list[dict[str, Any]],
    helper_mcus: list[dict[str, Any]],
) -> dict[str, Any]:
    """The written `bundle-manifest.json`, and the envelope's `data`. Insertion
    order IS the key order: schema_version, generated_by, hw_info, slices,
    helper_mcus, boot_order. Pure: no IO."""
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "generated_by": GENERATED_BY,
        # Carried verbatim off the YAML so additive keys (`eeprom`, anything a
        # newer SDK adds) survive into the bundle -- see
        # `system_manifest.raw_passthrough`.
        "hw_info": hw_info,
        "slices": slices,
        "helper_mcus": helper_mcus,
        "boot_order": boot_order,
    }
