<!-- SPDX-License-Identifier: Apache-2.0 -->
# Alp Lab toolchain roadmap

Three repos ship one toolchain. This is the shared version axis for all of them.
GitHub milestones mirror what is listed here — add a version here first, then
create the milestone, never the other way round.

Canonical copy lives in `alplabai/tan-cli`. `alp-sdk` and `alp-sdk-vscode`
should link here rather than keep their own divergent copies.

## The three repos

```
   alp-sdk  ──────────►  tan  ──────────►  Alp IDE (alp-sdk-vscode)
   the SoM SDK           the CLI            the VS Code extension

   board.yaml, metadata,  plans and         a GUI over tan; runs it and
   schemas, examples      executes builds   parses its JSON envelope
```

Dependencies run left to right, and **only** left to right. `alp-sdk` does not
know `tan` exists. `tan` reads an `alp-sdk` checkout. The extension shells `tan`
and never re-implements it.

That shape is why the three release on different cadences and why only two
couplings actually matter:

| Coupling | Where it lives | What it controls |
|---|---|---|
| `SUPPORTED_CLI_VERSION` | `alp-sdk-vscode`, `src/alpCli/service.ts` | which `tan` release the extension downloads |
| `PINNED_SDK_TAG` | `tan-cli`, `.github/workflows/parity.yml` | which `alp-sdk` `tan` is tested against |

A `tan` release that no extension pin names reaches no IDE user. That is
deliberate — it is how a release candidate stays opt-in.

## Which versions go together

The line to install today:

| Component | Version | Notes |
|---|---|---|
| alp-sdk | **v0.14.0** | |
| tan | **v0.4.1** | Rust build |
| Alp IDE | **v0.4.x** | stable channel; pins tan `0.4.1` |

Planned, once the Python `tan` ships:

| Component | Beta line | Stable line |
|---|---|---|
| alp-sdk | its own cadence, unaffected | its own cadence, unaffected |
| tan | `v0.5.0-rc1` — Python | `v0.5.0` — Python |
| Alp IDE | `v0.5.x` — **pre-release** | `v0.6.x` — stable |

### How to tell a beta from a stable Alp IDE build

**The minor version's parity is the channel.** Odd minor (`v0.3.x`, `v0.5.x`) is
the pre-release channel; even minor (`v0.4.x`, `v0.6.x`) is stable. There is no
separate label to read — the number *is* the switch.

So a customer who does not opt into VS Code pre-releases stays on an even-minor
extension pinned to a stable `tan`, and is never moved onto the Python `tan` by
an automatic update.

Two things not to misread on the releases page: `cli-rs-v*` tags are a retired
in-repo Rust CLI, a dead lineage — never read them as the extension. And GitHub's
"Latest" badge marks the newest *non-prerelease*, so while the extension sits on
an odd minor, "Latest" is not the newest build.

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

## The migration

One arc: **the Rust `tan` becomes a Python `tan`, and `tan` becomes the only
planner and executor.** Today `alp-sdk` plans and `tan` executes; at the end
`tan` does both and `alp-sdk` carries no Python.

### tan — `v0.5.0-rc1` · opt-in release candidate

The first tag at which a Python `tan` exists. Ships the essential command
surface — `build`, `generate`/`emit`, `doctor`, `sdk`, `kconfig`, `init`,
`flash`, `bootstrap`, `validate` — plus the
`{command, ok, exitCode, project, data, issues}` envelope the extension parses,
and the v0.4.1 compatibility floor.

Assets are PyInstaller freezes of `python/`, on four targets. Windows arm64 and
Linux arm64 get no asset in this release: PyInstaller cannot cross-compile and
there is no runner to freeze on. Those hosts stay on the stable line.

Gated on Target 1 green on silicon.

### tan — `v0.5.0` · general availability

`SUPPORTED_CLI_VERSION` moves; the Python `tan` becomes what customers get.
Gated on the RC having soaked, not on a date.

