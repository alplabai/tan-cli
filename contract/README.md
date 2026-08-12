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

**One case is the declared exception to that last sentence** —
`validate-offline-clean` (tan-cli#498). Its `test_envelope_matches_expected`
run is `pytest.mark.xfail(strict=True)`, so a NEW drift in that envelope beyond
the declared one stays green there; the live pin for its shape is
`test_validate_command.py::test_text_and_json_formats_are_unchanged`, not this
fixture. The five `debug-config-preview-*` goldens used to be exceptions too;
they were re-recorded against the shipping CLI under tan-cli#502 and are real
gates again (see "Why the five `debug-config-preview-*` goldens were
re-recorded" below).

**There is exactly ONE enforcer, and deleting the other did not weaken it.**
`crates/tan-cli/tests/contract.rs` used to run the same fixtures against the
frozen Rust v0.4.1 oracle as a secondary compatibility check; tan-cli#269
deleted it with the rest of `crates/`. That is not a coverage loss:
`contract.rs` named its 17 cases in 17 hand-written `contract_case!` lines,
while `test_contract_envelopes.py` AUTO-DISCOVERS the same 17 by walking
`CONTRACT.iterdir()` — so the Python gate already covered everything the Rust
one did, and it is the gate a NEW case is covered by with nothing to remember
to add.

**What DID change is that the five `debug-config-preview-*` goldens are no
longer frozen.** The reason they were frozen was structural: those
`expected.json` files were `crates/`'s golden too, `crates/` was frozen, and
regenerating them measurably reddened `contract.rs` on every platform. That
blocker went with `contract.rs`, so re-recording them became an ordinary change
to this directory rather than something policy forbids — which is exactly what
tan-cli#502 does: all five are re-recorded against the shipping Python CLI and
gated normally again, with no `xfail` left on any of them. A re-recorded golden
must carry a `PROVENANCE.txt` (see "Fixture shape" below); one without
provenance is indistinguishable from a laundered one.

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
otherwise reds that gate outright. The Rust `contract.rs` gate used to retain
responsibility for entries owned by the frozen oracle; tan-cli#269 deleted it,
so any registry entry whose `emittedBy` still points into `crates/` is now
unowned and should be repointed or dropped. The release workflow publishes the
registry. Renaming or removing a
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
| `data.generatedAt`, `.summary.{pass,warn,fail}`, `.checks[].{name,status,scope,detail,fix?}`, `.missingPrerequisites[].{tool,command}`, `.nextSteps` | `doctor` (both invocations) | `python/tests/commands/test_doctor_command.py` — KEY-SET assertions, not a golden, because doctor's values are host facts. `test_a_scrubbed_host_exits_4_with_exactly_one_envelope_and_no_traceback` reads `name`/`status` off the spawned envelope and pins `data`'s key set; `test_unknown_is_counted_in_no_summary_bucket` pins the three summary buckets; `test_collect_leads_the_report_with_the_build_preflight_and_fails_a_workspaceless_host` pins the literal `workspace`. (The Rust `doctor_build_data_keys_the_extension_reads` cited here until #601 went with `crates/`.) **As of tan-cli#664, this key set is also PUBLISHED** — `envelope-contract.json`'s `envelopes.doctor` (built from `contract/doctor-data-keys.json`, the single source) — kept in lockstep with the shipping command by `python/tests/conformance/test_doctor_contract_key_set.py`, which runs a real `tan doctor --format json` and fails on either an undeclared emitted key or a declared key the command stopped emitting. See "The `doctor` family is a key set, not a golden" below. |
| `data.checks[].scope` | `doctor` (both invocations); also the `support-bundle` FILE's `doctor.checks[]`, not that command's envelope | `python/tests/gates/test_doctor_check_scope.py` + `test_every_check_on_the_wire_carries_a_scope` — see "`doctor` check scope" below |
| `data.written` | `build --materialise` | **NOT COVERED.** Reaching it needs a resolvable alp-sdk checkout and a Python spawn; nothing in this suite is allowed either. |
| `data.releases` | `sdk list` | **NOT COVERED.** Hits the GitHub releases API. |
| `data.configuration` (the `launch.json` entry alp-sdk-vscode#342 writes verbatim) | `debug-config` | goldens `debug-config-preview-{zephyr-mcu,zephyr-mcu-sdk-identity,baremetal-mcu,native-host,yocto-userspace}` — one per `--target-kind`, re-recorded against the shipping CLI under tan-cli#502 and no longer `xfail`'d, so an added key or a changed `program`/`executable` reds here. Oracle-parity fixtures additionally covered the bare `zephyr-mcu` invocation (all three servers) and `native-host`, but they consumed `crates/` and were deleted with it in tan-cli#269; these goldens are what survived. |

The `build --materialise` and `sdk list` rows are stated rather than quietly
omitted: an uncovered field that reads as covered is worse than one everybody
knows about.

### `doctor` check scope, and why `--build` needs no second spawn (#549)

**Every `data.checks[]` entry carries `scope`, one of exactly two values:
`host` or `project`.** A consumer splitting the report into "facts about this
machine" and "facts about the project you opened" reads that field. It must
never go back to matching `checks[].name`.

**This covers `support-bundle` too, not only `doctor`.**
`tan support-bundle` builds its debug report from the same `Check` type, so
every entry in the WRITTEN BUNDLE's `doctor.checks[]` carries `scope` on the
same two values. Its own check names (`workspaceRoot`, `sdkRoot`,
`cortexDebugExtension`, `{server}Backend`, `gdb`, `lldb`, …) are classified by
the same rule below. That command's stdout ENVELOPE is unaffected — its `data`
carries `outputPath`/`targetKind`/`server`/`decisionCount` and no `checks[]` at
all — so the shape change is in the attached file only. Stated rather than left
to be discovered: the field arrived for `doctor`'s consumer, but it changed
`support-bundle`'s output in the same commit, and a shape change nobody wrote
down is what this file exists to prevent.

| Value | The check's verdict is about |
|---|---|
| `host` | This machine: a tool on PATH, an OS setting, the host interpreter, the home directory, whether the pinned Zephyr SDK publishes a build for this OS/arch. Worth rendering with no folder open. |
| `project` | The selected project, the resolved alp-sdk checkout, or the Zephyr workspace built for it: is a `board.yaml` there, did a workspace resolve, does its Zephyr match the SDK's pin, where did this venv/SDK come from. |

A project fact may still refine a `host` check — `hostPython`'s floor is the
higher of the SDK manifest's `pythonMinVersion` and the workspace Zephyr's —
without changing what the verdict is *about*. The rule is the subject, not the
inputs.

**This field is additive.** It is a new key on an existing object; `name`,
`status`, `detail` and `fix` are untouched, no key was removed or renamed, and
the field is present on every entry rather than sometimes. A consumer on an
older pin ignores it; a consumer on a newer pin reading an older tan's envelope
sees it absent, which is the same `undefined` its current allowlist code
already handles. No golden moved: there is no `doctor` case under
`envelopes/`, and none of the committed goldens contains a `checks[]` array.

**The scope list is deliberately NOT reproduced here.** Which names are `host`
today is exactly the hand-list this field exists to delete. The rule above and
the field are the contract; `python/tests/gates/test_doctor_check_scope.py`
pins the current classification inside this repo so a reclassification is a
reviewed edit rather than a silent one.

**Why it was needed:** the extension carried a hand-written list of tan's check
names. Between v0.4.0 and 0.5.1 `zephyrSdkHost` was renamed
`zephyrSdkAvailableForHost`; the stale entry matched nothing, nothing failed,
and the row it was meant to admit was simply never admitted — which reads to a
user as "not a problem" rather than "not asked" (alp-sdk-vscode#472, patched
downstream in alp-sdk-vscode#487).

**`doctor` and `doctor --build` emit the same checks.** `--build` gated exactly
one check (`zephyrWorkspace`) and stopped gating it in tan-cli#290; it is
accepted for compatibility and reads nowhere. A consumer needs ONE invocation,
not two merged. Pinned by `test_build_and_plain_doctor_emit_the_same_checks`
(unit) and `test_build_and_plain_doctor_put_the_same_checks_on_the_wire`
(spawned binary), so this is a promise rather than an observation about one
release. Changing it means changing those tests.

Two sentences that stood here previously — that `tan doctor` WITHOUT `--build`
emits "a different check vocabulary (`workspaceRoot`, `lldb`, `longPaths`, …)"
— described the frozen v0.4.1 oracle, not the shipping Python CLI. On the
Python CLI `workspaceRoot` and `lldb` are `support-bundle`'s checks and are not
emitted by `doctor` at all, and `longPaths` is emitted by both invocations on
Windows. The check names themselves remain unfrozen: no consumer should match
them now that `scope` exists.

### The `doctor` family is a key set, not a golden (tan-cli#664)

Every family under `envelopes` before tan-cli#664 was a byte golden: fixed
`args`, a fixed `exitCode`, and a fixed `envelope` a real run must match
exactly. `doctor` cannot work that way — its `data` VALUES are host facts
(installed tool versions, absolute paths, which checks even apply on this
machine), which is exactly why no `envelopes/doctor/` fixture directory has
ever existed here (see "`doctor` check scope" above: "there is no `doctor`
case under `envelopes/`"). What alp-sdk-vscode actually binds to is not any
one of those values — it is the **key set**: `packages/alp-core/src/deps/
planner.ts` reads `data.missingPrerequisites`, and the debug troubleshooting
panel renders every `checks[].fix` and `data.nextSteps` verbatim
(alp-sdk-vscode#491). Before tan-cli#664 that key set was pinned only inside
this repo (`test_doctor_command.py`, `test_doctor_check_scope.py`) — real
protection for tan's OWN behaviour, but never shipped where a consumer could
gate against it, which is the gap issue #664 was filed to close.

So `doctor`'s entry in `envelopes` carries `dataKeys` (the required `data` key
set, each key's value a TYPE description — `"string"`, `"int"` — never a
literal) instead of `exitCode`/`envelope`. `contract/doctor-data-keys.json` is
the single source (its own `_comment` records how it was enumerated and why
`status` stays a free string rather than a pinned pass/warn/fail/unknown
enum — a value added later must survive the trip); the release workflow folds
it into `envelope-contract.json` verbatim, the same way it already folds
`issue-codes.json`. `python/tests/conformance/test_doctor_contract_key_set.py`
is what keeps the two from drifting apart: it spawns a real `tan doctor
--format json` and fails if the emitted key set and the declared one disagree,
in EITHER direction — an emitted key nobody declared, or a declared key the
command stopped emitting, both fail loudly rather than reading as "no
problem". Renaming or dropping a declared key is the same breaking wire
change `contract/README.md`'s frozen-issue-code rule describes: bump the CLI
MAJOR/MINOR, record it in `CHANGELOG.md`, and open the matching
alp-sdk-vscode issue.

**Left to the maintainer, not settled here (tan-cli#664):** on every run
measured so far, `data.nextSteps` has been byte-identical to the ordered list
of non-null `checks[].fix` values (`next_steps()`'s own dedup logic:
one entry per DISTINCT fix, in check order — the same order `checks[]`
already carries). Whether that is a guaranteed, intentional relationship or
an incidental one nothing enforces is not decided by this key-set entry
either way — it only promises the two keys both exist and are arrays of
strings. Also left alone: `checks[].status`'s current pass/warn/fail/unknown
vocabulary and `checks[].scope`'s host/project vocabulary are documented
elsewhere in this file (see "`doctor` check scope" above) but deliberately
NOT re-pinned as an enum in `contract/doctor-data-keys.json` — the key SET is
the contract this entry publishes, not the value vocabulary.

### Published as a release asset

Every tagged release carries **`envelope-contract.json`** beside the binaries:

```jsonc
{
  "schemaVersion": 1,
  "tanVersion": "0.4.0",
  "issueCodes": [ /* issue-codes.json, verbatim */ ],
  "envelopes": {
    "presets-heterogeneous-som": { "args": [...], "exitCode": 0, "envelope": { ... } },
    // …one entry per golden case
    "doctor": { "args": [...], "dataKeys": { /* contract/doctor-data-keys.json's dataKeys, verbatim */ } }
  }
}
```

Built by the `Bundle the envelope contract` step in
`.github/workflows/release.yml` — pure re-packaging of committed files gated by
the Python conformance/issue-code tests. It
exists so the extension's own contract test diffs against a
published artefact instead of a hand-copied fixture that drifts. Fetch it at
`https://github.com/alplabai/tan-cli/releases/download/<tag>/envelope-contract.json`.

The packaging step re-packages `expected.json` files, not `tan`'s live output,
so **what the asset advertises is exactly as accurate as the goldens are**. The
five `debug-config-preview-*` entries used to be the counter-example: they
carried frozen-oracle values, so the asset told a consumer that
`data.configuration.preLaunchTask` and `yocto-userspace`'s
`debug-config.gdbserver-address-unresolved` issue never appear — while the same
asset's `issueCodes` list published that very code. Both are corrected as of
tan-cli#502; those five now carry the shipping CLI's own output.

**Residual limitation:** `validate-offline-clean` is still recorded from the
frozen oracle (`xfail`'d, tan-cli#498), so that one entry advertises an exit-0
"clean" envelope for a `board.yaml` the shipping CLI refuses with exit 2
`validate.schema-violation`. The rule for telling them apart: a golden with a
`PROVENANCE.txt` beside it is a re-recording against the shipping CLI; a golden
named in `DELIBERATE_DIVERGENCE` is not.

## Fixture shape (`envelopes/<case>/`)

One directory per case, mirroring the retired `cli-rs/contract` harness:

| File | Contents |
|---|---|
| `args.txt` | The `tan` argv, **one token per line** (not shell-split — avoids quoting ambiguity across platforms). |
| `expected.json` | The full golden envelope, normalized (see below). |
| `expected.exit` | The golden process exit code, as a bare integer. |
| *(when re-recorded)* `PROVENANCE.txt` | Why this golden was re-recorded, when, against which `tan` version, what moved, and why the previous recording was wrong. **Required on any golden re-recorded against the shipping CLI** — a re-recorded golden with no provenance is indistinguishable from a laundered one. Harness metadata, like the three rows above: skipped when fixture inputs are copied (`CASE_METADATA`). |
| *(optional)* `board.yaml` / other fixture inputs | Copied into the isolated working directory the case runs in before `tan` is spawned. **Directories are copied recursively**, which is what lets a case ship a synthetic `sdk/` checkout (`scripts/alp_project.py` + `metadata/…` + `examples/…`) and pass `--sdk-root ./sdk`. That relative argv keeps the "no absolute paths in argv" rule intact — `data.sdkRoot` comes back as the literal `./sdk` on every platform. |

`contract/fixtures/` (sibling directory) is not an envelope-golden directory.
It holds shared synthetic SDK, bootstrap-manifest, and toolchain inputs.
`contract/fixtures/bootstrap/manifest.json` is a vendored copy of alp-sdk's
`metadata/bootstrap.json`, re-vendored at `PINNED_SDK_TAG` by tan-cli#585. It
is verified data, not frozen data: `python/tests/commands/test_bootstrap_command.py`
asserts `tan.core.bootstrap.fallback_facts` equals it field for field (no
exemptions) and that no instruction in it names a subcommand in
`sdk_cmd.NOT_PORTED_SDK_SUBCOMMANDS`. `tests/parity/bootstrap_manifest_parity.py`
byte-diffs it against an SDK checkout by hand.

`contract/doctor-data-keys.json` (another sibling, tan-cli#664) is also not an
`envelopes/<case>/` directory, for the reason above: `doctor`'s `data` cannot
be a golden. It is the single source the release workflow folds into
`envelopes.doctor` verbatim — see "The `doctor` family is a key set, not a
golden" above.

## What a golden does NOT cover: key ORDER

The Python harness compares parsed dictionaries, whose equality is
**order-insensitive** (the retired Rust harness compared `serde_json::Value`s
with the same property). A golden therefore pins the key set and values, never
emission order. Key order is a real contract for commands
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
| `validate-offline-empty-document` | `validate --offline --format json` (empty fixture `board.yaml`) | 2 | An empty/comment-only document used to report exit 0 "clean" — pins that the shipping Python CLI refuses it, message and exit code alike, as the frozen Rust oracle did when it was recorded. |
| `sdk-current-no-sdk` | `sdk current --format json` | 0 | Reports `sourceTier: "none"` in a workspace with no SDK configured — offline, host-independent given the isolated `HOME`. |
| `sdk-unknown-subcommand` | `sdk bogus --format json` | 1 | Runtime-failure envelope shape; the only offline path that exercises exit code 1 in this set. |
| `generate-board-yaml-missing` | `generate --format json` (no `board.yaml` present) | 2 | `generate`'s `data` schema (`{schemaVersion,targets,written,failed}`) is distinct from `init`'s and was otherwise completely unguarded — this is the first guard clause in `python/tan/commands/generate_cmd.py`, needing no board/SDK/network to reach. |
| `debug-config-preview-zephyr-mcu` | `debug-config --target-kind zephyr-mcu --server jlink --preview` | 0 | |
| `debug-config-preview-zephyr-mcu-sdk-identity` | `debug-config --target-kind zephyr-mcu --server jlink --core m55_hp --sdk-root ./sdk --preview` (fixture SDK) | 0 | |
| `debug-config-preview-baremetal-mcu` | `debug-config --target-kind baremetal-mcu --server openocd --preview` | 0 | |
| `debug-config-preview-yocto-userspace` | `debug-config --target-kind yocto-userspace --server gdbserver --preview` | 0 | |
| `debug-config-preview-native-host` | `debug-config --target-kind native-host --server none --preview` | 0 | One profile per `--target-kind`. Unlike the other cases these pin a `data` value that is itself a consumer ARTEFACT, not a report: alp-sdk-vscode#342 writes `data.configuration` into `launch.json` verbatim, so the golden pins the emitted key SET — an added key or a changed `program`/`executable` fails here instead of shipping. **All five were re-recorded against the shipping Python CLI under tan-cli#502 and carry a `PROVENANCE.txt`; none is `xfail`'d any more — see "Why the five `debug-config-preview-*` goldens were re-recorded" right below this table.** `--preview` reads no `board.yaml`, spawns no Python and probes no PATH; the only host-dependent output is the absolute working directory, tokenized as `__WORKDIR__` above. |
| `presets-no-sdk` | `presets --format json` (no SDK resolvable) | 0 | Pins the `presets.sdk-root-unresolved` warning ON THE WIRE — the one frozen issue code reachable hermetically — plus the full `PresetsData` key set with `soms: []`. |
| `presets-heterogeneous-som` | `presets --sdk-root ./sdk --format json` (fixture SDK) | 0 | Issue #106's worked example made executable. The fixture SoM has an `a55` (`machine:` → yocto) and an `m33` (`board:` → zephyr), so `data.soms[].cores[].{id,os}` carries two different values — rename `soms` or `cores` and this fails instead of quietly scaffolding a multi-core part single-core with no IPC. Also pins `boardLibraries` discovery. |
| `explain-overview` | `explain --format json` | 0 | `data.available.projectTemplates`, the New Project wizard's starter list. Fully hermetic — the catalogues are static, no SDK involved. |
| `examples-catalog` | `examples --sdk-root ./sdk --format json` (fixture SDK) | 0 | `data.examples[].sourceDir`, which is what `tan init --from-example <sourceDir>` is handed back; a rename breaks scaffolding from an SDK example. Also pins README-derived `title`/`description`. |
| version-format tests (no fixture dir) | `--version` | 0 | `python/tests/test_cli_skeleton.py` asserts the format rather than a literal version that changes every release. (A Rust mirror of it existed until tan-cli#269 deleted `crates/`.) |
| issue-code gates (no fixture dir) | — | — | Python AST gates check the shipping emit sites. They prove spelling/registration, while command tests prove reachability. The Rust half, which checked the registry entries the frozen oracle owned, went with `crates/` in tan-cli#269 — see the `emittedBy` note under "Frozen issue codes". |
| doctor `--build` key set (no fixture dir) | `doctor --build --format json` | — | KEY-SET assertion, not a value diff: doctor's values are host facts (what is on PATH, whether a Zephyr workspace exists), its key names are not. Covers `data.summary.{pass,warn,fail}`, `data.nextSteps`, `data.checks[].{name,status}` and the literal check name `workspace`, in `python/tests/commands/test_doctor_command.py` — the envelope `data` key set and `summary`'s `{pass,warn,fail}` shape, plus the build preflight's leading check names (`sdk`, `boardYaml`, `workspace`) in `test_collect_leads_the_report_with_the_build_preflight_and_fails_a_workspaceless_host`. The single named Rust assertion that used to own this row, `doctor_build_data_keys_the_extension_reads`, went with `crates/` in tan-cli#269; the Python coverage is spread across that module rather than concentrated in one test. |
| `doctor` published key set (`contract/doctor-data-keys.json`, no `envelopes/` fixture dir) | `doctor --format json` | — | tan-cli#664: the SAME key-set fact as the row above, but PUBLISHED into `envelope-contract.json`'s `envelopes.doctor.dataKeys` rather than pinned only inside this repo. See "The `doctor` family is a key set, not a golden" above. Kept honest by `python/tests/conformance/test_doctor_contract_key_set.py`, which spawns a real `tan doctor --format json` and fails on either an emitted key this file doesn't declare or a declared key the command stopped emitting. |

### Why the five `debug-config-preview-*` goldens were re-recorded (tan-cli#502)

All five `debug-config-preview-*` goldens above were **re-recorded against the
shipping Python CLI** (`tan 0.5.2-rc1.dev0`) on 2026-08-09 and are no longer
`xfail`'d — `data.configuration` and `issues[]` are real gates again on every
one of the five, including the three (`zephyr-mcu-sdk-identity`,
`baremetal-mcu`, `yocto-userspace`) that never had an oracle-parity fixture at
all. Each case carries a `PROVENANCE.txt` recording what moved, when, against
which version, and why the previous recording was wrong.

What they were re-recorded FROM, and why that recording was wrong — two
distinct, unrelated causes, both legitimate shipped-behaviour changes:

- **`zephyr-mcu`, `zephyr-mcu-sdk-identity`, `baremetal-mcu`, `native-host`**
  (tan-cli#138): `data.configuration.preLaunchTask` is present and the old
  golden omitted it. tan-cli#85 had made it opt-in; #138 restored the v0.3.1
  default for the three build target kinds because alp-sdk-vscode's task
  providers depend on exactly these labels (`docs/DEBUG.md:326,342,360,392`
  there) and never pass `--pre-launch-task`, so shipping without the default
  silently breaks build-then-debug. Emitted values, verbatim: `alp: build
  active target` (both `zephyr-mcu` cases), `alp: build baremetal target`,
  `alp: build native_sim target`. Live unit pins for the value itself:
  `python/tests/core/test_debug_launch.py` (`DEFAULT_PRE_LAUNCH_TASK`, key
  position via `runToEntryPoint`/`preLaunchTask` adjacency) and
  `python/tests/commands/test_debug_config_command.py` at the envelope level
  for `zephyr-mcu`.

  **Why re-recording all five mattered, and not just the two the cause-grouping
  makes salient: oracle-parity coverage of the REST of the envelope was
  narrower PER CASE than that grouping suggests, and is now zero.** It came
  from
  `python/tests/parity/test_oracle_parity.py::test_debug_config_resolution_matches_rust`
  and `::test_debug_config_native_host_preview_global_format_matches_rust`,
  both deleted with the oracle suite in tan-cli#269. They diffed the WHOLE
  envelope against the live frozen oracle with `preLaunchTask` stripped out
  first, so any OTHER field drifting (a changed `executable`, a dropped
  `servertype`, a stray key) failed there — but only for the **bare**
  `zephyr-mcu` invocation (jlink/openocd/pyocd, no `--sdk-root`/`--core`) and
  for `native-host`. `zephyr-mcu-sdk-identity` shares the `preLaunchTask` cause
  but was a DIFFERENT parity case (it resolves `configuration.device` and
  `project.boardYaml` off the fixture SDK, which the bare `zephyr-mcu` case
  does not exercise), and it never had a full-envelope parity fixture; that
  resolution is unit-pinned in
  `python/tests/commands/test_debug_config_command.py` and nowhere else.
  `baremetal-mcu`'s full envelope never had one either — only its
  `preLaunchTask` value was unit-pinned. So for those two cases an
  unrelated drift anywhere in `data.configuration` was unguarded outside the
  golden, and once the parity tests went with `crates/` these re-recorded
  goldens became the only envelope-level gate they have. Extending the parity
  parametrization was filed as tan-cli#529 while that suite still existed; the
  suite is gone, so the goldens are the coverage.
- **`yocto-userspace`** (tan-cli#321, an unrelated cause from the four above):
  `data.configuration` was already correct — this target deliberately gets no
  `preLaunchTask` default at all — but the envelope also carries a
  `debug-config.gdbserver-address-unresolved` info issue (registered
  `reserved`/`consumer: none` in `issue-codes.json`) when the default
  `<host>:<port>` placeholder is still unresolved, and the old golden said
  `issues: []`. That made the published asset self-contradictory: it listed the
  code in `issueCodes` while its envelope told the consumer this command
  reports nothing.

Both were previously handled by DECLARING the difference as a strict xfail
rather than re-recording. That is what this change reverses, and the reason is
not tidiness: a strict xfail fails only on XPASS, so it pinned "this envelope
differs from its golden somehow" and nothing finer. Every other field in all
five — a changed `executable`, a dropped `servertype`, a stray new key — was
unguarded for as long as the declaration stood, and `data.configuration` is the
value alp-sdk-vscode#342 writes into `launch.json` verbatim. The stale values
also shipped: `release.yml`'s `Bundle the envelope contract` step re-packages
`expected.json` into the published `envelope-contract.json` asset.

Recorded because it was the entire objection to re-recording for as long as it
applied, and because it is the evidence these five goldens really did move: the
frozen v0.4.1 oracle emitted neither the `preLaunchTask` nor the
`gdbserver-address-unresolved` issue, so `crates/tan-cli/tests/contract.rs` —
which held that oracle to these same `expected.json` files — went from **24
passed; 0 failed** to **19 passed; 5 failed** when the re-record was first
measured in-tree on 2026-08-09, the five failures being exactly these cases and
nothing else. That harness, and the `cargo test --locked` CI job that ran it,
were deleted in tan-cli#269. The disagreement therefore no longer exists
anywhere: the Python conformance harness is the only gate over these goldens,
it pins the SHIPPING wire, and the release assets ARE the Python program
(PyInstaller freezes of `python/`, tan-cli#271). Nothing green depends on these
five matching the retired oracle any more — and re-recording is what stops
`data.configuration` losing its last envelope-level gate on the three cases
(`zephyr-mcu-sdk-identity`, `baremetal-mcu`, `yocto-userspace`) that never had
a parity fixture.

Deliberately **outside the envelope**: nothing, as of tan-cli#399's close-out.
`faultdecode` was the one verb here — its `--format json` used to print the
SDK's unwrapped fault report
(`fault_detected`/`inputs`/`flags`/`addresses`/`root_cause`/`symbols`)
verbatim, the output contract it inherited from the retired
stdio-inheriting forward to `python -m alp_cli faultdecode` (the oracle mapped
the global `--format json` onto the child's own `--json`, in the
since-deleted `crates/tan-cli/src/commands/sdk_cli.rs`) — but
`faultdecode_cmd.py` now
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

1. Run the case's `args.txt` through the **shipping Python CLI** by hand from an
   empty directory, with `SOURCE_DATE_EPOCH=0` and `HOME`/`USERPROFILE` pointed
   at another empty directory, `--format json`. (Reuse the conformance
   harness's own `fresh_dir` / `copy_fixture_inputs` / `normalise` if you can —
   that is how the tan-cli#502 re-record was captured, and it removes any chance
   of the recording and the comparison disagreeing about isolation.)
2. Copy the printed envelope into `expected.json`, converting any `\` path
   separator to `/` (Windows only — Unix output is already normalized).
3. Update `expected.exit` if the exit code changed.
4. **Write or update the case's `PROVENANCE.txt`** — what was re-recorded, on
   what date, against which `tan` version, and why the previous recording was
   wrong. This is not optional bookkeeping: a re-recorded golden with no
   provenance is indistinguishable from a laundered one.
5. Re-run `cd python && python -m pytest
   tests/conformance/test_contract_envelopes.py -q`. That is the whole gate —
   the second, `cargo`-side harness this step used to name went with `crates/`
   in tan-cli#269.
6. Explain the *intentional* shape change in the commit message — a golden
   update with no explanation of why the wire format changed is exactly the
   drift this gate exists to catch.
