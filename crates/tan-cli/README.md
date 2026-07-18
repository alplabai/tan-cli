<!-- SPDX-License-Identifier: Apache-2.0 -->
# tan

The native `tan` CLI for Alp Lab embedded projects. `tan` consumes the alp-sdk
*build-plan* and executes it, with a stable JSON envelope on stdout.

A port of `cli-rs` (from `alp-sdk-vscode`) for alp-sdk **ADR-0020 Phase 2**. See
the workspace README for the three-repo model.

Ported commands: `validate`, `generate`, `init`, `scaffold`, `examples`,
`doctor`, `completion`, `diff`, `presets`, `pinmux`, `explain`, `inspect`,
`trace`, `debug-config`, `support-bundle`, `sdk`, `bootstrap`, `build`
(and its `image`/`flash`/`clean`/`renode` west-forwarding wrappers).
