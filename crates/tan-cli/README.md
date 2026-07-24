<!-- SPDX-License-Identifier: Apache-2.0 -->
# tan

The native `tan` CLI for Alp Lab embedded projects. `tan` consumes the alp-sdk
*build-plan* and executes it, with a stable JSON envelope on stdout.

A port of `cli-rs` (from `alp-sdk-vscode`) for alp-sdk **ADR-0020 Phase 2**. See
the workspace README for the three-repo model.

Ported commands: `validate`, `generate`, `init`, `scaffold`, `examples`,
`doctor`, `completion`, `diff`, `presets`, `pinmux`, `explain`, `inspect`,
`trace`, `debug-config`, `support-bundle`, `sdk`, `bootstrap`, `build`,
`kconfig`. `build`/`run`/`size`/`image`/`flash`/`clean`/`renode` are native
Rust;
only `migrate`/`lock`/`quality` still forward to `west alp-*`. `kconfig`
wraps the SDK's `--emit kconfig` (needs a bootstrapped Zephyr workspace).
Plus `monitor`/`new-som`/`faultdecode` forwarders to the SDK `alp` CLI
(`python -m alp_cli <sub>`). `model` mirrors `alp model`
(`build`/`list`/`info`/`doctor`/`check`/`zoo`/`add`/`prep`/`run`/`ab`),
surfacing the JSON envelope (`{command,ok,exitCode,project,data,issues}`)
rather than being an opaque passthrough — `check` is a conservative offline
pre-flight (fit/perf estimate, verified on silicon later); `run`/`ab` are
host-reference runs, not target-SoM performance.
