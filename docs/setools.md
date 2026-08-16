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

The `swd_probe` backend (the GD32G553 supervisor bridge, not this doc's Flow
D) gets the identical read-only preflight, but `flash_args.expect_dpidr`
arms it **alone** there — `swd_probe`'s own `flash_args.jlink_device` already
names the write's own `-device` profile, so it is not a second, preflight-only
field the way Flow D's is. Set `flash_args.expect_dpidr` on a `swd_probe`
entry whenever the same cloned-serial risk applies (tan-cli#520).

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
`swd_probe`'s J-Link arm AND Flow D (`alif_mram_jlink`) — rather than
`swd_probe` alone. The coverage is a table (`DPIDR_GUARD_COVERAGE`) pinned to
the backend registry by a gate, so a new backend has to declare which side it
is on instead of inheriting silence.

It reached only `swd_probe` before, and that was measured, not theoretical: a
real AEN MRAM write through `tan flash` on 2026-08-10 emitted `ISSUES = []` —
no wrong-board guard and no signal that there was none — on a bench where one
J-Link serial is cloned across two probes.

What each path emits:

- **Flow D (`alif_mram_jlink`)** — raises the warning. The remedy names BOTH
  keys, because Flow D pairs `expect_dpidr` with `flash_args.jlink_device`
  (the live-core attach profile, *not* `jlink_flash_device`).
- **`swd_probe`, J-Link arm** — raises the warning; `expect_dpidr` alone is
  the remedy there.
- **`swd_probe`, openocd/pyocd arm** — raises **nothing at all**, by design:
  the SW-DP ID read is a JLinkExe-only primitive, so there is no guard to be
  unarmed and "set `expect_dpidr`" would be advice that cannot be taken
  (`plan_swd_probe` refuses that key on this arm at plan time). This is the arm
  the shipped `E1M-V2N101`/`V2N102`/`V2M101`/`V2M102` `flash_args` select on a
  host with no J-Link, i.e. the default path a bricked-bridge recovery takes
  today. The switch below still refuses it.
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

Its scope is the same table as the advisory (tan-cli#609): `swd_probe` **and**
Flow D. It was `swd_probe`-only when tan-cli#589 shipped it, which left the AEN
MRAM path — the genuine *customer* flash path of the two, the GD32 bridge being
factory-programmed by Alp Lab — outside both halves of the guard. On Flow D the
refusal fires ahead of the SETOOLS auto-sign, not merely ahead of the write:
`app-gen-toc` rewrites `build/app-package-map.txt` rather than appending, and
tan-cli#512 measured a wrong-board abort that correctly left slot0
byte-identical and still left the SETOOLS install mutated.

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

A `swd_probe` entry taking the **openocd/pyocd** arm refuses under this switch
unconditionally — the SW-DP ID read is a JLinkExe-only primitive, so that arm
cannot be armed at all. `openocd_usb_location` is not a substitute: a USB path
selects a *probe*, it never confirms which *board* is on the other end of the
SWD cable (tan-cli#589).

## Related

- `docs/adr/` — architecture decisions this backend follows (no new hardware
  fact invented in `tan`; every identifier above comes from `flash_args`,
  which alp-sdk's `metadata/**` populates).
- tan-cli#353, #365, #366, #367, #368, #369, #373 — the issues this doc and
  the surrounding fixes answer.
- tan-cli#520, #589, #609 — the wrong-board SW-DP ID guard: the preflight
  itself, the opt-in strict switch, and making both method-independent.
