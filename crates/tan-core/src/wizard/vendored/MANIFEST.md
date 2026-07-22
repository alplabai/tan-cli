<!-- SPDX-License-Identifier: Apache-2.0 -->
# Vendored scaffold provenance (alp-sdk#864)

This tree is `alp-sdk --emit scaffold` output, captured byte-for-byte (LF, no
retouching) and checked in so `tan init`/`tan scaffold` can read it without
ever shelling the SDK. `tests/parity/scaffold_byte_parity.py` re-runs the live
emit against a reachable alp-sdk checkout and fails loudly if this tree drifts
from an un-revendored SDK change.

## Source

- Repo: `alplabai/alp-sdk`
- Branch: `feat/864-emit-scaffold`
- Commit: `75ef3b02` (`fix(templates): drop stale SoM comment on --emit
  scaffold sku substitution`)
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

## Two-line substitution only — not a full per-SKU re-render

`alp_template.render_to_envelope`'s docstring (alp-sdk `scripts/
alp_template.py`) is explicit: `--sku` substitutes only the rendered
`board.yaml`'s `som.sku:` and top-level `preset:` lines against the SKU's own
`metadata/e1m_modules/<sku>.yaml` `default_board:` — a byte-identical
passthrough when `sku` already matches the example's own default. Nothing
else in the tree (the `cores:` topology, the CMakeLists.txt `--core` flag,
`src/main.c`, `README.md`) changes with `--sku`. Confirmed for `minimal`: the
`E1M-AEN801` and `E1M-V2N101` vendored trees differ in `board.yaml` only (2
lines); every other file is byte-identical between the two.

One consequence worth flagging: the vendored `minimal` scaffold's `cores:`
key is `m55_hp` for **both** vendored SKUs — for `E1M-V2N101` this disagrees
with `app_core_for_sku("E1M-V2N101")` (`"m33_sm"`). `vendored.rs` sidesteps
this by deriving the splice target from the vendored `board.yaml`'s own
`cores:` key (`vendored_app_core_key`), not from `app_core_for_sku`, so
`--cores` companion-splicing stays correct regardless of SKU. `tan init`'s
own upfront `--cores` validation (`commands/init/mod.rs`, checked before the
plan is built) still calls `app_core_for_sku` and would therefore validate
against `"m33_sm"` for an `E1M-V2N101` `zephyr-app` request even though the
vendored plan's real core key is `m55_hp` — a pre-existing-shape
inconsistency this change does not newly introduce (no test exercises
`zephyr-app` + `--cores` today; `--cores` is undocumented) but a maintainer
should decide whether that CLI-level check should become vendored-aware too.
