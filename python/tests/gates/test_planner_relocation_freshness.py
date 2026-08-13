# SPDX-License-Identifier: Apache-2.0
"""Staleness gate: catch `tan/planner/**` falling behind alp-sdk's
`scripts/alp_orchestrate/**` a second time.

`tan/planner/` is alp-sdk's `scripts/alp_orchestrate/` relocated. The first
relocation was cut from a branch 19 commits behind alp-sdk main, so the port
carried an OLDER shape than the SDK actually ships (five modules had moved
upstream in that window: `__init__.py`, `loader.py`, `manifest.py`,
`models.py`, and the then-brand-new `sdk_compat.py`). Every parity number the
port produced before that fix compared tan against its own stale copy of the
oracle, proving self-consistency, not fidelity.

This gate is the guard against that happening silently again. It pins the
SHA-256 of every upstream `scripts/alp_orchestrate/*.py` file this port was
last audited against -- `PINNED_SDK_COMMIT` below names it, and is the only
place that commit is written down. When `ALP_SDK_ROOT` is bound, the gate re-hashes the SAME
files out of the bound checkout; a mismatch means the SDK moved and this port
has NOT been re-audited against the new shape -- the fix is to diff the
changed file(s), port the behavioural delta into `tan/planner/`, and update
`PINNED_HASHES` (and `PINNED_SDK_COMMIT`) to match, the same way this gate's
own history was produced.

This is deliberately a content hash, not a byte-identical port comparison:
`tan/planner/paths.py` and a few import lines legitimately differ from their
alp-sdk counterparts for relocation reasons (a path that became a bound SDK
root, an import that changed shape) and always will -- see each file's own
"RELOCATED" docstring. What must NOT drift unnoticed is the upstream side of
that comparison: if `scripts/alp_orchestrate/loader.py` changes again, this
gate fails even though nothing in `tan` changed, because the fact this port depends on -- "this is what alp-sdk's loader.py did as of
the last audit" -- is no longer true.

Without `ALP_SDK_ROOT` there is no oracle to compare against, so the gate
SKIPS -- visibly, naming the missing env var, never a silent pass.

The root is resolved through `tests.conftest.sdk_root()` AT MODULE IMPORT
TIME, and that is load-bearing, not style. This gate used to read
`os.environ["ALP_SDK_ROOT"]` from inside the test body -- where the autouse
`_scrub_sdk_discovery_env` fixture has already deleted it -- so it skipped on
EVERY run, including runs with the variable exported. It had therefore never
once compared a hash, which is how the fork drifted eleven alp-sdk commits
without a word (tan-cli#275). A gate that cannot fail is not a gate.

tan-cli#279: PINNED_HASHES only ever looks inside `scripts/alp_orchestrate/`,
so a HAND-PORT of some OTHER alp-sdk file is invisible to it by construction --
`git log <pinned>..<new> -- scripts/alp_orchestrate` comes back empty and
reassuring while such a file drifts. `zephyr_board.py`
(`scripts/gen_zephyr_board.py`) and `project_loader.py`
(`scripts/alp_project_loader.py`) both did, and both shipped real defects
before anything noticed (a missing `CONFIG_USE_DT_CODE_PARTITION=y`, and an
unaudited unknown-`hw_rev` path). `HAND_PORT_HASHES` below closes that for the
ten known hand-ports; `test_every_planner_module_is_tracked_or_declared_exempt`
closes it for the CLASS, by asserting every `.py` file under `tan/planner/`
is named in one of PINNED_HASHES, HAND_PORT_SOURCES, or
EXEMPT_FROM_RELOCATION_TRACKING, so a future hand-port with none of the three
has nowhere to hide.

tan-cli#296: PINNED_HASHES and HAND_PORT_HASHES are two audits of two
different things, pinned at two different alp-sdk commits (PINNED_SDK_COMMIT
and HAND_PORT_PINNED_SDK_COMMIT). For a while `parity.yml` bound a single
alp-sdk checkout and pointed both tests at it, so the hand-port test was
silently measured against PINNED_SDK_COMMIT's tree -- a plan-shape-parity
commit, not the one HAND_PORT_HASHES was actually audited against -- and its
failures reported drift that was really just the wrong oracle. Per
`docs/ROADMAP.md`'s Standing Rule ("a frozen tree is measured at its own
freeze vendor point; a shipped Python surface is measured at PINNED_SDK_TAG"),
two audits with two pins need two checkouts. `test_hand_ported_planner_
modules_match_their_pinned_sdk_source` now reads its own root from
`ALP_SDK_HAND_PORT_ROOT`, never `ALP_SDK_ROOT`/`ALP_SDK_PARITY_ROOT` (which
stay the relocated-planner test's root, pinned at PINNED_SDK_COMMIT).

WHEN THIS GATE FAILS, SOMETHING HAS PROBABLY ALREADY PROPOSED THE FIX.
`python/scripts/planner_resync.py` + `.github/workflows/planner-resync.yml`
(alp-sdk#855, ADR-0020's cross-repo remediation) turn a red run here into a PR
against `dev` on the branch `auto/planner-resync`: look there before re-typing
an upstream delta by hand. It fires on alp-sdk's existing
`alp-sdk-planner-change` `repository_dispatch` and, as a backstop that needs no
alp-sdk credential and no path filter to be right, on a daily cron. Locally:

    python python/scripts/planner_resync.py --sdk-root <alp-sdk> --to origin/dev

(dry run; add `--apply` to write). What it will and will not do, because the
three tables below are NOT one thing:

  * PINNED_HASHES is 3-WAY MERGED, not copied. `tan/planner/` is not the
    verbatim mirror it is sometimes called: measured at `7d58ef32`, 16 of the
    20 relocated modules differ from their upstream counterpart, by 2 lines
    (`__init__.py`) to 329 (`kconfig_symbols.py`) -- only the UPSTREAM side of
    this comparison is pinned, and the tan side carries real adaptations. So
    the tool merges `base = upstream@PINNED_SDK_COMMIT`, `theirs =
    upstream@<new>`, `ours = tan/planner/<f>`. A conflict writes nothing and
    blocks PINNED_SDK_COMMIT.
  * HAND_PORT_HASHES is FLAGGED, never merged. These counterparts are
    restructured, renamed, split (`alp_project_loader.py` -> TWO tan modules)
    or inlined (`sentinels.py`), so there is no base/ours/theirs triple a merge
    could be correct over. The tool attaches the upstream diff and stops; it
    does not move HAND_PORT_PINNED_SDK_COMMIT, so this test stays red until a
    human ports it. That is the intended state, not a bug in the automation.
  * STRICT_LOADERS_PINNED_SDK_COMMIT is checked and NEVER moved automatically
    -- see that block below for why (it names the introducing commit, and it is
    where a known open gap is written down).

The automation does not weaken any of this. It proposes; it never merges, and
a re-sync it could only partly apply opens a PR that says so and fails its own
job rather than a green one that quietly dropped a file.

Like that test, a missing `ALP_SDK_HAND_PORT_ROOT` SKIPS rather than fails --
a run that never bound this root (ci.yml's `python` job, a local `pytest
tests/`, a contributor's checkout) is not set up to do this audit, not
broken, and must not go red for it. tan-cli#308 first made this branch
`pytest.fail` instead, on the reasoning that an invisible skip is how #275
drifted eleven commits unnoticed -- and that took ci.yml's `python` job down
with it, since it runs the whole suite with neither root bound. The
enforcement #308 wanted belongs where the root IS actually bound:
`parity.yml`'s seam1 job, the one place both roots are ever set, asserts BY
NODE ID that neither freshness test skipped there (see that job's
`python/tests/gates` step) -- so a skip in the job meant to run the audit is
still a hard CI failure, and this branch can go back to skipping everywhere
else without reopening #275.
"""

from __future__ import annotations

import hashlib
import os
import pathlib

import pytest

from tests.conftest import sdk_root

