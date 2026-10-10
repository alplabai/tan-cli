# SPDX-License-Identifier: Apache-2.0
"""`linux_mtd`: deploy a V2N / V2N-M1 CM33 image over the RUNNING Linux on the A55
(tan-cli#1314) -- the pure half.

**The CM33 image is NOT a partition: it sits INSIDE `mtd1`.** On V2N / V2M `mtd1` is the
`fip` partition (BL31 + U-Boot from offset 0); BL2 copies the CM33 image raw from
`mtd1 + xspi_offset` (0x1A0000) to SRAM and silently truncates past `image_max`. So this
backend erases and writes only that window, never the partition, and the facts that bound it
-- `cm33_boot.{sram_base,image_pad,image_max,xspi_offset}` in the SoC metadata
(`metadata/socs/renesas/rzv2n/n44.json`) -- come from the bound SDK or, failing that, the
manifest's `flash_args`. They are never defaulted: without them the run refuses. An erase of
the whole partition would destroy the FIP and brick the board. The procedure is the one in
alp-sdk `examples/multicore/rpmsg-v2n/README.md` ("Pad and flash to mtd1 @ 0x1a0000"):
`flash_erase /dev/mtd1 0x1a0000 <blocks>` then `mtd_debug write /dev/mtd1 0x1a0000 <len> <file>`.

This module decides everything that needs no IO: target, partition rules, the stored-image
checks, the argv of every ssh/scp step, `/proc/mtd` parsing and the read-back verdict.
`tan.commands.flash_linux_mtd` runs it.

**Not validated on silicon.** No V2N bench place existed when this was written; the tests use
a fake ssh/scp. See docs/linux-mtd.md.

**Untrusted values never reach a shell unquoted.** Host, user, port and partition come from a
manifest (which a checkout controls) or a flag. Each is checked against a strict charset first
-- a host starting with `-` would otherwise be an ssh option -- and every token of the remote
command is `shlex.quote`d anyway. ssh/scp run with `BatchMode=yes` and a connect timeout.
"""
from __future__ import annotations

import re
import shlex
import struct
import uuid
from dataclasses import dataclass
from typing import Any

METHOD = "linux_mtd"

#: The bootloader / bl2 region. Refused by index AFTER name resolution, so a name cannot reach it.
FORBIDDEN_INDEXES = (0,)

CONNECT_TIMEOUT_S = 10

# No ':' (scp would read `host:path` splits and bracketed IPv6 is not supported) and no '%'.
_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}")
_USER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9._-]{0,63}")
_MTD_RE = re.compile(r"mtd([0-9]{1,3})")
_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")

FOLLOW_UP = (
    "The new image is in flash but is NOT running. A remoteproc restart works only on an "
    "image built with the dev-only ALP_V2N_CM33_SRAM_NS=\"1\" (docs/rzv2n-m33-secure-boot.md, "
    "Lifecycle; bench-pending). Otherwise do a full SoC reboot or a PSU cold cycle with DSW1 in "
    "mode 2 (xSPI BL2): under mode 1 (eMMC-boot BL2) the CM33 never starts. tan does none of "
    "these."
)

