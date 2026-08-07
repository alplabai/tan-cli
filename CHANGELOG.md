<!-- SPDX-License-Identifier: Apache-2.0 -->
# Changelog

All notable changes to `tan` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versioning is
[SemVer](https://semver.org/).

## [0.5.2] — Unreleased

### Changed

- **`tan debug-config --core <id>` now refuses a `--core` matching no build
  slice even when `--target-kind` is given explicitly**, not only when it is
  omitted. Previously an explicit `--core` was only checked against
  `build/system-manifest.yaml` when `tan` had to infer the target class
  itself; naming `--target-kind` bypassed that check entirely, so a
  mistyped or stale `--core` on an otherwise-built project silently produced
  a launch configuration pointing at a generic pre-build path that does not
  exist (`${workspaceFolder}/build/app/zephyr/zephyr.elf`) instead of the
  real per-core artefact. A customer preparing a launch config for a core
  named in `board.yaml` but not yet built by THIS project is now refused
  (`debug-config.core-unknown`, exit 2) rather than getting a placeholder
  config back at exit 0 -- run `tan build` for that core first, or drop
  `--target-kind` to let it infer. (#489)

### Fixed

- **`tan debug-config` could destroy a customer's hand-authored
  `.vscode/launch.json`, in three separate ways, plus two smaller merge
  gaps found reviewing the fix.** All under (#489):
  - The write used a truncating `open(path, "w")`: any failure between the
    truncate and the flush (a full disk, a quota/`RLIMIT_FSIZE` hit, an I/O
    error, or the process dying) destroyed the file, and the next run then
    refused *permanently* at the malformed-JSON guard. Rewritten as a
    temp-sibling write, `fsync`'d before an `os.replace` onto the REAL
    (symlink-resolved) target, with a POSIX directory `fsync` after --
    matching, and closing gaps in, `bootstrap_cmd.reconcile_west_manifest_path`'s
    own pattern. A symlinked `launch.json` (dotfile-managed, or shared
    across worktrees) now keeps the link and updates the real file, instead
    of the link being replaced by a plain file holding stale content. A
    launch.json `tan` creates now lands at the umask-filtered default a
    plain `open(path, "w")` would have produced, not `mkstemp`'s own
    hardcoded `0600`; a rewrite carries the existing file's own mode across
    the swap.
  - Merging a fresh draft into an existing `configFiles`/`setupCommands`
    list matched entries by POSITION, so a shorter or differently-ordered
    resolved list silently deleted the customer's own extra entries (a
    hand-added second OpenOCD `.cfg`, extra `setupCommands` for a remote
    gdb session) and could pair unrelated entries, both destroying one and
    duplicating another. Matching is now primarily by IDENTITY (a dict's
    own `text` field, or a scalar's own value), wherever the matching entry
    sits, with position kept as a weaker fallback signal ONLY for a draft
    item that matches nothing already in the file -- needed to keep the
    merge idempotent: without it, a single resolved value that replaces a
    PRIOR run's own single resolved value (e.g. a rebuilt `runners.yaml`
    naming a different `--config`) was treated as an ADDITION every time,
    so `configFiles` accumulated every revision a project had ever been
    built with instead of holding only the current one.
  - `debug-config.sdk-identity-key-absent` misattributed "no core id was
    resolved to look up the SDK's per-core `jlink_device` map with" as "the
    SDK publishes no `device` value for this SoM at all" -- telling a
    customer on a never-built project to file a metadata bug when the
    working remedy was `--core <id>`. Split into
    `debug-config.sdk-identity-core-unresolved` for that case (and the
    sibling case of an unrecognised `--core`, which now names the cores the
    SDK does publish); `sdk-identity-key-absent` still fires, correctly,
    when the SDK's identity block genuinely carries no `jlink_device` map at
    all.
  - `--pre-launch-task ''` (documented as "omit the key entirely") was a
    silent no-op against an EXISTING `launch.json` already carrying one from
    a prior run -- the merge only visits keys the fresh draft carries, and
    the opt-out's own implementation removes the key from the draft rather
    than marking it for removal. `create_launch_json_write_plan` now takes
    an explicit set of keys to omit from the merged result. **This is the
    one place in this whole fix where `tan` deliberately REMOVES
    hand-authored content**: if a customer had themselves typed a
    `preLaunchTask` value into the entry `tan` merges into, `--pre-launch-task
    ''` now deletes it -- correct, and the flag's own documented meaning, but
    worth stating plainly rather than only as "a key from a prior run".

  Known, accepted limitation: position is a heuristic, not real provenance,
  so a customer's hand-added `configFiles`/`setupCommands` entry that
  matches nothing in the fresh draft AND happens to sit within the draft's
  own length can still be overwritten, the same way the pre-#489 code
  always overwrote whatever sat at that index -- `sdk-identity-overwrite`
  discloses the one case a caller can identify (an SDK-filled, not
  build-resolved, single value) rather than hiding it behind `ok: true`.
  Closing this further needs a real provenance record ("did `tan` write
  THIS value, in a prior run"), which nothing here or on disk keeps today;
  tracked as a follow-up (#518), not built in this change.

- **Two release gates went red on every open PR the moment `v0.5.1` was
  tagged.** `version-identity` refused a tree still claiming `0.5.1` once
  that became a published tag (the state `version_check.py --not-released`
  exists to catch), and `test_installer_release_layout.py`'s bare-`latest`
  tests failed because `latest` began resolving to a tag its hardcoded
  `RELEASES` snapshot did not carry. Both are the checks working as
  designed; both needed the post-release follow-up they describe.

## [0.5.1] — 2026-08-04

### Fixed

- **`tan renode` accepted `--project PATH` but silently dropped it, resolving
  the build root from the CWD instead.** Every sibling command
  (`build`/`size`/`image`/`validate`/`generate`) honours `--project`; pointed at
  a built project from elsewhere, `tan renode --project <proj>` looked for --
  and failed to find -- `<cwd>/build/system-manifest.yaml`, and its own
  `tan build --project <cwd>` remedy would have built the wrong directory.
  `app_path` now resolves through the same `build_output.resolve_app_base`
  ladder `size`/`image` already call: an explicit `APP_PATH` positional wins,
  otherwise `--project`, then cwd. Every downstream string (`manifest_path`,
  `elf`, the remedy, `project.root`) derives from `app_path`, so the refusal now
  names the resolved project root rather than the CWD. (#470)
- **`bootstrap.workspace-orphan-refused` named a stringified Python `None` for
  its destination, and advised dropping a `--workspace` flag the invocation
  never carried.** Already unreachable in practice on this branch -- the
  `target is not None` gate added by tan-cli#389/#390 means the no-`--workspace`
  shape this was filed against no longer refuses at all, so the report
  reproduces against the published `v0.5.0` rather than `dev` -- but the inline
  message still interpolated `target` unconditionally, and would print `None`
  again the moment that gate is loosened. Split into
  `workspace_orphan_refusal()`, alongside the two sibling refusals that already
  had this shape, with a dedicated no-destination branch: "...so it cannot be
  relocated -- its .west/config would still name this checkout. ... This
  workspace is already bootstrapped; re-run without --sdk-root pointing into
  it, or clone a SECOND alp-sdk checkout elsewhere and pass --sdk-root at that
  one." The `--workspace`-supplied branch is unchanged. (#469)
- **`contract/issue-codes.json` registered two codes twice, one pair
  disagreeing on severity.** `bootstrap.adopted-venv-unusable` carried a
  `warning` entry (matching `ensure_venv`'s live `log.warn` call site) and a
  stale `error` entry describing a superseded
  `adopted_venv_unusable_refusal()` / literal `Issue(..., "error", ...)`
  implementation that no longer exists in `bootstrap_cmd.py` -- a leftover from
  a second, independently-merged implementation of the same tan-cli#390 fix.
  `bootstrap.venv-recreated` was a straight duplicate. JSON duplicate-key
  semantics are last-wins, so the published severity depended on file order,
  with no code diff to notice. Kept the `warning` entries:
  `adopted-venv-unusable` is not in `WORKSPACE_BLOCKING`, so
  `Log.take_issues()` never escalates it, and the run's failing verdict travels
  separately via `bootstrap.failed` at `error`. Both stale entries removed;
  `venv-recreated`'s two notes merged into the survivor. (#467)
- **New gate**: `tests/gates/test_issue_code_registry_has_no_duplicate_codes.py`
  parses `contract/issue-codes.json` through a duplicate-key-rejecting
  `object_pairs_hook` AND a second pass over the parsed `issueCodes` array. The
  real defect was two well-formed array entries sharing one `code`, which an
  `object_pairs_hook` alone cannot see -- and which was invisible to
  `test_every_issue_code_is_registered.py`'s `set` comprehension and to
  `test_issue_code_registry_shape.py`'s plain list. Now fails loudly, naming
  every duplicate. (#467)
- **27 `contract/issue-codes.json` issue codes shipped camelCase in v0.5.0,
  breaking every consumer's kebab-case convention.** `doctor_cmd.py`'s
  `checks_to_issues()` and `support_bundle_cmd.py`'s `_doctor_issues()` both
  derived an issue code straight from `Check.name`, which is camelCase (it
  doubles as a JSON data key and a display string, and stays that way —
  `name` itself is unchanged by this fix). alp-sdk-vscode's contract gate
  encodes `ISSUE_CODE_SHAPE = /^[a-z][a-z0-9-]*\.[a-z0-9-]+$/`, and its
  `findFrozenCodes` hard-asserts every published `issueCodes[]` entry against
  it, so a code shaped otherwise REDS the consumer's contract gate outright —
  loud, not silent — which is exactly what blocked the pin to v0.5.0. All 27
  are `"status": "reserved"`,
  `"consumer": "none"` — nothing consumed the old spelling, so the rename is
  free, exactly as the v0.5.0 entry above already said of the sibling
  `support-bundle.<checkName>` batch ("costing nothing to rename later").
  - `doctor.boardYaml` → `doctor.board-yaml`, `doctor.bootstrapManifest` →
    `doctor.bootstrap-manifest`, `doctor.homePath` → `doctor.home-path`,
    `doctor.hostPrerequisites` → `doctor.host-prerequisites`,
    `doctor.hostPython` → `doctor.host-python`, `doctor.longPaths` →
    `doctor.long-paths`, `doctor.pythonFloor` → `doctor.python-floor`,
    `doctor.sdkProvenance` → `doctor.sdk-provenance`, `doctor.sevenZip` →
    `doctor.seven-zip`, `doctor.venvProvenance` → `doctor.venv-provenance`,
    `doctor.westResolved` → `doctor.west-resolved`, `doctor.zephyrSdk` →
    `doctor.zephyr-sdk`, `doctor.zephyrSdkAvailableForHost` →
    `doctor.zephyr-sdk-available-for-host`, `doctor.zephyrVersion` →
    `doctor.zephyr-version`, `doctor.zephyrWorkspace` →
    `doctor.zephyr-workspace`; `support-bundle.boardYaml` →
    `support-bundle.board-yaml`, `support-bundle.bootstrapManifest` →
    `support-bundle.bootstrap-manifest`, `support-bundle.gdbserverBackend` →
    `support-bundle.gdbserver-backend`, `support-bundle.homePath` →
    `support-bundle.home-path`, `support-bundle.hostPrerequisites` →
    `support-bundle.host-prerequisites`, `support-bundle.jlinkBackend` →
    `support-bundle.jlink-backend`, `support-bundle.longPaths` →
    `support-bundle.long-paths`, `support-bundle.noneBackend` →
    `support-bundle.none-backend`, `support-bundle.openocdBackend` →
    `support-bundle.openocd-backend`, `support-bundle.pyocdBackend` →
    `support-bundle.pyocd-backend`, `support-bundle.sdkRoot` →
    `support-bundle.sdk-root`, `support-bundle.zephyrSdkAvailableForHost` →
    `support-bundle.zephyr-sdk-available-for-host`.
- **Fixed at the derivation, not by hand-editing the 27 outputs**: both
  commands now route their fallback issue-code suffix through a new
  `doctor_cmd.kebab_check_name()` helper (`check.code or
  f"doctor.{kebab_check_name(check.name)}"` /
  `f"support-bundle.{doctor_cmd.kebab_check_name(c.name)}"`), so a future
  `Check` with a camelCase `name` and no explicit `code=` override cannot
  reintroduce this. An explicit `code=` override still wins, unchanged.
- **New gate**: `tests/gates/test_issue_code_registry_shape.py` fails if any
  `contract/issue-codes.json` code does not match
  `^[a-z][a-z0-9-]*\.[a-z0-9-]+$` (the exact alp-sdk-vscode consumer regex,
  duplicated verbatim since that repo isn't a Python import here), and
  separately asserts the regex itself still rejects the 27 pre-fix
  camelCase spellings — non-vacuity, so this cannot regress to a gate that
  would have waved the original defect through.
- **`tan kconfig` could not reach the workspace an ordinary `tan bootstrap`
  had just built** (tan-cli#453) — the `prj.conf` LSP feed for
  alp-sdk-vscode, so this was a dead symbol menu by default on any host with
  another Zephyr checkout around. Two compounding defects: `west` was spawned
  bare, depending on PATH having it, which `tan bootstrap` deliberately never
  does (it installs `west` only inside the workspace-local venv); and
  `ZEPHYR_BASE` resolution carried a private, weaker 3-tier ladder instead of
  the shared, manifest-verified `tan.core.venv.west_workspace_dir` that
  `build`/`flash`/`west_forward` already use, so an unrelated ambient
  `$ZEPHYR_BASE` was accepted over the workspace `tan bootstrap` had just
  built for the very `--sdk-root` in play. `kconfig` now delegates to that
  same shared resolver rather than a third, driftable copy, and the bare
  `west` spawn is fixed at its real site,
  `tan/planner/kconfig_symbols.py`'s `_load_board_symbols`, which now
  resolves `west_program(...)` and spawns that absolute path.
- **`tan quality` and `tan migrate` could never succeed under any input**
  (tan-cli#454) — both shell a `west` extension that declares an argument
  REQUIRED (`alp_quality.py`'s `--profile`, `alp_migrate.py`'s mutually
  exclusive `--check`/`--preview`/`--apply`), and neither flag existed on
  tan's own surface, so every real run died on the child's own argparse usage
  error (`quality.failed` / `migrate.failed`) before doing any work. Both
  commands now declare the flag(s) for real and refuse BEFORE spawning `west`
  when absent (`quality.profile-required` / `migrate.mode-required`, exit 2)
  rather than round-tripping through a child process to find out. No default
  is guessed for either: `quality`'s profile and `migrate`'s mode (`--apply`
  mutates the customer's `board.yaml`) are both left to the caller.
- **`tan generate` bare/`--all` refused every re-run once
  `boards/native_sim_native_64.overlay` existed, even against its own prior
  output.** `_overlay_would_overwrite`'s guard was existence-only, so a
  routine edit-board.yaml / generate / build loop's second `generate --all`
  refused the WHOLE run forever after the first (`data` empty, exit 3) purely
  because the overlay was already there (tan-cli#457). The guard is now
  content-aware: an existing overlay carrying the banner every
  `native-sim-overlay` emit writes is tan's own prior output and is freely
  rewritten, in `--all` or an explicit `--target native-sim-overlay` alike; one
  that lacks it is a real hand edit and still refuses with
  `generate.would-overwrite` (exit 3) unless `--force` is passed — the
  truncation the guard exists for is unchanged.
- **`tan debug-config` defaulted a hardware project to `native-host`,
  writing a `launch.json` naming a binary the build never produces**
  (tan-cli#456). `_select_slice`'s `or hw_slices` fallback silently
  discarded an explicit `--core` that matched no hardware slice in the
  built manifest, falling through to the native-sim/host branch instead of
  refusing — a J-Link `cortex-debug` session pointed at a binary that never
  gets built. Fixed in `50b9456`: `--core native_sim` now correctly infers
  `targetKind=native-host` / `server=none` / `type=lldb`, and a `--core`
  matching no slice refuses outright rather than guessing a target class.
  New test: `test_an_omitted_target_kind_with_a_core_matching_nothing_refuses`.
- **`tan debug-config` reported two USER-ACTIONABLE preconditions as
  `ExitCode.INTERNAL_FAILURE` (5)** — an omitted `--target-kind` on a real
  hardware project (`som.sku` set) whose `build/system-manifest.yaml` does
  not exist yet, and an explicit `--core` matching no slice in an existing
  one — reporting a one-command-early user as a tan CRASH to any consumer
  that classifies by exit code, alp-sdk-vscode included (tan-cli#462). Both
  now exit `ExitCode.VALIDATION_FAILURE` (2), matching the distinction
  tan-cli#262 already settled for `tan validate`: a verdict the command CAN
  produce about the caller's own input is exit 2, and exit 5 stays reserved
  for a genuine tan-side crash. Two new registry codes replace the shared
  `debug-config.internal-failure` for these two cases —
  `debug-config.build-manifest-missing` (message unchanged) and
  `debug-config.core-unknown` (message now names the cores the build
  actually produced, e.g. `its cores: m55_hp, m55_he`, instead of only
  saying the passed one does not match). The `except Exception` catch-all
  backstop is unaffected — that one is a genuine internal failure and exit 5
  is still correct there. `infer_target_kind` now returns a
  `(target, code, message)` 3-tuple instead of `(target, message)`; its two
  still-ambiguous shapes (more than one target class, or none at all) are
  unchanged and keep exit 5. Not a breaking wire change: every
  `debug-config.*` code in the registry is still `"status": "reserved"`/
  `"consumer": "none"`.
- **The other two `infer_target_kind` refusals were the SAME defect as the
  pair above and got left at exit 5.** Review round on tan-cli#462: a
  mixed-core board's `build/system-manifest.yaml` naming more than one
  target class with no `--core` to narrow them, and a manifest naming hardware
  slices whose `os` maps to no known target class at all (e.g. a lone
  `os: linux` slice), both hit a WELL-FORMED, tan-produced manifest — the
  first is worse than the pair already fixed, since it hits every run against
  a fully built, CORRECT project forever, not just one command early — and
  both have a working remedy the message itself names (`--target-kind`
  explicit). Both now exit `ExitCode.VALIDATION_FAILURE` (2) via two more
  registry codes, `debug-config.target-kind-ambiguous` and
  `debug-config.no-debuggable-target-class`, replacing the shared
  `debug-config.internal-failure` for these two cases too (messages
  unchanged). `infer_target_kind` returns a reason code for all four
  refusals now, none left unclassified. Not a breaking wire change: every
  `debug-config.*` code in the registry is still `"status": "reserved"`/
  `"consumer": "none"`.
- **A relocating `tan bootstrap` had no test proving the very next `tan
  doctor` could still find the checkout it just moved (tan-cli#463).** The
  tan-cli#185 auto-relocation into `<parent>/alp-workspace/alp-sdk` already
  repoints `~/.alp/sdk-default` at the new location (`_write_global_sdk_pointer`,
  gated on an actual relocation and skipped under `--dry-run`, both
  unchanged) — but nothing exercised the READ side through a real subprocess
  boundary: a *second*, independent `tan doctor` invocation, the way a
  customer's next terminal command actually works, reading nothing but that
  file. A regression there (`resolve_sdk_tiered`'s `globalDefault` tier, a
  `_home_alp_dir()` mismatch, or a writer/reader JSON-shape drift) would have
  passed every existing check. `.alp/sdk-path` (a PROJECT pin) is
  deliberately not what carries this: bootstrap runs in the workspace
  PARENT, which is not itself a tan project, so the pin that must answer is
  the machine-global default, not a project-scoped one that flow never
  writes. New test:
  `test_a_relocating_bootstrap_leaves_a_later_doctor_able_to_find_the_sdk`.
- **The e2e harness's tan-cli#407 assertion was path-form-blind on
  Windows.** `scripts/e2e-full.sh`'s two-checkout divergence check grepped
  doctor's text report for its own `$D407` in Git Bash's MSYS mount form
  (`/c/v06/...`), but doctor renders that path through `_abs_posix()` —
  always the drive-letter form (`C:/v06/...`, via
  `os.path.abspath().replace('\\','/')`) — even under Git Bash. Same
  directory, different spelling, so the grep never matched and the harness
  reported `#407: doctor's text report is silent about the second checkout`
  on a report that named it plainly. The comparison now normalises through
  `cygpath -m` (a no-op on POSIX hosts, where the two forms already agree)
  before matching; the negative control (silent on a genuine single-checkout
  host) is unchanged and stays strict.
- **`tan diff` reported `no effective-config differences` on a board.yaml
  `tan validate` rejects with four errors** — the two commands read the same
  file and disagreed about whether it was usable at all (tan-cli#455). `diff`
  only ever checked whether it could PARSE `board.yaml` well enough to
  normalize it; it had no notion of the SDK's own semantic rules (a SoM SKU
  pattern, whether a preset exists, the closed `peripherals:` enum). Fixed by
  reusing `validate`'s own spawn-and-analyze machinery
  (`analyze_validator_output`, the same `scripts/validate_board_yaml.py`
  argv) whenever an SDK has resolved: a non-clean verdict now becomes a
  `diff.<outcome>` refusal — `diff.schema-violation`/`diff.failed`/
  `diff.missing-preset`/`diff.hardware-revision`/`diff.spawn-failed`/
  `diff.python-too-old` — at the same exit code `validate` would report for
  that outcome, instead of a clean, meaningless comparison. `diff` still
  never requires an SDK on its own; with none resolved it falls back to the
  structural checks alone, exactly as before.
- **`tan pinmux`'s text renderer dropped the envelope's `issues[]`, so an
  exit-2 error printed no error at all** — a user saw a plausible
  `pinmux: family=v2n pads=0` and nothing else, including on a hard failure
  (tan-cli#458). Text mode now follows the summary line with one
  `pinmux: <issue.message>` line per issue on stderr, matching the
  text-mode convention `generate`/`monitor`/`build`/`doctor`/`examples`
  already use for their own issues. `size`/`image` build a separate text
  list that drops issues the same way — a distinct, still-open gap, out of
  scope here.
- **`tan bootstrap --print-env` reported a workspace `bootstrap` would never
  create, before the first real bootstrap ever ran** (tan-cli#459). It
  assumed the checkout's PARENT was the west workspace — exactly the
  assumption a dirty parent makes `bootstrap` itself reject in favour of
  auto-relocating into `<parent>/alp-workspace` (tan-cli#302/#185) — so on
  that same host `--print-env` and `--dry-run` disagreed: one printed
  `ZEPHYR_BASE=<parent>/zephyr`, three paths verified absent on disk and that
  no future run would ever create, at `ok: true`, `issues: []`; the other
  correctly named `<parent>/alp-workspace/zephyr`. Fixed by
  `_print_env_outcome`, which now projects `--print-env`'s reported paths
  through the SAME write-time decision the real run would make — the
  relocation guard or a `$ZEPHYR_BASE` adoption — so it can no longer diverge
  from `--dry-run` over identical input. New test:
  `test_print_env_agrees_with_dry_run_on_a_dirty_parent`.
- **A second project's `tan bootstrap` could silently repoint a FIRST
  project's SDK at the wrong checkout, `ok: true`, no warning** (tan-cli#464).
  A relocating bootstrap recorded the move only in the machine-global
  `~/.alp/sdk-default` — one pointer, last-writer-wins across every project on
  the host — so the moment a second project bootstrapped and relocated, the
  first project's `tan sdk current`/`tan build` silently started resolving
  the SECOND project's checkout instead of its own, with `issues` identical
  to the correct case. **This entry replaces an earlier same-cycle attempt at
  this fix**, measured to carry five majors on review (an independent design
  review separately confirmed the shape) and reworked before ever shipping in
  a tagged release:
  - **Reverted**: an actual relocation also writing a directory-scoped
    `.alp/sdk-path` in the directory `bootstrap` ran in, at the
    higher-precedence `projectPin` tier. That directory is bootstrap's own
    cwd — the workspace PARENT in the documented quickstart, not a project —
    so a bootstrap run from `$HOME` pinned `~/.alp/sdk-path` *inside tan's own
    machine-global config dir*, silencing the warning below for essentially
    every project the user owns on that host; it also silently overwrote an
    existing pin `tan init` had written, with no issue and no log line, and
    it only ever helped at the exact directory bootstrap ran in — one `cd`
    into a subdirectory and the silence returned. `tan init`'s own
    `_pin_sdk` remains the only writer of a project's `.alp/sdk-path`.
  - **Kept and now disclosed everywhere, not just `sdk current`**:
    `~/.alp/sdk-default` still records `writtenFor` — which project's
    bootstrap last wrote it. Resolution through the `globalDefault` tier is
    unchanged (the same root still answers; this is a disclosure fix, not a
    prevention one — a machine-global default exists to answer for projects
    anywhere on the host, so refusing would exit 2 for every legitimate
    out-of-tree project on a one-SDK machine). A caller resolving through it
    from a workspace under neither `writtenFor` nor the SDK path itself gets
    `sdk.global-default-foreign-project`, a WARNING never a refusal. Reaching
    only `sdk current` was itself a defect this rework closes:
    `resolve_sdk_root_ladder`/`resolve_sdk_root_wide` (`build_cmd.py`) now
    return a named `SdkRootResolution` instead of a 3-element tuple — a
    fourth positional element would have reproduced the exact silent-drop
    shape this issue exists to close at the next field the ladder needs to
    carry — and `build`, `flash`, `generate`, `image`, `run`, `size`, `doctor`,
    `clean`, `examples`, `bootstrap`, `presets`, `new-som` and `renode` all
    append the warning beside their existing `sdk.project-pin-unresolved`
    now — `size`/`image` unconditionally, on every manifest-gate refusal too
    (including `image.bundle-write-failed`, not just its `manifest-
    unavailable`/`manifest-invalid` siblings), from one shared
    `sdk_cmd.sdk_resolution_issues`. `flash`'s equivalent, `_error`, reached
    the SAME shared helper in a first pass at this rework but one early
    return — its OWN `flash.sdk-root-not-found`, the SDK failing to resolve
    at all — bypassed `_error` and stayed gated to the happy path until a
    review round closed it too; `flash` is unconditional as of this same
    section. `tan init` surfaces it BEFORE `_pin_sdk` writes — the pin `init`
    makes is PERMANENT, so silently baking a foreign checkout into a brand
    new project is worse than one build using the wrong SDK once.
  - **Fixed**: `writtenFor: ""` used to pass `_pointer_written_for`'s bare
    `isinstance(value, str)` check and reach `_workspace_under(ws, "")`,
    which resolves `Path("")` to the process's own cwd — so whether the
    warning fired depended on where the caller happened to be standing, not
    on anything the pointer recorded. `_pointer_written_for` now rejects a
    non-absolute value at the source; every other malformed shape (an int, a
    list, a dict, `null`) already returned `None` safely and still does.
    Also widened, in the same fix and **user-visible**: absoluteness used to
    be judged by the READER's own `pathlib` flavour, so a legitimate
    POSIX-absolute `writtenFor` (`/home/u/projB`) silently read as "no
    opinion" on a Windows reader, and symmetrically for a Windows-absolute
    one (`C:/projB`) read on POSIX — accepted now when EITHER
    `PurePosixPath` or `PureWindowsPath` calls it absolute, so a pointer one
    platform's `tan` legitimately wrote no longer goes silent purely because
    a DIFFERENT platform's `tan` is the one reading the shared file.
  - `_undo_relocation`'s rollback is back to restoring (or clearing) exactly
    the one pointer it overwrote — the second, project-pin restore path this
    rework's first attempt added (and its own risk of misattributing a
    pointer-restore failure to the wrong file, the tan-cli#284 shape its
    docstring warned against) is gone along with the write it guarded.
  New/updated tests:
  `test_a_second_projects_relocation_does_not_silently_repoint_the_first`
  (two real bootstraps, one shared HOME, driven through subprocesses — never
  by inspecting pointer bytes, which already had coverage and did not catch
  this; now pins that A's later `sdk current` DOES resolve B's checkout, with
  the warning present, rather than pinning the reverted per-project pin),
  `test_build_command.py`'s new two-project scenario proving `tan build`
  itself — not just `sdk current` — emits the warning,
  `test_sdk_command.py` coverage for the empty-string, wrong-type, and (both
  directions of) cross-platform-absolute `writtenFor` shapes, and
  `test_flash_command.py::test_sdk_root_ladder_broken_pin_discloses_on_the_not_found_refusal`,
  which pins `flash`'s own last gap: a broken `.alp/sdk-path` pin with
  nothing lower on the ladder to fall through to used to return bare
  `['flash.sdk-root-not-found']`, unlike the identical state on `size`/
  `image`; it now returns `['sdk.project-pin-unresolved',
  'flash.sdk-root-not-found']`, matching them.

## [0.5.0] — 2026-08-04

*This section was headed `## [0.6.0]` until tan-cli#377. **0.5.0 has never been
released** — only `v0.5.0-rc1`…`v0.5.0-rc4` — so everything here and everything
in the rc sections below ships together as the first real 0.5.0. The BREAKING
`tan validate` exit-code change is measured against **v0.4.1**, the last actual
release, and a MINOR bump already covers it under this project's own pre-1.0
rule. A `## [0.6.0]` heading also made the release-body slice unresolvable:
release.yml extracts the notes by an exact `^## \[<tag minus v>\]` match, so a
`v0.5.0` tag would have found no section and refused to publish.*

### Added

- **All seven formerly-deferred verbs are now real commands** (`scaffold`,
  `completion`, `diff`, `pinmux`, `inspect`, `trace`, `support-bundle`
  — tan-cli#260, CLOSED). Each used to be a uniform stub — exit 1, one shared
  `cli.command-deferred` issue, naming this tracking issue — registered only
  so the command resolved instead of falling through to Click's exit-2
  unknown-command error. `tan/commands/deferred_cmd.py` keeps only the two
  constants (`DEFERRED_ISSUE_CODE`, `DEFERRED_ISSUE_URL`) and the context
  settings `tan build`'s own still-deferred *flags* (`--plan`, `--target`,
  …) reuse; the stub factory and its `DEFERRED_VERBS` tuple are gone.
  `tan/cli.py`'s `_HONOURS_ROOT_FORMAT` now spells the seven names directly
  rather than deriving them from that removed tuple.
- **`contract/issue-codes.json` gained 49 `"reserved"` entries** for codes
  the seven newly-real verbs (and this wave's `doctor --fix` consent-gate
  work) already emitted with nowhere registered to bind to:
  `diff.board-yaml-missing`/`.internal-failure`/`.pyyaml-unavailable`/
  `.schema-violation`; `pinmux.internal-failure`; `trace.sdk-root-unresolved`/
  `.board-yaml-missing`/`.internal-failure`; `support-bundle.<checkName>`
  (20 entries, mirroring the existing `doctor.<checkName>` family verbatim,
  since `support-bundle`'s doctor section reuses `doctor_cmd`'s `Check`
  objects unchanged); `doctor.fix-needs-sudo`/`.fix-installed`/
  `.fix-spawn-failed`/`.fix-failed`/`.fix-timed-out`/`.fix-suppressed`;
  `scaffold.name-required`/`.cancelled`/`.invalid-template`/`.invalid-name`/
  `.internal-failure`; `debug-config.gdbserver-address-unresolved`; and 9
  `renode.sim-*`/`renode.expect-ignored` codes from the `--sim-mode` gateway
  (tan-cli#77) that had never been registered either. None of these were
  reachable before this wave — `diff.*` had ZERO entries of any kind — so
  none is a wire break; every one is `"reserved"`/`"consumer": "none"`,
  costing nothing to rename later.
- **`tan flash` can auto-sign an Alif Ensemble slot0 ATOC via SETOOLS**
  (tan-cli#353, #365-#369, #373; `tan/core/setools.py`, new). Flow D
  (`alif_mram_jlink`, J-Link straight over SWD, no SE-UART) needs a SIGNED
  ATOC from Alif's own `app-gen-toc` step; a fresh AEN801 manifest's
  `flash_args` carries only `jlink_flash_device` (measured on real silicon,
  e1m-aen-evk-01/E8 AE822), so before this every customer had to sign by
  hand outside `tan` and paste the result back in. `tan flash` now drives
  `app-gen-toc` for you against a SETOOLS install you already have — three
  precedence-ordered sources (new `--setools-dir` flag, `SETOOLS_DIR`, or
  the least-durable `flash_args.setools_dir`, tan-cli#368), never a
  filesystem search, and never under `--dry-run`. See
  [`docs/setools.md`](docs/setools.md). SETOOLS itself is license-gated and
  neither `tan` nor alp-sdk redistributes it.

### Changed

- **Development builds no longer claim a published release's version**
  (tan-cli#377). Between releases `tan --version` now answers a development
  identity (`0.5.0-rc5.dev0` today) instead of repeating the last tag's number:
  `dev` reported `0.5.0-rc4` for this entire wave, so the published rc4 binary
  and every build of the development line were indistinguishable in a bug
  report despite substantial behaviour and contract differences between them.
  `python/scripts/version_check.py` gained `--not-released`, which REFUSES a
  tree whose `TAN_VERSION` is the version of an existing release tag that does
  not point at HEAD — the bump alone would have repeated the defect next cycle.
  It also now checks that `CHANGELOG.md` carries the section the tag will be
  released under, moving #212's missing-section failure off the release job
  (four freezes and an immutable tag too late) and onto the PR.
- **BREAKING: `tan validate`'s not-yet-ported spawn path now exits 2
  (`VALIDATION_FAILURE`), not 1 (`RUNTIME_FAILURE`)** (tan-cli#262, TAKEN).
  Before this release, a `board.yaml` present with an unresolvable SDK (or,
  once the real validator spawn path lands, any post-spawn verdict failure)
  answered `validate.spawn-not-implemented` at exit 1 — indistinguishable
  from a genuine tan crash. Measured against the oracle
  (`target/debug/tan.exe`): every guard-level `validate` refusal already
  exits 2, and exit 1 there is reserved for the ONE case a spawned validator
  returns an unmappable status — this port had flattened that distinction.
  "The validator could not produce a verdict" is now treated as the
  validator's own verdict everywhere, at exit 2, matching the guard cases.
  **Who must act:** any CI step that greps this exit code and branches on
  `-eq 1` specifically (rather than "non-zero") now sees 2 instead and will
  silently stop matching; `alp-sdk-vscode` renders exit 2 as severity
  "warning" and exit 1 as "error", so a consumer keyed on the old code will
  now show a genuine validation gap as a warning rather than an error until
  it is updated to read exit 2. A real `tan`-side crash (an unreadable
  `board.yaml`, an unexpected internal exception) is unaffected and keeps
  exit 5.

### Fixed

- **The test suite did not necessarily test THIS repo's `tan`** (tan-cli#423).
  26 test files spawn `[sys.executable, "-m", "tan", ...]`, which resolves to
  whatever `tan` the interpreter finds first -- on one host an editable install
  pointing at a different worktree entirely, so the suite's verdict depended on
  the directory `pytest` was started from. It had already produced one FALSE
  parity failure (`multicore_rpmsg-imx93: alp-sdk refuses but tan does not`),
  filed as a real divergence before the cause was found; tan refuses that
  `status: tbd` hw_rev correctly in every emit mode. `tests/conftest.py` gains
  `tan_under_test`, a session-scoped autouse fixture modelled on the existing
  `pinned_oracle`: it asserts the imported `tan` resolves inside this repo's
  `python/` and prepends that directory to `PYTHONPATH` for the spawned
  children. Measured: `test_faultdecode_command.py` went from 2 failed to 24
  passed when run from the repo root.
- **`build` reported a bare `terminated with exit code: 1` when the Zephyr SDK
  cross toolchain was missing** (tan-cli#419), although `doctor`'s `zephyrSdk`
  check already diagnosed exactly that with the exact remedy. CMake's refusal
  (`Could not find a package configuration file provided by "Zephyr-sdk"`) sat
  in the pass-through child output the envelope never named. The slice message
  now names the cause and the `west sdk install --version 1.0.1 -t
  arm-zephyr-eabi` remedy, importing the command string from `doctor_cmd`
  rather than re-spelling it so the two cannot drift. A slice that fails for
  any other reason keeps the bare message -- a conditional re-wording must not
  mislabel unrelated failures.
- **`generate` left its writability probe's empty file behind when the emit was
  refused** (tan-cli#420). `_ensure_writable` proves the destination writable
  with `open("ab")`, which CREATES it; nothing removed it again. The envelope
  was always honest (exit 3, `data.written == []`, `generate.emit-failed`), but
  the DISK was not: `cmake/alp.cmake` hands
  `${CMAKE_BINARY_DIR}/generated/alp.conf` to Zephyr as `EXTRA_CONF_FILE`, and
  Zephyr accepts an empty conf file silently -- no configuration applied,
  nothing said. Inside tan's own flow the refusal stops the build, but a
  standalone `west build` after a failed `tan generate` reaches that configure
  with nothing re-running generate in between. The probe's file is now removed,
  and only ever that file: only when the probe itself created it (`existed`
  captured BEFORE the open), only while still zero-byte (re-checked at unlink
  time, so a partial artefact from an emitter that died mid-write survives as
  evidence), with cleanup failure swallowed so it cannot replace the real
  refusal.
- **`validate` reported `validation failure` when nothing had been validated**
  (tan-cli#350). Two different situations produced one message -- no
  `board.yaml` to check, and a `board.yaml` that genuinely failed its schema --
  and the first is the state every new user is in before `tan init`. Now three
  distinguishable verdicts: `validate.board-yaml-missing` naming the searched
  path and both fixes; a clean pass; and a real failure that carries the
  `validate.schema-violation` errors that produced it. The escalated case (the
  message firing on a project tan itself scaffolded) is gone with the
  not-ported path it came from, removed in tan-cli#376.
- **The frozen binary took 13-19 s to run ANY command on macOS arm64**
  (tan-cli#349) because the PyInstaller `--onefile` bundle re-extracted ~14 MB
  of interpreter and shared libraries on every invocation, and on macOS each
  extracted `.dylib` falls outside the parent's ad-hoc signature and is
  inspected individually on load. `--onedir` moved that to install time. The
  fix shipped verified on Linux and Windows only -- macOS was the one platform
  it could not be measured on by hand -- so the `clean-host` matrix, which
  already freezes and runs the binary on `macos-15` and `macos-15-intel`, now
  times `--version` there: 5 runs, median against a 5 s ceiling. Measured on
  macOS arm64: **0.342 s**, against 13.25 / 19.35 / 19.35 / 18.58 / 19.74 s
  before.
- **The oracle-provenance gate fired on commits that never touched the oracle.**
  It compared reachability alone, so any commit after the recorded SHA counted
  as drift. It now compares the `crates/` tree hash first and reports only when
  the tree genuinely differs.
- **`tan build`'s deferred flags pointed every refusal at a CLOSED issue.**
  `DEFERRED_ISSUE_URL` named tan-cli#260, which tracked the seven verbs -- all
  of which shipped in this release, closing it. A user following that link
  landed on a closed issue about commands that work. The flags now point at
  tan-cli#427, which tracks the flags themselves, and the message no longer
  names a release at all: it said "deferred to v0.6.0" while the release it
  meant was renumbered to 0.5.0, and a refusal that promises a version is a
  claim this port cannot keep true.- **Two SDK discovery ladders answered different checkouts from one directory
  and both reported `sourceTier: "discovery"`, so nothing on the wire said
  which one had answered** (tan-cli#407). In a workspace holding both a child
  `<ws>/alp-sdk` — what `tan bootstrap` clones — and a lateral `../alp-sdk`,
  `tan generate` emitted `build/generated/alp.conf`, the DTS overlays and
  `alp_hw_info_build.h` out of one checkout's `metadata/` while `tan build`,
  `tan size`, `tan trace` and `tan doctor` planned and reported against the
  other. Measured ten commands for ten under the identical tier string, and
  `tan sdk current` — the one command a user runs to ask "which SDK am I
  on?" — answered with the narrow one only, reporting the readiness and
  `VERSION` of the checkout `tan generate` did not use. The envelope's
  `sdk: {root, sourceTier}` key exists (tan-cli#110) precisely so
  `alp-sdk-vscode` can tell which SDK produced a result instead of guessing;
  one tier name for two answers is that key failing at its only job.

  The divergence itself is deliberate and oracle-measured (see
  `resolve_sdk_root_ladder`'s own docstring) and is **unchanged**. The tier
  string cannot move either: `SdkSourceTier` is a closed five-value wire
  contract no consumer expects to grow, and `test_build_manifest.py` pins the
  exact `(path, "discovery", None)` tuple both ladders return because that is
  the oracle's own answer. What changed is the silence. Six commands now emit
  the shared `sdk.discovery-divergent` warning naming BOTH checkouts, so a
  consumer holding one envelope from each side matches them on the code:
  `tan build` and `tan sdk current` on the narrow ladder, `tan init`,
  `tan generate`, `tan examples` and `tan renode` on the wide one.

  `tan doctor` renders it as a check rather than reporting one of the two
  roots as if it were the only one: its `sdk` check moves `pass` → `warn`,
  carries `code: "sdk.discovery-divergent"` and names the checkout the four
  wide commands resolve. The exit code does not move — `exit_code_for` keys
  on `fail` alone, and a split workspace is a warning, not a broken host.

  `tan renode` needed a second fix to benefit at all: both its `fail_sdk`
  helpers built a single-issue list, so its six early refusals dropped this
  warning *and* the pre-existing `sdk.project-pin-unresolved` one. Measured
  before the fix, a divergent workspace refused with
  `renode.manifest-unavailable` alone — the refusal that matters most,
  because its own message says to run `tan build` first and `tan build` is
  the ladder that disagrees. Both issues are now computed at resolution time
  and ride on every envelope the command can still produce, with the refusal
  itself kept as `issues[0]`.

  A structural gate (`tests/commands/test_sdk_discovery_ladders.py`) now
  fails if a module calls `resolve_sdk_root_wide` without emitting the code,
  so a sixth wide caller cannot land silently unwired.

  The ten narrowly-resolving commands this originally left silent (`size`,
  `trace`, `clean`, `run`, `flash`, `inspect`, `presets`, `image`, `kconfig`,
  `validate`) are now covered too, and not by wiring each of them: the
  warning is attached at `Envelope.__init__`, the one seam every command's
  envelope passes through. They each reach the ladder by a different seam,
  which is exactly why per-command wiring left them out -- and a fix present
  on six commands and missing on ten leaves the reported silence in place,
  since `alp-sdk-vscode` branches on `sourceTier` from both groups. Gated on
  `sourceTier == "discovery"` before anything touches the disk, so an
  ordinary single-checkout host pays nothing: every higher tier
  (`--sdk-root`, the project pin, the global default) is shared verbatim by
  both ladders and cannot be the pair that differs. The seam skips a command
  that already emitted the code, so the six wired above do not double-report.

  Two defects surfaced only by measuring the seam against the real layout,
  either of which would have shipped a warning that looked wired and was
  not: `project.root` is `null` for `examples` and `sdk current` -- two of
  the commands measured as divergent -- so an early bail on a null root left
  the wide side unreported; and it is the relative `"."` for several others,
  where `Path(".").parent` is `"."`, collapsing the ladders' lateral
  candidate onto the child so a real divergence reads as agreement.
  Measured: `doctor` warned while `validate`, from the same directory, did
  not. Verified over the issue's own layout on the frozen binary: eleven
  commands each carry the code, and a single-checkout layout stays silent.
- **BLOCKER: the Flow D SETOOLS auto-sign's own soft-failure guard could
  destroy a prior sign record instead of only detecting a fresh one**
  (tan-cli#373, regression in #365's own fix). `app-package-map.txt` is
  APPEND-mode — the accumulated sign record for the whole SETOOLS install,
  including hand-runs done outside `tan`, per `flash-jlink.sh`/
  `flash-jlink-mramxip.sh`/`flash-update-log-dual.sh` — but the guard
  `os.remove`d it before every sign to detect an `app-gen-toc` exiting 0
  without actually writing. On a manifest with two Flow D entries where one
  pointed its own `flash_args.atoc_map` at that same file, the other
  entry's auto-sign wiped it first: `app-gen-toc` recreated it holding only
  the SECOND entry's block, and the first entry's `atoc_address` resolved
  to the second entry's address — a mismatched ATOC burned into on-die
  MRAM, recoverable only by re-provisioning over SE-UART. Replaced with a
  size+mtime snapshot taken before the spawn: an append changes both, so an
  unchanged snapshot after a zero exit is the same soft-failure signal,
  without deleting anything.
- **`tan flash --dry-run`'s SETOOLS preview still bypassed most of Flow D's
  own validation** (tan-cli#373, #366 narrowed not closed). A `--dry-run`
  whose `SETOOLS_DIR` resolved reported `ok:true` for a manifest with e.g. a
  quoted `jlink_speed`, because that check lived only inside
  `plan_alif_mram_jlink`, unreached from the preview's early return — and on
  a REAL run the SETOOLS auto-sign (writing into the customer's install)
  happened before that refusal was ever reached. `jlink_speed`/`confirm`
  are now validated in `validate_flow_d_shape`, the one function both the
  preview and the real plan-builder call before either can proceed.
- **The wrong-DP-ID preflight remediation had replaced the wiring/
  `jlink_serial` advice for three OTHER banners it does not apply to**
  (tan-cli#369 regression). That fallback is shared by four cases — a
  genuinely different SW-DP ID answering, an unrecognised banner, an
  unplugged-SWD-ribbon `Cannot connect to target.`, and a refused
  `jlink_serial`'s `Cannot connect to J-Link.` — but #369's rewrite gave all
  four the cloned-serial explanation, silently deleting the original
  "check the probe selection (jlink_serial) and the wiring" sentence
  (tan-cli#353) for the three where `jlink_serial` genuinely IS the fix. Now
  branched on whether a DP ID was actually read.
- **`is_elf_artefact` only accepted `.elf`, narrowing #367(a)'s own
  three-shape decision** (no extension, `.elf`, `.out`) with nothing
  flagging it. `output_artefact: app` (no extension) or `app.out` now
  resolves to its same-stem sibling `.bin` again, same as `zephyr.elf` does.

### Shipped earlier in the 0.5.0 pre-releases

0.5.0 has never been released — only `v0.5.0-rc1` through `v0.5.0-rc4`. The
four sections below are those pre-releases, folded in unchanged so that this
entry is the complete 0.5.0 delta against **v0.4.1**, the last real release.
They are kept as separate dated subsections rather than merged topic-by-topic
because several later entries supersede earlier ones within the same line, and
that sequence is what a future reader archaeologising a regression needs.

### [0.5.0-rc4] — 2026-08-02

*Everything below was found by running the published `v0.5.0-rc3` binary as a
customer would — on real Windows, macOS and Linux hosts, in isolated
environments — not by testing the source. Two of them destroy or misplace a
user's files; one silently drops a core from a multi-core build.*

#### Fixed

- **`tan bootstrap --dry-run` moved the user's alp-sdk checkout and rewrote
  `~/.alp/sdk-default`.** One command on a clean directory left `alp-sdk/`
  **gone**, `alp-workspace/` created and the global SDK pointer repointed —
  while reporting `exit 0`, `ok: true`, and narrating the relocation in the
  past tense. No data was lost (all 4067 files survived the move and `git log`
  still resolved) but a preview flag must not move a repository or rewrite a
  global config. `relocate_checkout()` now takes `dry_run`: it still validates
  and reports the planned destination, performs no `mkdir` and no `os.rename`,
  skips the pointer write, and says *"would move"* / *"would set"*. The
  relocation itself is unchanged on a real run — that behaviour is what closed
  #302 (#323).
- **`tan init` and `tan generate` followed symlinked parents and wrote outside
  the project while reporting success.** A pre-existing directory symlink under
  the project caused bytes to land elsewhere, with the envelope reporting the
  logical in-project path and `ok: true` — a data-placement bug and a
  truthfulness bug together. A new shared guard (`tan/core/fs_confine.py`,
  `resolve_confined`) compares two **resolved** paths; both write surfaces and
  `init`'s `.alp/sdk-path` pin now confine before writing anything, refusing the
  whole run rather than partway through. A symlinked *project root* still works
  (#325).
- **A dangling `$ZEPHYR_BASE` silently dropped a core from a multi-core build.**
  Zephyr's `build` extension makes a separate `west_topdir(self.source_dir)`
  call that walks from the slice's own app directory — which `_pin_west_workspace`
  (#307) never covered, since that only pins the child's cwd for west's startup
  search. An app bundled inside the workspace resolves regardless; a `tan init`
  project is always a **sibling** of the workspace, so it has no ancestor
  `.west` and falls through to `$ZEPHYR_BASE`, which west's `set_zephyr_base`
  trusts with no existence check. Result: `ok: m55_he`, `failed: m55_hp`,
  `FATAL ERROR: Could not find a west workspace in this or any parent
  directory` — blaming west for a stale environment variable. Every west slice
  spawn is now independent of an inherited `ZEPHYR_BASE`, and the failed
  slice's `reason` names the workspace tan resolved and the `ZEPHYR_BASE` the
  spawn saw (#336).
- **`tan --version` was not eager: a version probe executed the following
  subcommand.** `tan --version init --template zephyr-app --destination <dir>`
  printed the version **and created the project**. The root callback returned
  rather than short-circuiting dispatch, which a Click/Typer group callback does
  not do when a subcommand is also present. Now handled through the framework's
  own `is_eager` + callback mechanism, matching the Rust oracle (#326).
- **`envelope.serialize-failed` printed `exitCode: 5` while the process exited
  `0`.** `Envelope.to_json()` fell back correctly but `emit()` never propagated
  the fallback code, so stdout and `$?` disagreed — breaking the invariant every
  consumer relies on, `process exit code == stdout envelope.exitCode`. The
  fallback code now reaches the process boundary (#327).
- **`tan image` rejected helper firmware the SDK actually ships.** alp-sdk
  defines `firmware_path` as repository-relative, but `_bundle_helper` resolved
  it only under `build/`, so a real `cc3501e-v0.2.0.bin` (1087888 bytes) present
  in the SDK produced `image.helper-missing` and refused the bundle. Relative
  paths are now tried against both roots in a defined order, and the error names
  every root tried with its absolute path (#330).
- **`sdk list --online` split table rows in half.** `releaseNotesSummary` is the
  release body verbatim, truncated to 60 characters with no whitespace handling;
  a body whose first line was short put a literal newline inside a row, and the
  `{flags}` suffix then landed after the spilled text rather than on the row it
  described. Whitespace is collapsed before truncating (#316).
- **`bootstrap`'s `INCOMPATIBLE $ZEPHYR_BASE` message named the verdict but not
  the cause**, while its two sibling paths both named theirs. A workspace that
  missed on **two** axes at once — version skew *and* a foreign manifest — fell
  past both specific branches into a catch-all whose own comment claimed "not a
  usable west workspace at all", which was untrue of the reported case. The
  branch now accumulates the observed facts and renders them together. The
  decision and exit code are unchanged; the fallback was already correct (#334).
- **`tan/planner/` had drifted from alp-sdk's `scripts/alp_orchestrate/`**, so
  tan emitted different Kconfig and accepted a board alp-sdk refuses. On
  `mproc-mailbox`, `tan generate`'s Zephyr conf fragment was **56 lines against
  alp-sdk's 63** — seven Kconfig symbols a user's build never got. On
  `rpmsg-imx93`, alp-sdk refused the board (`E1M-NX9101` hw_rev `r1`, status
  `tbd`) while tan reported `ok` and planned a build for it. Re-synced, and
  seam1's harness now **compares** a legitimate refusal on both sides instead of
  treating alp-sdk's non-zero emit as a harness abort (#320).

#### Internal

- Two test fixtures wrote an executable and immediately `exec`'d it with no
  guard, so a sibling test's still-open write handle could make the kernel
  return `ETXTBSY` — the third occurrence of #250's race, and the cause of a
  flaky **required** check (3 failures in 40 consecutive full-suite runs,
  0 after the fix). The guard is hoisted so every fixture picks it up; the crate
  now has zero unguarded write-then-exec sites (#318, #333).

#### Known issues

- `@alplabai/tan` has never published to npm: `NPM_TOKEN` is a classic token and
  `npm publish` demands an OTP. Needs an automation token (#233).
- `SUPPORTED_CLI_VERSION` still lives in alp-sdk-vscode rather than being
  derived from the Python tan (#268).

### [0.5.0-rc3] — 2026-08-01

*Found by running the published `v0.5.0-rc2` binary end to end on a real
Windows host -- fresh and dirty -- rather than testing the source or trusting a
green CI run on a clean runner. Every defect below was invisible to both.*

#### Fixed

- **`tan doctor` exited 4 on every fresh install.** `tan bootstrap` succeeds and
  deliberately leaves `west` off PATH (its own next-steps block says to activate
  the venv afterwards), so "west in the venv, absent from PATH" is not an edge
  case -- it is the guaranteed post-bootstrap state, and the same state a
  GUI-launched VS Code is always in. `west`'s Fail claimed "every build slice is
  executed through it", which the build log disproved in the same workspace:
  west resolved from the venv and the build produced a real ARM ELF while
  `doctor` called the host broken (#299).
- **`westResolved` now FAILS when west resolves nowhere.** Splitting the pair
  above exposed that its Warn had rested on `west` failing first, so with both
  warning a host where NO slice could run exited **0**. `west` answers "is it on
  bare PATH" and is never fatal; `westResolved` answers "can a slice run at all"
  and owns the exit code. Caught by the e2e, not by the tests.
- **`doctor` and `bootstrap` stopped telling users to raise
  `prerequisites.pythonMinVersion`** -- the change alp-sdk#1078 tried and
  reverted, because that key is host-universal while the floor is Zephyr's, so
  raising it refuses a 3.10/3.11 host for a Yocto-only project that builds
  today. `bootstrap` emitted it *while refusing*, making it the last line a
  blocked user read (#300).
- **The `sdk` check names the tier it resolved through**, and a cwd checkout it
  did not select, so a report describing a different SDK than the one you are
  standing in is no longer indistinguishable from a wrong answer. Comparison is
  `normcase`d -- without it, the same directory in different case reported
  itself as unselected (#301).
- **The macOS release asset shipped with no CA trust anchors at all.** A
  PyInstaller freeze bundles its own `ssl` but no CA bundle and does not fall
  back to the platform trust store, so every `urllib` HTTPS call in `sdk list
  --online` failed `CERTIFICATE_VERIFY_FAILED` on the published
  `tan-aarch64-apple-darwin` v0.5.0-rc2 asset -- measured on real macOS arm64
  hardware, where `curl`, a browser and system `python3` all verified the same
  endpoint fine in the same minute. `tan/net.py` now builds every request's
  `SSLContext` through `truststore` (verifies via the OS's own trust store, so
  a real corporate CA installed in the machine's keychain keeps working) and
  falls back to `certifi`'s bundled CA list only if `truststore` is absent or
  its platform verifier fails. The failure text also stopped asserting *"This
  is usually a TLS-intercepting proxy or a corporate CA"* as the cause with no
  evidence for it -- there was neither on the reporting host, and the same
  confident wording would have sent a customer on a genuinely proxied network
  hunting in the wrong place too. `scripts/verify_binary.sh` gains a fifth
  proof that the freeze actually bundles both mechanisms, since no unit test
  can catch this: a source-tree run has the developer's own trust, and the bug
  is invisible until the frozen artifact runs (#304, release-blocker).
- **`longPaths` read the Windows registry flag and nothing else, so `tan
  doctor` said `pass` on a fresh install while `tan bootstrap`'s own `west
  update` died with "Filename too long" a moment later.** Windows'
  `LongPathsEnabled` governs manifested Win32 API calls; it does nothing for
  git, which `west update` uses for every module clone/checkout and which
  refuses a long path unless its OWN `core.longpaths` is set, regardless of
  the registry -- measured on a real Windows 11 host with a genuinely fresh
  `HOME` (no global `.gitconfig`), where `bootstrap` failed inside
  `hal_nxp`'s `tf-psa-crypto` vendor tree even though `doctor` had just
  reported the host fine. `longPaths` now reads BOTH axes (`git config --get
  core.longpaths`, which resolves system/global/local precedence itself) and
  **fails** -- not warns -- exactly when the registry says yes and git does
  not, since that combination breaks `west update` with certainty rather
  than merely risking it; it warns when only one axis is on, matching the
  pre-existing severity for neither. The remedy names the exact command,
  `git config --global core.longpaths true`. `tan bootstrap` also forces
  `core.longpaths=true` on the `west update` child's own environment
  (`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0`, reaching every
  project's own `git` subprocess without writing to any `.gitconfig` the
  workspace or the user owns), so a fresh install gets past the failure
  outright rather than only being warned about it (#306, release-blocker).

#### Added

- **`clean-host.yml` — the shipped-artefact gate on a genuinely clean host**
  (#278, escalated to a release-blocker). All six defects above were found by
  downloading the `v0.5.0-rc2` asset and running four commands on a host with
  no alp-sdk checkout, no `~/.alp`, and an empty cwd -- a state no prior CI job
  in this repo ever constructed, and the reason a green board shipped every
  one of them. `getting-started.yml` proves the documented first-install path
  works from a real `tan bootstrap`; it can never reproduce a HOST WITH NO SDK
  REACHABLE AT ALL, which is exactly the state #292/#301/#302 need and exactly
  the customer's actual first three commands. The new job freezes `tan` per
  platform (`macos-15-intel`/`macos-15`/`windows-latest`/`ubuntu-latest`, same
  shape as `release.yml`'s `build` job -- pre-tag, when these six needed to be
  caught) and, separately, downloads the actual published asset after a
  release goes out for a post-publish confirmation. Both legs run
  `python/scripts/clean_host_smoke.py`'s `tan --version` / `tan doctor
  --format json` / `tan sdk list --online` (the real GitHub API call, unmocked
  -- the #304 CA-trust canary) / `tan bootstrap --dry-run`, twice each for
  `doctor`/`bootstrap` -- once with `$ZEPHYR_BASE` unset, once pointed at a
  directory that exists but carries no Zephyr markers at all, deliberately
  bare rather than a fabricated lookalike (#286: a fixture shaped like what
  the probe looks for proves the fixture, not the code). `doctor`'s envelope
  is checked for internal self-consistency -- `ok`/`exitCode` agreeing with
  `checks[]`, and no two checks named `<subject>` and
  `<subject>Resolved`/`<subject>Provenance` (this repo's own corroborating-
  check naming convention) disagreeing pass vs. fail -- which generalises the
  #299 shape without grepping for `westResolved`/`west` by name. `macos-15`/
  `macos-15-intel` are mandatory (where #304 manifested); a Windows pass on
  `sdk list --online` is explicitly NOT read as CA-trust coverage, since
  CPython's `ssl` on win32 already falls back to the Windows certificate store
  where macOS and Linux have no equivalent.

#### Known

- `tan bootstrap` still refuses the documented quickstart layout (`tan.exe` and
  `alp-sdk/` in one directory) with "holds more than this checkout", and the
  remedy it offers moves the user's checkout (#302).

### [0.5.0-rc2] — 2026-08-01

*Everything the maintainer's first real `v0.5.0-rc1` run turned up, plus what
reviewing those fixes turned up in turn. Six release-blockers, and one theme:
almost every defect was a check measuring a **proxy** instead of the thing it
claimed to measure — and the ones that shipped green did so because a test
encoded the same assumption as the code.*

**Still a pre-release.** Nothing on the stable extension channel upgrades onto
this; the delivery mechanism is unchanged from rc1 (`alp-sdk-vscode` odd-minor
pre-release channel, `prerelease: true` / `make_latest: false`, both installers
resolving `latest` which excludes prereleases).

#### Fixed

- **`tan build` and `tan flash` could not find `west` on a host that had
  bootstrapped successfully (#289, release-blocker).** `4d70bdc`/`eac6cbf`
  landed a venv resolver and three consumers were never wired to it. On any host
  where the workspace venv is not *activated* — which is every GUI-launched VS
  Code, the extension's normal environment — every Zephyr slice was skipped with
  `tool 'west' not found`, and `tan flash`'s PATH-only gate refused where the
  Rust oracle succeeds. New `tan/core/venv.py` is the shared module
  `flash_cmd.py`'s own docstring asked for ("one shared module for both
  commands, not a second copy"), ported from `crates/tan-cli/src/venv.rs`; five
  scattered private helpers were deleted in favour of it.
  - `#106` — `build/execute.py` rewrites `tool == "west"` through
    `west_program()` and prepends the venv bin dir to the child's `PATH`.
  - `#59` — `flash_cmd.py`'s tool gate gains a venv fallback. PATH still wins
    the *availability* tie; `argv[0]` selection is venv-preferring, matching
    `crates/tan-cli/src/commands/flash/mod.rs:521-546`.
  - `#61` — `west_workspace_dir` (a **different** search: it walks for `.west/`,
    not `.venv/`) is resolved once and threaded as every spawn's cwd, so
    `west flash` can see alp-sdk's out-of-tree runners.

- **Flow D could write the wrong file to a board's MRAM.** Fixing #289 set the
  flash child's cwd to the west topdir, and `flash_args.atoc` was the one Flow D
  input never run through `resolve_artefact_path` — it reaches the J-Link
  Commander script verbatim (`loadbin {atoc} {atoc_address}`). The repo's own
  fixtures use a relative `atoc: atoc.bin` at five sites, so a relative path
  silently changed which file it named. Now anchored on `build_root`/`sdk_root`
  like `atoc_map` and `output_artefact` already were. The reason it was missed
  is recorded: `grep -rn "FLOW_D\|flow_d_preflight\|expect_dpidr" crates/`
  returns nothing — Flow D is Python-only, so Rust's cwd-safety audit was
  inherited without ever covering the one backend that writes on-die MRAM.

- **A bootstrapped venv could be invisible to build and flash (#291,
  release-blocker).** Creation picks the executable directory by what is on disk
  (`bootstrap_cmd.py Workspace.venv_bin` probes both layouts); resolution derived
  it from the host. A Git Bash-created `Scripts/` venv on a posix-reporting host
  was therefore created, then not found. Resolution now uses the same
  directory-wins rule, consuming `core/bootstrap.py`'s existing
  `venv_exe_names()` rather than restating it. **Deliberate oracle divergence:**
  `crates/tan-cli/src/venv.rs:34` is still `venv_layout(cfg!(windows))` — the
  same host-derived bug — so this port is knowingly more correct than the oracle
  here.

- **`typer>=0.12` was unbounded across a change in what typer *is* (#293,
  release-blocker).** typer 0.26 dropped its public-click dependency and began
  vendoring its own fork, and `cli.py` depends on that private hierarchy
  (`typer._click.exceptions`). Now `typer>=0.26,<0.28`, both ends measured by
  wheel inspection: `0.25.1`'s `requires_dist` still lists `click>=8.2.1` and
  `0.26.0`'s does not. The old floor was itself a lie — `typer==0.12.0` on PyPI
  is a broken metapackage stub requiring `typer-slim`/`typer-cli`. Matters
  because `pip install ./python` is the documented install path for `win32/arm64`
  and `linux/arm64`, which have no published asset, and because CI carries no
  lockfile and no pin. `tests/gates/test_typer_click_bound.py` now checks the
  range *and* the private-MRO coupling directly.

- **`tan init` pinned a relative SDK root that later invocations could not
  resolve (#263, release-blocker).** The pointer is written absolute, and a
  stored pin that cannot resolve now says so (`sdk.project-pin-unresolved`).

- **`tan build` reported `ok: true` / `exitCode: 0` / `issues: []` when every
  slice was skipped (#283).** A build that built nothing was indistinguishable
  from one that succeeded. Emits `build.nothing-built` / `build.missing-tool`.

- **`tan bootstrap` moved the alp-sdk checkout before it could refuse (#284).**
  An ancestor `.west` now returns `exitCode: 2` with
  `bootstrap.enclosing-west-workspace` and nothing moves. Rollback no longer
  re-derives what it should have remembered: `RelocationUndo` snapshots
  `old_root`, the pointer, `project`, `workspace_dir` and `venv_dir` *before*
  relocation. `_undo_relocation` returned `str | None` where a non-None string
  meant two opposite things, so a user whose checkout **had** moved back was
  told to "Move it back by hand" — naming a directory that no longer existed —
  while `data.sdkRoot`/`data.workspaceDir`/`project.root` kept the vacated path.
  The yocto-host and prerequisite refusals were **hoisted above** the relocation
  rather than given rollback paths.

- **`bootstrap: complete.` printed after a step reported a problem (#285)**, and
  `$LASTEXITCODE` was 0 on a self-declared incomplete venv. `completion_verdict`
  is now a port of the oracle's `verdict()` — named blocking codes, an
  `INCOMPLETE` line, an `--allow-partial` escape. The Python ceiling stays a
  **warning**: `PYTHON_CEILING_KNOWN_GOOD` is 3.12, measured by a real bootstrap
  run in `getting-started.yml`, and refuses no host.

- **`doctor` had dropped six checks the oracle emits, each reintroducing a
  closed defect (#286, #290, #294).** Found by one audit rather than one field
  at a time — @hkngln's call, and the right one, since every instance is the
  same proxy-measuring shape and invisible from any single case.
  - `zephyrSdk` (#160), plus the `sevenZip` sibling gated to match
    `build_readiness.rs:545` — `west sdk install` shells to patoolib for `.7z`
    with no pure-Python fallback, so without 7-Zip the fix hint cannot be
    followed.
  - `westResolved` (#123) — now the version of the **venv-resolved** west, never
    a re-probe of bare PATH. Verified by mutation: swapping in `on_path("west")`
    and re-probing the version each turn the test red.
  - `zephyrSdkAvailableForHost` / `longPaths` / `homePath` (#70) — the old check
    answered "is one installed *here*", never "*can* one be installed on this
    machine".
  - `sdk` / `boardYaml` / `workspace` / `zephyrVersion` preflight (#100, #98,
    #159), folded into **plain** `doctor` as Rust does.
  - `posix_venv_unusable` (#161) — the function already existed at
    `core/bootstrap.py:1140` and was simply never called from `doctor`.
  - `data.missingPrerequisites` (#203/#210) — absent from the envelope though
    `bootstrap_cmd.py` already populated it. The extension feature-detects on
    its absence, so nothing crashed; what regressed was the one-click dependency
    install at `alp-sdk-vscode/src/deps/vscodeAdapter.ts:379`.
  - `sdkProvenance` — git short-commit plus `metadata/sdk_version.yaml`.

- **Restoring those checks made `doctor` refuse hosts that work — four times,
  all caught before release.** `boardYaml` exited 4 on a fully bootstrapped host
  with no `board.yaml` in cwd (the first command a user runs after bootstrap,
  from the directory bootstrap just made); `_posix_venv_capable` failed *closed*
  on an inconclusive probe where `crates/tan-cli/src/util.rs:241-247` is
  `.unwrap_or(true)` on purpose; an Apple-silicon Mac running an x86_64 Python
  was told to build on Linux (no `sysctl.proc_translated` check); and a
  `$ZEPHYR_BASE`-only workspace was told "no Zephyr workspace". Every one was
  green because its test asserted the refusal was correct.

- **A Zephyr pin mismatch was reported twice** — `summary.fail` 5 instead of 4,
  two issue codes, two `nextSteps` for one remedy. Rust drops its own duplicate
  `boardYaml` for exactly this reason.

- **`NO_COLOR` was checked two different ways and `faultdecode` diverged
  (#288).** `size` used a presence check (matching `style.rs:27` and the
  NO_COLOR spec); `faultdecode` used a truthy check, so `NO_COLOR=` kept colour
  on. Both now call `tan.env.no_color_requested()`. Deliberately **not**
  `typer.Option(envvar="NO_COLOR")` — Click coerces bool envvars by truthy-string
  parsing, so `NO_COLOR=` would raise `not a valid boolean` and crash a command
  that works today.

- **234 seconds of silence during CMake configure read as a hang (#287).** Phase
  announcements and an elapsed heartbeat, armed at the shared `_dispatch` choke
  point so `tan run` is covered too, TTY-gated and byte-silent under
  `--format json`. Line width comes from `shutil.get_terminal_size()`; the
  previous hardcoded 88-column erase pad stacked rows on any terminal narrower
  than 114.

- **`tan bootstrap --non-interactive` exited 2.** It is the literal first-blink
  command in `parity.yml` and the docs. `bootstrap` now accepts the five globals
  the oracle accepts and ignores.

- Smaller: the published asset filename leaking into `--help` usage text (#280);
  `python_repr` diverging on nested mappings (#277); the `TBD` sentinel trimmed
  on one path and not another (#276); a `venv_has_usable_pip` `ETXTBSY` flake
  (#250); `zephyr_board.py` escaping `PINNED_HASHES` (#279); and
  `getting-started.yml` smoke-testing the **Rust** `tan` while the release ships
  a PyInstaller freeze (#278).

#### Changed

- **`doctor`'s `--build` is accepted and inert.** The check set is now
  unconditional, so there is no build-gated half left. `README.md` no longer
  claims `--build --fix` repairs anything.
- **`doctor` gained `sdkProvenance` and `data.missingPrerequisites`**; consumers
  reading the envelope get two keys that did not exist in rc1.
- **`--fix` is still not accepted** (tan-cli#295). `alp-sdk-vscode` calls
  `tan bootstrap` instead, which is what actually creates the venv and west
  workspace; accepting the flag as a no-op would have given users a Fix button
  that reports success having repaired nothing.

### [0.5.0-rc1] — 2026-07-31

*The first release in which `tan` is a Python program: the planner relocated
into it, so `tan` now plans AND executes, and the four assets are PyInstaller
freezes of `python/` rather than cargo builds of `crates/`.*

**No stable user upgrades onto this release.** `SUPPORTED_CLI_VERSION` in
`alp-sdk-vscode` moves to `0.5.0-rc1` only on that extension's PRE-RELEASE
channel -- extension `v0.5.0`, an odd minor, which `release-vsix.yml`
publishes with `--pre-release` (alp-sdk-vscode#446). Stable extension users
stay on an even minor pinned at the Rust `tan` and are untouched until GA
(#268); opting into the beta channel is the whole delivery mechanism for this
RC. The tag also publishes with `prerelease: true` / `make_latest: false`, and
both installers resolve `latest` through GitHub, which excludes prereleases, so
`install.sh` and `install.ps1` still install the last stable release. Everyone
else installs by hand.

#### Changed
- **The planner relocated into `tan`, so `tan` now plans AND executes.**
  alp-sdk's `scripts/alp_orchestrate/` (20 modules, ~6.2k lines) is now
  `python/tan/planner/`, and `tan build` renders the build plan **in-process**
  instead of shelling
  `PYTHONPATH=<sdk>/scripts python -m alp_orchestrate --emit build-plan`. That
  drops the requirement for an interpreter named `python`/`python3` on PATH
  carrying PyYAML and jsonschema — the frozen binary's single most likely
  first-run failure. The subprocess call survives as a fallback for exactly one
  case: a build of `tan` that cannot import its own planner (no `jsonschema`),
  against an SDK that still ships `alp_orchestrate`.

  This is a MOVE, not a rewrite — the accumulated silicon behaviour (carve-out
  top-down allocation, FNV-1a endpoint ids, partition bottom-up allocation,
  per-core OS derivation from Cortex class, Kconfig section order) is precisely
  what must not be re-derived. `tests/parity/test_planner_emit_parity.py`
  imports both planners into one process and asserts byte-identical output for
  every mode over every `board.yaml` in the SDK's `examples/`; it skips, loudly,
  without an `ALP_SDK_ROOT` naming a checkout that still carries the original.

  **`metadata/**` did not move and must not** (ADR-0017): the generators
  relocated, the facts did not. The fact readers stayed too —
  `alp_project_loader`, `alp_project_emit`, `alp_registries` and
  `alp_cli.validator` are still imported from `<sdk_root>/scripts`, and
  `scripts/alp_project.py` remains tan's canonical SDK-root marker. What
  replaced `paths.py`'s walk-up-from-`__file__` derivation is an explicit
  binding (`tan/planner_root.py`): every `metadata/**` path is a function of the
  resolved `sdk_root`, and importing the planner before a root is bound raises
  instead of silently reading the wrong tree.
- **The release assets are PyInstaller `--onefile` freezes of `python/`, and
  there are FOUR of them, not eight** (#271). The published set is
  `tan-x86_64-pc-windows-msvc.exe`, `tan-x86_64-apple-darwin`,
  `tan-aarch64-apple-darwin` and `tan-x86_64-unknown-linux-gnu`. `--onefile` is
  required rather than preferred: the extension downloads a raw binary to ONE
  cached path and has no unpack step anywhere in it. The asset NAMES keep the
  Rust target triples because `alp-sdk-vscode`'s `releaseAssetForTarget`
  hardcodes them and builds its download URL from them.

  **`tan-aarch64-pc-windows-msvc.exe` and an arm64 Linux asset are NOT
  published**, and a download of either 404s. PyInstaller cannot
  cross-compile -- every asset must be frozen on the architecture it runs on
  -- and adding two more runner types was out of scope for this tag. That is a
  decision, not a platform limit (`windows-11-arm` and `ubuntu-24.04-arm` are
  current hosted labels, and this repo is public, so arm64 minutes are not a
  barrier either), so it can be revisited whenever arm64 assets are wanted;
  `pip install ./python` is the route until then. There is no `-musl` asset
  either: PyInstaller's musllinux bootloader carries ELF interpreter
  `/lib/ld-musl-x86_64.so.1`, so a musl freeze runs ONLY on musl distros --
  it is not the run-anywhere static binary the Rust `-musl` target produced,
  and shipping it would have broken every Ubuntu/Debian/Fedora user.

  **The Linux asset is glibc**, frozen in `python:3.12-slim-bullseye` (Debian
  11, glibc 2.31) -- exactly the floor the retired `cargo-zigbuild`
  `x86_64-unknown-linux-gnu.2.31` pin targeted, so nothing is lost against the
  Rust asset. The floor quoted in the release notes is MEASURED over the
  appended PyInstaller payload (libpython plus the extension modules,
  enumerated from `.build/tan/PKG-00.toc`) inside the build container, never
  asserted and never read off the outer ELF: `readelf -V dist/tan` there sees
  only PyInstaller's vendored bootloader, a container-invariant `GLIBC_2.14`
  that cannot detect the build image regressing to a newer glibc. The release
  job fails rather than publish notes quoting a floor nothing measured.

  `alp-sdk-vscode`'s `service.ts` still maps `linux/x64` to the MUSL triple,
  so the extension cannot download this asset. Deliberate for this tag -- it
  never reaches an RC anyway -- and repointed with the pin move at GA (#268).
- **`scaffold`, `completion`, `diff`, `pinmux`, `inspect`, `trace` and
  `support-bundle` are withdrawn from this build, not silently absent.** The
  Python port stubs all seven (`python/tan/commands/deferred_cmd.py`) rather
  than porting them yet: each parses its real argv (any positional/flags are
  accepted, never rejected) and then exits `RUNTIME_FAILURE` (1) naming
  `tan-cli#260`, in both `--format json` and text mode; the shared issue code
  `cli.command-deferred` is carried in the JSON envelope only -- text mode
  prints just the deferral message and the issue URL. That is deliberately
  louder than Typer's own unknown-command `UsageError` (exit 2,
  `cli.parse-error`) would have been for an absent verb -- a caller (or the
  extension) can tell "known but deferred" apart from a typo like `tan bulid`
  by the issue code in `--format json`, or by exit 1 vs Typer's exit 2 in
  text mode.
- `jsonschema` is now the fourth runtime dependency. It arrived with the
  planner, which validates every `board.yaml` against
  `metadata/schemas/board.schema.json` before it plans anything. The frozen
  one-file binary measures 12377580 B against the 15000000 B ceiling.

#### Added
- **`tan build --execute` -- run a plan that came from `--plan-from`.** ADDED
  BY THIS PORT: v0.4.1 has no such flag, and there `--plan-from` implies
  `--plan` and outranks `--native`, so a file-supplied plan could not be
  dispatched at all. Taking a pinned, reviewed plan file and running it
  reproducibly is a normal hermetic-CI request, so this is a supported
  capability rather than a parity gap or a test hook. `--execute` implies
  `--materialise` (nothing can run that was never written) and reports the
  ORDINARY build shape -- `{schemaVersion, baseDir, slices[], warnings[]}` --
  with `written` deliberately omitted, being byte-for-byte the pinned plan's
  own declared artefact paths. A conflicting combination is refused with its
  own code, never resolved by silent precedence: `--execute --materialise` ->
  `build.conflicting-flags`, exit 2; `--execute` with a deferred flag ->
  `cli.command-deferred`, exit 1.

  **`--plan-from` itself still means what it meant in v0.4.1** -- it is a plan
  SOURCE, not a build trigger. Measured against the shipped `tan 0.4.1-dev`
  binary rather than inferred: `--plan-from p.json` and
  `--plan-from p.json --native` both give rc 0 and write 0 files with `data`
  = the plan, and `--plan-from p.json --materialise` gives rc 0 and 6 files.
  All three are reproduced exactly, `--native` included. Two earlier revisions
  of the port each redefined one of those argvs, which would have made a
  v0.4.1 script that only ever INSPECTED a plan silently begin writing six
  files into its own tree.

- **`tan debug-config` now resolves a real J-Link device / pyOCD target id
  from the SDK's published debug-probe identity, before the project has ever
  been built** (alp-sdk#1026). alp-sdk#987 shipped `variants[].debug.
  {pyocd_target, jlink_device, jlink_flash_device, openocd_config}` across 13
  Alif Ensemble variants with a schema, a gate and tests, and no reader
  anywhere — every generated `launch.json` kept the literal `<resolved-
  device>` / `<resolved-target-id>` placeholders #987 was filed to replace.
  `device` resolves from `jlink_device[<core id>]` (the `--core` flag, or the
  core a prior build already resolved); `targetId` resolves from the scalar
  `pyocd_target` with no core or build needed at all. A real build's own
  `runners.yaml` resolution still wins wherever it exists; this is a fallback
  for the field(s) it did not already resolve. `openocd_config` is absent
  from every published SoC family today, and stays the existing placeholder —
  the schema's own stance that an unpopulated key is a published "unknown" is
  never overridden with a guess. New `debug-config.sdk-identity-key-absent`
  (reserved) issue names that case explicitly instead of relying only on the
  generic "still needs resolution" note, which used to name `device` even for
  a server whose draft carries no such key.

#### Fixed
- **A pending `TBD` placeholder can no longer reach a flasher** (#222). alp-sdk
  writes `TBD` into a manifest field it has not filled in yet, and every guard
  on the flash path used to test for EMPTY -- which is the one thing a `TBD`
  placeholder is not. An unfilled field therefore behaved exactly like a
  filled one, and whether that ended in a loud refusal or a spawned flasher
  came down to whether the particular consumer happened to validate against a
  closed set: `flash_method: TBD` hit the backend registry and failed safely,
  while `output_artefact`/`firmware_path: TBD` hit nothing at all, resolved to
  `<build_root>/TBD` and reached a real J-Link write.

  One definition now answers it for the whole path (`flash_plan.is_pending`),
  shared with the bundle writer so `tan image` and `tan flash` can never
  disagree about what "unfilled" means. The value is trimmed before comparing
  -- a YAML `device: "  TBD  "` is the same unfilled field -- but deliberately
  NOT case-folded and NOT a substring test: `TBD-1234-XYZ` is a plausible part
  number and `flash_args.build_dir: /opt/TBDtool/x` a plausible path, and
  refusing either would block a legitimate flash. The two sites differ by
  consequence, per the per-entry rc convention: an unresolved `TBD` in
  `flash_args` SKIPS the entry (rc `-1`, never counted as a failure), an
  unresolved `TBD` artefact FAILS it (rc `> 0`).
- **`tan validate` in a fresh project answered "not ported yet" (exit 1) where
  the shipped binary answers `validate.board-yaml-missing` (exit 2).** The
  not-ported stub for the non-`--offline` spawn path short-circuited ABOVE the
  missing-`board.yaml` guard, so `tan validate` in an empty directory -- among
  the first things a brand-new user runs -- answered a different question than
  the oracle, with a different exit code. The guard now runs first, on both
  paths, under the existing `validate.board-yaml-missing` code.

  Measured against `target/debug/tan.exe` (`tan 0.4.1-dev`, `--format json`),
  not inferred from `crates/` and not taken from a report: an empty directory
  exits **2** `validate.board-yaml-missing` with `data.outcome: "failed"`; a
  project with `board.yaml` but no SDK root exits **2**
  `validate.sdk-root-unresolved`. Exit 1 is reachable in the oracle only AFTER
  a validator actually spawns and returns an unexpected status. The oracle
  therefore draws a line -- pre-spawn guards at 2, post-spawn failure at 1 --
  that the stub had flattened.

  An earlier revision of this entry claimed the opposite, and was wrong in both
  halves: that the oracle returns 1 for these cases, and that moving to 2 would
  be a considered BREAK for the next minor (#262). Neither was ever measured.
  For the guard cases 2 is what the oracle already does, so matching it is a
  compatibility fix. #262 is re-scoped to the one case that is a genuine
  release decision: `validate.failed` after a real spawn.

  **Corrected 2026-08 (v0.5.0): the paragraph this replaced claimed
  `validate.spawn-not-implemented` was still exit 1 at this port's rc1 tag.
  That was true when written and is not true of the current tree — #262 was
  decided and TAKEN in v0.5.0 (see that section above for the full BREAKING
  change): `validate.spawn-not-implemented` now also emits exit 2
  (`VALIDATION_FAILURE`), the same code the guard cases above already used,
  closing the divergence this paragraph used to describe as open.** The two
  genuine internal failures in the same file (an unreadable `board.yaml`, an
  unexpected exception inside the offline structural checker) are unchanged
  at exit 5, matching the oracle's offline path exactly.

- **`tan sdk install` / `tan sdk switch` refused at exit 5 (`InternalFailure`),
  telling CI and the extension that tan had crashed.** Neither is ported; that
  is a gap, not a crash. Both now exit 1 (`RuntimeFailure`) -- the code every
  other refusal in `sdk_cmd` already used (`sdk list` without `--online`, a
  bare `tan sdk`), the code the deferred-verb stubs settled on, and the code
  the oracle itself returns for a `sdk switch` that cannot resolve (measured:
  `tan sdk switch 0.0.0-nonexistent --format json` -> rc=1
  `sdk.path-not-found`). The 5 was justified in-code as "following
  `validate_cmd`'s precedent for its own unported spawn path" -- `validate_cmd`
  uses 1, so the comment cited a precedent for the opposite of what it did.

- **`tan init` could pin a customer to the WRONG SDK, permanently** (#263).
  `init`, `generate`, `examples` and `renode` were routed through the same
  SDK-root ladder as the other thirteen commands, whose last tier is the NARROW
  probe. The oracle carries two ladders, and those four take the WIDE one.
  Measured against `target/debug/tan.exe`, `HOME`/`USERPROFILE` isolated and
  `ALP_SDK_ROOT` unset: with a child `<ws>/alp-sdk` and a competing sibling
  `../alp-sdk`, the oracle resolves the child and the port resolved the sibling;
  inside an enclosing checkout, the oracle resolves the child and the port
  resolved the enclosing tree.

  `tan init` is the sharp one, because it WRITES `.alp/sdk-path` -- and that pin
  then outranks discovery for every later command in that project. A customer
  running `tan bootstrap` and then `tan init` beside a second checkout was bound
  to the wrong SDK for good, with nothing in any envelope saying so. Asserted on
  the FILE, not just the envelope: `.alp/sdk-path`'s `sdkPath` now holds the
  child in both cases. `build`, `doctor`, `sdk current` and `presets` are
  byte-identical to before -- their narrow ladder was always right.

  The issue's own prescribed fix -- inverting tier 4 for ALL commands -- was
  refuted by that measurement and deliberately not applied: it would have
  changed the thirteen too, tripping `build.sdk-switch-pristine` and deleting
  build directories. The five regression tests pinning the narrow ladder are
  untouched, precisely so nobody re-applies it.

- **`tan monitor` would have been a dead command in every published binary.**
  pyserial is an EXTRA (`[project.optional-dependencies] monitor`), so a frozen
  build carries it only if the build venv installed `.[monitor]`.
  `python-binaries.yml` did; `release.yml` -- the workflow a `v*` tag actually
  runs -- froze a bare `.`. So every asset this tag publishes would have
  advertised `tan monitor` in its `--help` and then refused to run it with
  `monitor.pyserial-missing`, whose hint (`pip install "alp-tan[monitor]"`) is
  the one thing a holder of a `--onefile` binary can never act on.

  Both workflows now freeze `.[monitor]`, and `tests/conformance/test_packaged_binary.py`
  asserts the frozen artifact reaches pyserial -- by issue code, not exit code,
  since a runner with no serial hardware exits non-zero either way
  (`monitor.no-port` is the pass, `monitor.pyserial-missing` the failure).

  Both halves measured on real freezes rather than reasoned about, because a
  gate that cannot fail is worse than none: with the extra the artifact is
  13805270 B and answers `monitor.no-port`; without it, 13729615 B and
  `monitor.pyserial-missing`. So PyInstaller does follow the lazy in-function
  `import serial` when it is installed, and does not acquire it otherwise -- no
  `--hidden-import` is needed and the extra genuinely decides the outcome.
  +75655 B, against 2.69 MB of headroom under the 16500000 B ceiling.

  The rationale in `build_binary.sh` said the opposite ("extras stay OUT ...
  freezing one in would make the binary disagree with what the wheel
  promises"), which inverts who can act: a wheel user can add an extra whenever
  they like, a binary holder cannot.

- **`install.sh` handed musl hosts a binary that cannot exec.** It maps every
  Linux host to `unknown-linux-gnu` with no libc detection. From v0.5.0 that is
  the only Linux asset, so on Alpine the failure mode is the bad one: not a
  checksum mismatch but a bare `not found` from the shell, AFTER the sha256
  verify has already passed -- so none of the four refusals fires and the
  script reports success. It also regresses people who worked before, since
  `latest` still resolves to v0.4.1 and its `tan-x86_64-unknown-linux-musl`
  asset is genuinely static. Now detected BEFORE the download (`ldd --version`
  naming musl, or the musl loader directly on images with no `ldd`) and refused
  with a reason and a pointer at the source install. README's manual-download
  recipe named the musl asset with the comment "musl = static, any distro";
  that asset is gone from this release on, and the static claim was never true
  of a PyInstaller freeze.

- **`tan <cmd> | head` exited 1 on Linux and macOS where the oracle exits 0.**
  Any reader that stops early (`head`, `grep -q`, a closed pager) closes tan's
  stdout mid-write. `__main__.py` guarded that, but on POSIX its `except
  OSError` arm is never reached: Rich's `Console._check_buffer` catches the
  `BrokenPipeError` and `Console.on_broken_pipe` raises `SystemExit(1)` before
  it -- and Typer's `_main` carries the same EPIPE-to-`sys.exit(1)` arm behind
  that. Both raise from INSIDE the block that caught the pipe, so the original
  error is left on `SystemExit.__context__`; following that chain is what
  identifies the case. `SystemExit(1)` alone is far too broad to remap (every
  failed `tan build` raises it), and the alternatives -- Click's
  `PacifyFlushWrapper` swap, Rich's `console.quiet` -- are private detail of
  someone else's library that an entrypoint has no business importing.

  Measured, not inferred: `tan 0.4.1` linux-musl writing its 4327-byte `--help`
  into a never-read 4 KiB pipe (`F_SETPIPE_SZ`, so EPIPE is guaranteed) exits 0
  with empty stderr; the port exited 1. Verified after: `tan generate --help |
  head -n1` under `pipefail` returns 0, `tan build` still 1, `tan bogus` still
  2, `--version` still 0.

  The Windows arm of the same guard (`errno.EINVAL` with `filename is None`,
  because CPython's Windows layer maps `ERROR_NO_DATA` to EINVAL rather than
  EPIPE) already existed and is unchanged -- it was factored into
  `_is_broken_pipe` here, not added. An earlier revision of this entry, and the
  message on the commit that carried the fix, both described that pre-existing
  half as the fix and called the defect Windows-only. It is the reverse:
  Windows was green throughout and POSIX was the broken platform.

- **The same `TBD` defect on the Rust side: a placeholder reached every
  flash backend as a real value** (#222).
  `TBD` is a deliberate alp-sdk convention — where the exact hardware
  configuration is not yet known the field is marked `TBD` rather than invented —
  so the convention itself routinely produces the value, and every guard in this
  area tested for *empty*, which is the one thing `TBD` is not. `fa_str`, the
  accessor twelve string reads across the backends go through, returned it:
  the literal string `TBD` became a `west flash --runner`, a build directory, a
  hex file, an OpenOCD config path and a `dd` destination. The same defect was
  measured in alp-sdk (`flash/mod.rs:307`), where it resolved to
  `<build_root>/TBD` and a real flasher was spawned against it.

  **There are two string accessors, and both carried the defect.**
  `plan_swd_probe` reads four fields — `base`, `jlink_device`, `interface`,
  `target` — through the *strict* `builders::fa_str_checked`, which had the same
  `!s.is_empty()` filter. That is the accessor whose whole reason for existing
  is the fields where a silent fallback to a baked-in default is dangerous, and
  two of the four reached a spawned command:

  - `jlink_device: TBD` has no validator behind it at all, so the plan was
    `JLinkExe -device TBD -if SWD -speed 4000 …` with `planning_only: false` —
    the alp-sdk sighting this issue reports, reproduced exactly.
  - `interface: TBD` / `target: TBD` are *worse* than unvalidated:
    `validate_identifier` accepts `TBD` (it is a plain alphanumeric identifier),
    and being non-empty it also suppressed the "`interface` and `target` are
    required" refusal — so the plan was
    `openocd -f interface/TBD.cfg -f target/TBD.cfg -c "program … verify reset exit"`.
    The correct error was silenced *because* the placeholder is not empty, which
    is the issue's thesis word for word.
  - `base: TBD` was already refused, but by `validate_address` happening to
    reject a non-hex charset — safe by a second guard, not by the accessor.

  There the placeholder is an **error**, not absent: reading an unfilled
  `jlink_device` as absent would flash a GD32G553 with `DEFAULT_JLINK_DEVICE`,
  and an unfilled `base` would program real silicon at `DEFAULT_BASE`, both
  while the manifest was saying "not yet known". This makes the string member of
  the `_checked` family agree with `fa_bool_checked`/`fa_int_checked`, which
  already hard-error on a present `TBD`. The tolerant `fa_str` reads it as
  absent because its callers' defaults are safe; the strict one refuses.

  Fixed once per accessor, not at the call sites: `TBD` now
  reads as **absent**, which is what it means. Every caller's existing
  `unwrap_or_else(default)` / `if let Some` then treats it as the unfilled field
  it is, with no new branch anywhere — `build_dir` falls back to the computed
  Zephyr build dir instead of a literal `TBD` directory, `target` to `flash`,
  `bs` to `4M`. It also matches what tan already did with the whole entry
  (`commands::flash` SKIPS a target whose `flash_args` contains `TBD`) and what
  the sibling `fa_bool_checked`/`fa_int_checked` already did with a bare
  `flash_args: TBD`.

  The sentinel is now defined ONCE (`PENDING_PLACEHOLDER` /
  `is_pending_placeholder`) rather than written out at four sites — the fourth
  being `sdk_catalogue::parse::is_tbd`, which four comments in `commands::flash`
  and `commands::image` cite *by name* as the definition of the convention. The
  failure mode of four copies is a fifth reader written without one, and
  `fa_str_checked` was that fifth reader.

  Two readers outside the flash path are knowingly left separate: `pinmux` drops
  a `TBD` `e1m_pad` as a sentinel row (that silicon pad has no E1M edge ball),
  and `size::resolve_variant` skips a `TBD` `silicon_variant` before its reverse
  SKU lookup. Different schemas, different questions, and neither plans a flash
  write. Both compare untrimmed, so `' TBD '` slips past them — filed as #276
  rather than folded in here, along with the open question of whether
  `PENDING_PLACEHOLDER` should live somewhere more neutral than `flash::args`.

  Every `fa_str` call site was audited for whether "absent" is safe there. Each
  is either already protected by a closed-set check that `TBD` also failed
  (`plan_yocto_wic`'s required-`target` plus its `/dev/` prefix refusal,
  `plan_xspi_flashwriter`'s `mtd0`/`mtd1`) or strictly improves. None was found
  where absent is more dangerous than the placeholder was — which is precisely
  why the four `fa_str_checked` fields got the opposite answer.

- **`project.boardYaml` now agrees with the filesystem in both directions**
  (#236, #170). The field's own contract has always read "if found", and the
  code returned a path either way: twenty commands each cloned
  `context.board_yaml_path` straight into the envelope, and the resolver builds
  `<root>/board.yaml` unconditionally — so a run in a directory with no
  `board.yaml` reported one, and a consumer that opened it got ENOENT. #170 is
  the same field failing the other way; its fix routed `debug-config` through
  that shared resolver, which without this would have traded a hardcoded `null`
  for a path to a file that need not exist.

  Both ends are fixed at the reporting seam — a new `Project::from_context`
  (plus `from_debug_context`, which reuses the `board_yaml_exists` flag
  `doctor`/`inspect`/`support-bundle` already carry) that all twenty sites now
  route through. Deliberately NOT in `resolve_project_context`:
  `board_yaml_path` is the path commands ACT on, and several need it precisely
  when the file is absent — `doctor`'s `read_board_model`, `validate`'s
  "does not exist" refusal, and `create_debug_workspace_context`, which mirrors
  the TypeScript side by carrying the path and a separate existence flag.
  Emptying it there would have stripped the path out of every "no board.yaml at
  `<path>`" message.

  Two further sites build the block by hand and were the starkest instances:
  `generate` and `validate --offline` each reported the path one line above the
  refusal that says the file does not exist. Both now report `null` and keep
  naming the path in the message.

  **Wire change (reporting only).** Seven golden envelopes move
  `project.boardYaml` from a path to `null` — the four `debug-config-preview-*`
  cases, both `presets-*` cases, and `generate-board-yaml-missing`, every one of
  which runs in a scratch directory that provably has no `board.yaml`. Nothing
  in `alp-sdk-vscode` reads the field; its only declaration
  (`src/alpCli/models.ts`) is already `string | null`. `project.root` is
  untouched — #236 rules that question explicitly out of scope.

- **`tan build --pristine` now reports the slices it did not wipe** (#183).
  `--pristine` has three paths that correctly decline the wipe, and all three
  were silent: the slice carries an explicit `-d`/`--build-dir` (west wrote
  somewhere tan cannot know), the plan cwd does not normalise under `build/`
  (the wipe target could hold files the build never created), or the build dir
  has no `CMakeCache.txt` (never configured, nothing to wipe). The run then
  exited 0 having done an incremental build the user had explicitly asked not
  to have — so a stale artefact sent them debugging the artefact, not the flag,
  and the command had told them it handled it. Each suppressed slice now emits
  `build.pristine-skipped` (severity `warning`, registered `reserved`) naming
  the slice and which of the three reasons applied, plus a `note:` line in text
  mode.

  The decision is one pure function, `tan_core::plan_exec::pristine_suppression`,
  with one emit site placed **outside** the two-guard block in
  `crates/tan-cli/src/commands/build/execute/mod.rs` — a message written into
  either guard could not have seen the other, and neither could have seen the
  third path, which sits inside both. That third path is also the only one a
  stock plan reaches: `write_sdk_stamp` itself `create_dir_all`s `<cwd>/build`
  to write `.tan-sdk-root`, so after any prior slice the directory exists
  holding nothing but tan's stamp, and every subsequent `--pristine` declined
  silently. The two named in the issue require a hand-written plan.

  No suppression changed. All three remain exactly as `#163` mutation-proved
  them; only the silence is gone.

- **A write could silently replace a hand-filled `.vscode/launch.json` value**
  with one resolved from the SDK's identity above, at `exit 0` with
  `issues: []` (alp-sdk#1026 review). The overwrite itself is the same,
  by-design behaviour a value resolved from a real build has always had; the
  defect was that it was undisclosed for this new source. A write that
  replaces a concrete existing `device`/`targetId`/`configFiles` value now
  emits a new `debug-config.sdk-identity-overwrite` (reserved) issue naming
  the old and new values.

- **Linux and macOS now see the manual-install hints the SDK provides for them**
  (#230). alp-sdk v0.14.0 added `manualInstallHints.posix.note` — the POSIX twin
  of the Windows key `tan bootstrap` already printed — and tan did not read it, so
  the data existed and was inert. Three facts reached no POSIX customer: that the
  Zephyr SDK is a separate, manual `west sdk install` (with the exact invocation),
  that the Arm GNU Toolchain is a separate install needed by three opt-in paths,
  and why `west sdk install` may print a `could not find a 'file' executable`
  warning that is harmless. The same class as 7-Zip being prose-only before #204,
  one file over.

  Shape, heading and position come from the oracle rather than from tan:
  `v0.14.0:scripts/bootstrap.sh` prints these under `NOT auto-installed (manual,
  one-time):` for `linux|macos` only, after the optional-native-libs section. The
  host gate is `Linux | MacOs` and deliberately **not** "anything that is not
  Windows" — `HostOs::Other` is a host tan could not identify, and every sentence
  in the note is Linux/macOS-specific.

  An SDK that declares no `manualInstallHints.posix` (every release before
  v0.14.0) renders exactly what it always did. The field is `Option` for that
  reason: a required field would turn each of those SDKs into a hard
  `ValidationFailure` that `tan build` inherits through auto-bootstrap.

## [0.4.1] — 2026-07-29

### Added
- **A CI job that actually runs the commands a customer types.** Until now NO
  workflow in either repo ran `tan bootstrap`, `tan init` or `tan build` --
  grepping `.github` for them returned three comment lines and no `run:` step.
  `parity.yml`'s `seam2` hand-assembles the workspace itself (`west init -l` /
  `west update` / `west zephyr-export`) and then builds an example that already
  exists, so it exercises the loader seam but never the customer path. Every
  regression in the venv phase, the west phase, the prerequisite gate or the
  workspace guard could reach customers with a green board. The new
  `first blink` job runs bootstrap -> doctor -> init -> build in that order on
  a runner that starts with none of it, and asserts the workspace artefacts
  (`.west/config`, `.venv`, `zephyr/`, `modules/`) actually exist rather than
  trusting exit 0. It clones alp-sdk into a DEDICATED parent (`ws/alp-sdk`),
  which is both what #185's workspace guard requires and the layout README
  tells customers to use -- a flat checkout beside the tan-cli tree would make
  `tan bootstrap --non-interactive` exit 2 by design.
- **`getting-started.yml` — the first CI job that runs `install.sh`,
  `tan bootstrap` or `tan init` at all** (#207). Before this file, a grep for
  tan subcommand invocations across `.github/**` returned three COMMENT lines
  (`parity.yml`) and no `run:` step, and `install.sh` was neither executed nor
  shellchecked by any workflow in either repo — so the whole
  environment-provisioning path a first-install customer walks (installer →
  venv → west workspace → scaffold → build) had no signal, and a regression in
  any phase of it would reach customers with an otherwise-green board. The
  `install.sh` evidence that existed was #176's one-off manual Ubuntu 20.04 LTS
  run, which is good evidence and which nothing preserved.
  The new job walks the README Quickstart in order on a stock `ubuntu-latest`:
  `shellcheck --shell=sh install.sh`, then `./install.sh` (a real GitHub
  Releases download verified against that release's `checksums.txt`, so the
  sha256 path is exercised rather than mocked), then `tan bootstrap`, then
  `west sdk install --version <v> -t arm-zephyr-eabi` — the remedy
  `build_readiness.rs` PRINTS and tan deliberately does not run itself, so
  running it here is the only way the printed remedy gets tested — then
  `tan init --name my-app` on its defaults with no TTY to prompt on (the
  #187/#198 hang class), then `tan validate` from the scaffolded project so the
  Quickstart's sibling `../alp-sdk` resolution is what resolves the SDK, then a
  real `tan build`, asserted down to an ARM ELF rather than to an exit code.
  Three deliberate choices, each of which would otherwise read as an
  inconsistency:
  1. **The job runs this checkout's `install.sh`, not the documented
     `curl … | sh`** — that pipe fetches `main`'s copy, so a PR editing
     `install.sh` would get no signal from its own diff. Same bytes, different
     transport.
  2. **`install.sh` installs the last RELEASE, so the binary it installs is
     replaced by a `cargo build` of the PR at the same path** before bootstrap
     runs. Otherwise every step after the installer would test a binary that
     predates the diff, and a bootstrap or init regression would sail through
     green. The installer is verified for real first; the swap is what gives
     the rest of the path its signal.
  3. **alp-sdk is cloned into a dedicated `alp-workspace/` parent**, which is
     exactly what the workspace-parent guard's own migration note in this
     release predicted a fresh `tan bootstrap` job would need: the guard REFUSES
     outright under a CI runner's non-terminal stdio, so cloning beside this
     repo's own checkout would fail before bootstrap did any real work.
  The alp-sdk pin is READ out of `parity.yml`'s `PINNED_SDK_TAG` rather than
  copied, and the Zephyr SDK version out of the SDK's own
  `metadata/toolchains.json` (the same read `seam2` does, and the same file
  `tests/parity/toolchain_lock_parity.py` holds `host_env.rs`'s
  `ZEPHYR_SDK_INSTALL_VERSION` against) — a second hardcoded copy of either
  would rot independently and let this gate quietly test a different SDK than
  the parity gate while both reported green. Both reads hard-fail rather than
  falling back to a default, since a silent fallback is worst in exactly the
  case where somebody moved the pin without knowing this file reads it.
  Deliberately not `paths`-filtered: a gate that skips itself on a "docs-only"
  PR reports green for a run that checked nothing.
- **`tan debug-config --svd <PATH>` — a user-supplied SVD, the first and only
  producer of `LaunchResolution.svd`** (#197). The resolution path for
  `svdFile`/`svdPath` was complete and tested, and structurally dead: the field
  is read at `debug_launch.rs:245` and was assigned nowhere in the workspace
  outside tests, so it was always `None`, both keys were always dropped, and
  cortex-debug's Cortex Peripherals (register) view was unreachable on every
  target tan supports. A green test suite said nothing about whether the
  feature could work, because the drop path's input could never be anything
  else.
  The SDK ships no SVD and may never ship one — alp-sdk#948's blocker is an
  unresolved Alif/Renesas redistribution-licence question on a public repo. A
  user-supplied path is the only route that survives that question going either
  way: a customer who downloaded the vendor's own SVD, which they are entitled
  to do, can point tan at it today.
  Two behaviours decided explicitly rather than by omission, since #67
  established what a bad `svdFile` costs:
  1. **A path that does not name a readable file FAILS the command** (no
     launch.json written) — it never falls back to dropping the key. That
     fallback would make a typo indistinguishable from not passing the flag,
     and the user explicitly named the file. A directory and an empty string
     are refused the same way.
  2. **A relative `--svd` anchors on the CURRENT DIRECTORY**, not the project
     root, because a flag typed at a shell prompt means what the shell means by
     it. The emitted value then goes through the same `workspace_relative`
     rewrite as `executable`: `${workspaceFolder}/…` inside the project,
     absolute outside it — and outside is the normal case, since a vendor SVD
     lives in the vendor SDK.
  A `--svd` on a target kind whose draft carries no `svdFile` field
  (`native-host`, `yocto-userspace`) is reported as a note rather than accepted
  in silence. A `debug.svd` key in `board.yaml` is deliberately NOT part of
  this change: it has a different lifetime (it travels with the project, so it
  would anchor on the project root) and belongs with the alp-sdk metadata
  contract, not with a per-invocation flag.

### Changed
- **Re-vendored against alp-sdk `v0.14.0`, and macOS now reads its own
  prerequisite list.** `parity.yml`'s `PINNED_SDK_TAG` moves from a dev commit
  (`cdfe1368`) to the release tag — reproducible where a dev tip is not, and the
  pin that all four parity gates resolve their SDK checkout from, so it is one
  atomic bump rather than four. It carried: the bootstrap manifest fixture
  (5577 → 7498 bytes), seven wizard-scaffold `README.md` files re-vendored from
  the live emit, and `MANIFEST.md`'s vendor point.

  v0.14.0 added `xz` and `wget` to `prerequisites.posix` **and** a separate
  `prerequisites.macos` that omits them. tan keyed its prerequisite list off an
  `is_windows` bool, so the re-vendor alone would have handed macOS the POSIX
  list and made `tan bootstrap` refuse outright on a stock macOS host — which
  ships neither `wget` nor a standalone `xz` — for tools the SDK does not ask
  macOS for. `BootstrapFacts::prerequisites` now takes the HOST. An undeclared
  `prerequisites.macos` (every SDK before v0.14.0) still falls back to `posix`,
  so the fallback is the old behaviour rather than a new guess.

  Also recorded rather than left to rot: `manualInstallHints`' doc comment
  claimed POSIX hosts have no manual-install fact "until one exists". v0.14.0
  added `manualInstallHints.posix.note`; tan does not read it yet, so that hint
  reaches no tan surface. A missed improvement, not a regression — serde ignores
  the key — and the same class as 7-Zip being prose-only before #204. Tracked as
  #230.
- **`tan bootstrap` no longer reports success after failing to install the
  dependencies its own next step needs** (#220). Measured end to end in one
  `first blink` run: the Zephyr requirements install died on `libudev.h`,
  bootstrap printed `bootstrap: complete.`, and the build that followed died
  with `ModuleNotFoundError: No module named 'elftools'` — three commands from
  the cause, with nothing linking back. That is a false success, not a warning.

  The fix is to the **verdict, not the phase**. Each install stays non-fatal:
  the run continues, the workspace is left on disk, and nothing downstream is
  blocked — a venv missing `hidapi` still builds `native_sim` perfectly well, so
  refusing outright would cost more customers than it saves. What changes is
  that a run which cannot do what it was bootstrapped for now says
  `bootstrap: INCOMPLETE`, names which installs failed, and exits 1.

  Three of the four pip phases count: `zephyr-requirements`, `sdk-extras`
  (`jsonschema`, which the loader imports) and `editable-install`.
  `pip-upgrade` is deliberately excluded — the pip already present still
  installs packages. **`--allow-partial`** reports success anyway, for the case
  where the missing packages are ones you know you do not need; it still prints
  what is missing, so it is an informed choice rather than a mute override. The
  blocking issues are raised at `error` severity only when they actually blocked
  the verdict — under `--allow-partial` they stay `warning`, because an `error`
  on a run that exits 0 is its own kind of lie.

  This BREAKS PARITY with alp-sdk's `bootstrap.sh` / `bootstrap.ps1`, which
  still warn and report success. `pip_phase`'s doc comment claimed the phases
  were non-fatal "matching both scripts"; that claim is now false and has been
  replaced with the divergence and its reason, rather than left to rot. Moving
  the scripts is alp-sdk#1038's decision.

### Fixed
- **`tan bootstrap --print-env` wrote to stderr, so the one thing it exists to
  produce could not be captured** (#227). Both obvious ways of consuming it
  produced nothing, and neither errored:
  ```sh
  eval "$(tan bootstrap --print-env)"                  # evaluated ""
  tan bootstrap --print-env > env.sh && . ./env.sh     # sourced ""
  ```
  `sh -n` on the empty file passed too — an empty file is a valid shell script
  — so even a check that the emitted block *parses* could report green having
  read nothing. That is exactly what `getting-started.yml`'s own `--print-env`
  step (#216) did: it passed while asserting nothing at all, and its uploaded
  `gs-print-env.sh` artefact was empty.
  The block now goes to **stdout**, and it is the only text output in tan that
  does. Sending text to stderr so `--format json` owns stdout outright is the
  right rule and every other command keeps it — but `--print-env` is not a
  report *about* a run, it is the run's product: a shell fragment whose whole
  purpose is to be consumed, in a CLI whose every other machine-readable
  surface is `--format json`. `--print-env`'s help text now says so.
  JSON mode is unchanged: the envelope remains the only thing on stdout there,
  and it already carries every path the block renders (`data.zephyrBase`,
  `data.venvDir`), so a JSON consumer never needed the shell spelling.
  Printed at the `--print-env` short-circuit rather than through `emit`,
  because `emit`'s two channels *are* the two streams (`json` → stdout, `text`
  → stderr) and threading a third through `CommandRun` would touch all 174 of
  its construction sites to serve one flag; `text` is returned empty so the
  block is never also written to stderr. Verified: stdout 480 bytes, stderr 0.
  Two permanent gates, in the places that were fooled: the #217 regression
  suite now reads the block off stdout and asserts stderr is empty in both
  modes, and `getting-started.yml`'s step uses a plain pipe again (no `2>&1`),
  so a return to stderr empties the capture and fails the size check.
- **`tan bootstrap` reported cwd-relative paths in `data.*` and `--print-env`
  while `project.root` in the same envelope was absolute** (#217). Every path
  the command reports is derived from `--sdk-root`, and the flag's spelling
  travelled all the way out: the README Quickstart's own
  `tan bootstrap --sdk-root ./alp-sdk` produced `data.workspaceDir: "."`,
  `data.venvDir: "./.venv"`, `data.zephyrBase: "./zephyr"` and
  `sdk.root: "./alp-sdk"` — beside a `project.root` that was already absolute.
  One envelope described one directory two ways, so a consumer had no way to
  know which fields it still had to resolve, and the failure mode was silence
  rather than an error: a relative path prepended to `PATH` works in the
  topdir and resolves to nothing anywhere else. `--print-env` carried the same
  values into a block whose own heading reads *"Add to your shell profile"* —
  a profile is sourced from `$HOME`, where `./zephyr` is not a directory.
  `sdk_root` is now absolutized once, before anything is derived from it, and
  the recorded `sdk.root` is overwritten to match (`sdk_report::record`'s
  first-writer-wins had already captured the pre-absolute string during
  project resolution — `override_after_relocation` is generalized to
  `override_root` and now serves both callers).
  `std::path::absolute`, deliberately **not** `canonicalize`: canonicalize
  requires the path to exist, and this runs before any phase has created
  anything; it also resolves symlinks, which would hand a customer back
  `/private/tmp/…` for the `/tmp/…` they typed. An already-absolute
  `--sdk-root` is therefore reported verbatim, which is its own regression
  test.
  Surfaced by `getting-started.yml` (#207) on its first run. That job now
  ASSERTS the four `data` paths are absolute instead of calling
  `os.path.abspath` on them, so the workaround cannot quietly outlive the fix.
  Writing the regression test also found #227 (`--print-env` writes to stderr,
  so the job's `| tee` had been capturing zero bytes and `sh -n` was reporting
  green on an empty file); that step now redirects and refuses an empty
  capture, and the underlying stream question is filed separately.
- **A tag whose CHANGELOG section was never written published a release whose
  entire body was one stub sentence, and nothing said so** (#212). The notes
  step fell back to `print(body if body else f"See CHANGELOG.md for {version}.")`
  and exited 0. The failure mode is not hypothetical: the version bump is what
  renames `## [Unreleased]` to `## [X.Y.Z]`, so a bump that edits the version
  files and forgets the CHANGELOG header lands exactly here — measured against
  `dev` before the fix, a `v0.4.1` tag found no section and would have shipped
  the stub. A release with no notes is not degraded but broken (they are the
  only human-readable record of what changed, and a tag is immutable once
  pushed), so the step now fails before publishing, naming the header it
  expected. An empty section fails the same way. Driven against the real
  CHANGELOG in all three states: missing → exit 1, empty → exit 1, present →
  exit 0 with the section extracted.
- **41 emitted issue codes were in the registry at no status, which left them
  ungated on BOTH sides of the seam at once** (#219). `frozen_issue_codes` only
  ever walked registry → source, checking each entry still exists. Nothing
  walked source → registry. An unregistered code was therefore invisible to
  this repo's checks (they iterate the registry) *and* to alp-sdk-vscode's
  (their gate reads the published artefact, which is built from that same
  registry) — so renaming one was silent in both repos simultaneously.
  `every_emitted_issue_code_is_registered` now walks the other way and fails
  when an emitted code has no entry, naming the code and the file.

  They are registered **`reserved`, not `frozen`**. The registry's own policy is
  promote-on-binding; freezing codes no consumer reads would over-commit and
  turn every future internal rename into a contract break for nobody's benefit.
  `reserved` is the declared-but-uncommitted state that already existed for
  exactly this — the file explicitly says not to invent a third status — so a
  reserved code may still be renamed freely, while the gate can finally see it.
  No new status was added.

  One consequence worth stating: the published `envelope-contract.json` carries
  the registry whole, so it grew from 4 issue codes to 68 (5 frozen, 62
  reserved, 1 retired) and from 20081 to 73091 bytes. That is the point — the
  asset presented itself as the contract while covering a fraction of what tan
  emits, and a silently-partial artefact is worse than either honest option. A
  consumer reads `status` to decide what a code promises; alp-sdk-vscode's
  reader is already status-aware and looks its own codes up in that map, so the
  extra entries change nothing for it.

  KNOWN CEILING, recorded rather than papered over: the scan matches the literal
  `code: "x.y"` shape, so codes assembled by a prefixing helper
  (`format!("bootstrap.{code}")`, `debug_config.rs`'s `failure_envelope`) are
  invisible to it. Today that is no hole — all of them are registered and
  `frozen_issue_codes` back-checks each — but a NEW code in one of those two
  files would still escape. Tracked as #224.
- **The Zephyr SDK — the one build-blocking `Fail` a first-install customer
  hits — was the only prerequisite that could never carry a Fix button**
  (#203, #210). `missingPrerequisites` was written in exactly one place,
  `push_tool`, whose shape is "PATH binary, keyed into the manifest's
  `prerequisites.install` map". `zephyrSdk` is neither — it is an env/install-dir
  detection with a command the SDK does not own — so it was pushed straight to
  `checks` and silently never reached the list. The extension reads that list to
  decide a check is actionable, so CMake, Ninja, git and Python all rendered
  with a Fix button and the one row that stops the build rendered without one.
  It is now recorded like every other absent prerequisite, carrying the real
  `west sdk install --version 1.0.1 -t arm-zephyr-eabi` rather than `null` — the
  exact command alp-sdk#855's fresh-host reporter needed and could not find. tan
  still does not run it; a download-and-extract is west's job once a workspace
  exists, not a read-only doctor's side effect. That command is also now
  assembled in ONE function instead of formatted at four call sites, so a
  version bump cannot leave the button running a different toolchain than the
  prose beside it recommends.
- **7-Zip is a hard prerequisite of `west sdk install` on native Windows and had
  no check anywhere** (#204). west delegates `.7z` extraction to `patoolib`,
  which shells out to an external `7z`/`7za`/`7zr`/`7zz`/`7zzs`/`unar` and has no
  pure-Python fallback, so without one the install dies inside patoolib with an
  error naming no Alp surface and no mention of 7-Zip. The fact existed in
  exactly one place — a prose line in `tan bootstrap`'s TEXT output
  (`manualInstallHints.windows.note[1]`) — reaching no JSON consumer, so the IDE
  could not surface it either. `doctor --build` now emits a `sevenZip` check
  with a runnable `winget install -e --id 7zip.7zip` (verified resolvable before
  being written down, not guessed). Native Windows only, and only while the SDK
  is still absent: the extractor is irrelevant once the toolchain is installed,
  so the row appears beside the `zephyrSdk` Fail it unblocks and vanishes with
  it rather than lingering as noise on every later run.
- **Every remedy `tan doctor` computed was hidden unless you already knew to
  pass `--verbose`** (#208). The "Next steps" block was gated on that flag, so
  the person who runs `doctor` *because* something broke — the one person who
  does not know to re-run it louder — saw the failures and none of the commands
  that repair them. It renders at default verbosity now. `--quiet` still
  suppresses it, and a clean report has no steps to print, so nothing changes on
  a host with nothing wrong.
- **Plain `tan doctor` reported debug readiness for `native-host` / `none` no
  matter what board the project declared** (#208). An absent `--target-kind`
  parsed to `native-host` and an absent `--server` to `none`, so a Zephyr project
  got a CodeLLDB verdict and not one word about a probe or a GDB server — every
  debug row was answering a question about a different target. The default is
  now derived from the project's own `board.yaml` (`zephyr` → `zephyr-mcu`,
  `baremetal` → `baremetal-mcu`, `yocto` → `yocto-userspace`; MCU-class first on
  a multicore board), with the server taken from that target's own supported
  list rather than a second hardcoded default that would have disagreed with it.
  No `board.yaml` still means `native-host` — the one case it is right, and the
  directory alp-sdk's bootstrap tells a customer to run `tan doctor` from. An
  explicit `--target-kind` / `--server` still wins outright.
- **`tan init` could not find the SDK `tan bootstrap` had just set up, breaking
  the documented Quickstart at step 3** (#218). SDK discovery checked the
  workspace root itself, its siblings (`../alp-sdk`, `../alp-sdk-upstream`) and
  then its nearest ancestor -- never a CHILD. That misses the exact moment the
  documented flow puts the user in: bootstrap runs with the checkout at
  `<ws>/alp-sdk`, and `tan init` is then typed from `<ws>`, because the project
  directory it is about to create does not exist yet. The checkout only becomes
  the documented "sibling `../alp-sdk`" one step later, once the user has cd'd
  into the new project -- which is why `tan build` worked while the `tan init`
  immediately before it failed with "alp-sdk root is unresolved" and no hint
  that the answer was one directory down. Discovery now also considers
  `<root>/alp-sdk`, ordered ahead of the siblings so a workspace holding both
  prefers the one bootstrap actually set up. Found by the new `first blink` CI
  job, which had to pass `--sdk-root alp-sdk` to get past it; that flag is now
  removed, so the job is the regression test.
- **`baremetal-mcu` × OpenOCD shipped a resolved `serverpath`/`searchDir` with
  NO `configFiles` to load, and `baremetal-mcu` × pyOCD had no target to
  select** (#139). `create_launch_draft`'s `BaremetalMcu` arm was ONE
  un-branched object for every server, unlike `ZephyrMcu` right above it,
  which already branches. `resolve_from_build` computed `config_files` and
  `target_id` for every target, but `apply_launch_resolution` only replaces a
  key the draft already carries — and the `baremetal-mcu` draft carried
  neither — so both values were silently discarded while OpenOCD's
  `serverpath`/`searchDir` extras (written unconditionally) still landed.
  `jlink` was unaffected, which is exactly why this shipped: a jlink-only
  bench pass proved nothing about the other two servers. `BaremetalMcu` now
  branches per server the same way `ZephyrMcu` does — OpenOCD gets
  `configFiles`, pyOCD gets `targetId` — so `apply_launch_resolution`'s
  existing `contains_key` guards finally have something to fill. Reachable
  hermetically: the `debug-config-preview-baremetal-mcu` golden (server
  `openocd`) now pins `configFiles` in the emitted `configuration`.
- **`tan debug-config`'s envelope reported the pre-merge draft's own
  `<resolved-…>` placeholders even after a write merged in the customer's
  real, hand-filled values — so a file a merge or legacy migration genuinely
  FIXED was reported back as still broken** (#180). `debug_config.rs`'s
  `success()` set `data.configuration` to `draft.clone()` unconditionally, in
  every mode; the merged document `create_launch_json_write_plan` computes
  internally was never put anywhere the caller could read it. Concretely: seed
  a legacy `"ALP: Zephyr Debug (J-Link)"` entry with a hand-filled
  `"device": "AE822F4M55_HP"`, run `tan debug-config`; the file on disk ends
  up with the real device, but the envelope's `data.configuration.device` read
  `"<resolved-device>"`. alp-sdk-vscode#342 folds exactly this field
  (`written.configuration`) into its debug-readiness report, so the customer
  was told placeholders remained on a file that no longer had any.
  `LaunchJsonWritePlan` now carries its own `written_configuration` — the
  entry actually written, merged or appended — and `debug-config.rs` reports
  THAT for a write, never the stale draft. `--preview` is unaffected by
  construction: it returns before the customer's file is ever read, so the
  draft IS the only configuration there is to report — proven by a new test
  and unmoved by the four `debug-config-preview-*` goldens (which never merge
  and therefore never had this bug).
- **A leftover legacy `"ALP: ..."` launch-configuration entry sitting
  alongside the maintained `"Alp: ..."` one was left completely silent, even
  for the customer whose real hand-filled values are still stranded on it**
  (#179). When BOTH a current-named and a legacy-named entry exist at once (a
  workspace that ran a pre-#155 `tan` and then a post-#155 one), the ordinary
  same-name merge runs on the `Alp:` entry and — correctly, per #133's own
  data-safety call — never touches the `ALP:` one, since nothing decides
  which of two possibly-hand-edited entries is authoritative. That
  correctness was undocumented at runtime: no `issues[]` entry, no text-mode
  line, `migrated_from: None`. A customer in exactly this shape gets `F5`
  aimed at the entry with the unresolved placeholder while their real values
  sit one entry down, and `tan` reports bare success. `create_launch_json_write_plan`
  now reports a new `legacy_entry_present` alongside `migrated_from`, and
  `debug-config` surfaces it as a new `debug-config.legacy-entry-untouched`
  issue (severity `info`, registered `reserved` in `contract/issue-codes.json`
  — no consumer yet), shown in text mode even under `--quiet` like the
  migration notice beside it. Still silent on the common cases that must stay
  silent: an ordinary re-run with no legacy entry anywhere, and the MISS/
  migration path itself (which already reports the same fact via
  `migrated_from` and must not double-report it).
- **`tan debug-config` hardcoded `project.boardYaml: null` on every
  invocation, even a success with a valid `board.yaml` sitting in the
  resolved project root** (#170). Every other command that reports a
  `Project` (`bootstrap`, `doctor`, `presets`, `validate`, …) populates
  `board_yaml` from the shared `resolve_cli_project_context` resolver;
  `debug-config` was the one holdout still hardcoding it `None`. It now uses
  the same resolver. Reporting-only — no consumer binds `project.boardYaml`
  yet, so nothing downstream changes behaviour — but a consumer that DID trust
  it to mean "no board.yaml was found" would have drawn the wrong conclusion.
  The four `debug-config-preview-*` goldens move as a direct, intended
  consequence: they run in a directory with no `board.yaml` on disk, and this
  resolver reports the CANDIDATE path unconditionally rather than only when
  the file exists — matching every other command's own pinned goldens
  (`presets-no-sdk`, `presets-heterogeneous-som`), not a new inconsistency.
- **The npm shim (`@alplabai/tan`) was pinned six releases behind — `npm i -g
  @alplabai/tan` would have fetched `v0.1.1`'s binaries under the current
  release tag.** `npm-shim/postinstall.js` resolves its download tag from
  `npm-shim/package.json`'s own `version` field, not the workspace version,
  and nothing enforced the two staying in sync — `npm-shim/README.md` only
  *documented* bumping both by hand. Measured on this machine: `Cargo.toml`
  `0.4.1-dev` vs. `npm-shim/package.json` `0.1.1`. Bumped the shim's version to
  match the workspace, and `release.yml`'s `verify-version` job now fails a
  tag outright if the two disagree again (same grep-based style as its
  existing tag-vs-`Cargo.toml` check, not a `cargo metadata`/JSON parse).
- **`tan bootstrap`'s POSIX "Next steps" told the customer to run a raw `west
  build -b native_sim/native/64 examples/peripheral-io/uart-echo --
  -DEXTRA_ZEPHYR_MODULES=$PWD`** — wrong twice over. `$PWD` resolves to the
  alp-sdk checkout only when the reader's shell happens to be sitting in it,
  which is never true right after the workspace-parent guard has relocated
  the checkout (the Windows arm already used the correct `tokens.sdk_root`
  instead; POSIX did not). And `west build` bypasses `tan` itself, contradicting
  README.md's own claim that tan is "the single executor and the user command
  surface." Now prints `tan build --sdk-root "<sdk_root>" --project
  "<sdk_root>/examples/peripheral-io/uart-echo"`. `tan build` has no
  board-override flag yet, so it cannot select `native_sim`; POSIX now suggests
  the same real-silicon target the Windows arm already does (verified live:
  `tan build --plan` against the pinned example resolves that exact slice).
- **A relocated alp-sdk checkout broke README.md's own next documented step.**
  `tan bootstrap`'s workspace-parent guard can move the checkout into a
  sibling `alp-workspace/` directory (#185); afterward, `tan init --name
  my-app` — run in the same shell, or a fresh one tomorrow — had no sibling
  `../alp-sdk` left to auto-discover, and `tan build` in the new project
  failed with "alp-sdk root is unresolved." The relocation now also repoints
  the machine-global default-SDK pointer (`~/.alp/sdk-default`) at the
  checkout's new location — which `tan init` already pins into every new
  project's own `.alp/sdk-path`, so closing the gap needed no change to
  `init` itself. Chosen over printing a corrected next command: a pointer
  file survives a closed terminal and a new shell tomorrow; printed text does
  not.
- **`tan init` no longer blocks forever when stdin is not a terminal** (#187).
  It rendered an inquire prompt (`Destination directory:`, ANSI escapes and all)
  to a terminal that was not there, then blocked on a stdin already at EOF --
  no timeout, no diagnostic, no exit. From the caller's side that is
  indistinguishable from a slow operation, so every non-TTY caller hung: CI, a
  `sh -c` from a script, an IDE task runner, a `Command::output()` from another
  tool. `--non-interactive` was the only workaround and nothing said so.
  The missing term was `std::io::stdin().is_terminal()`, which `bootstrap`
  already had; `init`'s own comment shows the `--format json` half of the same
  hang was fixed earlier, and the no-TTY half was simply never added. The
  predicate is now the pure, unit-tested `interactive_mode()`.

### Changed
- **`contract/issue-codes.json` was a strict subset of what `tan` actually
  emits — sixteen real codes were reachable with no registry entry at all, so
  `frozen_issue_codes` gated nothing about them and a rename would have been
  invisible in both this repo's CI and alp-sdk-vscode's simultaneously**
  (#111). An initial pass here registered only five of those (below); a
  follow-up audit of every `Issue { code: ... }` / `Log::warn` / `failure()`
  call site under `crates/tan-cli/src/commands/bootstrap/` and
  `debug_config.rs` found eleven more still unregistered and reaching the
  wire — `bootstrap.zephyr-base-stale`, `zephyr-base-incompatible`,
  `west-config-reconciled`, `west-config-reconcile-failed`, `pip-upgrade`,
  `zephyr-requirements`, `sdk-extras`, `editable-install`, `failed`, and
  `debug-config.internal-failure`, `write-failure`. All eleven are now
  registered `reserved` too, so the registry actually covers every code these
  two command families emit, not just the ones a prior pass happened to name.
  `contract/README.md`'s frozen-codes table now states its own selection
  criterion (`frozen`/`retired` only; `reserved` codes are enumerated by name
  in prose instead, since there are too many to table usefully) rather than
  silently including some reserved codes and omitting others with no stated
  reason.
  Registers `bootstrap.manifest` (fires at the FIRST
  `load_facts(&sdk_root)` call, strictly BEFORE `select_workspace`, the
  workspace-parent guard (#185), and any venv/west/pip phase — a doubled run
  costs seconds and leaves nothing on disk), `bootstrap.sdk-root-unresolved`
  (the one refusal that predates project resolution) and
  `bootstrap.zephyr-base-manifest-mismatch` (a `warning`, fired from
  `select_workspace`) as `reserved` — no consumer binds any of the three yet.
  Also **promotes `bootstrap.python-not-runnable` and `bootstrap.python-too-old`
  from undocumented to `frozen`**: verified in the read-only alp-sdk-vscode
  checkout that `PREREQ_CODES` (`src/alpCli/service.ts`,
  `prerequisitesMissingIssue`) already matches both by exact string alongside
  `bootstrap.prerequisites-missing`, so they were a real, already-bound
  contract this registry simply never recorded — `contract/README.md`
  documented the gap as a workaround ("a consumer that wants those two must
  match them by name") instead of the fix. Renaming either today is now a
  breaking-wire change like every other frozen code, not a silent one.
  Registers one more `reserved` code, `debug-config.legacy-entry-untouched`,
  for the new signal described under Fixed (#179) — folded into this same
  registry edit rather than a second PR, since both changes touch
  `contract/issue-codes.json`.
- **Re-vendored the scaffold fixtures and the toolchain lock against alp-sdk
  `cdfe1368` (alp-sdk#1016), and bumped `PINNED_SDK_TAG` to match.** #1016
  rewrote the `Customer workflow:` header in every example `board.yaml` from
  "copy this directory … and `west build`" to "`tan init --from-example
  <category>/<name>` … and `tan build`" (ADR-0020: tan is the whole command
  surface). Because `scripts/alp_cli/init.py` points `TEMPLATE_DIR` at
  `examples/peripheral-io/hello-world` and `--emit scaffold` copies those files
  verbatim, comments included, an alp-sdk comment sweep changes scaffold output
  — so `scaffold_byte_parity.py` went red, exactly as designed. Six vendored
  `board.yaml` files moved (`diagnostics`, `minimal`, `sensor`, each for both
  SKUs); `edge-ai` and `iot` were unaffected. Comment-only: no schema, core or
  peripheral content changed. Re-vendored by re-running the emit, not by
  hand-editing the files.
  `contract/fixtures/toolchains/toolchains.json` also moved, and the change is
  worth recording: alp-sdk adopted tan-cli#1012's note and now documents in
  that file's own comment that `check_toolchain_lock.py` scans only alp-sdk's
  workflows, naming `ZEPHYR_SDK_INSTALL_VERSION` as a cross-repo consumer
  covered solely by tan-cli's own byte-parity assertion. `zephyrSdk.version`
  is **unchanged at `1.0.1`**, so `ZEPHYR_SDK_INSTALL_VERSION` and the install
  command `tan doctor` prints are unaffected.
- **BEHAVIOUR CHANGE: `tan bootstrap` no longer sprays `zephyr/`, `modules/`,
  `.west/` and its venv into whatever directory the alp-sdk checkout happens
  to sit in — it now guards that parent, and refuses (or, interactively,
  offers to relocate) when it holds anything else** (#185). `west init -l
  <alp-sdk>` forces the west topdir to be the checkout's own PARENT (#769) —
  a customer who clones into `~/Downloads` used to get multi-gigabyte
  `~/Downloads/zephyr`, `~/Downloads/modules`, `~/Downloads/.west` and
  `~/Downloads/.venv`, unannounced, and OUTSIDE the checkout so no
  `.gitignore` could ever reach them.
  The trigger is a predicate, not a directory-name list (`Downloads`/
  `Desktop`/… would be locale-dependent and incomplete by construction):
  **proceed silently when the checkout's parent holds nothing but the
  checkout itself, bootstrap's own venv, and/or an existing west workspace
  (a `.west/config` that is actually readable — not merely an entry NAMED
  `.west`, which a plain file or an empty directory could also be, and which
  used to be enough to wave the guard through); otherwise guard.**
  `mkdir alp && cd alp && git clone …` — the documented flow — never
  prompts; a parent already holding a real `.west` workspace is never
  guarded either, and neither is a bootstrap that died between creating its
  venv and `west init` ever writing `.west` (a network drop mid pip
  install) — its own retry over that exact state reaches the existing venv
  recovery path instead of being refused. `$HOME` and `~/Downloads` always
  hold other entries, so they always guard.
  Interactively, an accepted guard relocates the checkout (its ENTIRE tree —
  `.git`, uncommitted changes, untracked files — via one atomic directory
  rename, never a copy that could half-complete) to `<parent>/alp-workspace/
  <checkout-name>` and builds the workspace there instead. A prompt needs an
  actual human at a console: `--non-interactive`/`--ci`/`--format json` all
  say so explicitly, and so does stdin simply not being a terminal (piped,
  redirected, or a CI runner with none of those flags set) — the guard
  **fails outright** (exit 2) rather than hanging on a prompt nothing will
  ever answer, naming the remedy: run `tan bootstrap --workspace <path>`
  (which relocates with NO guard at all — the customer has answered the
  question) or clone alp-sdk into a dedicated directory. `--workspace`
  itself is validated before anything touches disk — an empty/whitespace
  value (`--workspace ""`, the classic unset-`$WS` shell accident) and an
  ambiguous drive-relative root (an MSYS-style `/e/foo/ws` on Windows, which
  used to resolve against whichever drive the process happened to be
  running from) are both refused rather than guessed at, and `--print-env
  --workspace <path>` is refused too, rather than printing env lines for a
  directory nothing was ever moved into. A `$ZEPHYR_BASE` workspace that
  bootstrap is about to ADOPT is checked first, so a dirty checkout parent
  no longer trips the guard (or, interactively, offers to move the
  checkout) for a run that was never going to write there in the first
  place. A failed relocation attempt (destination collision, cross-device
  move, a Windows sharing violation — the latter two now name their own
  remedy) always leaves the checkout exactly where it was — the atomic
  rename cannot leave a customer with neither the original nor a working
  workspace. `tan build` and `tan doctor --build --fix` inherit this same
  guard via their delegated auto-bootstrap, and their refusal names `tan
  bootstrap --workspace <path>` explicitly, since neither has a `--workspace`
  flag of its own.
  When a relocation moves a project that lives INSIDE the checkout (the
  `alp-sdk/examples/.../hello-world` shape), the reported `project.root`/
  `project.boardYaml`/`sdk.root` are rebased onto the new location too,
  instead of continuing to name the path the checkout just vacated.
  **Migration note:** a CI pipeline that clones alp-sdk into a directory it
  shares with other content and then runs `tan bootstrap` there will newly
  fail instead of bootstrapping. No `.github/workflows/**` job in this repo
  invokes `tan bootstrap` DIRECTLY, but `parity.yml`'s `seam2` job reaches it
  INDIRECTLY: its `real build (tan build)` step runs in text mode with
  neither `--ci` nor `--format json`, and `tan build`'s auto-bootstrap
  (`preflight::maybe_auto_bootstrap`, gated only on text mode) would call
  `bootstrap::run` if the workspace readiness check ever failed there. That
  path is saved today not by the absence of a `tan bootstrap` call but by
  `west init -l alp-sdk` (`parity.yml`'s earlier `west workspace` step)
  having already created `$GITHUB_WORKSPACE/.west` as a real, config-bearing
  workspace before that step runs — which the guard now recognizes as such.
  The moment a workflow adds a genuinely fresh `tan bootstrap`/`tan build`
  call over a directory that is NOT already a west topdir, it will need
  `--workspace` or a dedicated clone directory like every other pipeline.
  Registers four new `reserved` issue codes (`bootstrap.workspace-guard`,
  `bootstrap.workspace-relocated`, `bootstrap.workspace-invalid`,
  `bootstrap.print-env-workspace-conflict`) in `contract/issue-codes.json` —
  no consumer binds to any of them yet.

### Security
- **README's Manual install section taught the unverified download the scripts
  had just been made to refuse** (#188). #176 stopped `install.sh` /
  `install.ps1` installing a binary they cannot verify; a few lines below the
  one-liners, on the same page, the Manual snippets still fetched a binary with
  no digest step, offered `tan --version` as the confirmation, and — on Windows
  — wrote straight to `tan.exe`, the exact ordering #176 changed because a bad
  binary that has already landed may already be locked or on PATH. That made
  the fallback for the higher-risk population (a locked-down host, a policy
  against `curl | sh`, a hand-carried air-gapped binary) the weaker path, and
  left a documented route to the unverified-`tan`-on-PATH state
  alp-sdk-vscode#393 describes.
  Both snippets now resolve `latest` ONCE — through the same redirect
  `install.sh` follows and the same API field `install.ps1` reads, so the manual
  path lands on the tag the one-liners land on — and build the binary and
  `checksums.txt` URLs from it. `releases/latest/download/…` resolves `latest`
  separately per fetch, which is the two-resolutions problem #176 fixed in the
  scripts, and the digest for a given filename really does move between tags
  (`tan-x86_64-pc-windows-msvc.exe` is `f159c1dc…` at `v0.4.0-rc1` and
  `a80fb5da…` at `v0.4.0`).
  They also download into a **fresh directory** and chain every step, because
  pinning the tag alone is not enough: with a fixed filename in the working
  directory and no gating, a reader who edits the tag and whose fetches 404
  verifies the PREVIOUS tag's binary against the PREVIOUS tag's digests and gets
  a confident `OK` — the same wrong-verdict class #176 removed, reproduced live
  against the real releases during review. The matching is by exact field as the
  installers do, so a neighbouring asset's line cannot satisfy the check, and an
  asset missing from `checksums.txt` is refused explicitly rather than left to
  `sha256sum -c`, which **exits 0 on empty input**. The two refusals are worded
  apart — an incomplete release is not a tampered download — and the PowerShell
  snippet additionally sets `$ErrorActionPreference = 'Stop'`, negotiates TLS
  1.2, and passes `-UseBasicParsing`, all three of which `install.ps1` already
  carries for Windows PowerShell 5.1 hosts.
  The Linux default is now the static `-musl` asset, matching what `install.sh`
  selects rather than the `-gnu` build with its glibc floor — this section is
  the fallback for exactly the old or locked-down host that floor excludes.
  **Both** mechanisms are documented, and deliberately: sha256 needs nothing
  but coreutils (or PowerShell's built-in `Get-FileHash`), so it is the
  baseline a host that cannot install `gh` can still run, while `gh attestation
  verify <file> --repo alplabai/tan-cli --signer-workflow
  alplabai/tan-cli/.github/workflows/release.yml` proves the file came out of
  that workflow, which a digest published in the same release cannot.
  `--signer-workflow` is not optional garnish: without it the command binds the
  artefact to *some* workflow in the repo, not to the release job. The README
  now says why each check is there, so neither gets "fixed" back out.
- **`install.sh` and `install.ps1` now verify the downloaded binary against the
  release's `checksums.txt` and refuse to install on any failure** (#176).
  Previously both wrote the download straight to the install destination with
  nothing but TLS behind it; the only occurrence of "verify" in either file was
  prose telling the user to run `tan --version` afterwards, which establishes
  that something runs, not that it is what we published. `alp-sdk-vscode`
  already verified its own managed download against the same file
  (alplabai/alp-sdk-vscode#389), so the two acquisition paths for the same
  binary disagreed about whether they checked it — and the unverified one is
  what the extension's **"Install tan CLI (global)"** button runs, whose result
  the extension's resolver then prefers over its own verified copy, on every
  activation.
  Failures are worded as the distinct facts they are, all of them refusing and
  leaving the install destination untouched: digest **mismatch**;
  `checksums.txt` **could not be fetched** (says nothing about the binary);
  the asset **not listed** in it; and, POSIX only, **no `sha256sum`/`shasum`**
  on PATH. There is deliberately no `--no-verify` escape hatch — a flag that
  turns the check off is the hole this closes, wearing a consent form.
  `install.ps1` now downloads to a temp file and moves it into place only after
  the digest matches; it previously wrote directly to the destination, so a bad
  binary had already landed by the time anything could detect it.

### Fixed
- **`tan scaffold` still hung on a non-TTY stdio, and `tan init` still hung
  whenever stderr was the redirected handle** (#187 follow-up to #198). #198
  fixed the reported command by adding a `stdin_is_tty` term to `init`'s own
  local predicate. Two live hangs survived it.
  - **`tan scaffold` was never touched.** It carried the same flags-only gate
    (`!--non-interactive && !--ci && !--format json`) and blocked on its own
    `Text::new("Module name:")` prompt under exactly the redirected stdin the
    issue described. It now refuses instead — its non-interactive contract is a
    refusal, not a default, since a module name has no sane one — exiting 2 with
    `scaffold.name-required`.
  - **`stdin=tty, stderr=piped` still hangs on a stdin-only gate**, and that is
    not an exotic shape: it is every wrapper that captures output while
    inheriting the terminal, `.stderr(Stdio::piped())` included. inquire splits
    the two handles — it renders to **stderr** (so a redirected stderr hides the
    prompt) and reads through crossterm's `tty_fd()`, which **opens `/dev/tty`**
    when stdin is not a terminal. Confirmed live under a pty: still running
    after 12s, zero files created, the prompt's escapes sitting in the capture.
    The same mechanism means the Unix block was never on the redirected stdin at
    all — `tan init </dev/null` from a real terminal session blocked on
    `/dev/tty`, and the `init: Cancelled.` exit 1 seen instead on CI and in
    agent shells is only what happens where `/dev/tty` cannot be opened.
  - **One home for the rule, because it was three.** #198's `init`-local
    `interactive_mode` predicate (and its test table) moved to `cli.rs`, gained
    the `stderr_is_tty` term, and is now reached through
    `GlobalArgs::can_prompt()`. `init`, `scaffold` and `bootstrap` all call it.
    `bootstrap` had grown its own inline copy in #185 — fixing the rule in one
    command and not the others is exactly how this bug outlived its own fix.
  - A missing terminal still falls back to the flag-derived defaults rather than
    erroring, unchanged from #198: that is what `--template`'s own help already
    promised ("defaults to `zephyr-app` when not given and there is no TTY to
    prompt on"). `--non-interactive`'s own help and the README flag table now
    say which commands default and which refuse.
  - The non-TTY path is now driven in `tests/init_non_tty_stdin.rs`, which
    spawns the real binary with `Stdio::null()` and captured stderr under a 60s
    watchdog — the interactive path was exercised and the redirected-stdio path
    was not, the same shape as #176's `irm | iex`-tested /
    `.\install.ps1`-untested parse bug. The PROMPTING branch remains manual: no
    CI runner has a TTY, so an `is_terminal()` false negative would degrade `tan
    init` to a silent default scaffold with every gate green.
- **`.\install.ps1` did not parse at all under Windows PowerShell 5.1** — the
  PowerShell that ships with Windows (#176, found while adding the above). The
  file is BOM-less UTF-8; 5.1 decodes such a file using the system ANSI
  codepage, and the em dash `—` (`0xe2 0x80 0x94`) has a third byte that decodes
  to **U+201D**, which PowerShell honours as a string delimiter. The result was
  `The string is missing the terminator: "` and a cascade of brace errors, none
  of them near the real cause. Both installers are now pure ASCII.
  The documented `irm … | iex` one-liner was NOT affected — that path decodes
  UTF-8 correctly before parsing — which is why this survived: the pipe-to-shell
  form was exercised and the download-and-run form was not.

### Added
- **`tan build --pristine`** (#163) — force-wipe every slice's build dir
  before dispatch, the manual counterpart to the automatic sdk-switch-pristine
  wipe (issue #52) for a stale build dir the recorded-stamp heuristic doesn't
  (or can't yet) catch. Not a second wipe mechanism: it forces the SAME
  `SdkStampAction::Pristine` decision the automatic check already makes, still
  inside the same two structural safety guards (an explicit `-d`/
  `--build-dir` in the slice's own command; a plan cwd outside `build/`) — it
  never touches a build dir tan cannot vouch for, forced or not. A no-op on a
  never-configured build dir (nothing to wipe).
- **`tan generate --target os-topology`** (#115) — the per-core
  natural-vs-effective OS facts `scripts/alp_project.py --emit os-topology`
  produces (`core_type`, `runtime_class`, `default_os`/`effective_os`,
  `overridden`, `allowed_os`), the shape alp-sdk#95's heterogeneous-SoM
  configurator UI needs and `system-manifest`'s flat `slices[].os` cannot
  provide. Previously reachable only via a raw `python -m alp_cli emit
  os-topology`. alp-sdk's own docs describe the bare invocation as "JSON to
  stdout, for IDEs" (unlike the file-writing targets), which raised a real
  question over `tan generate --target` vs. a new `tan inspect` reader —
  resolved by measuring live against the pinned SDK checkout rather than
  guessing: `--output <path>` writes a normal file for this target exactly
  like `carrier-netlist`/`west-libraries`/`hw-info-h` (the SDK's shared
  `_write_or_print` helper honors it identically), so it drops into the
  existing `generate` shape. Writes to `build/generated/os-topology.json`,
  is in the default (`tan generate`/`--all`) target set, and `--core` is
  refused rather than silently tolerated: measured live, the SDK accepts but
  ignores it for this target (`alp_project.py: --core is ignored for --emit
  os-topology (project-level emit)`, stderr, exit 0, output unchanged) —
  the same "does nothing" shape `carrier-netlist`/`native-sim-overlay` are
  already refused for. Covered by `live_run_generate.rs`'s real spawn suite
  (`os_topology_target_writes_the_per_core_os_facts`), which asserts the
  actual written JSON's `m55_hp` core entry rather than assuming its shape
  from another target's golden.
- **`tan generate --target west-libraries`** (#114) — the `west.yml` library
  auto-pin fragment `scripts/alp_project.py --emit west-libraries` produces,
  listing the Zephyr modules and exact west project pins a project's
  `board.yaml` `libraries:` declarations require. Drops into the existing
  single-file target shape exactly like the other five, writing to
  `build/generated/alp-west-libs.yml` (alp-sdk's own documented convention,
  `docs/board-config-emit.md`) and included in the default (`tan generate` /
  `--all`) target set. Companion gap #113 (`hw-info-h`) is addressed below.
- **`tan generate --target hw-info-h`** (#113) — the build-time
  `ALP_HW_BUILD_*` identifier header companion to `<alp/hw_info.h>` that
  `scripts/alp_project.py --emit hw-info-h` produces (delegated to
  `scripts/alp_project_emit/hw_info.py`), baking `board.yaml`'s SoM SKU/
  family/hw_rev and (when declared) board name/hw_rev into string macros an
  app cross-checks against the runtime EEPROM read via
  `alp_hw_info_assert_matches_build()`. Previously reachable only via a raw
  `python -m alp_cli emit hw-info-h --output <path>` that bypassed `tan`
  entirely. Same single-file, project-wide shape as `west-libraries`: writes
  to `build/generated/alp_hw_info_build.h` (alp-sdk's own documented
  convention, `docs/board-config-emit.md`), is in the default (`tan
  generate`/`--all`) target set, and `--core` is optional -- forwarded
  through to `alp_project.py`, where it picks which core's OS lands in the
  generated `ALP_HW_BUILD_OS`/`ALP_HW_BUILD_PRIMARY_CORE` macros.
- **`tan generate` live-run e2e** (`crates/tan-cli/tests/live_run_generate.rs`)
  — spawns a REAL `alp_project.py` from a real alp-sdk checkout and asserts
  what actually lands on disk, closing the gap unit tests against a stub
  loader script cannot: that alp-sdk accepts the argv `tan` composes, and
  that files land where `tan` claims. Covers `zephyr-board` (including the
  SoM-swap non-collision case PR #157 fixed: two SoMs sharing a core id must
  never write into the same directory), `west-libraries` (output parses as
  real YAML), `hw-info-h`, `--core` forwarded vs. refused, and the
  `ValidationFailure`(2)-not-`InternalFailure`(5) exit-code mapping. Wired
  into `parity.yml`'s `seam2` job (right after its `west`/PyYAML/jsonschema
  deps install, ahead of the Zephyr SDK/Renode installs it needs neither of)
  against the same `PINNED_SDK_TAG` checkout the materialise/build steps
  already use; hard-fails (not skips) if `TAN_LIVE_RUN_SDK_ROOT` is unset
  where `seam2` also sets `TAN_LIVE_RUN_REQUIRED=1`, self-skips everywhere
  else -- including this repo's own bare `cargo test` CI job, which wires
  neither var and would otherwise hard-panic on all five tests on every push
  and PR (an env-detection bug caught in review before merge: gating the
  panic on the generic `CI=true` GitHub Actions sets on every runner, rather
  than a dedicated opt-in, made that unrelated job fail). A spawn/module
  environment failure (missing interpreter, missing PyYAML/jsonschema) is
  also now correctly distinguished from a real target failure in the panic
  message on every platform, including the `program not found` text Rust's
  `Command::output()` reports on Windows and the `is required.  Install via`
  text `alp_project.py` itself exits with for either missing dependency.
- **`tan generate --target zephyr-board --core <id>`** (#116) — the tan front
  door for the per-core Zephyr board tree generator
  (`scripts/gen_zephyr_board.py` via `--emit zephyr-board`), the step
  alp-sdk's `docs/porting-new-som.md` documents right after `tan new-som`
  scaffolds a new SoM. Previously reachable only via a raw
  `python -m alp_cli emit zephyr-board --core <id> --output <dir>` that `tan`
  otherwise never requires. Unlike every other `generate` target this one
  hard-requires `--core` and writes a DIRECTORY of files (`<board>.dts`,
  `.yaml`, `_defconfig`, two `Kconfig.*`, the pinctrl `.dtsi`, `board.yml`)
  rather than one fixed file, so it is deliberately NOT part of the
  default/`--all` set -- reachable only via an explicit `--target
  zephyr-board --core <id>`, landing under `build/boards/<board-dir>/` where
  `<board-dir>` is the SDK's own `alp_e1m_<sku-slug>_<core>` board-directory
  name (matching `docs/porting-new-som.md` Step 7's own `--output
  build/boards/alp_e1m_aen901_m55_hp/` example), never a bare core id -- two
  SoMs that share a core id (a first-class SoM-swap flow) write to different
  directories instead of colliding in one. `--core` is required for this
  target, optionally accepted -- forwarded straight through to
  `alp_project.py` -- on `zephyr-conf`/`yocto-conf`/`cmake-args`/
  `dts-overlay`/`west-libraries`/`hw-info-h` (`alp_project.py`'s own `--core`
  help documents it as meaningful there too, at the pinned SDK tag), and
  refused on every other target (`carrier-netlist`, `native-sim-overlay`, and
  the default/`--all` set, none of which the SDK ever reads `--core` for).

### Fixed
- **`tan debug-config` silently deleted every JSONC comment (plus any BOM and
  trailing comma) in the customer's `.vscode/launch.json` on every write, not
  just the one entry actually being changed (#182).** Root cause:
  `create_launch_json_write_plan` parsed the whole file into a `serde_json::Value`
  (via the existing, still-correct `strip_jsonc` tolerant read) and then
  re-serialized the WHOLE document with `serde_json::to_string_pretty` — so a
  `// remote probe, do not commit` note or a hand-written `// swap to m55_he
  for the HP build` was gone at exit 0, with no diff shown and no mention in
  the run's own output, on a file the customer owns and hand-edits. Reproduced
  end-to-end against the real binary: a 4-comment, BOM-carrying, trailing-comma
  fixture went to 0 comments and lost its BOM on a single `tan debug-config`
  run, including on an entry the run wasn't even asked to touch. Considered a
  jsonc-preserving parser crate first and rejected it: the write plan only
  ever changes ONE array element (or appends one), so a targeted byte-level
  splice needs no parser at all, and — unlike a round-trip through even a
  jsonc-aware library — guarantees every byte outside the edited span survives
  by construction (there is no re-serialization pass over it to lose
  anything), rather than merely "usually". New `tan_core::jsonc_splice` locates
  the target entry's exact byte span in the ORIGINAL text (mirroring
  `strip_jsonc`'s own string/comment tracking so the two never disagree about
  what counts as structure) and the write copies everything outside that span
  through unconditionally. Falls back to the old whole-document re-serialize
  only when there is no original text to splice into (a brand-new file) or the
  locator can't confidently place the array (no top-level `configurations` key
  in the raw text) — never a malformed write. A comment sitting ABOVE the
  edited entry survives (it's outside the entry's own `{...}` span); a comment
  BETWEEN that one entry's own keys is the one unavoidable loss, exactly like
  any tool that overwrites one JSON object's fields — every comment on every
  OTHER entry, the BOM, and any trailing comma are untouched. A file tan
  genuinely cannot parse still refuses to write rather than guessing, unchanged
  from before — the bug was always "destroys and reports success", not "fails
  to parse a good file". The four `debug-config-preview` envelope goldens are
  unaffected: preview never reads the customer's file.
- **A semantically no-op `debug-config` re-run still spliced (and reformatted)
  the maintained entry, destroying a comment inside it even though nothing
  about the entry had actually changed (review follow-up on #182).**
  Reproduced end-to-end: generate the entry once, hand-add
  `// DO NOT DELETE: probe serial 000123456789` between two of its keys with
  nothing else touched, re-run the identical command — 444 → 394 bytes, the
  comment gone, exit 0, `issues: []`. The extension shells `debug-config`
  before every debug session, so the no-change re-run is the COMMON case, not
  an edge case. `create_launch_json_write_plan` now compares the merged entry
  against what was already on disk and, when they are identical, skips the
  splice entirely and hands back `original`'s own bytes verbatim — an
  unchanged entry means an unchanged document, since the splice never touches
  anything else anyway. Also closes the silent half of #182 itself: a write
  that DOES drop a comment (the one still-unavoidable case — a comment
  sitting between two keys of the entry actually being replaced, or the
  whole-document fallback) now reports it as an `issues[]` entry,
  `debug-config.comments-dropped` (severity `info`, registered `reserved` in
  `contract/issue-codes.json`, no consumer yet), plus a `note:` line in text
  mode — #182 named "a tool that deletes user-authored content must not report
  unqualified success" as the non-negotiable floor, and silence on that one
  remaining loss path did not meet it. Also fixed: a document with two
  top-level `"configurations"` keys (`serde_json` resolves the duplicate to
  the LAST one; the byte-level locator used to stop at the FIRST) could splice
  into the wrong array, destroying whichever entry sat in the one actually
  rewritten and leaving the other stale — the locator now bails to the safe
  whole-document fallback on a duplicate instead of guessing. Also fixed: a
  splice into a CRLF-authored `launch.json` used to emit the new entry's own
  newlines as bare LF, leaving a mixed-EOL file (Windows is tan's primary
  platform); the splice now matches whichever line ending is already dominant
  in the file. Also fixed: appending into an array collapsed onto one line
  (`"configurations": []`, VS Code's own stock template) used to indent the
  new entry at a flat 4 spaces regardless of the file's actual indent width
  and leave the closing `]` stranded at column 0; it now derives the entry's
  indent from its neighbours and keeps the closing bracket aligned.
- **The sdk-switch-pristine stamp recorded only the resolved `--sdk-root`
  PATH, so an SDK checkout that changes CONTENT at the SAME path was silently
  reused — the #163 symptom, still live after the `--pristine`/no-stamp fix
  above closed only the path-changing case (review follow-up on #163).**
  Reproduced end-to-end and with a unit test: seed a build dir under one SDK
  commit, `git checkout` a different tag in the SAME `--sdk-root` checkout (or
  `tan sdk switch <path>` back to a literal path that never moved — `sdk.rs`
  resolves that to an unchanged pointer), and a plain re-run of `tan build`
  never noticed — no `build.sdk-switch-pristine` issue, stale build state
  intact. The plan already carries the fix's own input: a freshly emitted
  plan's `sdkCommit` reflects the checkout's CURRENT git HEAD. A new
  `tan_core::plan_exec::sdk_stamp_key` folds it into the identity
  `sdk_stamp_action` compares and the executor writes to `.tan-sdk-root`
  (`<root>@<sdkCommit>`, degrading to the root alone on an older/commit-less
  plan — matching, and staying compatible with, the historical stamp). An
  existing root-only stamp mismatches the new key exactly once, the first
  build after upgrading — the same "fail toward a spurious rebuild, never
  toward trusting a stale one" trade-off the original mechanism already makes.
- **`tan build --pristine` misreported a harmless no-op as a wipe for a slice
  whose build tool never used `<cwd>/build` at all (review follow-up on
  #163).** The `--pristine` guard checked bare `cwd.join("build").exists()` —
  but `write_sdk_stamp` itself `create_dir_all`s that exact directory to write
  `.tan-sdk-root`, so after ANY prior successful build (a yocto/bitbake slice
  included, whose own state lands elsewhere — bitbake's TMPDIR defaults to
  `<TOPDIR>/tmp`, not `<cwd>/build`) the directory `.exists()` with nothing in
  it but tan's own stamp. Reproduced end-to-end against a real E1M-V2N101
  plan: `tan build --native --pristine` reported `build.sdk-switch-pristine —
  a55_cluster: --pristine passed; wiping build dir before dispatch` for the
  bitbake slice's build dir, which held only `.tan-sdk-root`. Not destructive
  (nothing bitbake wrote was there to lose), but exactly the "misreport a
  harmless no-op as a wipe" the `.exists()` guard was added to prevent. Now
  gated on `cmake_cache_configured` — the SAME "was this dir ever really
  configured" signal the automatic (non-forced) branch already uses.
- **`kconfig_fixture_parity.py` and `scaffold_byte_parity.py` still treated an
  explicit `--sdk` as a hint inside the required `seam1 -- plan-shape parity`
  job (#175).** #173 fixed this for `toolchain_lock_parity.py` and
  `bootstrap_manifest_parity.py`, deliberately leaving these two — the finding
  scoped only the two it named. Both vendored their own
  `_looks_like_sdk_checkout`/`resolve_sdk_root` pair instead of the shared
  `tests/parity/_sdk_checkout.py`, so a `--sdk` that failed to resolve (e.g.
  the checkout path moving, or alp-sdk reorganising `scripts/alp_orchestrate/`
  away) silently fell through to `$ALP_SDK_ROOT` / a sibling checkout and,
  finding neither, printed SKIP and exited 0 — both steps run inside
  `parity.yml`'s required `seam1-plan-shape` job with a real `--sdk alp-sdk`,
  so a moved checkout would go green having checked nothing. Both now import
  `sdk_root_or_exit_code`, same as the other two: an explicit `--sdk` that
  does not resolve is a hard FAIL (exit 1); omitting `--sdk` keeps the
  existing local-dev-loop SKIP (exit 0). Verified against a real pinned
  alp-sdk checkout: both now reach an actual byte-diff/scaffold-emit
  comparison and PASS, not skip.
- **A build dir from a PRE-#52 tan release (no `.tan-sdk-root` stamp at all,
  not just one stamped for a different SDK root) is the exact shape a
  reporter saw silently reused after `tan sdk switch` (#163).** Root-cause
  finding: the auto-pristine machinery landed in v0.4.0 (issue #52) and is
  unchanged since — `sdk_stamp_action` (`tan-core::plan_exec`) already treats
  a missing stamp on an already-configured build dir as stale by design (the
  one way a pre-feature build dir ever self-heals), and every guard/input in
  the wiring (`crates/tan-cli/src/commands/build/execute/mod.rs`) traces
  through to `Pristine` for this exact input shape. A new end-to-end test,
  `execute_slices_wipes_a_pre_feature_build_dir_with_no_stamp_at_all` —
  reproducing the reporter's precise shape (`CMakeCache.txt` present,
  `.tan-sdk-root` absent, switched `--sdk-root`) rather than only the
  pure-function unit test's coverage of it — confirms the mechanism fires
  correctly today; mutating either the pure decision or the executor wiring
  makes it fail. No second pristine mechanism was added. What the mechanism
  cannot give a user in retrospect: the wipe + warning fire exactly ONCE, on
  the run that detects the switch (deliberately, so a compile-error retry
  loop keeps its incremental state) — a build that then fails for an
  unrelated reason (as the reporter's did, #160's missing Zephyr SDK
  toolchain) and is retried shows nothing about the switch on any later
  attempt, because by then the stamp already matches. `tan build --pristine`
  (above) covers the manual case the issue asked for.
- **`ZEPHYR_SDK_INSTALL_VERSION` was a hand-ported copy of a fact alp-sdk
  owns and guards, on a side alp-sdk's own drift gate cannot see (#172).**
  `tan doctor`'s `zephyrSdk` check names the exact `west sdk install
  --version <..>` command a customer needs (#160), sourced from a Rust
  constant in `crates/tan-core/src/host_env.rs`. alp-sdk's
  `metadata/toolchains.json` is the single source of truth for that pin and
  is policed by `scripts/check_toolchain_lock.py` — but that gate's scope is
  CI *workflows*, and it cannot see a tan-cli checkout, so a future SDK-side
  Zephyr SDK bump would leave `tan` silently printing a stale, wrong install
  command with no test or CI job on either side catching it. Measured
  runtime-read (resolving the SDK root and reading the manifest live) and
  rejected it: `zephyr_sdk_toolchain_check` is a pure function taking only a
  `bool`, and the string is wanted precisely when the toolchain — and often
  the SDK itself — is not yet resolved on a fresh host, which is the exact
  case #160 was filed over. Instead, `metadata/toolchains.json` is now
  vendored at `contract/fixtures/toolchains/toolchains.json`
  (`tests/parity/toolchain_lock_parity.py` byte-diffs it against the pinned
  alp-sdk checkout in CI, the same pattern already used for
  `metadata/bootstrap.json`), and a new `cargo test`
  (`host_env::tests::zephyr_sdk_install_version_matches_the_real_toolchain_lock`)
  asserts the constant equals the vendored fixture's `zephyrSdk.version`
  field — so a re-vendor that isn't matched by a constant update now fails
  loudly instead of shipping a wrong remedy. Review follow-up: a second,
  unguarded hand-ported copy of the same pin survived in
  `zephyr_sdk_host_check`'s macos-x86_64 fix string (a literal `1.0.1` no
  test exercised) — it now reads `ZEPHYR_SDK_INSTALL_VERSION` too, so an SDK
  version bump can no longer leave `tan doctor` naming two different Zephyr
  SDK versions on the same Intel Mac report. `tests/parity/
  toolchain_lock_parity.py` and `bootstrap_manifest_parity.py`'s shared
  `--sdk` resolution also no longer treats an explicit `--sdk <path>` as a
  hint: previously a `--sdk` that didn't resolve silently fell through to
  `$ALP_SDK_ROOT` / a sibling checkout and, finding neither, printed SKIP and
  exited 0 — meaning `seam1-plan-shape`'s required `--sdk alp-sdk` step would
  turn green without checking anything if that checkout path ever moved or
  stopped shipping `scripts/alp_orchestrate/`. It now hard-fails (exit 1)
  when an explicit `--sdk` doesn't resolve; the no-`--sdk` local dev-loop
  skip is unchanged. Factored into a shared `tests/parity/_sdk_checkout.py`
  instead of two more copies of the same resolution logic.
- **The four first-blink blockers a customer actually hits on a fresh host
  (alp-sdk#855's deliberate fresh-host run), closing #159 (PR #166)'s
  companion set:**
  - **#160 — nothing installed or named the Zephyr SDK toolchain, and
    `zephyrSdkHost` read as "installed" when it meant only "a host build
    exists for this platform".** Renamed to `zephyrSdkAvailableForHost` so a
    `[+]` cannot be misread as "the toolchain is present" — the check that
    means that, `zephyrSdk`, is a hard `Fail` when absent since #159/#166, and
    the two sitting under confusable names on the same report was worse than
    either alone. `zephyrSdk` now also appears in PLAIN `tan doctor`, not only
    `--build`, both driven by the identical `crate::toolchain::zephyr_sdk_detected()`
    probe so the two run modes can never disagree. Its `fix` now names the
    EXACT command that got the alp-sdk#855 reporter past this — `west sdk
    install --version 1.0.1 -t arm-zephyr-eabi` — instead of a bare docs URL;
    tan does not run it itself (a real download+extract belongs to `west`
    once a workspace exists, not a `doctor`-time side effect). That command
    now also lands in the check's `detail`, not only `fix` — `fix` renders
    only under `--verbose`, so plain `tan doctor` was still showing this
    Fail with no remedy at all, matching the `sdk`/`workspace`/
    `hostPrerequisites` checks that already inline theirs.
  - **#161 — a partially-failed bootstrap left a broken `.venv` that every
    retry silently reused, repeating the identical failure with no new
    information.** Reproduced on a real Debian/Ubuntu host missing
    `python3-venv`: `python3 -m venv` fails with `ensurepip is not
    available`, but the interpreter file it leaves behind IS real, so
    `tan bootstrap`'s presence check accepted it and died again at `No module
    named pip`. `ensure_venv` now probes an existing venv's own `python -m
    pip --version` before reusing it; an unusable one is removed and
    recreated rather than adopted. Additionally, `tan doctor`/`tan bootstrap`
    now probe venv CAPABILITY (`import ensurepip`) rather than merely
    `python3`'s presence, so this fails loudly at `doctor` time, before
    `bootstrap` ever runs — `python3-venv` itself could not be added to the
    prerequisite list here, since that list is pinned byte-equal to alp-sdk's
    `metadata/bootstrap.json` (a manifest fact this repo does not own; filed
    upstream as alp-sdk#1011). The new `remove_dir_all` this recreate path
    added now refuses a manifest `venv.dirName` that is not a plain relative
    path segment (e.g. `".."`) at `metadata/bootstrap.json` parse time —
    unvalidated, that field joins straight onto the west topdir, so a
    manifest naming `".."` would have pointed the removal at the topdir's
    PARENT.
  - **#162 — `tan sdk install` left nothing selected, and `tan sdk current`
    sent the user back to `install` with no exit.** `tan sdk install` now
    auto-selects the version it just installed whenever nothing is currently
    active (never overriding an existing selection), reusing the exact
    `tan sdk switch` repair alp-sdk-vscode#388 already shells rather than a
    second copy of it. `tan sdk current`'s "nothing selected" message now
    recommends `switch` (naming what's already cached) instead of `install`
    again whenever the cache is populated. Also fixed: `sdk install`/
    `current`/`switch` reported `version (unknown)` for every git-clone
    install because no released alp-sdk ships a top-level `VERSION` file;
    `check_sdk_readiness` now falls back to `metadata/sdk_version.yaml`, the
    same file `tan doctor`'s `sdkProvenance` check already read successfully.
    Deliberately NOT done: an automatic "pick the newest `~/.alp/sdk-cache`
    entry" global fallback for the directory-scoped-selection gap — this
    codebase already refuses to guess among ambiguous Zephyr SDK installs for
    the same reason ("newest" is not even a well-defined ordering), and the
    two fixes above close the loop for the common case without it. `install`'s
    own auto-select now says so explicitly, too: its success text gets a
    `note` line stating the selection is scoped to the current directory,
    since `install` is typically the very first command a new user runs,
    often from a directory they will not `cd` back into.
  - **#164 — `tan examples` printed only a count, never the list
    `--from-example` needs a source dir from.** Text mode now renders
    `id`/`title` per line (`--verbose` adding the description), plus a new
    `--filter <substring>` (matched case-insensitively against `id`/`title`)
    for narrowing the 97-entry catalog. Text mode also strips
    README-derived markdown noise (`![alt](url)` image/badge spans — a real
    example's title otherwise renders a raw `shields.io` badge URL inline —
    and `**bold**` emphasis markers) from the rendered `title`/`description`;
    the JSON `data.examples[]` payload keeps the raw value unchanged.
- **`tan_core::GENERATION_TARGET_CATALOG` (what `tan explain`/`tan trace`
  read) listed only 4 of the 10 real `tan generate --target` values (#165),
  omitting `hw-info-h`, `west-libraries`, `zephyr-board`, `carrier-netlist`,
  and `native-sim-overlay` — two of which shipped in the immediately
  preceding PR, so the discovery surface was already behind the command
  surface.** Root cause: `generate.rs` kept its OWN second, hand-maintained
  copy of the target list (`ALL_EMIT_MODES` + a `match emit {..}` output-path
  table), independent of the catalog — exactly how the two drifted apart,
  silently, with nothing to catch it. Fixed by unifying the direction of
  derivation the other way round from a naive merge: the catalog (plus
  `tan_core::ALL_EMIT_MODES`, now the SOLE copy) is the single source of
  every target's `emit` key and output path; `generate.rs` reads
  `tan_core::ALL_EMIT_MODES` directly and derives `output_path_for_emit` from
  `tan_core::generation_target_support` instead of hand-duplicating either.
  `zephyr-board` is the one target that genuinely does not fit a single
  `output_relative_path` string (it writes a per-SKU/core DIRECTORY of
  files, not one file) — it stays a documented, explicit exception
  (`GenerationTargetSupport::is_directory`), not a second silent divergence:
  a new `catalog_matches_all_emit_modes_plus_zephyr_board` test fails if the
  catalog's target set ever again differs from `ALL_EMIT_MODES` plus
  `zephyr-board`. The stale "the four emit targets" module doc comment is
  also corrected. `tan explain --target <mode>` now covers all ten
  `generate` targets, including the `os-topology` target added above
  (#115). `tan trace`/`tan support-bundle` deliberately do NOT: they
  enumerate a separate, narrower `tan_core::BUILD_CONFIG_EMIT_MODES` (the
  four per-core build-config targets a `tan build` slice actually
  materialises — `zephyr-conf`/`dts-overlay`/`cmake-args`/`yocto-conf`,
  unchanged from before this PR), since their own module docs describe
  reporting "the generation decisions a build would make" — pointing them
  at the full nine-target set instead (an earlier revision of this fix did)
  would have them claim project-level exports like `carrier-netlist`/
  `os-topology` that no build ever runs (review finding 1).

  Two more defects surfaced measuring this against the real generated
  output. The catalog's own `zephyr-board` entry named its documentary
  output path `build/boards/<sku-slug>_<core>/`, missing the `alp_e1m_`
  prefix `zephyr_board_dir_name` actually writes, so `tan explain --target
  zephyr-board` pointed at a directory that does not exist (review finding
  3) — fixed to `build/boards/alp_e1m_<sku-slug>_<core>/`. And
  `generate.rs`'s `output_path_for_emit` joined the catalog's
  `/`-separated literal in one `Path::join` call, leaving its internal
  `/`s untranslated on Windows while `zephyr-board`'s own component-wise
  join used the native separator — so a single `tan generate` envelope's
  `data.written[]` mixed `build/generated/alp.conf` next to
  `build\boards\alp_e1m_aen801_m55_hp` (review finding 4) — fixed by
  splitting the relative literal on `/` and folding each component onto
  the workspace root, so every target's path is native and internally
  consistent.
- **`tan generate`'s argument-shape errors (`--core` paired with a target it
  doesn't scope; `--target zephyr-board` with no `--core`) now exit
  `ValidationFailure` (2) with issue code `generate.invalid-target`, not
  `InternalFailure` (5) with `generate.internal-failure`.** An ordinary usage
  mistake used to be indistinguishable from a genuine internal bug on both
  the exit code and the wire.
- **`tan bootstrap`'s printed next steps still told the user to reinstall via
  the retired, unpinned `cargo install --git https://github.com/alplabai/tan-cli
  --bin tan` (#117).** alp-sdk#988 replaced every such site repo-wide with the
  pinned `install.sh`/`install.ps1` one-liner except this one, which lives in
  the Rust binary rather than a doc or script alp-sdk#988's sweep could reach.
  Now prints the same platform-appropriate one-liner README.md's own
  "Automatic" section documents -- verified live (`curl`/`irm` against both
  raw GitHub URLs answer HTTP 200; `cargo install alp-tan-cli` was checked too
  and still 404s on crates.io, per #151, so it was deliberately NOT used as
  the replacement). Left as plain `tan doctor`, not `--build`: since #100
  plain doctor already folds in the build-readiness preflight, and
  `bootstrap`'s next-steps block is explicitly one of the sites that fold was
  written to cover (see `assemble_doctor_report`'s doc comment).
- **`tan doctor --build` stays accepted, permanently, as a deliberate
  compatibility surface (#112).** #100 folded build-readiness into plain `tan
  doctor`, making `--build` read as redundant -- but both `alp-sdk-vscode`
  call sites (pinned to `SUPPORTED_CLI_VERSION "0.4.0"`) hardcode literal
  `["doctor", "--build"]` / `["doctor", "--build", "--fix"]` argv with no
  fallback. Removing the flag, or turning it into a usage error, would break
  the Toolchain Doctor panel and its "Bootstrap now" remedy -- the primary
  recovery path from a failed readiness check -- at the same moment. No
  deprecation window and no planned removal date: the two commands stay
  permanently distinct (own OS-set resolution, own check vocabulary), not a
  shim scheduled to collapse into one.
- **A release can no longer report a registry publish it did not perform
  (#151).** `publish · crates.io` and `publish · npm shim` each emitted a
  `::warning::` and exited **0** when their token was unset, so the v0.4.0 run
  summary read `success` for both while the registries answered
  `crate 'alp-tan-cli' does not exist` and `dist-tags: None`. `cargo install
  alp-tan-cli` and `npm i -g @alplabai/tan` were documented install paths that
  had never worked, and every signal the workflow produced said otherwise.

  Both jobs already run on FINAL tags only (`!contains(github.ref_name, '-')`),
  so the graceful skip only ever applied where it is least defensible: a
  finished release advertising two install channels. A missing token now FAILS
  that job and writes the outcome to `$GITHUB_STEP_SUMMARY` rather than an
  annotation -- #151 is the proof an annotation is too weak, since the warning
  was emitted correctly and still read as a publish. A successful publish
  writes its own summary line too, so the run states what shipped either way.

  The GitHub release is untouched: both jobs are `needs: release`, so the
  binaries, `checksums.txt`, `envelope-contract.json` and the attestation are
  already published before either can fail. The run going red reports the truth
  about two registry channels; it does not withhold the release. Pre-release
  tags never reach either job, so rc behaviour is unchanged.

  `docs/release-contract.md` claimed "**No secrets.** Same-repo release uses the
  default `GITHUB_TOKEN` only" -- flatly, with two secret-dependent jobs in the
  same workflow. That line is why nobody noticed, and it is now a table naming
  each secret and what breaks without it.

  The README advertised both commands with no caveat, so a customer following
  it today hits a failure. It now carries a warning naming #151 and pointing at
  the release binaries, while keeping the command names documented so they do
  not change under anyone once the tokens land.

  Also corrected: the crates.io skip message asserted "The GitHub release
  binaries + npm shim still ship". The npm shim did NOT ship, for the same
  reason, and that job had no way to know.

- **Seam 1's oracle carried a stale `multicore_rpmsg-imx93` fixture, papered
  over with a tolerance that could pass a silently dropped command
  (#855).** The vendored oracle predated alp-sdk#999 (merged `ff39401d`),
  which teaches the planner to refuse a `west build -b <board>` command for a
  Zephyr slice whose board has no tree, emitting `command: null` plus a
  `board-tree-missing` warning instead. Rather than re-vendor the one stale
  fixture, a prior pass added a `command -> null` tolerance to
  `tests/parity/seam1_field_diff.py` -- a comparator-local workaround alp-sdk's
  own copy of this comparator carries no equivalent of, and one that was
  uncoupled from the warning it claimed to require: it accepted a command
  vanishing with **no** accompanying warning at all, so seam 1 could go green
  on a silently dropped command. Fixed by re-vendoring
  `tests/parity/oracle/multicore_rpmsg-imx93.build-plan.json` byte-for-byte
  from alp-sdk at `PINNED_SDK_TAG`, rather than hand-editing it here, and
  deleting the tolerance outright -- `slices[*].debug.probe` is once again the
  *only* delta `seam1_field_diff.py` allows through the gate. The comparator's
  own negative-matrix suite (`tests/parity/test_seam1_field_diff.py`) now also
  runs in CI, ahead of the live comparator, so a future comparator regression
  fails before it is trusted to judge alp-sdk.
- **`tan debug-config` wrote `"ALP: ..."` launch-configuration names while the
  `alp-sdk-vscode` extension writes the same four configurations `"Alp: ..."`
  (#133).** Both sides merge into `.vscode/launch.json` by exact `name` match,
  so the two spellings never matched and every debug configuration a customer
  generated with both tools ended up duplicated. `tan` now emits `Alp:` --
  byte-for-byte identical to the extension's four names.

  **Migration, corrected (#133 reopened):** this note originally said an
  existing `ALP: ...` entry is left in place and has to be deleted by hand.
  That was wrong, and not merely cosmetic: the orphaned `ALP:` entry is
  exactly where a customer's own hand-resolved fields (a hand-filled
  `device`, for instance) already lived, while the maintained `Alp:` entry
  kept the unresolved placeholder -- so the advice to delete it would have
  thrown away the working value. `tan debug-config` now adopts a legacy
  `ALP: ...` entry onto its `Alp: ...` counterpart the first time it runs
  after this fix, via the same merge an ordinary re-run already uses: a
  hand-filled value on an unresolved-placeholder field (`device`,
  `miDebuggerServerAddress`, `configFiles`, ...) carries across, while every
  other field tan owns (`executable`, `cwd`, `runToEntryPoint`, ...)
  refreshes to this run's values exactly as on an ordinary re-run. It
  reports the migration as a `debug-config.legacy-entry-migrated` issue
  (severity `info`) so an automated consumer or a `--format json` reader can
  tell why the file changed. No manual deletion is needed, and none should
  be done. The one case left alone on purpose: a workspace that already has
  BOTH a maintained
  `Alp: ...` entry and a legacy `ALP: ...` one (from running `tan
  debug-config` both before and after this fix) keeps the legacy entry
  exactly as it is -- nothing here decides which of two possibly-hand-edited
  entries is authoritative, so that specific case is the only one where a
  customer may still want to look at the file by hand.

  The company is "Alp Lab" (never "ALP Lab"), and the same misspelling had
  spread to other user-facing strings: the `tan sdk list` spinner and table
  header ("Fetching ALP SDK releases...", "ALP SDK releases (N)"), the
  `sdk.rs` error contradicting its own "Alp SDK: ..." string 79 lines above it
  ("... is not a valid ALP SDK root."), and the New Project / module-scaffold
  wizard's generated `README.md` headings ("# ALP Starter Project", "# ALP
  Module Scaffold") and its `body_line1` for the minimal template, which
  reaches the device console via `puts()`. All corrected to `Alp`. Left
  untouched, deliberately: the `ALP_SDK_ROOT`/`ALP_FLASH_FORCE` env vars, the
  `__ALP_SIM_DONE_N__` Renode sentinel, the `ALP-B*` diagnostic codes, and the
  `ALP_*`/`CONFIG_ALP_*` C/Kconfig identifiers in vendored scaffold sources --
  those are identifiers, not brand prose, and renaming them breaks real
  behaviour.

- **Doctor/inspect prose that named a VS Code setting on a path a terminal
  user hits, finishing the sweep #127 started (#134).** Four sites still told
  a `tan`-only user to "set `alpSdk.pythonPath`" or "open a workspace" -- a
  setting and an editor action neither exists for them. `util.rs`'s
  `python_too_old` (read by `build`, `validate`, `generate`, and the SDK
  passthrough commands, and landing in `issues[].message` under `--format
  json`, so it stays useful to both readers) now names the CLI remedy first
  and keeps the VS Code setting as a parenthetical. `tan inspect`'s
  `sdkRoot`/`workspaceRoot` details and `tan doctor`'s plain (non-`--build`)
  `workspaceRoot` check -- both unreached by the extension, which only ever
  shells `--build` -- now point at `--project <dir>` / `--sdk-root <path>` /
  `tan sdk switch <path>`, reusing the vocabulary `build_preflight_checks`
  already uses for the same facts.

- **doctor: retired the permanently-`unknown` `peripheralViewerExtension` /
  `memoryViewExtension` checks (#132).** Both mirrored a TS
  `MCU_COMPANION_VIEWERS` constant that does not exist on any shipped
  `alp-sdk-vscode` branch, and `marus25.cortex-debug` force-installs both
  `mcu-debug.peripheral-viewer` and `mcu-debug.memory-view` as hard
  `extensionDependencies` regardless -- `cortexDebugExtension` already covers
  the same ground on the only host that can actually answer the question.
  Neither check code is in the frozen `contract/issue-codes.json` registry, so
  nothing on the wire contract changes.

- **doctor: the native-host `lldb` check no longer warns and recommends
  installing a debugger CodeLLDB already ships (#131).** `vadimcn.vscode-lldb`
  bundles a complete LLDB inside its own extension directory and never reads
  PATH, so a bare-PATH miss used to `Warn` and suggest "Install LLDB or
  lldb-dap for native-host debug flows" -- a no-op remedy that fired on
  essentially every customer machine, immediately under a line that (post-#102)
  already said "vadimcn.vscode-lldb: unknown -- the standalone tan binary
  cannot see VS Code's installed extensions." The check is now informational
  and always `Pass`, mirroring `alp-sdk-vscode`'s own fix for this exact class
  (PR #369): no `fix`, so nothing lands in `nextSteps` and no `doctor.lldb`
  issue is raised. The resolved executable name is still reported when one is
  found on PATH -- only the verdict and the advice for a miss were wrong.

## [0.4.0] — 2026-07-28

### Added
- **The JSON envelope vocabulary alp-sdk-vscode gates on is now a frozen,
  tested, published contract (#106).** The extension matches four issue codes
  with `===` and reads a dozen `data` field names behind `?? []` fallbacks, and
  **every one of those matches fails open** — rename any and the extension does
  not error, does not log and does not warn, it silently skips the check or
  renders stale data, CI green on both sides. The headline case: rename
  `data.soms` and the New Project wizard falls back to a static catalogue that
  carries no `cores`, so a heterogeneous SoM scaffolds single-core with no IPC.
  The reference part E1M-AEN801 is multi-core, so that is the default path, not
  an edge case.
  - `contract/issue-codes.json` is the single source for the frozen codes
    (`bootstrap.windows-unsupported` — retired but RESERVED,
    `bootstrap.yocto-host`, `bootstrap.prerequisites-missing`,
    `presets.sdk-root-unresolved`), gated by `frozen_issue_codes` in
    `crates/tan-cli/tests/contract.rs`. The consumer is deliberately NOT
    loosened to prefix matching: a prefix match on `bootstrap.` would swallow
    codes it has no verdict for.
  - Four new golden envelopes extend the existing `contract/envelopes/` suite
    (12 → 18 tests): `presets-no-sdk`, `presets-heterogeneous-som` (an `a55`
    yocto + `m33` zephyr fixture SoM — the worked example above, made
    executable), `explain-overview`, and `examples-catalog`. A case fixture can
    now be a directory tree, so a case can carry a synthetic `sdk/` checkout
    and pass `--sdk-root ./sdk`.
  - `doctor --build`'s `data` keys get a key-set assertion rather than a golden
    (its values are host facts): `summary.{pass,warn,fail}`, `nextSteps`,
    `checks[].{name,status}`, and the literal check name `workspace`.
  - Tagged releases now publish **`envelope-contract.json`** beside the
    binaries — the frozen codes plus one golden envelope per command family —
    so the extension's contract test can diff against a published artefact
    instead of a hand-copied fixture.
  - Two consumer fields stay UNCOVERED and are documented as such in
    `contract/README.md` rather than quietly omitted: `build --materialise`'s
    `data.written` (needs a resolvable SDK + a Python spawn) and `sdk list`'s
    `data.releases` (network).
- **`tan sdk list` carries GitHub's `draft`/`prerelease` flags through (#122).**
  Both booleans were already in the Releases API response `tan` parses but were
  dropped before reaching either the JSON envelope or the text table — a
  consumer asking "what is the latest SDK?" could not tell a release candidate
  apart from a genuine release, with no error and no log line. `SdkRelease`
  now carries `draft`/`prerelease` (default `false` when GitHub omits or
  misencodes either key, never a reason to drop the release), and the
  `tan sdk list` table marks a flagged entry with `[draft]`/`[prerelease]`.
  tan does not filter on either flag or add a `--include-prereleases` switch —
  the consumer decides what "latest" means; tan's job is only to publish the
  fact it already has instead of destroying it. One caveat: `fetch_releases`
  sends no `Authorization` header, and GitHub returns `draft: true` entries
  only to a caller with push access, so against the public `alp-sdk` repo
  `[draft]` never renders today — it activates the moment a token is added.
- **`tan doctor --build` checks `git`, `python`, `dtc` and `gperf`, and every
  check can now carry a resolved `version` (#120, #123).** Four of the six
  host tools a build needs were previously invisible to `data.checks[]`; `git`
  and `python` are checked unconditionally (every backend's build-plan
  emission runs `alp_project.py` through both, not just Zephyr's), `dtc` and
  `gperf` are gated on the Zephyr entry in `data.osSet` (Yocto/baremetal-only
  projects never see them) and — matching the retired `alp doctor`'s own
  `_check_dtc`/`_check_gperf` — stay `warn` rather than `fail`. `python`
  reports a version FLOOR, not bare presence: an interpreter below the
  manifest's `pythonMinVersion` fails with its own detail, distinct from "not
  found".
  - Each `data.checks[]` entry gains an optional `version` — absent, never
    `null`, when unresolved or not meaningful (`zephyrSdk`, `vendorToolchain`)
    — reporting whatever tan itself resolved rather than leaving a consumer to
    re-probe PATH and risk a second, disagreeing answer. `westResolved`'s
    version comes from the SAME workspace-venv resolver its status does,
    kept independent of `west`'s bare-PATH version so the two rows can never
    be attributed to the wrong resolver.
  - `missingPrerequisites[].tool` for python is now host-correct — `python3`
    on a served POSIX host, `python` on Windows — matching `prerequisites.
    install.linux`/`.macos`'s own key and `tan bootstrap`'s `posix_refusal`
    naming for the identical missing tool, instead of always `python`, which
    a POSIX consumer could not re-key back into that same install map.
- **Vendor `board-diagnostics` and `iot-starter` from the SDK scaffold catalog
  (#14).** Closes out the last two vendorable entries from alp-sdk#864's
  scaffold catalog (added by alp-sdk#903): `board-diagnostics` now emits the
  SDK's real board self-test app (SoM/SoC identity, RUN operating-point
  profile, on-module I2C management-bus scan) for both
  `E1M-AEN801`/`E1M-V2N101`, and `iot-starter` emits the SDK's real Wi-Fi +
  `mqtts://` MQTT/TLS telemetry app on the CC3501E bridge — `E1M-AEN801` only,
  matching the SDK catalog's AEN-only + preview status.
  - `iot-starter` narrows `--som` to `E1M-AEN801`: any other SKU is rejected
    with `init.invalid-som` before a single file is planned, never a silent
    fall-back onto the retired hand-written generator.
- **The JSON envelope now names which alp-sdk root a command actually
  resolved (#110).** A new optional top-level `sdk: { root, sourceTier }` key
  reports the exact path + precedence tier (`sdkRootFlag`/`projectPin`/
  `globalDefault`/`discovery`) the command used — so a consumer (the vscode
  extension) can finally tell which SDK produced a result instead of guessing,
  especially on the unpinned/first-run path where discovery now walks up to
  an enclosing checkout (#101).
  - Populated from a value RECORDED at the moment one of `tan`'s three
    resolvers actually resolved something, never from a second, fresh
    resolution — the three resolvers have different candidate sets, so
    re-resolving to fill the envelope could report a path the command never
    actually used.
  - Absent entirely (not `null`) when nothing resolved, keeping every
    existing contract golden byte-identical.
- **`tan renode --sim-mode` serves the studio hardware-simulator socket contract
  (#77, socket half).** The flag existed for CLI-surface stability but errored
  "not yet ported", so studio had nothing to connect to. It now boots the
  `--image-bundle`'s firmware in headless Renode and exposes the two sockets the
  gateway needs. The contract was ported from the RETIRED Python
  (`west alp-renode --sim-mode`, deleted in `alp-sdk@df312cec` under ADR-0020
  Phase 4), not re-derived from issue prose — the prose omits four things the
  implementation carries, and all four are honoured: the `ERR <reason>` reply,
  the `ready (timeout <N>s).` readiness marker, LOWERCASE `0xnn` reply hex, and
  the Secure `SCB->VTOR` (0xE000ED08) write the generated boot script needs on
  ARMv8-M + TrustZone, where `LoadELF` does not seed it and the core otherwise
  HardFault-storms from address 0.
  - Both listeners are bound on ephemeral `127.0.0.1` ports **before** the
    descriptor names them, so a client that reads `sim-descriptor.json` and
    connects at once can never race into an `ECONNREFUSED`. They are bound on
    port 0 and their assigned ports read back rather than picked-then-rebound,
    which removes the Python's bind-then-close TOCTOU window outright.
  - `<bundle>/sim-descriptor.json` carries exactly the schema's four keys —
    `control_socket`, `uart_socket` (`tcp://127.0.0.1:<port>` URIs),
    `framebuffers`, `peripherals`.
  - The control socket is line-oriented, one request → one reply, with three
    verbs: `sysbus ReadBytes <base> <count>` (reply normalised from Renode's
    bracketed UPPER-case list to `count` space-separated lowercase `0xnn`
    tokens, scoped to the brackets so an echoed command address cannot leak in
    as a phantom byte, and a short read is an error never a padded answer);
    `sysbus WriteBytes <base> <hex…>` (expanded to per-byte `sysbus WriteByte
    <base+i>`, because Renode's own `WriteBytes` takes `(bytes, addr)` — the
    reverse of studio's order); and any other line forwarded verbatim. A
    malformed line or monitor fault answers `ERR <reason>` and keeps the
    connection, and every reply is flattened to a single line so a client can
    never desynchronise.
  - The `data` payload records `descriptor`, `controlPort` and `uartPort`, so a
    JSON consumer reads the descriptor's path and the two ports out of the
    envelope instead of assuming `<bundle>/sim-descriptor.json` and parsing them
    back out of the file it first has to find.
  - A CPU that halts on its first instruction fetch fails the run with an
    `renode.cpu-halted` error issue and exit 1, matching the plain smoke's latch
    (issue #64). In sim mode the monitor owns Renode's stdout, so the halt is
    latched in the pump thread — a halt landing between two client commands
    belongs to no command's collection window, and previously would have
    resurfaced at best as an `ERR` on whichever command came next while `tan`
    still exited 0. Sim mode is exactly where a mis-seeded VTOR shows up this way.
  - Teardown sends the monitor's `quit`, polls up to 1 s for Renode to act on it,
    and only then kills — the Python's `terminate()` + `wait(10)` + `kill` on a
    shorter budget. Killing immediately after the flush gave the emulation no
    time to close its sockets or flush its log.
  - **DEFERRED to a follow-up on the same issue, which stays OPEN:** the
    `ram_console_buf` RAM-ring → UART-socket streamer, the wired-UART console
    path (Renode's own socket terminal), and the per-SKU sim profiles that fill
    `framebuffers`/`peripherals` — empty for now. The UART socket accepts and
    holds connections while streaming nothing, exactly as the Python did for an
    image carrying no `ram_console_buf` symbol, so studio's serial view connects
    and stays empty rather than failing to connect. `--expect` is reported as
    ignored in sim mode rather than silently dropped: the console goes to the
    socket, so there is no console text to scan.
  - **That deferral is never silent.** Every sim run carries a
    `renode.sim-profile-deferred` **warning** issue (and prints it in text mode)
    stating that `framebuffers`/`peripherals` are both empty and that the UART
    socket streams nothing, so an empty descriptor cannot read as a successful
    one — the Python REFUSED a SKU with no profile outright, and `tan` keeps
    exit 0 only because the control socket genuinely works without one. For
    `E1M-AEN801` — the first-target SKU, whose Python console was a WIRED
    hardware UART served by Renode's own socket terminal — the warning names that
    deferred path as a second, independent reason its UART is silent.
- **Shell completions are gated against clap (#92).** The three scripts under
  `completion_scripts/` are hand-maintained and nothing compared them to the
  `#[arg(long)]` definitions, so flags drifted silently — `--core` was missing
  from all three since #66 and was caught only by a human reading the diff. A
  test now walks clap's BUILT command tree and asserts, per subcommand and in
  both directions, that every long flag appears in the arm each script actually
  runs (and that no script offers a flag clap no longer accepts). Per-arm, not
  file-wide: zsh's `_arguments` arms do not inherit, so a global flag must be
  repeated in each one, and a whole-file "appears somewhere" check reported
  parity while `tan sdk --format<TAB>` completed nothing. Fixing the drift the
  gate then exposed makes several flags newly completable across all three
  shells. `completion_scripts/**` is now `text eol=lf` in `.gitattributes`: a
  CRLF checkout both breaks the scripts on their target shell and makes the
  gate's layout markers miss, which would have surfaced as a misleading
  "layout changed" panic on the `windows-latest` leg only.
- **`tan doctor` checks the host environment: `zephyrSdkHost`, `longPaths`,
  `homePath`.** alp-sdk ADR 0021's cross-cutting requirements name three host
  facts that decide whether a toolchain can be provisioned at all, and all
  three previously surfaced only as a confusing failure much later.
  - **`zephyrSdkHost` — `Fail`.** The pinned Zephyr SDK publishes host builds
    for `linux-aarch64`, `linux-x86_64`, `macos-aarch64` and `windows-x86_64`
    and nothing else (verified against `zephyrproject-rtos/sdk-ng` `v1.0.1`,
    which is what alp-sdk `west.yml`'s `zephyr: v4.4.1` pin requires via that
    tree's `SDK_VERSION` file). Two hosts are therefore unserved and they are
    **not the same case**. `windows-arm64` has never been published, and the
    ADR's remedy applies: route to WSL2, where the distro is `linux-aarch64`,
    which *is* served. `macos-x86_64` — an Intel Mac — was published through
    `0.17.4` and **dropped in `1.0.0`**, so it is equally unserved at the pin,
    with no WSL2 equivalent and no `macos-aarch64` substitute (Rosetta
    translates x86_64 *for* Apple silicon, not the reverse); its remedy is a
    Linux host, and it says so instead of repeating the WSL2 advice, which on
    macOS cannot be followed. `Fail`, not `Warn`, because there is no artifact
    to install — the same category as a missing `ninja`, which
    `hostPrerequisites` already fails on. Apple silicon and every other served
    host pass. The arch compared is the **machine's**, resolved at runtime
    (`IsWow64Process2` on Windows, `sysctl.proc_translated` on macOS), not
    `std::env::consts::ARCH`, which is the arch tan was *compiled* for. tan
    ships `x86_64-pc-windows-msvc` and `x86_64-apple-darwin` as their own
    release assets and both run on aarch64 hardware — the first because
    Windows-on-ARM emulates x64 transparently (making it the likeliest way tan
    runs there at all, which would have left the `windows-arm64` arm almost
    unreachable), the second under Rosetta (which would have failed a fully
    served Mac and sent its owner after a Linux box). Linux stays on the
    constant: no Linux asset tan ships can differ from its host.
  - **`longPaths` — `Warn`, Windows only.** Reads
    `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled` through
    the registry API (not `reg query`: that costs a process and depends on the
    `PATH` of a host that is by definition suspect). An absent value counts as
    disabled, because absent *is* the Windows default; only a read that
    genuinely failed reports "unknown", and that is a `Warn` too rather than a
    blind `Pass`. `Warn` and not `Fail` because Windows 11 still ships the flag
    off, so failing would exit 4 on essentially every stock Windows host —
    including the many that build fine from a short workspace root. It is a
    probable cause, and its value is attribution: the failure it predicts
    arrives as a CMake or compiler error about a file that plainly exists. The
    fix is the elevated `New-ItemProperty` one-liner, printed rather than run —
    ADR 0021's Tier A promises zero elevation, so an `HKLM` write belongs to
    the undecided Tier B consent flow.
  - **`homePath` — `Warn`, all platforms.** Reports the actual resolved path
    when it contains a space (`USERPROFILE` on Windows, else `HOME`; the same
    resolution `~/.alp` uses). `Warn`, not `Fail`: it is a real historical
    Zephyr breakage but a degraded-but-usable one, and a host whose account is
    two words is not in the same category as one that cannot run the toolchain
    at all. An unresolvable home is also a `Warn` rather than a silent pass.
  On the PLAIN report only, never `--build` — these are host facts needing no
  `board.yaml`, no workspace and no SDK, exactly as `hostPrerequisites` below,
  and ADR 0021 Lane 1 P0a runs `tan doctor` before anything project-shaped
  exists. `zephyrSdkHost` looks adjacent to `--build`'s existing `zephyrSdk`
  probe but answers the opposite question — "can an SDK be installed on this
  host at all" versus "is one installed here" — and reporting the SDK story
  twice under two names is the trap this changelog documents below. The same
  three checks are appended to `tan support-bundle`'s doctor payload, for the
  same reason `hostPrerequisites` is. **Consumer-visible:** `data.checks[]`
  grows by two entries (three on Windows), `data.summary` counts them, and
  `zephyrSdkHost` can move plain `tan doctor` to exit 4 on a `windows-arm64`
  or Intel-Mac host — which compounds the exit-4 change below, and is the
  honest verdict for a machine no pinned toolchain serves.
  (alp-sdk ADR 0021, tan-cli#70)
- **`sdk.west-config-reconcile-failed` (new issue code, `tan sdk switch`).**
  `.west/config`'s reconciliation reported every failure — an unreadable
  config, a read-only one, one held open by another process (the routine
  Windows shape) — identically to "already correct", so the user was told the
  switch was clean while `west` kept resolving its manifest from the stale
  pointer. The three-way outcome (`tan_core::ManifestReconcile`) makes the
  failure distinguishable, and it now surfaces in text and in the envelope with
  the OS's own reason and what to do about it. Severity `warning` at exit code
  `0`, matching `clean.remove-failed` and `build.sdk-switch-pristine-failed` —
  a best-effort repair that failed while the command carried on. The exit code
  is deliberate: the switch itself DID happen (the active-SDK pointer is
  written) and failing it would block the escape hatch out of a broken
  workspace. `tan bootstrap` gained the same distinction as a
  `west-config-reconcile-failed` warning, where it matters most: `west update`
  is about to run against whatever manifest that unrewritten pointer names —
  and a bootstrap whose reconcile failed no longer records the workspace as
  synced, since that update resolved the OLD SDK's manifest.
- **`tan debug-config --pre-launch-task <TASK>`.** Opt-in re-entry for the
  `preLaunchTask` the command used to emit unconditionally (see Changed). The
  flag carries the task NAME rather than being a bare on/off switch, for two
  reasons: a consumer that has actually registered a `TaskProvider` will use
  its own name, not one of ours, so a boolean would keep a tan-owned string
  baked into a file the consumer owns; and with the name supplied from
  outside, the four hardcoded task strings leave the contract entirely instead
  of surviving as a default nobody can change. Off by default — nothing is
  emitted unless a name is passed.
- **`contract/envelopes/` pins `debug-config --preview`.** Four goldens, one
  per `--target-kind` (`debug-config-preview-{zephyr-mcu,baremetal-mcu,
  yocto-userspace,native-host}`). Unlike the seven existing cases these pin a
  `data` value that is itself a consumer ARTEFACT rather than a report:
  alp-sdk-vscode#342 writes `data.configuration` into the user's `launch.json`
  verbatim, so the golden pins the emitted key SET. The `preLaunchTask` bug
  below was reachable only by reading two repos by hand; it would now fail
  `cargo test`. `--preview` reads no `board.yaml`, spawns no Python and probes
  no PATH (the reason `bootstrap`/`doctor` have no golden), so it is
  legitimately host-independent; the one absolute path it reflects back
  (`project.root` / `launchJsonPath`) is tokenized as `__WORKDIR__` by the
  harness.
- **`tan doctor` reports a missing host prerequisite without `--fix`.**
  `check_prerequisites` had no caller outside `tan bootstrap`, so the only way
  a missing `ninja` surfaced was `tan doctor --build --fix`, which runs
  bootstrap to find out — and in the extension a missing `ninja` therefore read
  as `failed to launch (exit code: 1)` from the bootstrap terminal. Plain
  `tan doctor` now runs bootstrap's own gate (not a second copy of it) and
  reports a `hostPrerequisites` check. The CHECK is on the plain report only,
  not `--build`: prerequisites are a HOST fact needing no `board.yaml`, no
  workspace and no SDK, and alp-sdk ADR 0021's Lane 1 P0a runs `tan doctor`
  *before* the bootstrap terminal exists — while `--build` already probes
  `ninja`/`cmake` through `BuildToolProbe` and would report them twice. (One
  fact, one check — but `--build` does carry the machine-readable
  `missingPrerequisites` data derived from those probes; see Changed.) The
  check's detail names which tool list it checked against — the SDK's
  `metadata/bootstrap.json` or tan's built-in fallback — so a run with no
  resolvable SDK still checks the host and says which list it used, rather
  than implying it read the SDK's. A manifest that resolved but was REFUSED
  (unsupported `schemaVersion`, unparseable) is a third case, not the second:
  the refusal message `tan bootstrap` treats as a fatal `ValidationFailure` is
  now carried into the check's detail as
  `metadata/bootstrap.json rejected: …` and downgrades the check to `Warn`
  (a refusal still outranks it as `Fail`). `tan doctor` is the command a user
  runs to find out why `tan bootstrap` refuses, so it is the last one that may
  swallow the reason — it reports it without repeating bootstrap's exit code.
  The same check is appended to `tan support-bundle`'s doctor payload, for the
  reason below. Two caveats worth stating plainly: **the `ninja` case above is
  Windows-only**, because the tool list is — the manifest's
  `prerequisites.posix` is `[git, cmake, python3]` and names no `ninja`, while
  `prerequisites.windows` adds it, an asymmetry the manifest records faithfully
  rather than unifying (on Linux/macOS a missing `ninja` still surfaces only
  through `tan doctor --build`'s `BuildToolProbe`, which probes it by name on
  every platform); and the gate **spawns interpreter subprocesses**
  (`probe_host_python`, which is what makes this check a strict superset of the
  retired `python` one), so plain `tan doctor` and `tan support-bundle` now cost
  ~0.5 s per invocation where they previously did PATH lookups only (measured
  516/548/516 ms for `tan --format json doctor`, debug build, Windows host).
  That is the price of the check, not a regression — but the extension may call
  plain `doctor` on activation, so it is recorded here rather than
  misdiagnosed later. (alp-sdk ADR 0021 P0a)
- **`tan bootstrap` reports its missing prerequisites as structured data.**
  The envelope's issue message is the message lines joined with a space, and
  an install command contains the same spaces the join used — so
  `Missing required tools:   ninja  ->  winget install -e --id
  Ninja-build.Ninja Install the tools above …` cannot be split back into
  `<tool>`/`<command>` pairs safely, and alp-sdk-vscode#347 deleted the parse
  that tried. `data.missingPrerequisites` now carries
  `[{tool, command}]` alongside the unchanged message: `command` is the
  `winget install` one-liner where tan knows one and `null` where it does not
  (an unlisted tool, and every POSIX host until alp-sdk#949 lands
  `prerequisites.install.posix`) — never advice prose, which a consumer would
  render as a runnable button that cannot work. The field is `null`, not `[]`,
  on every run that did not reach the prerequisite gate, so "not reported" is
  distinguishable from "reported empty". `data.schemaVersion` stays `"2"`: the
  field is additive and optional, and a consumer that does not know it is
  unaffected. The two Python-floor refusals — which have no missing tool at
  all, so no `{tool, command}` pair could carry their fix — now report under
  their own codes `bootstrap.python-not-runnable` and
  `bootstrap.python-too-old` instead of `bootstrap.prerequisites-missing`;
  **a consumer matching `bootstrap.prerequisites-missing` for those two cases
  must add the new codes.** `bootstrap.prerequisites-missing` itself is
  unchanged for the missing-tool case, message text included. (#70)
- **`tan build` auto-pristines a slice build dir left stale by an SDK switch.**
  Switching the active SDK (`~/.alp/sdk/v0.11.0` → `~/.alp/sdk/v0.13.0`) left
  every previously-configured slice failing with west's raw `Build directory
  … is for application "…/v0.11.0/firmware/alp-stock-shim" … FATAL ERROR:
  refusing to proceed without --force`, which in the extension surfaced only
  as `terminated with exit code: 1`. Each slice build dir now carries a
  `.tan-sdk-root` stamp written before the tool spawns; a dir that is
  configured but absent-or-differently stamped is wiped and re-configured,
  reported as `build.sdk-switch-pristine` naming both SDK roots. The wipe
  skips any slice with an explicit `-d`/`--build-dir` and only fires under
  the project's own `build` root. (#52)
- **`tan renode --core <CORE_ID>`** — boot ONE Zephyr slice of a multicore
  project in the headless smoke. A manifest with more than one Zephyr slice (an
  E1M-AEN801's `m55_hp` + `m55_he`) was refused outright with "the Renode smoke
  boots a single-Zephyr-slice system", leaving no way to smoke-test such a
  project at all. `--core` narrows the zephyr set before the runnable filter, so
  an explicitly named blocked/skipped slice still boots exactly like a lone one
  does (the smoke touches no hardware). A name matching no zephyr slice fails
  with `UnknownCore`, listing the manifest's zephyr cores. The refusal message
  now names the flag. Unchanged for a single-slice project.

### Changed
- **`tan sdk switch <version>` resolves the bare version against more than one
  cache root (#62).** It joined `~/.alp/sdk-cache` and nothing else, while the
  layout that reported #62 keeps its SDKs under `~/.alp/sdk` (the VS Code
  extension's install root) — so `tan sdk switch v0.13.0` failed with
  `path-not-found` on a version sitting right there on disk, and the whole
  `.west/config` reconciliation shipped in #74 was unreachable for exactly the
  users who needed it. Three roots are tried in a fixed order, first real
  checkout wins: `--destination` (now honoured by `switch`, not just
  `install`), then `~/.alp/sdk-cache` (so `install X && switch X` selects what
  the install just wrote), then the parent directory of the currently active
  SDK — no config declares a cache root, so where the active SDK sits is the
  only authoritative record of where this machine keeps them. Not a filesystem
  search: three named roots, each of which the user can point at.
- **`sdk.bootstrap-recommended` is derived from workspace state, not from
  whether a rewrite fired.** It was latched to the `.west/config` rewrite
  happening, so a *second* `tan sdk switch` — pointer already reconciled by the
  first, `topdir/zephyr` and `modules/` still the previous SDK's trees — went
  silent exactly when the user had not acted on the advice yet. It now fires
  whenever the workspace cannot be shown to match the selected SDK: the pointer
  must name it AND a `tan bootstrap` `west update` must have been recorded
  against it. `tan bootstrap` writes that record (`<topdir>/.west/
  tan-workspace-sdk`) after an update that actually ran; nothing else on disk
  answers "which SDK's manifest were these trees checked out from", since
  `.west/config` is rewritten by the reconcile itself without the trees
  changing. **A workspace bootstrapped before this record existed has none, so
  the first `sdk switch` after upgrading advises a bootstrap it may not need —
  one `tan bootstrap` run clears it for good.** The message wording follows the
  evidence: a diverged pointer *proves* the workspace belongs to another SDK, a
  matching one with no record only means it cannot be confirmed.
- **`tan debug-config` no longer emits `preLaunchTask` by default — it was
  naming a task nothing defines.** Every generated profile carried one of
  `alp: build active target`, `alp: build baremetal target`, `alp: deploy and
  start gdbserver` or `alp: build native_sim target`. No `tasks.json` in this
  repo or in a generated project defines them, and alp-sdk-vscode contributes
  only `{"type":"alpRun"}` with no `TaskProvider` registered for any of the
  four. VS Code resolves `preLaunchTask` BEFORE launching, fails to find the
  task, and aborts pre-launch — so the session never started, out of a
  `launch.json` that reads perfectly. Consumer-visible payload change:
  `data.configuration` (and the written `launch.json`) has one fewer key.
  Build-before-debug is still the behaviour we want, which is why the
  capability came back as `--pre-launch-task` above rather than being deleted;
  it just cannot be the default while nothing provides the task.
- **The `doctor` envelope gained `data.missingPrerequisites` and a
  `doctor.hostPrerequisites` issue code.** The new check (see Added) reports
  `Fail` on every prerequisite refusal — each one blocks a build, and bootstrap
  itself refuses to run against exactly these — so **a host missing a
  prerequisite now makes plain `tan doctor` exit `4` (`doctorFailure`) where it
  previously passed**, and raises a `doctor.hostPrerequisites` error issue. The
  structured half rides on `data.missingPrerequisites`, deliberately the same
  key, the same `[{tool, command}]` element and the same `null`-never-`[]` rule
  as the `bootstrap` envelope's field, so one fact does not get two
  vocabularies. The code is `doctor.*`, not the `bootstrap.prerequisites-missing`
  a consumer may already match: in this CLI an issue code's prefix is the
  command that emitted the envelope, without exception, and a `bootstrap.*` code
  inside a `doctor` envelope would tell a consumer a command ran that did not.
  `missingPrerequisites` is present on **both** `doctor` payloads — the plain
  report (including its error envelopes) and `--build`'s `BuildReadinessReport`
  — always as an explicit key, `null` when there is no missing TOOL to name.
  What differs is where each gets its list from, and that is deliberate:
  - **plain `tan doctor`** carries the `hostPrerequisites` CHECK and fills the
    field from its refusal. `null` on a clean host, on an error envelope that
    never reached the probe, and on the two Python-floor refusals, whose fix no
    `{tool, command}` pair can carry.
  - **`tan doctor --build`** carries the field as **data only — there is no
    `hostPrerequisites` check in that mode.** It already probes
    `west`/`cmake`/`ninja`/`bitbake` through `BuildToolProbe` and reports each
    as its own check, so mirroring the aggregate check would report the same
    tool twice under two names. The field is derived from exactly those
    PATH-binary checks, so it inherits their OS gating (a Zephyr-only project is
    never told to install `bitbake`; a non-Linux host gets `yoctoHost` instead
    of a `bitbake` entry) and their dedup (`cmake`, needed by two declared OSes,
    appears once). Excluded on purpose: `zephyrSdk` (env-var detection, its fix
    is a docs URL), `bmaptool` (two tools, one advisory, working `dd`
    fallback), `yoctoHost` and `vendorToolchain` (no tool name at all) — none
    has a single `{tool, command}` pair that could carry it. `command` is the
    `winget` one-liner only on Windows and only for a tool tan knows one for;
    `west` and `bitbake` report `command: null` rather than an invented ID.
    This is what `alp-sdk-vscode` needs: it calls only `tan doctor --build`
    (`src/toolchain.ts:219`, `:248`), and its `runToolchainFix` previously had
    nothing runnable to put behind a Fix button, so a missing `ninja` reached
    the user as `failed to launch (exit code: 1)`.

  `--build`'s payload `schemaVersion` stays `"1"`: the field is additive and
  optional, and its other keys (`generatedAt`/`osSet`/`summary`/`checks`/
  `nextSteps`) are unchanged. (alp-sdk ADR 0021 P0a)
- **The retired `doctor` `python` check.** Plain `tan doctor` no longer emits a
  `python` check. It probed `context.python_binary`, which in this CLI is always
  the bare `python3`/`python` — literally the tool `hostPrerequisites` now probes
  off the manifest's prerequisite list — so one host fact landed twice under two
  names with two severities (`Warn` vs `Fail`) and two different exit-code
  consequences. The retired one was also the weaker probe: no `pythonMinVersion`
  floor, and no `py`-launcher widening, so a Windows host with only the launcher
  installed got a `python` `Warn` beside a `hostPrerequisites` `Pass` about the
  same interpreter. **A consumer matching the `python` check name or the
  `doctor.python` issue code must move to `hostPrerequisites` /
  `doctor.hostPrerequisites`**, which reports the same fact as a `Fail`.
  `tan doctor --build` is unaffected (it never had this check).
- **`tan support-bundle`'s doctor payload gained `missingPrerequisites` and the
  `hostPrerequisites` check.** The bundle (payload `schemaVersion` `"1"`) built
  its `DoctorReport` without ever running the prerequisite gate, so it serialized
  `"missingPrerequisites": null` — which that field defines as "checked, nothing
  missing" — for a host nobody probed. A bundle is what a user attaches
  *precisely when bootstrap failed*, so it both hid the missing `ninja` and
  asserted the host was fine. It now runs the same gate, which also means **a
  missing prerequisite makes `tan support-bundle` exit `4` and emit a
  `support-bundle.hostPrerequisites` error issue** (the bundle file is still
  written). Payload `schemaVersion` stays `"1"`: additive and optional.
- **`nextSteps` now includes the remediation of every appended check.**
  `nextSteps` was computed once inside the report builders, before `tan-cli`
  appends the checks that need IO — `hostPrerequisites`, `sdkProvenance`, and
  on `--build` the project/workspace preflight and the `--fix` bootstrap outcome
  — so those checks' `fix` strings never reached the field the envelope
  documents as "deduplicated remediation steps for non-passing checks" and the
  extension renders as a Fix button. Appending a check now re-derives the field
  as part of the same call (`tan_core::append_doctor_check` /
  `prepend_doctor_checks`), so there is no trailing recompute statement left for
  a caller to forget. **`nextSteps` gains entries and follows check order**; on
  `--build` the preflight's `tan sdk switch <path>` / `tan init` now lead it.
- **`tan bootstrap`'s two Python-floor refusals report under their own issue
  codes.** A host whose `python` does not run now raises
  `bootstrap.python-not-runnable`, and one below `pythonMinVersion` raises
  `bootstrap.python-too-old`, instead of both sharing
  `bootstrap.prerequisites-missing` — neither names a missing TOOL, so neither
  can carry the new `data.missingPrerequisites` entries (see Added).
  **A consumer matching `bootstrap.prerequisites-missing` for those two cases
  must add the new codes**; the code is unchanged, message text included, for
  the missing-tool case it originally described. (#70)
- **Install commands come from the SDK manifest, not tan's `winget` table
  (#90).** `data.missingPrerequisites[].command` — the field alp-sdk-vscode's
  `runToolchainFix` puts behind a Fix button — was rendered from a hardcoded
  four-entry `match` on tool name, plus two more copies of
  `Python.Python.3.12` embedded in the Windows Python-floor refusal prose. It is
  read from `prerequisites.install` (alp-sdk#959, ADR 0021 Lane 1 P0b) now, and
  the table is **deleted** rather than kept as a fallback: `fallback_facts` —
  which an SDK without `metadata/bootstrap.json` falls back to, i.e. every SDK a
  customer can install today — carries the same commands, pinned byte-equal to
  the vendored manifest by the fallback-vs-manifest field-for-field test, so no
  host loses a command and there is no second, ungated copy of a drift-gated
  fact. A manifest predating #959 has no `install` key at all;
  that stays a clean parse (it is additive at an unchanged `schemaVersion: 1`,
  and a hard refusal there would reach `tan build`/`tan run` through
  auto-bootstrap) and gap-fills from the same constants when the `install` key is
  absent entirely, the rule tan already applies to a build-plan key an older
  producer omits. The gap-fill is **per OS**: an out-of-contract `install` that
  serves only some of `linux`/`macos`/`windows` fills the rest from the constants
  instead of leaving them empty, so a manifest carrying `windows` alone cannot
  silently strip every POSIX command (or, with `install: {}`, all of them).
- **Every POSIX `missingPrerequisites` entry reported `command: null`.** That
  branch had no install commands at all; `prerequisites.install.linux`/`.macos`
  supply real ones, so Linux gets `sudo apt-get install -y cmake` and macOS
  `brew install cmake` where both used to get nothing. Resolution is by HOST,
  in one place: the manifest keys install commands `linux`/`macos`/`windows`
  while keying the tool LISTS `posix`/`windows`, and collapsing that asymmetry
  anywhere else would hand a macOS user Debian's package manager. A POSIX host
  the manifest does not serve (neither Linux nor macOS) keeps the all-`null`
  behaviour rather than being handed the nearest OS's commands. The printed
  POSIX refusal LINE is unchanged — `bootstrap.sh` names the tools and nothing
  else, and it is still the parity oracle. `tan doctor --build`'s
  `BuildToolProbe` loses its `is_windows` field with the table it existed to
  gate.
- **`tan doctor --build` reports a REFUSED `metadata/bootstrap.json` (#90).**
  New `bootstrapManifest` check, `warn`, in `data.checks[]` — with the rejection
  message verbatim and the same fix prose plain `doctor` puts in
  `hostPrerequisites`' tail. `--build` now reads the manifest (for the install
  commands above), and a version-skewed or unparseable one made it substitute
  tan's compiled-in constants with **nothing on the wire**: no check, no issue,
  and `sdkProvenance` reports only the git short-commit and
  `metadata/sdk_version.yaml`, never the manifest. `--build` is the mode
  alp-sdk-vscode shells for `runToolchainFix`, so on a future `schemaVersion: 2`
  SDK its Fix button would have run a stale command silently — the exact drift
  the version-skew guard exists to prevent. `warn`, not `fail`: the exit code is
  unchanged and the fallback commands are still real.

### Removed
- **`tan init --template host-tooling-starter` (#14).** Retired entirely
  while closing out the SDK scaffold catalog — its `WizardTemplateId`
  variant, generator, and registry entry are gone, not just left unvendored.
  `tan init --template host-tooling-starter` now exits 2 with
  `init.invalid-template`. `minimal-app` is now the only template left
  hand-generated, deliberately deferred (its `contract/` golden is owned by
  an in-flight contract-surface change).

### Fixed
- **`ninja` was a Windows-only prerequisite in tan's own fallback, so a POSIX
  host on a legacy SDK still hit the original defect (#121).** alp-sdk declared
  `ninja` a POSIX prerequisite with real `install.linux`/`install.macos`
  commands in #971/#981 (merged `d6fd3a18`), but the fact lives in THREE places
  and only one had moved:
  - `metadata/bootstrap.json` upstream -- fixed there;
  - `contract/fixtures/bootstrap/manifest.json`, tan's vendored copy -- now
    re-vendored byte-exact (sha256 `5202025aac269040f1893c843b2071d69f0a7f4bdd7b91755d832aa706466c7a`,
    5577 bytes, LF) with `PINNED_SDK_TAG` moved `0ed078a6` -> `3ffd8774`,
    20 commits forward;
  - **`fallback_facts`, the hand-ported constants a legacy SDK with no manifest
    actually uses** -- which still said `prerequisites_posix: [git, cmake,
    python3]` and served no POSIX `ninja` command. Fixed here. That third copy
    is the one a customer on a released SDK hits.
  - Three expectations moved with it, each because the data moved and not to
    make the suite green: `parses_every_field_of_the_real_manifest` (reads the
    re-vendored fixture), `the_posix_refusal_stays_one_line_but_now_carries_real_commands`
    (whose name had outrun its assertion -- `ninja` was the last commandless
    POSIX entry), and `a_posix_host_gets_its_own_package_manager_and_never_winget`,
    which carried a written prediction that it would go red on exactly this
    change, with instructions not to weaken it.
  - Note `ninja-build` vs `ninja`: the package name differs from the binary
    name, which is the whole argument for carrying these as data.
- **A pre-release tag would have shipped to every customer as `latest`.**
  `release.yml` set neither `prerelease` nor `make_latest`, so a `v0.4.0-rc1`
  tag's classification rested entirely on the action's default -- and
  `install.sh` fetches `releases/latest/download/<asset>` directly, which
  GitHub excludes a pre-release from ONLY when the flag is set. Both flags are
  now derived from the one fact that distinguishes them, the hyphen in the tag,
  so they cannot disagree with each other or with the tag.
  - `publish_crates` and `publish_npm` skip a pre-release. npm was the sharpest
    of the three: `npm publish` passes no `--tag`, so it defaults to the
    `latest` dist-tag, and an unguarded rc would have become plain
    `npm i -g @alplabai/tan` -- with npm unpublish far more restricted than a
    crates.io yank. Skipping keeps an rc fully retractable, which is the reason
    to cut one; `--tag next` is the documented relaxation.
  - `docs/release-contract.md` gains the pre-release contract, and its Linux
    target table is corrected: it documented `linux/x64`+`linux/arm64` as
    consuming the `-gnu` assets with musl "not (yet) wired into"
    `releaseAssetForTarget`, while the extension has mapped both to `-musl`
    because the `-gnu` assets carry a glibc floor. The doc now separates the
    zigbuild PIN (`2.31`) from the MEASURED floor of the shipped binary
    (`GLIBC_2.30`, per `readelf -V`), and warns off the "2.31 floor /
    GLIBC_2.39 not found" wording the extension still carries -- the
    phenomenon is real but both numbers in it are wrong
    (alp-sdk-vscode#370).
    alp-sdk fixed the same mix-up in its own install docs in alp-sdk#990.
- **`tan explain --template edge-ai-starter` described a project that is not
  the one `tan init` writes (#124).** `project_template_details` read the
  wizard registry's `libs` field unconditionally, but that field is
  deliberately blanked for a vendored template (its files come from the SDK's
  `--emit scaffold` tree instead) — `edge-ai-starter` reported "Default
  libraries: (none)" while its vendored `board.yaml` declares
  `libraries: [tflite-micro]`, one line under prose that names TFLite-Micro
  directly. `iot-starter` and `board-diagnostics` had already been hand-synced
  correct ahead of this fix (#128); `edge-ai-starter` was the one live wrong
  answer. `explain`'s "Default libraries" line now derives from the vendored
  `board.yaml`'s own `libraries:` block (`vendored_library_names_for`, new in
  `tan-core::wizard::service::vendored`) for every vendored template, instead
  of a second hand-synced registry field that can drift from it — the
  registry's `libs` stays authoritative only for `minimal-app`, the one
  template left hand-generated. "Default features" is unchanged (still
  registry-sourced): a vendored `board.yaml` has no representation for
  `iot-starter`'s inherent `mqtt: true`, so deriving that line fully is not
  possible without reintroducing a different self-contradiction.
  - Follow-up hardening (review of #137): `vendored_library_names` parsed the
    vendored `board.yaml`'s `libraries:` block with a hand-rolled line scan
    that matched only the `- name: <value>` spelling, not the bare-shorthand
    `- <value>` form `tan_core::model`'s own `LibraryEntry` already accepts —
    a future re-vendor shipping the shorthand form would have silently gone
    back to "Default libraries: (none)" with no test catching it. It now
    parses through `tan_core::model::BoardModel`/`LibraryEntry` directly, so
    both spellings are covered. Also widened
    `vendored_library_names_matches_across_families` from asserting only the
    `edge-ai` AEN/V2N pair to all four (`minimal`/`sensor`/`edge-ai`/
    `diagnostics`) — the doc comment already claimed family-invariance for
    every vendored template, but only one pair was checked.
- **`tan bootstrap` reused a workspace across a patch-level Zephyr bump, so the
  next build was green against the wrong Zephyr AND the wrong hal_alif (#98).**
  The reuse test compared only `MAJOR.MINOR`, so upgrading alp-sdk `v0.13.0` ->
  `dev` (zephyr `v4.4.0` -> `v4.4.1`, hal_alif `v2.2.0` -> `v2.3.0`) printed
  `Reusing compatible alp-sdk workspace` and skipped `west update` entirely.
  `parse_zephyr_version_file` and `parse_west_zephyr_pin` now carry
  `MAJOR.MINOR.PATCH` and `decide_workspace_reuse` compares the whole pin, with
  a new `Stale` outcome for a tree that IS this SDK's, just left behind.
  - Stale runs `west update` rather than only warning: a warning alone leaves
    the next build green against the wrong Zephyr, which is the defect itself.
    It is not the aggressive reading either -- it is byte-for-byte the command a
    bootstrap with no `$ZEPHYR_BASE` would run over the same topdir, gated on a
    manifest that already proved the tree belongs to this SDK. It also fixes the
    part a zephyr-only comparison never could: `west update` moves `hal_alif`,
    `cmsis` and `mcuboot` to their pins too.
  - The second route is closed as well. `tan build`'s auto-bootstrap fires on
    `is_warn("zephyrVersion")`, which compared two `MAJOR.MINOR` values, so
    `4.4` == `4.4` and no re-bootstrap fired -- making `--no-auto-bootstrap`'s
    own `--help` text ("by default a text-mode build with ... a stale one, runs
    `tan bootstrap` first") a false promise. It now reaches that branch.
- **An unreadable `metadata/bootstrap.json` was indistinguishable from an absent
  one (#99).** `load_facts` treated EVERY read error as "legacy SDK", so a
  `chmod 000` manifest on a `dev` tree and a released tree with no manifest
  produced envelopes identical in every field carrying a verdict: `ok:true`,
  `exitCode:0`, `factsFromManifest:false`, empty `issues`. The conflation was
  deliberate and its comment said why ("every released alp-sdk today has no
  manifest at all") -- a premise that expired when `dev` shipped one. Only
  `ErrorKind::NotFound` falls back now; every other kind is a hard error naming
  the path and the OS error, in the same shape `parse_bootstrap_manifest`
  already produces.
- **Plain `tan doctor` probed nothing about the build environment and printed
  byte-identical output across four materially different host states (#100).**
  It is the command alp-sdk's `bootstrap` prints as the customer's very next
  step and `README.md`'s Quickstart documents as the health check that "catches
  a missing toolchain/HAL", yet it ran only the debug-readiness set — the same
  seven checks, same summary, same exit 4 on a host whose documented example
  build failed on both Zephyr slices and on the host where it succeeded. It now
  folds in `probe_build_preflight`, the same call `tan build` and
  `tan doctor --build` already make, so `sdk` / `workspace` / `westResolved`
  appear in plain `tan doctor` too. `--build` is unchanged: it keeps its own
  `board.yaml`-derived OS-set resolution and its `BuildToolProbe` layer, and its
  envelope key set is now pinned by a test — it is the live cross-repo contract
  `alp-sdk-vscode` shells (`["doctor","--build"]`, `["doctor","--build","--fix"]`),
  and it has no plain-`doctor` consumer, which is what makes the fold safe. The
  preflight's own `boardYaml` is dropped from the fold so exactly one check of
  that name is ever emitted.
- **`tan doctor --fix` parsed, was accepted, and did nothing (#100).** `run()`
  reads the flag only inside its `--build` branch, so `tan doctor --fix`
  produced output line-for-line identical to plain `tan doctor` — no "fixed N",
  no "nothing to fix", no error. It is now `requires = "build"` at the clap
  level and fails as a usage error. `--build --fix` is unaffected.
- **`boardYaml` hard-failed with exit 4 at an alp-sdk checkout root, where there
  is no `board.yaml` and no reason for one (#100).** That is exactly where
  `bootstrap` tells a customer to run `tan doctor`, so the first command a new
  user typed reported `1 failed` for a non-problem. A missing `board.yaml` is
  now a warning when no project was named and a failure once `--project` or
  `--board-yaml` selected one. `tan doctor --build`'s own `boardYaml` stays a
  hard fail — that mode answers "can this build run", and none can without it.
- **`tan doctor` claimed `vadimcn.vscode-lldb is installed.` on hosts with no
  VS Code and counted it among the passes (#102).** The standalone binary cannot
  enumerate a marketplace extension; the `DebuggerExtensionsState` all-`true`
  literal at three call sites was an inherited assumption from the extension's
  `resolveCliDebugContext`, where `true` IS correct because that code can
  introspect its own host. The four extension-presence checks
  (`codeLLDBExtension`, `cortexDebugExtension`, `cppToolsExtension`, the MCU
  companion viewers) now render a new `unknown` status outside VS Code: not a
  pass, counted in no summary bucket, raising no issue and no next step. The
  pass-through `true` defaults stay in `tan-core` for the extension's use.
  `--build` emits no `unknown` check, so its envelope is untouched.
- **`sdkRoot`'s failure text named "The extension" from the standalone binary
  (#102).** `tan` itself did the resolving; the message now says
  `No alp-sdk checkout resolved.` and points at `tan sdk switch <path>` /
  `--sdk-root <path>`.
- **`debug-config` emitted `"type": "codelldb"`, a debug type no extension
  registers, so F5 refused every native_sim session (#104).**
  `vadimcn.vscode-lldb` v1.12.2 declares
  `contributes.debuggers[0].type = "lldb"` — `codelldb` is the extension's
  marketplace NAME, not a debug type, and VS Code answered `Configured debug
  type 'codelldb' is not supported.` native_sim is the only debug target
  reachable with no probe and no board, i.e. the first debugging experience any
  customer has, so this had never worked. The value is now `lldb`, taken from
  CodeLLDB's own manifest rather than from what the code used to say. The class
  is detectable from here on: `every_emitted_debug_type_is_one_an_extension_contributes`
  walks every target kind × server and checks the emitted `type` against a
  hardcoded table of the three types tan can emit — `cortex-debug`
  (`marus25.cortex-debug` v1.12.1), `cppdbg` (`ms-vscode.cpptools` v1.23.6),
  `lldb` (`vadimcn.vscode-lldb` v1.12.2), exactly the extensions `debug doctor`
  declares — each row naming the extension and version it was verified against,
  and a compile-time guard failing the build if a new `DebugTargetKind` is added
  without listing it.
- **`debug-config` overwrote a hand-resolved `launch.json` value with a
  `<resolved-…>` placeholder on every write (#105).** A same-named configuration
  was replaced wholesale, so a customer told to hand-fill
  `"device": "AE822F4M55_HP"` got `"device": "<resolved-device>"` written back
  over it on their next F5 — data loss on their own file, with no confirm and no
  backup, and an unexitable loop around the advice they had just been given. The
  same held for every `<resolved-…>` this command emits (`svdFile`/`svdPath`/
  `gdbPath`/`serverpath`/`searchDir`/`configFiles`/`miDebuggerPath`). The write
  plan now merges key-by-key over the existing entry under one narrow rule: an
  incoming unresolved placeholder never overwrites a concrete existing value.
  That rule is also what separates "the customer set this deliberately" from
  "this is our old output" — our output for a field we cannot resolve is
  *literally* an angle-bracket token, so anything concrete in the file is real.
  The inverse still works: whenever a run CAN resolve a field the incoming value
  is concrete and overwrites unconditionally, so a stale value that is now wrong
  is still updateable (the `codelldb` → `lldb` repair above lands on existing
  entries for exactly this reason). Arrays follow the same rule with a
  whole-list case: an all-placeholder incoming `configFiles` keeps the existing
  list intact, or a hand-added second `.cfg` would be lost to a per-index merge
  against a one-element draft. Key order follows the existing entry with new
  keys appended, and keys the customer added that tan never writes
  (`preLaunchTask`, `serverArgs`, …) are untouched.
- **The placeholder predicate called `<host>:<port>` a resolved value.** It
  tested for the `<resolved-` PREFIX, so the yocto draft's two-token
  `miDebuggerServerAddress` passed as a real address. Concretely: a yocto config
  whose `<resolved-gdb>` did resolve lost the "Placeholder fields … still need
  resolution" note while its gdbserver address was still unusable — the note
  going silent on exactly the config that cannot launch. The test is now any
  angle-bracket token (`is_unresolved_placeholder`, matching the extension's
  `/<[^<>]*>/`), and `${workspaceFolder}`-style VS Code substitutions still
  count as resolved because they carry no angle bracket. One predicate in
  `tan-core` now backs both the note and the merge, so "still needs resolution"
  and "do not overwrite this by hand-filled value" cannot disagree.
- **`tan doctor --build` rated a missing `ninja` or `cmake` `warn`, so it exited
  0 on a host that cannot build (#103).** `ninja` is the generator CMake picks by
  default on every Zephyr host, so its absence does not degrade a build, it stops
  `west build` outright; `cmake` is at least as blocking (Zephyr AND baremetal).
  Both now report `fail` when absent, which also makes `--build` agree with plain
  `tan doctor`, whose `hostPrerequisites` check has always called a missing
  prerequisite `fail`. Deliberately NOT widened further: `west` stays `warn`
  because this check probes bare PATH while the executor resolves west from the
  workspace venv, so a correctly bootstrapped host that builds fine routinely
  fails the probe (the venv-aware verdict is the preflight's `westResolved`
  check, already in the same report); `bitbake`, `zephyrSdk`, `bmaptool` and
  `vendorToolchain` are optional or advisory by design. Severity is independent
  of whether the manifest carries an install one-liner — a `null` command means
  tan cannot offer a button, not that the build will succeed.
- **A terminal user never saw the runnable install command `tan doctor --build`
  already had (#103).** `missingPrerequisites[].command` has been sourced from
  the SDK manifest's `prerequisites.install.<os>` since #95, but text mode
  renders only `checks` and `nextSteps`, so the CLI showed `Install Ninja.` while
  the VS Code extension's Fix button got `sudo apt-get install -y ninja-build`
  from the same report. The command is now appended to each check's `fix` prose —
  appended, not substituted, because the prose carries constraints the command
  does not (`cmake (>=3.20)`) — and omitted when the manifest lists none, the
  same "never invent one" rule `command` follows.
- **A latent `retarget_board_yaml_som` bug the vendored `iot` scaffold's
  column-aligned `som.sku:` comment exposed (#14).** Retargeting onto a
  tree's own SKU (a claimed byte-exact no-op) used to collapse the comment's
  alignment to a fixed two-space gap. It now replaces only the value token,
  leaving the rest of the line untouched.
- **The documented Quickstart `tan --project examples/<cat>/<name> build`, run
  from an alp-sdk checkout root, failed with `no SDK selected` (#101).** SDK
  auto-discovery only ever probed the workspace root itself and two named
  SIBLINGS (`alp-sdk`, `alp-sdk-upstream`), never walking UP — and
  `cli_workspace_root` is `cwd.join(--project)`, so the Quickstart's nested
  example put the workspace root three levels BELOW the very checkout the
  command was invoked from, where no lateral candidate can exist. Discovery now
  falls back to the nearest ENCLOSING checkout (`tan_core::nearest_ancestor_sdk`,
  walking parents for `scripts/alp_project.py` and stopping at the first match
  or the filesystem root). The tier is shared by both discovery paths —
  `util::discover_sdk_root` (build/validate/doctor) and
  `tan_core::discover_workspace_sdk` (`tan sdk current`'s `sourceTier`) — so the
  documented "`sourceTier` never claims `discovery` for a path build won't
  resolve" invariant still holds. It is a strict fallback: it only runs when
  nothing lateral answered, so every workspace that resolved before resolves to
  exactly the same path, and because the walk stops at the first match it
  contributes at most ONE candidate and can never trip `project.rs`'s
  deliberate two-or-more-is-ambiguous rule.
- **`no SDK selected` pointed at a remedy that reports success and fixes
  nothing (#101).** The `.alp/sdk-path` pointer `tan sdk switch` writes is scoped
  per `--project` (deliberately — pinned by
  `switch_and_current_use_project_scoped_workspace_root_not_process_cwd`), so a
  bare `tan sdk switch <path>` printed `Switched project SDK to …`, visibly
  changed `tan sdk current`, and then left a `tan --project <p> …` build failing
  byte-for-byte identically. Under `--project` the check now names the scoped
  invocation (`tan --project <p> sdk switch <path>`) and `--sdk-root`, the one
  flag that always works and which the message never mentioned. The scoping
  itself is unchanged.
- **A `zephyr` slice that never loaded Zephyr was reported `[+] ok` for a host
  x86-64 binary (#97).** The out-of-the-box path — `tan init
  --non-interactive` then `tan build --native` — scaffolded the `minimal-app`
  template, whose hand-generated `CMakeLists.txt` never calls
  `find_package(Zephyr ...)`. `west build -b <board> <project>/src` configures
  such a tree anyway (CMake only emits a *dev* warning about the missing
  `project()` call), so the board name was never validated, `ninja` linked a
  host executable, the tool exited 0, and the executor reported `[+] ok
  (rc=0)` for an artefact `readelf -h` calls `Machine: Advanced Micro Devices
  X86-64` with no `zephyr/` build output at all. Two fixes, both required:
  - The executor now refuses such a slice. After a `zephyr` slice exits 0 it
    checks the build dir for evidence that Zephyr's boilerplate actually ran —
    a `ZEPHYR_BASE:` entry in `<cwd>/build/CMakeCache.txt` (what
    `find_package(Zephyr)` caches — verified present, at line 42, in a real
    Zephyr slice build dir, and absent from a baremetal plain-CMake build's
    complete 339-line cache) or, as a fallback, a `<cwd>/build/zephyr/`
    directory. Both signals are checked at the top of the build dir AND across
    its immediate subdirectories, because `--sysbuild` is a live path here (the
    V2N plan carries `-DSB_CONF_FILE=…/zephyr/sysbuild/v2n/sysbuild.conf`) and
    its superbuild owns the top-level dir while the real per-image Zephyr builds
    nest one level deeper — without that look the guard would fail a correct
    V2N build. One level is enough; sysbuild nests per-image, not recursively.
    Both are generated
    build artefacts, deliberately NOT configure-log text: a grep for
    `ZephyrConfig.cmake` breaks the moment CMake rewords a line. With neither
    present the slice fails with the customer-actionable cause — its
    `CMakeLists.txt` must call `find_package(Zephyr REQUIRED HINTS
    $ENV{ZEPHYR_BASE})` before `project()` — instead of an rc. The guard
    stands down when the slice redirects west's build dir (`-d`/`--build-dir`),
    where the evidence lives somewhere tan cannot see, the same refusal
    `resolve_zephyr_artefact` and the SDK-switch wipe already make. Living in
    the executor, it survives any future change to the default template or SKU.
  - The non-interactive `tan init` defaults are now a buildable pair:
    `zephyr-app` (vendored from the SDK's `minimal` scaffold, real
    `find_package(Zephyr)` + `board.yaml` → `alp.conf` via `EXTRA_CONF_FILE`)
    instead of `minimal-app`, and `DEFAULT_SOM_SKU` `E1M-AEN801` instead of
    `E1M-AEN701`. AEN701 has no qualified board tree in alp-sdk — only the two
    loose `zephyr/boards/alp_e1m_aen701_m55_{he,hp}.overlay` files — so its
    sibling `m55_he` slice died with `No board named
    'alp_e1m_aen701_m55_he' found`; AEN801 is the lead part and the only AEN
    SKU carrying both `zephyr/boards/alp/e1m_aen801_m55_he` and `…_m55_hp`.
    `minimal-app` and `E1M-AEN701` both remain valid explicit `--template` /
    `--som` values. This half shipped WITH the guard, never before it: alone it
    would have removed the `m55_he` failure that was the only reason the run
    exited non-zero, turning a red run into a green one with the host binary
    still in place.
- **A slice-confined unresolved `${TOOLCHAIN_ROOT}` failed the WHOLE plan,
  not just the slice that needed it.** `substitute_plan_tokens` inspected
  only the FIRST `${...}`-shaped token in each field and, on an unresolved
  `${TOOLCHAIN_ROOT}`, returned `UnresolvedToolchainRoot` for the entire
  plan — so ONE Zephyr slice naming a toolchain this host hasn't installed
  (e.g. a per-slice `ZEPHYR_SDK_INSTALL_DIR` override) refused to build
  every OTHER slice too, even a `native_sim` slice that needs no toolchain
  at all. The pass now scans every token in a field to completion (so a
  genuinely unknown token — a version/bug fact — still hard-fails the
  plan regardless of where it sits relative to a known one), and when the
  only problem in a slice's own fields is an unresolved `${TOOLCHAIN_ROOT}`,
  reports it as a demoted slice instead of erroring the plan. `tan build
  --native`'s executor routes a demoted slice through the SAME
  `executionPolicy.missingTool` seam a missing `bitbake`/`west` already
  uses — skip by default, fail under `missingTool: "fail"` — naming the
  slice and the host-specific advice (install a toolchain / disambiguate
  `ZEPHYR_SDK_INSTALL_DIR`) in both the text recap and a
  `build.toolchain-root-unresolved` envelope Issue (`warning` on skip,
  `error` on fail); the demoted slice's own `configArtefacts` are stripped
  before materialise ever sees them, so nothing with a live token in its
  path or contents is ever written. `boardYaml` and `sharedArtefacts[]`
  have no owning slice to route a skip to, so they keep the old hard
  failure unchanged. `tan build --materialise` has no per-slice dispatch
  seam either, so it decides once, up front, instead: skip omits just the
  demoted slice's artefacts (with a warning Issue naming it), fail writes
  nothing at all, matching the exit-nonzero/nothing-written shape
  `--materialise` always had. Two notes for anything parsing the envelope
  behind a pinned `SUPPORTED_CLI_VERSION` (alp-sdk-vscode): **`build.
  toolchain-root-unresolved` is no longer only a plan-fatal error** — it can
  now ride an `ok:true` envelope at `warning` severity when the skip policy
  applies, so a consumer must not treat that code alone as an `ok:false`
  signal. And **`substitute_slice`'s field-processing order changed**
  (`env`/`envAppendPath` now precede `command`, where `command` used to come
  between them and `configArtefacts`) — a slice carrying an unresolved token
  in BOTH `env` and `command.args` now reports the `env` field name in
  `LeftoverToken`, not the `command.args[…]` one a consumer may have
  previously seen for that (rare) shape. (#89)
- **`tan debug-config --target-kind native-host` pointed `program` at
  `zephyr.elf`, correcting #83.** #83 fixed the slice SELECTION (native_sim is
  found by board, not by `os`) but then took that slice's `output_artefact`
  verbatim. A manifest never records the host runnable: `resolve_zephyr_artefact`
  (`build/execute/manifest.rs`) is tan's ONLY writer of `output_artefact` — the
  field's "populated by `Orchestrator.fan_out`" lineage is stale, alp-sdk has
  been planner/emit-only since alp-sdk#848 retired `fan_out` — and it stores
  `<slice-cwd>/build/zephyr/zephyr.elf` unconditionally for every zephyr slice,
  native_sim included. There is no `.exe` branch anywhere. So `ALP: Native Sim
  Debug` handed CodeLLDB an ELF it cannot launch: the same failure #83 set out
  to fix, one directory entry over. `tan run` had it right all along
  (`find_native_sim_exe` swaps in the sibling `zephyr.exe`), and the reason the
  two drifted is that each path carried its own idea of the runnable — so the
  swap is now one pure `tan_core::run::native_sim_exe_beside`, called by BOTH.
  #83's test fixtures wrote `output_artefact: …/zephyr.exe`, a manifest tan
  cannot produce, which is exactly why they could not see this; they now write
  `zephyr.elf` and assert the resolved `program` is the sibling `.exe`. Only
  the `native-host` arm transforms — `zephyr-mcu`, `baremetal-mcu` and
  `yocto-userspace` still want their artefact verbatim.
- **`debug-config` emitted its launch configuration with scrambled key
  order.** Dropping the two unresolved `svdFile`/`svdPath` placeholders used
  `serde_json::Map::remove`, which under this workspace's `preserve_order`
  feature is a SWAP-remove: the last two keys were dragged up into the vacated
  slots, so every `zephyr-mcu` profile shipped as `…interface, device,
  servertype` instead of `…servertype, device, interface`. Harmless to a debug
  adapter, but key order matching the TS CLI is this module's stated contract,
  and the new goldens would otherwise have pinned the scrambled form as
  correct. `shift_remove` now.
- **`tan debug-config --target-kind native-host` pointed the debugger at a
  Cortex-M ELF.** The manifest slice was chosen by `os`, and native-host mapped
  to `zephyr` — so on a board that builds a real Zephyr MCU slice as well as a
  native_sim one, the first `os: zephyr` slice won and its `output_artefact`
  overwrote `program`. `ALP: Native Sim Debug` then handed CodeLLDB an ARM
  binary to run on the host. Nothing flagged it: the value is a concrete
  resolved path, so no `<resolved-…>` placeholder survived for a consumer to
  catch, and the extension never sends `--core` for this target, so that pin
  could not disambiguate it either. The native-host slice is now selected by
  the discriminator that already owns the question — `run::native_sim_slice`,
  which matches the bare `native_sim` board and Zephyr's qualified
  `native_sim/…` form — instead of by `os`. A single-native_sim project still
  resolves its real artefact (returning nothing for native-host would have
  regressed it to the draft's hard-coded
  `${workspaceFolder}/build/native_sim/zephyr/zephyr.exe`, wrong whenever the
  build dir is per-slice), and a project with no native_sim slice resolves
  nothing rather than the wrong ELF.
- **`tan sdk list` failed behind an HTTP proxy or a TLS-intercepting
  middlebox.** Two independent causes on the only command that makes an
  **in-process HTTP** request. It called a bare `ureq::get`, and ureq 2.x's
  default agent neither reads `ALL_PROXY`/`HTTPS_PROXY`/`HTTP_PROXY` (that
  needs `AgentBuilder` + `try_proxy_from_env`) nor consults the OS trust store
  — its rustls config trusts only the bundled webpki roots, so a corporate
  middlebox re-signing with a private CA from the Windows/macOS/Linux system
  store failed the handshake outright. Every in-process HTTP call now goes
  through one shared agent that honours the proxy environment (SOCKS included —
  ureq reads `ALL_PROXY` first, so its `socks-proxy` feature is now on;
  without it a `socks5://` tunnel would have hard-failed where it previously
  went direct) and trusts the bundled webpki roots **and** the system store
  (ureq's own `native-certs` feature would have swapped one for the other,
  breaking a host with an empty OS store instead). The agent also caps a whole
  request at 60 s — proxied now, a black-hole proxy would otherwise hang `tan`
  forever, and the extension waits on process exit.
  **Scheme-correct by choice.** Only `ALL_PROXY`/`HTTPS_PROXY` (and their
  lowercase aliases) select the proxy, in that precedence order.
  `HTTP_PROXY`/`http_proxy` are *not* applied to these `https://` requests, even
  though ureq's own `try_proxy_from_env` would apply them regardless of scheme:
  curl, git and Python all treat `HTTP_PROXY` as plain-HTTP-only, and a
  corporate host exporting just that one would otherwise have its GitHub request
  pushed through a proxy that may refuse `CONNECT` — breaking a machine that
  worked going direct. An empty value (`HTTPS_PROXY=`) counts as unset.
  **`NO_PROXY` is honoured**, for the same reason — ureq 2.12 has no support for
  it, and without it a host that sets both `HTTPS_PROXY` and a `NO_PROXY`
  covering GitHub would go from working-direct to proxied. Matching follows
  curl/git/Python: `*` bypasses everything; the list is comma-separated with
  whitespace and empty entries ignored; comparison is case-insensitive; an entry
  matches the host exactly or as a suffix **on a label boundary**, so both
  `github.com` and `.github.com` cover `api.github.com` while `hub.com` covers
  neither; and a `:port` on an entry is ignored (every request here is 443).
  The subprocesses `tan` spawns for network work (`git clone` in
  `tan sdk install`, `pip`/`west update` in `tan bootstrap`) are untouched by
  any of this: they inherit the proxy environment and use their own trust
  stores.
  A handshake or proxy failure — including `tan sdk install`'s `git clone` —
  now names the likely cause, a proxy or an untrusted corporate CA (without
  naming a specific knob — git's `http.sslCAInfo` would be wrong advice on the
  in-process path that shares the sentence), rather than
  surfacing a raw error a user reads as "the network is down"; a proxy that is
  set but unreachable is named too, from the environment, since ureq reports
  that as a plain connect failure that never says "proxy". That sentence names
  `ALL_PROXY`/`HTTPS_PROXY`/`NO_PROXY` and deliberately not `HTTP_PROXY`: both
  paths that reach it are `https://` (the API GET and the `git clone`), neither
  applies `HTTP_PROXY` to those, and a user who followed the advice and edited
  it would see no effect. Only the message text of the `sdk.fetch-failed` /
  `sdk.install-failed` issues gains that sentence; no issue code or `data` field
  changed. Absent proxy environment variables behave exactly as before.
- **`tan sdk switch` left `.west/config` pinned to the old SDK version.**
  The reconciliation that keeps `<topdir>/.west/config`'s `manifest.path` in
  sync already existed for `tan bootstrap` (#31), but `sdk switch` only ever
  rewrote the active-SDK pointer files (`.alp/sdk-path` /
  `~/.alp/sdk-default`) — `west` reads `.west/config` directly and
  independently, so a switch left it naming the OLD checkout, silently, until
  something needed the workspace (`west flash` falling back to an unrelated
  Zephyr tree and failing with `unknown runner`). `sdk switch` now reconciles
  it too, warning (never failing) and naming `tan bootstrap` as the next step
  when it fires, and guards the rewrite on the old target being either a real
  alp-sdk checkout or missing entirely (#62's reported state) — never a real,
  unrelated directory that merely shares the same parent as the SDK just
  switched to. As first shipped this reached only the path form (`tan sdk
  switch /path/to/sdk`): the bare-version form resolved `~/.alp/sdk-cache`
  alone and never got that far for the `~/.alp/sdk` layout that reported it —
  see the version-resolution entry under Changed, which lands in this same
  release. (#62)
- **`tan flash` could not find `west`'s out-of-tree runners.** No spawned
  backend ever set a child `current_dir`, so it inherited whatever directory
  invoked `tan flash`. `west`'s runner registration
  (`run_common.py`'s `zephyr_module.parse_modules(ZEPHYR_BASE,
  command.manifest)`) resolves out-of-tree runners (alp-sdk's `alif_flash`)
  ONLY from the west workspace manifest, discovered by walking the child's own
  cwd upward — never from `tan build`'s `EXTRA_ZEPHYR_MODULES` — so on an
  E1M-AEN801 bench `zephyr_west_flash` died with `FATAL ERROR: unknown runner
  "alif_flash"`. `tan flash` now resolves the same workspace topdir `tan
  build`'s legacy `west alp-*` entry already does and runs every spawned
  child there. The resolver also now refuses a `$ZEPHYR_BASE` whose manifest
  isn't alp-sdk's (a stock/unrelated Zephyr checkout is still a west
  workspace by the bare `.west`-dir test) rather than returning it
  unconditionally — the exact shape that left this fix a no-op on a host with
  such a `$ZEPHYR_BASE` already exported. An app with no workspace above it
  keeps today's inherited-cwd behavior. (#61)
- **`tan debug-config` emitted a launch configuration that could not launch.**
  `device`, `configFiles` and `svdFile` shipped as literal `<resolved-…>`
  placeholders, and `executable` was the fixed `build/app/zephyr/zephyr.elf` —
  wrong for every heterogeneous project. Each value is now resolved from what
  the build itself recorded: the per-core ELF from `system-manifest.yaml`, and
  `device` / `serverpath` / `searchDir` / `configFiles` / `gdbPath` from that
  slice's `runners.yaml` (the same file `west flash` reads), via the new pure
  `tan_core::runners`. `--core <CORE_ID>` picks the slice on a multicore board.
  Unresolved `svdFile`/`svdPath` keys are now dropped rather than left pointing
  nowhere — cortex-debug fails a session on an unreadable SVD, while an absent
  key only costs the peripheral view (no SVD is resolvable until alp-sdk#948).
  The "placeholder fields still need resolution" note is now keyed off what is
  actually left in the draft, and a board that registers no runner for the
  requested server says so instead of leaving the user to guess. (#66)
- **The Renode smoke's CPU halted on an MRAM-linked image.** Renode guesses
  `VectorTableOffset` from the LOWEST `vaddr` it sees. A Zephyr image linked to
  MRAM has a `.data` init segment that RUNS at 0x20000000 but is STORED at
  0x80018348, so the guess pointed at memory nothing was loaded to: SP/PC read
  back as zero and the CPU halted before executing one instruction, while the
  run still exited 0. `tan renode` now derives the real vector-table base from
  the ELF — the load address of the LOAD segment containing the entry point —
  and injects it as `$vtor` ahead of the descriptor include, correct for both
  the MRAM-linked and RAM-run shapes. Containment alone doesn't prove the
  vector table starts where the segment does — an allocated
  `.note.gnu.build-id` (or any offset/padded link) ahead of `_vector_table`
  would satisfy it and still hand back a confident wrong address — so the
  derivation is only trusted once the segment's own second word (the reset
  vector, Thumb bit cleared) matches the entry point too; no match, no
  answer. Inert until the descriptor reads `$vtor` (alp-sdk#947); an
  unreadable or unexpected ELF injects nothing and leaves Renode exactly as
  before.
- **The Renode smoke never actually booted, and reported success anyway.**
  `build_renode_argv` passed `--console --disable-xwt --hide-monitor --plain`;
  Renode 1.16.1 rejects that combination outright — "--hide-monitor and
  --console cannot be set at the same time" — printing its usage page and
  exiting **0**, so `tan renode` reported a clean smoke while nothing was ever
  simulated. `--hide-monitor` was redundant (Renode's own `--disable-xwt` help:
  "It automatically sets HideMonitor") and is gone. A new `renode_rejected_argv`
  guard latches Renode's own refusal wording off the console and fails the run
  (`renode.argv-rejected`, exit 1) regardless of exit status, so the next
  incompatible flag cannot pass silently either.
- **The Renode smoke reported success when the CPU halted on its first
  instruction fetch.** Without `--expect`, `tan renode` had exactly two
  failure signals — a non-zero `natural_exit` and the argv-rejection latch
  above — and neither trips when Renode boots, halts the CPU on its first
  instruction fetch, and shuts down cleanly: the run reported `ok: true` /
  exit 0 while no firmware code ever ran. A new `renode_cpu_halted` predicate
  matches Renode's own two exact console wordings (`CPU was halted` / `PC
  does not lay in memory`), latched in `run_renode` alongside
  `argv_rejected` and checked at the same priority — independently of
  `--expect`/`natural_exit`, since the whole point is catching a run that
  gave neither (`renode.cpu-halted`, exit 1). The `$vtor` injection above
  does not make this redundant: it stays inert until alp-sdk#947 wires
  `cpu VectorTableOffset $vtor` into the `.resc`, so the halt this guards
  against still reproduces today. (#64)
- **`tan flash` could not find the `west` that `tan build` uses.** `west` is
  installed INSIDE the `tan bootstrap` venv, and nothing activates that venv for
  a GUI-launched editor, so the ambient PATH has none. `tan build` has resolved
  the west-capable workspace venv since #106; `tan flash` only ever probed PATH,
  so on such a host a build succeeded and the flash that followed failed every
  Zephyr slice with `flash: slice '<core>' backend 'zephyr_west_flash' needs one
  of ["west"] on PATH; none found.` The venv resolution moved out of
  `commands::build` into a shared `venv` module; `flash` now uses it for the
  required-tool gate, for the backend's argv (the program is spawned by its
  absolute venv path), and for the child's PATH (so nested `west`/`python`
  resolve too). The tool-probing plan builders (`swd_probe`, `yocto_wic`) see
  the venv as well. With no west-capable venv — CI, an activated venv, the
  contract harness — every argv and message stays byte-identical to before.
  (#59)
- **The bootstrap manifest fixture was hand-written, not vendored, and `tan
  bootstrap` silently dropped `manualInstallHints`.**
  `contract/fixtures/bootstrap/manifest.json`'s `_comment` matched no alp-sdk
  commit at all. Re-vendored byte-for-byte from alp-sdk's
  `metadata/bootstrap.json` at `8b216a04` (dev), which had split the old
  `nativeLibHints.windows.note` into a shorter git-bash hint plus a new
  `manualInstallHints.windows.note` (the Arm GNU Toolchain / Zephyr SDK
  manual-install sentence, moved out of the "OPTIONAL NATIVE LIBRARIES"
  heading it was wrongly printed under — alp-sdk#917 review item 7).
  `BootstrapFactsDoc` had no field for the new key, so parsing a real manifest
  silently discarded that sentence while `optional_libs_block`'s Windows
  branch still hardcoded it AND appended the stale `nativeLibHints.windows`
  copy, printing it twice. Added `ManualInstallHint`/`ManualInstallHints`,
  wired them through `parse_bootstrap_manifest` and `BootstrapFacts`, and made
  the Windows branch read `manual_install_hints` instead. `PINNED_SDK_TAG`
  (`.github/workflows/parity.yml`) is now pinned to that same commit, so the
  bootstrap-manifest byte-parity gate actually gates instead of
  NOTICE-and-passing. (#69)
- **`tan bootstrap` printed the Arm GNU Toolchain URL and its PATH tip twice on
  native Windows.** `contract/fixtures/bootstrap/manifest.json` is re-vendored
  byte-for-byte from alp-sdk `0ed078a6` — past alp-sdk#961 (Arm-toolchain
  scoping) and #967 (dtc/gperf settled), which between them rewrote
  `manualInstallHints.windows.note` from one terse sentence into five elements
  and bumped `zephyr.version` to `v4.4.1`. Note element 4 now carries the Arm
  installer URL and the "tick 'Add path to environment variable'" tip verbatim,
  and element 1 carries the Zephyr-SDK `west sdk install` fact together with its
  workspace locator as prose — so `optional_libs_block`'s hardcoded
  Arm/Zephyr-SDK block, kept only for as long as the vendored fixture predated
  #961, became a word-for-word duplicate of the note printed immediately under
  it. Deleted: the Windows arm is now the heading plus the manifest note and
  nothing else, and the function no longer takes a `workspace_dir` (#961 dropped
  the interpolated resolved path upstream as well, so `bootstrap.ps1` prints no
  path there either — tan follows the oracle it mirrors rather than re-adding a
  locator the SDK deliberately replaced with prose). The hand-ported fallback
  constants, `ZEPHYR_VERSION` and `PINNED_SDK_TAG` move with the fixture. Note
  element 3 also retires the deleted heading's "host tools like dtc", which was
  simply wrong on Windows: the Zephyr SDK's native-Windows hosttools bundle
  ships neither `dtc` nor `gperf`. No released SDK loses anything and every one
  gains: `metadata/bootstrap.json` has never existed on alp-sdk `origin/main`
  (absent from its whole history and from `v0.13.0`), so a customer on a release
  takes the fallback-constants path, which this change upgrades to the same five
  elements — picking up the 7-Zip prerequisite, the dtc/gperf correction and the
  Arm-toolchain scoping. The single degraded case is an alp-sdk `dev` checkout
  between #917 and #961, where the manifest exists but still has the
  one-sentence note; it is dev-only and customer-unreachable. (#82)
## [0.3.1] — 2026-07-25

### Added
- **`tan bootstrap` is native Rust on every host; the `bash` dependency is
  gone** — it no longer shells `bash <sdkRoot>/scripts/bootstrap.sh`, and no
  longer refuses on native Windows. The venv → `pip install west` →
  `west init -l` / `west update` / `west zephyr-export` → #769 legibility
  guard → pip-deps flow runs natively on Linux, macOS and Windows, with the
  SDK's `scripts/bootstrap.sh` + `scripts/bootstrap.ps1` as the parity
  oracle for control flow and message strings. Text mode streams the install
  live; JSON mode emits exactly one envelope (#49).
- **Consumes `<sdkRoot>/metadata/bootstrap.json`** (alp-sdk#917) — the SDK's
  single source of truth for the workspace-assembly facts, which names tan a
  required consumer. The Zephyr pin, venv layout, prerequisite lists +
  Python floor, `west` pip spec (`west>=0.14.0`) and argv, pip package sets,
  the `env` map and the per-OS native-lib hints all come from it, with
  `${SDK_ROOT}`/`${WORKSPACE_DIR}` token substitution. An SDK without the
  manifest falls back to documented constants; a manifest with an
  unsupported `schemaVersion` is a hard error, never a silent fallback
  (RFC #843).
- **`tan build --no-auto-bootstrap`** — suppresses the implicit bootstrap a
  text-mode build triggers when no Zephyr workspace resolves. Now that the
  trigger can start a real unattended `west update` on every host (it used
  to refuse instantly on Windows), a build needs a way to say no. Default
  behaviour is unchanged. `tan doctor --build --fix` is explicit opt-in and
  is unaffected.

### Changed
- **BREAKING (wire): `bootstrap` envelope `data.schemaVersion` `"1"` →
  `"2"`.** `scriptPath` is REMOVED — it named
  `<sdkRoot>/scripts/bootstrap.sh`, which this command no longer runs (no
  consumer read it: the VS Code extension runs bootstrap in a terminal, not
  through the envelope, and there is no bootstrap fixture in
  `contract/envelopes/`). Added `workspaceDir`, `venvDir`, `zephyrBase`,
  `factsFromManifest` and `zephyrPin`. `sdkRoot`, `noPip`, `noWest` and
  `printEnv` are unchanged, as is the surrounding
  `{command, ok, exitCode, project, data, issues}` envelope.
- **Non-fatal bootstrap warnings now reach the envelope** as
  `severity: "warning"` issues (`bootstrap.pip-upgrade`,
  `.zephyr-requirements`, `.sdk-extras`, `.editable-install`,
  `.zephyr-base-manifest-mismatch`, `.zephyr-base-incompatible`,
  `.west-config-reconciled`, `.yocto-host`). A JSON run where every
  non-fatal pip step failed used to report `ok: true` with an empty
  `issues` array.
- **One Zephyr pin authority.** The `$ZEPHYR_BASE` workspace-reuse test now
  reads the SDK's own `west.yml` — the same file `doctor --build` /
  `build`'s preflight compares against — instead of a hardcoded value. With
  two sources, an SDK pin bump made bootstrap adopt the very workspace
  preflight called stale, so the auto-bootstrap self-heal never converged.
- **Yocto host gate.** `tan bootstrap` refuses (exit 2) only when EVERY
  in-play core resolves to Yocto on a non-Linux host; a mixed board
  bootstraps normally with a warning, and an unresolvable project always
  runs. The refusal reuses `doctor --build`'s `yoctoHost` wording.

## [0.3.0] — 2026-07-24

### Added
- **Zero-flag default-SDK resolution** — a new machine-global SDK default
  tier sits between the project pin and auto-discovery: `tan sdk switch
  --global` pins `~/.alp/sdk-default` (same shape as the project pin);
  `tan init` now resolves the new project's SDK through the full four-tier
  precedence (`sdkRootFlag` > `projectPin` > `globalDefault` > `discovery`
  > `none`) and pins it into the new project's `.alp/sdk-path` without a
  separate `tan sdk switch` step; `tan sdk current --json` reports which
  tier resolved via the new `sourceTier` field (#32).
- **`tan kconfig`** — board-scoped Kconfig symbol menu for one core (the
  vscode `prj.conf` LSP's live feed), wrapping the SDK's `alp_orchestrate
  --emit kconfig --core <id>` (alp-sdk #894) in the standard
  `Envelope<KconfigData>`. Workspace-dependent — the SDK's one deliberate
  exception to "every emit is hermetic" — so `tan kconfig` resolves
  `ZEPHYR_BASE` via the same workspace/venv resolver `tan build` already
  uses and fails loud (exit 2, `run 'tan bootstrap' first`) when no
  bootstrapped workspace resolves, instead of spawning the emit for a
  cryptic Python failure. `--core` defaults to the board's one declared
  Zephyr core when unambiguous; otherwise it's required, with an error
  naming the board's declared cores (#35).

### Changed
- **Release notes** — `release.yml` now slices the matching `## [X.Y.Z]`
  section out of `CHANGELOG.md` and publishes it as the GitHub Release body
  instead of an empty one (v0.2.0 shipped with no notes) (#30).
- **New golden-envelope contract test** (`crates/tan-cli/tests/
  contract.rs`) pins the JSON envelope shape of the vscode-parsed commands
  (`init`, `generate`, `validate`, `sdk`) across six offline, deterministic
  cases plus the `tan --version` format, so an accidental wire-format
  change fails `cargo test` instead of surfacing as a silent extension
  regression. Test infrastructure only; no change to `tan`'s own runtime
  behavior (#7).
- **Release assets** — the Linux `-gnu` binaries are now cross-built with
  `cargo-zigbuild` against a pinned **glibc 2.31** floor instead of inheriting
  the ubuntu-latest runner's own glibc (2.39, which broke consumers on older
  distros with `GLIBC_2.39 not found`); two new fully-static
  `-unknown-linux-musl` assets (x86_64 + aarch64) ship alongside them for
  Alpine/container consumers and arm64 Linux (no arm runner needed). Every
  release asset, plus a new `checksums.txt`, now carries a GitHub
  build-provenance attestation (`gh attestation verify`) (#6, #20).

### Fixed
- **`tan bootstrap` could silently pull the wrong SDK's west manifest.**
  After `tan sdk switch` between two cached SDK versions sharing a `.west`
  topdir, the "already initialised" path ran `west update` without
  reconciling `.west/config`'s `[manifest] path`, so it kept pulling the
  FIRST SDK's manifest. `tan bootstrap` (unless `--no-west`) now reconciles
  `manifest.path` against the resolved SDK root before shelling
  `bootstrap.sh`, preserving CRLF line endings and rewriting the file
  atomically (#31).
- **`tan kconfig`'s symbol deserialization now requires every key the
  SDK's `--emit kconfig` always emits** (`depends`/`help`/`symbols`)
  instead of silently defaulting a renamed/missing key to empty; the
  vendored `tests/fixtures/kconfig-contract/emit-kconfig.golden.json`
  contract anchor is now byte-diffed against alp-sdk's own canonical
  fixture by `tests/parity/kconfig_fixture_parity.py` in CI (#40).

## [0.2.0] — 2026-07-22

### Added
- **Build-plan token-substitution pass (alp-sdk #865, "hermetic build
  plans").** `tan_core::plan_tokens::substitute_plan_tokens` swaps
  `${SDK_ROOT}`/`${PROJECT_ROOT}`/`${PYTHON}` for tan's already-resolved
  values in every path-bearing plan string, gated on the additive top-level
  `planPathMode: "tokened"` field — a no-op on every plan the SDK emits
  today. Guards: a leftover `${...}` token after substitution fails loudly;
  a `--plan-from` plan's `sdkCommit` is checked against the resolved SDK
  checkout's actual `git` HEAD (the two-SDK split-brain guard); `${PROJECT_ROOT}`
  diverging from the executor's actual base dir refuses the build rather than
  silently building against the wrong tree.
- **`tan init`'s `zephyr-app` template now scaffolds from alp-sdk's vendored
  `--emit scaffold` output** (alp-sdk #864), retiring tan's own hand-rolled
  Rust scaffold generators — which had regressed a cross-core Kconfig leak.
  A cross-repo byte-parity gate (`tests/parity/scaffold_byte_parity.py`) holds
  the vendored `minimal` E1M-AEN801/E1M-V2N101 trees byte-identical to the
  SDK's emit.

### Changed
- **Seam-1 parity twin retuned to shape-only comparison** (alp-sdk #874/#879).
  The vendored comparator no longer diffs each slice's materialised
  config-artefact contents (`alp.conf`/`local.conf`/`cmake-args.txt`/
  sysbuild-conf bytes) against the frozen oracle — only command / env /
  `appDir` / skip-fail-decision shape — so a content-only emitter change no
  longer needs a hand-reviewed comparator strip to stay green. Test/CI
  infrastructure only; no change to `tan`'s own runtime behavior.
- **Seam-1 twin reconciled with alp-sdk #865's tokenized plans**: the
  comparator now maps a live `planPathMode: "tokened"` plan's
  `${SDK_ROOT}`/`${PROJECT_ROOT}` tokens onto the same normalized form the
  frozen (pre-#865, absolute-path) oracle collapses to, instead of diffing
  them as a foreign shape; the frozen `iot-fleet-ota` oracle fixture was
  re-synced to alp-sdk's #862-corrected bytes.

### Fixed
- **Re-vendored the `zephyr-app` scaffold from a corrected `--emit scaffold`**
  (alp-sdk #877): the E1M-V2N101 tree had shipped the non-buildable Alif
  `m55_hp` core (corrected to the Renesas `m33_sm` core) and a bare Zephyr
  board target, now the fully-qualified `board/soc/core` form; the
  `ALP_SDK_ROOT` CMake resolution now hard-errors instead of silently
  falling back to a relative-path guess. `tan init --cores` validation now
  derives the `zephyr-app` template's expected core from the vendored
  scaffold's own ground truth rather than the SKU-prefix heuristic every
  other template uses, fixing a latent E1M-NX9101 core mismatch in the
  process.
- **`tan build`/`flash`/`renode`'s "not built yet" error hints now say
  `tan build --project <path>`** — `build` takes no positional path
  argument, so the previous bare `tan build <path>` hint named an
  invocation clap rejects.

## [0.1.1] — 2026-07-20

A full adversarially-verified codebase review found data-loss and
hardware-programming defects in the 0.1.0 surface; this section is the fix set.
The unifying cause: external file content (`board.yaml`, the build plan, the
system manifest) was parsed leniently — correct for reading — but its unvalidated
strings then flowed into `remove_dir_all`, flash argv, and host-vs-hardware
decisions. Validated *acting* is now separated from tolerant *reading*.

### Security / data-loss
- **`tan clean` could delete the entire project tree.** A `--build-root` of `""`
  (an unset `$VAR`), `.`, or `..`, and a system-manifest slice `build_dir` of
  `""` / `.` / `/` / `../..`, each resolved to the project root or a filesystem
  root and were passed to `remove_dir_all`, exiting `0`. New shared guard
  `tan_core::path_guard::is_unsafe_removal_target` screens **every** removal
  target (build root and manifest-derived alike); a refused target is reported as
  a `clean.unsafe-target` error and fails the command, never silently cleaned.
- **Windows path-escape in the plan/manifest write paths.** The
  `is_absolute() || has ParentDir` guard missed `/x`, `\x` and `C:x` (none are
  `is_absolute()` on Windows, yet each makes `base.join()` discard the base).
  Replaced everywhere with `tan_core::path_guard::is_plain_relative` —
  materialise, the post-build manifest write, slice `cwd`, image archive names
  (`slice_archive_name`), `sdk install <version>`, and `init --name`.
- **`tan flash` could program the wrong address / a stale artefact.** An unquoted
  YAML `base:` that parsed as a number read as *absent* and silently defaulted to
  `0x08000000`; a *skipped* slice kept its plan-time artefact and was flashed
  after a green build. Flash args are now read strictly (a wrong-type scalar
  hard-errors, naming the key), and a slice whose `status != ok` is refused with
  a `flash.slice-not-built` error rather than programmed.
- **Build-root drift could leave `flash` reading a stale manifest.**
  `flash`/`size`/`image`/`renode` each read `<project>/build/system-manifest.yaml`,
  but the native build wrote the manifest under the plan's `buildRoot`. A plan
  emitting `buildRoot != "build"` would write elsewhere while those consumers
  read a stale one still under `build/`. The native build now refuses such a plan
  with a `build.unsupported-build-root` error instead of building where the rest
  of the suite cannot find the result.

### Fixed
- **`tan run --flash` could program hardware on a host project** (and `tan run`
  could execute a stale host binary). Host-vs-hardware is now decided from the
  build that just ran — an in-memory `NativeBuildOutcome` — never by re-reading a
  post-build `system-manifest.yaml` that a best-effort write may have left stale.
- **Silent data loss on every successful build.** The post-build manifest rewrite
  used the typed serializer that drops additive fields (rpmsg IPC carve-outs,
  `hw_info.eeprom`); it now uses the raw round-trip that preserves them.
- **`executionPolicy.unknownBackend` is now enforced** per the consumer contract
  (default fail), and a completion script drift check, JSON-envelope Issues for
  conditions previously reported only in text mode, and numerous smaller
  correctness/cross-platform fixes across `sdk`, `doctor`, `size`, `renode`,
  `image`, `validate`, and `init`.

### Changed
- **MSRV corrected to 1.86** (was declared 1.85). Edition 2024 needs 1.85, but
  the locked `ureq` → `url` → `idna` → `icu_*` tree needs 1.86 — building from
  source on 1.85 already failed. CI now verifies the declared value.
- **CI** — fmt/clippy run once on Linux; build + test now run on Linux, Windows,
  **and** macOS (the platforms a release ships assets for); every cargo call in
  both `ci` and `release` uses `--locked`; in-flight *pull-request* runs are
  superseded on a new push (a push to `main` is never cancelled).

## [0.1.0] — 2026-07-20

First public release of the `tan` executor CLI (alp-sdk ADR-0020 end-state B):
`tan` consumes the alp-sdk *build-plan* and executes it.

### Added
- **Build-plan consumer** — `tan build` runs natively off the SDK's
  `--emit build-plan`: materialise the per-slice files, then run each slice's
  command directly (no `west alp-build` extension command). Consumes the
  contract's `env` / `envAppendPath` and top-level `executionPolicy`, with a
  `schemaVersion` version-skew guard.
- **Native commands** — `clean`, `size`, `image`, `flash`, and `renode` ported
  to native Rust, retiring their `west alp-*` forwarders. `size` reads ELF
  sections directly, so it measures without an external `size` tool.
- **`tan run`** — build, then run on the host (native_sim) or program hardware.
  Flashing hardware requires an explicit `--flash`; a bare `tan run` never
  programs the board.
- **Release pipeline** — a tag-triggered per-platform build publishing raw
  `tan-<triple>[.exe]` binaries for six targets (see `docs/release-contract.md`).
- Post-build **system-manifest** seam (`build/system-manifest.yaml`).

### Changed
- Only `migrate` / `lock` / `quality` still forward to the surviving
  `west alp-*` extension commands; every other build/inspect command is native.

### Fixed
- `zephyr_west_flash`: `flash_args.runner` is now **optional**. When absent,
  `--runner` is omitted and `west flash` defers to the board.cmake default
  runner (e.g. AEN801's `alif_flash`) instead of hard-erroring.

### Removed
- The legacy `tan build --west` delegate.
