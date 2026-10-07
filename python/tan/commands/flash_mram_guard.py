# SPDX-License-Identifier: Apache-2.0
"""The IO half of Flow D's link-address guard (tan-cli#1371).

`tan.core.mram_link` decides; this finds the artefact's ELF, parses `slot0_load_address`
and resolves the MRAM aperture base from the SoC metadata (`soc_flash_base` -- the same
key `tan.planner.aperture` anchors on, never a literal).

**Scope, deliberately narrow.** The guard runs only for the Flow D shapes tan itself
controls: the mramxip `loadbin` at `slot0_load_address` and the SETOOLS auto-sign
(`mramAddress`). It does NOT run for an operator-supplied ATOC with no
`slot0_load_address` (that ATOC may carry a legitimate ITCM load entry), and it does
NOT run for Flow A (`zephyr_west_flash` on the `alif_flash` runner): alp-sdk's
`alif_flash.py` already refuses an unrecognised reset vector and supports images linked
at the ITCM global alias (0x58xxxxxx / 0x50xxxxxx) with an ATOC `loadAddress`, so an ELF
below `soc_flash_base` is not generally unbootable there. `west flash` generally and
`baremetal_cmake_flash` are likewise unchecked."""
from __future__ import annotations

import json
import os
from typing import Any

from tan.core.flash_plan import FLOW_D_METHOD, FlashPlanError, fa_str_checked
from tan.core.mram_link import mram_link_refusal

#: Ceiling on the bytes read: a Zephyr ELF with debug info is tens of MiB, never this.
MAX_ELF_BYTES = 512 * 1024 * 1024
_ELF_MAGIC = b"\x7fELF"


class ApertureUnresolved(Exception):
    """`soc_flash_base` could not be resolved; the message names the step that failed."""


def find_elf(artefact_path: str) -> bytes | None:
    """The bytes of the ELF behind a Flow D artefact: the artefact itself when it carries
    ELF magic, else the same-stem `.elf` beside a raw `.bin`. Read once.

    Limitation: the pairing is by file stem only. A `.bin` whose `.elf` sits elsewhere, or
    is named differently, is not found -- and a `.bin` with no ELF carries no link address,
    so it is not checked. `None` also for an unreadable or oversized file."""
    candidates = [artefact_path]
    stem, ext = os.path.splitext(artefact_path)
    if ext.lower() == ".bin":
        candidates.append(stem + ".elf")
    for path in candidates:
        try:
            if os.path.getsize(path) > MAX_ELF_BYTES:
                continue
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        if data[:4] == _ELF_MAGIC:
            return data
    return None


def slot0_address(flash_args: Any) -> int | None:
    """`flash_args.slot0_load_address` as an int, or `None` when absent or malformed (a
    malformed value is refused later by `validate_flow_d_shape` with its own message)."""
    try:
        text = fa_str_checked(flash_args, "slot0_load_address", True)
        return None if text is None else int(text, 16)
    except (FlashPlanError, ValueError):
        return None


def soc_flash_base(ctx: Any) -> int:
    """The MRAM aperture base: `soc_flash_base` of the SoC JSON the manifest SKU's SoM
    preset names, under the SDK in use. Raises [`ApertureUnresolved`] naming the step
    that failed (SDK/SKU unknown, preset not found, preset schema_version unsupported,
    SoC JSON not found, key missing)."""
    from tan.commands.size_cmd import _read_som_preset

    sdk_root, sku = getattr(ctx, "sdk_root", None), getattr(ctx, "sku", None)
    if not (sdk_root and sku):
        raise ApertureUnresolved("no SDK root or SoM SKU is known, so no SoC metadata can be read")
    metadata_root = os.path.join(sdk_root, "metadata")
    preset_path = os.path.join(metadata_root, "e1m_modules", f"{sku}.yaml")
    if not os.path.isfile(preset_path):
        raise ApertureUnresolved(f"the SoM preset for '{sku}' was not found at {preset_path}")
    preset = _read_som_preset(preset_path)
    if preset is None:
        raise ApertureUnresolved(
            f"the SoM preset {preset_path} is unreadable or its schema_version is unsupported"
        )
    parts = preset[0].split(":")
    if len(parts) != 3:
        raise ApertureUnresolved(f"the SoM preset {preset_path} names no 'vendor:family:part' silicon")
    soc_path = os.path.join(metadata_root, "socs", parts[0], parts[1], f"{parts[2]}.json")
    try:
        with open(soc_path, encoding="utf-8") as handle:
            base = json.load(handle).get("soc_flash_base")
    except FileNotFoundError:
        raise ApertureUnresolved(f"the SoC JSON was not found at {soc_path}") from None
    except (OSError, ValueError, AttributeError) as err:
        raise ApertureUnresolved(f"the SoC JSON {soc_path} is unreadable ({err})") from None
    if not isinstance(base, int) or isinstance(base, bool):
        raise ApertureUnresolved(f"the SoC JSON {soc_path} has no integer soc_flash_base key")
    return base


def mram_link_guard(
    artefact_path: str, entry_id: str, ctx: Any, *, slot0: int | None
) -> str | None:
    """The refusal for a Flow D entry whose ELF is loaded below its MRAM slot, else `None`.

    The floor is `slot0` (`slot0_load_address`) when known, else the SoC's
    `soc_flash_base`; an unresolvable base refuses, naming the failed step. The caller
    invokes this only for the shapes tan controls (see the module docstring)."""
    data = find_elf(artefact_path)
    if data is None:
        return None
    prefix = f"{FLOW_D_METHOD}[{entry_id}]: refusing -- "
    if slot0 is not None:
        floor, name = slot0, "slot0_load_address"
    else:
        try:
            floor, name = soc_flash_base(ctx), "the SoC's soc_flash_base"
        except ApertureUnresolved as err:
            return (
                f"{prefix}cannot verify the image's load address: {err}, so the MRAM "
                "aperture is unknown -- refusing to write an unchecked image."
            )
    message = mram_link_refusal(data, floor, name)
    return None if message is None else prefix + message
