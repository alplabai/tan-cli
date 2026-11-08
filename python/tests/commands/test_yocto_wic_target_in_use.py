# SPDX-License-Identifier: Apache-2.0
"""The yocto_wic write refuses a block device that is mounted, swap, held by
the kernel in any namespace, or that backs a mounted filesystem (the host root
disk), and refuses when it cannot tell. Same cases as alp-sdk's
`_require_unused_device`; the host is a dict-backed fake, never the real one."""

from __future__ import annotations

import errno
import json
import os
import stat
import types

import pytest

from tan.commands import flash_cmd
from tan.core import block_busy
from tan.core.block_busy import HostFs, unused_device_refusal

ROOT = "22 1 8:2 / / rw,relatime - ext4 /dev/root rw\n"
PROC = "23 22 0:21 / /proc rw - proc proc rw\n"
NO_SWAP = "Filename Type Size Used Priority\n"
SYS = block_busy.SYS_BLOCK
DEVBLK = block_busy.SYS_DEV_BLOCK

RDEV = {
    "/dev/sda": (8, 0), "/dev/sda2": (8, 2), "/dev/sda3": (8, 3),
    "/dev/sdb": (8, 16), "/dev/sdb1": (8, 17), "/dev/alias-of-sda2": (8, 2),
}


def fake_host(
    mountinfo: str = PROC,
    swaps: str | None = NO_SWAP,
    *,
    readable: bool = True,
    holders: dict[str, list[str]] | None = None,
    excl: dict[str, int] | None = None,
) -> HostFs:
    """sda (sda2 = root, sda3), sdb (sdb1), dm-0 on sda3; kernel-style links.
    `excl` maps a /dev path to the errno its O_EXCL open fails with."""
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
    files = {block_busy.MOUNTINFO: mountinfo}
    if swaps is not None:
        files[block_busy.SWAPS] = swaps
    partitions = {f"{SYS}/{n}/partition" for n in ("sda2", "sda3", "sdb1")} | {
        f"{SYS}/sda/sda2/partition", f"{SYS}/sda/sda3/partition", f"{SYS}/sdb/sdb1/partition",
    }
    lists = {
        f"{SYS}/dm-0/slaves": ["sda3"],
        f"{SYS}/sda": ["sda2", "sda3"],
        f"{SYS}/sdb": ["sdb1"],
        f"{SYS}/sda2": [], f"{SYS}/sda3": [], f"{SYS}/sdb1": [],
    }
    for name, hs in (holders or {}).items():
        lists[f"{SYS}/{name}/holders"] = hs
    excl = excl or {}

    def read_text(path: str) -> str:
        if not readable or path not in files:
            raise FileNotFoundError(path)
        return files[path]

    def rdev(path: str) -> tuple[int, int]:
        if path not in RDEV:
            raise FileNotFoundError(path)
        return RDEV[path]

    def open_excl(path: str) -> None:
        if path in excl:
            raise OSError(excl[path], os.strerror(excl[path]), path)

    return HostFs(
        read_text=read_text,
        realpath=lambda p: links.get(p, p),
        exists=lambda p: p in links or p in partitions,
        isdir=lambda p: readable and (p == DEVBLK or p in lists),
        listdir=lambda p: lists[p],
        rdev=rdev,
        open_excl=open_excl,
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


def test_a_node_alias_is_matched_by_device_number_not_by_name():
    fs = fake_host(ROOT + PROC)
    assert unused_device_refusal("/dev/alias-of-sda2", fs) is not None


def test_a_disk_under_a_mounted_device_mapper_node_is_refused():
    fs = fake_host("41 22 253:0 / /home rw - ext4 /dev/mapper/vg-home rw\n")
    assert unused_device_refusal("/dev/sda", fs) is not None


def test_an_active_swap_partition_is_refused():
    fs = fake_host(PROC, "Filename Type Size Used Priority\n/dev/sdb1 partition 1024 0 -2\n")
    err = unused_device_refusal("/dev/sdb", fs)
    assert err is not None and "active swap" in err


def test_an_unused_device_is_allowed():
    assert unused_device_refusal("/dev/sdb", fake_host(ROOT + PROC)) is None


def test_a_target_that_does_not_exist_is_not_this_guards_business():
    assert unused_device_refusal("/dev/nope", fake_host(ROOT + PROC)) is None


def test_an_unreadable_mount_table_is_refused_fail_closed():
    err = unused_device_refusal("/dev/sdb", fake_host(PROC, readable=False))
    assert err is not None and "cannot verify" in err


def test_an_unreadable_proc_swaps_is_refused_fail_closed():
    err = unused_device_refusal("/dev/sdb", fake_host(PROC, swaps=None))
    assert err is not None and "cannot verify" in err


# -- the namespace-independent half: a container sees none of the host's mounts


def test_a_kernel_claim_on_the_target_is_refused_though_mountinfo_is_empty():
    fs = fake_host(PROC, excl={"/dev/sdb": errno.EBUSY})
    err = unused_device_refusal("/dev/sdb", fs)
    assert err is not None and "claimed by the kernel" in err


def test_a_kernel_claim_on_a_child_partition_is_refused():
    fs = fake_host(PROC, excl={"/dev/sdb1": errno.EBUSY})
    err = unused_device_refusal("/dev/sdb", fs)
    assert err is not None and "'sdb1'" in err


def test_non_empty_holders_on_the_target_are_refused():
    err = unused_device_refusal("/dev/sdb", fake_host(PROC, holders={"sdb": ["dm-3"]}))
    assert err is not None and "dm-3" in err


def test_non_empty_holders_on_a_child_are_refused_even_without_its_node():
    fs = fake_host(PROC, holders={"sdb1": ["md0"]}, excl={"/dev/sdb1": errno.ENOENT})
    err = unused_device_refusal("/dev/sdb", fs)
    assert err is not None and "md0" in err


def test_empty_holders_and_a_missing_child_node_are_allowed():
    fs = fake_host(PROC, holders={"sdb": [], "sdb1": []}, excl={"/dev/sdb1": errno.ENOENT})
    assert unused_device_refusal("/dev/sdb", fs) is None


@pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM, errno.EIO, errno.ENOENT])
def test_a_probe_error_that_cannot_tell_refuses_on_the_target(code):
    err = unused_device_refusal("/dev/sdb", fake_host(PROC, excl={"/dev/sdb": code}))
    assert err is not None and "cannot verify" in err


@pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM, errno.EIO])
def test_a_probe_error_that_cannot_tell_refuses_on_a_child(code):
    err = unused_device_refusal("/dev/sdb", fake_host(PROC, excl={"/dev/sdb1": code}))
    assert err is not None and "cannot verify" in err


def test_no_sysfs_means_a_write_is_refused_as_on_macos_and_windows():
    fs = fake_host(PROC, readable=False)
    err = unused_device_refusal("/dev/sdb", fs)
    assert err is not None and "Linux host" in err


# -- wiring: the real write path, `--dry-run` and unconfirmed runs


def _entry_for(tmp_path, monkeypatch, fs: HostFs, target="/dev/sda", **ctx_kw):
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
    kw = dict(
        sku="S", build_root=str(build_root), sdk_root=str(tmp_path), dry_run=False,
        skip_missing_tools=False, force_confirm=True, capture=True, venv_bin=stub,
    )
    kw.update(ctx_kw)
    ctx = flash_cmd._Context(**kw)
    ft = flash_cmd.FlashTarget(
        kind="slice", id="a55", flash_method="yocto_wic",
        flash_args={"target": target}, output_artefact="core-image.wic",
    )
    blk = lambda _t: types.SimpleNamespace(st_mode=stat.S_IFBLK | 0o660)  # noqa: E731
    return flash_cmd._flash_entry(ft, ctx, yocto_wic_stat=blk)


def test_the_real_write_path_refuses_the_root_disk_with_its_own_issue_code(tmp_path, monkeypatch):
    rc, entry, _ = _entry_for(tmp_path, monkeypatch, fake_host(ROOT + PROC))
    assert rc == 1 and entry.status == "failed"
    assert entry.issue_code == "flash.yocto-wic-target-in-use"
    assert "refusing target '/dev/sda'" in entry.message
    assert os.path.exists(tmp_path / "build" / "core-image.wic")


def test_a_dry_run_skips_the_guard_and_still_previews(tmp_path, monkeypatch):
    rc, entry, _ = _entry_for(tmp_path, monkeypatch, fake_host(ROOT + PROC), dry_run=True)
    assert rc == 0 and entry.status in ("ok", "planned")
    assert entry.issue_code is None


def test_an_unconfirmed_run_skips_the_guard_and_still_previews(tmp_path, monkeypatch):
    rc, entry, _ = _entry_for(
        tmp_path, monkeypatch, fake_host(ROOT + PROC), force_confirm=False
    )
    assert rc == 0 and entry.status == "planned"
    assert entry.issue_code is None


def test_run_reports_the_refusal_as_issues_code(tmp_path, monkeypatch):
    """Through the REAL Typer callback and `_run`: the envelope carries
    issues[].code == flash.yocto-wic-target-in-use."""
    import typer
    from typer.testing import CliRunner

    monkeypatch.setattr(flash_cmd, "YOCTO_WIC_HOST_FS", fake_host(ROOT + PROC))
    monkeypatch.setattr(flash_cmd, "_yocto_wic_block_device_refusal", lambda *_a, **_k: None)
    monkeypatch.setattr(
        flash_cmd, "_execute",
        lambda *_a, **_k: pytest.fail("the in-use gate did not fire before the spawn"),
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    dd = tools / "dd"
    dd.write_bytes(b"")
    dd.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))
    (tmp_path / "sdk" / "scripts").mkdir(parents=True)
    (tmp_path / "sdk" / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "a.wic").write_bytes(b"x")
    (tmp_path / "build" / "system-manifest.yaml").write_text(
        """schema_version: 1
hw_info: {sku: S}
slices:
- {core_id: a55, os: yocto, output_artefact: a.wic, status: ok,
   flash_method: yocto_wic, flash_args: {target: /dev/sda, confirm: true}}
helper_mcus: []
boot_order: []
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    app = typer.Typer(add_completion=False)
    app.command("flash")(flash_cmd.flash)
    app.command("_unused")(lambda: None)
    result = CliRunner().invoke(
        app, ["flash", "--sdk-root", "./sdk", "--format", "json", "."]
    )
    payload = json.loads(result.stdout)
    assert result.exit_code == 1, result.stdout
    assert [i["code"] for i in payload["issues"]] == ["flash.yocto-wic-target-in-use"]
