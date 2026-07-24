<!-- SPDX-License-Identifier: Apache-2.0 -->
# ADR-0020 parity gate

`tan` (this repo) is the sole executor for the Alp Lab build (ADR-0020, end-state
B). alp-sdk's planner is the fast-moving half of that split, so a planner
change that emits fine but builds wrong must be caught before it reaches a
release, not discovered on a bench. This directory seeds the gate ADR-0020's
2026-07-20 Amendment (alp-sdk#855) says is release-blocking: a **two-seam
parity gate** plus a **cross-repo trigger** so alp-sdk CI can drive it on
every planner change.

## The two seams

| Seam | Checks | Status |
|---|---|---|
| **1 — plan shape** | Does a live `--emit build-plan` still match a frozen, hand-verified oracle's command / env / appDir / skip-fail-decision *shape*, field for field, over the SoM matrix? Deliberately does NOT re-diff the materialised config-artefact content (alp.conf/local.conf/cmake-args.txt/sysbuild-conf bytes) — see "Seam-1 scope" below. Toolchain-free; runs on any `ubuntu-latest` runner. | **Implemented here**: `seam1_field_diff.py` + `.github/workflows/parity.yml`'s `seam1-plan-shape` job. |
| **2 — real build** | Materialise byte-check, an actual `west`/Zephyr build off the plan, and a Renode smoke test — the thing seam 1 can't catch (a plan that *looks* right but doesn't build). | **Follow-up, not seeded here.** Needs a Linux runner with the Zephyr SDK / toolchain installed (`west`, the AEN/E1M-X Zephyr modules, Renode). Placeholder `seam2` job in `.github/workflows/parity.yml` documents this — it does not run a fake check and does not report success for work it didn't do. |

Yocto/A-core artefact parity is explicitly **out of scope** for both seams —
no bitbake-capable runner infra exists, and bitbake output isn't
byte-reproducible (ADR-0020, "Phase-3 parity gate").

## Why the oracle is frozen at `97ad481b`

