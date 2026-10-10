# `linux_mtd` — deploying a V2N / V2N-M1 CM33 image over the running Linux (tan-cli#1314)

> **Not validated on silicon.** No V2N bench place existed when this backend was
> written. The steps follow the maintainer's manual procedure and are tested only
> against a fake `ssh`/`scp`. It needs a V2N bench run before you rely on it.

`xspi_flashwriter` handles `mtd0`/`mtd1` only as bl2/fip over SCIF (its real write is
HW-gated), and `zephyr_west_flash` has no RZ/V2N CM33 runner. On V2N / V2N-M1 the CM33
image is therefore deployed from Linux on the A55: copy the padded image to the board,
erase the `mtd1` partition, write it. `flash_method: linux_mtd` does exactly that.

## Manifest

```yaml
slices:
- core_id: cm33
  os: zephyr
  output_artefact: m33_fw.bin
  flash_method: linux_mtd
  flash_args:
    host: 192.168.1.50        # or pass --target-host
    user: root                # optional
    port: 22                  # optional
    flash_partition: mtd1     # REQUIRED, never defaulted
    confirm: true             # or --confirm / ALP_FLASH_FORCE=1, like every writing backend
```

## What it does

1. `ssh … cat /proc/mtd` — the named partition must be listed and at least as large as
   the image; otherwise nothing is written.
2. `scp` the image to a fresh `/tmp/tan-linux-mtd-<random>.bin` on the target.
3. `flash_erase /dev/mtdN 0 0`.
4. `flashcp -v <tmp> /dev/mtdN`.
5. Read back the first image-length bytes (`head -c <n> /dev/mtdN | sha256sum`) and compare
   with the local sha256. A difference is `flash.linux-mtd-readback-mismatch`.
6. `rm -f` the temp file (also after a failure).

`ssh`/`scp` run with `BatchMode=yes` and `ConnectTimeout=10`: key authentication only,
a password prompt is never possible. Host key checking is left at your ssh defaults.
The target needs `flash_erase`, `flashcp`, `head` and `sha256sum` (mtd-utils and
coreutils/busybox); `scp` needs whatever transfer protocol your `scp` and the board's
sshd agree on.

## Guards

| Refusal | Code |
|---|---|
| neither `flash_args.host` nor `--target-host` | `flash.linux-mtd-no-host` |
| `flash_args.flash_partition` absent | `flash.linux-mtd-partition-required` |
| partition is `mtd0` (bootloader) | `flash.linux-mtd-partition-refused` |
| `--partition` differs from the manifest's `flash_partition` | `flash.linux-mtd-partition-mismatch` |
| partition not in the target's `/proc/mtd` | `flash.linux-mtd-partition-absent` |
| image larger than the partition | `flash.linux-mtd-image-too-large` |
| host, user, port or partition is not a plain value | `flash.linux-mtd-invalid` |
| an ssh/scp step failed | `flash.linux-mtd-failed` |
| read-back differs | `flash.linux-mtd-readback-mismatch` |

`--partition` is a cross-check, never an override. The partition must be an `mtdN` device
name. Host and user are checked against a strict character set (a host starting with `-`
would be an ssh option) and every token of the remote command is shell-quoted.

`--dry-run` prints the six commands without running any of them (so `/proc/mtd` is not
read and the size check is skipped); the host and partition rules above still apply.

## After the write

tan does **not** restart the remote processor or reboot the board. The entry carries a
`followUp` field saying so; do it yourself (stop/start the remoteproc, or reboot) to run
the new image. If the write fails after the erase, the message says the partition may hold
a partial image: re-flash before rebooting.

The entry's `linuxMtd` block records each step (`steps[]` with argv and rc), the partition
size, and `digest` (`local`, `readBack`, `match`).
