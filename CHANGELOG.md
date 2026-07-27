<!-- SPDX-License-Identifier: Apache-2.0 -->
# Changelog

All notable changes to `tan` are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versioning is
[SemVer](https://semver.org/).

## [Unreleased]

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
