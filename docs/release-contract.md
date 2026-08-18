<!-- SPDX-License-Identifier: Apache-2.0 -->
# tan release-asset contract

The `release` workflow (`.github/workflows/release.yml`) builds the `tan` binary
for each supported platform on a version-tag push and publishes them as GitHub
release assets. The **alp-sdk-vscode** extension downloads the matching asset on
activation, so the tag scheme and asset names are a **stable contract** — change
them only in lockstep with the extension's `releaseAssetForTarget`.

> **From v0.5.0 the assets are PyInstaller freezes of `python/`** (the Python
> port), not `cargo` builds of `crates/` — tan-cli#271. The asset NAMES keep
> the Rust target triples, because the extension hardcodes them. Four assets
> ship, not eight, and the crates.io publish is gone. **From v0.5.0 each asset
> is also an ARCHIVE (`.zip` / `.tar.gz`) of a PyInstaller `--onedir` freeze,
> not a raw binary** — tan-cli#349, see "Asset names" below for why. Everything
> below is written for that release; where it describes the retired Rust
> pipeline it says so explicitly.
>
> **v0.5.0 was the transition tag; it and v0.5.1 are both cut, and `latest`
> resolves to v0.5.1.** Every tag published BEFORE v0.5.0 — `v0.4.1` and
> `v0.5.0-rc4` included — shipped a RAW binary. rc4 carried the `--onefile`
> freeze as a raw asset, which is what the 13–19 s macOS measurement below was
> taken on; do not read "`--onedir`" or "archive" as something rc4 shipped,
> because it shipped neither. From v0.5.0 onward, every published tag ships
> the archive shape described below. Both installers support **both** shapes
> and decide per release (tan-cli#356) — see
> [Which shape a release publishes](#which-shape-a-release-publishes).

## Tag scheme

```
v<major>.<minor>.<patch>              e.g. v0.1.0     release
v<major>.<minor>.<patch>-<pre>        e.g. v0.4.0-rc1 pre-release
```

- SemVer, `v`-prefixed.
- The tag (minus the `v`) MUST equal `TAN_VERSION` in `python/tan/version.py`.
  That is the string the shipped binary PRINTS and the one alp-sdk-vscode
  compares against its `SUPPORTED_CLI_VERSION`, which is why it — and not any
  other file — is the source of truth. This holds for a pre-release too:
  `v0.4.0-rc1` requires `TAN_VERSION = "0.4.0-rc1"`.
- Two more files are reconciled against it in the same gate, and a release
  engineer has to move all three together:
  - `python/pyproject.toml`'s `version` must be the **PEP 440 rendering** of it
    (`0.5.0-dev` → `0.5.0.dev0`). The two spellings are not string-equal, which
    is exactly why the check is a script and not a `grep`.
  - `npm-shim/package.json`'s `version` must equal `TAN_VERSION` **exactly**
    (npm ships SemVer, so nothing is translated here): `postinstall.js` derives
    its download tag as `` v${pkg.version} ``, so a stale shim fetches a tag
    that does not exist.
  - `CHANGELOG.md` must already carry the `## [<version>]` section the release
    body is sliced from — checked here, at PR time, because `release.yml` only
    discovers it missing after four freezes, under a tag that is already
    immutable.
- **`Cargo.toml` is not read, and since tan-cli#269 does not exist.** It
  versioned the Rust crates on their own cadence and no release asset came from
  them. It used to be the gate (`grep -m1 '^version = ' Cargo.toml`), and that
  is precisely how a correct `v0.5.0` tag failed before a single asset was
  built. Recorded so a future reader does not go looking for a fourth version
  file on the theory that one was overlooked.
- The whole reconciliation lives in `python/scripts/version_check.py`, which
  `release.yml`'s `verify-version` job runs as
  `python python/scripts/version_check.py --selftest --tag "$GITHUB_REF_NAME"`.
  `ci.yml` runs the same script with `--self` on every push, so drift is a PR
  failure rather than a re-tag.

### Pre-releases

**The hyphen is the whole signal.** `release.yml` derives both flags from it, so
they cannot disagree with each other or with the tag:

| Tag | `prerelease` | `make_latest` |
| --- | --- | --- |
| `v0.4.0` | `false` | `true` |
| `v0.4.0-rc1` | `true` | `false` |

`publish_crates` is deleted for every tag (the assets never came from the
crates any more, and `crates/` itself is gone as of tan-cli#269, so publishing
`alp-tan-cli` would ship a different program under the same name). `publish_npm` is a **real job** and runs only on a FINAL tag — its
own `if` is `startsWith(github.ref, 'refs/tags/') && !contains(github.ref_name,
'-')`, so a pre-release skips it entirely, the same way it skips `make_latest`.
Even on a final tag the publish itself is **opt-in and off**: the job reads
`NPM_PUBLISH_ENABLED: ${{ vars.TAN_NPM_PUBLISH == 'true' }}` and, unless that
repository *variable* is `true`, records `published=false` and says so in the
run summary — a loud no-op rather than a silent skip. Arming it needs the
variable AND a replacement `NPM_TOKEN`: the configured one is a classic token on
a 2FA account, so `npm publish` answers `EOTP`. See
[`npm-shim/README.md`](../npm-shim/README.md) for the operator's half.

This is load-bearing rather than cosmetic. Both [`install.sh`](../install.sh)
and [`install.ps1`](../install.ps1) resolve what `latest` means through GitHub,
and GitHub excludes a release from `latest` **only when it is marked
`prerelease`**. So an unflagged rc becomes `latest` and is handed to every
customer running the documented install command.

The two scripts ask two different endpoints -- `install.sh` follows the
`/releases/latest` redirect, `install.ps1` reads the API's `tag_name`, each
being the mechanism that is actually robust on its host (see the comment in
either script). Both honour the same `prerelease` flag, so they agree: with
v0.4.0 marked prerelease, both resolve `latest` to **v0.3.1** rather than to the
higher version number.

Neither fetches `releases/latest/download/<asset>` any more. They pin the
resolved tag first and build **both** the binary URL and the `checksums.txt`
URL from it (#176), because resolving `latest` twice is not the same as
resolving it once: a release cut between the two fetches would verify one
release's asset against another release's digests, which yields a wrong verdict
rather than an error. The digest for a given filename genuinely moves between
tags -- `tan-x86_64-pc-windows-msvc.exe` is `f159c1dc…` at `v0.4.0-rc1` and
`a80fb5da…` at `v0.4.0` -- so anything that caches or hardcodes a digest is
wrong by construction.

Note that `tan sdk list` flags **alp-sdk** draft/pre-releases (tan-cli#122) —
that is a different release stream from tan's own, and neither protects the
other.

## Asset names

**From v0.5.0 (tan-cli#349), one ARCHIVE per target triple** — up to and
including `v0.5.0-rc4` it is one raw, uncompressed binary:

```
tan-<target-triple>.tar.gz     # Unix,    v0.5.0 and later
tan-<target-triple>.zip        # Windows, v0.5.0 and later
tan-<target-triple>            # Unix,    v0.5.0-rc4 and earlier
tan-<target-triple>.exe        # Windows, v0.5.0-rc4 and earlier
```

Why an archive now: the assets are PyInstaller `--onedir` freezes, not
`--onefile`. `--onefile` re-extracts its whole ~14 MB runtime into a fresh temp
dir on EVERY invocation — measured 13–19 s for `--version` on the published
v0.5.0-rc4 macOS `--onefile` asset (unsigned re-extracted `.dylib`s get
re-verified by the OS on every load), which **exceeds alp-sdk-vscode's own 3 s
version-probe budget** (`vscodeAdapter.ts:1406`) — that asset's `--version`
TIMED OUT under the extension's own probe, not merely "slow". `--onedir`
extracts once, at install time, instead of once per invocation: measured
0.337 s mean vs 0.880 s mean for `--version` on this same host. The archive is
the one-file-per-target shape that lets `checksums.txt` / the provenance
attestation / `install.sh` / `install.ps1` keep dealing with a single thing per
target even though the payload is now a directory (`tan` + `_internal/`), not
a single file — both installers unpack it and install a thin launcher rather
than the executable itself.

Download URL is fully deterministic:

```
https://github.com/alplabai/tan-cli/releases/download/<tag>/<asset>
```

Plus two non-binary assets, carrying the same build-provenance attestation:

| Asset | Contents |
| --- | --- |
| `checksums.txt` | sha256 of every other asset. |
| `envelope-contract.json` | The JSON envelope contract — the frozen issue codes (`contract/issue-codes.json`) plus one entry per command family: a byte golden envelope (`contract/envelopes/`) for most of them, and `doctor`'s `dataKeys` key-set entry (`contract/doctor-data-keys.json`, tan-cli#664 — `doctor`'s `data` values are host facts, so it cannot be a golden) — so a consumer's contract test diffs against a published artefact instead of a hand-copied fixture that drifts. See [`contract/README.md`](../contract/README.md). |

## Which shape a release publishes

Two shapes exist in the wild and both are supported for as long as the raw tags
are installable, so **a consumer must not assume either one**. tan-cli#356 is
what that costs when you do: #349 pointed both installers at the archive names
unconditionally, and since no published tag has them, `sh install.sh` —
the documented command, with no arguments — 404'd on
`tan-x86_64-unknown-linux-gnu.tar.gz` at `v0.4.1`.

`checksums.txt` is the answer. It lists **every** asset in the release, it is
published at every tag, and it is the integrity source a consumer has to fetch
anyway — so it doubles as the asset manifest at zero extra cost. Both
installers now fetch it FIRST, take `tan-<triple>.tar.gz` / `.zip` if it is
listed there and the raw `tan-<triple>` / `.exe` if it is not, and then verify
the download against the digest out of that same file **before** unpacking
anything or writing to the install directory.

Deliberately **not** a version comparison against `v0.5.0`. That is a second
source of truth about the release, kept somewhere the release cannot update,
and it has to model SemVer pre-release ordering correctly (`v0.5.0-rc4` sorts
BELOW `v0.5.0`) in POSIX `sh` and in PowerShell, in agreement, forever. It is
also **not** a magic-number sniff of the downloaded bytes, even though that is
#349's own rule on the alp-sdk-vscode side: the extension holds a file it has
already fetched, whereas an installer has to choose a NAME before there are any
bytes to sniff.

An installer that finds neither name refuses and says so, naming both — with
`checksums.txt` doubling as the manifest, "this platform has no asset in this
release" and "the release shipped an asset and forgot to check-sum it" arrive
through the same door, and only the release page can tell them apart.

## Targets published

**Four** assets, one per build runner:

| VS Code `process.platform` | `process.arch` | Target triple               | Asset name                           | Built on        |
| -------------------------- | -------------- | --------------------------- | ------------------------------------- | --------------- |
| `win32`                    | `x64`          | `x86_64-pc-windows-msvc`    | `tan-x86_64-pc-windows-msvc.zip`     | `windows-latest` |
| `darwin`                   | `x64`          | `x86_64-apple-darwin`       | `tan-x86_64-apple-darwin.tar.gz`     | `macos-15-intel` |
| `darwin`                   | `arm64`        | `aarch64-apple-darwin`      | `tan-aarch64-apple-darwin.tar.gz`    | `macos-15`      |
| `linux`                    | `x64`          | `x86_64-unknown-linux-gnu`  | `tan-x86_64-unknown-linux-gnu.tar.gz` | `ubuntu-latest` + `python:3.12-slim-bullseye` |

Each archive's one top-level entry is `tan/`, containing `tan` (`tan.exe` on
Windows) plus `_internal/` (its runtime) — `install.sh` / `install.ps1` unpack
it to a private `tan-cli-lib/` directory and install a thin launcher script
alongside it rather than the executable itself. A consumer not using either
installer must unpack the archive themselves and (on Unix) `chmod +x` the
`tan` executable inside it — the archive does not require this itself
(`tar`/`zip` both preserve the executable bit that `build_binary.sh` sets),
but it is cheap insurance the installers also apply unconditionally.

### Not published (accepted 404)

| Host | Asset the extension asks for | Status |
| --- | --- | --- |
| `win32`/`arm64` | `tan-aarch64-pc-windows-msvc.exe` | not built |
| `linux`/`arm64` | `tan-aarch64-unknown-linux-musl` | not built |

A PyInstaller freeze **cannot be cross-compiled** — it embeds the interpreter
it ran under, so each asset must be built on its own architecture. That is the
constraint; the *reason* those two are absent is that this release builds on
four runners and adding two more was out of scope. Hosted arm64 runners do
exist (`windows-11-arm`, `ubuntu-24.04-arm`), and this repo is public, so their
minutes are not a barrier either. So this is a revisitable decision, not a
platform limit — do not record it as "there is no runner", and do not record it
as a billing constraint: an earlier revision of this paragraph said the arm64
minutes were "billed and plan-gated on a private repo", which stopped being true
when the repo went public and would have discouraged exactly the revisit this
paragraph exists to invite.

`x86_64-unknown-linux-musl` is also gone, and deliberately. A musl freeze is
dynamically linked against `/lib/ld-musl-x86_64.so.1` (measured in
PyInstaller's own musllinux wheel: `bootloader/Linux-64bit-intel-musl/run`), so
it runs **only on musl distros** — it is not the "static, runs on any libc"
artefact the Rust `-musl` target produced, and shipping it under that name
would break every Ubuntu/Debian/Fedora consumer.

**`install.sh` refuses a musl host (e.g. Alpine) outright, for every `--version`
— including an older tag that genuinely still publishes a `-unknown-linux-musl`
asset**, such as `v0.4.1`. This is a deliberate, host-level refusal, not a
per-tag one: the raw-vs-archive shape selection is genuinely per-tag
(tan-cli#356), but musl support is not resurrected for any tag by this
installer, past or future, regardless of what that tag's own `checksums.txt`
lists. The alternative — working on Alpine for old tags and refusing for new
ones, with the boundary being whichever `--version` a user happened to type —
is a worse promise than one clear refusal every time. A musl consumer installs
from a checkout instead (`git clone` + `pip install ./tan-cli/python`), which
the refusal message says. Do not read this refusal as fixed by #356: it
predates that change and is orthogonal to it, and moving it to run per-tag
(after the `checksums.txt` lookup) is a valid future revisit, not a limitation
inherent to musl or to PyInstaller.

### Reference `releaseAssetForTarget` (vscode side)

**Do not re-embed the extension's table here.** An in-repo snapshot of another
repo's function is exactly what rotted: the block this replaces claimed
`linux:x64` "still resolves to the `-musl` triple" and that
`SUPPORTED_CLI_VERSION` was "still pinned to the last Rust release", and both
had been false for 13 days when `v0.6.0-rc1` was cut (tan-cli#800). It also
told the reader NOT to repoint `linux:x64` yet -- advice that, followed, would
have reintroduced the `-musl` 404 that alp-sdk-vscode#444 removed.

The authority is the extension's own source:
[`alp-sdk-vscode` `src/alpCli/service.ts`](https://github.com/alplabai/alp-sdk-vscode/blob/main/src/alpCli/service.ts).
Read it there rather than here.

Measured 2026-08-18 against that checkout, for orientation only -- if this
paragraph and the file disagree, the file wins:

* `SUPPORTED_CLI_VERSION` is `"0.5.1"` on `origin/main` and `"0.6.0-rc1"` on
  `origin/dev`. Neither pins a Rust release; the extension has fetched the
  Python `tan` since 2026-08-01.
* `TARGETS` is keyed `platform/arch` (a slash, not a colon) and `linux/x64`
  maps to `x86_64-unknown-linux-gnu`, carrying the comment
  `// ── gnu, NOT musl. Do not "restore" -musl here (#444) ──`. It is a table
  of triples from which two candidate asset names are built; the release's own
  `checksums.txt` decides between them. It is not a function returning one raw
  name.
* The unpublished pair is `win32/arm64` and `linux/arm64` -- the extension
  tracks them per version in `HOSTS_WITHOUT_RELEASE_ASSET`, at
  `src/alpCli/service.ts:168` on `origin/main` (the branch the link above
  points at) and `:211` on `origin/dev`. The entry
  `"0.6.0-rc1": ["win32/arm64", "linux/arm64"]` exists only on `dev`; main's
  table still ends at `"0.5.1"`, matching its `SUPPORTED_CLI_VERSION` pin.

`release.yml`'s header calls this document the thing the extension "MUST match
exactly". That claim is only checkable if something fails when the two drift,
and nothing does today: `python/tests/gates/test_release_docs_match_the_workflow.py`
parses `## Targets published`, asset counts, publish jobs and install commands,
and never reads this section. Until such a gate exists, treat the list above as
dated orientation, not as the contract's teeth.

## glibc floor (the Linux asset)

PyInstaller has no equivalent of zigbuild's `--target …-gnu.2.31` pin: a freeze
inherits the glibc of the machine that froze it. **The old distro is therefore
the mechanism** — the Linux asset is frozen inside `python:3.12-slim-bullseye`
(Debian 11, whose own glibc is 2.31). Freezing on bare `ubuntu-latest` would
link its 2.39 and reproduce `GLIBC_2.39 not found` exactly as before. Debian
11's own glibc version is not the same number as the measured payload floor,
though — see the table below: the actual `GLIBC_` symbol versions the frozen
payload references top out at 2.30, one minor below the distro's own libc.

The floor published in the release notes is **measured, in the build image,
over the payload**, and how it is measured matters:

| Where you look | What you get | Useful? |
| --- | --- | --- |
| `readelf -V` on the onedir executable | `GLIBC_2.14`, under every image | **No.** That is PyInstaller's vendored bootloader. It is a container-INVARIANT constant — measured identical from bullseye (real floor 2.30) and trixie (real floor 2.38) — so it cannot detect the build image regressing to a newer glibc, which is the only thing the measurement is for. Lower bound only. |
| the appended payload | the real floor | **Yes.** libpython + the extension modules + their `.so` dependencies, enumerated by walking the collected onedir tree (`dist/tan/`, the launcher plus `_internal/`) and probing every file as an ELF, then read with `pyelftools`. An earlier revision of this step enumerated `.build/tan/PKG-00.toc` instead and claimed that was "unchanged by --onedir vs --onefile" -- that was false: under `--onedir` PyInstaller collects the natives into `_internal/` instead of appending them, so the TOC lists only 2 BINARY/EXTENSION entries, the floor step refuses to guess, `build` fails, and `release` (needs: `build`) is skipped under an already-pushed tag. That is what happened to the v0.5.0 tag; fixed by 70f9846 (#451). |

The build step refuses to emit a number if the scan finds implausibly few
native files or no `GLIBC_` version at all, and the release job refuses to
publish notes without one. An unverified floor in the notes is a compatibility
promise nobody checked — the retired `-gnu` asset asserted 2.31 in a comment
while its consumers hit `GLIBC_2.39 not found`.

For reference, the retired Rust `-gnu` asset (v0.3.1) measured `GLIBC_2.30`:
ran on `ubuntu:24.04` (2.39), `ubuntu:22.04` (2.35), `debian:11` (2.31); failed
on `ubuntu:18.04` (2.27). **Do not repeat the "2.31 floor / `GLIBC_2.39` not
found" wording** that `alp-sdk-vscode/src/alpCli/service.ts` carries — the
phenomenon is real, both numbers in it are wrong (alp-sdk-vscode#370).

## Build provenance

Every release asset (all four `tan-*` archives plus `checksums.txt` and
`envelope-contract.json` — the step's `subject-path` is `assets/*`) carries
a GitHub **build-provenance attestation**, generated by
`actions/attest-build-provenance` in the `release` job. Verify a downloaded
asset with:

```
gh attestation verify <downloaded-file> --repo alplabai/tan-cli \
  --signer-workflow alplabai/tan-cli/.github/workflows/release.yml
```

`--repo` alone binds the artefact to *some* workflow in this repository;
`--signer-workflow` is what pins it to the release job specifically.

`checksums.txt` (sha256 of every archive) is itself a release asset and is
covered by the same attestation. The `release` job is the only job with
`id-token: write` / `attestations: write` — every other job keeps the
workflow-level `contents: write` (or, for `gates`, `contents: read`).

## alp-sdk must be released BEFORE (or with) the tan tag that requires it

**Check this before every tag.** `tan/planner/` is a hash-audited mirror of
alp-sdk's planner, and a re-sync can start REQUIRING a `metadata/**` fact that
only unreleased alp-sdk carries. When it does, the released `tan` refuses on a
released alp-sdk, and the refusal reads to a customer as a defect in their own
board or SoM metadata. That is not hypothetical — it is tan-cli#591, found in
the v0.6.0 triage sweep, and the maintainer's 2026-08-09 decision on it was
**option 3: cut an alp-sdk release when tan is about to release.** No SDK
version floor in tan, no graceful degradation. This section is where that
decision lives, because an issue is not a checklist.

**The check.** For each `metadata/**` fact the planner has started requiring
since the last tag, confirm the alp-sdk commit that introduced it is contained
in a published alp-sdk **tag**:

```
git -C <alp-sdk> tag --contains <commit>      # non-empty, or the tag is premature
```

**Currently outstanding — do not cut a tan release carrying the AEN board emit
until this clears:**

| Requirement | alp-sdk commit | In a tag? |
|---|---|---|
| SE-owned ATOC reservation (`atoc` in `memory_map:`) — alp-sdk#1289 | `d639e777` | **NO** — `git tag --contains d639e777` is empty; `git merge-base --is-ancestor d639e777 v0.15.0` is false |
| `zephyr_peripherals_dtsi` on the SoC JSON — alp-sdk#1352 | `7d58ef32` | **NO** — descendant of the above, same answer |

Both are reached by `tan generate --target zephyr-board` on any `E1M-AEN*`
SKU, so today that command fails against **every** released alp-sdk. Until an
alp-sdk release contains `7d58ef32`, a tan tag carrying this planner ships a
subcommand that cannot succeed for AEN users on any SDK they can install.

**When that release exists**, two strings stop being true and must be
revisited in the same change: `zephyr_board.py`'s ATOC refusal says *"upgrade
alp-sdk to a release that includes alp-sdk#1289"* (an issue number, not a
version — replace it with the actual floor once one exists), and this table's
"In a tag?" column. Both live upstream in alp-sdk
`scripts/gen_zephyr_board.py`, so the string fix is an alp-sdk change
re-synced in, never a patch to the mirror. Tracked as **alp-sdk#1354**, which
also carries the reason the ATOC message is not the one a user actually sees
today: `_aen_peripherals_dtsi()` runs first, and `d639e777` is an ancestor of
`7d58ef32`, so every checkout with the field already has the region.

## Decisions

- **Archive, not a raw binary (tan-cli#349, from v0.5.0).** The build
  switched from PyInstaller `--onefile` to `--onedir`, so each release asset
  is now a `.zip`/`.tar.gz` archive of a directory (`tan` + `_internal/`), not
  a single raw executable. **Why**: `--onefile` re-extracts its whole ~14 MB
  runtime into a fresh temp dir on EVERY invocation — measured 13–19 s for
  `--version` on the published v0.5.0-rc4 macOS asset (unsigned re-extracted
  `.dylib`s get re-verified by the OS on every load) — which blew past
  alp-sdk-vscode's own 3 s version-probe budget (`vscodeAdapter.ts:1406`): that
  asset's `--version` TIMED OUT under the extension's own probe, not merely
  "slow". `--onedir` extracts once, at install time, instead of once per
  invocation — measured 0.337 s mean vs 0.880 s mean for `--version` on the
  same host, a >2x win even on Windows, which was never the platform in
  trouble. This is a real behavioural cost, not a preference, so raw-binary
  stays retired for NEW tags even though it was simpler for a consumer to
  fetch: `install.sh` / `install.ps1` absorb the extra unpack step so most
  consumers never see it. Retired for new tags is not the same as gone: the raw
  assets already published stay published and stay installable, which is why
  both installers keep a working path for them and pick per release rather than
  per version number (tan-cli#356).
- **Four targets, one per runner.** A PyInstaller freeze embeds the interpreter
  it ran under, so there is no cross-build to be had: the runner IS the target.
  Eight targets were possible while the binary was a `cargo` build (Windows
  arm64 and macOS x64 cross-compiled via `rustup target add`; every Linux
  target via `cargo-zigbuild`) and none of that survives the port.
- **Every asset is executed before it is published.** `cargo build` proved a
  binary linked; a freeze proves nothing until it runs, so each build leg runs
  `python/tests/conformance/test_packaged_binary.py` against the artefact it
  just produced (archive layout, `--version` inside the extension's 3 s probe
  budget, `tan init --preview` to prove the packaged scaffold templates
  survived).
- **Race-free publish.** Matrix jobs upload artifacts; a single `release` job
  collates and creates the release, so parallel jobs never race on release
  creation.
- **The GitHub release needs no secrets** — archives, `checksums.txt`,
  `envelope-contract.json` and the provenance attestation all run on the default
  `GITHUB_TOKEN`. `CARGO_REGISTRY_TOKEN` is gone with its job; `NPM_TOKEN` is
  read only by a channel that is off until someone arms it:

  | Job | State | Consequence |
  |---|---|---|
  | `publish · crates.io` | **deleted** | `cargo install alp-tan-cli` resolves only to the stale Rust program under that name. Do not advertise it. |
  | `publish · npm shim` | **live, opt-in** — final tags only, and then only when the repository variable `TAN_NPM_PUBLISH` is `true` | Unarmed, `npm i -g @alplabai/tan` does not resolve (`E404` at every version) and the job says so in the run summary. Armed, it publishes and `release_gate` fails the tag if it did not. |

  **Present is not the same as usable.** `NPM_TOKEN` was configured for v0.4.1
  and the job still failed — `npm error code EOTP`, because a classic/publish
  token on a 2FA account makes `npm publish` demand an interactive one-time
  password no CI run can answer. Only an npm **automation** token (or a granular
  token) is exempt. The missing-secret refusal above cannot catch this: the token
  is there, so the job proceeds and fails at the registry, after signing a
  provenance statement into the public transparency log for a version that never
  published (#233).

  Both previously emitted a `::warning::` and exited **0**, so the run summary
  read `publish · crates.io  success` while crates.io answered
  `crate 'alp-tan-cli' does not exist` — what shipped for v0.4.0 (#151). The
  lesson survives the deletion: a publish channel that cannot work must fail or
  be switched off, never report success. Any doc that offers `cargo install
  alp-tan-cli` or `npm i -g @alplabai/tan` as an install path is wrong until
  crates.io comes back and `TAN_NPM_PUBLISH` has actually put a version on the
  registry — a job existing is not a package existing.
