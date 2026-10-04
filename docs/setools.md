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

## `--atoc-unqueryable` — a Flow D write replaces the *whole* ATOC

A Flow D write does not add an entry to the ATOC. It `loadbin`s a new ATOC over
the old one, **replacing the entire table** — and, unlike Flow A over the
SE-UART, there is no channel to ask the part what is resident first. So every
boot entry already in MRAM that your new ATOC does not name (an A32 boot chain,
an HP-core app, a diagnostic image) is **silently delisted** by the write. The
Secure Enclave then reports `[SES] ATOC ok`, because from its point of view the
table it was handed is perfectly valid — nothing in the transcript says
anything was lost.

Because `tan` cannot enumerate what it is about to replace, it asks *you* to
say you accept it. A confirmed Flow D write refuses
(`flash.atoc-replacement-unacknowledged`, exit 1) unless one of **exactly two**
spellings acknowledges the replacement:

- **`--atoc-unqueryable`** on `tan flash` (and on `tan run --flash`);
- **`flash_args.atoc_unqueryable: true`** in `build/system-manifest.yaml` — for
  `tan flash`, which reads that file as it stands. It is **not** an option for
  `tan run --flash`: every such run regenerates `build/system-manifest.yaml`
  from the planner before flashing, and the planner composes `flash_args` from
  the keys it knows, so a hand-added acknowledgement is overwritten by the same
  command that then asks for it. On `tan run`, pass the flag.

This is the same guard alp-sdk#2025 put on the bench scripts themselves —
`scripts/bench/aen/flash-jlink.sh`, `flash-jlink-hp.sh` and
`flash-jlink-mramxip.sh` refuse with exit 8 without the identical flag.

Three properties worth knowing:

- **It is not `--confirm`, and never an alias for it.** `--confirm` means "yes,
  write"; this means "yes, I accept that the entire ATOC is replaced". A
  confirmed run still refuses without it — including an `ALP_FLASH_FORCE=1`
  bench, deliberately.
- **There is no environment variable, on purpose.** Unlike `ALP_FLASH_FORCE`
  and `ALP_FLASH_REQUIRE_DPIDR`, which are properties of the *host*, this is a
  statement about *this* write's ATOC. An env var would be exported once into a
  shell profile or a CI job and then acknowledge every future write, including
  the unattended ones — which is exactly what alp-sdk#2025's own header warns
  against when it says the Flow D flag must never be merged or aliased with
  Flow A's `--replace-atoc`.
- **Previews still preview.** The refusal fires only where the write would
  really proceed (confirm gate armed *and* not `--dry-run`). `tan flash
  --dry-run` and an unconfirmed run both still report what they would do — and
  their message now states the whole-ATOC replacement, so you read it *before*
  arming the write rather than after.

A present-but-null or non-boolean `flash_args.atoc_unqueryable` is refused at
plan time — under `--dry-run`, and before any SETOOLS spawn — rather than read
as an absent key, so a mistyped acknowledgement is never quietly the same as no
acknowledgement.

On Flow D the refusal fires ahead of the SETOOLS auto-sign (same placement, and
same reason, as the wrong-board refusal above: `app-gen-toc` writes into your
SETOOLS install, and a refusal that fires after it has already run is a refusal
that did not prevent the mutation), and after the `ALP_FLASH_REQUIRE_DPIDR`
gate — writing the right table to the wrong board is the worse of the two
failures.

### Flow A: the `alif_flash` runner checks first — `--replace-atoc`

This is a **Flow D** guard, and only a Flow D guard. A slice that stays on
`zephyr_west_flash` — no `jlink_flash_device` on its `flash_args`, so `west
flash` picks the board.cmake default `alif_flash` runner and burns the ATOC
over the SE-UART (Flow A) — replaces the whole ATOC too, but it is guarded
differently, because that runner *can* ask the part what is resident first.
Every published Ensemble variant in alp-sdk metadata carries
`debug.jlink_flash_device`, so a planner-emitted AEN manifest dispatches Flow
D; Flow A is what a hand-written or legacy manifest reaches.

Since alp-sdk#2262, the `alif_flash` runner reads the resident ATOC back over
the SE-UART (`maintenance -opt getbanner`, then `-opt gettoc`) before it
burns, and refuses when a resident entry outside this build's own
`ALP-HE`/`ALP-HP` section would be silently delisted, or when that read could
not be verified. It records its verdict in
`<build_dir>/alif_flash/atoc-guard.json` (schema
`alp-sdk.alif-flash-atoc-guard.v1`, alp-sdk `docs/aen-provisioning.md`
section 0.6). `tan flash` and `tan run --flash` read it after `west flash`:

