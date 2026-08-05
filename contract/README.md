<!-- SPDX-License-Identifier: Apache-2.0 -->
# `contract/` — the JSON envelope drift gate

The vscode extension drives `tan <cmd> --format json` and hard-depends on
five things that nothing else in this repo pins:

- the top-level envelope shape, `{ command, ok, exitCode, project, data,
  issues }` (`python/tan/envelope.py`);
- the exit-code contract — 0 success, 1 runtime, 2 validation, 3 write, 4
  doctor, 5 internal (`python/tan/exit_codes.py`);
- `tan --version`'s first stdout line, `tan MAJOR.MINOR.PATCH`;
- the **frozen issue codes** it matches with `===` (`issue-codes.json`, below);
- the **`data` field names** it reads with `?? []` fallbacks (below).

`python/tests/conformance/test_contract_envelopes.py` runs every committed
fixture against `python -m tan`, the implementation that release assets ship,
and diffs the complete envelope and process exit code. It runs in the normal
Python CI job. A breaking shipping-wire change therefore fails before it can be
discovered silently in the extension.

`crates/tan-cli/tests/contract.rs` still runs the same fixtures against the
frozen Rust v0.4.1 oracle. It is a secondary compatibility check and also owns
the registry entries whose `emittedBy` path still points into `crates/`; it is
not the shipping gate. Both harnesses are native cross-platform tests rather
than shell scripts.

## The frozen wire vocabulary (issue #106)

`tan`'s envelope is a **versioned public contract**, not an implementation
detail. Two parts of it are matched by string on the consumer side, and both
matches **fail open**: an unrecognised issue code returns "no verdict" and a
missing `data` key falls back to `?? []`. The extension does not error, does
not log and does not warn — it silently skips the check or renders stale
data, with CI green on both sides. A rename here is therefore
indistinguishable from "no problem" until a customer hits it.

**Do not fix a rename by loosening the consumer.** A prefix match on
`bootstrap.` would swallow codes the extension has no verdict for. The
contract belongs to whoever owns the envelope: this repo.

### Exit codes (`python/tan/exit_codes.py`)

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Runtime failure (I/O, subprocess) |
| 2 | Validation failure (schema/semantic) |
| 3 | Write failure |
| 4 | `doctor` reported an unhealthy environment |
| 5 | Internal error (bug / unreachable state) |

### Frozen issue codes (`issue-codes.json`)

`issue-codes.json` is the single source. Python's
`test_every_issue_code_is_registered.py` scans shipping emit sites in the
source→registry direction, and `test_frozen_issue_codes.py` checks every
Python-owned registry entry in the registry→source direction.
`test_issue_code_registry_shape.py` checks a third, orthogonal thing: every
registered `code` matches alp-sdk-vscode's `ISSUE_CODE_SHAPE`,
`^[a-z][a-z0-9-]*\.[a-z0-9-]+$` (lowercase-kebab) — the shape the consumer's
`findFrozenCodes` hard-asserts on every published entry, so a code shaped
otherwise reds that gate outright. The Rust
`contract.rs` gate retains responsibility only for entries owned by the frozen
oracle. The release workflow publishes the combined registry. Renaming or
removing a
`status: "frozen"` code is a **breaking wire change** — bump the CLI
MAJOR/MINOR, record it in `CHANGELOG.md`, and open the matching
alp-sdk-vscode issue. A `status: "reserved"` code has no consumer yet
(`consumer: "none"`): the gate still checks the spelling exists at the
emission site, but renaming or dropping it costs nothing on the wire —
promote it to `frozen` the moment a consumer actually binds to it.

**Selection criterion for the table below: `frozen`/`retired` codes only** —
the ones where a rename or removal is the actual breaking wire change this
whole file exists to guard against. `reserved` codes are cheap to rename by
definition (nothing binds them with `===` yet), there are more of them than
usefully fit a table, and `issue-codes.json` is already their single source
with a full `consumerEffect` per entry — this table does not duplicate them.