#: alp-sdk commit this port was last audited against. Bumped from
#: `f4d87a1f` by the tan-cli #485 re-sync (#320 recurred: the #320 pin bump
#: held two days before alp-sdk landed two more contract-surface commits).
#: This bump carries alp-sdk #1169 (`_DRIVER_STATUS_SUFFIX` -- a
#: `<x>_driver_status` on_module field is a maturity tier, never a chip
#: slug; without the filter the literal string "none" reached
#: `CONFIG_ALP_SDK_CHIP_NONE=y`, an undeclared Kconfig symbol that aborted
#: Zephyr's CMake configure on every V2N-family SKU) and #1088 (`kind:
#: rpmsg` refuses `cacheable: true` -- `<alp/rpc.h>` has no
#: cache-maintenance implementation -- plus the companion
#: `_emit_cross_core_shmem_cache` widening to `entry.kind in ("raw_shmem",
#: "rpmsg")`) into `tan/planner/`. Also carries alp-sdk #1069's `carveout:
#: false` memory_map exclusion (`carveout.py`, already ported ahead of this
#: bump) and doc-only wording drift in `buildplan.py`/`orchestrator.py`
#: (alp-sdk #1214, no behavioural delta).
#:
#: `53557a60` -> `f30f4d4b` (tan-cli#543/#531, the audited bump #485 deferred
#: -- "`validate.py` ... stays for its own audited bump"; this is it). SIX
#: upstream commits, each classified by reading its diff rather than its
#: subject line, and every behavioural one ported into `tan/planner/`:
#:
#:   - `bc73b66c` (#1241) BEHAVIOURAL, and the live red: `_chip_has_driver`
#:     + the `som_chips = {s for s in som_chips if _chip_has_driver(s)}`
#:     filter in `kconfig.py`. `ethernet_phy: dp83825` made an undriven chip
#:     reachable from `on_module:` for the first time, so tan emitted
#:     `CONFIG_ALP_SDK_CHIP_DP83825=y` -- a symbol declared nowhere, since
#:     every `zephyr/kconfigs/chips.kconfig` entry reads "Compile
#:     chips/<part>/<part>.c" and there is no `chips/dp83825/`. This is what
#:     reddened `parity` on eight consecutive dispatch runs. Driver presence
#:     is checked ON DISK against the BOUND SDK root (`paths.REPO`), not read
#:     off `driver_status:`, exactly as upstream.
#:   - `66275c08` (#1331) BEHAVIOURAL: `partition.py`'s storage-region bounds
#:     check -- `_reserved_spans` + `_first_free`, the SoM's own `memory_map:`
#:     regions seeded into the overlap set, the bump allocator advancing PAST
#:     them, and a WARNING when the device origin cannot be derived+verified.
#:     Fixes a silent-corruption default (a littlefs mount resolving onto
#:     MCUboot at offset 0 of `mram_main`, `status` clean). tan-cli#545.
#:   - `5146f70d` (#1278) BEHAVIOURAL: `_slice_config_artefact` no longer
#:     returns `("cmake-args.txt", ...)` for a baremetal slice. The artefact
#:     was materialised and consumed by nothing; alp-sdk's own commit message
#:     names THIS repo as the half that still wrote it (tan-cli#492), so the
#:     removal had to land here or `--emit build-plan` parity stays red on
#:     every baremetal slice. `_slice_cmake_args` itself is untouched and
#:     still serves `tan generate --target cmake-args`.
#:   - `3e32ba1e` (#1228) DOC-ONLY *within this surface*: the whole
#:     `alp_orchestrate` delta is six lines inside `_emit_inference`'s
#:     DOCSTRING, naming `ETHOS_U_VARIANT_{U55,U65,U85}` /
#:     `TFLM_KERNEL_{NEON,HELIUM,REF}`. Verified rather than inherited: the
#:     emitting code already wrote those spellings (`kconfig.py:1083`,
#:     `:1038-1049`), so no emitted byte moves. Ported anyway to keep the
#:     mirror diffable.
#:   - `89b8112f` (#1267) NO BEHAVIOURAL DELTA, but NOT doc-only despite the
#:     `docs(accuracy)` subject -- it MOVES `_BLOCK_SLUGS` from `kconfig.py`
#:     to `slugs.py` (same frozenset, same single consumer). The move is
#:     ported so the two files stay diffable against upstream.
#:   - `f7c69cea` (#1223) BEHAVIOURAL ON BOTH FILES, again despite a
#:     `docs(accuracy)` subject -- `docs(...)` in a subject is not proof, and
#:     this is the commit that proves it. `loader.py` DROPS the legacy
#:     `raw: true` -> `fs: raw` normalisation (measured: the current
#:     `board.schema.json` declares no `raw` property and sets
#:     `additionalProperties: false` on storage items, so such a board is now
#:     rejected at validation instead). `validate.py` replaces the
#:     hand-listed `_CURATED_LIBRARIES` frozenset with
#:     `_curated_library_names(METADATA_ROOT)`, derived from
#:     `metadata/libraries/*.yaml` + `library-aliases-v1.json` -- the same
#:     reachability rule alp-sdk's `check_library_registry.py` cross-checks,
#:     so the two can no longer drift (9 curated libraries were silently
#:     missing from the old list).
#:
#: `f30f4d4b` -> `ccd34f06` (tan-cli#552/#553/#554/#555/#557/#558/#559/#562/
#: #563). THREE upstream commits touch `scripts/alp_orchestrate/` in this
#: range, all three BEHAVIOURAL, all three ported. Every one of the nine tan
#: issues they close was reproduced on `dev` and re-run after the port; none
#: was taken on the commit subject's word:
#:
#:   - `f6dcad09` (alp-sdk#1345) -> tan-cli#552/#553/#554. `carveout.py` gets
#:     `_endpoint_window` (`access_windows:`, the RZ/V2N CM33's 256 MiB DDR
#:     aperture -- a top-down allocation handed it `0x147f80000`, a 33-bit
#:     address that truncates BELOW the DDR base when cast to a pointer on
#:     the M33), `_first_free_down`, `placed_spans`, and a TWO-PASS placement
#:     that runs explicit `ipc[].address:` pins before the bump allocator so
#:     two channels can no longer resolve onto one physical address and both
#:     report `ok`. `partition.py` gets the same two-pass shape for
#:     `storage[].offset_kib:` -- in one pass a pin was only ever an obstacle
#:     for the siblings that sorted BEFORE it, so `offset_kib: 0` collided
#:     with an already-auto-allocated sibling and was dropped from
#:     `dts-partitions.dtsi` entirely.
#:   - `cf4ba601` (alp-sdk#1347) -> tan-cli#562/#563. `sdk_compat.py`'s
#:     family-table reader stops failing open: `_load_family_table` is RENAMED
#:     PUBLIC to `load_family_table` and now refuses four distinct
#:     present-but-unusable shapes (unreadable, unparseable, not a mapping, no
#:     `hw_revisions:` block) instead of answering `{}` -- which all three
#:     hw_rev gates read as "nothing to judge", so one tab-indented line
#:     disabled every one of them and emitted a wrong-hardware artefact at
#:     exit 0. `secure.py` stops hard-defaulting every SoM family to `mcuboot`
#:     (`_FAMILY_BOOT_METHOD_DEFAULTS`) and returns "" for a project with no
#:     Zephyr slice at all, so a Yocto-only V2N project with
#:     `boot.method: rsa3072` -- a value `validate.py` PERMITS for
#:     renesas-rzv2n -- no longer takes the whole build-plan emit down with
#:     sysbuild advice for a platform that never runs sysbuild.
#:   - `f01a2b94` (alp-sdk#1348) -> tan-cli#555/#557/#558/#559. `libraries.py`
#:     gains `scoped_names` + `_check_slice_requires` + `yocto_unwireable` so
#:     the core-scoped `libraries:` channel goes through the SAME ADR-0018
#:     layer as the project-wide one; `kconfig.py` gains `_split_server_url` /
#:     `_hawkbit_server_lines` (the URI was handed whole to
#:     `CONFIG_HAWKBIT_SERVER`, a DNS lookup that can never resolve, and
#:     `_PORT`/`_USE_TLS` were never emitted), `_hawkbit_poll_line` (the
#:     Kconfig symbol is MINUTES, board.yaml is SECONDS -- the schema's own
#:     1800 s default was emitted as 1800 minutes = 30 hours), and the
#:     `_LOG_MODULES` guard table behind a `_emit_diagnostics` that emits the
#:     CHOICE-symbol form `CONFIG_<MOD>_LOG_LEVEL_<LEVEL>=y` (the old int form
#:     was promptless and could not build for ANY key).
#:
#: `ccd34f06` -> `7d58ef32` (tan-cli#493/#591). Three upstream commits are in
#: this range and only two touch a tracked file:
#:   - `e85ba808` (alp-sdk#1246, `metadata/libraries/**` pin repairs) -- no
#:     `scripts/` file at all.
#:   - `a712a275` (alp-sdk#1344) -> `__init__.py`, `buildplan.py`,
#:     `orchestrator.py`. An `os: baremetal` slice planned one
#:     `cmake -S <app> -B <buildDir>` step, which only CONFIGURES: the PLANNER
#:     half is re-synced here (a `postCommands` step per baremetal slice, the
#:     configure's `-B` relative to its own cwd, the `-DALP_*` settings in
#:     both shapes CMake accepts, and `alp-baremetal.cmake` as the baremetal
#:     `_slice_config_artefact`). It had to move: `7d58ef32` is its child, and
#:     `metadata/socs/alif/ensemble/e8.json`'s new `zephyr_peripherals_dtsi`
#:     (which the re-synced `zephyr_board.py` REQUIRES) exists only in trees
#:     that also contain `a712a275`, so the emit-parity oracle cannot be
#:     pinned between the two. The CONSUMER half landed separately, in
#:     tan-cli#550/#551: `tan.core.build_plan` now parses `postCommands` and
#:     `tan.commands.build.execute` runs them, so a baremetal slice builds and
#:     an empty `artifacts.outputDir` is refused rather than reported `ok`.
#:     That is a `tan/core/` + `tan/commands/` change; the freshness hashes
#:     below still say nothing about it either way.
#:   - `7d58ef32` (alp-sdk#1352) -> `scripts/gen_zephyr_board.py` only, a
#:     HAND_PORT file; see HAND_PORT_PINNED_SDK_COMMIT below.
#: Every other file in both tables is byte-identical between `ccd34f06` and
#: `7d58ef32` -- re-hashed one by one, not assumed -- so this bump re-freezes
#: nothing unaudited.
#:
#: `7d58ef32` -> `1a9f753c` (tan-cli#657/#661/#662, unblocking all three).
#: Three upstream commits touch `scripts/alp_orchestrate/` in this range, all
#: three BEHAVIOURAL, ported into the four files this bump moves (`loader.py`,
#: `manifest.py`, `models.py`, `orchestrator.py`; the other 16 tracked modules
#: re-hashed byte-identical, not assumed):
#:
#:   - `c3de155a` (alp-sdk#1362) the read-only SW-DP IDR (DPIDR) wrong-board
#:     preflight. `loader.py` splits `_resolve_jlink_flash_device`'s variant
#:     match out into its own `_resolve_variant_debug` (so a future `debug:`
#:     fact cannot resolve against a different variant than its siblings),
#:     adds `_resolve_flow_d_preflight` (`expect_dpidr` +
#:     `jlink_device[core_id]`, both-or-neither, never one of the two) and
#:     `_enforce_flow_d_preflight_pair` (refuses a Flow-D-armed slice whose
#:     variant publishes `expect_dpidr` with no matching `jlink_device` for
#:     that core -- an unarmed guard is recoverable, a guard silently
#:     dropped is not). `models.Slice` gains `expect_dpidr`/`jlink_device`.
#:     `orchestrator._slice_flash_recipe` emits both into `flash_args` as
#:     one inseparable pair (a downstream flasher refuses a half-armed one).
#:     tan's consumer side (`tan.core.flash_plan.validate_flow_d_preflight_
#:     args`, `tan flash`'s `swd_probe`/Flow D arms) already read this shape
#:     -- landed ahead of the planner re-pin, per tan-cli#661/#662.
#:   - `496e32ad` (alp-sdk#1364) the helper_mcu three-axis projection.
#:     `manifest._helper_mcus` stops treating `update_channel` as mutually
#:     exclusive with `flash_method`/`flash_args`: a helper (the GD32 bridge)
#:     can declare an OTA channel for normal field updates AND a
#:     `flash_policy: recovery_only` swd_probe method for a bricked board,
#:     and dropping the flash keys because a channel exists would delete
#:     that recovery path from the manifest instead of letting `tan flash`
#:     decline it on `flash_policy`. Every declared key
#:     (`firmware_path`/`flash_method`/`flash_args`/`flash_policy`/
#:     `update_channel`) is now projected independently. tan's consumer side
#:     (`tan.core.flash_plan`'s `HelperMcu.flash_policy` field, `flash_cmd.py`'s
#:     recovery-only gate) already reads `flash_policy` -- landed ahead of
#:     this re-pin, per tan-cli#611.
#:   - `1a9f753c` (alp-sdk#1374) `flash_args.slot0_load_address` (tan-cli#353):
#:     the AEN MRAM slot0-XIP load address Flow D's auto-sign-via-SETOOLS
#:     path needs, which alp-sdk never emitted before this. `loader.py` adds
#:     `_resolve_slot0_load_address` (sourced from the SoM preset's
#:     `memory_map:`, NOT the SoC JSON -- this is SDK/module build policy,
#:     not a silicon fact; reuses `zephyr_board.py`'s own
#:     `_aen_role_slot0_map` via a LAZY import -- `zephyr_board.py` imports
#:     `_load_yaml` from `loader.py` at module scope, so a module-level
#:     import back would be circular) and
#:     `_enforce_slot0_disjoint_across_roles` (refuses a dual-M55 AEN SoM
#:     whose `m55_he`/`m55_hp` slices resolve to the SAME address -- not
#:     reachable today, kept as a guard against re-introducing #1069's
#:     HE/HP MRAM collision). `models.Slice` gains `slot0_load_address`;
#:     `orchestrator._slice_flash_recipe` emits it into `flash_args`. tan's
#:     consumer side (`tan.core.flash_plan`'s `slot0_load_address` handling,
#:     `plan_alif_mram_jlink`) already reads this key -- landed ahead of
#:     this re-pin, per tan-cli#657.
#:
#: `HAND_PORT_PINNED_SDK_COMMIT` moves to the same commit in this same
#: change (see that pin's own comment) -- re-hashed, not a bare bump: all
#: ten `HAND_PORT_HASHES` source files are byte-identical between `7d58ef32`
#: and `1a9f753c`, so nothing is re-frozen past an unaudited delta.
#: `STRICT_LOADERS_PINNED_SDK_COMMIT` does NOT move -- it names the alp-sdk
#: commit that INTRODUCED `strict_loaders.py`'s known read-escape gap (see
#: that pin's own comment), not merely "the last audit point"; moving it
#: would erase that meaning even though `scripts/strict_loaders.py` is also
#: byte-identical between `26b0040e` and `1a9f753c` (re-hashed, confirmed).
#:
#: `a3173305` -> `d00dbdc1` (tan-cli#560), BEHAVIOURAL: alp-sdk#1360/#1401.
#: `_slice_artifacts` in `buildplan.py` now reports a zephyr slice's six
#: `artifacts` paths (`elf`/`map`/`bin`/`sizeReport`/`symbols`/
#: `compileCommands`) rooted at `<buildDir>/build/`, the tree `west build`
#: -- run with cwd=`<buildDir>` and no `-d` -- actually writes; the old
#: spelling named `<buildDir>/zephyr/zephyr.elf`, a file west never
#: creates. `orchestrator.py`'s change in the same alp-sdk commit is
#: comment-only (the `_slice_command` docstring no longer claims the
#: consumer "reconciles" the nesting -- it doesn't have to any more).
#: `tests/parity/seam1_field_diff.py`'s vendored twin gains the matching
#: `_NESTED_ARTIFACT_TAILS` allowance, keyed on the same six fields and the
#: same one-segment `build/` insertion, so the frozen 97ad481b oracle's
#: un-nested paths keep passing against a live emit at this pin.
#:
#: `HAND_PORT_PINNED_SDK_COMMIT` moves to the same commit in this same
#: change (see that pin's own comment) -- re-hashed, not a bare bump: eight
#: of the ten `HAND_PORT_HASHES` source files are byte-identical between
#: `a3173305` and `d00dbdc1`; the two that moved (`scripts/gen_zephyr_
#: board.py`, `scripts/alp_template.py`) carry their own BEHAVIOURAL
#: deltas, ported into `zephyr_board.py` / `template.py` -- see that pin's
#: comment for the breakdown. `STRICT_LOADERS_PINNED_SDK_COMMIT` does NOT
#: move, same reasoning as above: `scripts/strict_loaders.py` does not
#: appear in the `a3173305..d00dbdc1` diff at all.
PINNED_SDK_COMMIT = "d00dbdc124491c89f68f404cd7ac9d26127f038f"  # alp-sdk origin/dev

