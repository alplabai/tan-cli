<!-- SPDX-License-Identifier: Apache-2.0 -->
# tan

**The standalone Alp Lab build CLI.** `tan` consumes the alp-sdk *build-plan* and
executes it — it is the single executor and the whole user command surface for
building, flashing, and inspecting Alp Lab E1M / E1M-X firmware.

> **Status: DRAFT SKELETON.** The command surface is stubbed; the executor is not
> implemented yet. This repo seeds Phase 2 of alp-sdk **ADR-0020** and will be
> reconciled with the existing Rust CLI (`cli-rs`) currently in `alp-sdk-vscode`.
> It is private until the architecture is co-signed. **Before going public:** add
> the full `LICENSE` file (Apache-2.0; the SPDX identifier is set in `Cargo.toml`
> and source headers meanwhile).

## Where it sits (three repos, one executor)

```
 alp-sdk-vscode  ──shells──►  tan (this repo)  ──drives──►  alp-sdk
 (VS Code ext)                (executor + CLI)              (planner + libs)
```

- **alp-sdk** — the planner + libraries. Emits the machine-readable *build-plan*
  (`python -m alp_orchestrate --emit build-plan`). Keeps **zero** user commands.
- **tan** — this repo. Consumes the plan and executes each per-core slice
  (`west` / `bitbake` / `cmake`), owns skip-vs-fail, env application, scheduling,
  cancellation, progress UX, SDK version management, and the manifest it also
  reads back for flash/size/image. **What a standalone SDK user installs — no
  VS Code needed.**
- **alp-sdk-vscode** — a thin extension that shells `tan`.

Dependency direction is one-way: **extension → tan → alp-sdk.** Installing `tan`
never drags in the extension.

## The seam: the build-plan

`tan` reads SDK internals through exactly one contract — the build-plan JSON
(`metadata/schemas/build-plan-v1.schema.json` in alp-sdk). `src/build_plan.rs`
models the consumer side. Two guarantees the ADR pins:

- **Version-skew guard** — `tan` rejects a plan whose `schemaVersion` it doesn't
  support (or, later, an unknown *required-for-execution* key) instead of
  silently falling back to hand-ported behaviour. That silent fallback is exactly
  the drift RFC #843 fixed; skew must not re-introduce it.
- **`env` vs `envAppendPath`** — `env` is set verbatim; `envAppendPath` is
  appended (os.pathsep) *only if not already present*, so a consumer that resolves
  those paths itself is not silently overridden ("plan wins / CLI fills gaps").

## Command surface (stubbed)

`tan build | flash | image | size | renode | clean | sdk | doctor | validate`

See `src/commands.rs::build` for the intended `build` flow.

## Build

```
cargo build
cargo test
```

## References

- alp-sdk **ADR-0020** (the decision this implements): alplabai/alp-sdk PR **#846**
- **RFC #843** (the drift that motivated it): alplabai/alp-sdk#843
- **RFC #837** (`alp` → `tan` naming): alplabai/alp-sdk#837
