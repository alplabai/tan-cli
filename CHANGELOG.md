<!-- SPDX-License-Identifier: Apache-2.0 -->
# Changelog

All notable changes to `tan` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versioning is
[SemVer](https://semver.org/).

## [Unreleased]

### Added
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
  reason below. (alp-sdk ADR 0021 P0a)
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

### Changed
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

### Fixed
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
  switched to. (#62)
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

### Added
- **`tan renode --core <CORE_ID>`** — boot ONE Zephyr slice of a multicore
  project in the headless smoke. A manifest with more than one Zephyr slice (an
  E1M-AEN801's `m55_hp` + `m55_he`) was refused outright with "the Renode smoke
  boots a single-Zephyr-slice system", leaving no way to smoke-test such a
  project at all. `--core` narrows the zephyr set before the runnable filter, so
  an explicitly named blocked/skipped slice still boots exactly like a lone one
  does (the smoke touches no hardware). A name matching no zephyr slice fails
  with `UnknownCore`, listing the manifest's zephyr cores. The refusal message
  now names the flag. Unchanged for a single-slice project.

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
