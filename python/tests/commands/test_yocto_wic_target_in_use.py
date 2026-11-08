# SPDX-License-Identifier: Apache-2.0
"""The yocto_wic write refuses a block device that is mounted, swap, or backs
a mounted filesystem (the host root disk), and refuses when it cannot tell.
Ported from alp-sdk#3085; the host is a dict-backed fake, never the real one."""

from __future__ import annotations

import os
import stat
import types

from tan.commands import flash_cmd
from tan.core import block_busy
from tan.core.block_busy import HostFs, unused_device_refusal

ROOT = "22 1 8:2 / / rw,relatime - ext4 /dev/root rw\n"
PROC = "23 22 0:21 / /proc rw - proc proc rw\n"
NO_SWAP = "Filename Type Size Used Priority\n"
SYS = block_busy.SYS_BLOCK
DEVBLK = block_busy.SYS_DEV_BLOCK


def fake_host(mountinfo: str, swaps: str = NO_SWAP, *, readable: bool = True) -> HostFs:
    """sda (sda2 = root, sda3), sdb (sdb1), dm-0 on sda3; kernel-style links."""
    links = {
        f"{DEVBLK}/8:0": "/sys/devices/sda",
        f"{DEVBLK}/8:2": "/sys/devices/sda/sda2",
        f"{DEVBLK}/8:3": "/sys/devices/sda/sda3",
        f"{DEVBLK}/8:16": "/sys/devices/sdb",
        f"{DEVBLK}/8:17": "/sys/devices/sdb/sdb1",
        f"{DEVBLK}/253:0": "/sys/devices/virtual/dm-0",
        f"{SYS}/sda2": "/sys/devices/sda/sda2",
        f"{SYS}/sda3": "/sys/devices/sda/sda3",
        f"{SYS}/sdb1": "/sys/devices/sdb/sdb1",
        "/dev/root": "/dev/sda2",
    }
    files = {block_busy.MOUNTINFO: mountinfo, block_busy.SWAPS: swaps}
    partitions = {f"{SYS}/{n}/partition" for n in ("sda2", "sda3", "sdb1")}
    slaves = {f"{SYS}/dm-0/slaves": ["sda3"]}

    def read_text(path: str) -> str:
        if not readable or path not in files:
            raise FileNotFoundError(path)
        return files[path]

    return HostFs(
        read_text=read_text,
        realpath=lambda p: links.get(p, p),
        exists=lambda p: p in links or p in partitions,
        isdir=lambda p: readable and (p == DEVBLK or p in slaves),
        listdir=lambda p: slaves[p],
    )


def test_a_mounted_partition_target_is_refused():
    fs = fake_host(PROC + "40 22 8:17 / /mnt rw - ext4 /dev/sdb1 rw\n")
    err = unused_device_refusal("/dev/sdb1", fs)
    assert err is not None and "mounted at /mnt" in err


def test_a_whole_disk_whose_partition_is_mounted_is_refused():
    fs = fake_host(PROC + "40 22 8:17 / /mnt rw - ext4 /dev/sdb1 rw\n")
    err = unused_device_refusal("/dev/sdb", fs)
    assert err is not None and "sdb1" in err


def test_the_root_disk_shown_as_dev_root_is_refused_whole_or_partition():
    fs = fake_host(ROOT + PROC)
    assert unused_device_refusal("/dev/sda", fs) is not None
    assert unused_device_refusal("/dev/sda2", fs) is not None


def test_a_disk_under_a_mounted_device_mapper_node_is_refused():
    fs = fake_host("41 22 253:0 / /home rw - ext4 /dev/mapper/vg-home rw\n")
    assert unused_device_refusal("/dev/sda", fs) is not None


def test_an_active_swap_partition_is_refused():
    fs = fake_host(PROC, "Filename Type Size Used Priority\n/dev/sdb1 partition 1024 0 -2\n")
    err = unused_device_refusal("/dev/sdb", fs)
    assert err is not None and "active swap" in err


def test_an_unused_device_is_allowed():
    assert unused_device_refusal("/dev/sdb", fake_host(ROOT + PROC)) is None


def test_an_unreadable_mount_table_is_refused_fail_closed():
    err = unused_device_refusal("/dev/sdb", fake_host(PROC, readable=False))
    assert err is not None and "cannot verify" in err


def test_a_missing_mountinfo_alone_is_refused_fail_closed():
    fs = fake_host(PROC)
    gone = HostFs(
        read_text=lambda p: (_ for _ in ()).throw(FileNotFoundError(p)),
        realpath=fs.realpath, exists=fs.exists, isdir=fs.isdir, listdir=fs.listdir,
    )
    err = unused_device_refusal("/dev/sdb", gone)
    assert err is not None and "cannot verify" in err


def _entry_for(tmp_path, monkeypatch, fs: HostFs):
    monkeypatch.setattr(flash_cmd, "YOCTO_WIC_HOST_FS", fs)

    def _execute_must_not_run(*_a, **_k):
        raise AssertionError("the in-use gate did not fire before the real spawn")

    monkeypatch.setattr(flash_cmd, "_execute", _execute_must_not_run)
    build_root = tmp_path / "build"
    build_root.mkdir()
    (build_root / "core-image.wic").write_bytes(b"x")
    stub = tmp_path / "tool-stub"
    stub.mkdir()
    (stub / "dd").write_bytes(b"")
    ctx = flash_cmd._Context(
        sku="S", build_root=str(build_root), sdk_root=str(tmp_path), dry_run=False,
        skip_missing_tools=False, force_confirm=True, capture=True, venv_bin=stub,
    )
    target = flash_cmd.FlashTarget(
        kind="slice", id="a55", flash_method="yocto_wic",
        flash_args={"target": "/dev/sda"}, output_artefact="core-image.wic",
    )
    blk = lambda _t: types.SimpleNamespace(st_mode=stat.S_IFBLK | 0o660)  # noqa: E731
    return flash_cmd._flash_entry(target, ctx, yocto_wic_stat=blk)


def test_the_real_write_path_refuses_the_root_disk_with_its_own_issue_code(tmp_path, monkeypatch):
    rc, entry, _ = _entry_for(tmp_path, monkeypatch, fake_host(ROOT + PROC))
    assert rc == 1 and entry.status == "failed"
    assert entry.issue_code == "flash.yocto-wic-target-in-use"
    assert "refusing target '/dev/sda'" in entry.message
    assert os.path.exists(tmp_path / "build" / "core-image.wic")
