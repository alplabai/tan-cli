<!-- SPDX-License-Identifier: Apache-2.0 -->

# @alplabai/tan

npm distribution shim for the native Rust **`tan`** CLI (Alp Lab's standalone
build CLI). Installing this package downloads the platform-specific binary
from the matching GitHub release and exposes it as the `tan` command — no
Rust toolchain and no runtime Node dependency required.

```bash
npm install -g @alplabai/tan
tan --version
```

or run it without installing:

```bash
npx @alplabai/tan --version
```

For the full command reference and other install channels (`cargo install
alp-tan-cli`, the install scripts, manual binary download), see the [repo
README](../README.md).

## How it works

- `postinstall.js` maps the host platform/arch to a release target triple,
  downloads the RAW `tan-<target>[.exe]` binary from
  `https://github.com/alplabai/tan-cli/releases/download/v<version>/`, verifies
  its SHA-256 against the release's `checksums.txt`, then writes it into
  `binary/` and `chmod +x`s it. tan's release ships one uncompressed binary per
  triple (not a `.tar.gz`), so there is no archive to extract.
- `bin/tan.js` forwards `tan …` invocations to that native binary.

Prebuilt targets: **Linux x64/arm64**, **macOS x64/arm64** (Intel + Apple
Silicon), **Windows x64/arm64** — all six triples the release publishes. Any
other platform/arch has no prebuilt binary; install via `cargo install
alp-tan-cli --locked` or build from source instead.

## Checksum verification

The `release` workflow publishes a `checksums.txt` (GNU `sha256sum` output)
alongside the six binaries. `postinstall.js` fetches it from the same release
and verifies the downloaded binary's SHA-256 against the pinned digest
**before** writing it to disk and `chmod +x`ing it. It **fails closed** — a
missing `checksums.txt`, a missing entry for the target asset, or a digest
mismatch aborts the install rather than running an unverified binary.
(Resolves [alplabai/tan-cli#11](https://github.com/alplabai/tan-cli/issues/11).)

## Releasing

1. Bump the version in **both** the workspace `Cargo.toml`
   (`[workspace.package] version`) and `npm-shim/package.json` to the same
   value. This is enforced, not just documented: `release.yml`'s
   `verify-version` job fails the tag if they disagree (`postinstall.js`
   resolves its download tag from `package.json`'s version alone, so a stale
   shim version silently fetches the wrong release's binaries).
2. Tag `v<version>` and push. `release.yml`:
   - builds the six target binaries and attaches them to the GitHub release
     (`build` + `release` jobs);
   - publishes `tan-core` then `alp-tan-cli` to crates.io (`publish_crates`);
   - publishes this package to npm (`publish_npm`).

   Each publish job is gated on its token secret (`CARGO_REGISTRY_TOKEN` /
   `NPM_TOKEN`) and is skipped, not failed, when the secret is unset.
