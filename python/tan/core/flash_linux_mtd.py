# SPDX-License-Identifier: Apache-2.0
"""`linux_mtd`: deploy a V2N / V2N-M1 CM33 image over the RUNNING Linux on the A55
(tan-cli#1314) -- the pure half.

The CM33 image lives in the `mtd1` partition. The bench does it by copying the
padded image to the board and running `flash_erase` + `flashcp` there; this module
decides everything about that route that needs no IO: who the target is, which
partition may be written, the exact argv of every ssh/scp step, how `/proc/mtd` is
read, and how the read-back digest is judged. `tan.commands.flash_linux_mtd` runs it.

**Not validated on silicon.** No V2N bench place existed when this was written; the
steps follow the maintainer's manual procedure and are tested against fake ssh/scp
only. See docs/linux-mtd.md.

**Untrusted values never reach a shell unquoted.** Host, user, port and partition come
from a manifest (which a checkout controls) or a flag. Each is checked against a strict
charset first -- a host starting with `-` would otherwise be an ssh option
(`-oProxyCommand=...`) -- and every token of the remote command is `shlex.quote`d anyway.
ssh/scp run with `BatchMode=yes` and a connect timeout, so there is never a password prompt.
"""
from __future__ import annotations

import re
import shlex
import uuid
from dataclasses import dataclass
from typing import Any

METHOD = "linux_mtd"

#: Partition names that may never be written. `mtd0` is the bootloader / bl2 region.
FORBIDDEN_PARTITIONS = ("mtd0",)

CONNECT_TIMEOUT_S = 10

_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:%-]{0,252}")
_USER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9._-]{0,63}")
_PARTITION_RE = re.compile(r"mtd[0-9]{1,3}")

FOLLOW_UP = (
    "The new image is in flash but is NOT running: restart the remote processor "
    "(e.g. `echo stop > /sys/class/remoteproc/remoteprocN/state` then `echo start > ...`) "
    "or reboot the board to run it. tan does not do either."
)


class LinuxMtdError(Exception):
    """A refusal or failure, carrying the registered issue code it reports under."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Target:
    host: str
    user: str | None
    port: int | None

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def display(self) -> str:
        return self.destination + (f":{self.port}" if self.port else "")


@dataclass(frozen=True)
class Spec:
    target: Target
    partition: str  # `mtdN`

    @property
    def device(self) -> str:
        return f"/dev/{self.partition}"


def _arg(flash_args: Any, key: str) -> Any:
    return flash_args.get(key) if isinstance(flash_args, dict) else None


def _checked_target(flash_args: Any, cli_host: str | None) -> Target:
    host = cli_host or _arg(flash_args, "host")
    if not host:
        raise LinuxMtdError(
            "no target host: set flash_args.host in the manifest or pass --target-host",
            code="flash.linux-mtd-no-host",
        )
    user = _arg(flash_args, "user")
    port = _arg(flash_args, "port")
    if not isinstance(host, str) or not _HOST_RE.fullmatch(host):
        raise LinuxMtdError(
            f"host {host!r} is not a plain hostname or IP address", code="flash.linux-mtd-invalid"
        )
    if user is not None and (not isinstance(user, str) or not _USER_RE.fullmatch(user)):
        raise LinuxMtdError(
            f"flash_args.user {user!r} is not a plain user name", code="flash.linux-mtd-invalid"
        )
    if port is not None and (
        isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
    ):
        raise LinuxMtdError(
            f"flash_args.port {port!r} is not a TCP port number", code="flash.linux-mtd-invalid"
        )
    return Target(host, user, port)


def _checked_partition(flash_args: Any, cli_partition: str | None) -> str:
    declared = _arg(flash_args, "flash_partition")
    if not declared:
        raise LinuxMtdError(
            "flash_args.flash_partition is required (e.g. mtd1) and is never defaulted",
            code="flash.linux-mtd-partition-required",
        )
    if not isinstance(declared, str) or not _PARTITION_RE.fullmatch(declared):
        raise LinuxMtdError(
            f"flash_args.flash_partition {declared!r} is not an MTD device name like mtd1",
            code="flash.linux-mtd-invalid",
        )
    if cli_partition is not None and cli_partition != declared:
        raise LinuxMtdError(
            f"--partition {cli_partition!r} disagrees with the manifest's "
            f"flash_args.flash_partition {declared!r}; refusing to write either",
            code="flash.linux-mtd-partition-mismatch",
        )
    if declared in FORBIDDEN_PARTITIONS:
        raise LinuxMtdError(
            f"{declared} holds the bootloader and is never written by linux_mtd",
            code="flash.linux-mtd-partition-refused",
        )
    return declared


def resolve_spec(
    flash_args: Any, *, cli_host: str | None = None, cli_partition: str | None = None
) -> Spec:
    """Everything the manifest and flags say about the target, validated. Raises
    `LinuxMtdError`; nothing is defaulted."""
    target = _checked_target(flash_args, cli_host)
    return Spec(target, _checked_partition(flash_args, cli_partition))


# ── /proc/mtd ───────────────────────────────────────────────────────────────

_PROC_MTD_LINE = re.compile(r'^(mtd[0-9]+):\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+"(.*)"\s*$')


def parse_proc_mtd(text: str) -> dict[str, int]:
    """`{ "mtd1": size_in_bytes, ... }` from `/proc/mtd` (sizes are hex bytes)."""
    parts: dict[str, int] = {}
    for line in text.splitlines():
        m = _PROC_MTD_LINE.match(line.strip())
        if m:
            parts[m.group(1)] = int(m.group(2), 16)
    return parts


def check_partition(spec: Spec, proc_mtd: str, image_bytes: int) -> int:
    """The partition's size, once it is known to exist and to hold the image."""
    parts = parse_proc_mtd(proc_mtd)
    size = parts.get(spec.partition)
    if size is None:
        raise LinuxMtdError(
            f"{spec.partition} is not in the target's /proc/mtd (found: "
            f"{', '.join(sorted(parts)) or 'none'}); nothing was written",
            code="flash.linux-mtd-partition-absent",
        )
    if image_bytes > size:
        raise LinuxMtdError(
            f"the image is {image_bytes} bytes but {spec.partition} is {size}; nothing was written",
            code="flash.linux-mtd-image-too-large",
        )
    return size


