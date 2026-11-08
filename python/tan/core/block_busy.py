# SPDX-License-Identifier: Apache-2.0
"""Is a block device in use on this host?  The guard behind the yocto_wic
write refusal: a mistyped `flash_args.target` (`/dev/sda`) must never `dd`
over a mounted filesystem, active swap, or the disk backing either -- notably
the workstation's own root disk.

Ported from alp-sdk `scripts/flash_backends/yocto_wic.py`
(`_require_unused_device`, alp-sdk#3085); the two must refuse the same cases.

Three independent signals, any one refuses:

1. This namespace's mount table: /proc/self/mountinfo maj:min resolved through
   /sys/dev/block (so a root shown as `/dev/root` still maps to its disk) and
   /proc/swaps, followed through partition parents and dm/md slaves.
2. The kernel's own claim: `open(O_RDONLY | O_EXCL)` on the target and on each
   child partition fails with EBUSY while the kernel holds the device
   (mounted, swap, dm, md, LUKS) in ANY mount namespace -- a container handed
   the host's /dev/sdX does not see the host's mounts in mountinfo, but the
   claim is still there.
3. `/sys/class/block/<name>/holders` non-empty on the target or a child
   (an opened LUKS mapping, an assembled md array, an LV used by a VM).

The target is identified by device number (stat st_rdev ->
/sys/dev/block/maj:min -> kernel name), never by the basename of its path, so
a udev NAME= node or a mknod alias still matches.

FAILS CLOSED: if the mount table, /proc/swaps, sysfs, the target's device
number, or a claim probe cannot be read, the target cannot be proven unused
and is refused. The probe's errno decides: EBUSY = in use; ENOENT on a CHILD
partition node = that node does not exist here (the whole-disk open already
reports a claimed partition), skipped; every other errno (EACCES, EPERM,
ENOENT on the target itself, ...) = cannot tell, refused. A target that does
not exist at all is not this guard's business (nothing to overwrite; the
block-device gate owns that case) and is passed through. On a host without
/sys/dev/block (macOS, Windows) a real write is therefore always refused.

All host access goes through `HostFs`, so tests never touch the real host.
Not detected: multi-device btrfs members other than the one the kernel
reports, and ZFS pool members (the O_EXCL probe catches those on Linux).
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from dataclasses import dataclass

MOUNTINFO = "/proc/self/mountinfo"
SWAPS = "/proc/swaps"
SYS_BLOCK = "/sys/class/block"
SYS_DEV_BLOCK = "/sys/dev/block"
DEV = "/dev"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _rdev(path: str) -> tuple[int, int]:
    rdev = os.stat(path).st_rdev
    return os.major(rdev), os.minor(rdev)


def _open_excl(path: str) -> None:
    """Take and drop an exclusive claim; raises OSError(EBUSY) if held."""
    os.close(os.open(path, os.O_RDONLY | os.O_EXCL))


@dataclass(frozen=True)
class HostFs:
    """The host reads the check needs; defaults hit the real host."""

    read_text: Callable[[str], str] = _read
    realpath: Callable[[str], str] = os.path.realpath
    exists: Callable[[str], bool] = os.path.exists
    isdir: Callable[[str], bool] = os.path.isdir
    listdir: Callable[[str], list[str]] = os.listdir
    rdev: Callable[[str], tuple[int, int]] = _rdev
    open_excl: Callable[[str], None] = _open_excl


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
    Raises OSError when the mount table, /proc/swaps or sysfs cannot be read."""
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
    for ln in fs.read_text(SWAPS).splitlines()[1:]:
        src = ln.split()[0] if ln.strip() else ""
        if src.startswith("/dev/"):
            busy.append(("active swap", os.path.basename(fs.realpath(src))))
    return busy


def _children(fs: HostFs, name: str) -> list[str]:
    node = f"{SYS_BLOCK}/{name}"
    return [c for c in fs.listdir(node) if fs.exists(f"{node}/{c}/partition")]


def _claimed(fs: HostFs, name: str, *, child: bool) -> str | None:
    """Why `name` is held (holders / kernel claim), or None; OSError = cannot tell."""
    hdir = f"{SYS_BLOCK}/{name}/holders"
    if fs.isdir(hdir) and fs.listdir(hdir):
        return f"held by {', '.join(sorted(fs.listdir(hdir)))} (dm/md/LUKS/LV holders)"
    try:
        fs.open_excl(f"{DEV}/{name}")
    except OSError as exc:
        if exc.errno == errno.EBUSY:
            return "claimed by the kernel (mounted, swap, dm, md or LUKS)"
        if child and exc.errno == errno.ENOENT:
            return None
        raise
    return None


def unused_device_refusal(target: str, fs: HostFs | None = None) -> str | None:
    """A refusal message when `target` is in use or cannot be proven unused;
    None when it is provably unused (or does not exist)."""
    fs = fs or HostFs()
    cannot = f"yocto_wic: refusing target '{target}' -- cannot verify it is not in use ({{}}); write from a Linux host with /proc and /sys."
    try:
        major, minor = fs.rdev(target)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return cannot.format(exc)
    try:
        busy = _busy_devices(fs)
        link = f"{SYS_DEV_BLOCK}/{major}:{minor}"
        if not fs.exists(link):
            raise OSError(f"{link} not found")
        name = os.path.basename(fs.realpath(link))
        for what, dev in busy:
            if name in _family(fs, dev):
                return (
                    f"yocto_wic: refusing target '{target}' -- it is or backs a device "
                    f"in use ('{dev}', {what}); unmount it (and never target the "
                    "host's root disk)."
                )
        kids = _children(fs, name)
        for dev, is_child in [(name, False)] + [(k, True) for k in kids]:
            why = _claimed(fs, dev, child=is_child)
            if why:
                return (
                    f"yocto_wic: refusing target '{target}' -- '{dev}' is {why}, "
                    "possibly in another mount namespace; release it first."
                )
    except OSError as exc:
        return cannot.format(exc)
    return None
