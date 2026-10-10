# SPDX-License-Identifier: Apache-2.0
"""board.yaml SoC-capability pass (ALP-B010).

PORTED from alp-sdk `scripts/alp_cli/validator.py` (`_compat_pass` and its
SoC-capability helpers). Directories are passed in by the caller (the bound
SDK checkout's `metadata/`); nothing here derives a root from `__file__`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from tan.core.board_diagnostic import Diagnostic, DiagnosticCollector


def split_silicon_ref(silicon: str | None) -> tuple[str, str, str] | None:
    """Split a `vendor:family:part` `silicon:` key into its three slugs.

    None when *silicon* is falsy or not exactly 3 colon-separated parts
    (alp-sdk `alp_project_loader.split_silicon_ref`).
    """
    if not silicon:
        return None
    parts = silicon.split(":")
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def _compat_pass(
    data: dict[str, Any],
    path: Path,
    collector: DiagnosticCollector,
    *,
    som_dir: Path,
    soc_dir: Path,
) -> None:
    som = data.get("som")
    sku = som.get("sku") if isinstance(som, dict) else None
    silicon_ref = _silicon_ref_for_sku(sku, som_dir=som_dir)
    if silicon_ref is None:
        return
    soc_caps = _load_soc_caps(silicon_ref, soc_dir=soc_dir)
    if soc_caps is None:
        return

    # #602: `cores:` failing schema validation (wrong type, e.g. a string
    # or list instead of a mapping) is already reported as ALP-B004 --
    # guard here too instead of crashing on `.items()`.
    cores = data.get("cores")
    cores = cores if isinstance(cores, dict) else {}
    for core_name, core in cores.items():
        if not isinstance(core, dict):
            continue
        peripherals = core.get("peripherals") or []
        if not isinstance(peripherals, list):
            continue
        for idx, periph in enumerate(peripherals):
            # The board.yaml schema defines peripherals as an array of
            # strings (e.g. ["can", "i2c"]).  The schema pass already
            # rejects non-string entries, so skip anything unexpected.
            if not isinstance(periph, str):
                continue
            kind = periph
            if _soc_has_kind(soc_caps, kind):
                continue
            # Best-effort line: the sequence loader only attaches position
            # metadata to dict items; strings carry no __line__.  Fall back
            # to the core block's opening position.
            line = core.get("__line__", 1)
            col = core.get("__column__", 1)
            collector.add(
                Diagnostic(
                    # warning, not error: SoC peripherals JSON ingestion is
                    # incomplete for some parts, and some peripheral
                    # categories surface board-side rather than directly on
                    # the SoC (emmc / flash / ethernet via I/O controllers).
                    # A false-positive ALP-B010 must not block the build —
                    # surface the discrepancy and let the customer decide.
                    severity="warning",
                    path=path,
                    line=line,
                    col=col,
                    span=len(kind),
                    code="ALP-B010",
                    message=(
                        f"core '{core_name}': peripheral kind '{kind}' is not "
                        f"listed on silicon '{silicon_ref}' (SoC JSON may be "
                        f"incomplete or the peripheral is board-side)"
                    ),
                    hint=(
                        f"verify the SoC truly lacks {kind} before removing "
                        f"this entry; if the SoC has it but the metadata is "
                        f"stale, update metadata/socs/.../*.json"
                    ),
                )
            )


def _silicon_ref_for_sku(sku: str | None, *, som_dir: Path) -> str | None:
    if not sku:
        return None
    for som_path in som_dir.rglob(f"{sku}.yaml"):
        text = som_path.read_text(encoding="utf-8")
        doc = yaml.safe_load(text) or {}
        ref = doc.get("silicon")
        if isinstance(ref, str):
            return ref
    return None


def _load_soc_caps(silicon_ref: str, *, soc_dir: Path) -> dict[str, int] | None:
    # split_silicon_ref(), not resolve_soc_path(): this site roots at the
    # caller-injected `soc_dir`, not at a metadata root. Rebuilding it as
    # `resolve_soc_path(ref, soc_dir.parent)` happens to be exact today only
    # because every caller passes `<root>/socs` -- it would silently resolve
    # somewhere else the first time one didn't (#1096).
    parts = split_silicon_ref(silicon_ref)
    if parts is None:
        return None
    vendor, family, part = parts
    fp = soc_dir / vendor / family / f"{part}.json"
    if not fp.is_file():
        return None
    doc = json.loads(fp.read_text(encoding="utf-8"))
    peripherals = doc.get("peripherals", {}) if isinstance(doc, dict) else {}
    return peripherals if isinstance(peripherals, dict) else {}


# Map user-facing board.yaml peripheral kind names to the SoC JSON key
# prefixes that represent the underlying silicon capability.  Entries
# here are only needed when the user-facing name differs from the SoC
# JSON key (e.g. 'counter' is a Zephyr driver-model class that maps to
# the SoC's timer_* counters; 'sensor' is a driver-model class with no
# silicon peripheral equivalent so it is always considered present).
_PERIPHERAL_KIND_ALIASES: dict[str, tuple[str, ...]] = {
    # Zephyr COUNTER driver class backed by hardware timers.
    "counter": ("timer",),
    # Zephyr PWM driver class backed by hardware timers / PWM units.
    "pwm": ("timer", "pwm"),
    # Zephyr SENSOR driver class: software abstraction over various
    # I2C/SPI sensors; no dedicated silicon peripheral block required.
    # Always allow it -- the I2C/SPI bus is the actual constraint.
    "sensor": (),  # empty tuple = unconditionally present
}


def _soc_has_kind(caps: dict[str, int], kind: str) -> bool:
    """Map a peripheral 'kind' onto SoC capability keys.

    'kind' is the user-facing peripheral category (i2c, spi, can, ...).
    The SoC JSON uses the same names plus _lp suffixes for low-power
    variants; presence in EITHER counts.  Some peripherals also have
    variant suffixes (e.g. can_fd) -- if the base name matches a key
    whose value > 0 that also counts.

    High-level Zephyr driver classes (counter, pwm, sensor) that do not
    map 1-to-1 to SoC JSON keys are resolved via ``_PERIPHERAL_KIND_ALIASES``.
    """
    # Alias table: if the kind has a (possibly empty) alias list, check
    # those SoC key prefixes instead.  An empty alias list means the kind
    # is always considered present (software-only abstraction).
    if kind in _PERIPHERAL_KIND_ALIASES:
        aliases = _PERIPHERAL_KIND_ALIASES[kind]
        if not aliases:
            return True  # unconditionally present (e.g. 'sensor')
        for alias_prefix in aliases:
            direct_keys = (alias_prefix, f"{alias_prefix}_lp")
            if any((caps.get(k, 0) or 0) > 0 for k in direct_keys):
                return True
            for key, count in caps.items():
                if key == alias_prefix or key.startswith(f"{alias_prefix}_"):
                    if (count or 0) > 0:
                        return True
        return False

    # Direct and LP-suffixed match.
    direct_keys = (kind, f"{kind}_lp")
    if any((caps.get(k, 0) or 0) > 0 for k in direct_keys):
        return True
    # Variant-suffixed match: e.g. user writes 'can', SoC JSON has 'can_fd'.
    for key, count in caps.items():
        if key == kind or key.startswith(f"{kind}_"):
            if (count or 0) > 0:
                return True
    return False

