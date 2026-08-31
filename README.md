<!-- SPDX-License-Identifier: Apache-2.0 -->
# tan

[![ci](https://github.com/alplabai/tan-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/alplabai/tan-cli/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/alplabai/tan-cli?sort=semver)](https://github.com/alplabai/tan-cli/releases/latest)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

`tan` is the standalone build CLI for Alp Lab E1M and E1M-X projects. It reads
hardware metadata from an [alp-sdk](https://github.com/alplabai/alp-sdk)
checkout, creates build plans, and runs the tools needed to build, inspect,
flash, and debug firmware. VS Code is optional. The implementation is Python.

## Install

### Installer (recommended)

**Prerequisites.** On Linux and macOS the installer needs a downloader --
`curl` **or** `wget`, either one -- plus `tar` and `sha256sum` (macOS:
`shasum`). Nothing else. `tar` and the digest tool are already present on a
stock Debian/Ubuntu and on macOS; a downloader is not. A pristine
`ubuntu:24.04` has neither `curl` nor `wget`, so the command below fails there
with `bash: curl: command not found` until you install one:

```sh
sudo apt-get update && sudo apt-get install -y curl   # or: wget
```

That also pulls `ca-certificates`, which `ubuntu:24.04` does not ship either
and which the download needs. On Windows, `install.ps1` uses only PowerShell
built-ins, so there is nothing to install first.

Linux and macOS:

```sh
curl -fsSL https://raw.githubusercontent.com/alplabai/tan-cli/main/install.sh | sh
```

The same install with `wget`, if that is the downloader the host has:

```sh
wget -qO- https://raw.githubusercontent.com/alplabai/tan-cli/main/install.sh | sh
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/alplabai/tan-cli/main/install.ps1 | iex
```

The installers download the release for your platform, verify its SHA-256
digest, and install it for the current user. Open a new terminal if `tan` is not
immediately on `PATH`.

That prerequisite list is the whole of it, and it is deliberately shorter than
the one a *build* needs: the release asset is a self-contained freeze, so the
installed `tan` runs on a host with no `python3`, no `git` and no compiler --
`tan --version` and `tan doctor` both work there. Building firmware needs more;
see [What a build needs](#what-a-build-needs) below.

For a system-wide install, pass `--system` (Unix) or `-System` (Windows)
through to the script -- piping straight into `sh` or `iex` swallows a bare
`--system`/`-System` before the installer ever sees it:

```sh
curl -fsSL https://raw.githubusercontent.com/alplabai/tan-cli/main/install.sh | sh -s -- --system
```

```powershell
&([scriptblock]::Create((irm https://raw.githubusercontent.com/alplabai/tan-cli/main/install.ps1))) -System
```

See [`docs/release-contract.md`](docs/release-contract.md) for asset names,
manual verification, and OS support.

Every release from v0.5.0 on publishes four archives:

- Windows x64
- Linux x64 with glibc
- macOS x64
- macOS arm64

Linux arm64, Linux musl, and Windows arm64 do not have prebuilt archives.
Install from source on those hosts.

### From source

Python 3.12 or newer is required. Install into a virtual environment, not the
system interpreter: on a PEP 668 host (Debian/Ubuntu, including stock
`ubuntu:24.04`) a bare `python3 -m pip install ./python` refuses with
`error: externally-managed-environment` instead of installing anywhere.
Debian/Ubuntu's `python3` package also does not include `venv` itself --
`python3 -m venv` fails there until `python3-venv` is installed:

```sh
sudo apt-get install -y python3-venv   # Debian/Ubuntu only
git clone https://github.com/alplabai/tan-cli
cd tan-cli
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\Activate.ps1
python3 -m pip install ./python
tan --version
```

For serial monitoring, install the optional dependency from the checkout:

```sh
python3 -m pip install "./python[monitor]"
```

`tan` is not published on PyPI, and `@alplabai/tan` is not currently published
on npm. The `alp-tan-cli` crate on crates.io is a stale v0.4-era Rust CLI, no
longer built from this repository and not the current program. Use a GitHub
release or a source checkout.

### What a build needs

Getting `tan` onto the host and building firmware with it are different sets
of tools, and only the first one is short. A build needs, on `PATH`:

- **Linux:** `git`, `cmake`, `python3`, `ninja`, `xz`, `wget`.
- **macOS:** `git`, `cmake`, `python3`, `ninja`.
- **Windows:** `git`, `cmake`, `python`, `ninja`.

Beyond that list: on Debian/Ubuntu `tan bootstrap` cannot create its workspace
virtual environment until `python3-venv` is installed. On native Windows, `west sdk
install` needs a 7-Zip-compatible archive tool on `PATH` instead -- west
delegates `.7z` extraction to `patoolib`, which shells out to an external
`7z`/`7za`/`7zr`/`7zz`/`7zzs`/`unar` binary and has no pure-Python fallback
(`winget install -e --id 7zip.7zip`). `tan doctor` has a dedicated `sevenZip`
check for this, on every native-Windows host: it warns when none of those
binaries is on `PATH` and names the same `winget` command. It does not wait for
the Zephyr SDK to be missing first — a host that already has the SDK and no
7-Zip is exactly the host whose next `west sdk install` dies with
`Zephyr SDK setup requires '7z'` (tan-cli#736). `tan bootstrap`'s own
cross-toolchain phase (below) checks for the same thing before it ever runs
`west sdk install` itself, and refuses cleanly instead of hitting that error.

Do not assemble any of these lists by hand. `tan doctor` reads its checks from
the SDK's own `metadata/bootstrap.json`, so it stays correct when the SDK
changes it, and it names what is missing on *this* host -- Windows included --
together with the command that fixes it:

```text
bootstrap.prerequisites-missing: missing from PATH: cmake, ninja
doctor.zephyr-sdk: Zephyr SDK toolchain not detected (ZEPHYR_SDK_INSTALL_DIR unset)
                   -- from an initialised west workspace, run `west sdk install`
doctor.west-resolved: west resolved neither through the workspace venv nor PATH
                   -- no build slice can be executed. Run `tan bootstrap`
```

`tan doctor` needs none of those tools itself, so run it first, on the bare
host, rather than guessing -- this holds on Windows exactly as it does on
Linux and macOS.

## Quickstart

Start in an empty working directory:

```sh
git clone https://github.com/alplabai/alp-sdk
tan bootstrap --sdk-root ./alp-sdk
tan init --name my-app
cd my-app

tan validate
tan build
tan size
tan run --flash --confirm
```

What those commands do:

1. `bootstrap` prepares west, Zephyr, the Python environment and SDK
   dependencies into a workspace venv at `alp-workspace/.venv` (next to the
   SDK checkout by default) -- and, as its final phase (ADR 0021 Lane 1 P1),
   acquires the Zephyr SDK cross-toolchain (`arm-zephyr-eabi`) that
   `tan build` needs for real silicon, into the artifact-keyed store
   `~/.alp/toolchains/zephyr-sdk-<version>-arm-zephyr-eabi/` (or
   `$ALP_TOOLCHAIN_ROOT`, shared across every project pinning that same
   version). It reads the version to install from the SDK checkout's own
   `metadata/toolchains.json`, so a pin bump there reaches you on your next
   `tan bootstrap` with no `tan` upgrade needed. A second `tan bootstrap`
   against the same pin is near-instant: it probes the existing install's
   compiler and re-verifies its stamp rather than reinstalling. Pass
   `--no-toolchain` to skip this phase (the rest of `bootstrap` is
   unaffected, and `native_sim` builds never need a cross toolchain at all).
   On a minimal Linux host this phase also needs `file` on PATH
   (Debian/Ubuntu: `sudo apt-get install -y file`); without it the underlying
   `west sdk install`'s own host-tools step fails with "Host tools installation failed" and names nothing.
2. If you skip the toolchain phase, or need to point at a different pin, run
   `west sdk install` by hand from inside the workspace venv:

   ```sh
   source alp-workspace/.venv/bin/activate    # Windows: alp-workspace\.venv\Scripts\Activate.ps1
   export ZEPHYR_BASE="$PWD/alp-workspace/zephyr"
   west sdk install --version 1.0.1 -t arm-zephyr-eabi
   ```

   the exact command `tan doctor`'s `zephyrSdk` check also names, so it
   stays correct if that pin ever moves. On a minimal Linux host this also
   needs `file` on PATH (Debian/Ubuntu: `sudo apt-get install -y file`);
   without it the SDK's own host-tools step fails with "Host tools installation failed" and names nothing. alp-sdk's `metadata/bootstrap.json`
   (`manualInstallHints.posix.note[2]`) calls a missing `file` "WARN-only, not
   a bootstrap.sh prerequisite", and both statements are true: that note is
   written for the `--no-hosttools` invocation in its own `note[0]`, which
   never runs the host-tools step. The command above installs host tools, so
   it needs `file`. Add `--no-hosttools` and it does not.
3. `init` creates a Zephyr application and pins the SDK checkout in
   `.alp/sdk-path`.
4. `validate` checks `board.yaml` and related metadata.
5. `build` plans, materialises, and builds every core slice.
6. `size` reports firmware use against the SoM memory budget.
7. `run --flash` builds and then runs or programs the selected target. On a
   hardware target (a native_sim/host target always just runs), `--flash`
   alone only *previews* the write: every slice comes back `planned`, nothing
   reaches the device, and the run exits non-zero naming the remedy. Add
   `--confirm` (as above) to actually arm the write, or set
   `ALP_FLASH_FORCE=1` in the environment, or `flash_args.confirm: true` in
   the manifest -- the same three-way gate `tan flash --confirm` already has.
   This is deliberate, not a bug: a fresh checkout must not silently
   reprogram an attached module.

Run `tan doctor` if setup or toolchain discovery fails; its `toolchain` check
reports stamp-vs-pin for the resolved project (a version-skewed or
never-verified install is a `fail` naming `tan bootstrap`, never a silent
"looks present"), and its `zephyrSdk` check answers the broader "does any
toolchain exist on this host at all" question, unconditionally. `tan doctor
--fix` installs missing prerequisites, but only at a real, interactive
terminal -- it is a no-op (exit 4) under a pipe, a redirect, or CI, so it is
not a scripted-onboarding remedy. It never spawns `sudo` itself: it runs a
prerequisite's manifest install command directly when already root, and
otherwise prints the exact command to run by hand.

If you do not want the west workspace next to the SDK checkout, choose it
explicitly:

```sh
tan bootstrap --sdk-root ./alp-sdk --workspace /path/to/alp-workspace
```

`--workspace` does not simply relocate where the workspace metadata is
written: the west topdir is always the checkout's parent, so this **moves**
the `alp-sdk` checkout itself to `/path/to/alp-workspace/alp-sdk` and updates
the machine-global `~/.alp/sdk-default` pointer to it. Run this before
anything else that references `--sdk-root ./alp-sdk` by its old path, or
those calls stop resolving; if a project's `.alp/sdk-path` already pins the
old location, re-run `tan init`/`tan bootstrap` from that project after the
move.

## Common commands

| Task | Command |
| --- | --- |
| Create a project | `tan init --name my-app` |
| Check a project | `tan validate` |
| Build firmware | `tan build` |
| Build and run or flash | `tan run --flash --confirm` (`--confirm` arms the write on a hardware target; see the quickstart) |
| Flash an existing build | `tan flash --confirm` |
| Inspect firmware size | `tan size` |
| Create an image | `tan image` |
| Remove build output | `tan clean` |
| Generate configuration files | `tan generate` |
| Check the host setup | `tan doctor` |
| Start a serial monitor | `tan monitor` |
| Generate debugger settings | `tan debug-config` |
| List examples and presets | `tan examples`, `tan presets` |
| Explain resolved project settings | `tan inspect` |
| Explain a template or generation target | `tan explain` |
| Show help | `tan <command> --help` |

When a project's `build/system-manifest.yaml` names more than one debug
target class (e.g. a `yocto` A-cluster slice beside a `zephyr` M-core slice),
`debug-config` exits 2 with `debug-config.target-kind-ambiguous`; pass
`--target-kind <zephyr-mcu|baremetal-mcu|yocto-userspace|native-host>`, or
`--core <core_id>` to narrow to one slice. A multi-core SoM whose cores all
share one target class (e.g. two Zephyr cores) is unaffected -- the classes
fold to one and no flag is needed.

The full command surface also includes `scaffold`, `completion`, `diff`,
`pinmux`, `inspect`, `trace`, `support-bundle`, `kconfig`, `faultdecode`,
`model`, and `new-som`. `migrate`, `lock`, and `quality` forward to their
corresponding `west alp-*` commands: `migrate` requires `--check`,
`--preview`, or `--apply`; `quality` requires `--profile`. The other
commands run directly in `tan`.

For Alif Ensemble MRAM flashing with SETOOLS, see
[`docs/setools.md`](docs/setools.md).

## Choosing an SDK

Most project commands find alp-sdk in this order:

1. `--sdk-root <path>`
2. the project's `.alp/sdk-path` pin
3. the user's default SDK pointer
4. a nearby `alp-sdk` checkout

Step 3 is one pointer shared across every project on the host and is
last-writer-wins: another project's `tan bootstrap` can repoint it. `tan`
warns (`sdk.global-default-foreign-project`) rather than resolving silently;
use `--sdk-root` or the project pin to be explicit.

Use `--sdk-root` when more than one checkout is nearby or when you want a
one-off override:

```sh
tan build --sdk-root /path/to/alp-sdk
```

`tan sdk list` and `tan sdk current` work today. `tan sdk install` and
`tan sdk switch` are not implemented yet, so clone the SDK yourself and use
`--sdk-root` or let `tan init` write the project pin.

## Automation and JSON output

Add `--format json` for machine-readable output:

```sh
tan build --format json
```

The stable top-level envelope is:

```text
{command, ok, exitCode, project, data, issues}
```

plus an optional `sdk` object, present only on commands that resolved an
alp-sdk checkout -- absent, never null, when none was resolved.

**You do not need a flag for this.** tan already treats a run as
non-interactive when `stdin` or `stderr` is not a terminal — piped, redirected,
or a CI runner — and `--format json` settles it on its own, whatever the
terminal looks like. That rule is applied unasked (`tan/core/consent.py`'s
`can_prompt`, whose `not json_mode` is an unconditional term). It reads `stdin`
and `stderr` and deliberately never `stdout` — which matters more than it
sounds, because in text mode nothing is written to stdout at all
(`tan/env.py`). `stderr` is therefore both the report channel and half the
consent gate: `tan doctor --fix 2> log.txt` from a real terminal is treated as
unattended and the fix is suppressed (`doctor.fix-suppressed`), while
`> log.txt` leaves consent untouched and captures an empty file. Where a
command has a documented default it takes it; where it has none it fails rather
than asking.

`--ci` and `--non-interactive` exist as explicit "do not ask me" signals on top
of that, and **every registered command parses them**:
`tan/core/global_flags.py` holds the shared spec and injects it into any
command that does not already declare the flag itself. What keeps that true is
split in two. `tests/gates/test_global_flags_gate.py` fails the build for the
29 commands that reject an unknown option; it cannot speak for `lock`,
`migrate` and `quality`, which register `ignore_unknown_options`
(`west_forward_cmd.py`) and would swallow an undeclared flag into the `west`
passthrough rather than reject it — those three are held by
`test_west_forward_command.py`'s
`test_a_leading_global_flag_is_consumed_not_forwarded`. They are not ROOT
options either: a leading one is relocated across the subcommand boundary, so
`tan --ci doctor` and `tan doctor --ci` are the same run, while a bare
`tan --ci` with no subcommand is `No such option: --ci`.

`tan build` parses all ten of the shared flags, but only ONE of them still
refuses the invocation (tan-cli#427): `--no-auto-bootstrap`, because this port
has no implicit "run `tan bootstrap` on a missing Zephyr workspace" trigger
yet for the flag to disable in the first place (accepting it ahead of that
trigger existing would be its own silent no-op). The other seven —
`--target`, `--all`, `--verbose`, `--quiet`, `--no-color`,
`--non-interactive`, `--ci` — are accepted and dropped, the SAME
`accept_global_flags` mechanism 17 other commands already use: the oracle's
own `cli.rs` declares them ONLY on the shared `GlobalArgs` struct and
`build`'s Rust handler never read them either, so this is not a narrower
stand-in for refusing them, it is the identical oracle behaviour. `--plan`,
`--manifest` and `--manifest-from` are a third bucket, RETIRED rather than
deferred or accepted: each still parses but is refused with
`build.flag-retired` (exit 2), naming its replacement (`--plan-from`,
`--materialise`/`--execute`) in the message itself. `--project`,
`--board-yaml`, `--sdk-root`, `--plan-from`, `--materialise`, `--native`,
`--execute`, `--build-root`, `--format` and `--pristine` are unaffected by any
of the three buckets:

```console
$ tan build --ci --plan-from plan.json --format json
{"command":"build","ok":true,"exitCode":0,"project":{...},
 "data":{"schemaVersion":1,...},"issues":[]}
$ tan build --no-auto-bootstrap --format json
{"command":"build","ok":false,"exitCode":2,...,"issues":[{"code":"build.flag-retired",
 "severity":"error","message":"`--no-auto-bootstrap` is retired: `tan build` never
 bootstraps implicitly, so there is nothing for it to disable — run `tan bootstrap`
 yourself when a workspace needs preparing."}]}
```

So a script that adds `--ci` to every tan invocation no longer breaks on
`build`. The one flag in the shared set that still refuses names itself and
says what to run instead: `--no-auto-bootstrap` is retired rather than
pending (tan-cli#427), because `tan build` never bootstraps implicitly.
Elsewhere `--ci` does three separate things, and only the first is consent:

* **Consent**, on the two commands that read the flag for it — `doctor --fix`
  (`doctor_cmd.py`'s `fix_allowed`) and `scaffold` (`scaffold_cmd.py`'s
  `interactive`), both through `can_prompt`. This is the half worth reaching
  for: it is what stops `tan doctor --fix` running unattended
  `winget install`s under a pty-allocating runner. It is NOT the whole
  prompting surface, though — `new-som` prompts too, gates on stdio alone
  (`stdin_is_tty() and stderr_is_tty()`, `new_som_cmd.py`) and `del`s both
  flags unread, so under that same pty-allocating runner `tan new-som --ci`
  blocks on `New SoM SKU (E1M-<UPPERCASE> shaped)` forever. Pass its required
  flags there instead.
* **Colour**, which has nothing to do with prompting. Inside `tan/env.py`'s
  `use_color` (`if no_color or ci or no_color_requested()`) `--ci` is a second
  spelling of `--no-color`, so on `doctor` and `size` — that helper's only two
  callers — it changes text output where no prompt was ever possible. It is not
  a synonym outside it: `faultdecode` colours through its own
  `_use_color(no_color)` and ignores `--ci`. `--non-interactive` changes colour
  nowhere.
* **The refusal message.** Where stdio is already non-tty the rule above has
  decided first, but `--ci` still names itself in `doctor`'s
  `doctor.fix-suppressed` reason list — an `issues[]` difference a JSON
  consumer sees.

Rely on the stdio rule for the consent half; reach for `--ci` on `doctor` and
`scaffold` when you want that refusal regardless of what stdio looks like —
`build` accepts and drops `--ci` (and `--verbose`, `--quiet`, `--no-color`,
`--target`, `--all`, `--non-interactive`) identically, doing nothing with any
of them.

## How tan fits with the SDK

```text
alp-sdk-vscode  ->  tan  ->  alp-sdk
VS Code UI          CLI      metadata, schemas, examples, west extensions
```

- `alp-sdk`: hardware metadata, schemas, examples, and the remaining
  `west alp-*` extensions.
- `tan`: SDK selection, planning, build execution, and the manifest that
  `flash`/`size`/`image` read.
- `alp-sdk-vscode`: an optional UI that invokes `tan`.

A successful build writes `build/system-manifest.yaml`, which records the
per-core artifacts and is reused by downstream commands.

## Development

New implementation work belongs under `python/`:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e "./python[monitor]"
.venv/bin/python -m pip install pytest
(cd python && ../.venv/bin/python -m pytest tests -q)
python3 python/scripts/version_check.py --selftest --self
```

**Always install into a venv you create — never a bare `pip install -e ./python`
or `pip install --user -e ./python`.** Run without an active venv, that writes
an editable install into your OS user site-packages, and from that moment
every bare `python3` process on the machine resolves `import tan` to
whichever checkout was installed last, regardless of which worktree it is
actually running in — including another developer's or another agent's
checkout, on a shared box (tan-cli#665). It costs nothing to notice while it
is happening: `tan --version` and `which tan` keep answering normally, so a
full `pytest tests -q` run can report hundreds of misleading failures (or,
worse, a false green) with no other symptom. `tests/conftest.py`'s
`tan_under_test` fixture refuses loudly at session start if `import tan`
resolves to anything outside this checkout's own `python/` — that is the
backstop, not a substitute for using a venv in the first place.

That run means the same thing on every machine, including a bench host with
real debug tooling installed. The suite neutralises the debug/flash probe
identities — `JLinkExe`, `openocd`, `pyocd`, `west` and friends —
for its own duration (`python/tests/conftest.py`, `PROBE_TOOLS`), so a
which()-gated branch answers the way it answers on a CI runner rather than the
way this host happens to be provisioned. Before that (tan-cli#603) seven
`test_flow_d_preflight_*` cases passed locally and failed on ubuntu, windows
and macos at once. Ordinary host tooling — `git`, `python3`, `sleep`, the
coreutils the installer-script tests execute — is left alone. A test that
needs a probe tool present seeds its own and points `PATH` at it, which is
what makes the inventory readable in the test.

Useful directories:

```text
python/tan/commands/   command orchestration, filesystem and subprocess work
python/tan/core/       domain logic and wire models
python/tan/planner/    in-process planner, mirrored from alp-sdk
python/tan/templates/  project templates included in the package
python/tests/          unit, conformance, parity, and repository gates
contract/              the JSON envelope goldens shared with alp-sdk-vscode
```

Release assets are PyInstaller freezes of `python/`. The Rust implementation
this program was ported from is deleted; the behaviour it was measured against
survives as the frozen captures under `python/tests/fixtures/oracle_captures/`.

## More documentation

- [Release assets and verification](docs/release-contract.md)
- [SETOOLS setup](docs/setools.md)
- [Development roadmap](docs/ROADMAP.md)
- [Changelog](CHANGELOG.md)
- [License](LICENSE)

`tan` is licensed under Apache-2.0.
