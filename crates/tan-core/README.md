<!-- SPDX-License-Identifier: Apache-2.0 -->

# tan-core

[![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/alplabai/tan-cli/blob/main/LICENSE)

> **Frozen behaviour oracle.** `tan-core` is retained with the v0.4.1-era Rust
> CLI for parity and contract checks. The shipping implementation and current
> domain logic live in `python/tan/`; release assets are not built from this
> crate.

At its freeze point, this was the pure, **I/O-free** domain logic for
[`tan`](https://github.com/alplabai/tan-cli) —
the standalone Alp Lab build CLI that consumes the alp-sdk *build-plan* and
executes it. This crate is the shared engine behind the `tan` binary
(`crates/tan-cli`).

`tan-core` is the frozen Rust home of the build-plan / system-manifest **consumer**
contracts plus the pure decision logic for the native commands. It contains
**no** terminal, filesystem-walking, or process code — only deterministic,
serde-based transforms — so the same logic stays testable in isolation and could
later be bridged elsewhere (napi-rs / WASM).

## What's inside

| Module | Responsibility |
| --- | --- |
| `build_plan` | Parse the SDK's build-plan contract (consumer structs, `envAppendPath`, `executionPolicy`) + the version-skew guard. |
| `system_manifest` | The post-build system-manifest contract — `parse_system_manifest` / `overlay_run_results` / `serialize_system_manifest`. |
| `kconfig` | The `--emit kconfig` contract (workspace-dependent) — `parse_kconfig` + `resolve_default_kconfig_core`. |
| `plan_exec` | **Pure** env-append + skip/fail-policy decisions (`apply_env_append`, `assemble_slice_env`, `resolve_action`). |
| `size` | `tan size` measurement logic + SoM-preset → SoC-variant budget resolution. |
| `flash` | `tan flash` boot-order / artefact planning. |
| `renode` | `tan renode` headless-smoke command planning. |
| `clean` | `tan clean` build-tree selection. |
| `image_bundle` | `tan image` bundle shape + forward-slash artefact paths. |
| `build_readiness` | Per-OS build-toolchain readiness reports. |
| `presets`, `sdk_catalogue` | SoM/SKU catalogue, chip/board presets, topology. |
| `loader`, `project` | Generation-target planning + workspace / project resolution. |
| `wizard` | Project / module scaffolding plans. |
| `clock` | Deterministic ISO-8601 UTC timestamps (`SOURCE_DATE_EPOCH`-aware). |

## Design

`tan-core` is where the keep-files-small / pure-logic-in-core convention lands:
every non-IO decision for a native command lives here with unit tests, while the
IO/executor shell stays in `crates/tan-cli`. Reading SDK internals goes through
exactly one seam — the build-plan JSON — modelled on the consumer side here.

## License

[Apache-2.0](https://github.com/alplabai/tan-cli/blob/main/LICENSE).
