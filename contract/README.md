<!-- SPDX-License-Identifier: Apache-2.0 -->
# `contract/` — the JSON envelope drift gate

The vscode extension drives `tan <cmd> --format json` and hard-depends on
three things that nothing else in this repo pins:

- the top-level envelope shape, `{ command, ok, exitCode, project, data,
  issues }` (`crates/tan-cli/src/envelope.rs`);
- the exit-code contract — 0 success, 1 runtime, 2 validation, 3 write, 4
  doctor, 5 internal (`crates/tan-cli/src/exit.rs`);
- `tan --version`'s first stdout line, `tan MAJOR.MINOR.PATCH`.

`crates/tan-cli/tests/contract.rs` (run by `cargo test`, part of the normal
CI `test` job — no separate CI wiring needed) spawns the real, compiled `tan`
binary against the golden fixtures in `envelopes/` below and diffs the
result. A breaking wire-format change fails `cargo test` here instead of
being discovered later, silently, in the extension.

This is a **Rust integration test, not a shell script** (unlike the retired
`cli-rs/contract/run.sh`): `cargo test` already runs it cross-platform (this
repo's CI test job matrixes ubuntu/windows/macos-latest — a bash harness
would need a second execution path on Windows CI runners for no benefit),
needs no new CI job, and gets `cargo`'s own binary discovery
(`CARGO_BIN_EXE_tan`) for free instead of a hand-rolled `target/debug/tan(.exe)`
path.

## Fixture shape (`envelopes/<case>/`)

One directory per case, mirroring the retired `cli-rs/contract` harness:

| File | Contents |
|---|---|
| `args.txt` | The `tan` argv, **one token per line** (not shell-split — avoids quoting ambiguity across platforms). |
| `expected.json` | The full golden envelope, normalized (see below). |
| `expected.exit` | The golden process exit code, as a bare integer. |
| *(optional)* `board.yaml` / other fixture inputs | Copied into the isolated working directory the case runs in before `tan` is spawned. |

`contract/fixtures/` (sibling directory) is unrelated — it holds a synthetic
SDK checkout tree consumed by `crates/tan-cli/src/commands/presets.rs`'s own
unit tests, not an envelope golden.

## Determinism

The harness makes every case reproducible on any machine or CI runner, not
just the one that captured it:

- **Isolated working directory** — each case runs in a fresh, empty temp
  directory, never inside the checkout. `tan init`'s create/update file diff
  and `tan`'s sibling-`alp-sdk` auto-discovery both read the current
  directory's contents, so running inside the repo tree would make a golden
  depend on incidental files at the checkout location.
- **Isolated working-directory PARENT** — that fresh directory is itself
  nested under its own fresh, uniquely named parent
  (`.../tan-contract-<case>-<pid>/root`), never spawned directly under the
  shared system temp root. `discover_workspace_sdk` (tan-core `project.rs`)
  probes the working directory's *parent* for a sibling `alp-sdk/`; if that
  parent were the shared temp root, a stray `alp-sdk` checkout left there by
  something else could flip a golden's `sourceTier` to `discovery`.
- **Isolated `HOME`/`USERPROFILE`** — also a fresh temp directory per case,
  so a developer's real `~/.alp/sdk-default` (or lack of one) can never
  change what `sdk current` reports.
- **`SOURCE_DATE_EPOCH=0`** — honored by `crate::util::generated_at_iso`; set
  unconditionally even though none of the current cases emit a timestamp, so
  a future timestamped case is covered without touching the harness.
- **No absolute paths in argv** — every case invokes `tan` without
  `--project`/`--sdk-root`/`--destination`, so path fields the CLI reflects
  back (`project.root`, `boardYamlPath`, …) come out as `.`/`./board.yaml`
  rather than an absolute, machine-specific path. Nothing needed a
  `__SDKROOT__`-style substitution token (the convention `tests/parity/`
  uses) as a result.
- **Scoped path-separator normalization** — the one thing case selection
  can't avoid by construction: `PathBuf::to_string_lossy()` renders
  `./board.yaml` as `.\board.yaml` on Windows. The harness normalizes
  `\` → `/` on the freshly captured side before diffing, but only on the
  known path-shaped fields (`root`, `boardYaml`, `boardYamlPath`,
  `destination`, `relativePath`, `sdkPath`, `sdkPinned`, `written`,
  `unchanged` — see `PATH_KEYS` in `contract.rs`), not every string leaf.
  A blanket rewrite would also launder a real drift inside `issues[].message`
  or any other value that happens to contain a backslash — exactly the kind
  of change this gate exists to catch. Committed goldens are authored with
  forward slashes in those fields, matching the normalized form.

## Cases pinned today

| Case | Command | Exit | Why |
|---|---|---|---|
| `init-preview-minimal-app` | `init --template minimal-app --preview --format json` | 0 | Deterministic scaffold plan — the envelope shape `init` templates hand the extension (`{schemaVersion,templateId,destination,preview,fileChanges,written,unchanged,sdkPinned}`). |
| `init-invalid-template` | `init --template bogus-template --format json` | 2 | Validation-failure envelope shape for `init`. |
| `validate-offline-clean` | `validate --offline --format json` (fixture `board.yaml`) | 0 | The offline structural validator's clean-outcome envelope — no Python/SDK spawn, so it's genuinely deterministic and network-free. |
| `validate-offline-schema-violation` | `validate --offline --format json` (malformed fixture `board.yaml`) | 2 | Same command, non-clean outcome — pins the `issues[]` shape too. |
| `sdk-current-no-sdk` | `sdk current --format json` | 0 | Reports `sourceTier: "none"` in a workspace with no SDK configured — offline, host-independent given the isolated `HOME`. |
| `sdk-unknown-subcommand` | `sdk bogus --format json` | 1 | Runtime-failure envelope shape; the only offline path that exercises exit code 1 in this set. |
| `generate-board-yaml-missing` | `generate --format json` (no `board.yaml` present) | 2 | `generate`'s `data` schema (`{schemaVersion,targets,written,failed}`) is distinct from `init`'s and was otherwise completely unguarded — this is `generate`'s first guard clause (`commands/generate.rs`'s `run()`), needing no board/SDK/Python/network to reach. |
| `version_first_line_matches_contract` (in `contract.rs`, no fixture dir) | `--version` | 0 | Not a golden diff — `tan MAJOR.MINOR.PATCH` would need editing on every release if pinned literally, so the test asserts the *format* instead. |

Deliberately not covered: `sdk list` (hits the GitHub releases API — network),
`doctor` (probes the host toolchain — host-dependent). Not exhaustive by
design — this pins the envelope *shape* + exit-code contract for the
commands the extension actually parses (validate, init/generate templates,
sdk), not full command coverage.

## Regenerating a golden after a *deliberate* envelope change

There is no `--bless` flag (the retired shell harness had one; the Rust
suite doesn't need the extra code for six fixtures). To update a golden on
purpose:

1. Build `tan` and run the case's `args.txt` by hand from an empty directory,
   with `SOURCE_DATE_EPOCH=0` and `HOME`/`USERPROFILE` pointed at another
   empty directory, `--format json`.
2. Copy the printed envelope into `expected.json`, converting any `\` path
   separator to `/` (Windows only — Unix output is already normalized).
3. Update `expected.exit` if the exit code changed.
4. Re-run `cargo test -p tan --test contract` and confirm it passes.
5. Explain the *intentional* shape change in the commit message — a golden
   update with no explanation of why the wire format changed is exactly the
   drift this gate exists to catch.
