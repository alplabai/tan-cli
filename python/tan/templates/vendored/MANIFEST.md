<!-- SPDX-License-Identifier: Apache-2.0 -->
# Vendored scaffold provenance (alp-sdk#864)

This tree is `alp-sdk --emit scaffold` output, captured byte-for-byte (LF, no
retouching) and checked in so `tan init`/`tan scaffold` can read it without
ever shelling the SDK. `tests/parity/scaffold_byte_parity.py` re-runs the live
emit against a reachable alp-sdk checkout and fails loudly if this tree drifts
from an un-revendored SDK change.

## Source

- **tan-cli#501: `sensor` and `diagnostics` gained `boards/native_sim_native_64.{overlay,conf}`.**
  Neither template's vendored tree carried the `boards/` overlay+conf pair
  their canonical example (`examples/peripheral-io/i2c-master` for `sensor`,
  `examples/bringup/board-selftest` for `diagnostics`) ships, even though
  Zephyr auto-discovers that path with no CMakeLists wiring at all. Without
  it, `west build -b native_sim/native/64` on a scaffolded project has no
  `alp-i2c0` DT alias and no `CONFIG_EMUL`/`CONFIG_I2C_EMUL`, so the run does
  not match either README's own "Expected output" block (measured: an
  unaliased I2C open fails with `alp_last_error=-2` instead of the documented
  NACK probe). `edge-ai` ships the same gap but is unaffected -- read from
  `examples/ai/cold-chain-monitor/src/main.c`, whose `bus != NULL &&
  bme280_init(...)` short-circuit lands on the same synthetic-data `LOG_WRN`
  branch whether the I2C alias resolves or not, not independently re-measured
  with a real Zephyr build -- and is deliberately left unvendored (declared in
  `scaffold_byte_parity.py`'s `DELIBERATELY_MISSING_EXTRAS`, see below). Fixed
  by vendoring the pair (byte-identical, from the pinned commit below) into
  all four `sensor`/`diagnostics` × SKU trees and adding both paths to
  `tests/parity/scaffold_byte_parity.py`'s `NON_ENVELOPE_EXTRAS` (the same
  mechanism `native_sim.conf`/tan-cli#379 use).

  Landed alongside two follow-on fixes the first cut of this change was
  missing (both re-measured against the pinned SDK tag below):
  1. `native-sim-overlay`'s destination colliding with this vendored file
     used to make a freshly scaffolded project's bare `tan generate` refuse
     outright (`generate.would-overwrite`, exit 3, nothing written), and
     `--all --force` — the refusal's own recommended remedy — silently
     replaced the vendored `alp-i2c0` overlay with tan's GPIO-only emit.
     `generate_cmd.py` now leaves `native-sim-overlay` alone (dropped from
     the run, `generate.overlay-not-owned` warning, exit 0) whenever it
     rides along in the bare/`--all` default set rather than being named by
     an explicit `--target`; an explicit `--target native-sim-overlay`
     still refuses without `--force` and still overwrites with it, exactly
     as before. `python/tests/commands/test_generate_command.py` pins both
     directions.
  2. `scaffold_byte_parity.py` could not have failed on a MISSING vendored
     extra at all: `augment_with_example_extras` only ever reached for a
     `NON_ENVELOPE_EXTRAS` name the vendored tree already carried, so
     `--sdk <1a9f753c>` was **9/9 PASS with this whole change reverted**
     (measured both ways). `missing_extras` closes that: any
     `NON_ENVELOPE_EXTRAS` file the live example ships that the vendored
     tree does not is now a hard failure unless declared in
     `DELIBERATELY_MISSING_EXTRAS` (`edge-ai`'s pair is the one entry).
     `python/tests/core/test_scaffold.py::
     test_sensor_and_diagnostics_scaffolds_ship_the_native_sim_board_pair`
     is the SDK-free pin of the same gap.

  A first cut of this change also gave all four `sensor`/`diagnostics`
  `CMakeLists.txt` files `list(PREPEND EXTRA_CONF_FILE ...)`, on the theory
  that appending the generated `alp.conf` let it win over the vendored board
  conf's `CONFIG_EMUL`/`CONFIG_I2C_EMUL`. MEASURED false with a real CMake
  configure against Zephyr v4.4.1: `boards/native_sim_native_64.conf` joins
  `CONF_FILE`, not `EXTRA_CONF_FILE` (Zephyr's board-dir auto-discovery), and
  `CONF_FILE_AS_LIST` is merged strictly before `EXTRA_CONF_FILE_AS_LIST`
  regardless of PREPEND/APPEND order within the latter -- PREPEND and APPEND
  produced the identical merge order and identical `.config` either way.
  Reverted to plain `list(APPEND ...)`, which is what `--emit scaffold`
  produces unedited, so these four files carry no "Deliberate edit" anymore
  (see below; this is unrelated to the real `iot`/tan-cli#379 PREPEND, whose
  scenario is a caller's own `-DEXTRA_CONF_FILE=` build line, which neither
  `sensor` nor `diagnostics` documents).

  Verified against `PINNED_SDK_TAG` (`1a9f753c`) after the fixes above:
  `scaffold_byte_parity.py --sdk <1a9f753c>` is rc 0, **9/9** PASS, `sensor`
  and `diagnostics` reporting 8 files each (was 6) — and rc 1, **4 FAIL**
  (the two vendored `boards/` files missing on each of the four trees) with
  the vendoring reverted, so the gate now actually covers this change.

- **`PINNED_SDK_TAG` has since moved past this vendor point, deliberately.**
  The tan-cli#552…#563 planner re-sync bumped `parity.yml`'s `PINNED_SDK_TAG`
  (and `ci.yml`'s `sdk_parity` checkout `ref:`) from `f30f4d4b` to `ccd34f06`,
  and **no re-vendoring was needed**: nothing under `examples/`, the scaffold
  catalog or the templates themselves changed in that range — the whole
  alp-sdk diff is `scripts/`, `metadata/schemas|socs`, `docs/`, `tests/` and
  `CHANGELOG.md`. Verified, not assumed: `scaffold_byte_parity.py --sdk
  <ccd34f06>` is rc 0, **9/9** (template, sku) pairs PASS against this tree
  unchanged. So the vendor point below stays where it is, and the two refs are
  allowed to differ for exactly this reason.

  The tan-cli#493/#591 re-sync moved both refs again, `ccd34f06` →
  `7d58ef32`, and again needed **no re-vendoring**. Six `examples/**` files
  did move in this range (five `board.yaml`s and
  `power-timing/littlefs-keyvalue/src/main.c`), but none is a scaffold
  template or a catalog entry. Verified, not assumed: `scaffold_byte_parity.py
  --sdk <7d58ef32>` is rc 0, **9/9** (template, sku) pairs PASS against this
  tree unchanged.

- **Current vendor point (all templates):** **`d00dbdc1`**
  (`d00dbdc124491c89f68f404cd7ac9d26127f038f`, alp-sdk `dev`) — tan-cli#560's
  re-sync, which moves `PINNED_SDK_COMMIT`/`HAND_PORT_PINNED_SDK_COMMIT` past
  alp-sdk#1394/#1399 (`c07254b2`) and #1400 (`739e998b`), both landed in the
  `HAND_PORT_HASHES` file `scripts/alp_template.py`. #1400 added
  `_rewrite_stale_sdk_root_comment()`, which rewrites the `ALP_SDK_ROOT`
  comment paragraph in every emitted `CMakeLists.txt` that carries the
  in-tree `../../..` guess block. **Four** files moved, all `CMakeLists.txt`:
  `edge-ai`/E1M-AEN801, `edge-ai`/E1M-V2N101, `minimal`/E1M-AEN801 and
  `minimal`/E1M-V2N101; `edge-ai`'s pair also gains #1390's
  `OUTPUT_VARIABLE`/`ERROR_VARIABLE` capture on `execute_process`, folded
  into the same alp-sdk range.

  **Re-vendoring was necessary but not sufficient.** `scripts/alp_template.py`
  is a `HAND_PORT_HASHES` file, so this re-sync also ports
  `_rewrite_stale_sdk_root_comment()` and the loop-based
  `_scaffold_cmakelists` into `tan/planner/template.py` itself (kept in step
  with `_derive_pin_doc_renames`'s alp-sdk#1394 collision guard, the other
  behavioural delta in this same file across the range) — see that module's
  own history. `scaffold_byte_parity.py` (vendored bytes vs. the SDK emit)
  alone would have gone green on the re-vendor without the emitter port;
  `test_planner_emit_parity.py` (tan's OWN emit vs. the SDK emit) is what
  actually exercises the ported code.

  `iot`/E1M-AEN801's `CMakeLists.txt` still carries the standing
  `DELIBERATE_EDITS` entry (tan-cli#379's `list(PREPEND EXTRA_CONF_FILE
  ...)`) and was NOT re-vendored — re-vendoring it would silently revert that
  fix. Verified at `d00dbdc1`: `scaffold_byte_parity.py` **9/9 PASS** (rc 0).

- **Prior vendor point:** **`f30f4d4b`** (alp-sdk `dev`,
  the commit `parity.yml`'s `PINNED_SDK_TAG` named when that was captured) —
  re-vendored by the tan-cli#543/#544/#545 planner re-sync. **Eleven** files
  moved:
  - The **seven** `README.md` doc-link files (`diagnostics`, `minimal` and
    `sensor` for both SKUs, plus `iot`/E1M-AEN801) change only
    `blob|tree/v0.15.0-rc1/` → `blob|tree/v0.15.0/`. Those bytes are now the
    EMIT'S OWN: alp-sdk has since cut the real `v0.15.0` tag (`e2928b9f`), so
    tan-cli#384's seven `DELIBERATE_EDITS` entries in
    `tests/parity/scaffold_byte_parity.py` are **RETIRED** rather than
    re-pointed — that module's doctrine makes an `un_edit` with nothing left
    to undo a hard failure, so a healed divergence must force its entry out.
    The `un_edit_doc_link_ref` transform is kept for the next pre-release
    vendor point.
  - `edge-ai`'s **two** `README.md` files gain the alp-sdk#1266 board-target
    rewrite, and its **two** `testcase.yaml` files follow their example's
    current content. `edge-ai/*/testcase.yaml` is RETAINED, not dropped: the
    catalog record still carries
    `testcase_yaml: ["examples/ai/cold-chain-monitor/testcase.yaml"]` and the
    file exists on disk, so `augment_with_example_extras` diffs it like any
    other file (both `edge-ai` pairs report 8 files, not 6).
  - `iot`/E1M-AEN801's `CMakeLists.txt` keeps its tan-cli#379 deliberate edit
    and was NOT rewritten.

  Re-vendored by re-running the live emit through
  `tests/parity/scaffold_byte_parity.py`'s OWN `discover_vendored_matrix` +
  `emit_live_scaffold` + `augment_with_example_extras`, writing each changed
  path with `newline="\n"` and never touching a `DELIBERATE_EDITS` path — the
  same throwaway-driver shape as the bumps below, run on Linux (Python
  3.12.3). Verified after: `scaffold_byte_parity.py --sdk <f30f4d4b>` rc 0,
  **9/9** (template, sku) pairs PASS.

  This bump lands in the SAME commit as the `PINNED_SDK_TAG` move and the
  `tan/planner/` port it depends on, because either half alone reds a seam.

- **Previous vendor point:** **`v0.15.0-rc1`** (`996937ac`) —
  the release tag, re-vendored to match `parity.yml`'s `PINNED_SDK_TAG` move
  off `v0.14.0`. Same seven `README.md` files as the v0.14.0 bump below and
  NOTHING else: every changed line differs only by the doc-version link
  `blob/v0.14.0/` → `blob/v0.15.0/` (the SDK's own doc-link renderer drops the
  `-rc1` suffix; verified line-for-line, `git diff | grep -vc
  "v0\.1[45]\.0"` returns 0). No schema, core, peripheral or `board.yaml`
  content changed — in particular alp-sdk#1068 (`CONFIG_USE_DT_CODE_PARTITION`
  in the AEN board `_defconfig`) touches none of `--emit scaffold`'s output;
  `edge-ai` (whose README carries no version-pinned links) diffed clean at 0/8
  files, confirming it. No `PINNED_SDK_COMMIT`/`PINNED_HASHES` re-audit was
  needed either: `scripts/alp_orchestrate/` is byte-identical between
  `0f3cefbe` (the planner's last audit point) and this tag.
  - Re-vendored by re-running the live emit through
    `tests/parity/scaffold_byte_parity.py`'s OWN `discover_vendored_matrix` +
    `emit_live_scaffold`, writing each changed path with `newline="\n"` — the
    same throwaway-driver shape as the v0.14.0 bump below, run on Windows
    (Python 3.12.10; the "needs WSL" note two bumps below was already
    superseded — see that entry).
  - **Diverged from `crates/tan-core/src/wizard/vendored/` on purpose.**
    That tree was frozen at `v0.14.0` (`ef79eab0`) and never re-vendored; this
    one is what a Python `tan` actually reads, so it tracks `PINNED_SDK_TAG`.
    tan-cli#269 has since deleted `crates/` outright, which makes this the only
    vendored scaffold tree in the repo. The obsolete
    Python-vs-Rust byte-identity test was retired; the shipping tree is guarded
    by its LF-only unit test and by `tests/parity/scaffold_byte_parity.py`
    against the live pinned SDK instead.
  - **The vendor ref and `parity.yml`'s `PINNED_SDK_TAG` are NOT the same ref,
    and that is deliberate.** The pin is `f4d87a1f` (tan-cli#320, a commit past
    `v0.15.0-rc1` — see that variable's own comment for why), while this tree
    stays captured at the `v0.15.0-rc1` TAG, because the links below have to
    name a ref a customer can open in a browser and `v0.15.0` does not exist
    (tan-cli#384). Re-measured at `f4d87a1f`: `--emit scaffold` is
    byte-identical to this tree across all 9 (template, sku) pairs except the
    two deliberate edits below — no `board.yaml`, `prj.conf`, `src/` or
    `CMakeLists.txt` content moved in that range. That is what makes the split
    safe; it is not a licence to let the two drift unchecked, and
    `scaffold_byte_parity.py` is what re-checks it.
  - Bumping that pin drives all FOUR parity gates — it re-vendors this tree,
    the bootstrap manifest fixture, the toolchain lock and the kconfig golden
    together, or the gates go red. They are one atomic unit, not four
    independent bumps. (The toolchain lock and kconfig fixture needed no byte
    change this round — see each gate's own re-run notes; only this tree and
    the bootstrap manifest fixture had real drift.)
  - Re-vendored by re-running the emit, not by editing these files. A
    hand-edit that happens to match today is a copy that drifts tomorrow;
    the point of this tree is that it is generated.
- Repo: `alplabai/alp-sdk`
- Ref: `v0.15.0` — the ref every shipped doc link in this tree pins, and the
  one `tests/core/test_template_integrity.py` reads off THIS line to check
  them against. It is now the emit's OWN rendered ref rather than a hand-edit:
  the emit renders the link ref from the SDK's `VERSION` (dropping any
  pre-release suffix), and alp-sdk has since cut the real `v0.15.0` tag
  (`e2928b9f`), so **tan-cli#384's hand-edit is retired and the seven
  `DELIBERATE_EDITS` entries with it.** Links resolve as emitted.
- Commit: **`f30f4d4b`** (alp-sdk `dev`) — the checkout the emit was RUN
  against, and the same commit `parity.yml`'s `PINNED_SDK_TAG` now names.
  Distinct from `Ref:` above on purpose: `Ref:` is the ref the rendered LINKS
  name (a browsable tag, `v0.15.0`), `Commit:` is where the BYTES came from.
  `f30f4d4b` is 6 contract-surface commits past the `v0.15.0` tag, which is
  why the two are not one line.
- Previous: `v0.15.0-rc1` (release tag) / **`996937ac`** — the seven READMEs
  described in the entry below. History below.
- Previous: **v0.14.0 (`ef79eab0`)** — the release tag, re-vendored for tan
  v0.4.1. Seven `README.md` files moved (`diagnostics`, `minimal` and `sensor`
  for both SKUs, plus `iot`/E1M-AEN801), and NOTHING else: all 40 changed
  lines differ only by the doc-version link `blob/v0.13.0/` → `blob/v0.14.0/`,
  verified line-for-line. No schema, core, peripheral or `board.yaml` content
  changed. `edge-ai` is untouched for both SKUs because its README carries no
  version-pinned links at all.
  - Re-vendored by re-running the live emit through
    `tests/parity/scaffold_byte_parity.py`'s OWN `emit_live_scaffold`, so the
    bytes written are by construction the bytes that gate compares against —
    not a hand-substitution that happens to match. Run from WSL: the emit needs
    a Linux host, and the gate cannot be checked on Windows at all.
  - Prior vendor point: `cdfe13684e362c75f6df2b190ec1c3e736c48731` —
    alp-sdk#1016, which rewrote the `Customer workflow:` header in every example
    `board.yaml` from "copy this directory … and `west build`" to "`tan init
    --from-example <category>/<name>` … and `tan build`" (ADR-0020: tan is the
    whole command surface). Six `board.yaml` files moved; comment-only.
- Previous: **v0.13.0 (`93ef5726`)** — `minimal` was re-vendored at this commit
  (only its `README.md` doc-version link changed, `v0.11.1` -> `v0.13.0`; the
  scaffold content itself is unchanged since the `a0849e10` vendor point
  below). `sensor` and `edge-ai` were vendored fresh at this same commit --
  `edge-ai` supersedes the `edge-ai-starter` flag this manifest carried
  before v0.13.0 (see "Template-id mapping" below).
  - Prior vendor point: `a0849e10` (`feat(build-plan): --emit scaffold
    derives cores per SKU + adapts scaffold content (#864) (#877)`) —
    re-vendored from this commit (tan-cli#25 had vendored `75ef3b02`, before
    #877 fixed `--emit scaffold` deriving the wrong, non-buildable Alif
    `m55_hp` core for every SKU including `E1M-V2N101`; see "App-core
    disagreement" below).
- **`diagnostics`/`iot` vendor point:** commit
  `0ed078a6d04f4072ab00d8ce92cb2ac82f7adcbc` — an `alp-sdk` `dev` commit, 31
  commits past `v0.13.0`, pinned as `tan-cli`'s own
  `.github/workflows/parity.yml` `PINNED_SDK_TAG` at vendor time. alp-sdk#903
  (closed) added both catalog entries; vendored at this exact SHA (rather
  than a later `dev` tip) so the checked-in bytes match what `parity.yml`'s
  own `scaffold_byte_parity.py` step already re-verifies on every PR.
- Command: `PYTHONPATH=$SDK/scripts python3 scripts/alp_project.py --emit
  scaffold --template <id> --sku <SKU>`

### Deliberate edits on top of the emit

The rule above ("re-vendored by re-running the emit, not by editing these
files") has standing exceptions, each because the emit's own output is wrong
for a customer and the fix lives in alp-sdk, not here. Each is a real diff
`tests/parity/scaffold_byte_parity.py` would otherwise report, and each
disappears on its own the moment alp-sdk fixes it and this tree is
re-vendored — nothing here needs unwinding by hand.

The `DELIBERATE_EDITS` table below currently carries **one** live entry (the
`iot` CMakeLists edit). tan-cli#384's seven `README.md` doc-link entries are
NOT among them — they were RETIRED when alp-sdk cut the real `v0.15.0` tag
(see "Current vendor point" above); listed here only as history, not as a
current exception. tan-cli#501's four `sensor`/`diagnostics` CMakeLists edits
were REVERTED (see entry 3 below) — the theory behind them measured false, so
those four files carry no deliberate edit at all now.

**Each is DECLARED to that gate, in `scaffold_byte_parity.py`'s
`DELIBERATE_EDITS`, and the declaration is strict in both directions.** An
entry is not a path-level allow-list: it carries an `un_edit` that maps these
bytes back onto what the emit is expected to say, and the byte-diff runs
against THAT — so an unrelated change in the same file still fails the gate,
and a declared edit that finds nothing to undo (this tree re-vendored, or
alp-sdk fixing its emit) ALSO fails, forcing the entry out instead of leaving
a dead excuse behind. That is the same `xfail(strict=True)` discipline
`python/tests/parity/test_scaffold_content_oracle_parity.py` used on the
port-vs-oracle axis, before tan-cli#269 deleted that axis with the oracle.
Editing this section without editing that table (or the reverse) is what the
strictness exists to catch.

1. **(RETIRED, history only) Doc-link ref, all seven `README.md` files (tan-cli#384).** The emit
   renders cross-directory links as `github.com/alplabai/alp-sdk/blob/v<SDK
   VERSION>/…`, and this tree is vendored from a PRE-RELEASE
   (`v0.15.0-rc1`) — so it emitted 40 links to `v0.15.0`, a tag alp-sdk has
   never cut. Every one 404s in a scaffolded project. Rewritten to
   `v0.15.0-rc1`, the exact ref these bytes come from; all 14 distinct link
   targets verified present at that ref (`git cat-file -e v0.15.0-rc1:<path>`
   for each, 14/14 OK).
   `python/tests/core/test_template_integrity.py` holds both halves: one test
   keeps the links and the `- Ref:` line above in step, and
   `test_the_vendored_ref_is_a_tag_alp_sdk_actually_has` asks GitHub whether
   that ref EXISTS — consistency is not existence, and the two agreed on
   `v0.15.0` while all 40 links were dead. It queries the EXACT-ref endpoint
   (`/git/ref/tags/`, singular) and carries a permanent negative control,
   because the plural `/git/refs/tags/` prefix-matches and answers 200 for
   `v0.15.0` with `refs/tags/v0.15.0-rc1` — a gate built on it calls every
   dead link healthy.
2. **`iot`'s `CMakeLists.txt`: `list(PREPEND …)`, not `APPEND` (tan-cli#379).**
   Zephyr merges `EXTRA_CONF_FILE` in list order, last assignment wins.
   Appending the generated `alp.conf` put it AFTER a caller's own
   `-DEXTRA_CONF_FILE=native_sim.conf` (measured: `native_sim.conf;<build-dir>/
   generated/alp.conf`), so the emitted `CONFIG_MBEDTLS=y` overrode the very
   overlay that exists to turn it off — the README's documented native_sim
   build and `testcase.yaml`'s `extra_args` were both no-ops. Prepending keeps
   `alp.conf` winning over `prj.conf` (the whole list merges after it) and
   lets an explicit caller overlay win. alp-sdk's own
   `examples/connectivity/mqtt-telemetry/CMakeLists.txt` still appends;
   the other two templates without any vendored native_sim conf (`minimal`,
   `edge-ai`) are left as emitted (neither ships an overlay or documents a
   `-DEXTRA_CONF_FILE=` build).
3. **(REVERTED, history only) `sensor`/`diagnostics` `CMakeLists.txt`, both
   SKUs: `list(PREPEND …)` (tan-cli#501), on the theory that it was the same
   defect class as #2.** It is not: `boards/native_sim_native_64.conf` (the
   pair these four trees gained in the same change) joins Zephyr's
   `CONF_FILE`, not `EXTRA_CONF_FILE` -- Zephyr auto-discovers a board's own
   `boards/<board>.conf` regardless of `EXTRA_CONF_FILE` order, and
   `CONF_FILE_AS_LIST` merges strictly before `EXTRA_CONF_FILE_AS_LIST`
   either way. MEASURED with a real CMake configure against Zephyr v4.4.1:
   PREPEND and APPEND produced the identical merge order and identical
   `.config` (`CONFIG_EMUL=y`, `CONFIG_I2C_EMUL=y` both ways). Reverted to
   plain `list(APPEND …)`, which is what `--emit scaffold` produces unedited
   for these four files, so they carry no deliberate edit anymore and this
   entry is not live.

## Template x SKU matrix vendored

| tan `WizardTemplateId` | SDK catalog id | Vendored SKUs | Example dir | Files |
|---|---|---|---|---|
| `zephyr-app` | `minimal` | `E1M-AEN801`, `E1M-V2N101` | `examples/peripheral-io/hello-world` | 6 |
| `sensor-starter` | `sensor` | `E1M-AEN801`, `E1M-V2N101` | `examples/peripheral-io/i2c-master` | 8 |
| `edge-ai-starter` | `edge-ai` | `E1M-AEN801`, `E1M-V2N101` | `examples/ai/cold-chain-monitor` | 8 |
| `board-diagnostics` | `diagnostics` | `E1M-AEN801`, `E1M-V2N101` | `examples/bringup/board-selftest` | 8 |
| `iot-starter` | `iot` | `E1M-AEN801` only (`status: preview`) | `examples/connectivity/mqtt-telemetry` | 7 |

Layout: `vendored/<sdk-template-id>/<sku>/<path>`, e.g.
`vendored/minimal/E1M-AEN801/CMakeLists.txt`. Four templates ship past the
common six: `edge-ai` adds `src/cold_chain.c` + `src/cold_chain.h` (the
cold-chain-metrics core the app links against); `sensor` and `diagnostics`
each add `boards/native_sim_native_64.{conf,overlay}` (tan-cli#501 — a
board-dir pair Zephyr auto-discovers with no CMakeLists wiring at all); and
`iot` adds `native_sim.conf` at the project root (tan-cli#379 — NOT
auto-discovered the same way: its own README build line passes it explicitly
via `-DEXTRA_CONF_FILE=native_sim.conf`; see the non-envelope-extras section
at the end).

`python/tan/core/scaffold.py::_vendored_files` reads these through the packaged
`tan.templates.VENDORED_ROOT`. Setuptools includes the tree in a wheel/source
install and the PyInstaller build includes it in the frozen distribution. The
reader:

- picks the SKU-family bucket (`E1M-V2N*`/`E1M-V2M*` -> the `E1M-V2N101`
  tree, everything else -> the `E1M-AEN801` tree, mirroring
  `app_core_for_sku`'s own family split) — except `iot`, which has only ONE
  tree (`E1M-AEN801`) and no family split at all (see "iot-starter is
  AEN-only" below);
- retargets `board.yaml`'s `som.sku:` line onto the caller's exact `--som`
  value when it isn't the tree's own vendored SKU (reusing the existing
  `retarget_board_yaml_som`, the same mechanism `init --from-example` already
  uses) — a byte-exact no-op for a tree's own vendored SKU, including a
  column-aligned trailing comment on the `sku:` line (`iot`'s board.yaml has
  one; `retarget_board_yaml_som` replaces only the value token, not the rest
  of the line);
- splices `--cores` companions (+ a default RPMsg channel to the first active
  one) into the vendored `cores:` block via `splice_companion_cores`, mirroring
  the retired Rust `gen_board_yaml` companion-core loop.

## Template-id mapping: resolved vs. deferred (maintainer decision)

`zephyr-app -> minimal`, `sensor-starter -> sensor`, `edge-ai-starter ->
edge-ai`, `board-diagnostics -> diagnostics`, and `iot-starter -> iot` are
mapped/vendored — five mappings confirmed clean, each template's existing
generator (`gen_zephyr_project_files`) already targeted (or was retired in
favor of) a real, west-buildable Zephyr layout structurally matching the
SDK's canonical scaffold (`find_package(Zephyr)` + `board.yaml` -> generated
Kconfig): `examples/peripheral-io/hello-world` for `minimal`,
`examples/peripheral-io/i2c-master` for `sensor`,
`examples/ai/cold-chain-monitor` for `edge-ai`,
`examples/bringup/board-selftest` for `diagnostics`, and
`examples/connectivity/mqtt-telemetry` for `iot`. `zephyr-app` is also the
one directly responsible for #864's motivating regression: the retired
CMakeLists.txt ran `--emit zephyr-conf` **without** `--core <id>`, which on a
heterogeneous (`--cores`) project lets one core's Kconfig leak into another
core's build. Every vendored template's CMakeLists.txt threads `--core <id>`
explicitly, closing it. `sensor-starter`'s and `board-diagnostics`'s
hand-written generators emitted a generic polling/checklist stub,
`edge-ai-starter`'s a generic arena-sizing stub, and `iot-starter`'s a
Wi-Fi/MQTT/TLS-toggle stub with no real transport; the vendored trees instead
emit the SDK's real TMP112 `<alp/chips/tmp112.h>` i2c-master example, BME280
cold-chain-monitor app, board self-test (SoM/SoC identity + RUN
operating-point + I2C management-bus scan), and CC3501E Wi-Fi6+BLE bridge
MQTT/TLS telemetry app, respectively.

`edge-ai-starter` was flagged (not vendored) through v0.11.1: the SDK's
`edge-ai` scaffold's `cores:` topology did **not** change with `--sku` back
then (see "Per-SKU substitution" below) — its `E1M-V2N101` render kept
`cores:` keyed on `m55_hp`/`a32_cluster`, the Alif-only pair, not the real
Renesas `m33_sm`/`a55_cluster`. alp-sdk#877 (the same fix that resolved
`zephyr-app`'s per-SKU core bug) fixed this for `edge-ai` too, so it is now
vendored at v0.13.0: `E1M-V2N101`'s tree correctly keys the app core on
`m33_sm` (companion `a55_cluster`, `os: "off"`). It is also the FIRST
HETEROGENEOUS (multi-core) vendored template — its `board.yaml` lists the
companion core BEFORE the app core, which is why
`python/tan/core/scaffold.py::vendored_app_core_key` reads the core that OWNS
an `app:` key rather than trusting
positional "first child under `cores:`" (correct for `minimal`/`sensor` only
because they are single-core).

`board-diagnostics` and `iot-starter` were flagged (not vendored) through
v0.13.0 because no SDK catalog template covered diagnostics/bring-up or
Wi-Fi/MQTT/TLS connectivity at all (`gateway` is Modbus, not IoT
connectivity). alp-sdk#903 added both catalog entries; they are vendored at
`0ed078a6d04f4072ab00d8ce92cb2ac82f7adcbc` (see "Source" above), retiring
`tan`'s hand-written generators for both templates — including
`iot-starter`'s `config/iot.env.example` file, which the vendored `iot`
scaffold doesn't emit (SSID/broker config lives in `src/main.c`, per the
scaffold's own "Customer workflow" comment).

### `iot-starter` is AEN-only

`iot`'s SDK catalog entry covers exactly ONE SoM SKU, `E1M-AEN801`
(`status: preview`; `supported.som_skus: ["E1M-AEN801"]`) — the Wi-Fi
transport is the CC3501E Wi-Fi6+BLE bridge, silicon-validated only on that
SKU; `E1M-V2N101`'s Murata Wi-Fi (`murata_lbee5hy2fy`) is `hil_silicon:
untested` with an unmerged Linux data path. Unlike every other mapped
template, there is no `_V2N` tree and no `_family_bucket` call for `iot`
(`python/tan/core/scaffold.py` uses `IOT_STARTER_SUPPORTED_SKU` directly).
`tan init`'s upfront guard (`python/tan/commands/init_cmd.py`) rejects any
`--som` other than `E1M-AEN801` for this template with `init.invalid-som`, naming the
supported SKU, before a single file is planned — never a silent fall-back
onto a hand-written generator, which would keep alive on this one path the
exact drift issue #14 retires everywhere else.

### `minimal-app` stays hand-generated; tan-cli#309 fixed its CMake AND its `app:`

**`minimal-app`** is semantically closest to SDK `minimal`, but it is not
vendored from the SDK catalog at all — it is tan's OWN generator
(`tan/core/scaffold.py`'s `_minimal_app_files`), the ONLY tan wizard template
left hand-generated after the vendoring pass above. Folding it onto the same
vendored tree as `zephyr-app` would make the two templates byte-identical in
the wizard's template picker (a product decision to merge/deprecate one, not
something to invent here), and `contract/envelopes/init-preview-minimal-app/
expected.json` pins its exact eight-file list/order — path + change-kind
only, never file content or `board.yaml`'s `app:` value, so it did not need
re-pinning for the fix below.

Through v0.5.0-rc3 this template had TWO compounding bugs, and an earlier pass
at this fix landed only the first — which, alone, turns a silent wrong-binary
build into a hard CMake configure error, not a working one (caught by
adversarial review before it shipped):

1. **Which file `west build` even reads.** `board.yaml`'s `app:` decides this,
   via the planner's `_zephyr_app_dir` (`tan/planner/orchestrator.py`): it
   resolves `app:` to a directory and picks that directory ITSELF whenever it
   holds a `CMakeLists.txt` of its own, falling back to the PARENT only when
   it does not. This template's `src/` deliberately keeps its own
   `CMakeLists.txt` (the two-file split below), so `app: ./src` sent `west
   build` straight at `src/CMakeLists.txt` — the root `CMakeLists.txt`
   (`project()` + `add_subdirectory(src)`, never `add_executable`) was dead
   code the entire time, not the file at fault.
2. **What that file said.** `src/CMakeLists.txt` — the file actually
   configured — called plain `add_executable(alp_app ${ALP_APP_SOURCES})`, no
   `find_package(Zephyr ...)` anywhere in either CMake file. CMake configures
   and links that shape fine: measured on a real checkout, `west build -b
   <board> <project>/src` produced a genuine PE32+ x86-64 `alp_app.exe` built
   from `CMakeFiles/alp_app.dir/{main,features/app_bootstrap}.obj` —
   `app_bootstrap.c` WAS compiled and linked, just into a host binary Zephyr's
   own build machinery never touched — so `tan build` reported success for a
   project that was never Zephyr at all.

tan-cli#309 fixed both, in `tan/core/scaffold.py`: `_minimal_app_root_cmake`/
`_minimal_app_src_cmake` now emit `find_package(Zephyr REQUIRED HINTS
$ENV{ZEPHYR_BASE})` before `project()` in the root file, with `src/
CMakeLists.txt` contributing via `target_sources(app PRIVATE ...)`/
`target_include_directories(app ...)` against Zephyr's own `app` target
instead of a second `add_executable` — the same KIND of CMake every vendored
tree above already writes, while keeping its own smaller, hand-generated
CONTENT; and `_minimal_app_board_yaml` now emits `app: .` (the project root)
instead of `./src`, so `_zephyr_app_dir` resolves straight to the root file
without ever consulting `src/`. Measured after both fixes, against a real
CMake + Ninja + Zephyr SDK: configure reaches Zephyr's own boilerplate
(`Loading Zephyr default modules`, board/toolchain/devicetree resolution) and
a full build compiles `src/features/app_bootstrap.c` into `app/libapp.a`
alongside `src/main.c`, which Zephyr's own link step pulls in whole
(`-Wl,--whole-archive app/libapp.a`) on the way to a real `zephyr.elf`.

**`crates/tan-core/src/wizard/service/c_project.rs` shipped the pre-#309
broken shape (both bugs) and was never fixed** — it was frozen, and tan-cli#269
deleted it; its own `wizard/vendored/MANIFEST.md` had
already flagged the CMake half of this ("a `board.yaml` declaring `os:
zephyr` over a plain-CMake tree is exactly the silent host-binary build that
issue [#14] reports") as "deliberately deferred, not a permanent gap" before
the freeze, and the freeze is why it never got un-deferred there. `minimal-app`
stays **not** the non-interactive `tan init` default — `zephyr-app` is
(tan-cli #97) — an independent choice (a vendored, real-catalog scaffold vs.
tan's own hand-generated stub) that #309 does not revisit.

**A correction to tan-cli#309's own "Measured" evidence.** Its table reports
`Machine: ARM`, a real `zephyr.elf`, `CMakeFiles/app.dir/src/main.c.obj`
present, and no `app_bootstrap*.obj` — but under the pre-#309 shape (`app:
./src` + `src/CMakeLists.txt`'s `add_executable(alp_app ...)`), the object
directory is named `alp_app.dir` (the target's own name), never `app.dir`,
and paths are relative to `src/` (already the CMake source root), so the real
artefact is `alp_app.dir/main.obj`, not `app.dir/src/main.c.obj` — confirmed
by reproducing that exact shape locally. `app.dir/src/main.c.obj` and a real
ARM `zephyr.elf` are what the SAME `E1M-AEN801` plan's OTHER Zephyr slice
(`m55_he`, which builds `${SDK_ROOT}/firmware/alp-stock-shim`, a genuine
Zephyr app with its own unrelated `src/main.c`) would produce. The evidence
table most likely mixed that slice's build output into the `minimal-app`
customer slice's (`m55_hp`) row — the underlying defect (silent wrong-binary
build) is real and independently reproduced above, but that one table
conflates two slices.

**`host-tooling-starter`** (a host-tool monorepo scaffold, not a
firmware/board.yaml project — categorically out of the scaffold catalog's
scope) is **retired entirely** (tan-cli#14): its `WizardTemplateId` variant,
generator (`c_project.rs`'s old `gen_host_tooling_files`), and registry entry
are gone, not just left unvendored.

Reverse gap (informational, no tan-side action): the SDK catalog also ships
`peripheral`, `multicore-rpmsg`, and `gateway`, none of which has a tan wizard
counterpart today.

## SKU-family gap: NXP is not in the SDK catalog

The SDK catalog's `supported.som_skus` for every mapped template EXCEPT `iot`
is exactly `["E1M-AEN801", "E1M-V2N101"]` — no `E1M-NX9*` SKU is covered by
anything in the catalog (`iot` narrows further still, to `["E1M-AEN801"]`
only — see "`iot-starter` is AEN-only" above). `app_core_for_sku` gives NX9
its own core id (`m33`, distinct from both vendored trees' `m55_hp`).

**tan-cli#579 settled the maintainer call this section used to leave open:
an NX9 `--som` is now REFUSED, not defaulted onto the Alif tree.** The
vendored lookup used to fall through to `E1M-AEN801` on the theory that
init-time output is best-effort and `tan validate` re-checks it. Measured,
that theory did not survive contact: `tan init --som E1M-NX9101 --template
sensor-starter` exited 0 with `issues: []` and wrote **five of six files
byte-identical to the Alif render** — a `CMakeLists.txt` still pinned to
`--emit zephyr-conf --core m55_hp` (contradicting the `m33` that
tan-cli#494's `retarget_board_yaml_cores` had just written into the
`board.yaml` beside it), a README telling an NXP customer to run `west build
-b alp_e1m_aen801_m55_hp/ae822fa0e5597ls0/rtss_hp .`, and `preset:
e1m-evk` / `chips: [tmp112]` describing another module's BOM. `tan validate`
cannot re-check content it has no opinion about, and the artefact is
committed by then.

So `tan/core/scaffold.py`'s `_SOM_FAMILIES` now carries `("E1M-NX9", "m33",
None)` and `_vendored_family` raises `UnsupportedSomError` →
`init.som-unsupported` (exit 2). `--template minimal-app` — tan's own
hand-generated, vendor-neutral tree, which reads nothing from here — still
scaffolds every SKU, and `--from-example` still copies a real SDK example, so
the refusal is not a dead end. **This retires the moment the SDK catalog
grows NX9 coverage: re-vendor the tree here and replace that `None` with its
directory name.** Vendoring one by hand instead is not an option —
everything under this directory is `--emit scaffold` output captured
byte-for-byte, and `tests/parity/scaffold_byte_parity.py` re-runs the live
emit against a reachable checkout and fails on drift.

## Per-SKU substitution (alp-sdk#864/#877) — not a two-line patch

Earlier (`75ef3b02`, tan-cli#25's original vendor point) `--sku` substituted
only the rendered `board.yaml`'s `som.sku:`/`preset:` lines, and the
`cores:` key stayed the template's own canonical SKU's core for every SKU —
for `minimal` that meant `m55_hp` (Alif) even when `--sku E1M-V2N101` was
requested, a non-buildable core id on Renesas silicon. alp-sdk#877 fixed
`--emit scaffold` to derive the real per-SKU app core (`app_core_for_sku`'s
SDK-side equivalent) and adapt every file that names it: `board.yaml`'s
`cores:` key, `CMakeLists.txt`'s `--core` flag, and `README.md`'s
"On real silicon" board-id line (now the fully-qualified Zephyr 4.4 form,
e.g. `alp_e1m_v2n101_m33_sm/r9a09g056n48gbg/cm33`). Confirmed for `minimal`:
`E1M-AEN801` and `E1M-V2N101` now differ in `board.yaml`, `CMakeLists.txt`,
and `README.md`; `prj.conf`, `src/main.c`, and `testcase.yaml` (not part of
the scaffold envelope, see below) stay byte-identical between the two.

The vendored `minimal`/`sensor`/`edge-ai`/`diagnostics` scaffolds' `cores:` key
is `m33_sm` for `E1M-V2N101`/`E1M-V2M101` and `m55_hp` for `E1M-AEN801`. `iot`
has no per-SKU variance; its app core is always `m55_hp`. The shipping Python
reader derives the `--cores` companion-splice target from each planned
`board.yaml` through `vendored_app_core_key`, rather than re-deriving it from
`app_core_for_sku`. `plan_template_files` reads and retargets the tree first;
`splice_companion_cores` then operates on those actual bytes. Unit tests in
`python/tests/core/test_scaffold.py` cover the family mapping, companion splice,
and heterogeneous app-owner selection, so a future re-vendor cannot silently
move the app core while the CLI keeps using an independent table.

### Heterogeneous `cores:` — `vendored_app_core_key` reads the `app:` owner

`edge-ai`'s `board.yaml` lists its companion core FIRST
(`a55_cluster:`/`a32_cluster:`, `os: "off"`, no `app:` key) and the real app
core SECOND (`m33_sm`/`m55_hp`, the one with `app: ./src`) — the reverse
insertion order every single-core `minimal`/`sensor` tree happens to share
with its app core being the only entry. `vendored_app_core_key` therefore
scans every child under `cores:`, tracking the current `  <core>:` key, and
returns whichever one owns a `    app:` line, instead of positionally
trusting the first indented child (which used to be correct only because
`minimal`/`sensor` are single-core). Python's
`test_vendored_app_core_key_skips_a_pre_declared_companion_listed_first` and
`test_splice_adds_a_companion_and_a_default_rpmsg_channel` are the regression
guards for the lookup and for where a `--cores` splice lands its IPC endpoint.

`testcase.yaml` and `iot`'s `native_sim.conf` are vendored alongside the
scaffold envelope but are **not** part of `--emit scaffold`'s output — the
catalog's `files.user_owned` for every mapped template is
`board.yaml`/`prj.conf`/`CMakeLists.txt`/`src/main.c`/`README.md` (plus, for
`edge-ai`, `src/cold_chain.c`/`src/cold_chain.h`) only. `native_sim.conf`
(tan-cli#379) is the same class and, for `iot`, the same PAIR:
`testcase.yaml`'s `extra_args: EXTRA_CONF_FILE=native_sim.conf` is what loads
it, so vendoring one without the other shipped a scaffold whose own twister
scenario and whose own documented `west build` named a file that did not
exist. Both are byte-diffed against the catalog example's own copy
(`scaffold_byte_parity.py`'s `NON_ENVELOPE_EXTRAS`), not against the emit.
`testcase.yaml` is a byte-exact copy of the canonical example's own
`testcase.yaml`
(`examples/peripheral-io/hello-world/testcase.yaml` for `minimal`,
`examples/peripheral-io/i2c-master/testcase.yaml` for `sensor`,
`examples/ai/cold-chain-monitor/testcase.yaml` for `edge-ai`,
`examples/bringup/board-selftest/testcase.yaml` for `diagnostics`,
`examples/connectivity/mqtt-telemetry/testcase.yaml` for `iot`) — the SDK's
twister harness for that example, family-independent like every other
non-`board.yaml` file. `tests/parity/scaffold_byte_parity.py` diffs it
against that example directory rather than the live scaffold emit.