#: sha256 of every `scripts/alp_orchestrate/<name>.py` at PINNED_SDK_COMMIT,
#: for every upstream module that has a same-named relocated counterpart
#: under `tan/planner/` (i.e. the actual drift surface -- files renamed on
#: relocation, like `alp_project_loader.py` -> `som_metadata.py`, have no
#: single upstream file to pin and are out of scope for this hash check).
PINNED_HASHES: dict[str, str] = {
    "__main__.py": "77b98caf27ba425b888a19f8727683bba23e7c24ebb4b6aa1874e5316a291d27",
    "__init__.py": "03b610ce02d1819d09ad3d5d233bbbd46b950bdc09448748b17ebc5a1b57f272",
    "buildplan.py": "54c49e8bd21dc0a283b6499b6b39314b089cf2cd65166451f884df614b7dca9f",
    "carveout.py": "23e7920110c333a1f3cbf51ce186c4c2cebdb3ef1573c06df64ca1e9a80be478",
    "cli.py": "b2d9e82d62c5dd1668d4d893e148fb66efc50825b465c8f8385f9bf668572419",
    "headers.py": "9a9cc0ca4801b2bdb7a551662e4dddf27c47bb42fad06939c92a8c95b221156b",
    "kconfig.py": "4c4a5abea3b1316d66e01f8bcd2e32411c28863179c9983746c06be52e415d30",
    "kconfig_symbols.py": "fe3a3df4aa00db808ce8443548d113b4a97cf600b5fda106d075e8d071243729",
    "libraries.py": "bf4fd845248067f7713ce270ced265ba2a2c981f91f34911fe446849e9f57a5d",
    "loader.py": "e871dedab4aa21746fab8a5e57f863d11986b5a604f02d48c9b92852f8a4f417",
    "manifest.py": "f38de96a9626672bc08f181e09b3a545d8dc846c0423cc6e9dd08c3b96a87d1d",
    "memregion.py": "f3e62050172bb1500e98d0023eda7408a67e1085a70a4acd92f45f08213ebfa3",
    "models.py": "edf04000c361838b1523eff1a3fd1a5d7b60a5e95583c9acf5d587b8d379ea32",
    "orchestrator.py": "c5d25284f9f06a907c7b95e5d905e17d9a0fa68c9df3f25bb419537a2ce1d5a6",
    "partition.py": "7f37224ff1aa05dd6d943424a664bc4d115dc05853762072854d43ea3628591c",
    "paths.py": "a2d8b74570f88ad223d797d6428a58fc3851dad6bb9a1ae2c2aa109db789bc93",
    "sdk_compat.py": "db2c6658b421cf862118b468ff164cdeea36debae291af37ad6f840fe9565970",
    "secure.py": "f58c5e4ff2c00908ccb3783ce925b26cdaa311548dc8694ffdb0fd01dfce8e95",
    "slugs.py": "339bffdb8e5fef41eefc0cd2eb05705c2b3e53580c7cfd775e1dd1c65127d5cb",
    "topology.py": "12f5f62d3adeb9e935594934fd2fc2b1fbeaec6f466d6dd89c329c54e844f3b1",
    "validate.py": "07202af06235cc4bcd262ff457b0139e93cd9ad01ccf07b35e4d0ef99e05afa0",
}

