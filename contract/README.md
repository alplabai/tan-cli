<!-- SPDX-License-Identifier: Apache-2.0 -->
# `contract/` — the JSON envelope drift gate

The vscode extension drives `tan <cmd> --format json` and hard-depends on
six things that nothing else in this repo pins:

- the top-level envelope shape, `{ command, ok, exitCode, project, data,
  issues }` (`python/tan/envelope.py`);
- the exit-code contract — 0 success, 1 runtime, 2 validation, 3 write, 4
  doctor, 5 internal (`python/tan/exit_codes.py`);
- `tan --version`'s first stdout line, `tan MAJOR.MINOR.PATCH`;
- the **frozen issue codes** it matches with `===` (`issue-codes.json`, below);
- the **`data` field names** it reads with `?? []` fallbacks (below);
- the **`(inert:KIND)` marker** in `--help`, which it records into its own
  `test/golden/tan-surface/surface.json` (below). Not an envelope field — the
  extension reads `--help` text for its option surface — but a wire fact all
  the same, and the one thing on this list a consumer used to have to INFER.

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
| `data.soms[]`, `.sku`, `.displayName`, `.family`, `.cores[].{id,os}` | `presets` | golden `presets-heterogeneous-som` (a55/yocto + m33/zephyr). The same `.cores[]` entries also carry `type`/`allowedOs` on the wire (tan-cli#870, additive at `schemaVersion` `"1"`) -- `type` is the raw `metadata/socs/**/*.json` `cores[].type` string, `allowedOs` is that type's excluded cross-class OS subtracted from the checkout's own `board.schema.json` enum via `tan.core.os_class`, the same cortex-a/cortex-m convention `tan.planner.validate` gates a build on for the cross-class exclusion itself -- narrower for an UNRESOLVED core type, where `allowedOs` degrades to `[]` but the build-time gate still accepts `baremetal`/`off` (deliberate: presets offers nothing rather than guess) -- but as of this writing no consumer reads either field yet (`git grep allowedOs` over `alp-sdk-vscode@dev` is empty; `src/ideHub/messages.ts`'s `cores?: { id: string; os: string }[]` declares neither): present on the wire type, not among the fields this consumer actually reads (see the `build --plan` row's `schemaVersion` for the same distinction). `allowedOs` degrades to `[]` on a schema miss alone; `type` degrades to `""` on any of four SoC-lookup misses (measured): an unresolvable `silicon:` (not a `vendor:family:part` triple), a resolvable `silicon:` with no matching `metadata/socs/**` file, a resolved SoC JSON whose `cores[]` either has no entry for this core's id or has one whose `type` key is itself empty or absent, or (tan-cli#957) an entry whose `type` key is present but not a JSON string at all -- a schema-invalid tree `soc-spec-v1.schema.json` itself forbids but does not stop `tan` from reading, since nothing validates the checkout before `presets` does -- neither field fails the command over it, and `type` never carries the raw non-string value onto the wire either. The `presets-heterogeneous-som` fixture's own shape pins both degraded values at once, not populated ones (its `silicon:` is not a `vendor:family:part` triple, which degrades `type`, and its `sdk/` tree carries no `board.schema.json`, which independently degrades `allowedOs`), but exercises only the NO-SCHEMA path for `allowedOs`: both conditions co-occur in that one fixture, and the missing-schema check short-circuits `allowed_os_lookup` before the unresolved `type` is ever consulted, so this golden does not exercise (and would not catch a regression in) the separate unresolved-core-type guard -- deleting that guard still leaves this golden green (measured); the RESOLVED (non-empty) shape, and the unresolved-core-type guard specifically, are covered at the unit level in `python/tests/commands/test_presets_command.py`, not by a golden. |
| `data.sdkRoot`, `.skus`, `.libraries`, `.boardLibraries`, … | `presets` | goldens `presets-no-sdk` + `presets-heterogeneous-som` |
| `data.available.projectTemplates` (+ `moduleTemplates`, `generationTargets`), `data.summary`, `data.details` | `explain` | golden `explain-overview` |
| `data.som.{initAcceptsSkus,initRefusesSkuPrefixes}` | `explain --template <project-template-id>` | golden `explain-template-iot-starter` (tan-cli#866). Present only on a PROJECT-template hit (the one selector kind with a SoM concept) — absent, not `null`, for a module-template or generation-target hit, the same absent-vs-null convention `--code`'s `data.diagnostic`/`data.suggestions` already use. **These name a REFUSAL prediction — "will `tan init` accept/refuse this `--som`" — not a capability statement (PR #985 review, major 1); do not read them as "the SDK supports this pair".** `initAcceptsSkus` is the exact-SKU allowlist `init.invalid-som` refuses against (`null` when the template carries none); `initRefusesSkuPrefixes` is the family-tree exclusion `init.som-unsupported` refuses against (always `[]` when `initAcceptsSkus` is set, since an exact allowlist already implies it). Both are read from `tan.core.scaffold`'s `TEMPLATE_SUPPORTED_SKUS`/`UNSUPPORTED_SOM_FAMILY_PREFIXES` — the SAME tables `tan init` gates on — not a second, hand-typed copy. For every template except `iot-starter`/`multicore-mailbox`, `initRefusesSkuPrefixes` (`["E1M-NX9"]`) is provably WIDER than alp-sdk's own scaffold-catalog `supported.som_skus` (`["E1M-AEN801", "E1M-V2N101"]`, `tan/templates/vendored/MANIFEST.md`) — `tan init` accepts a `--som` alp-sdk's catalog never validated this template against, by design (`_family_bucket`'s unrecognised-prefix fallback), and this field says so correctly rather than inventing a narrower vocabulary tan cannot verify without a checkout. `tests/commands/test_explain_command.py::test_every_sku_mentioned_in_template_prose_matches_its_structured_som_data` gates every SKU literal in project-template prose (not just an "only"-adjacent one) against drifting from `TEMPLATE_SUPPORTED_SKUS`, with a named exemption (`_CONTRAST_MENTIONS`) for `iot-starter`'s deliberate `E1M-V2N101` contrast mention. |
| `data.examples[].{id,sourceDir,title,description}` | `examples` | golden `examples-catalog` |
| `data.examples[].{category,som,board,cores[].{id,os,app},coreCount,osSet,declares}` (tan-cli#484, additive at `schemaVersion` `"1"`) | `examples` | golden `examples-catalog-facets`. Present PER-ROW only when the bound checkout's generated `metadata/catalog.json` has an entry for that row's `sourceDir` (`tan.core.example_facets.load_example_facets`) -- an older SDK, a stale catalogue missing a newer example, or a catalogue that fails to parse all degrade to the pre-#484 four-key row, never a failure. `category` is the only one of these ALWAYS present once any facet is: the catalogue's own grouping key. Every other field is independently optional and OMITTED (never emitted `null`) exactly as alp-sdk's own generator omits it -- `cores`/`coreCount`/`osSet` come from that catalogue's topology resolver (`gen_catalog.py::_resolved_core_facets`, the same `alp_project.py --emit os-topology` path `tan build` itself plans against) and are absent together when a board's topology does not resolve; `som`/`board` are absent for a portable example that declares neither. `category` is ALREADY read (measured against `alp-sdk-vscode@origin/dev`): `packages/alp-core/src/examples/category.ts`'s `exampleCategory()` prefers an explicit `ex.category` over its own `sourceDir`-leading-segment fallback the moment one is on the wire, so this row promotes that field from "derived client-side" to "sent by the producer" with no extension change needed. `som`/`board`/`cores`/`coreCount`/`osSet`/`declares` are pre-consumer as of the same measurement (`git grep -n 'coreCount\|osSet\|declares' -- src/ packages/` over `alp-sdk-vscode@origin/dev` matches nothing on this envelope) -- present on the wire ahead of the consumer landing, same distinction the `presets` `type`/`allowedOs` row above notes. |
| `data.targets` / `.written` / `.failed` | `generate` | golden `generate-board-yaml-missing` |
| `data.generatedAt`, `.summary.{pass,warn,fail}`, `.checks[].{name,status,scope,detail,fix?}`, `.missingPrerequisites[].{tool,command}`, `.nextSteps` | `doctor` (both invocations) | `python/tests/commands/test_doctor_command.py` — KEY-SET assertions, not a golden, because doctor's values are host facts. `test_a_scrubbed_host_exits_4_with_exactly_one_envelope_and_no_traceback` reads `name`/`status` off the spawned envelope and pins `data`'s key set; `test_unknown_is_counted_in_no_summary_bucket` pins the three summary buckets; `test_collect_leads_the_report_with_the_build_preflight_and_fails_a_workspaceless_host` pins the literal `workspace`. (The Rust `doctor_build_data_keys_the_extension_reads` cited here until #601 went with `crates/`.) **As of tan-cli#664, this key set is also PUBLISHED** — `envelope-contract.json`'s `envelopes.doctor` (built from `contract/doctor-data-keys.json`, the single source) — kept in lockstep with the shipping command by `python/tests/conformance/test_doctor_contract_key_set.py`, which runs a real `tan doctor --format json` and fails on either an undeclared emitted key or a declared key the command stopped emitting. See "The `doctor` family is a key set, not a golden" below. |
| `data.checks[].scope` | `doctor` (both invocations); also the `support-bundle` FILE's `doctor.checks[]`, not that command's envelope | `python/tests/gates/test_doctor_check_scope.py` + `test_every_check_on_the_wire_carries_a_scope` — see "`doctor` check scope" below |
| `data.written` | `build --materialise` | **NOT COVERED.** Reaching it needs a resolvable alp-sdk checkout and a Python spawn; nothing in this suite is allowed either. |
| `data.releases[].{tag,publishedAt,tarballUrl,releaseNotesSummary,releaseNotes,draft,prerelease}`, `data.subcommand` | `sdk list` | **PUBLISHED key set** (`contract/sdk-list-data-keys.json` → `envelope-contract.json`'s `envelopes.sdk-list`), tan-cli#887. Not a byte golden and not a fixture dir, for the same reason `doctor` is not: the values are whatever alp-sdk has published on GitHub at the moment of the call. Kept in lockstep by `python/tests/conformance/test_sdk_list_contract_key_set.py`, which runs the real command with **only the socket replaced** and fails on an emitted key nobody declared or a declared key the command stopped emitting. It does NOT prove GitHub still sends what tan reads — nothing offline can — only that the payload→wire mapping is the declared one. `draft`/`prerelease` are always real booleans, defaulting to `false` when the payload omits them (tan-cli#122), and `releases` is `[]`, never absent, on the no-`--online` and fetch-failure paths. |
| `data.{subcommand,removed,path,version,wasActive,freedBytes}` | `sdk remove` | golden `sdk-remove-absent` (tan-cli#790), the one fully offline/hermetic case: an idempotent no-op on a target that was never there, `removed: false`. `data.path` is the resolved absolute target — new to `PATH_KEYS`, so it normalises to `__WORKDIR__` the same way `boardYamlPath`/`launchJsonPath` already do. The six refusal/failure codes (`sdk.remove-missing-argument`, `sdk.remove-outside-root`, `sdk.remove-is-cache-root`, `sdk.remove-active`, `sdk.remove-in-use`, `sdk.remove-permission` — flat, one dash, no dot; `contract/issue-codes.json`'s own entries explain why a nested `sdk.remove.<reason>` shape is not on this wire at all) and a real successful removal all need a filesystem the harness cannot pre-seed hermetically (a real install to delete, a real lock/permission to trip) — covered instead by `python/tests/commands/test_sdk_command.py`'s own removal-behaviour tests, including mutation-proved read-only-directory, `wasActive`-on-refusal, outside-root-refusal, and cache-root-itself-refusal cases. |
| `data.sku`, `.boardYaml`, `.slices[].{coreId,backend,buildDir,env,command,configArtefacts[].{path,contents}}`, `.sharedArtefacts[].{path,contents}`, `.warnings[].{code,coreId,message}` | `build --plan` | **NOT COVERED, and now permanently unreachable.** tan-cli#427 RETIRED the flag rather than implementing it -- "Two overlapping plan surfaces is worse than one, so the oracle spellings go" (the maintainer's own decision). `tan build --plan --format json` answers `ok:false`, `exitCode:2`, `build.flag-retired`, naming `--plan-from` (plus `--materialise`/`--execute` to act on it) as the replacement in the refusal message itself -- the refusal envelope's `data` carries only that message, never plan data, and never will through this flag. Two different producers sat behind this row before the retirement, and only one of them survives (tan-cli#853): `--plan-from` (reachable today, unchanged by #427; `_acquire_plan`, `build_cmd.py:874`, called at `:1534`) reads the caller's own plan FILE and echoes it verbatim at exit 0 once it parses -- an unreadable file still refuses -- returning BEFORE `apply_plan_token_substitution` runs (`_MODE_PLAN`'s early return); a passthrough of the caller's file, not this emitter, so a golden recorded from it pins whatever fixture it was handed, tokens or not. `--plan` itself would instead have echoed whatever `emit_build_plan` (`python/tan/planner/buildplan.py:375`) renders in-process -- MEASURED, before the retirement, against a real board (`emit('build-plan', ..., board_yaml=examples/multicore/rpmsg-v2n/board.yaml)`): the plan was tagged `"planPathMode": "tokened"` and every slice's `env.ALP_SDK_ROOT` / `envAppendPath.{EXTRA_ZEPHYR_MODULES,PYTHONPATH}` carried a literal, UNSUBSTITUTED `${SDK_ROOT}` token (`buildplan.py:610,620-623,636`) -- emitter facts, not fields this consumer currently binds (`alp-sdk-vscode@dev`'s `BuildPlanData`/`BuildPlanSlice`, `src/ideHub/messages.ts:456-481`, declare neither); the same held for `schemaVersion`, which the TS interface DOES declare but which `packages/alp-core/src/tanPayloadShape.ts`'s `BUILD_PLAN_SHAPE` names explicitly "NOT read on this path". That whole paragraph is kept here as the HISTORICAL record of what this row's `data` shape would have been, not a live description: with `--plan` retired, the in-process planner's own raw JSON is dispatched (not shown) by an ordinary `tan build`, and no other flag echoes it unsubstituted, so nothing above is reachable through any `tan build` invocation any more. `src/ideHub/buildPlanPanel.ts` (tan-cli#200) has nothing left to read from `build --plan`; if a future flag resurrects in-process plan display, re-measure rather than trust this paragraph. |
| `data.slices[].{core_id,os,status,flash_method,build_dir,board,machine,image,app,reason,output_artefact,log_path}`, `.ipc[].{name,kind,endpoints[],status,reason}`, `.helper_mcus[].{name,chip,flash_method,firmware_path}` | `build --manifest`, `build --manifest-from <path>` | **NOT COVERED, and now permanently unreachable.** tan-cli#427 RETIRED both flags rather than implementing them: a native `tan build` already writes `build/system-manifest.yaml` (plain YAML, readable directly), so neither flag was implemented on top of it. `tan build --manifest --format json` / `--manifest-from <path>` each answer `ok:false`, `exitCode:2`, `build.flag-retired`, naming that file as the replacement in the refusal message itself -- never `cli.command-deferred`/exit 1 any more. Note for whoever freezes `system-manifest.yaml`'s own shape instead (the artefact these flags would have echoed): the extension matches the literal `"TBD"` on `slices[].flash_method`, `helper_mcus.flash_method` and `helper_mcus.firmware_path` to gate its Flash button, and matches `slices[].os === "off"` to decide whether a core participates -- both are load-bearing wire VALUES, not placeholders tan may change freely. |
| `data.slices[]` keyed by `.core_id`; `.status`, `.flash`, `.ram` (each `{used,total,pct}`, any member `null`), `.budget_note` | `size` | **NOT COVERED.** The command is reachable but needs a built ELF and a manifest -- a bare run answers `ok:false`, `exitCode:1`, `size.manifest-unavailable`. `slices[].status` is matched BY VALUE (`not-built`, `n/a`, `over`, `warn`, `no-budget`; anything else renders as "in budget"), so those strings are wire content. |
| `data.configuration` (the `launch.json` entry alp-sdk-vscode#342 writes verbatim) | `debug-config` | goldens `debug-config-preview-{zephyr-mcu,zephyr-mcu-sdk-identity,baremetal-mcu,native-host,yocto-userspace}` — one per `--target-kind`, re-recorded against the shipping CLI under tan-cli#502 and no longer `xfail`'d, so an added key or a changed `program`/`executable` reds here. Oracle-parity fixtures additionally covered the bare `zephyr-mcu` invocation (all three servers) and `native-host`, but they consumed `crates/` and were deleted with it in tan-cli#269; these goldens are what survived. |
| `data.programsDevice` (additive at `schemaVersion` `"1"`), `data.configuration.loadFiles` (cortex-debug targets only) | `debug-config` | same five goldens, second re-record under tan-cli#945 (see "Why the five `debug-config-preview-*` goldens were re-recorded" below). tan-cli#945: a consumer had no way to tell, from the written profile alone, whether starting it programs the attached target — cortex-debug's own `loadFiles` schema default silently falls back to `executable` ("if this property does not exist, then the executable is used to program the device", `marus25.cortex-debug` 1.12.1), which alp-sdk-vscode#586 had to reimplement client-side because neither of its flash gates could see the write happen inside cortex-debug's own spawned probe server. `programsDevice` is `true` for `zephyr-mcu`/`baremetal-mcu`, `false` for `yocto-userspace` (a `cppdbg` attach to an already-deployed gdbserver) and `native-host` (no target hardware exists) — `tan.core.debug_launch.programs_device`, keyed on `targetKind` alone, so it is present on every outcome this command can report, including a validation failure with `configuration: null`. It is not necessarily *correct* on every one of those: the internal-failure backstop paths (`_internal_failure` and its siblings) report a fixed `zephyr-mcu`/`none` placeholder target that never learned what was actually asked for, so `programsDevice` on those is present and CONSERVATIVE (`true`, fail-safe) rather than an answer for the target the caller actually named (tan-cli#1020 review) — understating the write is the bug class this field exists to close; overstating it is merely unhelpful. The same conservative mismatch can also happen on an ordinary, successful write: `programsDevice` is keyed on `targetKind` alone, never on the `loadFiles` this particular write actually lands, so a merge that PROTECTS a customer's own explicit attach-only `[]` (see below) still reports `programsDevice: true` beside `data.configuration.loadFiles: []` in the same exit-0 payload (tan-cli#1020 re-review) — a consumer that needs per-write precision reads `data.configuration.loadFiles` itself rather than trusting `programsDevice` alone for that one case. `loadFiles` is emitted explicitly on every cortex-debug draft, naming the SAME artefact `executable` does, and both are kept in sync by `apply_launch_resolution` once a real build resolves one. A hand-authored `loadFiles` already in the file — an explicit `[]` for attach-only included — is protected on a rerun rather than merged or overwritten unless `.alp/debug-launch-provenance.json` proves tan wrote it itself; see `debug-config.load-files-preserved` below for the disclosure and `tan.core.debug_launch._merge_load_files` for the merge rule (tan-cli#1020 review). |

The four NOT COVERED rows -- `build --materialise`, `build --plan`,
`build --manifest*` and `size` -- are stated rather than quietly omitted: an
uncovered field that reads as covered is worse than one everybody knows about.
(`sdk list` was the fifth until tan-cli#887 published its key set. Worth
recording why it moved: its FIELDS were never wrong -- `draft` and `prerelease`
have been on the wire since `tan sdk` was first added, measured on the pinned
v0.6.0-rc1 -- but "emitted today" and "promised" are different things, and this
row said in as many words that it was the former.)
The last three were in neither list until tan-cli#200 found them, which is the
failure mode this paragraph exists to prevent, so it is worth saying plainly
that the rule needs applying when a command family is ADDED, not only when
coverage is dropped.

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
set) instead of `exitCode`/`envelope`. Every value under `dataKeys` is a
MACHINE TOKEN — `"string"`, `"int"`, `"string|null"` — never a prose sentence,
so a consumer can validate structurally without parsing English: `checks`
is `{requiredKeys, optionalKeys}` (today `optionalKeys` is just `fix`, which
`Check.as_dict()` omits — never nulls — when a check carries no remediation),
and `missingPrerequisites` is `{nullable: true, items: {tool, command}}`.
`contract/doctor-data-keys.json` is the single source (its own `_comment`
records how it was enumerated and why `status` stays a free string rather
than a pinned pass/warn/fail/unknown enum — a value added later must survive
the trip). The release workflow's "Bundle the envelope contract" step folds
only `args` and `dataKeys` from it into `envelope-contract.json` — not the
whole file: `_comment` stays a repo-only authoring note, never a published
field. (`issue-codes.json` is also folded partially, not whole-file
verbatim: `.github/workflows/release.yml`'s bundling step reads only that
file's own `issueCodes` array into the bundle's `issueCodes`, dropping
`issue-codes.json`'s own `schemaVersion` and `_comment` too — neither
contract source ships as its literal file.)
`python/tests/conformance/test_doctor_contract_key_set.py` is what keeps the
published file from drifting apart from the shipping command: it spawns a
real `tan doctor --format json`, derives every required/optional key set —
including `checks[]`'s `requiredKeys`/`optionalKeys` and
`missingPrerequisites[]`'s `items` — FROM `contract/doctor-data-keys.json`
itself (never from a constant of its own), and fails if the emitted key set
and the declared one disagree, in EITHER direction, at every level: an
emitted key nobody declared, or a declared key the command stopped emitting,
both fail loudly rather than reading as "no problem". Renaming or dropping a
declared key is the same breaking wire change `contract/README.md`'s
frozen-issue-code rule describes: bump the CLI MAJOR/MINOR, record it in
`CHANGELOG.md`, and open the matching alp-sdk-vscode issue.

**Settled here (tan-cli#664):** `data.nextSteps` is NOT guaranteed to equal
the ordered list of non-null `checks[].fix` values. `next_steps()`
(`python/tan/commands/doctor_cmd.py`) additionally DEDUPES — one entry per
DISTINCT fix, in check order — and skips `pass`/`unknown` checks even when
they carry a `fix`, so the two arrays can and do differ in length on a real
run: measured on the 2026-08-12 run this contract was enumerated against, 13
checks produced 8 non-null `fix` values but only 7 `nextSteps` entries (two
checks shared the fix `tan bootstrap`, which collapsed to one `nextSteps`
entry). A consumer that wants "the fix for check N" must read
`checks[N].fix`, not index into `nextSteps` — `nextSteps` is a deduplicated
action list, not a per-check parallel array. This key-set entry promises only
that both keys exist and are arrays of strings. Also left alone:
`checks[].status`'s current pass/warn/fail/unknown vocabulary and
`checks[].scope`'s host/project vocabulary are documented elsewhere in this
file (see "`doctor` check scope" above) but deliberately NOT re-pinned as an
enum in `contract/doctor-data-keys.json` — the key SET is the contract this
entry publishes, not the value vocabulary.

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
    "doctor": { "args": [...], "dataKeys": { /* contract/doctor-data-keys.json's dataKeys, verbatim */ } },
    "sdk-list": { "args": [...], "dataKeys": { /* contract/sdk-list-data-keys.json's dataKeys, verbatim */ } }
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

## Inert options and their kind (`--help`, tan-cli#886)

`tan` accepts a number of options it does not read. **Every one of them ends
its `--help` text with a marker naming WHICH KIND of inert it is**, rendered by
`python/tan/core/inert.py` and nothing else:

```
--no-auto-bootstrap  Accepted by other commands; not implemented for `build`
                     yet. (inert:deferred:tan-cli#427)
--build              Accepted for compatibility: ... (inert:compatibility:tan-cli#290)
--project            Project root. Not read: ... (inert:not-applicable)
```

(`--plan` was this section's example for `deferred` before tan-cli#427: it is
now RETIRED instead, a fifth, actively-read mechanism this closed vocabulary
does not cover -- see the `build --plan` row above. A retired flag carries no
`(inert:...)` marker at all; its value selects a real, distinct refusal
(`build.flag-retired`), which is why it moved out of this example rather than
keeping its old slot.)

Read it back with, after collapsing runs of whitespace:

```
\(inert:(?<kind>[a-z-]+)(?::(?<ref>[^)\s]+))?\)
```

| Kind | Means | Will it ever act? |
|---|---|---|
| `deferred` | An upstream issue tracks its arrival. **Always carries a ref** — `inert_help` refuses to render one without it. | Yes, eventually |
| `compatibility` | Kept so an existing caller's command line keeps parsing, after the behaviour it used to select stopped being conditional. | **No** |
| `parity` | Accepted only because a sibling surface accepts it — the v0.4.1 oracle's clap `GlobalArgs` are `global = true`, so every verb parses all ten (tan-cli#261, `tan.core.global_flags`). Hidden on every command today. | **No** |
| `not-applicable` | Structurally meaningless for this command, whatever tan implements later. | **No** |

**`deferred` is the only non-permanent kind, and the KIND is what says so —
never the presence of a ref.** `compatibility` and `parity` both name the issue
that explains their history; a consumer that keys "will this arrive?" off
`ref != null` gets `doctor --build` wrong, which is the exact customer-visible
defect tan-cli#886 was filed about ("not implemented yet, see tan-cli#427" told
about a flag that is never going to act).

**The vocabulary is closed.** An unrecognised kind is a tan bug, not a value to
fall back on; renaming or dropping one is the same breaking wire change the
frozen-issue-code rule describes — bump the CLI MAJOR/MINOR, record it in
`CHANGELOG.md`, and open the matching alp-sdk-vscode issue.

**Why parentheses and not `[inert:…]`.** Typer runs this app with
`rich_markup_mode="rich"`, so help text is rich MARKUP: a square-bracketed
marker parses as a style tag and renders as *nothing at all* — measured, the
token vanishes from `tan build --help` entirely. The token also carries no
whitespace, so rich's wrapping can never split it across two lines.

**What keeps it true:** `python/tests/gates/test_inert_option_markers.py`,
which walks the built Click tree (not the source) and fails on an unknown
kind, a `deferred` with no ref, a hidden inert option of a non-`parity` kind,
an option that reads as inert in prose but carries no marker, and — the pin
that matters to this repo's consumer — any change at all to the census of
**visible** inert options. That census is 15 rows today: the twelve `build`
deferrals, `doctor --build`, and `faultdecode`'s `--project`/`--sdk-root`.

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
be a golden. It is the single source the release workflow folds `args` and
`dataKeys` from into `envelopes.doctor` — not the whole file verbatim, same
as above — see "The `doctor` family is a key set, not a golden" above.

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
- **No absolute paths in argv** — every case invokes `tan` without an
  absolute `--project`/`--sdk-root`/`--destination`, so path fields the CLI
  reflects back (`project.root`, `boardYamlPath`, …) come out as
  `.`/`./board.yaml` rather than a machine-specific one. Nothing needed a
  `__SDKROOT__`-style substitution token (the convention `tests/parity/`
  uses) as a result. `sdk-remove-absent` (tan-cli#790) is the first case to
  pass `--destination` at all — a RELATIVE one (`./sdk-cache`), so the rule
  still holds — and the first whose `data` reflects an absolute path anyway
  (`data.path`, the resolved removal target): that field is what made `path`
  join `PATH_KEYS`, so it normalises through the `__WORKDIR__` substitution
  below instead of a third token kind.
- **Scoped path-separator normalization** — the one thing case selection
  can't avoid by construction: Windows path rendering produces
  `./board.yaml` as `.\board.yaml` on Windows. The harness normalizes
  `\` → `/` on the freshly captured side before diffing, but only on the
  known path-shaped fields (`root`, `boardYaml`, `boardYamlPath`,
  `destination`, `path`, `relativePath`, `sdkPath`, `sdkPinned`, `written`,
  `unchanged`, `launchJsonPath` — see `PATH_KEYS` in the Python conformance
  harness), not every
  string leaf. A blanket rewrite would also launder a real drift inside
  `issues[].message` or any other value that happens to contain a backslash —
  exactly the kind of change this gate exists to catch. Committed goldens are
  authored with forward slashes in those fields, matching the normalized form.
- **`__WORKDIR__` for a reflected absolute path** — the case family the
  "no absolute paths in argv" rule above cannot cover: `debug-config` reports
  the working directory it resolved (`project.root`) and the `launch.json`
  path it would write, absolute, whatever the argv; `sdk remove` (tan-cli#790)
  resolves its `<version|path>` argument against `--destination` and reports
  that resolved, absolute `data.path` back the same way. Those fields are
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
| `sdk-remove-absent` | `sdk remove v0.0.0-nonexistent --destination ./sdk-cache --format json` | 0 | tan-cli#790: idempotent removal of a target that was never there — `removed: false`, `freedBytes: 0`, exit 0 (a no-op is a SUCCESS, matching `sdk current`'s "nothing configured" convention). `--destination ./sdk-cache` is a relative argv token, matching the "no absolute paths in argv" rule below; `data.path` still comes back absolute (the resolved target), which is what makes it the first case to need the `__WORKDIR__` substitution for a field OTHER than `project.root`/`launch.json`. |
| `generate-board-yaml-missing` | `generate --format json` (no `board.yaml` present) | 2 | `generate`'s `data` schema (`{schemaVersion,targets,written,failed}`) is distinct from `init`'s and was otherwise completely unguarded — this is the first guard clause in `python/tan/commands/generate_cmd.py`, needing no board/SDK/network to reach. |
| `debug-config-preview-zephyr-mcu` | `debug-config --target-kind zephyr-mcu --server jlink --preview` | 0 | |
| `debug-config-preview-zephyr-mcu-sdk-identity` | `debug-config --target-kind zephyr-mcu --server jlink --core m55_hp --sdk-root ./sdk --preview` (fixture SDK) | 0 | |
| `debug-config-preview-baremetal-mcu` | `debug-config --target-kind baremetal-mcu --server openocd --preview` | 0 | |
| `debug-config-preview-yocto-userspace` | `debug-config --target-kind yocto-userspace --server gdbserver --preview` | 0 | |
| `debug-config-preview-native-host` | `debug-config --target-kind native-host --server none --preview` | 0 | One profile per `--target-kind`. Unlike the other cases these pin a `data` value that is itself a consumer ARTEFACT, not a report: alp-sdk-vscode#342 writes `data.configuration` into `launch.json` verbatim, so the golden pins the emitted key SET — an added key or a changed `program`/`executable` fails here instead of shipping. **All five were re-recorded against the shipping Python CLI under tan-cli#502 and carry a `PROVENANCE.txt`; none is `xfail`'d any more — see "Why the five `debug-config-preview-*` goldens were re-recorded" right below this table.** `--preview` reads no `board.yaml`, spawns no Python and probes no PATH; the only host-dependent output is the absolute working directory, tokenized as `__WORKDIR__` above. |
| `presets-no-sdk` | `presets --format json` (no SDK resolvable) | 0 | Pins the `presets.sdk-root-unresolved` warning ON THE WIRE — the one frozen issue code reachable hermetically — plus the full `PresetsData` key set with `soms: []`. |
| `presets-heterogeneous-som` | `presets --sdk-root ./sdk --format json` (fixture SDK) | 0 | Issue #106's worked example made executable. The fixture SoM has an `a55` (`machine:` → yocto) and an `m33` (`board:` → zephyr), so `data.soms[].cores[].{id,os}` carries two different values — rename `soms` or `cores` and this fails instead of quietly scaffolding a multi-core part single-core with no IPC. Also pins `boardLibraries` discovery. |
| `explain-overview` | `explain --format json` | 0 | `data.available.projectTemplates`, the New Project wizard's starter list. Fully hermetic — the catalogues are static, no SDK involved. |
| `explain-template-iot-starter` | `explain --template iot-starter --format json` | 0 | `data.som.{initAcceptsSkus,initRefusesSkuPrefixes}` (tan-cli#866) — a project template's `tan init` `--som` refusal policy as structured data, not just the `details[]` prose it used to be. `iot-starter` carries the ONE per-template `TEMPLATE_SUPPORTED_SKUS` restriction whose description string used to hand-repeat it ("(E1M-AEN801 only)"), so this is also the golden proving that parenthetical, and the explanation's Wi-Fi-transport sentence, are now GENERATED from `data.som.initAcceptsSkus`, not typed twice. Fully hermetic — no SDK involved on this path (`--template` never resolves a checkout). |
| `examples-catalog` | `examples --sdk-root ./sdk --format json` (fixture SDK) | 0 | `data.examples[].sourceDir`, which is what `tan init --from-example <sourceDir>` is handed back; a rename breaks scaffolding from an SDK example. Also pins README-derived `title`/`description`. The fixture SDK ships no `metadata/catalog.json`, so this also pins the pre-#484 FOUR-key row shape for a checkout that predates the facets. |
| `examples-catalog-facets` | `examples --sdk-root ./sdk --format json` (fixture SDK) | 0 | tan-cli#484. Same example as `examples-catalog`, but this fixture SDK's `metadata/catalog.json` carries a real `gen_catalog.py`-shaped entry for it (`category`/`som`/`board`/`cores[]`/`coreCount`/`osSet`/`declares`) — pins that every one of those keys reaches the wire, in the order alp-sdk's own issue illustrated, appended after the original four. |
| version-format tests (no fixture dir) | `--version` | 0 | `python/tests/test_cli_skeleton.py` asserts the format rather than a literal version that changes every release. (A Rust mirror of it existed until tan-cli#269 deleted `crates/`.) |
| issue-code gates (no fixture dir) | — | — | Python AST gates check the shipping emit sites. They prove spelling/registration, while command tests prove reachability. The Rust half, which checked the registry entries the frozen oracle owned, went with `crates/` in tan-cli#269 — see the `emittedBy` note under "Frozen issue codes". |
| doctor `--build` key set (no fixture dir) | `doctor --build --format json` | — | KEY-SET assertion, not a value diff: doctor's values are host facts (what is on PATH, whether a Zephyr workspace exists), its key names are not. Covers `data.summary.{pass,warn,fail}`, `data.nextSteps`, `data.checks[].{name,status}` and the literal check name `workspace`, in `python/tests/commands/test_doctor_command.py` — the envelope `data` key set and `summary`'s `{pass,warn,fail}` shape, plus the build preflight's leading check names (`sdk`, `boardYaml`, `workspace`) in `test_collect_leads_the_report_with_the_build_preflight_and_fails_a_workspaceless_host`. The single named Rust assertion that used to own this row, `doctor_build_data_keys_the_extension_reads`, went with `crates/` in tan-cli#269; the Python coverage is spread across that module rather than concentrated in one test. |
| `doctor` published key set (`contract/doctor-data-keys.json`, no `envelopes/` fixture dir) | `doctor --format json` | — | tan-cli#664: the SAME key-set fact as the row above, but PUBLISHED into `envelope-contract.json`'s `envelopes.doctor.dataKeys` rather than pinned only inside this repo. See "The `doctor` family is a key set, not a golden" above. Kept honest by `python/tests/conformance/test_doctor_contract_key_set.py`, which spawns a real `tan doctor --format json` and fails on either an emitted key this file doesn't declare or a declared key the command stopped emitting. |
| `sdk list` published key set (`contract/sdk-list-data-keys.json`, no `envelopes/` fixture dir) | `sdk list --online --format json` | — | tan-cli#887: the second `dataKeys` family, for the same reason as `doctor` — the values are upstream release facts, the key names are not. Published into `envelope-contract.json`'s `envelopes.sdk-list.dataKeys`. Kept honest by `python/tests/conformance/test_sdk_list_contract_key_set.py`, which replaces only `urllib.request.OpenerDirector.open` and runs the real `_fetch_releases` → `parse_remote_sdk_releases` → `_list_data` → `emit()` path. Both directions fail loudly, and a second run feeds an entry carrying nothing but `tag_name` so a key tan defaults rather than emits cannot hide behind a fully-populated fixture. |

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

### Second re-record: `data.programsDevice` + `loadFiles` (tan-cli#945)

All five `debug-config-preview-*` goldens were re-recorded a SECOND time, on
2026-08-30 against `tan 0.6.1-rc1.dev0`, purely additively — `data.
schemaVersion` stays `"1"` on every one. Two new facts, both explained in
full in the `data.programsDevice`/`data.configuration.loadFiles` row of the
frozen-`data`-field-names table above:

- `data.programsDevice: bool` on every case (`true` for the two cortex-debug
  targets, `false` for `yocto-userspace`/`native-host`).
- `data.configuration.loadFiles` on the three cortex-debug cases
  (`zephyr-mcu`, `zephyr-mcu-sdk-identity`, `baremetal-mcu`) only — it is not
  a field `cppdbg`/`lldb` have any concept of, so `yocto-userspace` and
  `native-host`'s `configuration` is untouched by this re-record.

Each case's `PROVENANCE.txt` carries its own "second re-record" section with
the exact diff. `issues[]` is unchanged on all five.

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
