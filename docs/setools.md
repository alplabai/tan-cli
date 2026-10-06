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

A successful sign names which SETOOLS install did it (`--setools-dir`,
`SETOOLS_DIR`, or `flash_args.setools_dir` — see `setools.source` in `tan
flash`'s own output), not only a failed one.

Because the sign is side-effect-free, `--dry-run` (and an unconfirmed run) run
`app-gen-toc` too, in the scratch tree, so the preview reports the real ATOC
placement. They still never spawn `JLinkExe`.

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

## What a Flow D write reports, and what it proves (tan-cli#1321)

`verifybin` compares the image against J-Link's flash **cache**, not the chip, so
tan says `cache-verified`, never a bare "verified". The entry's `jlink` block
carries the evidence: `dpidr` (the SW-DP ID the write transcript read, else the
read-only preflight's, with `dpidrSource`), `transcriptPath` (a file under
`<build>/flash-logs/` with the Commander script and both streams) and
`transcriptTail`, `verification`, and `reset` / `resetFailures`. A transcript
containing `Failed to halt CPU`, `CPU is not halted`, `Reset: Failed` or `CPU may
have not been reset` downgrades the message to `PIN-reset NOT confirmed` and
raises `flash.jlink-reset-unconfirmed` (warning).

`--readback` re-reads every written region in a **fresh** J-Link session
(`savebin`), through the same probe-selection guard as the write, and compares
sha256: `readback-verified` on a match, `flash.readback-mismatch` on a
difference. A fresh session is stronger than the cache but still weaker than
reading after a cold power cycle, which is what alp-sdk#2233 says proves a write
on the bench.

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

### What this guard does *not* cover: Flow A

This is a **Flow D** guard, and only a Flow D guard. A slice that stays on
`zephyr_west_flash` — no `jlink_flash_device` on its `flash_args`, so `west
flash` picks the board.cmake default `alif_flash` runner and burns the ATOC
over the SE-UART (Flow A) — writes with no acknowledgement and no warning
today. Every published Ensemble variant in alp-sdk metadata carries
`debug.jlink_flash_device`, so a planner-emitted AEN manifest dispatches Flow D
and *is* guarded; a hand-written or legacy manifest without that key is not.

That gap is deliberately left open rather than closed by widening this flag:
alp-sdk#2025's own header says the Flow D flag must never be merged or aliased
with Flow A's `--replace-atoc`, precisely because an operator on a no-SE-UART
slot passes the Flow D one on every run — and a flag that answered both gates
would silence the one that *can* query the part first. Flow A's answer is a
query-based check (upstream's `bench_atoc_replace_guard`), which is its own
piece of work.

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