#: alp-sdk commit the SDK-SIDE SOURCE FILES in HAND_PORT_HASHES were last
#: audited against -- the commit `v0.15.0-rc1` names, not the tag itself, so a
#: tag that is later moved cannot silently repoint what this pins.
#: `zephyr_board.py` and `project_loader.py` were re-vendored to this point in
#: 947f3d0 / 52fdd01.  This pin does NOT claim the `tan/planner/` SIDE is
#: frozen at that same point, and it is not: tan-cli#485 forward-ported alp-sdk
#: #1048's case-insensitive TBD match into `zephyr_board.py:109` and
#: `som_metadata.py:140`, and #1025's status-gate half into
#: `project_loader.py:196` -- both strictly AFTER 996937ac, both ported
#: without moving this pin. This test HASHES THE SDK SIDE ONLY
#: (`upstream = root / rel_path`, never the `tan/planner/` file), so a
#: hand-port racing ahead of its own recorded audit point like this is
#: invisible to it by construction -- a real, standing gap in this gate's
#: coverage that a future audit pass should account for, not a claim that it
#: doesn't exist.
#:
#: `996937ac` -> `f30f4d4b` (tan-cli#544, and the standing gap above CLOSED for
#: this round): TEN upstream commits touch these four files, and the gap is
#: exactly why each was checked against the CURRENT `tan/planner/` file rather
#: than assumed missing. Four were ALREADY PRESENT, forward-ported without
#: moving this pin, and re-confirmed here by measurement, not memory:
#:
#:   - `1ad76193` (#1069, disjoint per-core slot0 windows) -- present in
#:     `zephyr_board.py`'s `_aen_role_slot0_map` / disjoint branch.
#:   - `93ab1a54` (#1048, one `is_tbd()` helper) -- present as the inlined
#:     case/whitespace-insensitive match at `zephyr_board.py:108` and
#:     `som_metadata.py:143` (tan-cli#485).
#:   - `1a91a232` (#1025, the status gate on the second resolution path) --
#:     present at `project_loader.py:205` (`sdk_compat.revision_buildable`,
#:     raising `SdkRevisionNotBuildable`). Re-measured downstream: this is
#:     what makes `seam1_field_diff.py` report `PASS multicore_rpmsg-imx93
#:     (alp-sdk and tan both refuse this board)`, so tan-cli#425's recorded
#:     divergence no longer reproduces.
#:   - `8b1a460a` (#1004) + `f4d87a1f` (#1096) -- NOT APPLICABLE. Both
#:     consolidate alp-sdk's five/four hand-rolled `silicon:` splits onto
#:     `resolve_soc_path()`/`split_silicon_ref()`. `tan/planner` already keeps
#:     ONE soft-fail copy (`som_metadata.resolve_soc_path`, same falsy /
#:     not-exactly-3-parts -> `None` contract) plus the raising variant in
#:     `loader._silicon_to_soc_path`; the remaining upstream delta is
#:     docstring prose about call sites that do not exist here.
#:
#: FOUR carried real deltas and are ported in this change:
#:
#:   - `d639e777` (#1289) BEHAVIOURAL, and the reason this bump exists:
#:     `zephyr_board.py`'s `_aen_flash_partitions` reserves the App MRAM top
#:     for the SE-owned ATOC. `_AEN_STORAGE_KIB` 128 -> 96, new
#:     `_AEN_ATOC_KIB = 32`, an `atoc` region appended after `storage` in BOTH
#:     branches, `_AEN_ATOC_KIB` added to the `reserved` sum, and the four
#:     label/nodelabel maps extended. `96 + 32 == 128`, so `image_kib` is
#:     unchanged and no committed AEN board's slot geometry moves. SETOOLS
#:     top-anchors the ATOC package at the App MRAM window end (`0x80580000`
#:     on the E8) and grows it DOWNWARD at PROVISIONING time; bench-observed
#:     on E1M-AEN801 2026-08-08, ATOC magic `ckBS` (`0x53426B63`) intact at
#:     `0x8057EA50` while a Zephyr app erased `0x80560000` inside the SAME
#:     `storage` partition. Either direction leaves an unbootable part, and it
#:     is silent until the next boot. tan-cli#544/#515.
#:   - `77abfd7c` (#1279) BEHAVIOURAL: `project_emit/hw_info.py`'s per-core
#:     macro guidance. This is EMITTED TEXT inside the generated
#:     `<alp/hw_info.h>`, not a source comment, so the old wording was a live
#:     byte divergence from the SDK oracle.
#:   - `cb7f64ae` (#1125, #1126) BEHAVIOURAL (security): `_safe_join` +
#:     `PathEscapeError` in `template.py`, applied at both `_rendered_bytes`
#:     read sites and at `render_to_envelope`'s `example_dir`. Resolve-then-
#:     contain, so it fails closed on traversal, an absolute `rel`, and a
#:     symlink escape alike. The `#1125` half (`build_model`'s name pattern)
#:     is alp-sdk-only -- that function did not relocate.
#:   - `b24770fa` (#1287) + `560b256e` (#1266) BEHAVIOURAL: `_cmake_core_map`
#:     (each Zephyr core's OWN `--core` rename applied to its OWN
#:     `CMakeLists.txt`, via `orchestrator._zephyr_app_dir`) and
#:     `_scaffold_readme`'s qualified-then-short board-target rewrite plus the
#:     `_m33_sm` `west flash --host <board-ip>` rule. Both change
#:     `--emit scaffold` bytes, which is why `python/tan/templates/vendored/`
#:     is re-vendored in this same change.
#:
#: `ccd34f06` -> `7d58ef32` (tan-cli#493/#591). ONE file in this table moved:
#: `scripts/gen_zephyr_board.py`, by alp-sdk#1352 -- the upstream fix for both
#: issues, reported against this hand-port and fixed at the source. The other
#: eight are byte-identical between the two commits (re-hashed one by one, not
#: assumed), so nothing else is re-frozen past an unaudited delta. What the
#: re-sync carries: the Ensemble part designator, the family display string and
#: the peripherals-overlay include now come from the SoC JSON (`part`,
#: `family`, the new `zephyr_peripherals_dtsi`) instead of being E8 generator
#: constants applied to every `E1M-AEN*` SKU; a SoC declaring no overlay is
#: REFUSED rather than inheriting the E8's; four fail-open paths raise (a
#: half-authored per-core `<role>_slot0` map, a `silicon_variant:` matching no
#: `order_code`, partition extents/overlaps, and an `mcuboot` base off the App
#: MRAM window); the `atoc`-absent message names the alp-sdk vintage
#: (alp-sdk#1289) instead of the consumer's SoM preset; and the `_defconfig`
#: console pads come from `metadata/pinmux/aen.yaml`.
#:
#: This re-sync REQUIRES a bound SDK whose `metadata/socs/alif/ensemble/e8.json`
#: declares `zephyr_peripherals_dtsi` -- measured: against `ccd34f06` the AEN
#: board emit now refuses, and `test_tan_generate_writes_a_zephyr_board_tree_
#: byte_for_byte` fails. That is why PINNED_SDK_COMMIT and `parity.yml`'s
#: PINNED_SDK_TAG move to the same commit rather than staying behind.
#:
#: `7d58ef32` -> `1a9f753c` (tan-cli#657/#661/#662, moved alongside
#: PINNED_SDK_COMMIT above): a PURE re-pin, not a bare bump -- all ten
#: `HAND_PORT_HASHES` source files below (`scripts/gen_zephyr_board.py`
#: through `scripts/alp_project_emit/west_libs.py`) were re-hashed one by
#: one against `1a9f753c` and are byte-identical to their `7d58ef32`
#: hashes; `git diff --stat 7d58ef32..1a9f753c -- <each path>` confirms
#: empty for every one. Moved to the same commit as PINNED_SDK_COMMIT only
#: because it costs nothing to audit and keeps the two pins from silently
#: drifting apart on a future bump that touches one bundle but not the
#: other.
#:
#: `1a9f753c` -> `a3173305` (tan-cli#639, this re-sync): the same PURE
#: re-pin, and left behind once already -- the first cut of this branch
#: moved `ci.yml`'s `ref:`, `parity.yml`'s `PINNED_SDK_TAG` and
#: `PINNED_SDK_COMMIT` to `a3173305` and left THIS constant at
#: `1a9f753c`, which is the "two pins, one checkout" trap this file's
#: header warns about, in its fourth variant. It passed anyway, but on a
#: coincidence rather than on correctness: all ten `HAND_PORT_HASHES`
#: source files below are byte-identical between `1a9f753c` and
#: `a3173305` (sha256 compared one by one; 44 files changed across that
#: range and not one of them is in this bundle), so a stale pin bound
#: against a freshly-checked-out `a3173305` re-hashed to the same values
#: and nothing went red. Had any one of the ten moved, the gate would
#: have failed pointing at a file the branch never touched. Move all
#: four together or the next bump lands the red instead.
#:
#: `a3173305` -> `d00dbdc1` (tan-cli#560), a REAL re-audit, not a bare
#: bump: `git diff --stat a3173305..d00dbdc1 -- <each of the ten>` is
#: non-empty for exactly two files.
#:
#:   - `scripts/gen_zephyr_board.py` (alp-sdk#1373/#1407, tan-cli#690):
#:     every AEN board's generated `Kconfig.defconfig` gains a `choice
#:     LOG_MODE / default LOG_MODE_MINIMAL` block. Zephyr's inherited
#:     LOG_MODE_DEFERRED needs `CONFIG_LOG_PROCESS_THREAD` to run, and the
#:     AEN bench procedure runs apps whose `main()` never yields (the
#:     non-yielding busy loop is what keeps the Secure Enclave from gating
#:     the DAP/SE-UART) -- so a healthy, fault-free board printed ZERO
#:     UART bytes. Ported verbatim into `zephyr_board.py::
#:     _aen_kconfig_defconfig` as the new `_AEN_LOG_MODE_DEFAULT` constant,
#:     same placement inside the `if BOARD_<sym>` guard as ROM_START_OFFSET.
#:   - `scripts/alp_template.py` (alp-sdk#1394/#1399 + #1400): TWO
#:     behavioural deltas, both ported into `template.py`. #1394 adds a
#:     collision guard to `_derive_pin_doc_renames` -- two `pins:` entries
#:     re-deriving a SHARED `doc:` string to two different targets now
#:     raises `TemplateError` instead of silently keeping whichever
#:     resolution ran last (the separate `resolved` map records every
#:     entry's resolution, not only the renaming ones). #1400 adds
#:     `_rewrite_stale_sdk_root_comment` -- `_scaffold_cmakelists` now
#:     loops instead of `subn`, rewriting each guess block's own preceding
#:     comment paragraph (which used to keep teaching an in-tree
#:     `../../..` fallback the hardened block no longer has) in step with
#:     the code. tan's two deliberate divergences from the oracle (the
#:     `_ALP_SDK_ROOT_REQUIRED_BLOCK in text` idempotence guard, and the
#:     `_SDK_ROOT_DEPENDENT_RE` `TemplateError` raise where upstream
#:     returns best-effort unchanged) are preserved. #1400 changes `--emit
#:     scaffold` bytes, which is why `python/tan/templates/vendored/` is
#:     re-vendored in this same change -- see that tree's MANIFEST.md.
#:
#: The other eight of the (then-)ten are byte-identical between `a3173305`
#: and `d00dbdc1` (re-hashed one by one, not assumed), so nothing else was
#: re-frozen past an unaudited delta. `scripts/strict_loaders.py` does not
#: appear in the diff at all, so `STRICT_LOADERS_PINNED_SDK_COMMIT` stays put.
#:
#: BLOCKER, found in review of tan-cli#560 itself: that "ten" was never the
#: full hand-port surface. `scripts/alp_cli/faultdecode.py` -- the alp-sdk
#: source `tan/core/faultdecode.py` is hand-ported from -- sat OUTSIDE both
#: `scripts/alp_orchestrate/` (PINNED_HASHES) and this table, so the
#: `a3173305..d00dbdc1` re-audit above never looked at it, and alp-sdk
#: dad5b35a (#1389, "lead with the escalated fault, not the escalation") --
#: which lives INSIDE that same range and changes exactly that file -- went
#: completely unaudited. It happened to be harmless (upstream ADOPTED both of
#: tan-cli#616's declared divergences verbatim, so `tan/core/faultdecode.py`
#: needed no code change), but the gate could not have told the difference
#: from a real drift; it never even ran the comparison. `faultdecode.py` now
#: joins this table (11th entry) so the next SDK-side change to it is
#: audited the same way as everything else here.
HAND_PORT_PINNED_SDK_COMMIT = "d00dbdc124491c89f68f404cd7ac9d26127f038f"  # alp-sdk origin/dev

