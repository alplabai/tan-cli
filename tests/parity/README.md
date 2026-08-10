<!-- SPDX-License-Identifier: Apache-2.0 -->
# ADR-0020 parity gate

`tan` (this repo) is the sole planner/executor shipped to users. Its relocated
planner under `python/tan/planner/` is checked against alp-sdk's original,
fast-moving planner source, so a producer change that emits fine but builds
wrong must be caught before it reaches a release, not discovered on a bench.
The build-plan remains the internal planner/executor seam. This directory seeds
the gate ADR-0020's
2026-07-20 Amendment (alp-sdk#855) says is release-blocking: a **two-seam
parity gate** plus a **cross-repo trigger** so alp-sdk CI can drive it on
every planner change.

## The two seams

| Seam | Checks | Status |
|---|---|---|
| **1 — plan shape** | Does a live `--emit build-plan` still match a frozen, hand-verified oracle's command / env / appDir / skip-fail-decision *shape*, field for field, over the SoM matrix? Deliberately does NOT re-diff the materialised config-artefact content (alp.conf/local.conf/cmake-args.txt/sysbuild-conf bytes) — see "Seam-1 scope" below. Toolchain-free; runs on any `ubuntu-latest` runner. | **Implemented here**: `seam1_field_diff.py` + `.github/workflows/parity.yml`'s `seam1-plan-shape` job. |
| **2 — real build** | Materialise the plan, run an actual build off it, and Renode-smoke the artefact — the thing seam 1 can't catch (a plan that *looks* right but doesn't build, or an executor that mishandles a correct plan). | **Implemented.** `seam2` in `.github/workflows/parity.yml`. The old "needs a Linux runner with the toolchain installed" blocker expired: alp-sdk#976 showed the Zephyr SDK and Renode v1.16.1 both install in-job on stock `ubuntu-latest`, and that recipe is reused here pins and all. Three hard gates — `tan build --materialise` with a non-empty `data.written`; `tan build --native` (deliberately **not** plain `west`, which would test Zephyr and assert nothing about the seam); then the ELF must exist, `file` must call it ARM, and Renode must boot it under alp-sdk#974's three assertions. |

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

`.github/workflows/parity.yml` runs **both** `seam1-plan-shape` and `seam2` on
every pull request (against a pinned alp-sdk ref — see the workflow's
`PINNED_SDK_TAG` comment) and on a `repository_dispatch` of type
`alp-sdk-planner-change` — the cross-repo trigger ADR-0020's Amendment
requires. alp-sdk fires it on every push touching its contract surface, and
`client_payload.sdk_ref` picks the exact SDK ref under test, overriding the pin
for that run.

Both jobs run on a dispatch, not just seam 1, and seam 2 is the reason the
trigger is worth having: alp-sdk's own `parity-seam1.yml` diffs alp-sdk's emit
against alp-sdk's OWN frozen oracle and never runs tan, so "alp-sdk changed and
tan can no longer EXECUTE its plan" is invisible on that side. A pinned PR run
here cannot see it either, until someone bumps the pin by hand.

**The result surfaces in tan's Actions, not on the alp-sdk PR.** Posting a
commit status back to alp-sdk would need a credential for alp-sdk inside this
repo, and the only one available is the same GitHub App whose private key would
then have to live in a second place; that widening is not worth it today.
`parity.yml`'s own pin-freshness warning covers the other direction — it says
when `PINNED_SDK_TAG` has rotted behind alp-sdk's contract surface — and is
skipped on dispatch runs, which deliberately ignore the pin.

## Scaffold byte-parity (alp-sdk#864)

`scaffold_byte_parity.py` is the equivalent gate for the *vendored* scaffold
templates: for every vendored (template, sku) pair it re-runs a live
`alp_project.py --emit scaffold` and asserts byte-identity against the
vendored tree, so a future SDK scaffold change that isn't re-vendored fails
loudly instead of silently drifting (the same class of drift seam 1 guards
against for build-plans).

