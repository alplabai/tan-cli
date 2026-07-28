<!-- SPDX-License-Identifier: Apache-2.0 -->
# tan release-asset contract

The `release` workflow (`.github/workflows/release.yml`) builds the `tan` binary
for each supported platform on a version-tag push and publishes them as GitHub
release assets. The **alp-sdk-vscode** extension downloads the matching asset on
activation, so the tag scheme and asset names are a **stable contract** — change
them only in lockstep with the extension's `releaseAssetForTarget`.

## Tag scheme

```
v<major>.<minor>.<patch>              e.g. v0.1.0     release
v<major>.<minor>.<patch>-<pre>        e.g. v0.4.0-rc1 pre-release
```

- SemVer, `v`-prefixed.
- The tag (minus the `v`) MUST equal the `tan` crate version in the workspace
  `Cargo.toml` (`[workspace.package] version`). The `verify-version` job fails
  the release if they differ, so a mismatched tag never publishes assets. This
  holds for a pre-release too: `v0.4.0-rc1` requires `version = "0.4.0-rc1"`.

### Pre-releases

**The hyphen is the whole signal.** `release.yml` derives both flags from it, so
they cannot disagree with each other or with the tag:

| Tag | `prerelease` | `make_latest` | crates.io |
| --- | --- | --- | --- |
| `v0.4.0` | `false` | `true` | published |
| `v0.4.0-rc1` | `true` | `false` | **skipped** |

The npm shim is skipped for a pre-release too, and that path was the
sharpest: `npm publish` passes no `--tag`, so npm defaults to the `latest`
dist-tag -- an unguarded rc would become plain `npm i -g @alplabai/tan` for
every consumer, and npm unpublish is far more restricted than a crates.io
yank. The relaxation, when an rc should be installable, is `--tag next`.

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

crates.io is skipped for a pre-release deliberately: a crates.io publish can
only be **yanked**, never deleted, while a GitHub pre-release can be removed
outright. Keeping an rc off crates.io keeps it fully retractable, which is the
reason to cut one at all. (crates.io does accept SemVer pre-release versions,
and `cargo install` / `^` ranges skip them by default — so this is a
conservative call, not a forced one.)

