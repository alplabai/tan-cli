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

The stable line and the current opt-in pre-release line:

| Component | Stable | Opt-in / pre-release |
|---|---|---|
| alp-sdk | **v0.15.0** | none live; tan parity may pin a newer exact commit than the latest tag |
| tan | **v0.5.1** — shipping Python port | none live |
| Alp IDE | **v0.4.0** | **v0.5.x** pre-release channel |

The Python `tan` line is now stable: `v0.5.0` shipped general availability and
`SUPPORTED_CLI_VERSION` has already moved to it (now `0.5.1`) in
`alp-sdk-vscode`'s default branch. Alp IDE's own stable cutover to consume it
by default remains `v0.6.x`.

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
Established by RUNNING the v0.4.1 oracle while one existed; `crates/` is now
deleted (tan-cli#269), so what remains of that measurement is the frozen
capture store at `python/tests/fixtures/oracle_captures/` and the
`contract/envelopes/*` goldens. Cite those; do not reconstruct an answer from
memory or from a doc.

## The migration

One arc: **the Rust `tan` becomes a Python `tan`, and `tan` becomes the only
planner and executor.** The release path, the planner and now the repo itself
have moved: `python/tan/planner/` plans in-process, the Python executor runs
the plan, and `crates/` is deleted. The remaining end state is alp-sdk dropping
its original planner copy.

### tan — `v0.5.0-rc1` · opt-in release candidate

The first tag at which a Python `tan` existed. It shipped the essential command
surface — `build`, `generate`/`emit`, `doctor`, `sdk`, `kconfig`, `init`,
`flash`, `bootstrap`, `validate` — plus the
`{command, ok, exitCode, project, data, issues}` envelope the extension parses,
and the v0.4.1 compatibility floor.

Assets are PyInstaller freezes of `python/`, on four targets. Windows arm64 and
Linux arm64 get no asset in this release: PyInstaller cannot cross-compile and
adding the available hosted arm64 runners was out of scope, not impossible.
Those hosts stay on the stable line or install the Python package from a
checkout.

Gated on Target 1 green on silicon.

### tan — `v0.5.0` · general availability — SHIPPED (2026-08-04)

`SUPPORTED_CLI_VERSION` moved; the Python `tan` is what customers get.
`v0.5.1` (2026-08-05) followed as a patch on the same GA line and is today's
`latest`; `alp-sdk-vscode`'s default branch already pins
`SUPPORTED_CLI_VERSION = "0.5.1"`.

### tan — `v0.6.0` · retire the oracle, and the known divergences

The command-surface work once planned for this milestone SHIPPED AS `0.5.0`
and its issues moved to that milestone, so `v0.6.0` names the next release
and nothing already delivered. The full command surface landed inside the
`v0.5.0` RC cycle instead of waiting for a later one: the seven verbs that shipped as stubs at rc1
(`scaffold`, `completion`, `diff`, `pinmux`, `inspect`, `trace`,
`support-bundle` — tan-cli#260, #257), `model` (#253), `new-som` (#254),
`monitor` (#255), `faultdecode` (#256), and `renode --sim-mode` (#77) are all
real by `v0.5.0-rc4`. What is still deferred to `v0.6.0` is narrower — the
known oracle divergences filed during the port (see the `deferred` label) —
and the oracle's own retirement, which landed here rather than at `v0.7.0`.

**`tan renode` is removed.** The verb, `tan/core/renode_plan.py`,
`tan/core/renode_sim.py` and the 27 published `renode.*` issue codes all go;
`renode --sim-mode` (#77) shipped in the `v0.5.0` RC cycle and does not
survive into `v0.6.0`. Renode is retired repo-wide, not paused: alp-sdk#1539
re-instates and widens ADR-0022's retirement, deleting its four
`pr-renode-*` workflows and the `examples/aen/aen-sim-vision` example, and
`parity.yml`'s `seam2` job now stops at the ARM-ELF assertion instead of
booting the artefact. Dropping registered codes SHRINKS the
`envelope-contract.json` release asset, so this is a breaking CLI-surface
change carried by `v0.6.0`, not an additive one.

Deferred is not a bug backlog — the `deferred` label means *chosen*, and each
issue records what the oracle does so the choice can be re-read later.

**`crates/` is deleted** (tan-cli#269): the two crates, `Cargo.toml`,
`Cargo.lock`, the Rust-oracle parity suite under `python/tests/parity/`, and
the five cargo CI jobs. `tan` is the sole planner and executor, which is the
end state ADR-0020 names bar one item — alp-sdk still carries its own Python
planner copy.

The three-step order this was gated on, all now done:

1. **Out of the release path — at `v0.5.0-rc1`.** The RC ships PyInstaller
   freezes and no cargo build at all. After that tag, nothing customers receive
   is Rust.
2. **Out of the correctness path.** The oracle's observed behaviour was frozen
   into committed captures *while a working binary still existed* — the only
   item that got harder the longer it waited, since anything never captured is
   unrecoverable once the binary is gone. Those captures survive the deletion,
   at `python/tests/fixtures/oracle_captures/`; a frozen answer also beats a
   live oracle, which can itself drift.
3. **Out of the repo.** `contract/` was the one thing that could have been
   silently un-enforced by the deletion, since `crates/tan-cli/tests/
   contract.rs` was one of its two gates. It is not: `python/tests/
   conformance/test_contract_envelopes.py` AUTO-DISCOVERS every case via
   `CONTRACT.iterdir()`, where the Rust side listed 17 by hand — same 17 cases,
   and a new one is gated with nothing to remember.

Issues whose fix lived in `crates/` and was never ported die with it. Each
carries a comment saying what survived the port and what did not.

### Alp IDE — `v0.5.x` beta, `v0.6.x` stable

The extension's pre-release line already consumes Python `tan` candidates; its
stable cutover remains the delivery mechanism. The remaining work is the stable
pin, the Linux target triple/archive handling, and an honest message on the two
platforms a Python `tan` release does not publish.

### alp-sdk — unchanged cadence

`alp-sdk` is upstream and independent; none of the above changes how it ships.
Its only obligations to the rest of the toolchain are to keep firing the
`alp-sdk-planner-change` dispatch when its contract surface moves, and — much
later, at `tan` `v0.7.0` — to drop its own Python planner once `tan` owns
planning outright.

## Standing rules

Internal. Each is here because it has already cost a round.

- **The oracle is gone; cite a CAPTURE, never a recollection.** The rule used
  to be "measure the oracle by RUNNING `target/debug/tan.exe`, never by reading
  `crates/` or docs" — source-reading gave a wrong answer twice. `crates/` is
  deleted (tan-cli#269), so there is nothing left to run: what an oracle
  behaviour WAS is now answerable only from `python/tests/fixtures/
  oracle_captures/`, `contract/envelopes/*`, or a provenance comment in
  `python/tan/**` that records the original measurement. If none of those
  covers the question, the answer is not recoverable — decide the behaviour on
  its merits and say so, rather than asserting what the oracle "would have"
  done.
- **A conclusion is not a measurement.** Two full rounds of wrong work came from
  believing a stale comment over the adjacent example.
- **Parity is measured against `PINNED_SDK_TAG`, on a clean LF-native clone.**
  A dirty or differently-reffed `alp-sdk` produces confident nonsense in both
  directions.
- **`contract/` is live shared API data** — edit it when the Python emit-site
  gates and consumer compatibility rules require the change. Its sole enforcer
  is now `python/tests/conformance/test_contract_envelopes.py`; keep that
  suite's auto-discovery intact rather than replacing it with a hand-written
  case list.
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
- tan-cli gates on its own pytest suite (`python -m pytest tests -q` from
  `python/`) — the cargo checks that used to sit beside it are gone with
  `crates/`. alp-sdk gates on `bash scripts/test-all.sh`. alp-sdk-vscode gates
  on its pnpm suite. They are not interchangeable.
