# SPDX-License-Identifier: Apache-2.0
"""J-Link probe selection for `tan flash` (tan-cli#1312). Pure decision logic
plus one read-only sysfs enumerator.

WHY THIS EXISTS. J-Link Commander selects an emulator by SERIAL only
(`SelectEmuBySN`). On the Alp Lab bench every probe carries the SAME OEM-cloned
serial, so a serial cannot tell two boards apart, and a Flow D MRAM write
issued with a shared serial lands on whichever probe the J-Link DLL opens
first. What DOES identify a probe on Linux is its USB port path (`3-4.2`, the
sysfs device name), and that path is not something J-Link can be told.

THE DESIGN (the honest one). tan resolves a `--probe-usb-path` to the serial
sysfs reports for that port and feeds THAT to J-Link -- but only when the
serial is unique among the visible J-Links. When other visible probes share
the serial, tan cannot address the one at the named path, and it REFUSES
(`flash.probe-ambiguous`) naming the collision, rather than pretending the
path was honoured. The way out is isolation OUTSIDE tan: a wrapper that masks
every other probe (their `/dev/bus/usb` node AND their sysfs directory) in a
private mount namespace, so exactly one probe is visible. Run tan inside such a
namespace and enumeration sees one probe: the shared serial is then
unambiguous. If instead the wrapper stands in for the `JLinkExe` binary, tan
itself still sees every probe, so the caller may assert the isolation with
`TAN_PROBE_ISOLATED=1`; that assertion is unverifiable by tan, is the only
thing it relaxes (the shared-serial refusal for an explicitly named USB path,
which must still exist), and is echoed in the envelope.

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

#: The env var a caller sets to assert it runs tan against a probe-masking
#: wrapper that tan cannot see from here. See the module docstring.
ISOLATED_ENV = "TAN_PROBE_ISOLATED"

#: Test seam: read the USB device tree from this directory instead of
#: `/sys/bus/usb/devices`, so a suite that spawns real `tan` subprocesses never
#: depends on which J-Links the host happens to have plugged in.
SYSFS_ROOT_ENV = "TAN_USB_SYSFS_ROOT"

CODE_AMBIGUOUS = "ambiguous"
CODE_NOT_FOUND = "not-found"
CODE_CONFLICT = "selector-conflict"


@dataclass(frozen=True)
class JLinkProbe:
    """One visible J-Link: its USB port path and the serial sysfs reports
    (`None` when the attribute is missing/unreadable)."""

    usb_path: str
    serial: str | None


@dataclass(frozen=True)
class ProbeSelection:
    """The outcome of resolving which probe a run will use.

    `refusal_code` is one of the `CODE_*` suffixes (the issue code is
    `flash.probe-<suffix>`) and is set iff the run must refuse before any write.
    `serial` is what Flow D must put in `SelectEmuBySN` (`None`: pin nothing).
    """

    serial: str | None
    usb_path: str | None
    source: str
    visible: int | None
    candidates: tuple[str, ...] = ()
    isolation_asserted: bool = False
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
        if self.isolation_asserted:
            out["isolationAsserted"] = True
        if self.overrides_manifest_serial is not None:
            out["overridesFlashArgsSerial"] = self.overrides_manifest_serial
        if self.candidates:
            out["candidates"] = list(self.candidates)
        return out

    def describe(self) -> str:
        who = self.serial if self.serial is not None else "(none pinned)"
        where = f" at USB path {self.usb_path}" if self.usb_path else ""
        return f"probe selection: serial {who}{where} via {self.source}"


def is_valid_usb_path(text: str) -> bool:
    return bool(_USB_PATH_RE.match(text))


def isolation_asserted(environ: dict[str, str] | os._Environ) -> bool:
    return environ.get(ISOLATED_ENV, "") == "1"


def enumerate_jlinks(root: str | None = None) -> list[JLinkProbe] | None:
    """Every SEGGER device visible in sysfs, sorted by path. READ-ONLY.

    `None` means "this host cannot enumerate" (no sysfs USB tree: macOS,
    Windows, a container without sysfs) -- distinct from `[]`, "enumerated and
    found none", because the caller must not read an unverifiable host as an
    empty bench."""
    root = root or os.environ.get(SYSFS_ROOT_ENV) or SYSFS_USB_DEVICES
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
        base = os.path.join(root, name)
        if _read_attr(os.path.join(base, "idVendor")) != SEGGER_VENDOR_ID:
            continue
        serial = _read_attr(os.path.join(base, "serial"))
        probes.append(JLinkProbe(usb_path=name, serial=serial or None))
    return probes


def _read_attr(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return None


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


_ISOLATION_HINT = (
    "J-Link Commander selects by serial only, so tan cannot address one of several "
    "same-serial probes by USB path. Run tan where only the target probe is visible "
    "(a wrapper that masks the other probes' /dev/bus/usb nodes AND their sysfs "
    "directories in a private mount namespace), or give the probes distinct serials."
)


def resolve_probe_selection(
    probes: list[JLinkProbe] | None,
    *,
    cli_serial: str | None,
    cli_usb_path: str | None,
    manifest_serial: str | None,
    isolated: bool = False,
) -> ProbeSelection:
    """Decide which J-Link a Flow D run will use, or that it must refuse.

    `probes` is `enumerate_jlinks()`'s answer (`None`: cannot enumerate).
    Precedence: `--probe-usb-path`, then `--probe-serial`, then
    `flash_args.jlink_serial`; the CLI selectors override the manifest for this
    run. Pure."""
    if cli_usb_path is not None:
        return _by_usb_path(probes, cli_usb_path, cli_serial, manifest_serial, isolated)
    serial = cli_serial if cli_serial is not None else manifest_serial
    if serial is not None:
        return _by_serial(probes, serial, cli_serial, manifest_serial)
    return _unselected(probes)


def _usb_path_refusal(
    probes: list[JLinkProbe] | None, path: str, cli_serial: str | None
) -> ProbeSelection | None:
    """The refusals that do not depend on the shared-serial question."""
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
    if hit.serial is None:
        return _refuse(
            CODE_NOT_FOUND,
            f"--probe-usb-path {path}: the probe's serial is unreadable from sysfs, so "
            "J-Link has nothing to select it by.",
            serial=None, usb_path=path, source="cli-usb-path", visible=visible,
        )
    if cli_serial is not None and cli_serial != hit.serial:
        return _refuse(
            CODE_CONFLICT,
            f"--probe-usb-path {path} is a probe with serial {hit.serial}, but "
            f"--probe-serial {cli_serial} was also given. The two selectors name "
            "different probes; refusing.",
            serial=hit.serial, usb_path=path, source="cli-usb-path", visible=visible,
        )
    return None


def _by_usb_path(
    probes: list[JLinkProbe] | None,
    path: str,
    cli_serial: str | None,
    manifest_serial: str | None,
    isolated: bool,
) -> ProbeSelection:
    refusal = _usb_path_refusal(probes, path, cli_serial)
    if refusal is not None:
        return refusal
    assert probes is not None  # `_usb_path_refusal` refused the None case
    hit = next(p for p in probes if p.usb_path == path)
    sharing = [p for p in probes if p.serial == hit.serial]
    if len(sharing) > 1 and not isolated:
        return _refuse(
            CODE_AMBIGUOUS,
            f"--probe-usb-path {path}: serial {hit.serial} is shared by "
            f"{len(sharing)} visible probes ({', '.join(_paths(sharing))}). "
            + _ISOLATION_HINT,
            serial=hit.serial, usb_path=path, source="cli-usb-path",
            visible=len(probes), candidates=_paths(sharing),
        )
    return ProbeSelection(
        serial=hit.serial,
        usb_path=path,
        source="cli-usb-path",
        visible=len(probes),
        candidates=_paths(sharing) if len(sharing) > 1 else (),
        isolation_asserted=isolated and len(sharing) > 1,
        overrides_manifest_serial=(
            manifest_serial if manifest_serial not in (None, hit.serial) else None
        ),
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
        if cli_serial is not None and manifest_serial not in (None, cli_serial)
        else None
    )
    if probes is None:
        # Unverifiable host: J-Link itself selects by the serial, as before.
        return ProbeSelection(serial, None, source, None, overrides_manifest_serial=overrides)
    visible = len(probes)
    matches = [p for p in probes if p.serial == serial]
    if not matches and cli_serial is not None:
        return _refuse(
            CODE_NOT_FOUND,
            f"--probe-serial {cli_serial}: no visible J-Link carries that serial. "
            "Visible J-Links: "
            + (", ".join(f"{p.usb_path} ({p.serial})" for p in probes) or "none")
            + ".",
            serial=serial, usb_path=None, source=source, visible=visible,
            candidates=_paths(probes),
        )
    if len(matches) > 1:
        return _refuse(
            CODE_AMBIGUOUS,
            f"serial {serial} ({source}) is shared by {len(matches)} visible probes "
            f"({', '.join(_paths(matches))}), so J-Link would open whichever "
            "enumerates first -- possibly the wrong board. Pass --probe-usb-path "
            "<bus-port> to name one. " + _ISOLATION_HINT,
            serial=serial, usb_path=None, source=source, visible=visible,
            candidates=_paths(matches),
        )
    return ProbeSelection(
        serial, matches[0].usb_path if matches else None, source, visible,
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
            "flash_args.jlink_serial. Refusing before any write. Pass --probe-usb-path "
            "<bus-port>.",
            serial=None, usb_path=None, source="unpinned", visible=len(probes),
            candidates=_paths(probes),
        )
    return ProbeSelection(None, probes[0].usb_path, "sole-visible", 1)