#: sha256 of every alp-sdk source file a `tan/planner/**` module was
#: hand-ported from OUTSIDE `scripts/alp_orchestrate/`, keyed by its
#: alp-sdk-relative path -- NOT by filename: `project_loader.py` and
#: `som_metadata.py` are both ported out of the SAME
#: `scripts/alp_project_loader.py`, so a filename-keyed table (like
#: PINNED_HASHES above, where the relocation is 1:1) cannot hold this.
#:
#: `scripts/sentinels.py` is in THIS table but deliberately NOT in
#: HAND_PORT_SOURCES: it has no `tan/planner/` file of its own to name. Its
#: one function is hand-copied INTO another module --
#: `zephyr_board.py::_is_tbd`, the relocated spelling of `sentinels.is_tbd`
#: (alp-sdk #1048), which upstream imports from both `scripts/` and
#: `alp_orchestrate/` and which would otherwise cost a whole tracked module
#: for three lines. `slugs.py:42` sets the same precedent with its own
#: `_is_tbd`. Tracking the SDK-side hash anyway is the point: without it, an
#: upstream change to what counts as the `TBD` sentinel (the normalisation is
#: currently case- and surrounding-whitespace-insensitive, exact-match only)
#: would drift both copies silently, and neither this table nor
#: `test_every_planner_module_is_tracked_or_declared_exempt` -- which only
#: walks files that exist under `tan/planner/` -- would say a word. Verified
#: byte-identical between `ccd34f06` and `7d58ef32` (1186 bytes,
#: `54c0b5c4...`), so pinning it here at HAND_PORT_PINNED_SDK_COMMIT re-freezes
#: nothing unaudited.
#:
#: `scripts/alp_cli/faultdecode.py` is in THIS table for the same reason as
#: `sentinels.py` above and, like it, deliberately NOT in HAND_PORT_SOURCES:
#: its port, `tan/core/faultdecode.py`, lives under `tan/core/`, not
#: `tan/planner/`, so it has no `tan/planner/`-relative path this dict's
#: `HAND_PORT_SOURCES` half (a coverage list over `tan/planner/**.py`, see
#: that dict's own docstring) could name. Tracking the SDK-side hash anyway
#: is the point (tan-cli#560 review, blocker 2): without it, this entire
#: freshness gate had zero visibility into `scripts/alp_cli/faultdecode.py`,
#: and alp-sdk dad5b35a (#1389) changed it inside the very
#: `a3173305..d00dbdc1` range this file's own pin moved across, unseen. sha256
#: taken directly from the `d00dbdc1` checkout this HAND_PORT_PINNED_SDK_COMMIT
#: already names, so pinning it here re-freezes nothing unaudited either.
#:
#: The eight `scripts/alp_cli/{diagnostic_format,validate,new_som,doctor,
#: explain,monitor,model,validator}.py` entries below close the same blind
#: spot (tan-cli#560 review, minor 2): each is named as a hand-port source in
#: a comment somewhere under `python/tan/` --
#: `tan/output_format.py`/`tan/commands/validate_cmd.py`
#: (`diagnostic_format.py`), `tan/commands/validate_cmd.py` (`validate.py`),
#: `tan/commands/new_som_cmd.py` (`new_som.py`),
#: `tan/commands/doctor_cmd.py`/`tan/core/doctor_libraries.py` (`doctor.py`),
#: `tan/core/error_catalog.py`/`tan/commands/explain_cmd.py` (`explain.py`),
#: `tan/commands/monitor_cmd.py` (`monitor.py`), and
#: `tan/commands/model_cmd.py` (`model.py`). `scripts/alp_cli/validator.py`'s
#: `load_board_schema`/`iter_schema_errors` are hand-ported into
#: `tan/planner/loader.py` (that file's own docstring: "RELOCATED from
#: alp-sdk's scripts/alp_cli/validator.py, the last module-scope import this
#: file made across the repo boundary") -- `loader.py` itself is already
#: tracked in `PINNED_HASHES` above (its main body relocated from
#: `scripts/alp_orchestrate/loader.py`), so this is the SAME split-heritage
#: shape `sentinels.py` set the precedent for: a hand-port INTO an
#: already-tracked file still needs its own SDK-side source hashed. None of
#: these eight sources live under `tan/planner/` themselves, so like
#: `faultdecode.py` they are deliberately NOT added to `HAND_PORT_SOURCES`
#: either. sha256 taken directly from the `d00dbdc1` checkout
#: HAND_PORT_PINNED_SDK_COMMIT already names, and each verified unchanged
#: across the `a3173305..d00dbdc1` range this file's own pin moved across, so
#: pinning them here re-freezes nothing unaudited. `scripts/alp_project_loader.py`
#: -- the ninth source the same review named -- is deliberately NOT re-added:
#: it is already a key above (added for `tan/planner/project_loader.py` and
#: `tan/planner/som_metadata.py`), and its OWN additional hand-port sites
#: (`tan/core/renode_plan.py`, `tan/commands/new_som_cmd.py`) are already
#: covered by that one entry -- `HAND_PORT_HASHES` is keyed by the alp-sdk
#: source path, not by the consuming `tan` file, so a second key for the same
#: path would be a no-op duplicate, not a new audit.
HAND_PORT_HASHES: dict[str, str] = {
    "scripts/gen_zephyr_board.py": "083018a76a774d6ea37da87d3d8dda6eda4515c5cd924ebf0b5a141c9ba2cf9b",
    "scripts/sentinels.py": "54c0b5c4211a638f1a6141340e76b2bc7e32935b8c61ba5e8948e2da1ab81d9c",
    "scripts/alp_project_loader.py": "d5f142173a13cfac9e130ef8fde90d35d6bb92d21d152925a275b3e8bdaa49db",
    "scripts/alp_template.py": "9321c7e31759ef4f9c03c2c750b1d7d7f4019b9a50dd2679668deaa2b0054708",
    "scripts/alp_project_emit/__init__.py": "62c4742bc373e7fafcd8aa864ad7692d3c05b610c6d7457023aeb82c98847d88",
    "scripts/alp_project_emit/bom_netlist.py": "d2ccef0b4453aede2119cf9af1de7c1f97f2780f7cf1ec7e9b717aafaa8e32f8",
    "scripts/alp_project_emit/dts.py": "cb6d4278e2fc886a23c28f2ef30b4ae9714738071219f7c29cbccbbeb1bc1782",
    "scripts/alp_project_emit/hw_info.py": "25673bb45305ce3f54280560beea8577bdf04bfdada44a133ff7ca48fbe05167",
    "scripts/alp_project_emit/native_sim.py": "24943e7099d745b254b853135ff0b4ae8415be7946d93170d479b637105f18c0",
    "scripts/alp_project_emit/west_libs.py": "0bfad8fb6c22b955d0554f8fffca8c1c9bf9f73d3c64778b9ba2de76eb6a972d",
    "scripts/alp_cli/faultdecode.py": "3a9e82b7b6892523923e6f571602be1e3bb11e24090dde0b90f6a5ae207aaa0b",
    "scripts/alp_cli/diagnostic_format.py": "d6f7872013b7990a08ca724814daaf800a8acf37dec4d9a7d5078807757d162d",
    "scripts/alp_cli/validate.py": "c7b7175798c3e8f0d7961fb40e3318f60570f7ec85d131c599f83109c2cebfe6",
    "scripts/alp_cli/new_som.py": "1118f99aa8c334c5d058e69b0e454954b4637678971d9c47472e45dc2d4eb558",
    "scripts/alp_cli/doctor.py": "f2faa07cecbbffc1bcfb510210e3f24d96a3ad6864eef8f3fe92f93886ddacd5",
    "scripts/alp_cli/explain.py": "b9e05d32896d1e0855f1c040b581b3e77869b4b03b15371c125757be1e0e09fe",
    "scripts/alp_cli/monitor.py": "1f67ee1372c73e2bd76b5c9338c141e31accdef1e206b7a64806cf8eb691c0e2",
    "scripts/alp_cli/model.py": "a51be0a8d3a16bd408bb57d01f049175406b73cc48ab9346d39555c3aa5b1925",
    "scripts/alp_cli/validator.py": "8dac2e4d3799fe67feceb74e587f23b5e8b44a40df2805220632f8edae26a421",
}

