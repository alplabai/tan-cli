<!-- SPDX-License-Identifier: Apache-2.0 -->

# @alplabai/tan

> **This package is not published.** `npm view @alplabai/tan` answers
> `E404 Not Found` at every version, and `release.yml`'s `publish_npm` job is
> switched off (it names the two reasons). Nothing below works until it is
> turned back on — use the install scripts or a release asset instead.

npm distribution shim for the **`tan`** CLI (Alp Lab's standalone build CLI).
Installing this package downloads the platform-specific binary from the
matching GitHub release and exposes it as the `tan` command — no runtime Node
dependency required.

```bash
npm install -g @alplabai/tan
tan --version
```

or run it without installing:

```bash
npx @alplabai/tan --version
```

For the full command reference and the other install channels (the install
scripts, a manual binary download, `pip install ./python` from a checkout), see
the [repo README](../README.md).

## How it works

- `postinstall.js` maps the host platform/arch to a release target triple,
  downloads the RAW `tan-<target>[.exe]` binary from
  `https://github.com/alplabai/tan-cli/releases/download/v<version>/`, verifies
  its SHA-256 against the release's `checksums.txt`, then writes it into
  `binary/` and `chmod +x`s it. tan's release ships one uncompressed binary per
  triple (not a `.tar.gz`), so there is no archive to extract.
- `bin/tan.js` forwards `tan …` invocations to that native binary.

Prebuilt targets from v0.5.0: **Linux x64** (`-gnu`), **macOS x64/arm64**
(Intel + Apple Silicon), **Windows x64** — four assets, not six. `postinstall.js`
still maps `win32/arm64` and `linux/arm64` to triples the release no longer
publishes, so those two hosts 404 during postinstall; that map has to be
narrowed before this package is published again. Any platform/arch without a
prebuilt binary can install from a checkout instead: `pip install ./python`.

## Checksum verification

The `release` workflow publishes a `checksums.txt` (GNU `sha256sum` output)
alongside the binaries. `postinstall.js` fetches it from the same release
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
   - freezes the four target binaries and attaches them to the GitHub release
     (`build` + `release` jobs);
   - does **not** publish this package: `publish_npm` is `if: ${{ false }}`.
     The crates.io job is gone entirely — the assets are no longer built from
     `crates/`, so publishing `alp-tan-cli` would ship a different program
     under the same name.

   Re-enabling `publish_npm` needs both: `postinstall.js`'s target map narrowed
   to what the release actually publishes, and a working `NPM_TOKEN` (the
   current one is a classic token on a 2FA account, so `npm publish` answers
   `EOTP` and waits for an OTP that no unattended job can supply).