**It defaults to `python/tan/templates/vendored/` — the tree the shipped
binary actually reads.** That is the point of the default: this gate measures
a surface that tracks `PINNED_SDK_TAG`, so it must point at the tree that
moves with the pin. The Rust wizard kept a second copy, frozen at its own
v0.14.0 vendor point with its own SDK-free `cargo test`; tan-cli#269 deleted
it with `crates/`, so `python/tan/templates/vendored/` is now the only
vendored scaffold tree in the repo. `--vendored <path>` still points this
script at an arbitrary tree by hand.

Unlike `seam1_field_diff.py`, it is optionally self-skipping — no reachable
alp-sdk checkout (`--sdk` / `$ALP_SDK_ROOT` / a sibling `alp-sdk` checkout) is
a clean no-op, not a failure. Wired into `.github/workflows/parity.yml` as a
`scaffold byte-parity` step inside the `seam1-plan-shape` job — it reuses that
job's pinned alp-sdk checkout + emit deps (same as `kconfig_fixture_parity.py`)
rather than cloning a second time, so a re-vendor that drifts from the pinned
SDK's `--emit scaffold` fails CI instead of shipping silently.

```
python3 tests/parity/scaffold_byte_parity.py --sdk /path/to/an/alp-sdk/checkout
```

## Kconfig fixture byte-parity (alp-sdk#893/#894/#897)

