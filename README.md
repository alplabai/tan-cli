<!-- SPDX-License-Identifier: Apache-2.0 -->
# tan

**The standalone Alp Lab build CLI.** `tan` consumes the alp-sdk *build-plan* and
executes it — it is the single executor and the user command surface for
building, flashing, and inspecting Alp Lab E1M / E1M-X firmware.

> **Status: PRIVATE DRAFT SEED.** This repo seeds **Phase 2 of alp-sdk ADR-0020**.
> It is a *snapshot port* of the Rust CLI (`cli-rs`) that currently lives in
> `alp-sdk-vscode` — **not** the history-preserving extraction (that lands
> later). Only the SDK-interacting slice is ported so far (see "What's ported").
> Private until the architecture is co-signed. **Before going public:** add the
> full `LICENSE` file (Apache-2.0; the SPDX identifier is set in each
> `Cargo.toml` and source header meanwhile).

## Where it sits (three repos, one executor)

```
 alp-sdk-vscode  ──shells──►  tan (this repo)  ──drives──►  alp-sdk
 (VS Code ext)                (executor + CLI)              (planner + libs)
```

- **alp-sdk** — the planner + libraries. Emits the machine-readable *build-plan*
  (`python -m alp_orchestrate --emit build-plan`). Keeps **zero** user commands.
- **tan** — this repo. Consumes the plan and executes each per-core slice
  (`west` / `bitbake` / `cmake`), owns skip-vs-fail, env application, scheduling,
  progress UX, SDK version management, and the manifest it reads back for
  flash/size/image. **What a standalone SDK user installs — no VS Code needed.**
- **alp-sdk-vscode** — a thin extension that shells `tan`.

Dependency direction is one-way: **extension → tan → alp-sdk.** Installing `tan`
never drags in the extension. The user-facing command / binary is `tan`, not
`alp` (RFC #837).

## Workspace layout

A Cargo workspace mirroring `cli-rs`:

```
Cargo.toml                     # [workspace] + [workspace.dependencies] + [profile.release]
crates/
  tan-core/                    # pure domain logic (no IO): build-plan + system-manifest
    src/build_plan.rs          #   contracts, board.yaml model/validate, presets,
    src/loader.rs              #   debug/doctor reports, SDK readiness, pinmux, …
    src/lib.rs
    …
  tan-cli/                     # the `tan` binary — argument parsing, IO, subprocess exec
    src/main.rs
    src/cli.rs
    src/commands/{build,sdk,doctor,validate,generate,bootstrap}.rs
    …
```

`tan-core` is `alp-core` ported faithfully (symbol names preserved; only the
crate name and cosmetic `alp`-branding changed). `build_plan.rs` additionally
carries the newer ADR-0020 fields the SDK now emits — per-slice
`envAppendPath` and the top-level `executionPolicy`.

## What's ported (this snapshot)

- **tan-core** — the full domain crate (verbatim from `alp-core`).
- **tan-cli** — `validate`, `generate`, `doctor`, `sdk`, `bootstrap`, and
  `build` (with its `image` / `flash` / `clean` / `renode` west-forwarding
  wrappers). The other 13 `cli-rs` commands (init/scaffold/examples/completion/
  diff/presets/pinmux/explain/inspect/trace/debug-config/support-bundle) are
  **dropped** from this snapshot; they return in the full extraction.

## The seam: the build-plan

`tan` reads SDK internals through exactly one contract — the build-plan JSON
(`metadata/schemas/build-plan-v1.schema.json` in alp-sdk). `tan-core`'s
`build_plan.rs` models the consumer side. Two guarantees the ADR pins:

- **Version-skew guard** — `tan` rejects a plan whose `schemaVersion` it doesn't
  support instead of silently falling back to hand-ported behaviour. That silent
  fallback is exactly the drift RFC #843 fixed; skew must not re-introduce it.
- **`env` vs `envAppendPath`** — `env` is set verbatim; `envAppendPath` is
  appended (os.pathsep) *only if not already present*, so a consumer that
  resolves those paths itself is not silently overridden ("plan wins / CLI fills
  gaps").

## Build

```
cargo build --all-targets
cargo test
```

## References

- alp-sdk **ADR-0020** (the decision this implements)
- **RFC #843** (the drift that motivated it): alplabai/alp-sdk#843
- **RFC #837** (`alp` → `tan` naming): alplabai/alp-sdk#837
