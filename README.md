<!-- SPDX-License-Identifier: Apache-2.0 -->
# tan

[![ci](https://github.com/alplabai/tan-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/alplabai/tan-cli/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/alplabai/tan-cli?sort=semver)](https://github.com/alplabai/tan-cli/releases/latest)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

**The standalone Alp Lab build CLI.** `tan` consumes the alp-sdk *build-plan* and
executes it — it is the single executor and the user command surface for
building, flashing, and inspecting Alp Lab E1M / E1M-X firmware.

`build` / `run` / `size` / `image` / `flash` / `clean` / `renode` are native
Rust; only `migrate` / `lock` / `quality` still forward to `west alp-*`, and
`model` / `monitor` / `new-som` / `faultdecode` to the SDK `alp` CLI. Licensed
**Apache-2.0** (see [`LICENSE`](LICENSE); the SPDX identifier is also set in each
`Cargo.toml` and source header).

## Install

Every version tag publishes a raw, uncompressed binary per platform.

### Automatic (recommended)

The install scripts detect your platform, download the matching binary, and put
`tan` on your PATH. They install **user-local by default — no `sudo`/admin**
(`~/.local/bin` on Unix; `%LOCALAPPDATA%\Programs\tan` + your user PATH on
Windows). Add `--system` / `-System` for a system-wide install (that path needs
elevated permission).

On Unix, if the install dir is not already on PATH, the script appends one line
to your login shell's rc (`~/.zshrc` / `~/.bash_profile` / `~/.profile`) — with a
printed notice, idempotently — so `tan` works in a new shell (this is what makes a
no-sudo install global on macOS, where `~/.local/bin` isn't on the default PATH).
Pass `--no-modify-path` to skip it. On Windows the script already updates your
user PATH.

```sh
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/alplabai/tan-cli/main/install.sh | sh
# system-wide (/usr/local/bin, uses sudo):   curl -fsSL …/install.sh | sh -s -- --system
```

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/alplabai/tan-cli/main/install.ps1 | iex
# system-wide (%ProgramFiles%, run in an elevated PowerShell):   … ; .\install.ps1 -System
```

### Manual

Pick the asset for your host (full table in [`docs/release-contract.md`](docs/release-contract.md)):

**Linux / macOS**

```sh
# x86_64 linux; swap the asset name for your platform
curl -fsSL -o tan https://github.com/alplabai/tan-cli/releases/latest/download/tan-x86_64-unknown-linux-gnu
chmod +x tan && sudo mv tan /usr/local/bin/tan
tan --version
```

**Windows (PowerShell)**

```powershell
Invoke-WebRequest -Uri https://github.com/alplabai/tan-cli/releases/latest/download/tan-x86_64-pc-windows-msvc.exe -OutFile tan.exe
.\tan.exe --version
```

**From source** (Rust **1.86+**, edition 2024):

```sh
git clone https://github.com/alplabai/tan-cli && cd tan-cli
cargo install --path crates/tan-cli --locked
```

`tan` needs an **alp-sdk checkout** to plan against. It is found, in order, from
`--sdk-root <path>`, the `.alp/sdk-path` pointer `tan sdk switch` writes, or an
`alp-sdk/` directory beside the project. `tan sdk install <version>` only
downloads into `~/.alp/sdk-cache` — follow it with `tan sdk switch <version>` to
select it. No VS Code required.

## Quickstart

```sh
# Start in a directory holding an alp-sdk checkout — clone one, or
# `tan sdk install <version> && tan sdk switch <version>`.
tan bootstrap --sdk-root ./alp-sdk    # west + Zephyr workspace + Python deps
tan init --template minimal-app --som E1M-AEN701 --name my-app
cd my-app                             # sibling ../alp-sdk resolves automatically

tan validate                          # schema + semantic checks on board.yaml
tan build                             # plan → materialise → per-core slice build
tan size                              # footprint vs the SoM memory budget
tan run --flash                       # build, then run (host) or program (hardware)
```

`tan doctor --build --fix` diagnoses (and repairs what it can) a build
environment that is not ready. `tan completion --shell zsh` emits a completion
script.

## Commands

| Area | Commands |
| --- | --- |
| **Project** | `init` · `scaffold` · `examples` · `explain` · `presets` · `pinmux` |
| **Configure & verify** | `validate` · `generate` · `diff` · `inspect` · `trace` · `doctor` · `debug-config` · `support-bundle` · `kconfig` |
| **Build & run** (native) | `build` · `run` · `flash` · `image` · `size` · `clean` · `renode` |
| **Environment** | `bootstrap` · `sdk` · `completion` |
| **Forwarders** | `migrate` · `lock` · `quality` → `west alp-*`; `model` · `monitor` · `new-som` · `faultdecode` → `python -m alp_cli` |

`tan <command> --help` for flags. Global flags apply to every command:

| Flag | Effect |
| --- | --- |
| `--project <PATH>` | Project root (default: current directory). |
| `--board-yaml <PATH>` | Explicit `board.yaml`, overriding project resolution. |
| `--sdk-root <PATH>` | alp-sdk checkout to plan against. |
| `--format json` | Machine-readable envelope instead of text. |
| `--ci` | Implies `--non-interactive` and disables color. |
| `--quiet` / `--verbose` / `--no-color` | Output volume and styling. |

`--format json` emits the stable envelope
`{command, ok, exitCode, project, data, issues}` — the contract the
alp-sdk-vscode extension consumes. Text output is for humans and may change;
the envelope is the API.

## Where it sits (three repos, one executor)

```
 alp-sdk-vscode  ──shells──►  tan (this repo)  ──drives──►  alp-sdk
 (VS Code ext)                (executor + CLI)              (planner + libs)
```

- **alp-sdk** — the planner + libraries. Emits the machine-readable *build-plan*
  (`python -m alp_orchestrate --emit build-plan`). Ships an `alp` console script
  (plus `alp-mcp`) and the `west alp-*` commands `tan` forwards to (see
  Forwarders below) — it is not a user-facing CLI surface in its own right.
- **tan** — this repo. Consumes the plan and executes each per-core slice
  (`west` / `bitbake` / `cmake`), owns skip-vs-fail, env application, scheduling,
  progress UX, SDK version management, and the manifest it reads back for
  flash/size/image. **What a standalone SDK user installs — no VS Code needed.**
- **alp-sdk-vscode** — a thin extension intended to shell `tan`; as of this
  writing the extension still resolves/downloads a binary named `alp`
  (`SUPPORTED_CLI_VERSION` 0.2.0) — the repoint to `tan` is pending.

Dependency direction is one-way: **extension → tan → alp-sdk.** Installing `tan`
never drags in the extension. The user-facing command / binary is `tan`, not
`alp` (RFC #837).

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

A build writes `build/system-manifest.yaml` — the post-build IDE/tool contract
(per-core slices, IPC, helper MCUs) that `flash` / `image` / `size` / `renode`
read back.

## Workspace layout

A Cargo workspace; pure logic lives in `tan-core`, all IO and subprocess
execution in `tan-cli`:

```
Cargo.toml                     # [workspace] + [workspace.dependencies] + [profile.release]
crates/
  tan-core/                    # pure domain logic (no IO)
    src/build_plan.rs          #   build-plan consumer contract + version-skew guard
    src/system_manifest.rs     #   post-build manifest parse/overlay/serialize
    src/plan_exec.rs           #   pure env-append + skip/fail-policy decisions
    src/{flash,debug,wizard,sdk_catalogue}/   #   backends, reports, templates, presets
  tan-cli/                     # the `tan` binary — arg parsing, IO, subprocess exec
    src/{main,cli,envelope,exit}.rs
    src/commands/{build,run,flash,init}/          #   module dirs
    src/commands/{sdk,doctor,validate,…}.rs
```

`tan-core` is `alp-core` ported faithfully (symbol names preserved; only the
crate name and cosmetic `alp`-branding changed). `build_plan.rs` additionally
carries the newer ADR-0020 fields the SDK now emits — per-slice
`envAppendPath` and the top-level `executionPolicy`.

## Development

Four gates, all of them, before every push. CI runs `fmt` + `clippy` once on
Linux and matrixes `build` + `test` across Linux, Windows, and macOS:

```sh
cargo fmt --all --check
cargo clippy --all-targets -- -D warnings
cargo build --all-targets
cargo test
```

House rules: keep files small, put pure logic in `tan-core` (with unit tests)
rather than the executor, and never rename an SDK-contract string
(`alp-sdk`, `alp_orchestrate`, `board.yaml`, `alp.conf`, `.alp/…`) — only the
user-facing binary is `tan`.

## Releases

Version-tag pushes (`v<major>.<minor>.<patch>`) build per-platform `tan`
binaries and publish them as GitHub release assets for the alp-sdk-vscode
downloader. The tag must equal the workspace `Cargo.toml` version — CI fails the
release otherwise. The exact tag scheme, per-target asset names, and the vscode
`releaseAssetForTarget` mapping are the release-asset contract — see
[`docs/release-contract.md`](docs/release-contract.md).

## References

- alp-sdk **ADR-0020** (the decision this implements)
- **RFC #843** (the drift that motivated it): alplabai/alp-sdk#843
- **RFC #837** (`alp` → `tan` naming): alplabai/alp-sdk#837