#: `tan/planner/`-relative path -> the alp-sdk-relative source path it was
#: hand-ported from (a key into HAND_PORT_HASHES). Doubles as the coverage
#: list `test_every_planner_module_is_tracked_or_declared_exempt` checks
#: every on-disk `tan/planner/**.py` file against.
HAND_PORT_SOURCES: dict[str, str] = {
    "zephyr_board.py": "scripts/gen_zephyr_board.py",
    "project_loader.py": "scripts/alp_project_loader.py",
    "som_metadata.py": "scripts/alp_project_loader.py",
    "template.py": "scripts/alp_template.py",
    "project_emit/__init__.py": "scripts/alp_project_emit/__init__.py",
    "project_emit/bom_netlist.py": "scripts/alp_project_emit/bom_netlist.py",
    "project_emit/dts.py": "scripts/alp_project_emit/dts.py",
    "project_emit/hw_info.py": "scripts/alp_project_emit/hw_info.py",
    "project_emit/native_sim.py": "scripts/alp_project_emit/native_sim.py",
    "project_emit/west_libs.py": "scripts/alp_project_emit/west_libs.py",
    # strict_loaders.py is intentionally NOT in HAND_PORT_HASHES -- see the
    # STRICT_LOADERS_* block below for why it needs its own pin/root/test
    # rather than joining this bundle.
    "strict_loaders.py": "scripts/strict_loaders.py",
}


def test_hand_port_sources_declares_its_one_strict_loaders_exception():
    """Every `HAND_PORT_SOURCES` value is a key into `HAND_PORT_HASHES` --
    EXCEPT `scripts/strict_loaders.py`, deliberately (see the STRICT_LOADERS_*
    block below). Without this assertion, deleting
    `test_strict_loaders_matches_its_pinned_sdk_source` (or its
    `STRICT_LOADERS_*` pin) would leave `strict_loaders.py` in
    `HAND_PORT_SOURCES` -- satisfying `test_every_planner_module_is_tracked_
    or_declared_exempt`'s coverage check -- with NO staleness hash anywhere
    checking it, and neither coverage gate would notice: this is the one
    assertion that catches that specific silent regression."""
    unhashed = set(HAND_PORT_SOURCES.values()) - set(HAND_PORT_HASHES)
    assert unhashed == {"scripts/strict_loaders.py"}, (
        "HAND_PORT_SOURCES names a hand-port source with no staleness hash "
        f"anywhere: {sorted(unhashed)}. Every HAND_PORT_SOURCES value must be "
        "a key in HAND_PORT_HASHES, except scripts/strict_loaders.py (which "
        "has its own STRICT_LOADERS_HASH instead -- see that block's own "
        "comment for why). If this assertion fails because the set is EMPTY, "
        "scripts/strict_loaders.py's own STRICT_LOADERS_* pin was deleted or "
        "renamed -- restore it, don't just widen this assertion."
    )