Note that `tan sdk list` flags **alp-sdk** draft/pre-releases (tan-cli#122) —
that is a different release stream from tan's own, and neither protects the
other.

## Asset names

One **raw, uncompressed binary per target triple** (no `.zip` / `.tar.gz`):

```
tan-<target-triple>            # Unix   (no extension)
tan-<target-triple>.exe        # Windows
```

Download URL is fully deterministic:

```
https://github.com/alplabai/tan-cli/releases/download/<tag>/<asset>
```

Plus two non-binary assets, carrying the same build-provenance attestation:

| Asset | Contents |
| --- | --- |
| `checksums.txt` | sha256 of every other asset. |
| `envelope-contract.json` | The JSON envelope contract — the frozen issue codes (`contract/issue-codes.json`) plus one golden envelope per command family (`contract/envelopes/`), so a consumer's contract test diffs against a published artefact instead of a hand-copied fixture that drifts. See [`contract/README.md`](../contract/README.md). |

## Targets published

| VS Code `process.platform` | `process.arch` | Target triple                | Asset name                          |
| -------------------------- | -------------- | ---------------------------- | ----------------------------------- |
| `win32`                    | `x64`          | `x86_64-pc-windows-msvc`     | `tan-x86_64-pc-windows-msvc.exe`    |
| `win32`                    | `arm64`        | `aarch64-pc-windows-msvc`    | `tan-aarch64-pc-windows-msvc.exe`   |
| `linux`                    | `x64`          | `x86_64-unknown-linux-musl`  | `tan-x86_64-unknown-linux-musl`     |
| `linux`                    | `arm64`        | `aarch64-unknown-linux-musl` | `tan-aarch64-unknown-linux-musl`    |
| `darwin`                   | `x64`          | `x86_64-apple-darwin`        | `tan-x86_64-apple-darwin`           |
| `darwin`                   | `arm64`        | `aarch64-apple-darwin`       | `tan-aarch64-apple-darwin`          |

Windows ships **both** x64 and arm64 assets — the extension picks by
`process.arch` (`win32`+`x64` vs `win32`+`arm64`). After download on a Unix host
the extension must `chmod +x` the raw binary.

### Additional Linux assets (glibc)

Two more assets ship per release. **They are NOT what the extension downloads**
— they exist for consumers that specifically want a glibc build:

| Target triple                | Asset name                       |
| ----------------------------- | --------------------------------- |
| `x86_64-unknown-linux-gnu`   | `tan-x86_64-unknown-linux-gnu`   |
| `aarch64-unknown-linux-gnu`  | `tan-aarch64-unknown-linux-gnu`  |

The `-gnu` assets cross-build via `cargo-zigbuild` pinned to
`x86_64-unknown-linux-gnu.2.31`, so they carry a glibc floor. **That floor is
the reason the extension maps both Linux targets to `-musl` instead** — a `-musl`
build is fully static and runs on any distro/libc.

Two numbers here, and they are not the same number:

| | Value | Source |
| --- | --- | --- |
| zigbuild **pin** | `2.31` | `release.yml`'s `--target …-gnu.2.31` |
| **measured** floor of the shipped v0.3.1 binary | `GLIBC_2.30` | `readelf -V`, plus a live matrix |

The pin caps which symbols may be used; the binary happened to need nothing
above 2.30. Measured behaviour on the v0.3.1 `-gnu` asset: runs fine on
`ubuntu:24.04` (2.39), `ubuntu:22.04` (2.35) and `debian:11` (2.31); fails on
`ubuntu:18.04` (2.27) with `version 'GLIBC_2.30' not found`. The `-musl` asset
ran on all four.

**Do not repeat the "2.31 floor / `GLIBC_2.39` not found" wording** that
`alp-sdk-vscode/src/alpCli/service.ts` currently carries — the phenomenon is
real but both numbers in it are wrong, tracked as alp-sdk-vscode#370. This table
is the measured version.

**This section previously said the opposite** — that `linux/x64` and
`linux/arm64` consumed the `-gnu` assets and that the musl assets were "not
(yet) wired into" `releaseAssetForTarget`. That has not been true since the
extension repointed to `-musl`. Corrected here rather than left to mislead the
next reader of the contract; `alp-sdk` documented the same `-gnu`/`-musl`
mix-up in its own install docs and fixed it in alp-sdk#990.

### Reference `releaseAssetForTarget` (vscode side)

```ts
function releaseAssetForTarget(platform: NodeJS.Platform, arch: string): string {
  const triple = {
    "win32:x64": "x86_64-pc-windows-msvc",
    "win32:arm64": "aarch64-pc-windows-msvc",
    // musl, NOT gnu — see the glibc floor below. Changing these two back
    // reintroduces the -gnu asset's glibc floor (measured GLIBC_2.30 on
    // v0.3.1), which breaks pre-Ubuntu-20.04 / pre-Debian-11 consumers.
    "linux:x64": "x86_64-unknown-linux-musl",
    "linux:arm64": "aarch64-unknown-linux-musl",
    "darwin:x64": "x86_64-apple-darwin",
    "darwin:arm64": "aarch64-apple-darwin",
  }[`${platform}:${arch}`];
  if (!triple) throw new Error(`unsupported platform ${platform}/${arch}`);
  return platform === "win32" ? `tan-${triple}.exe` : `tan-${triple}`;
}
```

## glibc floor (the `-gnu` assets)

`tan-x86_64-unknown-linux-gnu` and `tan-aarch64-unknown-linux-gnu` are
cross-built with **`cargo-zigbuild`** against a pinned **glibc 2.31** floor
(`x86_64-unknown-linux-gnu.2.31` / `aarch64-unknown-linux-gnu.2.31`), not the
ubuntu-latest runner's own glibc (2.39 on ubuntu-24.04, which produces a
binary that fails with `GLIBC_2.39 not found` on older distros such as Debian
bookworm/2.36). 2.31 is the floor the retired `alp` CLI's `release-cli-rs.yml`
pipeline used (issue #6's cited prior art).

Note the distinction drawn in the `-gnu` asset section above: **2.31 is the
zigbuild pin, not the measured floor.** The shipped v0.3.1 `-gnu` binary needs
nothing above `GLIBC_2.30` (`readelf -V`), so it runs on Debian 11 and fails
only below roughly Ubuntu 20.04. The `GLIBC_2.39` figure in this paragraph is
the counterfactual — what building on the raw runner *without* the pin would
produce — not what the shipped asset does.

## Build provenance

Every release asset (all eight `tan-*` binaries plus `checksums.txt`) carries
a GitHub **build-provenance attestation**, generated by
`actions/attest-build-provenance` in the `release` job. Verify a downloaded
asset with:

```
gh attestation verify <downloaded-file> --repo alplabai/tan-cli
```

`checksums.txt` (sha256 of every binary) is itself a release asset and is
covered by the same attestation. The `release` job is the only job with
`id-token: write` / `attestations: write` — every other job keeps the
workflow-level `contents: write` (or, for `gates`, `contents: read`).

## Decisions

- **Raw binary, not an archive.** The stripped release `tan` is small; a raw
  asset means the downloader fetches one file and (on Unix) `chmod +x`s it — no
  unzip step, no archive-layout assumption.
- **Eight targets.** The four requested + `aarch64-pc-windows-msvc` (arm64
  Windows) + `aarch64-unknown-linux-gnu` + the two `-musl` Linux targets (#6).
  Windows arm64 and macOS x64 cross-compile from their sibling runner via
  `rustup target add`; all four Linux targets cross-build on ubuntu-latest via
  `cargo-zigbuild` — no extra runner types, no `gcc-aarch64-linux-gnu` cross
  linker (zigbuild supplies its own via `zig cc`).
- **Race-free publish.** Matrix jobs upload artifacts; a single `release` job
  collates and creates the release, so parallel jobs never race on release
  creation.
- **The GitHub release needs no secrets** — binaries, `checksums.txt`,
  `envelope-contract.json` and the provenance attestation all run on the default
  `GITHUB_TOKEN`. **The two registry publishes do not**, and this bullet used to
  say "No secrets" flatly, which is how nobody noticed that neither was
  configured:

  | Job | Secret | Without it |
  |---|---|---|
  | `publish · crates.io` | `CARGO_REGISTRY_TOKEN` | `cargo install alp-tan-cli` does not resolve |
  | `publish · npm shim` | `NPM_TOKEN` | `npm i -g @alplabai/tan` does not resolve |

  Both previously emitted a `::warning::` and exited **0**, so the run summary
  read `publish · crates.io  success` while crates.io answered
  `crate 'alp-tan-cli' does not exist`. That is what shipped for v0.4.0 (#151).
  On a FINAL tag a missing secret now **fails the job** and writes the outcome
  to the run summary; both jobs are `needs: release`, so the GitHub assets are
  already published and unaffected — the run goes red to tell the truth about
  the two registry channels, not to withhold the release. Pre-release tags never
  reach either job (`!contains(github.ref_name, '-')`), so nothing about an rc
  changes.
