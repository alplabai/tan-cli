<!-- SPDX-License-Identifier: Apache-2.0 -->
# tan

[![ci](https://github.com/alplabai/tan-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/alplabai/tan-cli/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/alplabai/tan-cli?sort=semver)](https://github.com/alplabai/tan-cli/releases/latest)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

`tan` is the standalone build CLI for Alp Lab E1M and E1M-X projects. It reads
hardware metadata from an [alp-sdk](https://github.com/alplabai/alp-sdk)
checkout, creates build plans, and runs the tools needed to build, inspect,
flash, and debug firmware. VS Code is optional. The implementation is Python;
see [Development](#development) for the frozen Rust reference under `crates/`.

## Install

### Installer (recommended)

Linux and macOS:

```sh
curl -fsSL https://raw.githubusercontent.com/alplabai/tan-cli/main/install.sh | sh
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/alplabai/tan-cli/main/install.ps1 | iex
```

The installers download the release for your platform, verify its SHA-256
digest, and install it for the current user. Open a new terminal if `tan` is not
immediately on `PATH`.

Use `--system` on Unix or `-System` on Windows for a system-wide install. See
[`docs/release-contract.md`](docs/release-contract.md) for asset names, manual
verification, and OS support.

The v0.5 release publishes four archives:

- Windows x64
- Linux x64 with glibc
- macOS x64
- macOS arm64

Linux arm64, Linux musl, and Windows arm64 do not have prebuilt v0.5 archives.
Install from source on those hosts.

### From source

Python 3.12 or newer is required:

```sh
git clone https://github.com/alplabai/tan-cli
cd tan-cli
python3 -m pip install ./python
tan --version
```

For serial monitoring, install the optional dependency from the checkout:

```sh
python3 -m pip install "./python[monitor]"
```

`tan` is not published on PyPI, and `@alplabai/tan` is not currently published
on npm. The old `alp-tan-cli` crate installs the frozen v0.4-era Rust CLI, not
the current program. Use a GitHub release or a source checkout.

## Quickstart

Start in an empty working directory:

```sh
git clone https://github.com/alplabai/alp-sdk
tan bootstrap --sdk-root ./alp-sdk
tan init --name my-app
cd my-app

tan validate
tan build
tan size
tan run --flash
```

What those commands do:

1. `bootstrap` prepares west, Zephyr, the Python environment, and SDK
   dependencies.
2. `init` creates a Zephyr application and pins the SDK checkout in
   `.alp/sdk-path`.
3. `validate` checks `board.yaml` and related metadata.
4. `build` plans, materialises, and builds every core slice.
5. `size` reports firmware use against the SoM memory budget.
6. `run --flash` builds and then runs or programs the selected target.

Run `tan doctor` if setup or toolchain discovery fails. `tan doctor --fix`
installs missing user-level prerequisites interactively; it never invokes
`sudo` for you.

If you do not want the west workspace next to the SDK checkout, choose it
explicitly:

```sh
tan bootstrap --sdk-root ./alp-sdk --workspace /path/to/alp-workspace
```

## Common commands

| Task | Command |
| --- | --- |
| Create a project | `tan init --name my-app` |
| Check a project | `tan validate` |
| Build firmware | `tan build` |
| Build and run or flash | `tan run --flash` |
| Flash an existing build | `tan flash` |
| Inspect firmware size | `tan size` |
| Create an image | `tan image` |
| Remove build output | `tan clean` |
| Generate configuration files | `tan generate` |
| Check the host setup | `tan doctor` |
| Start a serial monitor | `tan monitor` |
| Generate debugger settings | `tan debug-config` |
| Run with Renode | `tan renode` |
| List examples and presets | `tan examples`, `tan presets` |
| Explain resolved project settings | `tan explain` |
| Show help | `tan <command> --help` |

On a multi-core SoM, `debug-config` needs `--core <name>` to pick a target;
without it, it exits 2 with `debug-config.target-kind-ambiguous`.

The full command surface also includes `scaffold`, `completion`, `diff`,
`pinmux`, `inspect`, `trace`, `support-bundle`, `kconfig`, `faultdecode`,
`model`, and `new-som`. `migrate`, `lock`, and `quality` forward to their
corresponding `west alp-*` commands: `migrate` requires `--check`,
`--preview`, or `--apply`; `quality` requires `--profile`. The other
commands run directly in `tan`.

For Alif Ensemble MRAM flashing with SETOOLS, see
[`docs/setools.md`](docs/setools.md).

## Choosing an SDK

Most project commands find alp-sdk in this order:

1. `--sdk-root <path>`
2. the project's `.alp/sdk-path` pin
3. the user's default SDK pointer
4. a nearby `alp-sdk` checkout

Step 3 is one pointer shared across every project on the host and is
last-writer-wins: another project's `tan bootstrap` can repoint it. `tan`
warns (`sdk.global-default-foreign-project`) rather than resolving silently;
use `--sdk-root` or the project pin to be explicit.

Use `--sdk-root` when more than one checkout is nearby or when you want a
one-off override:

```sh
tan build --sdk-root /path/to/alp-sdk
```

`tan sdk list` and `tan sdk current` work in v0.5. `tan sdk install` and
`tan sdk switch` are not implemented yet, so clone the SDK yourself and use
`--sdk-root` or let `tan init` write the project pin.

## Automation and JSON output

Add `--format json` for machine-readable output:

```sh
tan build --format json
```

The stable top-level envelope is:

```text
{command, ok, exitCode, project, sdk, data, issues}
```

Use `--ci` or `--non-interactive` in automation. Commands then refuse prompts
and unattended host changes instead of waiting for input. Redirected or piped
stdio is treated as non-interactive too.

## How tan fits with the SDK

```text
alp-sdk-vscode  ->  tan  ->  alp-sdk
VS Code UI          CLI      metadata, schemas, examples, west extensions
```

- `alp-sdk`: hardware metadata, schemas, examples, and the remaining
  `west alp-*` extensions.
- `tan`: SDK selection, planning, build execution, and the manifest that
  `flash`/`size`/`image`/`renode` read.
- `alp-sdk-vscode`: an optional UI that invokes `tan`.

A successful build writes `build/system-manifest.yaml`, which records the
per-core artifacts and is reused by downstream commands.

## Development

New implementation work belongs under `python/`:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e "./python[monitor]"
.venv/bin/python -m pip install pytest
(cd python && ../.venv/bin/python -m pytest tests -q)
python3 python/scripts/version_check.py --selftest --self
```

Useful directories:

```text
python/tan/commands/   command orchestration, filesystem and subprocess work
python/tan/core/       domain logic and wire models
python/tan/planner/    in-process planner, mirrored from alp-sdk
python/tan/templates/  project templates included in the package
python/tests/          unit, conformance, parity, and repository gates
crates/                frozen v0.4 compatibility oracle
```

The Rust reference still has CI coverage, but current release assets are built
from `python/`.

## More documentation

- [Release assets and verification](docs/release-contract.md)
- [SETOOLS setup](docs/setools.md)
- [Development roadmap](docs/ROADMAP.md)
- [Changelog](CHANGELOG.md)
- [License](LICENSE)

`tan` is licensed under Apache-2.0.