`tests/parity/oracle/*.build-plan.json` were captured at alp-sdk commit
`df312cec^` == `97ad481b` ("feat(build-plan): publish envAppendPath +
executionPolicy (ADR-0020 Phase 1, additive)", #847).

That specific SHA matters: it is the **last** alp-sdk commit that carries
*both*

- `Orchestrator.fan_out()` — the in-repo SDK-side executor, which until
  `df312cec` (#848, "retire the SDK executor, tan is sole executor") was
  still alive and usable as a real, in-repo build oracle; and
- the Phase-1 contract fields `tan` depends on today — per-slice
  `envAppendPath` and the top-level `executionPolicy`.

`df312cec` retires `fan_out` and every SDK-side executor outright (no legacy
shim survives that migration — ADR-0020 is explicit that there is no
rollback after Phase 4). After that commit there is no SDK-side executor left
to diff a live emit against, ever again. `97ad481b` is therefore the last
frame in which "does the live emit still match what the last real in-repo
oracle produced" is even an answerable question — freezing it now is
reconstructing that oracle retroactively, per the Amendment's remediation
step, not a routine fixture update.

## Seam-1 scope: shape only, not config-artefact content (alp-sdk#874 retune)

Seam-1 used to also byte-diff each slice's materialised config-artefact
`contents` (the rendered `alp.conf`/`local.conf`/`cmake-args.txt`) and
`sharedArtefacts[*].contents` (DTS overlays, `system_ipc.h`, sysbuild/TF-M
conf) against the frozen oracle. That turned *every* intentional emitter
content change (a Kconfig dependency-gating fix, a new peripheral default)
into a seam-1 failure that could only be "resolved" by adding another
hand-reviewed strip to `normalize_plan` — a per-change treadmill that eroded
the gate instead of doing its stated job (plan *shape* parity). alp-sdk#874's
follow-up retuned the comparator: `normalize_plan` now drops every
artefact's `contents` (`_drop_artefact_contents`), keeping only its `path`
in the shape check. Command, env, appDir, skip/fail decision, and
`debug.probe` are unaffected — see the sections below.

Content coverage does not go away — it lives on the alp-sdk side, in
`tests/fixtures/emit-snapshots/*.{build-plan,zephyr-conf}.snap`
(`scripts/check_emit_snapshots.py`), and eventually in seam-2's real build.
This vendored twin has no equivalent snapshot mechanism of its own (it only
ever compared against alp-sdk's own live emit), so it has nothing further to
add here — this section exists only so the two comparators' scope statement
stays in lockstep.

## Why the comparator normalizes (or drops) three fields

A `build-plan.json` is **not hermetic** — it embeds facts about the checkout
that emitted it, not just the board it plans for:

- **the checkout-root absolute path**, in `env.ALP_SDK_ROOT`, every
  `envAppendPath.*` entry, each slice's `appDir`, and (for sysbuild slices)
  embedded mid-string inside `command.args` (`-DSB_CONF_FILE=<root>/a;<root>/b`,
  which isn't a root-prefixed string outright — the comparator does a global
  substring replace, not a prefix check, to catch this);
- **`sdkCommit`**, the emitting commit's short SHA;
- **`sdkVersion`**, the emitting checkout's SDK release version.

None is a real parity break — they differ by construction between the
oracle's `97ad481b` capture checkout and whatever checkout is emitting the
live plan under test. `seam1_field_diff.py` normalizes the first two before
diffing: the checkout root (discovered from the plan's own
`slices[0].env.ALP_SDK_ROOT` — nothing is hardcoded) is replaced everywhere
with the literal token `__SDKROOT__`, and `sdkCommit` is replaced with
`__SHA__`. `sdkVersion` is dropped entirely instead of tokenized (mirrors
alp-sdk's own comparator fix, alp-sdk#883): unlike `sdkCommit`, whose oracle
value stays pinned to `97ad481b` forever, `sdkVersion` bumps on every
version-bump PR with zero shape change, so there is no stable token to
normalize it to.

## The one allowed delta: `debug.probe`

After normalization, the **only** field allowed to differ between the oracle
and a live emit is `slices[*].debug.probe`, and only in the direction
`"openocd"` (oracle, captured at `97ad481b`) `->` `null` (`df312cec` and
later). This is `#848`'s intentional, hand-reviewed change: the SDK-side
executor named a concrete debug-probe runner because it drove `west`/OpenOCD
itself; post-ADR-0020 the SDK doesn't own flashing at all (`tan` does), so
asserting `probe: "openocd"` would be a claim the SDK can no longer honestly
make. `null` means "the SDK isn't naming a probe" — a downgrade to
not-claiming, not a hidden capability loss. ADR-0020's Amendment states this
explicitly: "the only `97ad481b`<->`df312cec` emit delta is `debug.probe`
`"openocd"->null`, hand-reviewed."

Any other diff in the plan's SHAPE — a changed command, a changed `env`
value, a changed slice count, a `probe` change to anything other than that
exact transition — **fails** the gate. Config-artefact `contents` is dropped
before the diff runs at all (see "Seam-1 scope" above), so a content-only
change never reaches this allow-list in the first place. See
`seam1_field_diff.py`'s module docstring for the exact rule the comparator
implements.

## Running it locally

```
python3 tests/parity/seam1_field_diff.py \
  --sdk /path/to/an/alp-sdk/checkout \
  --oracle tests/parity/oracle
```

`--boards` restricts the check to specific oracle fixtures (filename minus
`.build-plan.json`, e.g. `--boards audio_i2s-tone multicore_rpmsg-v2n`);
omitted, it checks every fixture in `--oracle`. Exit code is `0` iff every
board's only diffs (if any) are the allowed `debug.probe` delta.

## CI wiring

`.github/workflows/parity.yml` runs `seam1-plan-shape` on every pull request
(against a pinned alp-sdk tag — see the workflow's `PINNED_SDK_TAG` comment)
and on a `repository_dispatch` of type `alp-sdk-planner-change` (the
cross-repo trigger ADR-0020's Amendment requires: alp-sdk CI fires this on
every planner change so a drifting emit surfaces on the *alp-sdk* PR, not
discovered later against a stale checkout). The dispatch payload's
`client_payload.sdk_ref` picks the exact SDK ref under test.

## Scaffold byte-parity (alp-sdk#864)

`scaffold_byte_parity.py` is the equivalent gate for the wizard's *vendored*
templates (`crates/tan-core/src/wizard/vendored/`, see that directory's
`MANIFEST.md`): for every vendored (template, sku) pair it re-runs a live
`alp_project.py --emit scaffold` and asserts byte-identity against the
vendored tree, so a future SDK scaffold change that isn't re-vendored fails
loudly instead of silently drifting (the same class of drift seam 1 guards
against for build-plans). Unlike `seam1_field_diff.py`, it is optionally
self-skipping — no reachable alp-sdk checkout (`--sdk` / `$ALP_SDK_ROOT` / a
sibling `alp-sdk` checkout) is a clean no-op, not a failure, since
`tan-core`'s own `cargo test` already covers the vendored tree's internal
consistency. Wired into `.github/workflows/parity.yml` as a `scaffold byte-parity`
step inside the `seam1-plan-shape` job — it reuses that job's pinned alp-sdk
checkout + emit deps (same as `kconfig_fixture_parity.py`) rather than cloning a
second time, so a re-vendor that drifts from the pinned SDK's `--emit scaffold`
fails CI instead of shipping silently.

```
python3 tests/parity/scaffold_byte_parity.py --sdk /path/to/an/alp-sdk/checkout
```

## Kconfig fixture byte-parity (alp-sdk#893/#894/#897)

`kconfig_fixture_parity.py` guards the same class of drift for `tan
kconfig`'s field contract (`crates/tan-core/src/kconfig.rs`): both that
crate and `crates/tan-cli/src/commands/kconfig.rs` `include_str!` a vendored
byte-copy of alp-sdk's canonical `--emit kconfig` contract anchor at
`tests/fixtures/kconfig-contract/emit-kconfig.golden.json` (same relative
path in both repos) so `parse_kconfig`/`Envelope<KconfigData>` can be tested
against the SDK's real field shape without a Zephyr/west workspace. This
script byte-diffs the vendored copy against the pinned alp-sdk checkout's
own copy — wired into `seam1-plan-shape` (it reuses that job's `alp-sdk`
checkout rather than cloning a second time). Like `scaffold_byte_parity.py`
it self-skips with no reachable alp-sdk checkout (`tan-core`'s own `cargo
test` already covers the vendored copy's internal consistency); unlike it,
a fixture simply absent at the *pinned* ref is ALSO not a fail (see
`PINNED_SDK_TAG`'s comment in `.github/workflows/parity.yml` and the
script's own docstring) — that branch only fires for a pin predating
alp-sdk#897 landing this fixture; `PINNED_SDK_TAG` is now `v0.13.0`
(past #897), so the gate byte-diffs for real. A byte MISMATCH (fixture
present upstream, content differs) always fails.

```
python3 tests/parity/kconfig_fixture_parity.py --sdk /path/to/an/alp-sdk/checkout
```