PARTIAL_STATE = (
    " -- the CM33 window was (partly) erased and may hold a partial image; do not reboot into "
    "it, re-flash first"
)
STILL_RUNNING = (
    "; the remote command may STILL be running on the board -- do not re-run until it has "
    "finished (a second concurrent erase/write corrupts the window)"
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
    #: Canonical `mtdN` (leading zeros stripped) or a /proc/mtd NAME.
    partition: str
    #: The /proc/mtd NAME the partition must carry (manifest `partition_name`), if declared.
    expect_name: str | None = None

    @property
    def by_index(self) -> bool:
        return _MTD_RE.fullmatch(self.partition) is not None


@dataclass(frozen=True)
class BootFacts:
    """`cm33_boot` from the SoC metadata. Every field is required; none is defaulted."""

    sram_base: int
    image_pad: int
    image_max: int
    xspi_offset: int


@dataclass(frozen=True)
class Row:
    index: int
    size: int
    erasesize: int
    name: str

    @property
    def device(self) -> str:
        return f"/dev/mtd{self.index}"


@dataclass(frozen=True)
class Layout:
    """Where a write lands, once `/proc/mtd` has been read."""

    row: Row
    offset: int
    blocks: int

    @property
    def device(self) -> str:
        return self.row.device


def _arg(flash_args: Any, key: str) -> Any:
    return flash_args.get(key) if isinstance(flash_args, dict) else None


# ── target and partition ────────────────────────────────────────────────────


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
            f"host {host!r} is not a plain hostname or IPv4 address (no ':' -- IPv6 literals "
            "are not supported; use a hostname)", code="flash.linux-mtd-invalid",
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


def canonical_partition(value: Any, what: str) -> str:
    """`mtd01` -> `mtd1` (so a leading zero cannot dodge an index comparison); a plain
    /proc/mtd name passes through. Anything else is invalid."""
    if isinstance(value, str):
        m = _MTD_RE.fullmatch(value)
        if m:
            return f"mtd{int(m.group(1))}"
        if _NAME_RE.fullmatch(value):
            return value
    raise LinuxMtdError(
        f"{what} {value!r} is neither an MTD device name like mtd1 nor a partition name",
        code="flash.linux-mtd-invalid",
    )


def _checked_partition(flash_args: Any, cli_partition: str | None) -> str:
    declared = _arg(flash_args, "flash_partition")
    if not declared:
        raise LinuxMtdError(
            "flash_args.flash_partition is required (e.g. mtd1) and is never defaulted",
            code="flash.linux-mtd-partition-required",
        )
    partition = canonical_partition(declared, "flash_args.flash_partition")
    if cli_partition is not None and canonical_partition(cli_partition, "--partition") != partition:
        raise LinuxMtdError(
            f"--partition {cli_partition!r} disagrees with the manifest's "
            f"flash_args.flash_partition {declared!r}; refusing to write either",
            code="flash.linux-mtd-partition-mismatch",
        )
    m = _MTD_RE.fullmatch(partition)
    if m and int(m.group(1)) in FORBIDDEN_INDEXES:
        raise LinuxMtdError(
            f"{partition} holds the bootloader and is never written by linux_mtd",
            code="flash.linux-mtd-partition-refused",
        )
    return partition


def _checked_name(flash_args: Any) -> str | None:
    name = _arg(flash_args, "partition_name")
    if name is None:
        return None
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise LinuxMtdError(
            f"flash_args.partition_name {name!r} is not a plain partition name",
            code="flash.linux-mtd-invalid",
        )
    return name


def resolve_spec(
    flash_args: Any, *, cli_host: str | None = None, cli_partition: str | None = None
) -> Spec:
    """Everything the manifest and flags say about the target, validated. Raises
    `LinuxMtdError`; nothing is defaulted."""
    target = _checked_target(flash_args, cli_host)
    partition = _checked_partition(flash_args, cli_partition)
    return Spec(target, partition, _checked_name(flash_args))


# ── the facts that bound the write ──────────────────────────────────────────

_FACT_KEYS = ("sram_base", "image_pad", "image_max", "xspi_offset")


def _facts_from(mapping: Any) -> BootFacts | None:
    if not isinstance(mapping, dict) or not any(k in mapping for k in _FACT_KEYS):
        return None
    vals = []
    for key in _FACT_KEYS:
        v = mapping.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise LinuxMtdError(
                f"cm33_boot fact {key} is missing or not a non-negative integer ({v!r}); "
                "refusing to guess a flash offset", code="flash.linux-mtd-boot-facts-unavailable",
            )
        vals.append(v)
    return BootFacts(*vals)


def resolve_facts(
    flash_args: Any, soc_cm33_boot: Any, unavailable: str, *, dry_run: bool = False
) -> tuple[BootFacts | None, bool]:
    """`(facts, authoritative)`. A real write needs the SoC metadata's `cm33_boot` from the
    bound SDK: a manifest is checkout-controlled and must never choose a flash offset. Manifest
    values may be given and must then agree with the SDK. When the SDK facts are unavailable
    the run refuses, naming WHY (`unavailable`); only a `--dry-run` goes on, with the manifest's
    numbers (or none) and `authoritative=False` so the plan is labelled as unverified."""
    sdk = _facts_from(soc_cm33_boot)
    manifest = _facts_from(flash_args)
    if sdk is None:
        if not dry_run:
            raise LinuxMtdError(
                f"no CM33 boot facts: the bound SDK's SoC metadata (cm33_boot: sram_base, "
                f"image_pad, image_max, xspi_offset) is unavailable -- {unavailable}. A flash "
                "offset is never taken from the manifest or defaulted; nothing was written",
                code="flash.linux-mtd-boot-facts-unavailable",
            )
        return manifest, False
    if manifest is not None and sdk != manifest:
        raise LinuxMtdError(
            f"flash_args {manifest} disagree with the SoC metadata's cm33_boot {sdk}; "
            "refusing to pick one", code="flash.linux-mtd-invalid",
        )
    if sdk.image_max <= sdk.image_pad + 8 or sdk.xspi_offset == 0:
        raise LinuxMtdError(
            f"cm33_boot facts are implausible ({sdk}); refusing", code="flash.linux-mtd-invalid"
        )
    return sdk, True


def expected_name(spec: Spec, soc_cm33_boot: Any) -> str:
    """The /proc/mtd NAME the partition must carry. REQUIRED: the SoC metadata's
    `cm33_boot.mtd_name` if it has one, else the manifest's `partition_name`, which must agree
    with the metadata when both exist. Without it nothing stops `flash_partition: mtd2` being
    erased at the FIP's CM33 offset, so the run refuses."""
    meta = soc_cm33_boot.get("mtd_name") if isinstance(soc_cm33_boot, dict) else None
    if isinstance(meta, str) and meta:
        if spec.expect_name is not None and spec.expect_name != meta:
            raise LinuxMtdError(
                f"flash_args.partition_name {spec.expect_name!r} disagrees with the SoC "
                f"metadata's {meta!r}", code="flash.linux-mtd-partition-mismatch",
            )
        return meta
    if spec.expect_name is None:
        raise LinuxMtdError(
            "flash_args.partition_name is required (e.g. `partition_name: fip`): the CM33 image "
            "sits inside one specific partition, and only its /proc/mtd NAME proves "
            f"{spec.partition} is it; nothing was written",
            code="flash.linux-mtd-partition-required",
        )
    return spec.expect_name


# ── the stored image ────────────────────────────────────────────────────────


def validate_image(head: bytes, size: int, facts: BootFacts) -> None:
    """The stored image is `image_pad` zero bytes + zephyr.bin (docs/provisioning-v2n.md,
    "CM33 image"). `head` is the file's first bytes. A raw zephyr.bin, a truncated file or an
    image too big for BL2 is refused before anything is copied."""
    pad, base = facts.image_pad, facts.sram_base
    if size > facts.image_max:
        raise LinuxMtdError(
            f"the image is larger than image_max (read {size} bytes); BL2 silently truncates past "
            f"{facts.image_max} (0x{facts.image_max:x})", code="flash.linux-mtd-image-invalid",
        )
    if size < pad + 8 or len(head) < pad + 8:
        raise LinuxMtdError(
            f"the image is {size} bytes, shorter than the {pad}-byte pad plus a vector table",
            code="flash.linux-mtd-image-invalid",
        )
    if any(head[:pad]):
        raise LinuxMtdError(
            f"the first {pad} bytes (0x{pad:x}) are not all zero -- this is not a padded "
            "image (a raw zephyr.bin must be prefixed with the zero pad)", code="flash.linux-mtd-image-invalid",
        )
    sp, reset = struct.unpack_from("<II", head, pad)
    if sp >> 24 != base >> 24:
        raise LinuxMtdError(
            f"initial SP 0x{sp:08x} (word at 0x{pad:x}) is outside SRAM0 (0x{base >> 24:02x}xxxxxx)",
            code="flash.linux-mtd-image-invalid",
        )
    lo, hi = base + pad, base + pad + facts.image_max
    if not reset & 1 or not lo <= reset & ~1 <= hi:
        raise LinuxMtdError(
            f"reset vector 0x{reset:08x} (word at 0x{pad + 4:x}) lacks the Thumb bit or lies "
            f"outside 0x{lo:08x}..0x{hi:08x}", code="flash.linux-mtd-image-invalid",
        )


# ── /proc/mtd ───────────────────────────────────────────────────────────────

_PROC_MTD_LINE = re.compile(
    r'^mtd([0-9]+):\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+"(.*)"\s*$'
)


def parse_proc_mtd(text: str) -> list[Row]:
    rows = []
    for line in text.splitlines():
        m = _PROC_MTD_LINE.match(line.strip())
        if m:
            rows.append(Row(int(m.group(1)), int(m.group(2), 16), int(m.group(3), 16), m.group(4)))
    return rows


def _find_row(spec: Spec, rows: list[Row]) -> Row:
    if spec.by_index:
        want = int(spec.partition[3:])
        found = [r for r in rows if r.index == want]
    else:
        found = [r for r in rows if r.name == spec.partition]
    if len(found) != 1:
        listed = ", ".join(f"mtd{r.index}={r.name!r}" for r in rows) or "none"
        raise LinuxMtdError(
            f"{spec.partition} is not (uniquely) in the target's /proc/mtd (found: {listed}); "
            "nothing was written", code="flash.linux-mtd-partition-absent",
        )
    return found[0]


def check_layout(spec: Spec, proc_mtd: str, facts: BootFacts, image_bytes: int,
                 want_name: str) -> Layout:
    """The write window, once `/proc/mtd` agrees with the declaration. Every refusal here is
    before anything is copied or erased."""
    row = _find_row(spec, parse_proc_mtd(proc_mtd))
    if row.index in FORBIDDEN_INDEXES:
        raise LinuxMtdError(
            f"{spec.partition} resolves to {row.device}, the bootloader; never written by "
            "linux_mtd", code="flash.linux-mtd-partition-refused",
        )
    if row.name != want_name:
        raise LinuxMtdError(
            f"{row.device} is named {row.name!r} in /proc/mtd, not the expected {want_name!r} "
            "(a reordered partition table?); nothing was written",
            code="flash.linux-mtd-partition-mismatch",
        )
    if row.erasesize <= 0 or facts.xspi_offset % row.erasesize:
        raise LinuxMtdError(
            f"offset 0x{facts.xspi_offset:x} is not a multiple of {row.device}'s erase size "
            f"0x{row.erasesize:x}; refusing an unaligned erase", code="flash.linux-mtd-invalid",
        )
    blocks = -(-image_bytes // row.erasesize)
    erase_end = facts.xspi_offset + blocks * row.erasesize
    if erase_end > row.size or erase_end > facts.xspi_offset + facts.image_max:
        raise LinuxMtdError(
            f"erasing {blocks} block(s) of 0x{row.erasesize:x} at 0x{facts.xspi_offset:x} would "
            f"end at 0x{erase_end:x}, past the {row.device} size 0x{row.size:x} or the CM33 "
            f"window (image_max 0x{facts.image_max:x}); nothing was written",
            code="flash.linux-mtd-image-too-large",
        )
    return Layout(row, facts.xspi_offset, blocks)


# ── argv of every step ──────────────────────────────────────────────────────


def _token() -> str:
    return uuid.uuid4().hex[:16]


def new_remote_paths() -> tuple[str, str]:
    """`(image, read-back)` temp paths on the target."""
    t = _token()
    return f"/tmp/tan-linux-mtd-{t}.bin", f"/tmp/tan-linux-mtd-rb-{t}.bin"


def _ssh_options() -> list[str]:
    return ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}"]


def ssh_argv(target: Target, remote_command: str) -> list[str]:
    """`remote_command` is ONE string for the remote shell: build it with `remote()`."""
    argv = ["ssh", *_ssh_options()]
    if target.port:
        argv += ["-p", str(target.port)]
    return [*argv, "--", target.destination, remote_command]


def scp_argv(target: Target, local_abs: str, remote_path: str) -> list[str]:
    """`local_abs` must be absolute so it can never be read as `host:path` or an option."""
    argv = ["scp", "-q", *_ssh_options()]
    if target.port:
        argv += ["-P", str(target.port)]
    return [*argv, "--", local_abs, f"{target.destination}:{remote_path}"]


def remote(*tokens: str) -> str:
    """One remote-shell command, every token quoted."""
    return " ".join(shlex.quote(t) for t in tokens)


def remote_proc_mtd() -> str:
    return remote("cat", "/proc/mtd")


def remote_erase(lay: Layout) -> str:
    return remote("flash_erase", lay.device, hex(lay.offset), str(lay.blocks))


def remote_write(lay: Layout, size: int, remote_path: str) -> str:
    return remote("mtd_debug", "write", lay.device, hex(lay.offset), str(size), remote_path)


def remote_read(lay: Layout, size: int, rb_path: str) -> str:
    return remote("mtd_debug", "read", lay.device, hex(lay.offset), str(size), rb_path)


def remote_sha(rb_path: str) -> str:
    return remote("sha256sum", rb_path)


def remote_cleanup(*paths: str) -> str:
    return remote("rm", "-f", *paths)


def parse_digest(output: str) -> str | None:
    """The 64-hex digest `sha256sum` printed, or `None`."""
    m = re.match(r"\s*([0-9a-fA-F]{64})\b", output)
    return m.group(1).lower() if m else None


def compare_digest(local: str, remote_out: str, device: str) -> str:
    """The read-back digest, once it matches the local one; else a coded error."""
    got = parse_digest(remote_out)
    if got is None:
        raise LinuxMtdError(
            f"the read-back of {device} produced no sha256 (got {remote_out.strip()[:120]!r})",
            code="flash.linux-mtd-failed",
        )
    if got != local.lower():
        raise LinuxMtdError(
            f"read-back mismatch on {device}: local sha256 {local}, flash holds {got}. "
            "The CM33 window may hold a bad image: do not reboot into it -- re-flash.",
            code="flash.linux-mtd-readback-mismatch",
        )
    return got


def planned_commands(spec: Spec, facts: BootFacts | None, local_abs: str, size: int | None,
                     paths: tuple[str, str]) -> list[dict]:
    """The steps a run would take, as shown by `--dry-run`. The device and block count are
    only known after `/proc/mtd` is read, so a name reference or the count shows a placeholder;
    without SDK facts (`facts` None) so does the offset."""
    n = size if size is not None else 0
    off = hex(facts.xspi_offset) if facts else "<xspi_offset: SDK facts unavailable>"
    device = f"/dev/{spec.partition}" if spec.by_index else f"/dev/mtd<{spec.partition}>"
    cmd_erase = remote("flash_erase", device, off, "<ceil(image/erasesize)>")
    cmd_write = remote("mtd_debug", "write", device, off, str(n), paths[0])
    cmd_read = remote("mtd_debug", "read", device, off, str(n), paths[1])
    steps = [
        ("probe-partitions", ssh_argv(spec.target, remote_proc_mtd())),
        ("copy-image", scp_argv(spec.target, local_abs, paths[0])),
        ("erase", ssh_argv(spec.target, cmd_erase)),
        ("write", ssh_argv(spec.target, cmd_write)),
        ("read-back", ssh_argv(spec.target, cmd_read)),
        ("digest", ssh_argv(spec.target, remote_sha(paths[1]))),
        ("cleanup", ssh_argv(spec.target, remote_cleanup(*paths))),
    ]
    return [{"step": name, "argv": argv} for name, argv in steps]
