<!-- SPDX-License-Identifier: Apache-2.0 -->
# Vendored scaffold provenance (alp-sdk#864)

This tree is `alp-sdk --emit scaffold` output, captured byte-for-byte (LF, no
retouching) and checked in so `tan init`/`tan scaffold` can read it without
ever shelling the SDK. `tests/parity/scaffold_byte_parity.py` re-runs the live
emit against a reachable alp-sdk checkout and fails loudly if this tree drifts
from an un-revendored SDK change.

## Source

- Repo: `alplabai/alp-sdk`
- Ref: `v0.13.0` (release tag — `git checkout v0.13.0` reproduces the exact
  pinned commit; `dev`'s tip does not)
- Commit: **v0.13.0 (`93ef5726`)** — `minimal` was re-vendored at this commit
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
- Command: `PYTHONPATH=$SDK/scripts python3 scripts/alp_project.py --emit
  scaffold --template <id> --sku <SKU>`

## Template x SKU matrix vendored

| tan `WizardTemplateId` | SDK catalog id | Vendored SKUs | Example dir | Files |
|---|---|---|---|---|
| `zephyr-app` | `minimal` | `E1M-AEN801`, `E1M-V2N101` | `examples/peripheral-io/hello-world` | 6 |
| `sensor-starter` | `sensor` | `E1M-AEN801`, `E1M-V2N101` | `examples/peripheral-io/i2c-master` | 6 |
| `edge-ai-starter` | `edge-ai` | `E1M-AEN801`, `E1M-V2N101` | `examples/ai/cold-chain-monitor` | 8 |

Layout: `vendored/<sdk-template-id>/<sku>/<path>`, e.g.
`vendored/minimal/E1M-AEN801/CMakeLists.txt`. `edge-ai` ships two extra files
over `minimal`/`sensor`'s six: `src/cold_chain.c` + `src/cold_chain.h` (the
cold-chain-metrics core the app links against).

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

`zephyr-app -> minimal`, `sensor-starter -> sensor`, and
`edge-ai-starter -> edge-ai` are mapped/vendored — the three mappings
confirmed clean, and the three templates whose existing generator
(`gen_zephyr_project_files`) already targeted (or, for `sensor-starter`/
`edge-ai-starter`, was retired in favor of) a real, west-buildable Zephyr
layout structurally matching the SDK's canonical scaffold
(`find_package(Zephyr)` + `board.yaml` -> generated Kconfig):
`examples/peripheral-io/hello-world` for `minimal`,
`examples/peripheral-io/i2c-master` for `sensor`,
`examples/ai/cold-chain-monitor` for `edge-ai`. `zephyr-app` is also the
one directly responsible for #864's motivating regression: the retired
CMakeLists.txt ran `--emit zephyr-conf` **without** `--core <id>`, which on a
heterogeneous (`--cores`) project lets one core's Kconfig leak into another
core's build. The vendored CMakeLists.txt threads `--core <id>` explicitly,
closing it — `sensor-starter`'s and `edge-ai-starter`'s vendored
CMakeLists.txt do the same. `sensor-starter`'s hand-written generator emitted
a generic sensor-polling stub, and `edge-ai-starter`'s a generic
arena-sizing stub; the vendored trees instead emit the SDK's real TMP112
`<alp/chips/tmp112.h>` i2c-master example and BME280 cold-chain-monitor app,
respectively.

`edge-ai-starter` was flagged (not vendored) through v0.11.1: the SDK's
`edge-ai` scaffold's `cores:` topology did **not** change with `--sku` back
then (see "Per-SKU substitution" below) — its `E1M-V2N101` render kept
`cores:` keyed on `m55_hp`/`a32_cluster`, the Alif-only pair, not the real
Renesas `m33_sm`/`a55_cluster`. alp-sdk#877 (the same fix that resolved
`zephyr-app`'s per-SKU core bug) fixed this for `edge-ai` too, so it is now
vendored at v0.13.0: `E1M-V2N101`'s tree correctly keys the app core on
`m33_sm` (companion `a55_cluster`, `os: "off"`). It is also the FIRST
HETEROGENEOUS (multi-core) vendored template — its `board.yaml` lists the
companion core BEFORE the app core, which is why `vendored_app_core_key`
(`vendored.rs`) reads the core that OWNS an `app:` key rather than trusting
positional "first child under `cores:`" (correct for `minimal`/`sensor` only
because they are single-core).

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

