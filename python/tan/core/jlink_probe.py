# SPDX-License-Identifier: Apache-2.0
"""J-Link probe selection for `tan flash` (tan-cli#1312). Pure decision logic
plus one read-only sysfs enumerator.

WHY THIS EXISTS. J-Link Commander selects an emulator by SERIAL only
(`SelectEmuBySN`). On the Alp Lab bench every probe carries the SAME OEM-cloned
serial, so a serial cannot tell two boards apart, and a Flow D MRAM write
issued with a shared serial lands on whichever probe the J-Link DLL opens
first. What DOES identify a probe on Linux is its USB port path (`3-4.2`, the
sysfs device name), and that path is not something J-Link can be told.

THE DESIGN (the honest one), in three layers:

1. SELECTION (`resolve_probe_selection`, from a sysfs enumeration). A
   `--probe-usb-path` is resolved to the serial sysfs reports for that port. A
   serial (CLI or `flash_args.jlink_serial`) is matched against the visible
   probes by its CANONICAL form (`canon_serial`: `603000869` and
   `000603000869` are the same serial -- YAML turns the unquoted one into an
   int). A serial no visible probe carries is refused; one that several visible
   probes carry is refused unless a USB path names the target.
2. VERIFICATION at spawn time (`verify_emulator_list`). Selection alone cannot
   make J-Link open the right probe when serials collide -- only isolation
   OUTSIDE tan can (a wrapper masking every other probe's `/dev/bus/usb` node
   AND sysfs directory in a private mount namespace). So immediately before
   EACH `JLinkExe` spawn tan runs the same binary read-only (`ShowEmuList`) and
   requires exactly one emulator whose canonical serial matches the selection.
   A masking wrapper on PATH passes; plain `JLinkExe` with cloned serials
   refuses. `TAN_PROBE_USB_PATH=<resolved path>` is exported in that spawn's
   environment so a wrapper can cross-check its own mask against it.
3. TOCTOU (`snapshot_drift`). The enumeration is repeated before each spawn
   and the run refuses if the set of (path, canonical serial) changed since
   selection.

Enumeration is strictly read-only (directory listing + reading two sysfs
attributes). It never opens a probe.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

#: SEGGER's USB vendor id as sysfs spells it.
SEGGER_VENDOR_ID = "1366"

SYSFS_USB_DEVICES = "/sys/bus/usb/devices"

#: A sysfs USB device name for a device on a hub port: `<bus>-<port>[.<port>...]`.
_USB_PATH_RE = re.compile(r"^[0-9]+-[0-9]+(\.[0-9]+)*$")

#: Exported into every `JLinkExe` spawn of a selected probe: the resolved USB
#: path, for a wrapper to cross-check its mask against.
USB_PATH_ENV = "TAN_PROBE_USB_PATH"

ISOLATION_VERIFIED = "verified-single-emulator"

CODE_AMBIGUOUS = "ambiguous"
CODE_NOT_FOUND = "not-found"
CODE_CONFLICT = "selector-conflict"

#: One `ShowEmuList` line: `J-Link[0]: Connection: USB, Serial number: 000603000869, ...`.
_EMU_SERIAL_RE = re.compile(r"Serial number:\s*([0-9A-Za-z]+)")


class ProbeEnumerationError(Exception):
    """A device directory that is populated but cannot be read (unreadable
    `idVendor`, or a SEGGER device with no readable `serial`). NOT the same as
    an empty directory, which is what a mount-namespace mask leaves behind."""


@dataclass(frozen=True)
class JLinkProbe:
    """One visible J-Link: its USB port path and the serial sysfs reports."""

    usb_path: str
    serial: str


@dataclass(frozen=True)
class ProbeSelection:
    """The outcome of resolving which probe a run will use.

    `refusal_code` is one of the `CODE_*` suffixes (the issue code is
    `flash.probe-<suffix>`) and is set iff the run must refuse before any write.
    `serial` is what Flow D must put in `SelectEmuBySN` (`None`: pin nothing).
    `candidates` lists every visible probe sharing `serial` when more than one
    does -- the case only spawn-time verification can settle.
    """

    serial: str | None
    usb_path: str | None
    source: str
    visible: int | None
    candidates: tuple[str, ...] = ()
    overrides_manifest_serial: str | None = None
    refusal_code: str | None = None
    refusal: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source,
            "serial": self.serial,
            "usbPath": self.usb_path,
            "visibleProbes": self.visible,
        }
        if self.overrides_manifest_serial is not None:
            out["overridesFlashArgsSerial"] = self.overrides_manifest_serial
        if self.candidates:
            out["candidates"] = list(self.candidates)
        return out

    def describe(self) -> str:
        who = self.serial if self.serial is not None else "(none pinned)"
        where = f" at USB path {self.usb_path}" if self.usb_path else ""
        shared = ""
        if len(self.candidates) > 1 and self.usb_path:
            shared = (
                f"; serial shared by {', '.join(self.candidates)} -- a real run needs the "
                "J-Link on PATH to show exactly one emulator with it (masking wrapper)"
            )
        return f"probe selection: serial {who}{where} via {self.source}{shared}"


def canon_serial(text: str) -> str:
    """The comparison form of a J-Link serial: stripped, and when all digits,
    without leading zeros (`000603000869` == `603000869`)."""
    text = text.strip()
    return str(int(text)) if text.isdigit() else text


def is_valid_usb_path(text: str) -> bool:
    return bool(_USB_PATH_RE.match(text))


def enumerate_jlinks(root: str | None = None) -> list[JLinkProbe] | None:
    """Every SEGGER device visible in sysfs, sorted by path. READ-ONLY.

    `None` means "this host cannot enumerate" (no sysfs USB tree: macOS,
    Windows, a container without sysfs) -- distinct from `[]`, "enumerated and
    found none". Raises `ProbeEnumerationError` for a populated device
    directory it cannot read -- never read as "no probe there"."""
    root = root or SYSFS_USB_DEVICES
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    probes: list[JLinkProbe] = []
    for name in names:
        # Interfaces (`3-4.2:1.0`) and root hubs (`usb3`) are not devices on a
        # port; only `<bus>-<port>` names are.
        if ":" in name or not _USB_PATH_RE.match(name):
            continue
        probe = _read_device(os.path.join(root, name), name)
        if probe is not None:
            probes.append(probe)
    return probes


def _read_device(base: str, name: str) -> JLinkProbe | None:
    try:
        populated = bool(os.listdir(base))
    except OSError:
        populated = False
    if not populated:
        return None  # an empty directory is what a namespace mask leaves
    vendor = _read_attr(os.path.join(base, "idVendor"))
    if vendor is None:
        raise ProbeEnumerationError(f"{name}: idVendor is unreadable")
    if vendor != SEGGER_VENDOR_ID:
        return None
    serial = _read_attr(os.path.join(base, "serial"))
    if not serial:
        raise ProbeEnumerationError(f"{name}: a SEGGER device whose serial is unreadable")
    return JLinkProbe(usb_path=name, serial=serial)


def _read_attr(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return None


def probe_set(probes: list[JLinkProbe] | None) -> frozenset[tuple[str, str]] | None:
    """The identity of an enumeration for the TOCTOU comparison."""
    if probes is None:
        return None
    return frozenset((p.usb_path, canon_serial(p.serial)) for p in probes)


def snapshot_drift(
    snapshot: frozenset[tuple[str, str]] | None, now: list[JLinkProbe] | None
) -> str | None:
    """A refusal when the visible probes changed between selection and spawn."""
    if snapshot is None:
        return None
    current = probe_set(now)
    if current == snapshot:
        return None
    return (
        "the visible J-Links changed between probe selection and the spawn "
        f"(was {_fmt_set(snapshot)}, now {_fmt_set(current)}); refusing rather than "
        "writing through a probe set that was not the one selected."
    )


def _fmt_set(items: frozenset[tuple[str, str]] | None) -> str:
    if items is None:
        return "unenumerable"
    return ", ".join(f"{p} ({s})" for p, s in sorted(items)) or "none"


def parse_show_emu_list(text: str) -> list[str]:
    """The serials a `ShowEmuList` output names, in order."""
    return _EMU_SERIAL_RE.findall(text)


def verify_emulator_list(serial: str, emu_serials: list[str]) -> str | None:
    """Spawn-time verification: exactly one listed emulator carries `serial`
    (canonically). `None` when so, else the refusal message."""
    want = canon_serial(serial)
    n = sum(1 for s in emu_serials if canon_serial(s) == want)
    if n == 1:
        return None
    if n == 0:
        return (
            f"J-Link's own ShowEmuList shows no emulator with serial {serial} "
            f"(saw: {', '.join(emu_serials) or 'none'}); refusing to write."
        )
    return (
        f"J-Link's own ShowEmuList shows {n} emulators with serial {serial}, so "
        "SelectEmuBySN would open whichever enumerates first -- possibly the wrong "
        "board. J-Link selects by serial only; run it where only the target probe is "
        "visible (a wrapper masking the other probes' /dev/bus/usb nodes AND sysfs "
        "directories in a private mount namespace) or give the probes distinct serials. "
        "flash_args.expect_dpidr stays mandatory on a shared-serial bench: it is the "
        "only check that the board answering is the one intended."
    )


def _paths(probes: list[JLinkProbe]) -> tuple[str, ...]:
    return tuple(p.usb_path for p in probes)


def _refuse(
    code: str,
    message: str,
    *,
    serial: str | None,
    usb_path: str | None,
    source: str,
    visible: int | None,
    candidates: tuple[str, ...] = (),
) -> ProbeSelection:
    return ProbeSelection(
        serial=serial,
        usb_path=usb_path,
        source=source,
        visible=visible,
        candidates=candidates,
        refusal_code=code,
        refusal=message,
    )


_HINT = (
    "Pass --probe-usb-path <bus-port> to name one probe. Note J-Link Commander selects "
    "by serial only, so with shared serials the write is additionally refused unless "
    "the J-Link on PATH is isolated to that one probe (a masking wrapper). "
    "flash_args.expect_dpidr stays mandatory on a shared-serial bench."
)


def resolve_probe_selection(
    probes: list[JLinkProbe] | None,
    *,
    cli_serial: str | None,
    cli_usb_path: str | None,
    manifest_serial: str | None,
) -> ProbeSelection:
    """Decide which J-Link a Flow D run will use, or that it must refuse.

    `probes` is `enumerate_jlinks()`'s answer (`None`: cannot enumerate).
    Precedence: `--probe-usb-path`, then `--probe-serial`, then
    `flash_args.jlink_serial`; the CLI selectors override the manifest for this
    run. Pure."""
    if cli_usb_path is not None:
        return _by_usb_path(probes, cli_usb_path, cli_serial, manifest_serial)
    serial = cli_serial if cli_serial is not None else manifest_serial
    if serial is not None:
        return _by_serial(probes, serial, cli_serial, manifest_serial)
    return _unselected(probes)


def _usb_path_refusal(
    probes: list[JLinkProbe] | None, path: str, cli_serial: str | None
) -> ProbeSelection | None:
    visible = None if probes is None else len(probes)
    if probes is None:
        return _refuse(
            CODE_NOT_FOUND,
            f"--probe-usb-path {path}: this host cannot enumerate USB devices (no "
            "sysfs), so tan cannot verify which probe that is. Refusing rather than "
            "writing through an unverified probe.",
            serial=None, usb_path=path, source="cli-usb-path", visible=None,
        )
    hit = next((p for p in probes if p.usb_path == path), None)
    if hit is None:
        return _refuse(
            CODE_NOT_FOUND,
            f"--probe-usb-path {path}: no J-Link is visible at that USB path. "
            f"Visible J-Links: {', '.join(_paths(probes)) or 'none'}.",
            serial=None, usb_path=path, source="cli-usb-path",
            visible=visible, candidates=_paths(probes),
        )
    if cli_serial is not None and canon_serial(cli_serial) != canon_serial(hit.serial):
        return _refuse(
            CODE_CONFLICT,
            f"--probe-usb-path {path} is a probe with serial {hit.serial}, but "
            f"--probe-serial {cli_serial} was also given. The two selectors name "
            "different probes; refusing.",
            serial=hit.serial, usb_path=path, source="cli-usb-path", visible=visible,
            candidates=_paths(
                [p for p in probes if canon_serial(p.serial) == canon_serial(hit.serial)]
            ),
        )
    return None


def _by_usb_path(
    probes: list[JLinkProbe] | None,
    path: str,
    cli_serial: str | None,
    manifest_serial: str | None,
) -> ProbeSelection:
    refusal = _usb_path_refusal(probes, path, cli_serial)
    if refusal is not None:
        return refusal
    assert probes is not None  # `_usb_path_refusal` refused the None case
    hit = next(p for p in probes if p.usb_path == path)
    sharing = [p for p in probes if canon_serial(p.serial) == canon_serial(hit.serial)]
    overrides = (
        manifest_serial
        if manifest_serial is not None
        and canon_serial(manifest_serial) != canon_serial(hit.serial)
        else None
    )
    return ProbeSelection(
        serial=hit.serial,
        usb_path=path,
        source="cli-usb-path",
        visible=len(probes),
        candidates=_paths(sharing) if len(sharing) > 1 else (),
        overrides_manifest_serial=overrides,
    )


def _by_serial(
    probes: list[JLinkProbe] | None,
    serial: str,
    cli_serial: str | None,
    manifest_serial: str | None,
) -> ProbeSelection:
    source = "cli-serial" if cli_serial is not None else "flash_args.jlink_serial"
    overrides = (
        manifest_serial
        if cli_serial is not None
        and manifest_serial is not None
        and canon_serial(manifest_serial) != canon_serial(cli_serial)
        else None
    )
    if probes is None:
        # Unverifiable host: the spawn-time ShowEmuList check is the only guard.
        return ProbeSelection(serial, None, source, None, overrides_manifest_serial=overrides)
    matches = [p for p in probes if canon_serial(p.serial) == canon_serial(serial)]
    if not matches:
        return _refuse(
            CODE_NOT_FOUND,
            f"serial {serial} ({source}): no visible J-Link carries that serial. "
            "Visible J-Links: "
            + (", ".join(f"{p.usb_path} ({p.serial})" for p in probes) or "none")
            + ".",
            serial=serial, usb_path=None, source=source, visible=len(probes),
            candidates=_paths(probes),
        )
    if len(matches) > 1:
        return _refuse(
            CODE_AMBIGUOUS,
            f"serial {serial} ({source}) is shared by {len(matches)} visible probes "
            f"({', '.join(_paths(matches))}), so J-Link would open whichever "
            "enumerates first -- possibly the wrong board. " + _HINT,
            serial=serial, usb_path=None, source=source, visible=len(probes),
            candidates=_paths(matches),
        )
    return ProbeSelection(
        matches[0].serial, matches[0].usb_path, source, len(probes),
        overrides_manifest_serial=overrides,
    )


def _unselected(probes: list[JLinkProbe] | None) -> ProbeSelection:
    if probes is None or not probes:
        return ProbeSelection(None, None, "unpinned", None if probes is None else 0)
    if len(probes) > 1:
        return _refuse(
            CODE_AMBIGUOUS,
            f"{len(probes)} J-Links are visible ({', '.join(_paths(probes))}) and nothing "
            "selects one: no --probe-usb-path, no --probe-serial, no "
            "flash_args.jlink_serial. Refusing before any write. " + _HINT,
            serial=None, usb_path=None, source="unpinned", visible=len(probes),
            candidates=_paths(probes),
        )
    only = probes[0]
    return ProbeSelection(only.serial, only.usb_path, "sole-visible", 1)
