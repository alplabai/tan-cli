<!-- SPDX-License-Identifier: Apache-2.0 -->
# Vendored scaffold provenance (alp-sdk#864)

This tree is `alp-sdk --emit scaffold` output, captured byte-for-byte (LF, no
retouching) and checked in so `tan init`/`tan scaffold` can read it without
ever shelling the SDK. `tests/parity/scaffold_byte_parity.py` re-runs the live
emit against a reachable alp-sdk checkout and fails loudly if this tree drifts
from an un-revendored SDK change.

## Source

- Repo: `alplabai/alp-sdk`
- Branch: `dev`
- Commit: `a0849e10` (`feat(build-plan): --emit scaffold derives cores per
  SKU + adapts scaffold content (#864) (#877)`) — re-vendored from this
  commit (tan-cli#25 had vendored `75ef3b02`, before #877 fixed `--emit
  scaffold` deriving the wrong, non-buildable Alif `m55_hp` core for every
  SKU including `E1M-V2N101`; see "App-core disagreement" below).
- Command: `PYTHONPATH=$SDK/scripts python3 scripts/alp_project.py --emit
  scaffold --template <id> --sku <SKU>`

## Template x SKU matrix vendored

| tan `WizardTemplateId` | SDK catalog id | Vendored SKUs |
|---|---|---|
| `zephyr-app` | `minimal` | `E1M-AEN801`, `E1M-V2N101` |

Layout: `vendored/<sdk-template-id>/<sku>/<path>`, e.g.
`vendored/minimal/E1M-AEN801/CMakeLists.txt`.

`crates/tan-core/src/wizard/service/vendored.rs` reads these via
`include_str!` (baked into the binary at compile time — no filesystem read at
`tan init` runtime) and:

- picks the SKU-family bucket (`E1M-V2N*`/`E1M-V2M*` -> the `E1M-V2N101`
  tree, everything else -> the `E1M-AEN801` tree, mirroring
  `app_core_for_sku`'s own family split);
- retargets `board.yaml`'s `som.sku:` line onto the caller's exact `--som`
  value when it isn't the tree's own vendored SKU (reusing the existing
  `retarget_board_yaml_som`, the same mechanism `init --from-example` already
  uses) — a byte-exact no-op for the two vendored SKUs themselves;
- splices `--cores` companions (+ a default RPMsg channel to the first active
  one) into the vendored `cores:` block, mirroring the retired
  `gen_board_yaml`'s companion-core loop.

## Template-id mapping: resolved vs. flagged (maintainer decision)

Only `zephyr-app -> minimal` is mapped/vendored in this change — the one
mapping the task explicitly confirmed as clean, and the one template whose
existing generator (`gen_zephyr_project_files`) already targeted a real,
west-buildable Zephyr layout structurally matching the SDK's canonical
`examples/peripheral-io/hello-world` scaffold (`find_package(Zephyr)` +
`board.yaml` -> generated Kconfig). It is also the one directly responsible
for #864's motivating regression: the retired CMakeLists.txt ran
`--emit zephyr-conf` **without** `--core <id>`, which on a heterogeneous
(`--cores`) project lets one core's Kconfig leak into another core's build.
The vendored CMakeLists.txt threads `--core m55_hp` explicitly, closing it.

Every other tan wizard template is **left on its existing hand-written
generator, unchanged** — each has a real gap against the SDK catalog rather
than a clean 1:1:

- **`minimal-app`** — semantically closest to SDK `minimal`, but its
  generator emits a plain-CMake, non-west-buildable stub (`include/app/
  app.h` + `src/CMakeLists.txt`), a structurally different shape than the
  SDK's canonical Zephyr scaffold. Folding it onto the same vendored tree as
  `zephyr-app` would make the two templates byte-identical in the wizard's
  template picker — a product decision (merge/deprecate one), not something
  to invent here.
- **`sensor-starter`** — same plain-CMake-shape gap against SDK `sensor`
  (which is a real TMP112 `<alp/chips/tmp112.h>` driver app, not a generic
  polling stub).
- **`edge-ai-starter`** — same plain-CMake-shape gap against SDK `edge-ai`
  (a concrete BME280 cold-chain-monitor app, not a generic arena-sizing
  stub). Also: the SDK's `edge-ai` scaffold's `cores:` topology does **not**
  change with `--sku` (see "Two-line substitution only" below) — its
  `E1M-V2N101` render still keys `cores:` on `m55_hp`/`a32_cluster`, not
  `m33_sm`. Vendoring it as-is would silently break the existing
  `app_core_for_sku`-driven core-consistency assumptions tan's init-time
  `--cores` validation relies on; flagging rather than papering over it.
- **`iot-starter`** — no SDK catalog template covers Wi-Fi/MQTT/TLS at all
  (`gateway` is Modbus, not IoT connectivity).
- **`board-diagnostics`** — no SDK catalog template is diagnostics/bring-up
  flavored.
- **`host-tooling-starter`** — categorically different (a host-tool monorepo
  scaffold, not a firmware/board.yaml project); out of the scaffold
  catalog's scope entirely.

Reverse gap (informational, no tan-side action): the SDK catalog also ships
`peripheral`, `multicore-rpmsg`, and `gateway`, none of which has a tan wizard
counterpart today.

## SKU-family gap: NXP is not in the SDK catalog

The SDK catalog's `supported.som_skus` for every template today is exactly
`["E1M-AEN801", "E1M-V2N101"]` — no `E1M-NX9*` SKU is covered by anything in
the catalog. `app_core_for_sku` gives NX9 its own core id (`m33`, distinct
from both vendored trees' `m55_hp`). The vendored lookup defaults an
unrecognized family (NX9 included) to the `E1M-AEN801` tree rather than
inventing NX9-specific content or erroring — consistent with the existing
`tan init` philosophy that init-time output for a SoM the SDK hasn't resolved
yet is best-effort and re-checked by `tan validate` once an SDK is available.
Whether tan should keep a permanent non-vendored fallback generator for NX9,
or whether the SDK catalog should grow NX9 coverage, is a maintainer call.

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

The vendored `minimal` scaffold's `cores:` key is now `m33_sm` for
`E1M-V2N101`/`E1M-V2M101` and `m55_hp` for `E1M-AEN801` — agreeing with
`app_core_for_sku`. `vendored.rs` still derives the `--cores` companion-splice
target from the vendored `board.yaml`'s own `cores:` key
(`vendored_app_core_key`/`vendored_app_core_for_sku`) rather than calling
`app_core_for_sku` directly, so a *future* re-vendor that (again) derives a
different core for a vendored SKU fails `cargo test`
(`vendored_app_core_matches_each_familys_board_yaml`) instead of silently
drifting. `tan init`'s upfront `--cores` validation
(`commands/init/mod.rs`) now calls `vendored_app_core_for_sku` for the
`zephyr-app` template specifically (every other template still uses
`app_core_for_sku`, matching its own hand-written `gen_board_yaml`), so the
CLI-level check can never again independently disagree with what
`create_wizard_plan_with_cores` actually plans.

`testcase.yaml` is vendored alongside the scaffold envelope but is **not**
part of `--emit scaffold`'s output — the catalog's `files.user_owned` for
`minimal` is `board.yaml`/`prj.conf`/`CMakeLists.txt`/`src/main.c`/
`README.md` only. It's a byte-exact copy of the canonical example's own
`examples/peripheral-io/hello-world/testcase.yaml` (the SDK's twister
harness for that example), family-independent like every other
non-`board.yaml` file. `tests/parity/scaffold_byte_parity.py` diffs it
against that example directory rather than the live scaffold emit.