### tan — `v0.6.0` · full command-surface parity

The verbs deliberately left out of the RC: `model`, `new-som`, `monitor`,
`faultdecode`, the introspection set, `renode`, and the seven entirely-unported
commands. Also the known oracle divergences filed during the port.

Deferred is not a bug backlog — the `deferred` label means *chosen*, and each
issue records what the oracle does so the choice can be re-read later.

### tan — `v0.7.0` · retire the oracle

`crates/` deleted, `alp-sdk`'s Python planner removed, `tan` the sole planner and
executor. The end state ADR-0020 names.

The blocking question is not code deletion. `crates/` is currently the **oracle**
— every parity test measures the port by running `target/debug/tan.exe`. Delete
it and the port loses the only thing that can tell it it has drifted. And
`contract/` is frozen today by `crates/tan-cli/tests/contract.rs`; if the Rust
test goes without a Python enforcer, the freeze quietly becomes advisory.

**Pulled forward deliberately.** The direction is to get off Rust as fast as is
safe, so Rust's *load-bearing* roles are removed in order rather than waiting for
the version number:

1. **Out of the release path — at `v0.5.0-rc1`.** The RC ships PyInstaller
   freezes and no cargo build at all. After that tag, nothing customers receive
   is Rust.
2. **Out of the correctness path.** Freeze the oracle's observed behaviour into
   committed fixtures *while a working binary still exists*. The only item that
   gets harder the longer it waits: once `crates/` is gone, anything never
   captured is unrecoverable. A frozen fixture also beats a live oracle, which
   can itself drift.
3. **Out of the repo.** Safe once 1 and 2 are done, and gated on answering who
   enforces `contract/` afterwards.

Issues whose fix lives in `crates/` were moved off the RC for the same reason:
`crates/` is frozen, so they are blocked by policy, not effort. Each carries a
comment saying what survives the port and what dies with the oracle.

### Alp IDE — `v0.5.x` beta, `v0.6.x` stable

The extension cannot move until `tan` ships, and it is the delivery mechanism.
Its cutover work is the three couplings plus the pin: a prerelease-capable pin,
the Linux target triple, and an honest message on the two platforms a Python
`tan` release does not publish.

### alp-sdk — unchanged cadence

`alp-sdk` is upstream and independent; none of the above changes how it ships.
Its only obligations to the rest of the toolchain are to keep firing the
`alp-sdk-planner-change` dispatch when its contract surface moves, and — much
later, at `tan` `v0.7.0` — to drop its own Python planner once `tan` owns
planning outright.

## Standing rules

Internal. Each is here because it has already cost a round.

- **Measure the oracle by RUNNING it.** `target/debug/tan.exe`, never by reading
  `crates/` or docs. Source-reading has given a wrong answer twice.
- **A conclusion is not a measurement.** Two full rounds of wrong work came from
  believing a stale comment over the adjacent example.
- **Parity is measured against `PINNED_SDK_TAG`, on a clean LF-native clone.**
  A dirty or differently-reffed `alp-sdk` produces confident nonsense in both
  directions.
- **Never edit `crates/` or `contract/`** — frozen.
- **A frozen tree is measured at its own freeze vendor point; a shipped
  Python surface is measured at `PINNED_SDK_TAG`.** Every parity gate has to
  pick one of the two — never compare a frozen tree against a moving pin.
  That is what makes the freeze absolute rather than something each pin bump
  re-negotiates.
- **LF is the convention.** Check both `git diff --numstat` and
  `--ignore-cr-at-eol`; a CRLF-only diff is invisible to one of them.
- **No exit codes behind pipes.** `cmd | tail` reports `tail`'s status.
- **The bench is serial and reservation-gated.** Verify `acquired:` before every
  write and every reportable read.
- tan-cli gates on its cargo checks plus its own pytest suite. alp-sdk gates on
  `bash scripts/test-all.sh`. alp-sdk-vscode gates on its pnpm suite. They are
  not interchangeable.
