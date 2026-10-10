# SPDX-License-Identifier: Apache-2.0
"""`tan flash` backend `linux_mtd` (tan-cli#1314), against a FAKE ssh/scp on PATH.

Hermetic: no network, no board. The fakes log every argv and emulate just enough of the
target (`/proc/mtd`, `flash_erase`, `flashcp`, a sha256 read-back). They prove tan's
sequencing, refusals and quoting; they do NOT prove the real board accepts the commands
(that needs a V2N bench run).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tan.core import flash_linux_mtd as core

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

_FAKE = r'''#!{python}
import hashlib, json, os, sys
d = os.environ["FAKE_DIR"]
tool = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(os.path.join(d, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps([tool] + args) + "\n")
if "BatchMode=yes" not in args or not any(a.startswith("ConnectTimeout=") for a in args):
    print("fake: BatchMode / ConnectTimeout missing", file=sys.stderr); sys.exit(99)
fail = os.environ.get("FAKE_FAIL", "")
if tool == "scp":
    if fail == "scp":
        print("scp: connection refused", file=sys.stderr); sys.exit(1)
    local = args[args.index("--") + 1]
    with open(local, "rb") as fh:
        open(os.path.join(d, "remote.sha"), "w").write(hashlib.sha256(fh.read()).hexdigest())
    sys.exit(0)
cmd = args[-1]
if cmd.startswith("cat /proc/mtd"):
    sys.stdout.write(os.environ["FAKE_PROC_MTD"]); sys.exit(0)
if cmd.startswith("flash_erase"):
    sys.exit(3 if fail == "erase" else 0)
if cmd.startswith("flashcp"):
    sys.exit(4 if fail == "flashcp" else 0)
if cmd.startswith("head -c"):
    digest = open(os.path.join(d, "remote.sha")).read()
    if fail == "corrupt":
        digest = "0" * 64
    print(digest + "  -"); sys.exit(0)
if cmd.startswith("rm -f"):
    sys.exit(0)
print("fake: unexpected command " + cmd, file=sys.stderr); sys.exit(98)
'''

PROC_MTD = (
    "dev:    size   erasesize  name\n"
    'mtd0: 00200000 00001000 "bl2"\n'
    'mtd1: 00400000 00001000 "cm33"\n'
)
IMAGE = b"\xa5" * 4096 + b"cm33-image"


def _fake_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name in ("ssh", "scp"):
        path = bindir / name
        path.write_text(_FAKE.format(python=sys.executable))
        path.chmod(0o755)
    (tmp_path / "fake").mkdir()
    return bindir


def _manifest(image: Path, flash_args: str) -> str:
    return (
        "schema_version: 1\nhw_info: {sku: E1M-V2N101}\nslices:\n"
        f"- {{core_id: cm33, os: zephyr, output_artefact: '{image}', status: ok,\n"
        f"   flash_method: linux_mtd, flash_args: {flash_args}}}\n"
        "helper_mcus: []\nboot_order: []\n"
    )


def _flash(tmp_path, flash_args="{host: 10.0.0.7, user: root, flash_partition: mtd1}",
           *argv, image_name="m33_fw.bin", env=None, confirm=True):
    work = tmp_path / "work"
    (work / "build").mkdir(parents=True)
    (work / "sdk" / "scripts").mkdir(parents=True)
    (work / "sdk" / "scripts" / "alp_project.py").write_text("")
    image = tmp_path / image_name
    image.write_bytes(IMAGE)
    (work / "build" / "system-manifest.yaml").write_text(_manifest(image, flash_args))
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
    return proc.returncode, json.loads(proc.stdout), image


def _calls(tmp_path: Path) -> list[list[str]]:
    log = tmp_path / "fake" / "calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def _remote_cmds(tmp_path: Path) -> list[str]:
    return [c[-1] for c in _calls(tmp_path) if c[0] == "ssh"]


def _entry(payload):
    return payload["data"]["entries"][0]


def _codes(payload):
    return [i["code"] for i in payload["issues"]]


def test_happy_path_runs_every_step_in_order_and_cleans_up(tmp_path):
    rc, payload, image = _flash(tmp_path)
    assert rc == 0, payload
    entry = _entry(payload)
    assert entry["method"] == "linux_mtd" and entry["status"] == "ok"
    calls = _calls(tmp_path)
    kinds = [c[0] for c in calls]
    assert kinds == ["ssh", "scp", "ssh", "ssh", "ssh", "ssh"]
    cmds = _remote_cmds(tmp_path)
    assert cmds[0] == "cat /proc/mtd"
    assert cmds[1] == "flash_erase /dev/mtd1 0 0"
    assert cmds[2].startswith("flashcp -v /tmp/tan-linux-mtd-") and cmds[2].endswith("/dev/mtd1")
    assert cmds[3].startswith(f"head -c {len(IMAGE)} /dev/mtd1 | sha256sum")
    assert cmds[4].startswith("rm -f /tmp/tan-linux-mtd-")
    assert all(any("root@10.0.0.7" in a for a in c) for c in calls)
    block = entry["linuxMtd"]
    local = hashlib.sha256(IMAGE).hexdigest()
    assert block["digest"] == {"algorithm": "sha256", "local": local, "readBack": local, "match": True}
    assert [s["step"] for s in block["steps"]] == [
        "probe-partitions", "copy-image", "erase", "write", "read-back", "cleanup",
    ]
    assert block["tempFileRemoved"] is True
    assert "remoteproc" in entry["followUp"] and "NOT running" in entry["followUp"]
    assert not _codes(payload)


def test_without_confirm_nothing_is_spawned(tmp_path):
    rc, payload, _ = _flash(tmp_path, confirm=False)
    assert _entry(payload)["status"] == "planned" and "flash.confirm-required" in _codes(payload), payload
    assert _calls(tmp_path) == []


def test_cli_host_overrides_the_manifest(tmp_path):
    rc, payload, _ = _flash(
        tmp_path, "{user: root, flash_partition: mtd1}", "--target-host", "board.lab", "--partition", "mtd1"
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


def test_mtd0_is_refused_even_with_a_matching_flag(tmp_path):
    rc, payload, _ = _flash(tmp_path, "{host: h1, flash_partition: mtd0}", "--partition", "mtd0")
    assert rc == 1 and "flash.linux-mtd-partition-refused" in _codes(payload)
    assert _calls(tmp_path) == []


def test_cli_partition_disagreeing_with_the_manifest_is_refused(tmp_path):
    rc, payload, _ = _flash(tmp_path, "{host: h1, flash_partition: mtd1}", "--partition", "mtd2")
    assert rc == 1 and "flash.linux-mtd-partition-mismatch" in _codes(payload)
    assert _calls(tmp_path) == []


def test_partition_absent_from_proc_mtd_writes_nothing(tmp_path):
    rc, payload, _ = _flash(tmp_path, "{host: h1, flash_partition: mtd5}")
    assert rc == 1 and "flash.linux-mtd-partition-absent" in _codes(payload)
    assert _remote_cmds(tmp_path) == ["cat /proc/mtd"]
    assert [c[0] for c in _calls(tmp_path)] == ["ssh"]


def test_image_larger_than_the_partition_writes_nothing(tmp_path):
    small = 'mtd0: 00200000 00001000 "bl2"\nmtd1: 00000400 00001000 "cm33"\n'
    rc, payload, _ = _flash(tmp_path, env={"FAKE_PROC_MTD": small})
    assert rc == 1 and "flash.linux-mtd-image-too-large" in _codes(payload)
    assert [c[0] for c in _calls(tmp_path)] == ["ssh"]


def test_readback_mismatch_is_a_coded_error_and_still_cleans_up(tmp_path):
    rc, payload, _ = _flash(tmp_path, env={"FAKE_FAIL": "corrupt"})
    assert rc == 1 and "flash.linux-mtd-readback-mismatch" in _codes(payload)
    digest = _entry(payload)["linuxMtd"]["digest"]
    assert digest["match"] is False and digest["readBack"] == "0" * 64
    assert _remote_cmds(tmp_path)[-1].startswith("rm -f /tmp/tan-linux-mtd-")
    assert "followUp" not in _entry(payload)


@pytest.mark.parametrize("step", ["scp", "erase", "flashcp"])
def test_a_failing_step_stops_the_run_and_cleans_up(tmp_path, step):
    rc, payload, _ = _flash(tmp_path, env={"FAKE_FAIL": step})
    assert rc == 1 and "flash.linux-mtd-failed" in _codes(payload)
    cmds = _remote_cmds(tmp_path)
    assert not any(c.startswith("head -c") for c in cmds)
    assert cmds[-1].startswith("rm -f /tmp/tan-linux-mtd-")
    if step == "erase":
        assert not any(c.startswith("flashcp") for c in cmds)


def test_dry_run_prints_the_plan_and_spawns_nothing(tmp_path):
    rc, payload, _ = _flash(tmp_path, "{host: h1, flash_partition: mtd1}", "--dry-run", confirm=False)
    assert rc == 0, payload
    entry = _entry(payload)
    assert entry["status"] == "ok" and "would run" in entry["message"]
    steps = [p["step"] for p in entry["linuxMtd"]["plannedSteps"]]
    assert steps == ["probe-partitions", "copy-image", "erase", "write", "read-back", "cleanup"]
    assert "followUp" in entry
    assert _calls(tmp_path) == []


def test_dry_run_still_applies_the_partition_rules(tmp_path):
    rc, payload, _ = _flash(tmp_path, "{host: h1, flash_partition: mtd0}", "--dry-run", confirm=False)
    assert rc == 1 and "flash.linux-mtd-partition-refused" in _codes(payload)


@pytest.mark.parametrize(
    "flash_args",
    [
        "{host: '-oProxyCommand=touch /tmp/pwned', flash_partition: mtd1}",
        "{host: 'h1;touch /tmp/pwned', flash_partition: mtd1}",
        "{host: h1, user: 'root;id', flash_partition: mtd1}",
        "{host: h1, user: '-oX=y', flash_partition: mtd1}",
        "{host: h1, port: '22; id', flash_partition: mtd1}",
        "{host: h1, flash_partition: 'mtd1; rm -rf /'}",
        "{host: h1, flash_partition: '../dev/sda'}",
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


def test_hostile_local_path_is_one_argv_element_and_never_reaches_the_remote_shell(tmp_path):
    name = "a b;touch pwned$(id)`id`.bin"
    rc, payload, _ = _flash(tmp_path, image_name=name)
    assert rc == 0, payload
    scp = next(c for c in _calls(tmp_path) if c[0] == "scp")
    assert scp[scp.index("--") + 1].endswith(name)
    assert not any("pwned" in c for c in _remote_cmds(tmp_path))


def test_every_remote_token_is_shell_quoted():
    assert core.remote("rm", "-f", "/tmp/a b;$(id)") == "rm -f '/tmp/a b;$(id)'"
    spec = core.resolve_spec({"host": "h1", "flash_partition": "mtd1"})
    argv = core.ssh_argv(spec.target, core.remote_erase(spec))
    assert argv[-3:] == ["--", "h1", "flash_erase /dev/mtd1 0 0"]
    assert "BatchMode=yes" in argv


def test_proc_mtd_parsing():
    assert core.parse_proc_mtd(PROC_MTD) == {"mtd0": 0x200000, "mtd1": 0x400000}
