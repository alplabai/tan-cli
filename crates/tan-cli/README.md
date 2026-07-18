<!-- SPDX-License-Identifier: Apache-2.0 -->
# tan

The native `tan` CLI for Alp Lab embedded projects. `tan` consumes the alp-sdk
*build-plan* and executes it, with a stable JSON envelope on stdout.

DRAFT SEED for alp-sdk **ADR-0020 Phase 2** — a snapshot port of `cli-rs` (from
`alp-sdk-vscode`), scoped to the SDK-interacting commands. See the workspace
README for the three-repo model.

Ported commands: `validate`, `generate`, `doctor`, `sdk`, `bootstrap`, `build`
(and its `image`/`flash`/`clean`/`renode` west-forwarding wrappers).