| Code | Status | Consumer effect if renamed |
|---|---|---|
| `bootstrap.windows-unsupported` (severity `error`) | retired | Emitted by tan ≤ v0.3.0 only. The consumer branch is permanent back-compat for anyone pinned to an old binary via `alpSdk.cliPath`, so the spelling is RESERVED and must never be re-used for a different verdict. |
| `bootstrap.yocto-host` (severity `error`) | frozen | A Yocto-only project is sent into a bootstrap that cannot work on this host. The mixed-board case reuses the suffix at severity `warning` and must stay a warning. |
| `bootstrap.prerequisites-missing` (severity `error`) | frozen | tan's own refusal is not recognised, so the extension spawns the real bootstrap terminal anyway and the customer watches the identical failure scroll past with the install guidance lost. |
| `presets.sdk-root-unresolved` (severity `warning`) | frozen | The New Project wizard silently falls back to its static catalogue, which carries no `cores`, so a **heterogeneous SoM scaffolds single-core with no IPC**. The reference part E1M-AEN801 is multi-core, so that is the default path. |
| `bootstrap.python-not-runnable` (severity `error`) | frozen | `python`/`python3` resolves on PATH but will not run (a Microsoft Store alias, or similar). Renamed, `alp-sdk-vscode`'s `prerequisitesMissingIssue` (`PREREQ_CODES`, `src/alpCli/service.ts`) no longer recognises tan's own refusal, so the extension spawns the real bootstrap terminal anyway and the customer watches the identical failure scroll past with the install guidance lost — same failure shape as `bootstrap.prerequisites-missing`. Carries no `missingPrerequisites[]` entry: a `{tool, command}` pair cannot represent "the Python you have will not run", so the fix travels only in `issues[].message`. |
| `bootstrap.python-too-old` (severity `error`) | frozen | The resolved Python is below the SDK tooling's floor (currently >= 3.10). Same consumer and the same failure shape as `bootstrap.python-not-runnable`; also tool-less. |

`bootstrap.prerequisites-missing`, `bootstrap.python-not-runnable` and
`bootstrap.python-too-old` are the three codes `alp-sdk-vscode`'s
`prerequisitesMissingIssue` matches (`PREREQ_CODES`, a `Set` matched with
`.has()` — equivalent to `===` for this purpose) to stop it spawning a real
bootstrap that has already been refused. The latter two carry no missing
TOOL at all, so `missingPrerequisites[]` is always empty for them; the fix
travels only in `issues[].message` (see `python/tan/core/bootstrap.py`).

Every other registered code is `reserved` — no consumer binds any of them yet,
so renaming or dropping one costs nothing on the wire. The registry contains
the complete, current list; duplicating hundreds of reserved entries here would
create a second list that immediately drifts.

### Frozen `data` field names, and exactly what covers each

| Field the extension reads | Command family | Gated by |
|---|---|---|
| `data.soms[]`, `.sku`, `.displayName`, `.family`, `.cores[].{id,os}` | `presets` | golden `presets-heterogeneous-som` (a55/yocto + m33/zephyr) |
| `data.sdkRoot`, `.skus`, `.libraries`, `.boardLibraries`, … | `presets` | goldens `presets-no-sdk` + `presets-heterogeneous-som` |
| `data.available.projectTemplates` (+ `moduleTemplates`, `generationTargets`), `data.summary`, `data.details` | `explain` | golden `explain-overview` |
| `data.examples[].{id,sourceDir,title,description}` | `examples` | golden `examples-catalog` |
| `data.targets` / `.written` / `.failed` | `generate` | golden `generate-board-yaml-missing` |
| `data.checks[].{name,status}`, `data.summary.{pass,warn,fail}`, `data.nextSteps`, the literal check name `workspace` | `doctor --build` | `doctor_build_data_keys_the_extension_reads` — a KEY-SET assertion, not a golden, because doctor's values are host facts |
| `data.written` | `build --materialise` | **NOT COVERED.** Reaching it needs a resolvable alp-sdk checkout and a Python spawn; nothing in this suite is allowed either. |
| `data.releases` | `sdk list` | **NOT COVERED.** Hits the GitHub releases API. |

The last two rows are stated rather than quietly omitted: an uncovered field
that reads as covered is worse than one everybody knows about.

`tan doctor` WITHOUT `--build` emits a different check vocabulary
(`workspaceRoot`, `lldb`, `longPaths`, …). No consumer matches those by name,
so they are deliberately not frozen.

### Published as a release asset

Every tagged release carries **`envelope-contract.json`** beside the binaries:

