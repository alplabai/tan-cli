# SPDX-License-Identifier: Apache-2.0
"""`tan flash` backend `linux_mtd` (tan-cli#1314): the IO half.

Copies the image to the running Linux on a V2N / V2N-M1 A55 over scp, then erases and
writes the named MTD partition with `flash_erase` + `flashcp`, reads it back and compares
a sha256. The decisions (target, partition rules, argv, digest judgement) are in
`tan.core.flash_linux_mtd`; this module only spawns ssh/scp and builds the entry.

It does NOT restart the remote processor or reboot the board: the envelope carries a
`followUp` saying so. Not validated on silicon -- see docs/linux-mtd.md.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, Callable

from tan.core import flash_linux_mtd as core
from tan.core.flash_plan import confirm_gate_note
from tan.core.flow_d_report import sha256_of
from tan.core.subprocess_env import spawn_env
from tan.core.tool_lookup import resolve_tool

_STEP_TIMEOUT_S = 300
_TAIL = 300

Result = tuple[int, Any, list[str]]


def _spawn(argv: list[str]) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=_STEP_TIMEOUT_S, env=spawn_env(),
            stdin=subprocess.DEVNULL, check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {_STEP_TIMEOUT_S}s"
    except OSError as err:
        return 127, "", str(err)
    return done.returncode, done.stdout or "", done.stderr or ""


class _Deploy:
    """One slice's run: the resolved spec, the image facts and the envelope block."""

    def __init__(self, target: Any, ctx: Any, entry: Callable[..., Any], lines: list[str],
                 report: dict[str, Any], artefact_path: str, flash_args: Any) -> None:
        self.target, self.ctx, self.entry, self.lines = target, ctx, entry, lines
        self.report, self.artefact_path, self.flash_args = report, artefact_path, flash_args
        self.block: dict[str, Any] = {"steps": []}
        report["linuxMtd"] = self.block
        self.spec = core.Spec(core.Target("", None, None), "")
        self.size: int | None = None
        self.digest = ""
        self.remote_path = core.new_remote_path()
        self.tools: dict[str, str] = {}

    def fail(self, message: str, code: str) -> Result:
        text = f"{core.METHOD}[{self.target.id}]: {message}"
        self.lines.append(f"  FAIL: {text}")
        return 1, self.entry(core.METHOD, "failed", 1, text, issue_code=code), self.lines

    def done(self, status: str, text: str, prefix: str = "") -> Result:
        self.lines.append(f"  {prefix}{text}")
        return 0, self.entry(core.METHOD, status, 0, text), self.lines

    def resolve(self) -> Result | None:
        try:
            self.spec = core.resolve_spec(
                self.flash_args, cli_host=self.ctx.target_host,
                cli_partition=self.ctx.target_partition,
            )
        except core.LinuxMtdError as err:
            return self.fail(err.message, err.code)
        self.block.update(
            target=self.spec.target.display(), partition=self.spec.partition,
            device=self.spec.device, image=self.artefact_path,
        )
        try:
            if os.path.isfile(self.artefact_path):
                self.size = os.path.getsize(self.artefact_path)
                self.digest = sha256_of(self.artefact_path)
                self.block.update(imageBytes=self.size, sha256=self.digest)
        except OSError as err:
            return self.fail(
                f"cannot read the image {self.artefact_path} ({err})", "flash.linux-mtd-failed"
            )
        return None

    def planned(self) -> list[dict]:
        steps = core.planned_commands(self.spec, self.artefact_path, self.size, self.remote_path)
        self.block["plannedSteps"] = steps
        self.report["followUp"] = core.FOLLOW_UP
        return steps

    def preview(self) -> Result | None:
        """The dry-run and not-confirmed arms; `None` when a real write should proceed."""
        if self.ctx.dry_run:
            shown = "; ".join(f"{p['step']}: {' '.join(p['argv'])}" for p in self.planned())
            return self.done(
                "ok",
                f"would run: {shown} (dry-run: /proc/mtd was not read, so the partition size "
                "was not checked)",
            )
        if self.size is None:
            return self.fail(
                f"the image {self.artefact_path} does not exist; build first",
                "flash.linux-mtd-failed",
            )
        if self.size == 0:
            return self.fail(f"the image {self.artefact_path} is empty", "flash.linux-mtd-failed")
        confirmed = self.ctx.force_confirm or (
            isinstance(self.flash_args, dict) and self.flash_args.get("confirm") is True
        )
        if confirmed:
            return None
        self.planned()
        return self.done(
            "planned",
            f"would write {os.path.basename(self.artefact_path)} -> {self.spec.device} on "
            f"{self.spec.target.display()} -- NOT written: "
            f"{confirm_gate_note('flash_args.confirm is false')}",
        )

    def run(self, step: str, argv: list[str]) -> tuple[int, str, str]:
        real = [self.tools[argv[0]], *argv[1:]]
        rc, out, err = _spawn(real)
        self.block["steps"].append({"step": step, "argv": argv, "rc": rc})
        return rc, out, err

    def step_failed(self, step: str, res: tuple[int, str, str], hazard: str = "") -> Result:
        rc, out, err = res
        tail = (err.strip() or out.strip())[-_TAIL:]
        return self.fail(f"{step} failed (rc {rc}): {tail}{hazard}", "flash.linux-mtd-failed")

    def write(self) -> Result | None:
        """probe, copy, erase, write, read back. `None` when every step passed."""
        spec, size = self.spec, self.size or 0
        res = self.run("probe-partitions", core.ssh_argv(spec.target, core.remote_proc_mtd()))
        if res[0] != 0:
            return self.step_failed("probe-partitions", res)
        try:
            self.block["partitionBytes"] = core.check_partition(spec, res[1], size)
        except core.LinuxMtdError as err:
            return self.fail(err.message, err.code)
        self.copied = True
        res = self.run("copy-image", core.scp_argv(spec.target, self.artefact_path, self.remote_path))
        if res[0] != 0:
            return self.step_failed("copy-image", res)
        res = self.run("erase", core.ssh_argv(spec.target, core.remote_erase(spec)))
        if res[0] != 0:
            return self.step_failed("erase", res)
        res = self.run("write", core.ssh_argv(spec.target, core.remote_write(spec, self.remote_path)))
        if res[0] != 0:
            return self.step_failed(
                "write", res,
                f" -- {spec.partition} was erased and may hold a partial image; re-flash before reboot",
            )
        res = self.run("read-back", core.ssh_argv(spec.target, core.remote_readback(spec, size)))
        if res[0] != 0:
            return self.step_failed("read-back", res, " -- the write is unverified")
        read_back = core.parse_digest(res[1])
        self.block["digest"] = {
            "algorithm": "sha256", "local": self.digest, "readBack": read_back,
            "match": read_back == self.digest,
        }
        try:
            core.compare_digest(self.digest, res[1], spec)
        except core.LinuxMtdError as err:
            return self.fail(err.message, err.code)
        return None

    def cleanup(self) -> None:
        res = self.run("cleanup", core.ssh_argv(self.spec.target, core.remote_cleanup(self.remote_path)))
        self.block["tempFileRemoved"] = res[0] == 0

    copied = False


def run_linux_mtd_entry(
    target: Any,
    ctx: Any,
    *,
    artefact_path: str,
    flash_args: Any,
    entry: Callable[..., Any],
    lines: list[str],
    report: dict[str, Any],
) -> Result:
    """One slice's deploy. Returns `(rc, entry, text-lines)` like `_flash_entry`."""
    job = _Deploy(target, ctx, entry, lines, report, artefact_path, flash_args)
    refused = job.resolve() or job.preview()
    if refused is not None:
        return refused
    env = spawn_env()
    for tool in ("ssh", "scp"):
        found = resolve_tool(tool, env).resolved
        if found is None:
            return job.fail("ssh and scp must both be on PATH", "flash.linux-mtd-failed")
        job.tools[tool] = found
    try:
        failed = job.write()
    finally:
        if job.copied:
            job.cleanup()
    if failed is not None:
        return failed
    report["followUp"] = core.FOLLOW_UP
    spec = job.spec
    msg = (
        f"{core.METHOD}[{target.id}]: wrote {job.size} bytes to {spec.device} on "
        f"{spec.target.display()}; read-back sha256 matches ({job.digest}). {core.FOLLOW_UP}"
    )
    return job.done("ok", msg, prefix="ok: ")