#: tan-cli#485: `tan/planner/strict_loaders.py` (the alp-sdk #1127
#: duplicate-key-rejecting YAML/JSON loaders, wired into `loader.py`'s
#: `_load_yaml`/`_load_json` to close the silent-sku-retarget hazard) was
#: hand-ported from `scripts/strict_loaders.py` -- a file that did not exist
#: at all at HAND_PORT_PINNED_SDK_COMMIT when this split was written (then
#: `996937ac`, which predates alp-sdk #1127). It gets its OWN pin/root/test
#: rather than joining HAND_PORT_HASHES/HAND_PORT_PINNED_SDK_COMMIT,
#: deliberately: auditing the rest of that bundle between 996937ac and the
#: pin current at the time turned up a REAL, narrower gap this fix does not
#: close. (`scripts/strict_loaders.py` DOES exist at the current
#: HAND_PORT_PINNED_SDK_COMMIT, `d00dbdc1` -- blob
#: `d4b6ce64850acb7893ecb894a96988636cc32324` -- so "does not exist" is no
#: longer why it stays split; the gap below is.) `scripts/alp_template.py`
#: gained `_safe_join`/
#: `PathEscapeError` (alp-sdk #1125/#1126); `tan/planner/template.py` has its
#: own parallel `tan.core.fs_confine.PathEscapeError`/`resolve_confined`,
#: wired into every catalog-driven WRITE (`scaffold.py:1049`,
#: `init_cmd.py:804`, `generate_cmd.py:488`, `pinmux_cmd.py:347`) -- so no
#: write can escape its destination root. What it is NOT wired into is
#: `template.py`'s catalog-driven READS: `_rendered_bytes` (`:210` joins
#: `base_dir / record["example"]`, `:214` joins that onto each `rel` and
#: calls `.read_bytes()`) and `render_to_envelope` (`:1091`, the same join
#: onto `board.yaml`). Measured with a traversal `rel` against a shared
#: catalog fixture: `tan`'s `_rendered_bytes` returns the escaped file's
#: bytes; alp-sdk's raises `PathEscapeError`. Those bytes reach the caller
#: through `emit_scaffold`'s `[{path, contents}]` envelope, so a malicious or
#: compromised `--metadata-root`/catalog entry can read an arbitrary file
#: readable by the `tan` process and hand it back as scaffold content.
#: Threat model, scoped honestly: this requires a hostile SDK/catalog
#: checkout, which is already trusted to run arbitrary CMake/west during a
#: normal build -- meaningfully narrower than #1126's write-bug severity (a
#: write escape corrupts files outside any project the caller chose; this
#: read escape discloses them), but real, and not closed by this change.
#: Folding strict_loaders.py into HAND_PORT_HASHES would force a choice
#: between silently re-freezing the whole bundle past this gap unaudited
#: (the exact "bare bump" tan-cli#485 exists to name) or fixing it as a
#: drive-by inside an unrelated change -- filed separately instead (see
#: tan-cli#485's own report). (alp-sdk #1069's disjoint per-core slot0
#: memory layout, the OTHER large delta in this bundle's window, is NOT a
#: gap: tan-cli#432 already ported it into `zephyr_board.py` byte-for-byte,
#: confirmed by re-reading the whole file, not just the diff.) Same
#: tan-cli#296 rationale that split PINNED_SDK_COMMIT from
#: HAND_PORT_PINNED_SDK_COMMIT in the first place: two audits that drifted
#: at different rates need two pins, not one shared one.
STRICT_LOADERS_PINNED_SDK_COMMIT = "26b0040e9a762c16aff5c7c53b2e19cc7583b2a4"  # alp-sdk origin/dev, introduces #1127

#: sha256 of `scripts/strict_loaders.py` at STRICT_LOADERS_PINNED_SDK_COMMIT.
STRICT_LOADERS_HASH = "29cd2c62836e70abf2fa3f4e8c0939b406bd8cb6b976d9e97bc75d4180e38eef"

#: The env var carrying a checkout pinned at STRICT_LOADERS_PINNED_SDK_COMMIT.
#: Its own name for the same tan-cli#296 reason HAND_PORT_SDK_ROOT_ENV is not
#: reused: three different pins now exist in this file (PINNED_SDK_COMMIT,
#: HAND_PORT_PINNED_SDK_COMMIT, STRICT_LOADERS_PINNED_SDK_COMMIT), and
#: sharing a root between any two of them silently measures one against the
#: wrong oracle.
STRICT_LOADERS_SDK_ROOT_ENV = "ALP_SDK_STRICT_LOADERS_ROOT"


def _resolve_strict_loaders_sdk_root() -> pathlib.Path | None:
    raw = os.environ.get(STRICT_LOADERS_SDK_ROOT_ENV)
    if raw and (pathlib.Path(raw) / "scripts" / "alp_project.py").is_file():
        return pathlib.Path(raw).resolve()
    return None


#: Resolved at import time, same reasoning as `SDK` / `HAND_PORT_SDK` above.
STRICT_LOADERS_SDK: pathlib.Path | None = _resolve_strict_loaders_sdk_root()


def test_strict_loaders_matches_its_pinned_sdk_source():
    """The `strict_loaders.py` half of tan-cli#279's hand-port staleness gate.

    Same shape as `test_hand_ported_planner_modules_match_their_pinned_sdk_source`,
    scaled down to the one file that needs its own pin -- see the
    STRICT_LOADERS_PINNED_SDK_COMMIT block above for why. Skips (rather than
    fails) without STRICT_LOADERS_SDK_ROOT_ENV bound, same posture as the
    other two freshness tests: a run not set up to do this audit is not
    broken for not doing it.
    """
    if STRICT_LOADERS_SDK is None:
        pytest.skip(
            f"{STRICT_LOADERS_SDK_ROOT_ENV} is not set (or does not point "
            "at a real alp-sdk checkout) -- no bound alp-sdk checkout to "
            "compare tan/planner/strict_loaders.py against, so this "
            "staleness gate cannot run. This is a SKIP about the missing "
            f"root, not a pass: set {STRICT_LOADERS_SDK_ROOT_ENV} to an "
            f"alp-sdk checkout at STRICT_LOADERS_PINNED_SDK_COMMIT "
            f"({STRICT_LOADERS_PINNED_SDK_COMMIT}) to actually exercise "
            "the gate."
        )
    upstream = STRICT_LOADERS_SDK / "scripts" / "strict_loaders.py"
    assert upstream.is_file(), (
        f"scripts/strict_loaders.py: gone from the bound SDK checkout"
    )
    current_hash = hashlib.sha256(upstream.read_bytes()).hexdigest()
    assert current_hash == STRICT_LOADERS_HASH, (
        "scripts/strict_loaders.py moved past the alp-sdk commit "
        f"({STRICT_LOADERS_PINNED_SDK_COMMIT}) tan/planner/strict_loaders.py "
        f"was last audited against: sha256 {current_hash} != pinned "
        f"{STRICT_LOADERS_HASH}. Diff it in the bound alp-sdk checkout, port "
        "the behavioural delta into tan/planner/strict_loaders.py, then "
        "update STRICT_LOADERS_HASH (and STRICT_LOADERS_PINNED_SDK_COMMIT) "
        "in this file to re-pin the audit."
    )


#: `tan/planner/`-relative paths with no alp-sdk source at all -- original tan
#: code, not a port. A file belongs here only if it genuinely has no alp-sdk
#: counterpart; a real hand-port belongs in HAND_PORT_SOURCES instead.
EXEMPT_FROM_RELOCATION_TRACKING: frozenset[str] = frozenset({
    # tan-cli#591: the general "does the bound alp-sdk checkout carry
    # capability X" floor `zephyr_board.py` calls into. Tan-native --
    # alp-sdk has no such module to drift against; the thing this file
    # tracks is a REGISTRY of alp-sdk facts (a schema property, a
    # metadata/quality-tasks-v1.json task id), not a port of alp-sdk code.
    "sdk_capability.py",
})


#: Resolved at import time -- see the module docstring on why that matters.
SDK: pathlib.Path | None = sdk_root()


def _sdk_root() -> pathlib.Path:
    if SDK is None:
        pytest.skip(
            "ALP_SDK_ROOT is not set -- no bound alp-sdk checkout to compare "
            "tan/planner/ against, so this staleness gate cannot run. This is "
            "a SKIP about the missing root, not a pass: set ALP_SDK_ROOT to a "
            "real alp-sdk checkout to actually exercise the gate."
        )
    return SDK


