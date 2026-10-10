# `linux_mtd` — deploying a V2N / V2N-M1 CM33 image over the running Linux (tan-cli#1314)

> **Not validated on silicon.** No V2N bench place existed when this backend was
> written. The steps mirror the procedure in alp-sdk
> `examples/multicore/rpmsg-v2n/README.md` ("Pad and flash to mtd1 @ 0x1a0000") and are
> tested only against a fake `ssh`/`scp`. It needs a V2N bench run before you rely on it.

`xspi_flashwriter` handles `mtd0`/`mtd1` only as bl2/fip over SCIF (its real write is
HW-gated), and `zephyr_west_flash` has no RZ/V2N CM33 runner. On V2N / V2N-M1 the CM33
image is therefore deployed from Linux on the A55. `flash_method: linux_mtd` does that.

## The CM33 image is inside `mtd1`, not a partition of its own

On V2N / V2M `mtd1` is the **`fip` partition** (BL31 + U-Boot from offset 0). BL2 copies the
CM33 image raw from `mtd1` + `cm33_boot.xspi_offset` (**0x1A0000**) to SRAM and silently
truncates anything past `image_max` (0x30000). **Erasing or writing the whole of `mtd1`
destroys the FIP and bricks the board.** `linux_mtd` therefore erases and writes only the
window `[xspi_offset, xspi_offset + ceil(image/erasesize) * erasesize)`.

The four numbers that bound the window come from the SoC metadata's `cm33_boot`
(`metadata/socs/renesas/rzv2n/n44.json`: `sram_base`, `image_pad`, `image_max`,
`xspi_offset`), read from the bound alp-sdk (`--sdk-root`). If no SDK metadata is readable,
the same four keys may be given in `flash_args`; if both exist they must agree. With neither,
the run refuses (`flash.linux-mtd-boot-facts-unavailable`): **there is no default offset.**

## Manifest

```yaml
slices:
- core_id: cm33
  os: zephyr
  output_artefact: m33_fw.bin     # the PADDED image, see below
  flash_method: linux_mtd
  flash_args:
    host: 192.168.1.50            # or pass --target-host; a hostname or IPv4 address (no ':')
    user: root                    # optional
    port: 22                      # optional
    flash_partition: mtd1         # REQUIRED, never defaulted; mtdN or a /proc/mtd name
    partition_name: fip           # optional: the /proc/mtd NAME this device must carry
    confirm: true                 # or --confirm / ALP_FLASH_FORCE=1, like every writing backend
```

## The image must be padded

The stored image is `0x3000` zero bytes followed by Zephyr's `zephyr.bin`
(docs/provisioning-v2n.md, "CM33 image"). Before copying anything tan checks, from the
`cm33_boot` numbers: total size <= `image_max`; the first `image_pad` bytes are zero; the
initial SP (word at `0x3000`) is in SRAM0 (`0x08xxxxxx`); the reset vector (word at `0x3004`)
has the Thumb bit and lies in `0x08003000..0x08033000`. A raw `zephyr.bin` is refused
(`flash.linux-mtd-image-invalid`).

## What it does

1. `ssh … cat /proc/mtd` — find the partition (by `mtdN` or by name), take its size and erase
   size. Refuse when it is absent, is `mtd0` (also when a name resolves there), its NAME
   differs from `partition_name`, `xspi_offset` is not a multiple of the erase size, or the
   erase would end past the partition or past `xspi_offset + image_max`.
2. `scp` the image to a fresh `/tmp/tan-linux-mtd-<random>.bin`.
3. `flash_erase /dev/mtdN 0x1a0000 <ceil(len/erasesize)>`.
4. `mtd_debug write /dev/mtdN 0x1a0000 <len> <tmp>`.
5. `mtd_debug read /dev/mtdN 0x1a0000 <len> /tmp/tan-linux-mtd-rb-<random>.bin`, then
   `sha256sum` of that file, compared with the local sha256. Each command's exit status is
   checked on its own. A difference is `flash.linux-mtd-readback-mismatch`.
6. `rm -f` both temp files (also after a failure). A failure to remove them is the warning
   `flash.linux-mtd-cleanup-failed`.

`ssh`/`scp` run with `BatchMode=yes` and `ConnectTimeout=10`: key authentication only, a
password prompt is never possible. Host key checking is left at your ssh defaults. The target
needs `flash_erase`, `mtd_debug` (mtd-utils), `sha256sum` and `rm`.

## Guards

| Refusal | Code |
|---|---|
| neither `flash_args.host` nor `--target-host` | `flash.linux-mtd-no-host` |
| `flash_args.flash_partition` absent | `flash.linux-mtd-partition-required` |
| partition is `mtd0` (any spelling, or a name that resolves there) | `flash.linux-mtd-partition-refused` |
| `--partition` differs from the manifest; `/proc/mtd` NAME differs from `partition_name` | `flash.linux-mtd-partition-mismatch` |
| partition not in the target's `/proc/mtd` | `flash.linux-mtd-partition-absent` |
| erase window past the partition or `image_max` | `flash.linux-mtd-image-too-large` |
| no `cm33_boot` facts | `flash.linux-mtd-boot-facts-unavailable` |
| image not a valid padded CM33 image | `flash.linux-mtd-image-invalid` |
| host, user, port or partition not a plain value; unaligned offset; flash_args vs metadata disagree | `flash.linux-mtd-invalid` |
| an ssh/scp step failed or timed out | `flash.linux-mtd-failed` |
| read-back differs | `flash.linux-mtd-readback-mismatch` |

`--partition` is a cross-check, never an override; `mtd01` is the same as `mtd1`.
`--target-host`/`--partition` given when no entry uses `linux_mtd` warn
(`flash.linux-mtd-option-ignored`). Host and user are checked against a strict character set
(a host starting with `-` would be an ssh option) and every token of the remote command is
shell-quoted; the local image path is passed to `scp` as an absolute path.

`--dry-run` prints the seven commands without running any of them (so `/proc/mtd` is not read
and the partition, name and block count are not checked); the host, partition, facts and image
rules still apply.

## After the write

tan does **not** restart anything. The entry's `followUp` says what running the new image takes:
the CM33 cannot be restarted from Linux except with the dev-only remoteproc stop/start on an
image built with `ALP_V2N_CM33_SRAM_NS="1"` (docs/rzv2n-m33-secure-boot.md, "Lifecycle";
bench-pending). Otherwise do a full SoC reboot or a PSU cold cycle with **DSW1 in mode 2 (xSPI
BL2)**; under mode 1 (eMMC-boot BL2) the CM33 never starts.

If a step fails after the erase starts, the message says the CM33 window may hold a partial
image: re-flash before rebooting. After a timeout the remote `flash_erase`/`mtd_debug` may still
be running on the board; do not re-run until it has finished.

The entry's `linuxMtd` block records each step (`steps[]` with argv and rc), the device, offset,
erase size and block count, the `cm33_boot` numbers used, and `digest` (`local`, `readBack`,
`match`).
