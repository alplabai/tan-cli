<!-- SPDX-License-Identifier: Apache-2.0 -->
# SETOOLS — signing an Alif Ensemble slot0 ATOC for Flow D

`tan flash`'s `alif_mram_jlink` backend ("Flow D": J-Link straight over SWD,
no SE-UART) burns a **signed ATOC** into an Alif Ensemble part's on-die MRAM.
Producing that signature is Alif's own job, done by the Alif Security Toolkit
(SETOOLS) `app-gen-toc` step — `tan` does not sign anything itself; it drives
`app-gen-toc` for you when it can find it, and refuses loudly, naming exactly
what it tried, when it cannot (tan-cli#365).

## SETOOLS is not part of tan, and never will be

SETOOLS is **license-gated and obtained directly from Alif**. Neither `tan`
nor alp-sdk redistributes it. Get it from the Alif developer portal under
your own Alif account, then point `tan` at the directory you installed it
into — the sections below cover how.

Two shapes matter, depending on host OS:

- **Linux bundle**: `app-release-exec-linux-SE_FW_x.y.z` — the one
  executable `tan` looks for inside it is `app-gen-toc` (`west flash`'s own
  `alif_flash` runner, for the SE-UART path, looks for `app-write-mram`
  separately; Flow D here never does). Running `app-gen-toc` writes
  `app-package-map.txt`, its own build **report** — not another executable,
  and not something `tan` searches for the way it searches for the tool.
- **Windows**: a genuine Windows SETOOLS install ships `app-gen-toc.exe`
  instead of the bare Linux name; `tan` looks for both.

## Pointing `tan flash` at your install: three sources, one precedence order

`tan flash` accepts three ways to say where SETOOLS lives. **Highest
precedence wins outright** — a lower source is never consulted once a higher
one resolves:

1. **`--setools-dir <path>`** — a flag on `tan flash` itself. The one durable,
   discoverable-from-`--help` way to pin this per invocation, regardless of
   shell session or manifest state.
2. **`SETOOLS_DIR=<path>`** — an environment variable. Survives across
   `tan build` runs (unlike the manifest field below), but is scoped to
   whatever shell/session set it.
3. **`flash_args.setools_dir`** in `build/system-manifest.yaml` — lowest
   precedence, and **not durable**: `tan build` regenerates this file on
   every run (`python/tan/commands/build/manifest.py`), and alp-sdk's own
   emit carries no `setools_dir` key at all. A hand-edit here is silently
   overwritten by your next build. Prefer the flag or the environment
   variable for anything you want to survive a rebuild; treat this field as
   build-owned, not a place to hand-author a durable setting.

If none of the three resolves, `tan flash` refuses with a message naming all
three sources, in this same order, and how to set each one — it never
searches the filesystem for a plausible SETOOLS install: a *wrong* SETOOLS
silently signing against the wrong part is worse than `tan` refusing outright.

## What `tan` actually does with it

When a Flow D entry has no `atoc`/`atoc_address` yet (an AEN801 slot0 slice's
manifest today typically carries only `jlink_flash_device` and
`slot0_load_address` — alp-sdk's emit does not sign anything itself),
`tan flash` drives one `app-gen-toc` sign step for you:

1. copies the build's raw `.bin` into `<SETOOLS_DIR>/build/images/`;
2. writes an app-only ATOC config to `<SETOOLS_DIR>/build/config/` — no
   `"DEVICE"` key: the on-module factory device config is already correct for
   your part, and this step must not overwrite it;
3. runs `app-gen-toc`, inside `SETOOLS_DIR`, against that config;
4. reads the resulting ATOC's MRAM placement back out of
   `<SETOOLS_DIR>/build/app-package-map.txt`. This file is **APPEND-mode** —
   the accumulated sign record for the whole install, including hand-runs
   you did outside `tan` — so `tan` never truncates or deletes it
   (tan-cli#373): it records the file's size and mtime beforehand and
   refuses if either is unchanged after a zero exit (a soft failure that
   would otherwise read back a stale, unrelated address as if it were
   fresh), and separately confirms `<SETOOLS_DIR>/build/AppTocPackage.bin`
   (which — unlike the map — IS overwritten whole every run, so there is no
   history in it to protect) was actually rewritten before trusting either.

A successful sign names which SETOOLS install did it (`--setools-dir`,
`SETOOLS_DIR`, or `flash_args.setools_dir` — see `setools.source` in `tan
flash`'s own output), not only a failed one.

Under `--dry-run` none of this touches your SETOOLS install or spawns
`app-gen-toc` at all — `tan flash --dry-run` prints what it *would* sign and
stops there.

If you already resolved a signature yourself — an explicit `flash_args.atoc`
+ `flash_args.atoc_address`, or `flash_args.atoc_map` pointing at your own
`app-package-map.txt` — none of the above runs; `tan` uses what you gave it
verbatim.

## Two probes, one cloned serial: why `jlink_serial` is not always enough

On a bench carrying more than one J-Link, `flash_args.jlink_serial` picks a
probe by serial only — `JLinkExe` has no USB-port selector. Some OEM J-Link
probes ship with a **cloned serial number shared across more than one
physical unit**, in which case `jlink_serial` alone cannot tell two probes
apart, even when set: a wrong-board write is now possible even with a serial
pinned. `flash_args.expect_dpidr` (paired with `flash_args.jlink_device`) is
the real per-silicon discriminator for this case — `tan` reads it back on
connect, before ever writing MRAM, and refuses when it doesn't match. Set
both when your bench has more than one probe, or when a shared/cloned serial
is a possibility; do not rely on `jlink_serial` alone to disambiguate.

`flash_args.expect_dpidr` must be a **full 32-bit SW-DP ID — 8 hex digits**
(an optional `0x`/`0X` prefix doesn't count towards the 8). `tan` refuses a
shorter value outright, at plan time (so it surfaces under `--dry-run`, not
only on a real write): a truncated ID like `0x2477`, or `0x477` — ARM's own
JEP106 designer field, shared by every ARM SW-DP — would otherwise match more
than one board and silently disarm the wrong-board guard (tan-cli#795).

### The unarmed-guard advisory, and which methods it covers

`flash_args.expect_dpidr` is **optional**, so a write with none set proceeds
unguarded. Since tan-cli#609 the `flash.dpidr-preflight-unarmed` warning covers
**every method `tan` itself composes a J-Link Commander session for** — today
Flow D (`alif_mram_jlink`) alone. The coverage is a table
(`DPIDR_GUARD_COVERAGE`) pinned to the backend registry by a gate, so a new
backend has to declare which side it is on instead of inheriting silence.

It reached only the (now-removed) `swd_probe` backend before #609, and that
was measured, not theoretical: a real AEN MRAM write through `tan flash` on
2026-08-10 emitted `ISSUES = []` — no wrong-board guard and no signal that
there was none — on a bench where one J-Link serial is cloned across two
probes.

What each path emits:

- **Flow D (`alif_mram_jlink`)** — raises the warning. The remedy names BOTH
  keys, because Flow D pairs `expect_dpidr` with `flash_args.jlink_device`
  (the live-core attach profile, *not* `jlink_flash_device`).
- **Every other method** (`zephyr_west_flash`, `baremetal_cmake_flash`,
  `yocto_wic*`, `xspi_flashwriter`) — raises nothing, because `tan` composes no
  probe session there for `expect_dpidr` to arm. That is not a safety claim
  about those methods; `west flash`'s own runner, for one, may well drive a
  J-Link, and `tan` has no view into how it selects a probe.

### `ALP_FLASH_REQUIRE_DPIDR=1` — making an unarmed write refuse

An unattended bench reads no warnings, and the openocd/pyocd arm emits none to
read, so `tan flash` also honours an env switch: with
**`ALP_FLASH_REQUIRE_DPIDR=1`** exported, a real write whose DPIDR preflight
would not run **fails the entry before anything is spawned**
(`flash.entry-failed`) instead of proceeding. Unset — the default — nothing
changes.

Its scope is the same table as the advisory (tan-cli#609): Flow D today. It was
`swd_probe`-only when tan-cli#589 shipped it (that backend was removed by
tan-cli#732), which left the AEN MRAM path — the genuine *customer* flash path
of the two, the GD32 bridge being factory-programmed by Alp Lab — outside both
halves of the guard. On Flow D the refusal fires ahead of the SETOOLS
auto-sign, not merely ahead of the write:
`app-gen-toc` appends a block to `build/app-package-map.txt` and rewrites
`build/AppTocPackage.bin` whole, and tan-cli#512 measured a wrong-board abort
that correctly left slot0 byte-identical and still left the SETOOLS install
mutated.

The policy belongs to the host, not to the manifest. Export it on a factory or
bench machine, where a wrong-board write is expensive and nobody is watching;
leave it unset on a customer machine, where a bricked-bridge recovery must not
be blocked by a metadata field alp-sdk has not populated yet. It is read as the
exact string `1`, the same as `ALP_FLASH_FORCE`.

Two things it does **not** do: it does not apply to `--dry-run` (a preview
writes nothing), and it does not make `expect_dpidr` mandatory in metadata. No
shipped alp-sdk preset carries a SW-DP ID today, and `tan` is forbidden from
deriving one — until metadata populates the field, exporting this variable
refuses these writes rather than guarding them.

## GD32 bridge programming: not this backend, not `tan` any more

`tan flash` no longer has a local-write path for the E1M-X V2N/V2M SoMs' GD32
bridge supervisor MCU (the `swd_probe` backend, removed by tan-cli#732 — GD32
programming is separating out of `tan` entirely). The GD32's **field-update**
path is untouched and stays: `helper_firmware[].update_channel:
alp_ota_spi_bridge` (protocol v0.6 Path A, slot-A/B application bootloader
with commit and rollback, over the bridge link rather than SWD), which alp-sdk
still emits and `tan` still projects into `build/system-manifest.yaml`. A
project that previously relied on `tan flash --helper gd32_bridge` for a local
SWD write (recovering a bricked bridge, say) has no in-tree `tan` replacement
as of this change; that gap is tracked separately, not silently dropped — see
tan-cli#610 (`needs-silicon`, the still-open contradiction over the GD32
bridge's own SW-DP ID), whose premise — settling `expect_dpidr` for a `tan
flash` write to the GD32 — no longer applies now that `tan` has no such write
to arm, but stays open rather than closed over: the underlying SW-DP ID
contradiction is a real, unresolved bench fact that whatever tool ends up
programming the GD32 will still need.

## Related

- `docs/adr/` — architecture decisions this backend follows (no new hardware
  fact invented in `tan`; every identifier above comes from `flash_args`,
  which alp-sdk's `metadata/**` populates).
- tan-cli#353, #365, #366, #367, #368, #369, #373 — the issues this doc and
  the surrounding fixes answer.
- tan-cli#520, #589, #609 — the wrong-board SW-DP ID guard: the preflight
  itself, the opt-in strict switch, and making both method-independent.
- tan-cli#732 — removed the `swd_probe` flash backend (GD32 programming
  separating out of `tan`); #610 above is the open follow-up it leaves.