#: The env var carrying a checkout pinned at HAND_PORT_PINNED_SDK_COMMIT.
#: Deliberately its OWN name -- NOT ALP_SDK_ROOT/ALP_SDK_PARITY_ROOT, which
#: `sdk_root()` above reads and which stay pinned to the DIFFERENT
#: PINNED_SDK_COMMIT. Reusing either here is the tan-cli#296 bug: two audits
#: of two different alp-sdk states sharing one checkout.
HAND_PORT_SDK_ROOT_ENV = "ALP_SDK_HAND_PORT_ROOT"


def _resolve_hand_port_sdk_root() -> pathlib.Path | None:
    """Same check `tests.conftest.sdk_root()` makes, for HAND_PORT_SDK_ROOT_ENV
    alone -- see the module docstring's tan-cli#296 note for why this cannot
    just be a second name added to that function's own env-var list."""
    raw = os.environ.get(HAND_PORT_SDK_ROOT_ENV)
    if raw and (pathlib.Path(raw) / "scripts" / "alp_project.py").is_file():
        return pathlib.Path(raw).resolve()
    return None


#: Resolved at import time, same reasoning as `SDK` above.
HAND_PORT_SDK: pathlib.Path | None = _resolve_hand_port_sdk_root()


def _hand_port_sdk_root() -> pathlib.Path:
    if HAND_PORT_SDK is None:
        # Skip, not fail -- same shape as `_sdk_root()` above, same reason: a
        # run that never bound this root (ci.yml's `python` job, a local
        # `pytest tests/`) is not "set up to do this audit", not broken.
        # tan-cli#308 made this branch `pytest.fail` instead, on the theory
        # that a skip here is how #275 drifted silently -- and that took
        # ci.yml's unbound `python` job down with it. The enforcement #308
        # wanted belongs where the root IS bound: `parity.yml`'s seam1 job
        # asserts BY NODE ID that this test did not skip there (see that
        # job's `python/tests/gates` step), so THIS branch can go back to
        # skipping without reopening #275 -- the one job meant to run the
        # audit can no longer silently no-op it.
        #
        # Still deliberately NOT a fallback to `_sdk_root()`
        # (ALP_SDK_ROOT/ALP_SDK_PARITY_ROOT, the relocated-planner root,
        # pinned at the DIFFERENT PINNED_SDK_COMMIT) -- that is the
        # tan-cli#296 bug: two audits, two pins, sharing one checkout.
        pytest.skip(
            f"{HAND_PORT_SDK_ROOT_ENV} is not set (or does not point at a "
            "real alp-sdk checkout) -- no bound alp-sdk checkout to compare "
            "the HAND-PORTED tan/planner/ modules against, so this "
            "staleness gate cannot run. This is a SKIP about the missing "
            f"root, not a pass: set {HAND_PORT_SDK_ROOT_ENV} to an alp-sdk "
            f"checkout at HAND_PORT_PINNED_SDK_COMMIT "
            f"({HAND_PORT_PINNED_SDK_COMMIT}) to actually exercise the gate."
        )
    return HAND_PORT_SDK


def test_relocated_planner_modules_match_the_pinned_sdk_audit():
    orchestrate = _sdk_root() / "scripts" / "alp_orchestrate"
    drifted: list[str] = []
    for name, pinned_hash in PINNED_HASHES.items():
        upstream = orchestrate / name
        if not upstream.is_file():
            drifted.append(f"{name}: gone from the bound SDK checkout")
            continue
        current_hash = hashlib.sha256(upstream.read_bytes()).hexdigest()
        if current_hash != pinned_hash:
            drifted.append(
                f"{name}: sha256 {current_hash} != pinned {pinned_hash}"
            )
    # The hash loop above only sees files it already knows about, so a BRAND-NEW
    # upstream module is invisible to it -- and that is precisely the drift that
    # caused this incident: `sdk_compat.py` arrived upstream and tan simply did
    # not have it. Pin the SET as well as the contents.
    upstream_names = sorted(q.name for q in orchestrate.glob("*.py"))
    if upstream_names != sorted(PINNED_HASHES):
        added = sorted(set(upstream_names) - set(PINNED_HASHES))
        removed = sorted(set(PINNED_HASHES) - set(upstream_names))
        if added:
            drifted.append(
                "NEW upstream module(s) with no counterpart audited here: "
                + ", ".join(added)
            )
        if removed:
            drifted.append(
                "module(s) removed upstream but still pinned here: " + ", ".join(removed)
            )

    assert not drifted, (
        "scripts/alp_orchestrate/ moved past the alp-sdk commit "
        f"({PINNED_SDK_COMMIT}) this port was last audited against. Diff each "
        "file below in the bound alp-sdk checkout, port the behavioural delta "
        "into the matching tan/planner/ module, then update PINNED_HASHES "
        "(and PINNED_SDK_COMMIT) in this file to re-pin the audit:\n  "
        + "\n  ".join(drifted)
    )


def test_hand_ported_planner_modules_match_their_pinned_sdk_source():
    """tan-cli#279: the HAND-PORT half of the staleness gate.

    Same shape as `test_relocated_planner_modules_match_the_pinned_sdk_audit`
    above, but keyed by the alp-sdk-relative source path rather than a
    `scripts/alp_orchestrate/`-relative name -- these nine files live all over
    alp-sdk's `scripts/` tree, not in one directory. Reads its OWN root
    (`_hand_port_sdk_root()`, ALP_SDK_HAND_PORT_ROOT) rather than `_sdk_root()`
    -- see the module docstring's tan-cli#296 note.
    """
    root = _hand_port_sdk_root()
    drifted: list[str] = []
    for rel_path, pinned_hash in HAND_PORT_HASHES.items():
        upstream = root / rel_path
        if not upstream.is_file():
            drifted.append(f"{rel_path}: gone from the bound SDK checkout")
            continue
        current_hash = hashlib.sha256(upstream.read_bytes()).hexdigest()
        if current_hash != pinned_hash:
            drifted.append(
                f"{rel_path}: sha256 {current_hash} != pinned {pinned_hash}"
            )
    assert not drifted, (
        "a tan/planner/ hand-port's alp-sdk source moved past the commit "
        f"({HAND_PORT_PINNED_SDK_COMMIT}) it was last audited against. Diff "
        "each file below in the bound alp-sdk checkout, port the behavioural "
        "delta into the tan/planner/ module(s) HAND_PORT_SOURCES names for it, "
        "then update HAND_PORT_HASHES (and HAND_PORT_PINNED_SDK_COMMIT) in "
        "this file to re-pin the audit:\n  "
        + "\n  ".join(drifted)
    )


def test_every_planner_module_is_tracked_or_declared_exempt():
    """tan-cli#279: the COVERAGE half -- no bound alp-sdk checkout needed.

    The two tests above only ever check files that are already keys in
    PINNED_HASHES / HAND_PORT_HASHES, so a brand-new hand-port added to
    `tan/planner/` from yet another alp-sdk source is invisible to both --
    which is exactly how `zephyr_board.py` and `project_loader.py` escaped
    for as long as they did. This asserts the SET instead: every `.py` file
    that exists on disk under `tan/planner/` must be named in one of
    PINNED_HASHES (relocated from `scripts/alp_orchestrate/`),
    HAND_PORT_SOURCES (hand-ported from elsewhere), or
    EXEMPT_FROM_RELOCATION_TRACKING (no alp-sdk source at all) -- so a future
    hand-port with none of the three has nowhere to hide.
    """
    planner = pathlib.Path(__file__).resolve().parents[2] / "tan" / "planner"
    tracked = set(PINNED_HASHES) | set(HAND_PORT_SOURCES) | EXEMPT_FROM_RELOCATION_TRACKING
    untracked = sorted(
        rel
        for p in planner.rglob("*.py")
        for rel in [str(p.relative_to(planner)).replace("\\", "/")]
        if rel not in tracked
    )
    assert not untracked, (
        "tan/planner/ has file(s) with no recorded alp-sdk origin: "
        + ", ".join(untracked)
        + ". If it is a hand-port of an alp-sdk file OUTSIDE "
        "scripts/alp_orchestrate/, add it to HAND_PORT_HASHES + "
        "HAND_PORT_SOURCES above (tan-cli#279). If it relocated FROM "
        "scripts/alp_orchestrate/, its basename belongs in PINNED_HASHES "
        "instead. If it has no alp-sdk source at all, declare it in "
        "EXEMPT_FROM_RELOCATION_TRACKING with a one-line reason."
    )
