# SPDX-License-Identifier: Apache-2.0
"""Is a block device in use on this host?  The guard behind the yocto_wic
write refusal: a mistyped `flash_args.target` (`/dev/sda`) must never `dd`
over a mounted filesystem, active swap, or the disk backing either -- notably
the workstation's own root disk.

Ported from alp-sdk `scripts/flash_backends/yocto_wic.py`
(`_require_unused_device`, alp-sdk#3085). Mounts are identified by the maj:min
in /proc/self/mountinfo resolved through /sys/dev/block, so a root shown as
`/dev/root` still maps to its disk. FAILS CLOSED: when the mount table or sysfs
cannot be read the target cannot be proven unused, so it is refused.

All host access goes through `HostFs`, so tests never touch the real host.
Not detected: multi-device btrfs members other than the one the kernel reports,
and ZFS pool members.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

MOUNTINFO = "/proc/self/mountinfo"
SWAPS = "/proc/swaps"
SYS_BLOCK = "/sys/class/block"
SYS_DEV_BLOCK = "/sys/dev/block"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@dataclass(frozen=True)
class HostFs:
    """The five host reads the check needs; defaults hit the real host."""

    read_text: Callable[[str], str] = _read
    realpath: Callable[[str], str] = os.path.realpath
    exists: Callable[[str], bool] = os.path.exists
    isdir: Callable[[str], bool] = os.path.isdir
    listdir: Callable[[str], list[str]] = os.listdir


def _family(fs: HostFs, name: str, seen: set[str] | None = None) -> set[str]:
    """`name` plus every whole disk beneath it (partition parent, dm/md slaves)."""
    seen = seen if seen is not None else set()
    if name in seen:
        return seen
    seen.add(name)
    node = f"{SYS_BLOCK}/{name}"
    try:
        if fs.exists(f"{node}/partition"):
            _family(fs, os.path.basename(os.path.dirname(fs.realpath(node))), seen)
        if fs.isdir(f"{node}/slaves"):
            for slave in fs.listdir(f"{node}/slaves"):
                _family(fs, slave, seen)
    except OSError:
        pass
    return seen


def _busy_devices(fs: HostFs) -> list[tuple[str, str]]:
    """`(what, kernel block name)` per mounted filesystem and swap area.
    Raises OSError when the mount table or sysfs cannot be read."""
    if not fs.isdir(SYS_DEV_BLOCK):
        raise OSError(f"{SYS_DEV_BLOCK} not available")
    busy: list[tuple[str, str]] = []
    for ln in fs.read_text(MOUNTINFO).splitlines():
        fields = ln.split()
        if len(fields) < 5:
            continue
        majmin, mountpoint = fields[2], fields[4]
        dev = f"{SYS_DEV_BLOCK}/{majmin}"
        if fs.exists(dev):
            busy.append((f"mounted at {mountpoint}", os.path.basename(fs.realpath(dev))))
        elif " - " in ln:
            # Anonymous device numbers (btrfs, overlay, tmpfs): fall back to
            # the mount source after the " - " separator when it is a /dev node.
            tail = ln.split(" - ", 1)[1].split()
            if len(tail) >= 2 and tail[1].startswith("/dev/"):
                busy.append((f"mounted at {mountpoint}", os.path.basename(fs.realpath(tail[1]))))
    try:
        for ln in fs.read_text(SWAPS).splitlines()[1:]:
            src = ln.split()[0] if ln.strip() else ""
            if src.startswith("/dev/"):
                busy.append(("active swap", os.path.basename(fs.realpath(src))))
    except OSError:
        pass
    return busy


def unused_device_refusal(target: str, fs: HostFs | None = None) -> str | None:
    """A refusal message when `target` is mounted, used as swap, or is (part
    of) the disk backing a mounted filesystem; None when it is provably unused."""
    fs = fs or HostFs()
    try:
        busy = _busy_devices(fs)
    except OSError as exc:
        return (
            f"yocto_wic: refusing target '{target}' -- cannot verify it is not "
            f"mounted ({exc}); write from a Linux host with /proc and /sys."
        )
    name = os.path.basename(fs.realpath(target))
    for what, dev in busy:
        if name in _family(fs, dev):
            return (
                f"yocto_wic: refusing target '{target}' -- it is or backs a device "
                f"in use ('{dev}', {what}); unmount it (and never target the "
                "host's root disk)."
            )
    return None
