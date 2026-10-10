# SPDX-License-Identifier: Apache-2.0
"""`tan flash` backend `linux_mtd` (tan-cli#1314), against a FAKE ssh/scp on PATH.

Hermetic: no network, no board. The fakes log every argv and emulate just enough of the
target (`/proc/mtd`, `flash_erase`, `mtd_debug`, `sha256sum`). They prove tan's sequencing,
window arithmetic, refusals and quoting; they do NOT prove the real board accepts the
commands (that needs a V2N bench run).

The load-bearing fact: the CM33 image lives INSIDE mtd1 (the FIP partition) at 0x1A0000, so a
whole-partition erase would brick the board. Every test of the happy path pins the erase to
that window.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tan.commands import flash_linux_mtd as fm
from tan.core import flash_linux_mtd as core

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

_FAKE = r'''#!{python}
import hashlib, json, os, shlex, sys
d = os.environ["FAKE_DIR"]
tool = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(os.path.join(d, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps([tool] + args) + "\n")
if "BatchMode=yes" not in args or not any(a.startswith("ConnectTimeout=") for a in args):
    print("fake: BatchMode / ConnectTimeout missing", file=sys.stderr); sys.exit(99)
fail = os.environ.get("FAKE_FAIL", "")
def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()
if tool == "scp":
    if fail == "scp":
        print("scp: connection refused", file=sys.stderr); sys.exit(1)
    local = args[args.index("--") + 1]
    open(os.path.join(d, "remote.bin"), "wb").write(open(local, "rb").read())
    sys.exit(0)
cmd = args[-1]
words = shlex.split(cmd)
if words[:2] == ["cat", "/proc/mtd"]:
    sys.stdout.write(os.environ["FAKE_PROC_MTD"]); sys.exit(0)
if words[0] == "flash_erase":
    sys.exit(3 if fail == "erase" else 0)
if words[:2] == ["mtd_debug", "write"]:
    sys.exit(4 if fail == "mtd_write" else 0)
if words[:2] == ["mtd_debug", "read"]:
    if fail == "mtd_read":
        sys.exit(5)
    data = open(os.path.join(d, "remote.bin"), "rb").read()
    if fail == "corrupt":
        data = b"\xff" + data[1:]
    open(os.path.join(d, "rb.bin"), "wb").write(data)
    sys.exit(0)
if words[0] == "sha256sum":
    if fail == "sha":
        sys.exit(6)
    print(sha(os.path.join(d, "rb.bin")) + "  " + words[1]); sys.exit(0)
if words[:2] == ["rm", "-f"]:
    sys.exit(7 if fail == "rm" else 0)
print("fake: unexpected command " + cmd, file=sys.stderr); sys.exit(98)
'''

PROC_MTD = (
    "dev:    size   erasesize  name\n"
    'mtd0: 00200000 00001000 "bl2"\n'
    'mtd1: 00800000 00001000 "fip"\n'
)
OFFSET = 0x1A0000
SOC = {
    "cm33_boot": {
        "sram_base": 0x08000000, "image_pad": 0x3000, "image_max": 0x30000, "xspi_offset": OFFSET,
        "mtd_name": "fip",
    }
}
SOC_NO_NAME = {"cm33_boot": {k: v for k, v in SOC["cm33_boot"].items() if k != "mtd_name"}}
BODY = b"cm33-body" * 600  # 5400 bytes


def padded(sp: int = 0x08100000, reset: int = 0x08003101, body: bytes = BODY) -> bytes:
    return bytes(0x3000) + struct.pack("<II", sp, reset) + body


IMAGE = padded()


def _fake_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name in ("ssh", "scp"):
        path = bindir / name
        path.write_text(_FAKE.format(python=sys.executable))
        path.chmod(0o755)
    (tmp_path / "fake").mkdir()
    return bindir


def _manifest(image: Path, flash_args: str, method: str) -> str:
    return (
        "schema_version: 1\nhw_info: {sku: E1M-V2N101}\nslices:\n"
        f"- {{core_id: cm33, os: zephyr, output_artefact: '{image}', status: ok,\n"
        f"   flash_method: {method}, flash_args: {flash_args}}}\n"
        "helper_mcus: []\nboot_order: []\n"
    )


def _sdk(work: Path, soc: dict | None) -> None:
    (work / "sdk" / "scripts").mkdir(parents=True)
    (work / "sdk" / "scripts" / "alp_project.py").write_text("")
    if soc is None:
        return
    meta = work / "sdk" / "metadata"
    (meta / "e1m_modules").mkdir(parents=True)
    (meta / "e1m_modules" / "E1M-V2N101.yaml").write_text("silicon: renesas:rzv2n:n44\n")
    (meta / "socs" / "renesas" / "rzv2n").mkdir(parents=True)
    (meta / "socs" / "renesas" / "rzv2n" / "n44.json").write_text(json.dumps(soc))


def _flash(tmp_path, flash_args="{host: 10.0.0.7, user: root, flash_partition: mtd1}",
           *argv, image_name="m33_fw.bin", env=None, confirm=True, image=IMAGE, soc=SOC,
           method="linux_mtd", auto_name=True):
    if auto_name and "flash_partition" in flash_args and "partition_name" not in flash_args:
        flash_args = flash_args.replace("flash_partition", "partition_name: fip, flash_partition", 1)
    work = tmp_path / "work"
    (work / "build").mkdir(parents=True)
    _sdk(work, soc)
    img = tmp_path / image_name
    img.write_bytes(image)
    (work / "build" / "system-manifest.yaml").write_text(_manifest(img, flash_args, method))
    bindir = _fake_bin(tmp_path)
    child_env = {
        **os.environ, "HOME": str(work), "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": str(PACKAGE_ROOT), "FAKE_DIR": str(tmp_path / "fake"),
        "FAKE_PROC_MTD": PROC_MTD, **(env or {}),
    }
    child_env.pop("ALP_FLASH_FORCE", None)
    if confirm:
        child_env["ALP_FLASH_FORCE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "tan", "flash", "--sdk-root", "./sdk", "--format", "json", *argv, "."],
        cwd=work, capture_output=True, text=True, env=child_env, timeout=120,
    )
    return proc.returncode, json.loads(proc.stdout), img


def _calls(tmp_path: Path) -> list[list[str]]:
    log = tmp_path / "fake" / "calls.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _remote_cmds(tmp_path: Path) -> list[str]:
    return [c[-1] for c in _calls(tmp_path) if c[0] == "ssh"]


def _entry(payload):
    return payload["data"]["entries"][0]


def _codes(payload):
    return [i["code"] for i in payload["issues"]]


def _refused_untouched(tmp_path, rc, payload, code):
    assert rc == 1 and code in _codes(payload), payload
    cmds = _remote_cmds(tmp_path)
    assert not any(c.startswith(("flash_erase", "mtd_debug write")) for c in cmds), cmds
    assert not any(c[0] == "scp" for c in _calls(tmp_path))


def test_happy_path_touches_only_the_cm33_window_inside_mtd1(tmp_path):
    rc, payload, _ = _flash(tmp_path)
    assert rc == 0, payload
    entry = _entry(payload)
    assert entry["method"] == "linux_mtd" and entry["status"] == "ok"
    assert [c[0] for c in _calls(tmp_path)] == ["ssh", "scp", "ssh", "ssh", "ssh", "ssh", "ssh"]
    cmds = _remote_cmds(tmp_path)
    blocks = -(-len(IMAGE) // 0x1000)
    assert cmds[0] == "cat /proc/mtd"
    # Never the whole partition: erase starts at 0x1a0000 and spans only the image's blocks.
    assert cmds[1] == f"flash_erase /dev/mtd1 0x1a0000 {blocks}"
    assert cmds[2].startswith(f"mtd_debug write /dev/mtd1 0x1a0000 {len(IMAGE)} /tmp/tan-linux-mtd-")
    assert cmds[3].startswith(f"mtd_debug read /dev/mtd1 0x1a0000 {len(IMAGE)} /tmp/tan-linux-mtd-rb-")
    assert cmds[4].startswith("sha256sum /tmp/tan-linux-mtd-rb-")
    assert cmds[5].startswith("rm -f /tmp/tan-linux-mtd-") and "-rb-" in cmds[5]
    assert not any(c.startswith("flashcp") or " 0 0" in c for c in cmds)
    assert all(any("root@10.0.0.7" in a for a in c) for c in _calls(tmp_path))
    scp = next(c for c in _calls(tmp_path) if c[0] == "scp")
    assert os.path.isabs(scp[scp.index("--") + 1])
    block = entry["linuxMtd"]
    local = hashlib.sha256(IMAGE).hexdigest()
    assert block["digest"] == {"algorithm": "sha256", "local": local, "readBack": local, "match": True}
    assert [s["step"] for s in block["steps"]] == [
        "probe-partitions", "copy-image", "erase", "write", "read-back", "digest", "cleanup",
    ]
    assert block["offset"] == OFFSET and block["eraseBlocks"] == blocks and block["tempFileRemoved"]
    assert "ALP_V2N_CM33_SRAM_NS" in entry["followUp"] and "DSW1" in entry["followUp"]
    assert not _codes(payload)


def test_without_confirm_nothing_is_spawned(tmp_path):
    rc, payload, _ = _flash(tmp_path, confirm=False)
    assert _entry(payload)["status"] == "planned" and "flash.confirm-required" in _codes(payload)
    assert _calls(tmp_path) == []


def test_cli_host_overrides_the_manifest_and_leading_zero_partition_agrees(tmp_path):
    rc, payload, _ = _flash(
        tmp_path, "{user: root, flash_partition: mtd1}", "--target-host", "board.lab",
        "--partition", "mtd01",
    )
    assert rc == 0, payload
    assert all(any("root@board.lab" in a for a in c) for c in _calls(tmp_path))


def test_port_is_passed_as_p_and_capital_p(tmp_path):
    rc, payload, _ = _flash(tmp_path, "{host: h1, port: 2222, flash_partition: mtd1}")
    assert rc == 0, payload
    for call in _calls(tmp_path):
        flag = "-P" if call[0] == "scp" else "-p"
        assert call[call.index(flag) + 1] == "2222"


def test_missing_host_is_refused(tmp_path):
    rc, payload, _ = _flash(tmp_path, "{flash_partition: mtd1}")
    assert rc == 1 and "flash.linux-mtd-no-host" in _codes(payload)
    assert _calls(tmp_path) == []


def test_missing_partition_is_refused_never_defaulted(tmp_path):
    rc, payload, _ = _flash(tmp_path, "{host: h1}")
    assert rc == 1 and "flash.linux-mtd-partition-required" in _codes(payload)
    assert _calls(tmp_path) == []


@pytest.mark.parametrize("ref", ["mtd0", "mtd00", "mtd000"])
def test_mtd0_is_refused_in_every_spelling(tmp_path, ref):
    rc, payload, _ = _flash(tmp_path, f"{{host: h1, flash_partition: {ref}}}")
    assert rc == 1 and "flash.linux-mtd-partition-refused" in _codes(payload)
    assert _calls(tmp_path) == []


def test_a_name_that_resolves_to_mtd0_is_refused(tmp_path):
    rc, payload, _ = _flash(tmp_path, "{host: h1, flash_partition: bl2}")
    _refused_untouched(tmp_path, rc, payload, "flash.linux-mtd-partition-refused")


def test_cli_partition_disagreeing_with_the_manifest_is_refused(tmp_path):
    rc, payload, _ = _flash(tmp_path, "{host: h1, flash_partition: mtd1}", "--partition", "mtd2")
    assert rc == 1 and "flash.linux-mtd-partition-mismatch" in _codes(payload)
    assert _calls(tmp_path) == []


def test_a_partition_given_by_name_resolves_through_proc_mtd(tmp_path):
    rc, payload, _ = _flash(tmp_path, "{host: h1, flash_partition: fip}")
    assert rc == 0, payload
    assert _remote_cmds(tmp_path)[1].startswith("flash_erase /dev/mtd1 0x1a0000 ")


def test_a_reordered_table_cannot_redirect_the_write(tmp_path):
    swapped = 'mtd0: 00200000 00001000 "bl2"\nmtd1: 00800000 00001000 "rootfs"\n'
    rc, payload, _ = _flash(
        tmp_path, "{host: h1, flash_partition: mtd1, partition_name: fip}",
        env={"FAKE_PROC_MTD": swapped},
    )
    _refused_untouched(tmp_path, rc, payload, "flash.linux-mtd-partition-mismatch")


def test_partition_absent_from_proc_mtd_writes_nothing(tmp_path):
    rc, payload, _ = _flash(tmp_path, "{host: h1, flash_partition: mtd5}")
    _refused_untouched(tmp_path, rc, payload, "flash.linux-mtd-partition-absent")
    assert _remote_cmds(tmp_path) == ["cat /proc/mtd"]


def test_window_past_the_partition_end_is_refused(tmp_path):
    small = 'mtd0: 00200000 00001000 "bl2"\nmtd1: 001A1000 00001000 "fip"\n'
    rc, payload, _ = _flash(tmp_path, env={"FAKE_PROC_MTD": small})
    _refused_untouched(tmp_path, rc, payload, "flash.linux-mtd-image-too-large")


def test_unaligned_offset_is_refused(tmp_path):
    odd = 'mtd0: 00200000 00001000 "bl2"\nmtd1: 00800000 00030000 "fip"\n'
    rc, payload, _ = _flash(tmp_path, env={"FAKE_PROC_MTD": odd})
    _refused_untouched(tmp_path, rc, payload, "flash.linux-mtd-invalid")


@pytest.mark.parametrize(
    "image",
    [
        BODY,  # a raw zephyr.bin, no pad
        bytes([1]) + bytes(0x2FFF) + struct.pack("<II", 0x08100000, 0x08003101) + BODY,
        padded(sp=0x20001000),
        padded(reset=0x08003100),  # no Thumb bit
        padded(reset=0x09003101),  # outside the window
        padded(reset=0x08002001),  # below sram_base + pad
        bytes(0x3000),  # no vector table
        padded(body=bytes(0x30000)),  # over image_max
    ],
    ids=["unpadded", "dirty-pad", "bad-sp", "no-thumb", "reset-high", "reset-low", "no-vectors", "too-big"],
)
def test_an_image_that_is_not_a_padded_cm33_image_is_refused_before_scp(tmp_path, image):
    rc, payload, _ = _flash(tmp_path, image=image)
    assert rc == 1 and "flash.linux-mtd-image-invalid" in _codes(payload)
    assert _calls(tmp_path) == []


def test_no_sdk_boot_facts_means_no_write_and_the_reason_is_surfaced(tmp_path):
    rc, payload, _ = _flash(tmp_path, soc=None)
    assert rc == 1 and "flash.linux-mtd-boot-facts-unavailable" in _codes(payload)
    assert "SoM preset" in _entry(payload)["message"]
    assert _calls(tmp_path) == []


def test_manifest_facts_alone_never_authorise_a_write(tmp_path):
    args = (
        "{host: h1, flash_partition: mtd1, sram_base: 0x08000000, image_pad: 0x3000, "
        "image_max: 0x30000, xspi_offset: 0x1A0000}"
    )
    rc, payload, _ = _flash(tmp_path, args, soc=None)
    assert rc == 1 and "flash.linux-mtd-boot-facts-unavailable" in _codes(payload)
    assert _calls(tmp_path) == []


def test_soc_lookup_failures_name_their_cause(tmp_path):
    assert "no alp-sdk root" in fm.soc_cm33_boot(None, "E1M-V2N101")[1]
    assert "no SoM SKU" in fm.soc_cm33_boot(str(tmp_path), "")[1]
    assert "unreadable" in fm.soc_cm33_boot(str(tmp_path), "E1M-V2N101")[1]
    (tmp_path / "metadata" / "e1m_modules").mkdir(parents=True)
    (tmp_path / "metadata" / "e1m_modules" / "E1M-V2N101.yaml").write_text("sku: x\n")
    assert "silicon" in fm.soc_cm33_boot(str(tmp_path), "E1M-V2N101")[1]
    _sdk(tmp_path / "w", {"other": 1})
    assert "no cm33_boot" in fm.soc_cm33_boot(str(tmp_path / "w" / "sdk"), "E1M-V2N101")[1]
    assert fm.soc_cm33_boot(str(tmp_path / "x"), "E1M-V2N101")[0] is None
    _sdk(tmp_path / "ok", SOC)
    assert fm.soc_cm33_boot(str(tmp_path / "ok" / "sdk"), "E1M-V2N101") == (SOC["cm33_boot"], "")


def test_the_sdk_name_alone_is_enough_and_the_manifest_name_is_optional(tmp_path):
    rc, payload, _ = _flash(tmp_path, "{host: h1, flash_partition: mtd1}", auto_name=False)
    assert rc == 0, payload
    assert _entry(payload)["linuxMtd"]["partitionName"] == "fip"


def test_a_manifest_only_name_never_authorises_a_write(tmp_path):
    # SDK facts without mtd_name; the manifest names flash_partition AND partition_name
    # consistently -- and wrongly. The manifest must not be able to pick the target.
    three = PROC_MTD + 'mtd2: 00800000 00001000 "rootfs"\n'
    rc, payload, _ = _flash(
        tmp_path, "{host: h1, partition_name: rootfs, flash_partition: mtd2}",
        soc=SOC_NO_NAME, env={"FAKE_PROC_MTD": three},
    )
    assert rc == 1 and "flash.linux-mtd-boot-facts-unavailable" in _codes(payload)
    assert "mtd_name" in _entry(payload)["message"]
    assert _calls(tmp_path) == []


def test_a_manifest_name_disagreeing_with_the_sdk_name_is_refused(tmp_path):
    three = PROC_MTD + 'mtd2: 00800000 00001000 "rootfs"\n'
    rc, payload, _ = _flash(
        tmp_path, "{host: h1, partition_name: rootfs, flash_partition: mtd2}",
        env={"FAKE_PROC_MTD": three},
    )
    assert rc == 1 and "flash.linux-mtd-partition-mismatch" in _codes(payload)
    assert _calls(tmp_path) == []


def test_dry_run_with_facts_but_no_sdk_name_is_labelled_unverified(tmp_path):
    rc, payload, _ = _flash(
        tmp_path, "{host: h1, flash_partition: mtd1}", "--dry-run", confirm=False,
        soc=SOC_NO_NAME, auto_name=False,
    )
    assert rc == 0, payload
    assert _entry(payload)["linuxMtd"]["bootFactsAuthoritative"] is False
    assert "NOT authoritative" in _entry(payload)["message"]


def test_the_snapshot_copy_is_private(tmp_path):
    deploy = fm._Deploy(
        SimpleNamespace(id="x"), SimpleNamespace(), lambda *a, **k: None, [], {}, "/x", {}
    )
    deploy.data = b"abc"
    path = deploy.snapshot_file()
    try:
        assert (os.stat(path).st_mode & 0o777) == 0o600
    finally:
        import shutil
        shutil.rmtree(deploy.snapshot_dir, ignore_errors=True)


def test_a_wrongly_named_partition_is_refused(tmp_path):
    three = PROC_MTD + 'mtd2: 00800000 00001000 "rootfs"\n'
    rc, payload, _ = _flash(
        tmp_path, "{host: h1, flash_partition: mtd2}", env={"FAKE_PROC_MTD": three}
    )
    _refused_untouched(tmp_path, rc, payload, "flash.linux-mtd-partition-mismatch")


def test_an_sdk_mtd_name_wins_and_must_agree_with_the_manifest():
    soc = {"mtd_name": "fip"}
    spec = core.resolve_spec({"host": "h", "flash_partition": "mtd1"})
    assert core.expected_name(spec, soc, "") == ("fip", True)
    named = core.resolve_spec({"host": "h", "flash_partition": "mtd1", "partition_name": "x"})
    with pytest.raises(core.LinuxMtdError) as err:
        core.expected_name(named, soc, "")
    assert err.value.code == "flash.linux-mtd-partition-mismatch"
    with pytest.raises(core.LinuxMtdError) as err:
        core.expected_name(named, {}, "no cm33_boot")
    assert err.value.code == "flash.linux-mtd-boot-facts-unavailable"
    assert core.expected_name(named, {}, "", dry_run=True) == ("x", False)


def test_the_image_is_snapshotted_once_so_later_edits_cannot_drift(tmp_path, monkeypatch):
    bindir = _fake_bin(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DIR", str(tmp_path / "fake"))
    monkeypatch.setenv("FAKE_PROC_MTD", PROC_MTD)
    img = tmp_path / "m33_fw.bin"
    img.write_bytes(IMAGE)
    real = fm._spawn

    def spawn(argv):
        if argv[-1] == "cat /proc/mtd":  # the file changes after it was validated
            img.write_bytes(padded(body=b"tampered" * 700))
        return real(argv)

    monkeypatch.setattr(fm, "_spawn", spawn)
    _sdk(tmp_path / "work", SOC)
    ctx = SimpleNamespace(
        target_host=None, target_partition=None, sdk_root=str(tmp_path / "work" / "sdk"),
        sku="E1M-V2N101", dry_run=False, force_confirm=True,
    )
    seen = {}
    rc, _, _ = fm.run_linux_mtd_entry(
        SimpleNamespace(id="cm33"), ctx, artefact_path=str(img),
        flash_args={"host": "h1", "flash_partition": "mtd1", "partition_name": "fip"},
        entry=lambda m, s, r, msg, **kw: seen.update(status=s, **kw) or seen,
        lines=[], report=seen,
    )
    assert rc == 0, seen
    assert (tmp_path / "fake" / "remote.bin").read_bytes() == IMAGE
    assert seen["linuxMtd"]["digest"]["match"] is True
    assert seen["linuxMtd"]["sha256"] == hashlib.sha256(IMAGE).hexdigest()


def test_flash_args_that_disagree_with_the_sdk_metadata_are_refused(tmp_path):
    args = (
        "{host: h1, flash_partition: mtd1, sram_base: 0x08000000, image_pad: 0x3000, "
        "image_max: 0x30000, xspi_offset: 0x1000}"
    )
    rc, payload, _ = _flash(tmp_path, args)
    assert rc == 1 and "flash.linux-mtd-invalid" in _codes(payload)
    assert _calls(tmp_path) == []


def test_readback_mismatch_is_a_coded_error_and_still_cleans_up(tmp_path):
    rc, payload, _ = _flash(tmp_path, env={"FAKE_FAIL": "corrupt"})
    assert rc == 1 and "flash.linux-mtd-readback-mismatch" in _codes(payload)
    digest = _entry(payload)["linuxMtd"]["digest"]
    assert digest["match"] is False
    assert _remote_cmds(tmp_path)[-1].startswith("rm -f /tmp/tan-linux-mtd-")
    assert "followUp" not in _entry(payload)


@pytest.mark.parametrize(
    ("step", "partial"),
    [("scp", False), ("erase", True), ("mtd_write", True), ("mtd_read", False), ("sha", False)],
)
def test_a_failing_step_stops_the_run_cleans_up_and_names_the_hazard(tmp_path, step, partial):
    rc, payload, _ = _flash(tmp_path, env={"FAKE_FAIL": step})
    assert rc == 1 and "flash.linux-mtd-failed" in _codes(payload)
    message = _entry(payload)["message"]
    assert ("partial image" in message) is partial
    assert _remote_cmds(tmp_path)[-1].startswith("rm -f /tmp/tan-linux-mtd-")
    if step == "erase":
        assert not any(c.startswith("mtd_debug") for c in _remote_cmds(tmp_path))


def test_a_timed_out_remote_step_says_it_may_still_be_running(tmp_path, monkeypatch):
    bindir = _fake_bin(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DIR", str(tmp_path / "fake"))
    monkeypatch.setenv("FAKE_PROC_MTD", PROC_MTD)
    real = fm._spawn

    def spawn(argv):
        if "flash_erase" in argv[-1]:
            return 124, "", "timed out after 300s"
        return real(argv)

    monkeypatch.setattr(fm, "_spawn", spawn)
    img = tmp_path / "m33_fw.bin"
    img.write_bytes(IMAGE)
    sdk = tmp_path / "work"
    _sdk(sdk, SOC)
    ctx = SimpleNamespace(
        target_host=None, target_partition=None, sdk_root=str(sdk / "sdk"), sku="E1M-V2N101",
        dry_run=False, force_confirm=True,
    )
    seen = {}

    def entry(method, status, rc, message, **kw):
        seen.update(status=status, message=message, **kw)
        return seen

    rc, _, _ = fm.run_linux_mtd_entry(
        SimpleNamespace(id="cm33"), ctx, artefact_path=str(img),
        flash_args={"host": "h1", "flash_partition": "mtd1", "partition_name": "fip"}, entry=entry, lines=[], report={},
    )
    assert rc == 1 and seen["issue_code"] == "flash.linux-mtd-failed"
    assert "STILL be running" in seen["message"] and "partial image" in seen["message"]


def test_a_temp_file_that_cannot_be_removed_is_a_warning(tmp_path):
    rc, payload, _ = _flash(tmp_path, env={"FAKE_FAIL": "rm"})
    assert rc == 0, payload
    warning = next(i for i in payload["issues"] if i["code"] == "flash.linux-mtd-cleanup-failed")
    assert warning["severity"] == "warning" and "/tmp/tan-linux-mtd-" in warning["message"]


def test_dry_run_prints_the_plan_and_spawns_nothing(tmp_path):
    rc, payload, _ = _flash(tmp_path, "{host: h1, flash_partition: mtd1}", "--dry-run", confirm=False)
    assert rc == 0, payload
    entry = _entry(payload)
    assert entry["status"] == "ok" and "would run" in entry["message"]
    steps = [p["step"] for p in entry["linuxMtd"]["plannedSteps"]]
    assert steps == [
        "probe-partitions", "copy-image", "erase", "write", "read-back", "digest", "cleanup",
    ]
    assert "followUp" in entry and "0x1a0000" in entry["message"]
    assert _calls(tmp_path) == []


def test_dry_run_still_applies_the_partition_and_facts_rules(tmp_path):
    rc, payload, _ = _flash(tmp_path, "{host: h1, flash_partition: mtd0}", "--dry-run", confirm=False)
    assert rc == 1 and "flash.linux-mtd-partition-refused" in _codes(payload)


def test_dry_run_without_sdk_facts_is_labelled_unverified(tmp_path):
    rc, payload, _ = _flash(
        tmp_path, "{host: h1, flash_partition: mtd1}", "--dry-run", confirm=False, soc=None
    )
    assert rc == 0, payload
    entry = _entry(payload)
    assert "UNAVAILABLE" in entry["message"] and "NOT authoritative" in entry["message"]
    assert entry["linuxMtd"]["bootFactsAuthoritative"] is False
    assert "SoM preset" in entry["linuxMtd"]["bootFactsUnavailable"]
    assert "<xspi_offset: SDK facts unavailable>" in entry["message"]
    assert _calls(tmp_path) == []


@pytest.mark.parametrize(
    "flash_args",
    [
        "{host: '-oProxyCommand=touch /tmp/pwned', flash_partition: mtd1}",
        "{host: 'h1;touch /tmp/pwned', flash_partition: mtd1}",
        "{host: 'h1:/etc', flash_partition: mtd1}",
        "{host: '[::1]', flash_partition: mtd1}",
        "{host: h1, user: 'root;id', flash_partition: mtd1}",
        "{host: h1, user: '-oX=y', flash_partition: mtd1}",
        "{host: h1, port: '22; id', flash_partition: mtd1}",
        "{host: h1, flash_partition: 'mtd1; rm -rf /'}",
        "{host: h1, flash_partition: '../dev/sda'}",
        "{host: h1, flash_partition: mtd1, partition_name: 'fip; id'}",
    ],
)
def test_hostile_values_are_refused_before_anything_spawns(tmp_path, flash_args):
    rc, payload, _ = _flash(tmp_path, flash_args)
    assert rc == 1 and "flash.linux-mtd-invalid" in _codes(payload)
    assert _calls(tmp_path) == []


def test_hostile_host_from_the_flag_is_refused(tmp_path):
    rc, payload, _ = _flash(tmp_path, "{flash_partition: mtd1}", "--target-host", "-oProxyCommand=x")
    assert rc == 1 and "flash.linux-mtd-invalid" in _codes(payload)
    assert _calls(tmp_path) == []


def test_hostile_local_name_never_reaches_scp_or_the_remote_shell(tmp_path):
    name = "a b;touch pwned$(id)`id`.bin"
    rc, payload, _ = _flash(tmp_path, image_name=name)
    assert rc == 0, payload
    scp = next(c for c in _calls(tmp_path) if c[0] == "scp")
    local = scp[scp.index("--") + 1]
    assert os.path.isabs(local) and local.endswith("/image.bin") and "pwned" not in local
    assert not any("pwned" in c for c in _remote_cmds(tmp_path))


def test_target_options_for_another_method_are_disclosed(tmp_path):
    rc, payload, _ = _flash(
        tmp_path, "{}", "--dry-run", "--target-host", "h1", method="zephyr_west_flash",
        confirm=False,
    )
    assert "flash.linux-mtd-option-ignored" in _codes(payload)


def test_every_remote_token_is_shell_quoted():
    assert core.remote("rm", "-f", "/tmp/a b;$(id)") == "rm -f '/tmp/a b;$(id)'"
    spec = core.resolve_spec({"host": "h1", "flash_partition": "mtd1"})
    argv = core.ssh_argv(spec.target, core.remote_proc_mtd())
    assert argv[-3:] == ["--", "h1", "cat /proc/mtd"] and "BatchMode=yes" in argv


def test_leading_zero_indexes_are_normalised():
    assert core.canonical_partition("mtd01", "x") == "mtd1"
    assert core.resolve_spec({"host": "h", "flash_partition": "mtd01"}, cli_partition="mtd1").partition == "mtd1"


def test_proc_mtd_parsing():
    rows = core.parse_proc_mtd(PROC_MTD)
    assert [(r.index, r.size, r.erasesize, r.name) for r in rows] == [
        (0, 0x200000, 0x1000, "bl2"), (1, 0x800000, 0x1000, "fip"),
    ]
