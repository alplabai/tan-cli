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
   precedence, **never executed** (tan-cli#1343/#1344), and **not durable**: the
   manifest is project-controlled, and tan will not run an `app-gen-toc` a
   checkout picked for a write to a board. A manifest-only source is still
   *found* (so the messages can name it), but a confirmed `tan flash` refuses it
   as `flash.setools-untrusted-source` before any spawn, and `--dry-run` / an
   unconfirmed run skip the sign (`setools.signSkipped: true`, issue
   `flash.preview-sign-skipped`) instead of reporting the ATOC placement. Name
   the install yourself with the flag or the environment variable. `tan build`
   also regenerates this file on every run
   (`python/tan/commands/build/manifest.py`) and alp-sdk's own emit carries no
   `setools_dir` key, so a hand-edit here would be overwritten anyway.

If none of the three resolves, `tan flash` refuses with a message naming all
three sources, in this same order, and how to set each one — it never
searches the filesystem for a plausible SETOOLS install: a *wrong* SETOOLS
silently signing against the wrong part is worse than `tan` refusing outright.

## What `tan` actually does with it

When a Flow D entry has no `atoc`/`atoc_address` yet (an AEN801 slot0 slice's
manifest today typically carries only `jlink_flash_device` and
`slot0_load_address` — alp-sdk's emit does not sign anything itself),
`tan flash` drives one `app-gen-toc` sign step for you:

1. makes a **private scratch overlay** of your SETOOLS install in the system
   temp directory (tan-cli#1325) — small directories copied, the large `alif/`
   firmware directory and the top-level tools symlinked, a fresh `build/` —
   and never writes into the install itself;
2. copies the build's raw `.bin` into the scratch `build/images/` and writes
   the ATOC config to the scratch `build/config/` (the `DEVICE` entry: see
   below);
3. runs the scratch copy of `app-gen-toc` inside the scratch tree against that
   config;
4. reads the resulting ATOC's MRAM placement, size and entry list back out of
   the scratch `build/app-package-map.txt`, and hands J-Link the scratch
   `build/AppTocPackage.bin`.

Your SETOOLS install is **byte-identical** afterwards: `build/AppTocPackage.bin`,
`build/app-package-map.txt` (which is APPEND-mode, the accumulated sign record
including your hand-runs), `build/images/`, `build/config/` and the SETOOLS logs
are all left exactly as they were, and no lock file or copy-out directory is
created in it. Because nothing shared is written, two `tan flash` runs against
one install can no longer cross-pair (tan-cli#380) without any lock. The scratch
tree is removed when the entry finishes; the entry reports it as
`setools: {dir, source, scratch, scratchRemoved}`.

A successful sign names which SETOOLS install did it (`--setools-dir` or
`SETOOLS_DIR`; see `setools.source` in `tan flash`'s own output), not only a
failed one. A manifest-only `flash_args.setools_dir` never signs (see source 3
above).

Because the sign is side-effect-free, `--dry-run` (and an unconfirmed run) run
`app-gen-toc` too, in the scratch tree, so the preview reports the real ATOC
placement, **when the install came from `--setools-dir` or `SETOOLS_DIR`**. With a
manifest-only source the preview skips the sign and says so. They still never
spawn `JLinkExe`.

### The `DEVICE` entry (tan-cli#1322)

The device configuration is an entry *inside* the ATOC package, and a Flow D
write replaces the whole table, so an ATOC signed without it **deletes** the
resident one rather than preserving it (measured on an evk-02: the resident
package `0x15C40` carried `DEVICE` `0x138` + the app; the app-only replacement
was `0xA50`). `tan flash` therefore signs a `DEVICE` entry by default, in the
exact shape alp-sdk's bench recipe uses (`binary`, `version "0.5.00"`,
`signed: true`), ahead of the app entry. Its source is, in order:

1. `flash_args.setools_device_config` — a path (relative paths resolve against
   the build root like every other manifest path); a path that does not exist is
   refused, never swapped for the stock file;
2. SETOOLS' own `<SETOOLS_DIR>/build/config/app-device-config.json`.

With neither, the run refuses with `flash.device-config-missing` (also under
`--dry-run`, before `app-gen-toc` is spawned). `--no-device-config` opts out and
signs an app-only ATOC; the envelope then says
`setools.deviceConfig.included: false` and the replacement note states that the
resident `DEVICE` entry is deleted. The stock file carries firewall regions
opened to `any_master`, HFXO trims and `SE_BOOT_INFO`; a CPU-only Zephyr app
boots without it (proven), bus masters writing to SRAM0 are not.

The whole-ATOC acknowledgement (`--atoc-unqueryable`) names the entries the new
ATOC carries (`This ATOC names: DEVICE, m55_he.`). Flow D cannot enumerate what
is resident, so the resident entries that will not be rewritten are listed only
when you supply them as `flash_args.resident_atoc_entries: [DEVICE, ALP-HE, ...]`
(read them off the SE-UART first with `maintenance -opt gettoc`); otherwise the
text says the resident table is unknown.

If you already resolved a signature yourself — an explicit `flash_args.atoc`
+ `flash_args.atoc_address`, or `flash_args.atoc_map` pointing at your own
`app-package-map.txt` — none of the above runs; `tan` uses what you gave it
verbatim.

## Reviewing a Flow D write before arming it (tan-cli#1318)

`tan flash --dry-run --format json` puts a `plan` block on every Flow D entry:
`jlinkScript` (the exact Commander script, `exec DisableAutoUpdateFW` first),
`argv`, `writes[]` as `{name, address, size, path, sectorSpan}`, and `atoc`
`{address, size, entries, signedByTan}`. `sectorSpan` counts 16 KiB sectors
(`first`, `end` exclusive, `count`, `bytes`) because the loader rewrites whole
sectors and fills the remainder with 0xFF -- an ATOC of 2640 B at `0x8057F5B0`
still rewrites the sector `0x8057C000`-`0x80580000`. For an ATOC tan signs, the
placement and entry list come from `app-gen-toc` run in the scratch overlay, so
they are what a real run will write; a dry run still never spawns the J-Link tool.

### Interrupted runs and Windows

The scratch tree is removed when the entry ends, including on an interrupt (it is
registered before `app-gen-toc` starts). `SIGKILL` cannot run cleanup, so a killed
`tan flash` leaves a `tan-setools-*` directory in the system temp directory; delete
it by hand. On POSIX the signing keys inside it are a symlink into your install; on
Windows without symlink privilege tan falls back to COPYING them, so there the
leftover holds a copy of the keys -- remove it.

## What a Flow D write reports, and what it proves (tan-cli#1321)

`verifybin` compares the image against J-Link's flash **cache**, not the chip, so
tan says `cache-verified`, never a bare "verified". The entry's `jlink` block
carries the evidence: `dpidr` (the SW-DP ID the write transcript read, else the
read-only preflight's, with `dpidrSource`), `transcriptPath` (a file under
`<build>/flash-logs/` with the Commander script and both streams) and
`transcriptTail`, `verification`, and `reset` / `resetFailures`. A transcript
containing `Failed to halt CPU`, `CPU is not halted`, `Reset: Failed` or `CPU may
have not been reset` first triggers a read-only, non-halting DHCSR check in a fresh
session: only `S_RESET_ST` set with `S_HALT` and `S_LOCKUP` clear (and no `Reset:`
or halt trouble in that session) proves the reset took (`jlink.resetConfirmedBy:
"dhcsr"`, no issue code). Because J-Link's own reads usually clear it, a second non-halting
witness also confirms: three `DWT_PCSR` (0xE000101C) PC samples that all fall inside the
image just flashed (`resetConfirmedBy: "pcsr"`, `jlink.reset: "new-image-running"` -- PCSR proves the new code
runs, not that the Secure Enclave pin reset took; the ranges are the PF_X LOAD segments of the
app ELF; `0xFFFFFFFF` and `0x00000000` are no sample, any sample outside vetoes). `S_RETIRE_ST` / `S_SLEEP` alone clear on read and are also
the old image idling, so they only set `jlink.coreRunning` ("core running, reset not
proven"). Otherwise the message becomes `PIN-reset NOT confirmed` and `flash.jlink-reset-unconfirmed` (info) appear.

`--readback` (Flow D) reads every written region back **inside the write session**: connect,
`loadbin`, `verifybin`, `h`, `savebin` per region, then the reset/run tail and a single `exit`
(tan-cli#1458: J-Link's `exit` resumes the core even after `h`, so a separate read-back session let
the app run on stale state first, and the chip is read before the new image can boot and put the
debug domain to sleep). Same probe-selection guard as the write; sha256 compared with the source
files. A match is reported as `jlink.verification: "insession-readback"` (`jlink.readbackMode:
"in-session"`), **not** `readback-verified`: J-Link may serve an in-session read from its flash
cache. Afterwards a short **fresh** `savebin` session runs; a match upgrades the value to
`readback-verified` (`jlink.freshReadback.state: "verified"`). It never fails the entry: if the
target cannot be read after the reset (low power) the value stays `insession-readback` with the
info issue `flash.readback-fresh-unconfirmed`; a full-length difference there is the same code as a
warning (garbage from a gated debug domain, or a real fault: power-cycle and read again). A
full-length difference in the in-session read is `flash.readback-mismatch`; a read that cannot read
the chip is `flash.readback-failed` ("target unreachable (low-power?)"), never a mismatch that
advises a re-flash. A session that dies in the halt/read steps is reported as the entry failure with
what J-Link echoed: whether the `verifybin`s passed (did the write land?) and whether the reset tail
started ("the board was NOT reset: reset or power-cycle it"). `--raw --readback` and the no-tail
fallback use a fresh session without a reset. With `--no-reset` the wording says "after a halt; no
reset command was sent", never "before the reset".

`--no-reset` (tan-cli#1445) sends **no** reset/run commands: the session ends after the
write, `verifybin` and (with `--readback`) the in-session read-back, at `exit`, so tan does
not start the new image and you can attach a console first. The honest limit: J-Link's `exit`
has been seen to resume the core on its own (bench, tan-cli#1458) and the board is not held in
reset, so this means "no reset command is sent", not "the core is stopped". The envelope says
`jlink.reset: "not-sent"` with a `resetNote`, and the message says so. No boot probe runs (there
is no reset to confirm). Reset or power-cycle when ready. Holding nRESET across `exit` (J-Link
`r0`) was considered and is not implemented: its behaviour could not be verified, an nRESET pulse
does not wake an Alif E8 that is in a correctly configured STOP, and a board left in reset until
`tan reset` or a power cycle is a worse default. Not valid with `--ram` or `--raw`.

### `--raw <file>@<addr>`: byte-exact sector restore

`tan flash --core <id> --raw he_slot0.bin@0x80010000 --raw atoc.bin@0x8057C000
--confirm [--readback]` puts `savebin` backups back exactly. One slice's J-Link
part profile and the Flow D probe guard, `loadbin` + `verifybin` per blob, **no
reset**, no signing. Every address is an explicit `0x` literal, 16 KiB
sector-aligned; every blob a whole number of sectors; all inside the SKU's MRAM
and non-overlapping, or `flash.raw-invalid` (previews included). tan derives no
address, so an ATOC/STOC is only written where you name it. A real write needs the
CLI `--confirm` (never a manifest's `flash_args.confirm`) and a provably held bench
reservation (`flash.raw-reservation-required` otherwise, failing closed):
`TAN_LEASE_NONCE` set to the nonce in your own `~/.cache/alplab-leases/<place>.lease`
(tan-cli#1457: labgrid's holder is `<host>/<user>` for every session of a user, so the
holder alone cannot tell sessions apart; acquire with
`eval "$(scripts/bench/tan-lease.sh acquire <place>)"`, release with
`scripts/bench/tan-lease.sh release <place>`);
the lease also records the place's labgrid `changed:` timestamp (updated on every acquire/release) and
the gate refuses a lease whose value no longer matches, so a lease left over from an earlier
acquisition is stale; `--raw` is unsupported where POSIX `pwd`/`grp`/uid are missing (Windows);
`JLINK_RUN_PLACE` set; the J-Link program tan runs resolves to the wrapper named by
an absolute `TAN_JLINK_WRAPPER` (outside the cwd, not world-writable; a marker string
inside some `JLinkExe` on `PATH` is not consulted); and `labgrid-client` (absolute,
`TAN_LABGRID_CLIENT` or a fixed directory list, never a `PATH` search) reports this
host/user (real-uid account) as the single `acquired:` holder, with the leased place's `swd` USB path equal to the selected probe's (`--probe-usb-path`). The wrapper and `labgrid-client` must be owned by root or you and not writable by others or by a shared group, along the symlink's own chain and the resolved target's. This guards against accidental writes; it is
not a security boundary. The MRAM window is the SKU's own variant `mram_mb` at the SoC
document's `soc_flash_base`; a SKU that does not resolve is refused. The envelope's
`raw.writes[]` carries each blob's `sha256`, address and sector span;
`raw.resetCommands` is `false` (no reset command is sent, but `loadbin` may halt the
core). Power-cycle afterwards so the Secure Enclave boots the restored contents.

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
of the two — outside both
halves of the guard. On Flow D the refusal fires ahead of the SETOOLS
auto-sign, not merely ahead of the write (tan-cli#512 measured a wrong-board
abort that correctly left slot0 byte-identical but had already mutated the
SETOOLS install; since tan-cli#1325 the sign no longer touches the install at
all, and the ordering is kept as defence in depth).

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
- On either refusal, west's runner-loading warnings (`The module for runner
  "rtsflash" could not be imported`, `WARNING: runners.alif_flash: ...`) are
  left out of the refusal's `West reported:` tail and reported verbatim as the
  warning `flash.runner-setup-warnings`. They describe this host's Python
  environment, not the write.
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

## Removed: the `swd_probe` flash backend

`tan flash` no longer has a local SWD-write path for on-module helper MCUs (the
`swd_probe` backend, removed by tan-cli#732). The helper **field-update** path
is untouched: `helper_firmware[].update_channel: alp_ota_spi_bridge`, which
alp-sdk still emits and `tan` still projects into `build/system-manifest.yaml`.
`tan` has no in-tree replacement for a local SWD write of a helper MCU; a
manifest that still declares `flash_method: swd_probe` is refused by name.

## Related

- `docs/adr/` — architecture decisions this backend follows (no new hardware
  fact invented in `tan`; every identifier above comes from `flash_args`,
  which alp-sdk's `metadata/**` populates).
- tan-cli#353, #365, #366, #367, #368, #369, #373 — the issues this doc and
  the surrounding fixes answer.
- tan-cli#520, #589, #609 — the wrong-board SW-DP ID guard: the preflight
  itself, the opt-in strict switch, and making both method-independent.
- tan-cli#732 — removed the `swd_probe` flash backend.
- tan-cli#1252 — `--atoc-unqueryable`: a Flow D write replaces the whole ATOC
  and cannot enumerate what is resident first, so the replacement must be
  acknowledged. Ports alp-sdk#2025 (PR alp-sdk#2029), which put the same
  refusal on the AEN bench scripts.
- tan-cli#1267 — `--replace-atoc`: Flow A's half, reading the `alif_flash`
  runner's own pre-burn verdict (alp-sdk#2262, PR alp-sdk#2275).

## `tan flash --ram`: is the probe on the HE core? (tan-cli#1354)

A generic `Cortex-M55` attach picks whichever M55 access port J-Link finds, and its
`Found Cortex-M55 r1p0` line is identical for the HE and the HP core. Before it loads
anything, `--ram` runs one read-only session (`connect`, then three `mem32` reads --
no halt, no write) and decides from two independent facts:

* **Primary -- the AP that reports `AP[n]: Core found`.** Its `APAddr` identifies the
  core: HE `0x00300000`, HP `0x00200000` (alp-sdk `scripts/bench/aen/openocd-ram-run.sh:16-17`,
  `changelog.d/2037-openocd-m55he-bench-core-selection.md:4`, `changelog.d/2025.md:31`).
  The AP, its address, the `CPUID register` and the `Found Cortex-M55` line are reported
  as `jlink.attachedCore` and `ram.coreCheck.ap`.
* **Corroboration -- the ITCM alias.** A core's local ITCM at `0x0` is its own global
  window (HE `0x58000000`: alp-sdk `metadata/socs/alif/ensemble/e8.json` `itcm_global_base`
  at line 106; `docs/aen-bench-bringup.md:20`), so the 4 words read at `0x0` must equal the
  4 words at the HE window. **The check reads only the local ITCM `0x0` and the HE window
  `0x58000000` -- never the HP window `0x50000000`:** bench round 8 (2026-10-07, evk-02)
  measured that reading it from the HE attach returns words without an error yet leaves the
  M55-HE unhaltable until a PIN reset.

Only an HE access port proceeds, and an HE access port whose local ITCM does not equal the
HE window is a conflict that refuses. An HP access port refuses with
`flash.ram-core-mismatch`; no placeable access port, or an unreadable check, refuses with
`flash.ram-core-unconfirmed`. `--assume-he` overrides only that last, evidence-missing case
(never HP evidence, never a conflict), at your own risk. After the load, the load session's
own Core-found AP is compared with the check's: a different AP, or HP, fails the entry with
`flash.ram-core-mismatch` and reports both (`jlink.attachedCore`, `jlink.attachedCoreAtLoad`).

Halt/reset trouble in the load transcript (`CPU could not be halted`, `Could not find
core`, `SYSRESETREQ has confused core`, `Reset: Failed`, `CPU may have not been reset`) is
reported as `jlink.resetFailures` plus the `flash.jlink-reset-unconfirmed` warning, and the
message says the load only worked through a J-Link fallback. The words read and the verdicts are in
`ram.coreCheck`. `ram.loaded` (`data.entries[].ram`) is `true` once the load session is
proven (the image is on the board and the device was reset), whatever fails afterwards; it is
absent when the entry failed before that point.

A stale alp-sdk checkout whose SoM presets are `schema_version: 1` now says so
(`unsupported SoM preset schema_version 1 (tan needs 2) -- update alp-sdk`) wherever
tan cannot read SoC metadata: the `--ram` aperture refusal and the `tan debug-config`
metadata notes (`tan size` keeps its own `size.som-schema-version-skipped`).

## `tan probe`: read-only J-Link identity and memory read (tan-cli#1406)

Two read-only questions that used to need raw `JLinkExe`. Neither verb halts, writes,
erases, resets or runs anything: every generated Commander script is `connect` plus
`mem32` reads and `exit` (a test scans for `w1`/`w2`/`w4`, `erase`, `loadbin`, `setpc`,
`go`, `reset` and `halt`). Probe selection, the trusted J-Link binary and the
`ShowEmuList` verification before each spawn are the ones `tan flash` uses, so a
probe-selection refusal reuses `flash.probe-ambiguous` / `-not-found` /
`-selector-conflict` / `-verify-failed` verbatim.

```sh
tan probe identify [--core m55_he|m55_hp] [--probe-usb-path 3-4.2] [--jlink PATH] [--build-root DIR]
tan probe read <addr> [<words>] [--core m55_he|m55_hp] [--probe-usb-path 3-4.2] [--jlink PATH]
```

* `identify` runs the DPIDR preflight script first and reports `identity.{dpidr,
  expectedDpidr, dpidrMatch, apAddr, cpuid, core, itcmVerdict, apVerdict, isolation}`. With a
  built project it compares the SW-DP ID with the selected slice's `expect_dpidr`; a
  difference (`probe.dpidr-mismatch`) or no ID (`probe.dpidr-unread`) exits 1 and **no further
  session runs**. Only when the target is the M55-HE (`--core m55_he`, or the manifest's
  selected slice is the HE) does it then run the `--ram` attach check (`mem32 0x0` and
  `mem32 0x58000000`, bench-proven only from an HE attach). For an HP or unknown target the
  verdict comes from the DPIDR banner's Core-found APAddr alone and `itcmVerdict` is
  `not-checked`. An attach that contradicts `--core` is `probe.core-mismatch` (exit 1).
  With no manifest, or no `expect_dpidr`, an info issue `probe.no-manifest` says the match was
  skipped; an existing but unparsable manifest is the `probe.manifest-unusable` warning.
* `read` returns `read.data` as hex words. `addr` is plain hex (`0x...`) or decimal, 4-byte
  aligned; `words` defaults to 4 and is at most 256 (`probe.read-too-large`). Anything else
  (including a range past 0xFFFFFFFF) is `probe.bad-argument`. It is one `mem32` session, and
  its own banner is checked: the SW-DP ID must match an armed `expect_dpidr`, and the
  Core-found AP (`read.attached`) must not contradict `--core`; otherwise the words are not
  returned.
* `read` refuses ANY overlap with `0x50000000`-`0x5FFFFFFF` on EVERY core
  (`probe.read-unsafe-region`), before a J-Link is spawned: from the HE that window is the HP ITCM
  alias (reading it leaves the core unhaltable until a PIN reset), and tan does not read it
  from any attach.
* `identify` skips the ITCM corroboration (`itcmVerdict: "not-checked"`) unless the target is
  the HE AND session 1's banner placed the attach on the HE access port; it then emits the
  info issue `probe.itcm-not-checked` naming the reason and the fix (`--core m55_he`). An AP
  that contradicts the claimed core (`--core`, else the manifest's selected slice) is
  `probe.core-mismatch`.
* The manifest's selected slice supplies `jlink_serial` / `jlink_speed` / `jlink_device`
  whether or not `expect_dpidr` is armed. Several slices that pin different serials need
  `--core`. A part-number `jlink_device` is replaced by `Cortex-M55` and the report says so
  (`jlink.device`, `jlink.deviceSubstitutedFrom`).
* The envelope's `scripts` hold the exact text sent for each probe session, including the
  `exec DisableAutoUpdateFW` first line; `guard.script` is the `ShowEmuList` verification
  that runs before each of them. The full transcript is written to
  `<build_root>/flash-logs/probe-<verb>-<ts>.log` (`$XDG_CACHE_HOME/tan/probe-logs`, else
  `~/.cache/tan/probe-logs`, when there is no build root; the oldest are pruned) and reported as `transcriptPath`. Temp scripts are `tan-probe-*.jlink`.