```jsonc
{
  "schemaVersion": 1,
  "tanVersion": "0.4.0",
  "issueCodes": [ /* issue-codes.json, verbatim */ ],
  "envelopes": {
    "presets-heterogeneous-som": { "args": [...], "exitCode": 0, "envelope": { ... } }
    // …one entry per golden case
  }
}
```

Built by the `Bundle the envelope contract` step in
`.github/workflows/release.yml` — pure re-packaging of committed files gated by
the Python conformance/issue-code tests and the frozen Rust oracle tests. It
exists so the extension's own contract test diffs against a
published artefact instead of a hand-copied fixture that drifts. Fetch it at
`https://github.com/alplabai/tan-cli/releases/download/<tag>/envelope-contract.json`.

## Fixture shape (`envelopes/<case>/`)

One directory per case, mirroring the retired `cli-rs/contract` harness:

| File | Contents |
|---|---|
| `args.txt` | The `tan` argv, **one token per line** (not shell-split — avoids quoting ambiguity across platforms). |
| `expected.json` | The full golden envelope, normalized (see below). |
| `expected.exit` | The golden process exit code, as a bare integer. |
| *(optional)* `board.yaml` / other fixture inputs | Copied into the isolated working directory the case runs in before `tan` is spawned. **Directories are copied recursively**, which is what lets a case ship a synthetic `sdk/` checkout (`scripts/alp_project.py` + `metadata/…` + `examples/…`) and pass `--sdk-root ./sdk`. That relative argv keeps the "no absolute paths in argv" rule intact — `data.sdkRoot` comes back as the literal `./sdk` on every platform. |

`contract/fixtures/` (sibling directory) is not an envelope-golden directory.
It holds shared synthetic SDK, bootstrap-manifest, and toolchain inputs used by
Python and Rust unit/parity tests.

## What a golden does NOT cover: key ORDER

The Python harness compares parsed dictionaries, whose equality is
**order-insensitive**; the Rust oracle harness compares
`serde_json::Value`s with the same property. A golden therefore pins the key
set and values, never emission order. Key order is a real contract for commands
that mirror TS output; pin it with a serialized-string assertion in the owning
Python module's tests, not here.

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
  shared system temp root. `discover_workspace_sdk`
  (`python/tan/commands/sdk_cmd.py`)
  probes the working directory's *parent* for a sibling `alp-sdk/`; if that
  parent were the shared temp root, a stray `alp-sdk` checkout left there by
  something else could flip a golden's `sourceTier` to `discovery`.
- **Isolated `HOME`/`USERPROFILE`** — also a fresh temp directory per case,
  so a developer's real `~/.alp/sdk-default` (or lack of one) can never
  change what `sdk current` reports.
- **`SOURCE_DATE_EPOCH=0`** — honored by
  `python/tan/core/timestamp.py::generated_at_iso`; set
  unconditionally even though none of the current cases emit a timestamp, so
  a future timestamped case is covered without touching the harness.
- **No absolute paths in argv** — every case invokes `tan` without
  `--project`/`--sdk-root`/`--destination`, so path fields the CLI reflects
  back (`project.root`, `boardYamlPath`, …) come out as `.`/`./board.yaml`
  rather than an absolute, machine-specific path. Nothing needed a
  `__SDKROOT__`-style substitution token (the convention `tests/parity/`
  uses) as a result.
