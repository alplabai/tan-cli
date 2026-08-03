<!-- SPDX-License-Identifier: Apache-2.0 -->
# tan

> **Frozen behaviour oracle.** This crate is the v0.4.1-era Rust implementation,
> retained for parity tests while the port is completed. It is not used to build
> current release assets, and new commands and fixes belong under `python/`.
> Building this crate produces the reference CLI, not the shipping Python CLI.

At its freeze point this was the native `tan` CLI for Alp Lab embedded projects.
It consumes the alp-sdk *build-plan* and executes it, with a stable JSON envelope
on stdout.

A port of `cli-rs` (from `alp-sdk-vscode`) for alp-sdk **ADR-0020 Phase 2**. See
the workspace README for the three-repo model.

Commands present in the frozen snapshot: `validate`, `generate`, `init`, `scaffold`, `examples`,
`doctor`, `completion`, `diff`, `presets`, `pinmux`, `explain`, `inspect`,
`trace`, `debug-config`, `support-bundle`, `sdk`, `bootstrap`, `build`,
`kconfig`. `build`/`run`/`size`/`image`/`flash`/`clean`/`renode` are native
Rust;
only `migrate`/`lock`/`quality` still forward to `west alp-*`. `kconfig`
wraps the SDK's `--emit kconfig` (needs a bootstrapped Zephyr workspace).
Plus `model`/`monitor`/`new-som`/`faultdecode` forwarders to the SDK `alp`
CLI (`python -m alp_cli <sub>`). The shipping Python CLI has since ported those
commands in-process; see the workspace README for the current surface.
