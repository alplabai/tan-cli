<!-- SPDX-License-Identifier: Apache-2.0 -->
# tan roadmap

The version axis. GitHub milestones mirror the versions listed here — add a
version here first, then create the milestone, never the other way round.

The arc is one migration: **the Rust `tan` becomes a Python `tan`, and `tan`
becomes the only planner and executor.** Today alp-sdk plans and `tan` executes;
at the end of this roadmap `tan` does both and alp-sdk carries no Python.

## The two acceptance targets

Everything below is scaffolding for these. Neither is a checkbox — both are
measured, on real hardware or against the real released binary.

**Target 1 — a customer's first hour works.** A fresh scaffold builds, flashes,
and blinks an LED on an E1M-AEN801, and survives a cold power-cycle. All of it
through `tan`, not through `west build` and `JLinkExe`. Only a cold power cycle
validly exercises this chain; `RSetType 2; r; g` mis-boots it.

**Target 2 — a v0.4.1 user upgrades with no manual migration.** Their existing
project, their existing `.alp/sdk-path`, their existing scripts and exit codes.
Established by RUNNING the v0.4.1 oracle, never by reading `crates/`.

## Versions

### v0.5.0-rc.1 — opt-in release candidate

The first tag at which a Python `tan` exists. Deliberately **not** the binary
the extension downloads: `SUPPORTED_CLI_VERSION` in alp-sdk-vscode stays
pinned, and that pin is the entire opt-in mechanism.

Ships the essential command surface — `build`, `generate`/`emit`, `doctor`,
`sdk`, `kconfig`, `init`, `flash`, `bootstrap`, `validate` — plus the
`{command, ok, exitCode, project, data, issues}` envelope alp-sdk-vscode parses,
and the v0.4.1 compatibility floor.

Gated on Target 1 green on silicon.

### v0.5.0 — general availability

`SUPPORTED_CLI_VERSION` moves. The Python `tan` becomes what customers actually
get. Gated on the RC having soaked, not on a date.

### v0.6.0 — full command-surface parity

The verbs deliberately left out of the RC: `model`, `new-som`, `monitor`,
`faultdecode`, the introspection set, `renode`, and the seven entirely-unported
commands. Also the known oracle divergences filed during the port.

Deferred is not a bug backlog — the `deferred` label means *chosen*, and each
issue records what the oracle does so the choice can be re-read later.

### v0.7.0 — retire the oracle

`crates/` deleted, alp-sdk's Python planner removed, `tan` the sole planner and
executor. The end state ADR-0020 names.

The blocking question is not code deletion. `crates/` is currently the **oracle**
— every parity test measures the port by running `target/debug/tan.exe`. Delete
it and the port loses the only thing that can tell it it has drifted. And
`contract/` is frozen today by `crates/tan-cli/tests/contract.rs`; if the Rust
test goes without a Python enforcer, the freeze quietly becomes advisory.

## Standing rules

These are here because each one has already cost a round.

- **Measure the oracle by RUNNING it.** `target/debug/tan.exe`, never by reading
  `crates/` or docs. Source-reading has given a wrong answer twice.
- **A conclusion is not a measurement.** Two full rounds of wrong work came from
  believing a stale comment over the adjacent example.
- **Never edit `crates/` or `contract/`** — frozen.
- **LF is the convention.** Check both `git diff --numstat` and
  `--ignore-cr-at-eol`; a CRLF-only diff is invisible to one of them.
- **No exit codes behind pipes.** `cmd | tail` reports `tail`'s status.
- **The bench is serial and reservation-gated.** Verify `acquired:` before every
  write and every reportable read.
- tan-cli gates on the four cargo checks plus its own pytest suite. alp-sdk gates
  on `bash scripts/test-all.sh`. They are not interchangeable.