- **Scoped path-separator normalization** — the one thing case selection
  can't avoid by construction: Windows path rendering produces
  `./board.yaml` as `.\board.yaml` on Windows. The harness normalizes
  `\` → `/` on the freshly captured side before diffing, but only on the
  known path-shaped fields (`root`, `boardYaml`, `boardYamlPath`,
  `destination`, `relativePath`, `sdkPath`, `sdkPinned`, `written`,
  `unchanged`, `launchJsonPath` — see `PATH_KEYS` in the Python conformance
  harness), not every
  string leaf. A blanket rewrite would also launder a real drift inside
  `issues[].message` or any other value that happens to contain a backslash —
  exactly the kind of change this gate exists to catch. Committed goldens are
  authored with forward slashes in those fields, matching the normalized form.
- **`__WORKDIR__` for a reflected absolute path** — the one case the
  "no absolute paths in argv" rule above cannot cover: `debug-config` reports
  the working directory it resolved (`project.root`) and the `launch.json`
  path it would write, absolute, whatever the argv. Those two fields are
  substituted down to the `__WORKDIR__` token on the captured side. The
  substitution anchors on the case's unique scratch-dir marker
  (`tan-contract-<case>-<pid>/root`) rather than on the harness's own
  `work_dir` string, because on macOS `$TMPDIR` is a symlink that
  `std::env::current_dir()` resolves through (`/var/…` → `/private/var/…`) and
  a whole-prefix comparison would silently stop matching there. Like the
  separator rewrite, it applies to `PATH_KEYS` fields only.

## Cases pinned today

| Case | Command | Exit | Why |
|---|---|---|---|
| `init-preview-minimal-app` | `init --template minimal-app --preview --format json` | 0 | Deterministic scaffold plan — the envelope shape `init` templates hand the extension (`{schemaVersion,templateId,destination,preview,fileChanges,written,unchanged,sdkPinned}`). |
| `init-invalid-template` | `init --template bogus-template --format json` | 2 | Validation-failure envelope shape for `init`. |
| `validate-offline-clean` | `validate --offline --format json` (fixture `board.yaml`) | 0 | The offline structural validator's clean-outcome envelope — no Python/SDK spawn, so it's genuinely deterministic and network-free. |
| `validate-offline-schema-violation` | `validate --offline --format json` (malformed fixture `board.yaml`) | 2 | Same command, non-clean outcome — pins the `issues[]` shape too. |
| `validate-offline-empty-document` | `validate --offline --format json` (empty fixture `board.yaml`) | 2 | An empty/comment-only document used to report exit 0 "clean" — pins that both the shipping Python CLI and frozen Rust oracle refuse it, message and exit code alike. |
| `sdk-current-no-sdk` | `sdk current --format json` | 0 | Reports `sourceTier: "none"` in a workspace with no SDK configured — offline, host-independent given the isolated `HOME`. |
| `sdk-unknown-subcommand` | `sdk bogus --format json` | 1 | Runtime-failure envelope shape; the only offline path that exercises exit code 1 in this set. |
| `generate-board-yaml-missing` | `generate --format json` (no `board.yaml` present) | 2 | `generate`'s `data` schema (`{schemaVersion,targets,written,failed}`) is distinct from `init`'s and was otherwise completely unguarded — this is the first guard clause in `python/tan/commands/generate_cmd.py`, needing no board/SDK/network to reach. |
| `debug-config-preview-zephyr-mcu` | `debug-config --target-kind zephyr-mcu --server jlink --preview` | 0 | |
| `debug-config-preview-baremetal-mcu` | `debug-config --target-kind baremetal-mcu --server openocd --preview` | 0 | |
| `debug-config-preview-yocto-userspace` | `debug-config --target-kind yocto-userspace --server gdbserver --preview` | 0 | |
| `debug-config-preview-native-host` | `debug-config --target-kind native-host --server none --preview` | 0 | One profile per `--target-kind`. Unlike the other cases these pin a `data` value that is itself a consumer ARTEFACT, not a report: alp-sdk-vscode#342 writes `data.configuration` into `launch.json` verbatim, so the golden pins the emitted key SET — an added key (the `preLaunchTask` these fixtures were added after, which named a task nothing defines and made VS Code abort pre-launch) or a changed `program`/`executable` fails here instead of shipping. `--preview` reads no `board.yaml`, spawns no Python and probes no PATH; the only host-dependent output is the absolute working directory, tokenized as `__WORKDIR__` above. |
| `presets-no-sdk` | `presets --format json` (no SDK resolvable) | 0 | Pins the `presets.sdk-root-unresolved` warning ON THE WIRE — the one frozen issue code reachable hermetically — plus the full `PresetsData` key set with `soms: []`. |
| `presets-heterogeneous-som` | `presets --sdk-root ./sdk --format json` (fixture SDK) | 0 | Issue #106's worked example made executable. The fixture SoM has an `a55` (`machine:` → yocto) and an `m33` (`board:` → zephyr), so `data.soms[].cores[].{id,os}` carries two different values — rename `soms` or `cores` and this fails instead of quietly scaffolding a multi-core part single-core with no IPC. Also pins `boardLibraries` discovery. |
| `explain-overview` | `explain --format json` | 0 | `data.available.projectTemplates`, the New Project wizard's starter list. Fully hermetic — the catalogues are static, no SDK involved. |
| `examples-catalog` | `examples --sdk-root ./sdk --format json` (fixture SDK) | 0 | `data.examples[].sourceDir`, which is what `tan init --from-example <sourceDir>` is handed back; a rename breaks scaffolding from an SDK example. Also pins README-derived `title`/`description`. |
| version-format tests (no fixture dir) | `--version` | 0 | `python/tests/test_cli_skeleton.py` and the Rust mirror assert the format rather than a literal version that changes every release. |
| issue-code gates (no fixture dir) | — | — | Python AST gates check the shipping emit sites; `contract.rs` checks Rust-owned registry entries. They prove spelling/registration, while command tests prove reachability. |
| `doctor_build_data_keys_the_extension_reads` (in `contract.rs`, no fixture dir) | `doctor --build --format json` | — | KEY-SET assertion, not a value diff: doctor's values are host facts (what is on PATH, whether a Zephyr workspace exists), its key names are not. Covers `data.summary.{pass,warn,fail}`, `data.nextSteps`, `data.checks[].{name,status}` and the literal check name `workspace`. |

Deliberately **outside the envelope**: nothing, as of tan-cli#399's close-out.
`faultdecode` was the one verb here — its `--format json` used to print the
SDK's unwrapped fault report
(`fault_detected`/`inputs`/`flags`/`addresses`/`root_cause`/`symbols`)
verbatim, the output contract it inherited from the retired
stdio-inheriting forward to `python -m alp_cli faultdecode` (the oracle maps
the global `--format json` onto the child's own `--json`,
`crates/tan-cli/src/commands/sdk_cli.rs`) — but `faultdecode_cmd.py` now
treats `--format json` as a second, distinct spelling that wraps the report in
the standard envelope; its OWN `--json` flag is the one that stays the
unwrapped compatibility surface, unchanged, so a saved script or a pipe using
`--json` still receives exactly the bytes it always did. `new-som`, in the
same unenveloped state until the same issue, closed the same way.

This is also pinned in code, not just prose: `python/tests/
test_cli_skeleton.py`'s `_ENVELOPE_SHAPE_EXEMPT` enumerates the registered
command table and asserts every command's `--format json` carries the
`{command,ok,exitCode,project,data,issues}` shape — EMPTY today, since
`faultdecode` was its only entry. Should a future command need an exemption,
name it there AND in this paragraph together — one without the other is
exactly the drift tan-cli#399 was filed about.

Deliberately not covered: `sdk list` (hits the GitHub releases API — network),
`build --materialise`'s `data.written` (needs a resolvable SDK + a Python
spawn), `kconfig` (the SDK's
`--emit kconfig` needs a bootstrapped `ZEPHYR_BASE` — alp-sdk's one
workspace-dependent emit, see alp-sdk `docs/cli.md`; `tan kconfig`'s pure
JSON→`KconfigData`→envelope shaping is unit-tested hermetically in
`python/tan/commands/kconfig_cmd.py` and its tests instead). Not exhaustive by
design — this pins the envelope *shape* +
exit-code contract for the commands the extension actually parses, not full
command coverage. What is uncovered is listed rather than omitted: silence
reading as coverage is how an inert gate survives.

## Regenerating a golden after a *deliberate* envelope change

There is no `--bless` flag. To update a golden on
purpose:

1. Build `tan` and run the case's `args.txt` by hand from an empty directory,
   with `SOURCE_DATE_EPOCH=0` and `HOME`/`USERPROFILE` pointed at another
   empty directory, `--format json`.
2. Copy the printed envelope into `expected.json`, converting any `\` path
   separator to `/` (Windows only — Unix output is already normalized).
3. Update `expected.exit` if the exit code changed.
4. Re-run `cd python && python -m pytest
   tests/conformance/test_contract_envelopes.py -q`. If the deliberate change
   also updates the frozen oracle contract, run `cargo test --locked -p
   alp-tan-cli --test contract` too.
5. Explain the *intentional* shape change in the commit message — a golden
   update with no explanation of why the wire format changed is exactly the
   drift this gate exists to catch.