- **`refused-foreign`** → `flash.atoc-guard-refused`, naming every resident
  entry the write would have delisted. Capture or restore them first, then
  re-run with **`--replace-atoc`** — or pass it only if losing them is
  intended. Two exceptions, where the message does *not* offer the flag:
  - the entry is the factory `MCUBOOT-` bootloader → the message points at
    alp-sdk's J-Link slot0 path (section 0.5, Option B): overriding deletes
    the bootloader and the module will not boot;
  - the entry is one **an earlier slice of the same run just wrote**
    (`ALP-HE` from `m55_he`, say) → the manifest cannot be Flow A-flashed one
    core after another, because each `alif_flash` write replaces the whole
    ATOC with only its own entry. The message points at alp-sdk's
    `scripts/bench/aen/flash-run-dualcore.sh <hp-build-dir> <he-build-dir>`,
    which burns one ATOC carrying both cores (HP-master shape; an HE-master
    ATOC is not scripted — see alp-sdk `docs/aen-bench-bringup.md`, "Flow A —
    Dual-core deferred-TOC boot").
- **`refused-unverified`** → `flash.atoc-guard-refused`, saying the read
  could not be verified. Check the SE-UART wiring and `SE_UART`, read the
  transcript the verdict names, confirm by hand what is resident, and only
  then reach for `--replace-atoc`. If the runner says the read succeeded but
  the table format was not recognised, file the transcript instead.
- **No verdict, or one tan cannot read** (missing, malformed, an unknown
  schema or status, or a status/`query_status`/`foreign` combination the
  runner does not produce) after a failure → the ordinary
  `flash.entry-failed`, with a note that the verdict could not be read. tan
  never reports a refusal the runner did not state.
- **No usable verdict after a *successful* write** →
  `flash.atoc-guard-unavailable`: the guard did not run for that write
  (most likely west loaded a different runner than the one tan checked), so
  the write is reported as unguarded, never silently as checked.

tan removes any verdict an earlier run left behind *before* it spawns `west
flash`: the runner clears it too, but only once west has loaded it, and a
`west flash` that fails before that would otherwise leave an old verdict
looking like this attempt's. A successful write says what the guard did
(`ATOC guard: clear`, or what `--replace-atoc` overrode); after a *failed*
write an override is reported as an override whose outcome is unknown, not
as a done deletion.

**`--replace-atoc` is not `--atoc-unqueryable`, and neither is ever accepted
in place of the other.** The Flow D flag acknowledges a write nothing can
check; this one overrides a check that ran. An operator on a no-SE-UART bench
passes the Flow D flag on every run, so a flag that answered both would
silence the one guard that can look first (alp-sdk#2025's header says the
same).

**One `--replace-atoc` reaches at most one write.** If it would be appended
to more than one entry of the run (two Flow A cores, say), the whole run is
refused before anything is written — previews included — with
`flash.replace-atoc-ambiguous`: the override given to accept losing what is
on the board now would otherwise also override the second core's guard into
delisting what the first core had just written. Narrow the run with `--core
CORE_ID` (or `--helper NAME`) and pass the flag there.

Where it applies, and what tan says when it does not:

- tan passes `--replace-atoc` to `west flash` only for a `zephyr_west_flash`
  slice whose runner is `alif_flash` — `flash_args.runner`, else the
  `flash-runner:` in the build's `zephyr/runners.yaml` — **and** whose
  runner has the guard. The runner checked is the one `west flash` will
  load: `scripts/west_commands/runners/alif_flash.py` under the alp-sdk
  module the build was configured with, as listed in the build's
  `zephyr_modules.txt`. Only when the build has no readable module list does
  tan fall back to the SDK it is bound to; every message names the file it
  read and why. `tan flash --dry-run` shows the flag in the planned argv.
- A relative `flash_args.build_dir` is anchored on the directory `west
  flash` runs in (the west workspace topdir when tan finds one), and the
  absolute path is what goes on the argv — so the argv, `runners.yaml`, the
  stale-verdict removal and the verdict read all name the tree west writes.
- A sysbuild `--build-dir` (one holding `domains.yaml`) with **one** domain
  is followed into that domain's build dir, where the runner runs and writes
  its verdict. With **more than one** domain the runner refuses the whole
  sysbuild before its guard runs (alp-sdk#2274) and writes no verdict; tan
  does not guess a domain, and says so if `--replace-atoc` was passed.
- Passed anywhere else — a Flow D slice, another backend, another runner, a
  multi-domain sysbuild, or a slice whose runner tan cannot learn (not built
  yet, no `flash_args.runner`) — it is not passed, and tan warns
  `flash.replace-atoc-not-applicable` rather than ignoring it.
- An `alif_flash` slice whose runner predates the guard (or whose build lists
  no alp-sdk module) warns `flash.atoc-guard-unavailable` on every run, flag
  or not: that write replaces the whole ATOC with nothing checking first. tan
  never passes the flag there — west would reject an argument the runner does
  not register.
- `tan run` without a flash (no `--flash`, a failed build, a host target)
  warns `run.flash-flags-ignored` when given `--replace-atoc` or
  `--atoc-unqueryable`: both only act on a write.

There is **no manifest spelling and no environment variable**, unlike Flow
D's `flash_args.atoc_unqueryable`. That key exists because Flow D can never
query, so a manifest can record the acknowledgement once. Flow A's guard does
query, and a persisted override would switch a working check off for every
later run, including the one where the board carries something new. (`tan
run --flash` regenerates the manifest before flashing anyway.)

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
- tan-cli#1252 — `--atoc-unqueryable`: a Flow D write replaces the whole ATOC
  and cannot enumerate what is resident first, so the replacement must be
  acknowledged. Ports alp-sdk#2025 (PR alp-sdk#2029), which put the same
  refusal on the AEN bench scripts.
- tan-cli#1267 — `--replace-atoc`: Flow A's half, reading the `alif_flash`
  runner's own pre-burn verdict (alp-sdk#2262, PR alp-sdk#2275).