The vendored `minimal`/`sensor`/`edge-ai` scaffolds' `cores:` key is `m33_sm`
for `E1M-V2N101`/`E1M-V2M101` and `m55_hp` for `E1M-AEN801` — agreeing with
`app_core_for_sku`. `vendored.rs` still derives the `--cores` companion-splice
target from EACH template's OWN vendored `board.yaml` `cores:` key
(`vendored_app_core_key`/`vendored_app_core_for_sku`/
`vendored_sensor_app_core_for_sku`/`vendored_edge_ai_app_core_for_sku`)
rather than calling `app_core_for_sku` directly, so a *future* re-vendor that
(again) derives a different core for a vendored SKU fails `cargo test`
(`vendored_app_core_matches_each_familys_board_yaml`/
`vendored_sensor_app_core_matches_each_familys_board_yaml`/
`vendored_edge_ai_app_core_matches_each_familys_board_yaml`) instead of
silently drifting. `tan init`'s upfront `--cores` validation
(`commands/init/resolve.rs`'s `app_core_for_template`) now calls
`vendored_app_core_for_sku`/`vendored_sensor_app_core_for_sku`/
`vendored_edge_ai_app_core_for_sku` for the `zephyr-app`/`sensor-starter`/
`edge-ai-starter` templates specifically (every other template still uses
`app_core_for_sku`, matching its own hand-written `gen_board_yaml`), so the
CLI-level check can never again independently disagree with what
`create_wizard_plan_with_cores` actually plans.

### Heterogeneous `cores:` — `vendored_app_core_key` reads the `app:` owner

`edge-ai`'s `board.yaml` lists its companion core FIRST
(`a55_cluster:`/`a32_cluster:`, `os: "off"`, no `app:` key) and the real app
core SECOND (`m33_sm`/`m55_hp`, the one with `app: ./src`) — the reverse
insertion order every single-core `minimal`/`sensor` tree happens to share
with its app core being the only entry. `vendored_app_core_key` therefore
scans every child under `cores:`, tracking the current `  <core>:` key, and
returns whichever one owns a `    app:` line, instead of positionally
trusting the first indented child (which used to be correct only because
`minimal`/`sensor` are single-core). Two `vendored.rs` unit tests are the
regression guards for this: `vendored_app_core_key_finds_the_app_core_not_the_first_listed_core`
and `edge_ai_cores_splice_targets_the_real_app_core_not_the_companion` both
assert the edge-ai V2N tree resolves to `m33_sm`, not `a55_cluster` — for the
raw key lookup and for where a `--cores` splice lands its IPC endpoint,
respectively.

`testcase.yaml` is vendored alongside the scaffold envelope but is **not**
part of `--emit scaffold`'s output — the catalog's `files.user_owned` for
`minimal`, `sensor`, and `edge-ai` is `board.yaml`/`prj.conf`/
`CMakeLists.txt`/`src/main.c`/`README.md` (plus, for `edge-ai`,
`src/cold_chain.c`/`src/cold_chain.h`) only. It's a byte-exact copy of the
canonical example's own `testcase.yaml`
(`examples/peripheral-io/hello-world/testcase.yaml` for `minimal`,
`examples/peripheral-io/i2c-master/testcase.yaml` for `sensor`,
`examples/ai/cold-chain-monitor/testcase.yaml` for `edge-ai`) — the SDK's
twister harness for that example, family-independent like every other
non-`board.yaml` file. `tests/parity/scaffold_byte_parity.py` diffs it
against that example directory rather than the live scaffold emit.