`kconfig_fixture_parity.py` guards the same class of drift for `tan
kconfig`'s field contract. The shipping tests in
`python/tests/commands/test_kconfig_command.py` consumes a vendored byte-copy
of alp-sdk's canonical `--emit kconfig` contract anchor at
`tests/fixtures/kconfig-contract/emit-kconfig.golden.json` (same relative
path in both repos) so the parse/envelope path can be tested
against the SDK's real field shape without a Zephyr/west workspace. (The
retired Rust oracle `include_str!`d the same file from two modules.) This
script byte-diffs the vendored copy against the pinned alp-sdk checkout's
own copy — wired into `seam1-plan-shape` (it reuses that job's `alp-sdk`
checkout rather than cloning a second time). Like `scaffold_byte_parity.py`
it self-skips with no reachable alp-sdk checkout
(`test_kconfig_command.py` already covers the vendored copy's internal
consistency); unlike it,
a fixture simply absent at the *pinned* ref is ALSO not a fail (see
`PINNED_SDK_TAG`'s comment in `.github/workflows/parity.yml` and the
script's own docstring) — that branch only fires for a pin predating
alp-sdk#897 landing this fixture; the current pin is past #897, so the gate
byte-diffs for real. A byte MISMATCH (fixture present upstream, content
differs) always fails.

```
python3 tests/parity/kconfig_fixture_parity.py --sdk /path/to/an/alp-sdk/checkout
```

## Bootstrap manifest byte-parity (alp-sdk#917)

`bootstrap_manifest_parity.py` guards the same class of drift for `tan
bootstrap`'s workspace-assembly FACTS. alp-sdk's `metadata/bootstrap.json` is
the single source of truth the two bootstrap scripts and `tan` all read (the
Zephyr pin, venv layout, prerequisites + Python floor, the `west` pip spec and
argv, pip package sets, the `env` map, the per-OS native-lib hints); its own
`_comment` names tan as a real reader of those facts since tan-cli PR #55.
alp-sdk polices its *scripts*
against it with `scripts/check_bootstrap_manifest.py` — but that gate cannot
see a tan-cli checkout, so nothing upstream catches this repo's own copy
drifting from the producer. This
script byte-diffs the vendored copy at
`contract/fixtures/bootstrap/manifest.json` (which the consumer and its tests
`include_str!`) against the pinned checkout's `metadata/bootstrap.json`. The
relative paths deliberately differ between the repos — it is a test fixture
here, SDK metadata there.

A failure here has a follow-on:
`python/tests/commands/test_bootstrap_command.py::test_the_fallback_constants_match_the_real_manifest_field_for_field`
asserts the hand-ported fallback constants (the path taken against any SDK
predating #917) equal this fixture field-for-field, so re-vendoring a changed
manifest fails that test until the constants are updated too. Until
tan-cli#269 a `cargo test` in `manifest.rs` asserted the same thing on the
Rust side; the Python case is now the only one.

**This script is not a CI gate, but its verdict is actionable again.** It was
removed from `.github/workflows/parity.yml` when the pin moved to v0.15.0-rc1,
on the reasoning that it measured a frozen fixture against a moving pin — a
comparison that can only ever fail. That reasoning depended on
`contract/fixtures/bootstrap/manifest.json` being frozen at the v0.14.0 vendor
point, and it no longer is: the fixture went stale enough to tell a customer,
mid-onboarding, to run `tan sdk switch` — a subcommand this build REFUSES
(`sdk_cmd.NOT_PORTED_SDK_SUBCOMMANDS`) — and tan-cli#585 re-vendored it at
`PINNED_SDK_TAG`. Per `docs/ROADMAP.md`'s Standing Rules `contract/` is live
shared API data, edited when the Python consumer requires it; only `crates/`
was ever the frozen tree, and tan-cli#269 deleted that.

The fixture's real gate is
`python/tests/commands/test_bootstrap_command.py::test_the_fallback_constants_match_the_real_manifest_field_for_field`,
which asserts the hand-ported fallback constants equal the fixture
field-for-field — every field, with no exemption, since #585. (Its Rust twin
in `manifest.rs` went with `crates/` in tan-cli#269.) A sibling case,
`test_no_instruction_in_the_vendored_manifest_names_a_refused_subcommand`,
stops a future re-vendor bringing back guidance for a refused subcommand.

There is no shipped Python vendored copy for this to guard: `tan/core/
bootstrap.py` reads `metadata/bootstrap.json` LIVE off the bound SDK root, so
nothing in the released artefact can drift from it.

The script is kept as a manual tool, not deleted. Pointed at `--sdk` =
`PINNED_SDK_TAG` a `DIFFERS` IS a re-vendor prompt: copy upstream over the
fixture, then update the fallback constants the field-for-field test holds
against it. Pointed at any other ref it only reports how far that ref has
moved.

```
python3 tests/parity/bootstrap_manifest_parity.py --sdk /path/to/an/alp-sdk/checkout
```

## Toolchain lock byte-parity (tan-cli#172)

`toolchain_lock_parity.py` guards the same class of drift for the Zephyr SDK
version `tan doctor`'s `zephyrSdk` check names in its `west sdk install
--version <..>` remedy (tan-cli#160). alp-sdk's `metadata/toolchains.json`
(issue #949 item 3) is the single source of truth for that pin, policed on
the alp-sdk side by `scripts/check_toolchain_lock.py` — but that gate's scope
is CI *workflows* (`.github/workflows/*.yml`); it cannot see a tan-cli
checkout, so nothing upstream caught
`tan/commands/doctor_cmd.py`'s `ZEPHYR_SDK_INSTALL_VERSION` holding a
hand-ported copy of the same fact on the side that gate cannot reach. This
script byte-diffs the vendored copy at
`contract/fixtures/toolchains/toolchains.json` against the pinned checkout's
`metadata/toolchains.json`.

A failure here has a follow-on:
`python/tests/commands/test_doctor_command.py::test_zephyr_sdk_install_version_matches_the_real_toolchain_lock`
asserts `ZEPHYR_SDK_INSTALL_VERSION` equals this fixture's `zephyrSdk.version`
field, so re-vendoring a bumped lock fails that test until the constant is
updated too. (Its Rust twin in `host_env.rs` went with `crates/` in
tan-cli#269.)

Self-skips with no reachable alp-sdk checkout, like the gates above. Unlike
the bootstrap/kconfig gates, there is no "predates the feature" branch:
`metadata/toolchains.json` already exists at every ref this repo's
`PINNED_SDK_TAG` has pointed at, so an upstream file missing at the pinned
ref is a real regression, not a legitimate skip.

```
python3 tests/parity/toolchain_lock_parity.py --sdk /path/to/an/alp-sdk/checkout
```
