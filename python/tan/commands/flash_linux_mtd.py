# SPDX-License-Identifier: Apache-2.0
"""`tan flash` backend `linux_mtd` (tan-cli#1314): the IO half.

Copies the padded CM33 image to the running Linux on a V2N / V2N-M1 A55 over scp, then
erases and writes ONLY the CM33 window inside the named MTD partition (`mtd1` is the FIP;
the window is at `cm33_boot.xspi_offset`) with `flash_erase` + `mtd_debug write`, reads that
window back with `mtd_debug read` and compares a sha256. The decisions (target, partition
rules, stored-image checks, argv, digest verdict) are in `tan.core.flash_linux_mtd`; this
module reads the SoC metadata, spawns ssh/scp and builds the entry.

It does NOT restart the remote processor or reboot the board: the envelope carries a
`followUp` saying what that takes. Not validated on silicon -- see docs/linux-mtd.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from tan.core import flash_linux_mtd as core
from tan.core.flash_plan import confirm_gate_note
from tan.core.subprocess_env import spawn_env
from tan.core.tool_lookup import resolve_tool
from tan.soc_ref import resolve_soc_path

_STEP_TIMEOUT_S = 300
_TIMED_OUT = 124
_TAIL = 300

Result = tuple[int, Any, list[str]]


def _spawn(argv: list[str]) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=_STEP_TIMEOUT_S, env=spawn_env(),
            stdin=subprocess.DEVNULL, check=False,
        )
    except subprocess.TimeoutExpired:
        return _TIMED_OUT, "", f"timed out after {_STEP_TIMEOUT_S}s"
    except OSError as err:
        return 127, "", str(err)
    return done.returncode, done.stdout or "", done.stderr or ""


def soc_cm33_boot(sdk_root: str | None, sku: str | None) -> tuple[Any, str]:
    """`(cm33_boot, reason)`: the `cm33_boot` object of `sku`'s SoC document in the bound SDK's
    metadata, or `(None, why-not)` -- the reason is surfaced, never swallowed."""
    if not sdk_root:
        return None, "no alp-sdk root is bound (pass --sdk-root)"
    if not sku:
        return None, "the system manifest names no SoM SKU"
    meta = Path(sdk_root) / "metadata"
    preset_path = meta / "e1m_modules" / f"{sku}.yaml"
    try:
        import yaml  # noqa: PLC0415

        preset = yaml.safe_load(preset_path.read_text(encoding="utf-8"))
    except ImportError:
        return None, "PyYAML is not installed"
    except (OSError, ValueError) as err:
        return None, f"the SoM preset {preset_path} is unreadable ({err})"
    path = resolve_soc_path(preset.get("silicon") if isinstance(preset, dict) else None, meta)
    if path is None:
        return None, f"the SoM preset {preset_path} has no resolvable `silicon:` reference"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return None, f"the SoC document {path} is unreadable ({err})"
    boot = doc.get("cm33_boot") if isinstance(doc, dict) else None
    return (boot, "") if boot is not None else (None, f"{path} has no cm33_boot object")


class _Deploy:
    """One slice's run: the resolved spec, the image facts and the envelope block."""

    copied = False

    def __init__(self, target: Any, ctx: Any, entry: Callable[..., Any], lines: list[str],
                 report: dict[str, Any], artefact_path: str, flash_args: Any) -> None:
        self.target, self.ctx, self.entry, self.lines = target, ctx, entry, lines
        self.report, self.flash_args = report, flash_args
        self.local = os.path.abspath(artefact_path)
        self.block: dict[str, Any] = {"steps": []}
        report["linuxMtd"] = self.block
        self.spec = core.Spec(core.Target("", None, None), "")
        self.facts: core.BootFacts | None = None
        self.authoritative = False
        self.unavailable = ""
        self.want_name = ""
        self.size: int | None = None
        self.data = b""
        self.snapshot_dir: str | None = None
        self.digest = ""
        self.paths = core.new_remote_paths()
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
            soc, why = soc_cm33_boot(self.ctx.sdk_root, self.ctx.sku)
            self.facts, self.authoritative = core.resolve_facts(
                self.flash_args, soc, why, dry_run=bool(self.ctx.dry_run)
            )
            self.unavailable = "" if self.authoritative else why
            self.want_name = core.expected_name(self.spec, soc)
            self.read_image()
        except core.LinuxMtdError as err:
            return self.fail(err.message, err.code)
        except OSError as err:
            return self.fail(f"cannot read the image {self.local} ({err})", "flash.linux-mtd-failed")
        f = self.facts
        self.block.update(
            target=self.spec.target.display(), partition=self.spec.partition,
            partitionName=self.want_name, image=self.local,
            bootFactsAuthoritative=self.authoritative,
            bootFacts=None if f is None else {
                "sramBase": f.sram_base, "imagePad": f.image_pad,
                "imageMax": f.image_max, "xspiOffset": f.xspi_offset},
        )
        if not self.authoritative:
            self.block["bootFactsUnavailable"] = self.unavailable
        return None

    def read_image(self) -> None:
        """Snapshot the file ONCE: size, digest, the stored-image checks and (for the copy)
        the bytes all come from this single read, so a file changing afterwards cannot make
        what is erased, written and compared differ from what was validated."""
        if not os.path.isfile(self.local):
            return
        cap = (self.facts.image_max if self.facts else 1 << 20) + 1
        with open(self.local, "rb") as fh:
            self.data = fh.read(cap)
        self.size = len(self.data)
        self.digest = hashlib.sha256(self.data).hexdigest()
        if self.facts is not None:
            core.validate_image(self.data[: self.facts.image_pad + 8], self.size, self.facts)
        self.block.update(imageBytes=self.size, sha256=self.digest)

    def snapshot_file(self) -> str:
        """The private copy of the snapshot that scp sends (mode 0600, removed afterwards)."""
        self.snapshot_dir = tempfile.mkdtemp(prefix="tan-linux-mtd-")
        path = os.path.join(self.snapshot_dir, "image.bin")
        with open(path, "wb") as fh:
            fh.write(self.data)
        return path

    def planned(self) -> list[dict]:
        steps = core.planned_commands(self.spec, self.facts, self.local, self.size, self.paths)
        self.block["plannedSteps"] = steps
        self.report["followUp"] = core.FOLLOW_UP
        return steps

    def preview(self) -> Result | None:
        """The dry-run and not-confirmed arms; `None` when a real write should proceed."""
        if self.ctx.dry_run:
            shown = "; ".join(f"{p['step']}: {' '.join(p['argv'])}" for p in self.planned())
            note = (
                "" if self.authoritative else
                " [SDK cm33_boot facts UNAVAILABLE -- the offset below is NOT authoritative and "
                f"a real run would refuse: {self.unavailable}]"
            )
            return self.done(
                "ok",
                f"would run: {shown} (dry-run: /proc/mtd was not read, so the partition, its "
                f"name and the erase block count were not checked){note}",
            )
        if not self.size:
            why = "is empty" if self.size == 0 else "does not exist; build first"
            return self.fail(f"the image {self.local} {why}", "flash.linux-mtd-failed")
        confirmed = self.ctx.force_confirm or (
            isinstance(self.flash_args, dict) and self.flash_args.get("confirm") is True
        )
        if confirmed:
            return None
        self.planned()
        return self.done(
            "planned",
            f"would write {os.path.basename(self.local)} -> {self.spec.partition} at "
            f"0x{self.facts.xspi_offset:x} on {self.spec.target.display()} -- NOT written: "
            f"{confirm_gate_note('flash_args.confirm is false')}",
        )

    def run(self, step: str, argv: list[str]) -> tuple[int, str, str]:
        rc, out, err = _spawn([self.tools[argv[0]], *argv[1:]])
        self.block["steps"].append({"step": step, "argv": argv, "rc": rc})
        return rc, out, err

    def step_failed(self, step: str, res: tuple[int, str, str], hazard: str = "") -> Result:
        rc, out, err = res
        tail = (err.strip() or out.strip())[-_TAIL:]
        running = core.STILL_RUNNING if rc == _TIMED_OUT and step != "copy-image" else ""
        return self.fail(
            f"{step} failed (rc {rc}): {tail}{hazard}{running}", "flash.linux-mtd-failed"
        )

    def stage(self, step: str, remote_cmd: str, hazard: str = "") -> tuple[Result | None, str]:
        res = self.run(step, core.ssh_argv(self.spec.target, remote_cmd))
        return (self.step_failed(step, res, hazard) if res[0] else None), res[1]

    def write(self) -> Result | None:
        """probe, copy, erase, write, read back. `None` when every step passed."""
        size = self.size or 0
        failed, proc_mtd = self.stage("probe-partitions", core.remote_proc_mtd())
        if failed:
            return failed
        try:
            lay = core.check_layout(self.spec, proc_mtd, self.facts, size, self.want_name)
        except core.LinuxMtdError as err:
            return self.fail(err.message, err.code)
        self.block.update(
            device=lay.device, partitionBytes=lay.row.size, eraseSize=lay.row.erasesize,
            offset=lay.offset, eraseBlocks=lay.blocks,
        )
        self.copied = True
        res = self.run(
            "copy-image", core.scp_argv(self.spec.target, self.snapshot_file(), self.paths[0])
        )
        if res[0]:
            return self.step_failed("copy-image", res)
        for step, cmd, hazard in (
            ("erase", core.remote_erase(lay), core.PARTIAL_STATE),
            ("write", core.remote_write(lay, size, self.paths[0]), core.PARTIAL_STATE),
            ("read-back", core.remote_read(lay, size, self.paths[1]), " -- the write is unverified"),
        ):
            failed, _ = self.stage(step, cmd, hazard)
            if failed:
                return failed
        failed, out = self.stage("digest", core.remote_sha(self.paths[1]), " -- the write is unverified")
        if failed:
            return failed
        got = core.parse_digest(out)
        self.block["digest"] = {
            "algorithm": "sha256", "local": self.digest, "readBack": got, "match": got == self.digest,
        }
        try:
            core.compare_digest(self.digest, out, lay.device)
        except core.LinuxMtdError as err:
            return self.fail(err.message, err.code)
        return None

    def cleanup(self) -> None:
        res = self.run("cleanup", core.ssh_argv(self.spec.target, core.remote_cleanup(*self.paths)))
        self.block["tempFileRemoved"] = res[0] == 0
        self.block["tempFiles"] = list(self.paths)


def ignored_options_note(targets: Any, select_method: Callable[[Any], Any]) -> str | None:
    """A warning when `--target-host` / `--partition` were given but no target in this run
    uses `linux_mtd`, so the operator is not left thinking they took effect."""
    if any((select_method(t) or t.flash_method) == core.METHOD for t in targets):
        return None
    return (
        "flash: --target-host / --partition only apply to flash_method linux_mtd; no entry "
        "in this run uses it, so they had no effect."
    )


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
        if job.snapshot_dir:
            shutil.rmtree(job.snapshot_dir, ignore_errors=True)
    if failed is not None:
        return failed
    report["followUp"] = core.FOLLOW_UP
    b = job.block
    msg = (
        f"{core.METHOD}[{target.id}]: wrote {job.size} bytes to {b['device']} at "
        f"0x{b['offset']:x} on {job.spec.target.display()}; read-back sha256 matches "
        f"({job.digest}). {core.FOLLOW_UP}"
    )
    return job.done("ok", msg, prefix="ok: ")
