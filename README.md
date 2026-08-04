<!-- SPDX-License-Identifier: Apache-2.0 -->
# tan

[![ci](https://github.com/alplabai/tan-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/alplabai/tan-cli/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/alplabai/tan-cli?sort=semver)](https://github.com/alplabai/tan-cli/releases/latest)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

**The standalone Alp Lab build CLI.** The shipping implementation is Python:
`tan` contains the planner and executor, reads board metadata and schemas from an
alp-sdk checkout, and is the user command surface for building, flashing, and
inspecting Alp Lab E1M / E1M-X firmware.

`bootstrap` / `build` / `run` / `size` / `image` / `flash` / `clean` / `renode` /
`monitor` run directly in `tan` — `bootstrap` included, so there is no `bash`
dependency and native Windows is a first-class host. So does the rest of the
surface: `model`, `new-som`, and `faultdecode` are native ports now
(tan-cli#253, #254, #256), not forwards to the SDK's `alp` CLI, and the seven
verbs that used to stub out (`scaffold`, `completion`, `diff`, `pinmux`,
`inspect`, `trace`, `support-bundle`) are real too (tan-cli#260, #257). Only
`migrate` / `lock` / `quality` still forward, to `west alp-*`. Licensed
**Apache-2.0** (see [`LICENSE`](LICENSE); the package metadata and source headers
carry the same identifier).

## Install

**From `v0.5.0`** every version tag publishes one archive per platform (`.zip`
on Windows, `.tar.gz` on Unix) — a PyInstaller `--onedir` freeze, not a raw
binary (tan-cli#349). **`v0.5.0` is not cut yet**: every tag published so far
ships a raw binary instead, including `v0.4.1` (which is what `latest` resolves
to today) and the `v0.5.0-rc4` pre-release. The install scripts read which shape
a release publishes off that release's own `checksums.txt` and install either
one (tan-cli#356), so the commands below work on both sides of that transition —
you do not need to know which tag you are on.

### Automatic (recommended)

The install scripts detect your platform, download the matching binary, and put
`tan` on your PATH. They install **user-local by default — no `sudo`/admin**
(`~/.local/bin` on Unix; `%LOCALAPPDATA%\Programs\tan` + your user PATH on
Windows). Add `--system` / `-System` for a system-wide install (that path needs
elevated permission).

On Unix, if the install dir is not already on PATH, the script appends one line
to your login shell's rc (`~/.zshrc` / `~/.bash_profile` / `~/.profile`) — with a
printed notice, idempotently — so `tan` works in a new shell (this is what makes a
no-sudo install global on macOS, where `~/.local/bin` isn't on the default PATH).
Pass `--no-modify-path` to skip it. On Windows the script already updates your
user PATH.

```sh
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/alplabai/tan-cli/main/install.sh | sh
# system-wide (/usr/local/bin, uses sudo):   curl -fsSL …/install.sh | sh -s -- --system
```

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/alplabai/tan-cli/main/install.ps1 | iex
# system-wide (%ProgramFiles%, run in an elevated PowerShell):   … ; .\install.ps1 -System
```

### Manual

Pick the asset for your host (full table in [`docs/release-contract.md`](docs/release-contract.md)).

**Verify the digest — the scripts refuse to install without it, and so should
you.** Two rules the installers follow and these snippets follow too: pin the
tag ONCE and build both URLs from it (resolving `latest` separately for the
binary and for `checksums.txt` can straddle a release and check one release's
bytes against another's digests — the digest for a given filename really does
move between tags), and do not put the binary in place until it matches.
`tan --version` is not a check: it proves something runs, not that it is what
we published.

Each snippet resolves `latest` **once** into `TAG`/`$Tag` — the same tag the
one-liners above resolve — and downloads into a **fresh directory**, so a failed
fetch can never leave you verifying a previous tag's leftovers and getting a
confident `OK`. Set the variable to an explicit `vX.Y.Z` from the
[releases page](https://github.com/alplabai/tan-cli/releases) to pick a
different one. (`latest` skips pre-releases, so it is not always the highest
version number.)

> **Check the asset name against the tag you picked.** The snippets below are
> written for the archive shape, i.e. `v0.5.0` and later. A pre-`v0.5.0` tag —
> `v0.4.1`, `v0.5.0-rc4`, anything else published so far — names the same triple
> with **no extension** (`.exe` on Windows) and that file **is** the executable:
> drop the `tar -xzf` / `Expand-Archive` step and install the downloaded file
> itself. Either way, `<tag>/checksums.txt` lists exactly what that release
> publishes, which is where the install scripts get the answer (tan-cli#356) —
> so it is also the fastest way to check by hand.

**Linux / macOS**

```sh
# Resolve latest ONCE (or set TAG=vX.Y.Z yourself), same redirect install.sh follows.
TAG=$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
  https://github.com/alplabai/tan-cli/releases/latest | sed 's#.*/tag/##')
ASSET=tan-x86_64-unknown-linux-gnu.tar.gz   # swap for your platform; gnu, not musl -- see docs/release-contract.md's glibc floor (a PyInstaller freeze can't produce a static musl artefact; the floor is measured per-release, published in that release's notes)
BASE=https://github.com/alplabai/tan-cli/releases/download/$TAG

# macOS has shasum, not sha256sum -- pick whichever is present.
SHA=sha256sum; command -v $SHA >/dev/null 2>&1 || SHA="shasum -a 256"

# Chained: a failed fetch stops the sequence instead of verifying a stale file.
d=$(mktemp -d) &&
curl -fsSL -o "$d/$ASSET" "$BASE/$ASSET" &&
curl -fsSL -o "$d/checksums.txt" "$BASE/checksums.txt" &&
line=$(awk -v a="$ASSET" '$2 == a' "$d/checksums.txt") &&
[ -n "$line" ] &&
printf '%s\n' "$line" | (cd "$d" && $SHA -c -) &&
tar -xzf "$d/$ASSET" -C "$d" &&             # unpacks to $d/tan/{tan,_internal/}
chmod +x "$d/tan/tan" &&                    # tar preserves the bit already; cheap insurance
sudo mv "$d/tan" /usr/local/lib/tan-cli &&
sudo ln -sf /usr/local/lib/tan-cli/tan /usr/local/bin/tan &&
tan --version
```

The verify step prints `<asset>: OK`. Every other outcome stops the chain and
installs nothing: a failed download, an asset missing from `checksums.txt` (the
`[ -n "$line" ]` guard — `sha256sum -c` exits **0** on empty input, so piping an
empty match straight into it would pass), and a digest mismatch (`FAILED`).
Failures are silent apart from the tool's own message; run the steps one at a
time if you need to see which stopped it.

**Windows (PowerShell)**

```powershell
# Stop on the first failed fetch, and negotiate TLS 1.2 -- Windows PowerShell 5.1
# still defaults to protocols github.com refuses. Both mirror install.ps1.
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# Resolve latest ONCE (or set $Tag = 'vX.Y.Z'), same API field install.ps1 reads.
$Tag   = (Invoke-RestMethod -Uri 'https://api.github.com/repos/alplabai/tan-cli/releases/latest' -UseBasicParsing).tag_name
$Asset = 'tan-x86_64-pc-windows-msvc.zip'
$Base  = "https://github.com/alplabai/tan-cli/releases/download/$Tag"

# Fresh dir, never the destination: a bad binary written straight to tan.exe has
# already landed, and may already be locked or on PATH.
$d = (New-Item -ItemType Directory -Path (Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid()))).FullName
Invoke-WebRequest -Uri "$Base/$Asset" -OutFile "$d\$Asset" -UseBasicParsing
Invoke-WebRequest -Uri "$Base/checksums.txt" -OutFile "$d\checksums.txt" -UseBasicParsing

# Exact field match, same as install.ps1 -- a substring match would accept a
# neighbouring asset's line.
$want = Get-Content -LiteralPath "$d\checksums.txt" | ForEach-Object {
  $p = $_ -split '\s+', 2
  if ($p.Count -eq 2 -and $p[1].Trim() -eq $Asset) { $p[0].Trim().ToLower() }
} | Select-Object -First 1
$got = (Get-FileHash -LiteralPath "$d\$Asset" -Algorithm SHA256).Hash.ToLower()

# Two different facts, deliberately worded apart: an incomplete release is not
# a tampered download.
if (-not $want) { throw "$Asset is not listed in $Tag's checksums.txt -- the release is incomplete. Nothing installed." }
if ($got -ne $want) { throw "SHA256 MISMATCH for $Asset ($Tag): expected $want, got $got. Nothing installed." }

# Only now unpack it. $Asset is an archive (tan\ containing tan.exe + _internal\),
# not a raw exe -- this is where install.ps1 puts it, minus its launcher script.
$dest = "$env:LOCALAPPDATA\Programs\tan"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Expand-Archive -LiteralPath "$d\$Asset" -DestinationPath $d -Force
Move-Item -LiteralPath "$d\tan" -Destination $dest -Force
& "$dest\tan\tan.exe" --version   # add $dest\tan to your user PATH to run `tan` from a new shell
```

**Stronger, when you have [`gh`](https://cli.github.com/):** every asset —
`checksums.txt` and `envelope-contract.json` included — carries a GitHub
build-provenance attestation.

```sh
gh attestation verify <downloaded-file> --repo alplabai/tan-cli \
  --signer-workflow alplabai/tan-cli/.github/workflows/release.yml
```

Both are documented rather than one, because they answer different questions.
sha256 proves the bytes match what is published beside them and needs nothing
but coreutils (or PowerShell's built-in `Get-FileHash`) — so it is the baseline
every host can run, including one that cannot install `gh`. The attestation
proves the file came out of a GitHub Actions run in this repo; `--signer-workflow`
is what narrows that to the release workflow specifically, rather than any
workflow here. Neither is implied by the other: a digest published in the same
release says nothing about who built it. Run the digest check always; add the
attestation when `gh` is available. Details in
[`docs/release-contract.md`](docs/release-contract.md).

**From source** (Python **3.12+**) — the release assets are PyInstaller freezes
of this same tree (tan-cli#271):

```sh
git clone https://github.com/alplabai/tan-cli && cd tan-cli
python3 -m pip install ./python
tan --version
```

`crates/` (the original Rust implementation, `cargo install --path
crates/tan-cli`) still builds and is still tested by CI, but it is a frozen
reference now — new features land only in `python/`, so building it produces
the stale, v0.4.1-era program under the same `tan` name.

### Package managers

**PyPI — not published.** The distribution name is reserved as `alp-tan`, but
`pip install alp-tan` currently returns 404 because the release workflow has no
PyPI publish job. Install from a checkout with `python3 -m pip install ./python`,
or use a GitHub release asset or installer above.

**crates.io — do not advertise.** `cargo install alp-tan-cli` still resolves
(it worked as of `v0.4.1`), but the `publish · crates.io` job was deleted at
`v0.5.0` — the assets are no longer `cargo` builds, so publishing `alp-tan-cli`
would ship a different program under the same name (docs/release-contract.md).
Installing it today gets you the stale Rust CLI, not the current `tan`.

**npm — does not resolve. Do not use these commands yet.**

> [!WARNING]
> `@alplabai/tan` **does not exist on the npm registry at any version.**
> `npm install -g @alplabai/tan` and `npx @alplabai/tan` both fail with
> `404 Not Found`. The v0.4.1 publish job failed with `npm error code EOTP` —
> the configured `NPM_TOKEN` requires an interactive one-time password, which no
> CI run can supply, so it needs replacing with an npm **automation** token
> ([#233](https://github.com/alplabai/tan-cli/issues/233)). Use a release
> archive above or the installer -- not crates.io (see Package managers
> above: that publish job is deleted too, and now installs the stale Rust CLI).
>
> The commands are recorded here only so the package naming does not change
> under anyone later:
>
> ```sh
> npm install -g @alplabai/tan   # 404 today
> npx @alplabai/tan --version    # 404 today
> ```
>
> The shim downloads the matching platform binary on install (see
> [`npm-shim/`](npm-shim/)); no Rust toolchain needed.

`tan` needs an **alp-sdk checkout** to plan against. Clone one yourself — `git
clone https://github.com/alplabai/alp-sdk` — and point tan at it. It is found,
in order, from `--sdk-root <path>`, the `.alp/sdk-path` pointer (written by `tan
init` for the project it scaffolds), or an `alp-sdk/` directory beside the
project. No VS Code required.

> [!WARNING]
> **`tan sdk install` and `tan sdk switch` do not work in this build.** Both
> exit 1 with `sdk.not-ported`; `tan sdk list` and `tan sdk current` work. They
> are unported rather than broken, and deliberately so for `switch`: it must
> write the active-SDK pointer *and* reconcile `<topdir>/.west/config`'s
> `manifest.path` ([#62](https://github.com/alplabai/tan-cli/issues/62)), and a
> version doing only the first reports success while `west` keeps resolving the
> manifest from the stale pointer. Until they land, `--sdk-root <path>` is the
> mechanism — every command below accepts it
> ([#381](https://github.com/alplabai/tan-cli/issues/381)).

## Quickstart

```sh
# Start in a directory holding an alp-sdk checkout:
#   git clone https://github.com/alplabai/alp-sdk
tan bootstrap --sdk-root ./alp-sdk    # west + Zephyr workspace + Python deps
                                      # (Linux, macOS and native Windows alike)
tan init --name my-app                # defaults to --template zephyr-app
                                      # --som E1M-AEN801
cd my-app                             # sibling ../alp-sdk resolves automatically

tan validate                          # schema + semantic checks on board.yaml
tan build                             # plan → materialise → per-core slice build
tan size                              # footprint vs the SoM memory budget
tan run --flash                       # build, then run (host) or program (hardware)
```

`tan doctor` sanity-checks the host: build readiness (SDK, Zephyr workspace,
west) alongside debug readiness for the selected target/server — the full
check list runs unconditionally. `--build` is accepted for compatibility
(both `alp-sdk-vscode` call sites pass it) and changes nothing. `--fix`
(ADR 0021, tan-cli#91) runs the SDK manifest's own install command for a
`hostPrerequisites` tool this host is missing, but only when the command needs
no elevation (Tier A — `winget`, `brew`, the small POSIX packages); anything
that needs `sudo` is refused and printed verbatim instead, never run — tan
never spawns `sudo` itself, since a password prompt has nowhere to go once
`--format json` has captured stdio, and would hang the process forever rather
than fail. `--fix` only ever acts in an interactive, non-CI, text-mode run:
`--ci`, `--non-interactive`, and `--format json` each disable it on their
own, and so does the same rule applied *unasked* — a piped or redirected
stdin/stderr (an automated run that never thought to pass one of those flags)
disables it exactly as hard, since a repair nobody watched happen is not
consent either way. It never re-checks its own work: this process already
read PATH once at start-up, so an install
landing after that is invisible to it — the honest outcome is "installed;
reopen your shell", not a claimed-verified pass.

`bootstrap` itself runs natively on Linux, macOS and Windows and needs no
`bash`; it only ever *names* the missing prerequisites rather than installing
system packages itself — the executor lives in exactly one place, `doctor
--fix` above, never in `bootstrap` (ADR 0021: "build my project" must never
turn into running installs with no escape hatch). The install commands come
from the SDK's own `metadata/bootstrap.json` (`prerequisites.install`, keyed
per OS), not from a table `tan` carries — so Windows prints the
`winget install` line for a missing `git`/`cmake`/`python`/`ninja`, and the
JSON envelope's `missingPrerequisites[].command` now carries real
`apt-get`/`brew` commands on Linux and macOS where it used to be `null` on
every POSIX host. The *printed* POSIX refusal line is deliberately unchanged —
it stays `bootstrap.sh`'s verbatim, naming the tools and nothing else. An SDK
too old to carry `prerequisites.install` falls back to the same commands, so no
host loses one. The rule across both commands is ADR 0021's: never *require*
copying a command — not "never print one".

Zephyr and baremetal cores build on every host. Only a project whose cores are
*all* Yocto is refused off Linux — a mixed board still bootstraps, with a
warning that the Yocto core itself needs WSL2 or a Linux host.

`west init -l` puts the workspace (`zephyr/`, `modules/`, `.west/`, the venv)
beside the alp-sdk checkout — its PARENT directory. If that parent holds
ANY other entry besides the checkout itself — dotfiles included; a stray
`.DS_Store`/`Thumbs.db`/`.gitignore` counts too, not just an obvious risk like
cloning into `~/Downloads` or `$HOME` — `bootstrap` guards it instead of
spraying multiple gigabytes there unannounced: interactively it offers to
move the checkout into a dedicated `alp-workspace/` sibling; under a
non-interactive stdio (`--non-interactive`/`--ci`/`--format json`, or stdin or
stderr is simply not a terminal — piped, redirected, or a CI runner) it refuses
outright, naming the fix. If a dedicated parent is inconvenient, the one-line
answer is `tan bootstrap --workspace <path>` — no guard, no prompt, workspace
built there. A parent already holding a REAL `.west` workspace (a readable
`.west/config`, not merely an entry named `.west`) is never guarded, and
bootstrap's own venv from an earlier, interrupted run is never counted as
foreign content either.

## Commands

| Area | Commands |
| --- | --- |
| **Project** | `init` · `scaffold` · `examples` · `explain` · `presets` · `pinmux` · `new-som` |
| **Configure & verify** | `validate` · `generate` · `diff` · `inspect` · `trace` · `doctor` · `debug-config` · `support-bundle` · `kconfig` · `faultdecode` |
| **Build & run** (direct) | `build` · `run` · `flash` · `image` · `size` · `clean` · `renode` · `monitor`‡ · `model` |
| **Environment** (direct) | `bootstrap` · `sdk` · `completion` |
| **Forwarders** | `migrate` · `lock` · `quality` → `west alp-*` |

All 32 registered commands run directly in `tan` except the three forwarders
above. `scaffold`, `completion`, `diff`, `pinmux`, `inspect`, `trace`, and
`support-bundle` were stubs that exited 1 with the issue code
`cli.command-deferred` through the earlier RCs; they are ported now
(tan-cli#260, #257). `model`, `new-som`, and `faultdecode` were thin forwards
to `python -m alp_cli`; they are native, in-process implementations now
(tan-cli#253, #254, #256) — `python -m alp_cli` is no longer load-bearing for
any `tan` command.

‡ `monitor` runs entirely in `tan` — it never resolves an alp-sdk checkout,
unlike `model`/`new-som`/`faultdecode` — but needs pyserial, which is an
*optional* dependency (`[project.optional-dependencies] monitor`, not
`dependencies`): `pip install "./python[monitor]"` from a checkout — the extra
is on the local path, since the `alp-tan` distribution that would carry it is
not published anywhere. A
release binary bundles pyserial at build time already. Without it, `tan
monitor` exits with the coded issue `monitor.pyserial-missing` naming the
fix — a binary built without that extra cannot pip-install its way out.

`tan <command> --help` for flags. Every command now parses the oracle's whole
global set (tan-cli#261, one shared `tan/core/global_flags.py`) — none of
`--project`, `--board-yaml`, `--sdk-root`, `--target`, `--all`, `--format`,
`--verbose`, `--quiet`, `--no-color`, `--non-interactive`, `--ci` raises "no
such option" anywhere any more.

**`tan build` is the exception, and it refuses rather than drops** (tan-cli#438
corrected this paragraph, which claimed otherwise). Measured on `tan build`,
each of these exits 1 with the coded issue `cli.command-deferred` naming the
flag and pointing at tan-cli#427:

    --plan  --target <EMIT>  --all  --manifest  --manifest-from <FILE>
    --no-auto-bootstrap  --pristine  --verbose  --quiet  --no-color
    --non-interactive  --ci

That is deliberate: they are real, working flags of the v0.4.1 oracle that this
port does not implement yet, and a DECLARED refusal is the only way to tell
"known but deferred" apart from a typo — an unknown flag is a Click
`UsageError` at exit 2, indistinguishable from `tan bulid`. See
`tan/commands/deferred_cmd.py`'s module docstring.

On every OTHER command a flag with no real use is accepted and dropped, as
below.

| Flag | Effect |
| --- | --- |
| `--project <PATH>` | Project root (default: current directory). |
| `--board-yaml <PATH>` | Explicit `board.yaml`, overriding project resolution. |
| `--sdk-root <PATH>` | alp-sdk checkout to plan against. |
| `--target <EMIT>` / `--all` | Parse on every command now instead of erroring, but the underlying behaviour is still deferred, not silently dropped: `tan build --target …`/`--all` refuses with the coded issue `cli.command-deferred` (tan-cli#427) naming it; every other command accepts and drops both with no effect. |
| `--format json` | Machine-readable envelope instead of text. |
| `--non-interactive` / `--ci` | Refuse to prompt or mutate the host without a human watching (`tan/core/consent.py`): a command with a documented default takes it, one without a default fails naming the missing flag. Applied *unasked* too — the same refusal fires when stdin or stderr is not a terminal (piped, redirected, a CI runner), not only when the flag is passed. `doctor --fix` (tan-cli#91) and `scaffold`'s prompt gate on this for real today; every other command accepts both flags without yet changing behaviour for them — except `tan build`, which refuses both with `cli.command-deferred` (see above). |
| `--quiet` / `--verbose` / `--no-color` | Output volume and styling — on every command except `tan build`, which refuses all three with `cli.command-deferred` (see above). |

`--format json` emits the stable envelope
`{command, ok, exitCode, project, sdk, data, issues}` — the contract the
alp-sdk-vscode extension consumes (`sdk` is optional: present only when the
command actually resolved an alp-sdk root). Text output is for humans and may
change; the envelope is the API.

`tan flash`'s Flow D backend (`alif_mram_jlink`) can auto-sign an Alif
Ensemble slot0 ATOC for you via a SETOOLS install you already have on disk —
SETOOLS is license-gated and obtained directly from Alif, never redistributed
by `tan`. See [`docs/setools.md`](docs/setools.md) for the three ways to
point `tan` at it (`--setools-dir`, `SETOOLS_DIR`, `flash_args.setools_dir`,
in that precedence order) and what it does with it.

## Where it sits (three repos, one CLI)

```
 alp-sdk-vscode  ──shells──►  tan (this repo)  ──reads/drives──►  alp-sdk
 (VS Code ext)                (planner + executor)              (metadata + tools)
```

- **alp-sdk** — board metadata, schemas, examples, libraries, and the `west
  alp-*` commands `tan` still forwards to. Its original Python planner remains
  the parity producer during the port, but the shipping CLI does not import or
  spawn it.
- **tan** — this repo. Its relocated in-process planner (`python/tan/planner/`)
  produces the build plan from SDK data, then its executor runs each per-core
  slice (`west` / `bitbake` / `cmake`). It also owns skip-vs-fail policy, env
  application, scheduling, progress UX, SDK selection, and the manifest read by
  flash/size/image. **What a standalone SDK user installs — no VS Code needed.**
- **alp-sdk-vscode** — a thin extension intended to shell `tan`; as of this
  writing the stable channel remains pinned to the Rust `tan` v0.4.1 line; the
  Python release candidates are opt-in until the extension pin moves.

Dependency direction is one-way: **extension → tan → alp-sdk.** Installing `tan`
never drags in the extension. The user-facing command / binary is `tan`, not
`alp` (RFC #837).

## The seam: the build-plan

The planner/executor seam is the build-plan JSON
(`metadata/schemas/build-plan-v1.schema.json` in alp-sdk). The producer is
`python/tan/planner/`; `python/tan/core/build_plan.py` models and validates the
consumer side. Two guarantees remain load-bearing:

- **Version-skew guard** — `tan` rejects a plan whose `schemaVersion` it doesn't
  support instead of silently falling back to hand-ported behaviour. That silent
  fallback is exactly the drift RFC #843 fixed; skew must not re-introduce it.
- **`env` vs `envAppendPath`** — `env` is set verbatim; `envAppendPath` is
  appended (os.pathsep) *only if not already present*, so a consumer that
  resolves those paths itself is not silently overridden ("plan wins / CLI fills
  gaps").

A build writes `build/system-manifest.yaml` — the post-build IDE/tool contract
(per-core slices, IPC, helper MCUs) that `flash` / `image` / `size` / `renode`
read back.

## Workspace layout

The shipping package lives under `python/`. Pure domain logic is kept in
`tan/core`, command orchestration in `tan/commands`, and the relocated planner in
`tan/planner`:

```
python/
  pyproject.toml               # package metadata + `tan` console script
  tan/
    cli.py                     # Typer command registration + global JSON handling
    commands/                  # command orchestration, IO, subprocess execution
    core/                      # domain logic and wire/data models
    planner/                   # relocated in-process planner
    templates/vendored/        # generated scaffold assets shipped in the package
  tests/                       # unit, conformance, gate, parity, and installer tests
```

`crates/` remains only as the frozen v0.4.1 behaviour oracle. It still builds in
CI while parity is being captured, but release assets and new implementation
work come from `python/`.

## Development

**Features land in `python/`.** That is where the release assets are frozen
from, so that is what a change has to be green in before every push:

```sh
pip install -e ./python              # the DECLARED dependency set, never a
                                     # hand-listed one — a wrong declaration
                                     # must fail here, not on a customer's
                                     # first run
pip install pytest
cd python && python -m pytest tests -q
python scripts/version_check.py --selftest --self   # the three version files agree
```

Zero failures is the gate, not a count — skips and xfails are green, and the
suite grows as ports land. `python/tests/gates/` is where the checks that read
the repo itself live — declared dependencies, issue-code registration, the
planner-relocation audit, and the gate that holds this section and the release
docs to what `release.yml` actually does; `python/tests/parity/` diffs against
the frozen Rust oracle and needs `ALP_SDK_ROOT` (and `TAN_RUST_BINARY`) or it
skips.

The four cargo gates still run in CI (`fmt` + `clippy` once on Linux, `build` +
`test` matrixed across Linux/Windows/macOS, plus an `msrv` job re-checking the
declared `rust-version`, 1.86) and still have to pass — but they verify the
**frozen oracle**, not the shipped program: `crates/` is a v0.4.1-era reference
the parity suite diffs against, and nothing built from it is released.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ./python
.venv/bin/python -m pip install pytest
(cd python && ../.venv/bin/python -m pytest tests -q)
python3 python/scripts/version_check.py --selftest --self
```

House rules: keep files small, put pure logic in `tan/core/` (with unit tests)
rather than in a command module, and never rename an SDK-contract string
(`alp-sdk`, `alp_orchestrate`, `board.yaml`, `alp.conf`, `.alp/…`) — only the
user-facing binary is `tan`.

## Releases

Version-tag pushes (`v<major>.<minor>.<patch>`) freeze one `tan` archive per
platform and publish them as GitHub release assets for the alp-sdk-vscode
downloader. The tag must equal `TAN_VERSION` in `python/tan/version.py` — the
string the shipped binary prints — and `python/pyproject.toml` (its PEP 440
rendering) and `npm-shim/package.json` must agree with it. `release.yml`'s
`verify-version` job runs `python/scripts/version_check.py`, which reconciles
those three and fails the release before a single asset is built. The workspace
`Cargo.toml` is deliberately not part of that check: it versions the frozen Rust
crates on their own cadence, and bumping it does nothing for a release.

Registry publication is not automatic. The crates.io job is deleted, and the npm
shim is published only when the repository variable `TAN_NPM_PUBLISH` is set to
`true` on a final tag — the `release_gate` job then asserts the outcome matched
that declaration, so an armed channel cannot ship nothing quietly. The exact tag
scheme, per-target asset names, and the vscode `releaseAssetForTarget` mapping
are the release-asset contract — see
[`docs/release-contract.md`](docs/release-contract.md).

## References

- alp-sdk **ADR-0020** (the decision this implements)
- **RFC #843** (the drift that motivated it): alplabai/alp-sdk#843
- **RFC #837** (`alp` → `tan` naming): alplabai/alp-sdk#837
