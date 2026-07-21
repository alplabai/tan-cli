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

- `postinstall.js` maps the host platform/arch to a release target triple and
  downloads the RAW `tan-<target>[.exe]` binary from
  `https://github.com/alplabai/tan-cli/releases/download/v<version>/` into
  `binary/`, then `chmod +x`s it. tan's release ships one uncompressed binary
  per triple (not a `.tar.gz`), so there is no archive to extract — the
  download is written straight to disk.
- `bin/tan.js` forwards `tan …` invocations to that native binary.

Prebuilt targets: **Linux x64/arm64**, **macOS x64/arm64** (Intel + Apple
Silicon), **Windows x64/arm64** — all six triples the release publishes. Any
other platform/arch has no prebuilt binary; install via `cargo install
alp-tan-cli --locked` or build from source instead.

## Checksum verification

Not yet implemented. The `release` workflow does not currently publish a
SHA-256 or checksums file alongside the binaries, so there is nothing to pin
against — adding a verification step now would fake a guarantee the release
doesn't back. Tracked as a follow-up (see the open question in
[alplabai/tan-cli#11](https://github.com/alplabai/tan-cli/issues/11)); once
the release starts publishing checksums, `postinstall.js` should verify the
downloaded binary's digest before `chmod +x`ing it.

## Releasing

1. Bump the version in **both** the workspace `Cargo.toml`
   (`[workspace.package] version`) and `npm-shim/package.json` to the same
   value.
2. Tag `v<version>` and push. `release.yml`:
   - builds the six target binaries and attaches them to the GitHub release
     (`build` + `release` jobs);
   - publishes `tan-core` then `alp-tan-cli` to crates.io (`publish_crates`);
   - publishes this package to npm (`publish_npm`).

   Each publish job is gated on its token secret (`CARGO_REGISTRY_TOKEN` /
   `NPM_TOKEN`) and is skipped, not failed, when the secret is unset.