# ── argv of every step ──────────────────────────────────────────────────────


def new_remote_path() -> str:
    return f"/tmp/tan-linux-mtd-{uuid.uuid4().hex[:16]}.bin"


def _ssh_options() -> list[str]:
    return ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}"]


def ssh_argv(target: Target, remote_command: str) -> list[str]:
    """`remote_command` is ONE string for the remote shell: build it with `remote()`."""
    argv = ["ssh", *_ssh_options()]
    if target.port:
        argv += ["-p", str(target.port)]
    return [*argv, "--", target.destination, remote_command]


def scp_argv(target: Target, local: str, remote_path: str) -> list[str]:
    argv = ["scp", "-q", *_ssh_options()]
    if target.port:
        argv += ["-P", str(target.port)]
    return [*argv, "--", local, f"{target.destination}:{remote_path}"]


def remote(*tokens: str) -> str:
    """One remote-shell command, every token quoted."""
    return " ".join(shlex.quote(t) for t in tokens)


def remote_proc_mtd() -> str:
    return remote("cat", "/proc/mtd")


def remote_erase(spec: Spec) -> str:
    return remote("flash_erase", spec.device, "0", "0")


def remote_write(spec: Spec, remote_path: str) -> str:
    return remote("flashcp", "-v", remote_path, spec.device)


def remote_readback(spec: Spec, image_bytes: int) -> str:
    """sha256 of the first `image_bytes` of the partition (not of the whole partition,
    which is larger than the image and still holds erased 0xFF past it)."""
    return (
        remote("head", "-c", str(image_bytes), spec.device) + " | " + remote("sha256sum")
    )


def remote_cleanup(remote_path: str) -> str:
    return remote("rm", "-f", remote_path)


def parse_digest(output: str) -> str | None:
    """The 64-hex digest `sha256sum` printed, or `None`."""
    m = re.match(r"\s*([0-9a-fA-F]{64})\b", output)
    return m.group(1).lower() if m else None


def compare_digest(local: str, remote_out: str, spec: Spec) -> str:
    """The read-back digest, once it matches the local one; else a coded error."""
    got = parse_digest(remote_out)
    if got is None:
        raise LinuxMtdError(
            f"the read-back of {spec.partition} produced no sha256 (got {remote_out.strip()[:120]!r})",
            code="flash.linux-mtd-failed",
        )
    if got != local.lower():
        raise LinuxMtdError(
            f"read-back mismatch on {spec.partition}: local sha256 {local}, flash holds {got}. "
            "The partition may hold a bad image; do not reboot into it -- re-flash.",
            code="flash.linux-mtd-readback-mismatch",
        )
    return got


def planned_commands(spec: Spec, local: str, image_bytes: int | None, remote_path: str) -> list[dict]:
    """The steps a run would take, as shown by `--dry-run`."""
    n = image_bytes if image_bytes is not None else 0
    steps = [
        ("probe-partitions", ssh_argv(spec.target, remote_proc_mtd())),
        ("copy-image", scp_argv(spec.target, local, remote_path)),
        ("erase", ssh_argv(spec.target, remote_erase(spec))),
        ("write", ssh_argv(spec.target, remote_write(spec, remote_path))),
        ("read-back", ssh_argv(spec.target, remote_readback(spec, n))),
        ("cleanup", ssh_argv(spec.target, remote_cleanup(remote_path))),
    ]
    return [{"step": name, "argv": argv} for name, argv in steps]
