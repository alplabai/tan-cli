<!-- SPDX-License-Identifier: Apache-2.0 -->

# @alplabai/tan

> **This package is not published.** `npm view @alplabai/tan` answers
> `E404 Not Found` at every version. `release.yml`'s `publish_npm` job only
> runs on a final (non-pre-release) tag, and even then is gated OFF by
> default behind the `TAN_NPM_PUBLISH` repository variable — see
> [Releasing](#releasing) for why. Nothing below works until it is armed —
> use the install scripts or a release asset instead.

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
  fetches the release's `checksums.txt` from
  `https://github.com/alplabai/tan-cli/releases/download/v<version>/` and asks
  it which asset this tag actually published for that triple — the archive
  name (`tan-<target>.zip` / `.tar.gz`) first, the raw name
  (`tan-<target>[.exe]`) as fallback — downloads whichever one it finds,
  verifies its SHA-256 against the pinned digest, and only then installs it.
- `bin/tan.js` forwards `tan …` invocations to `tan-cli-lib/tan[.exe]`.

**From v0.5.0 (shipped 2026-08-04) the asset is an archive of a PyInstaller
`--onedir` freeze**, not a raw binary
([#349](https://github.com/alplabai/tan-cli/issues/349)). Every tag published
since ships only that archive shape; the raw `tan-<target>[.exe]` name this
shim also asks for is a legacy fallback reachable only on `v0.4.1` and
earlier. (This package is unpublished — see the note at the top — so
`package.json`'s own version, an `-rc1.dev0`-suffixed pre-release string that
moves ahead of the last real tag, names no tag that has actually shipped
either shape; the claim above is about what real tags publish, not about
this shim's own pin.) Asking for the archive name
unconditionally, before any tag published it, was
[#362](https://github.com/alplabai/tan-cli/issues/362) — it 404'd on every tag
that existed at the time. `postinstall.js`'s `selectRelease` decides which
shape a given tag actually published from its `checksums.txt` (the same rule
`install.sh` / `install.ps1` follow,
[#356](https://github.com/alplabai/tan-cli/issues/356)), never from the
version number, so both shapes still install correctly if an old tag is ever
requested by `--version`.

The archive's one top-level entry is `tan/`, holding `tan` (`tan.exe` on
Windows) plus `_internal/`, the runtime — **the executable does not run
without that sibling**, which is why an archive installs as a directory and
`tan` on `PATH` is a launcher, exactly as `install.sh` / `install.ps1` do it. A
raw binary installs the same way — as a one-file `tan-cli-lib/` directory — so
`bin/tan.js` never needs to know which shape this tag shipped. Unpacking an
archive shells out to the system `tar` (bsdtar on Windows and macOS, GNU tar on
Linux): node's stdlib reads neither `tar` nor `zip`, and a `postinstall`
script is the last place to want a dependency. Entries are checked before
extraction — an absolute path, a `..` component, or anything outside `tan/`
aborts the install rather than being written.

Prebuilt targets from v0.5.0: **Linux x64** (`-gnu`), **macOS x64/arm64**
(Intel + Apple Silicon), **Windows x64** — four assets, not six.
`postinstall.js`'s `TARGETS` map names only those four; `win32/arm64` and
`linux/arm64` (no asset published for either — a PyInstaller freeze cannot be
cross-compiled) hit `resolveTarget()`'s "no prebuilt binary" branch and a
`pip install` pointer instead of a download that would 404. Any platform/arch
without a prebuilt binary can install from a checkout instead:
`pip install ./python`.

## Checksum verification

The `release` workflow publishes a `checksums.txt` (GNU `sha256sum` output)
alongside the assets. `postinstall.js` fetches it FIRST — before choosing an
asset name at all, since `checksums.txt` also doubles as the manifest of which
shape this tag published (see above) — and verifies the downloaded bytes'
SHA-256 against the pinned digest **before installing anything** — extraction
writes attacker-named paths to disk, so it belongs after the digest check, not
before it. It **fails closed**: a missing `checksums.txt`, a missing entry for
either candidate asset name, or a digest mismatch aborts the install rather
than extracting, `chmod +x`ing or running an unverified binary. The verified
bytes are then moved into place with renames, so `tan-cli-lib/` is either the
previous install or the new one, never a half-written freeze — and a failed
swap restores the previous install rather than losing it.
(Resolves [alplabai/tan-cli#11](https://github.com/alplabai/tan-cli/issues/11),
[#362](https://github.com/alplabai/tan-cli/issues/362).)

## Releasing

1. Bump `TAN_VERSION` in `python/tan/version.py` — the source of truth, and the
   string the shipped binary actually prints — and `npm-shim/package.json`'s
   `version` to match it exactly. This is enforced, not just documented:
   `python/scripts/version_check.py --selftest --tag` (run by `release.yml`'s
   `verify-version` job) fails the tag if they disagree — `postinstall.js`
   resolves its download TAG from `package.json`'s version alone (the
   ``const TAG = `v${pkg.version}`;`` line in `npm-shim/postinstall.js`), so a
   stale shim version silently fetches from the wrong release. Which ASSET
   SHAPE it asks for at that tag is not version-derived, though —
   `selectRelease` decides that from the tag's own
   `checksums.txt` (see [How it works](#how-it-works)), which is what lets this
   shim install correctly at both a raw-asset tag and an archive tag without
   caring which one `package.json`'s version happens to be. `Cargo.toml` is
   **not** part of this check, and since tan-cli#269 does not exist: it
   versioned the retired Rust crates, never the release assets.
2. Tag `v<version>` and push. `release.yml`:
   - freezes the four target binaries and attaches them to the GitHub release
     (`build` + `release` jobs);
   - only even ATTEMPTS to publish this package on a final (non-pre-release)
     tag: `publish_npm`'s job-level `if` is
     `startsWith(github.ref, 'refs/tags/') && !contains(github.ref_name, '-')`
     (`release.yml`), so a `-rc*`/`-preN` tag skips this job entirely, the same
     way it skips `make_latest`. The crates.io job is gone entirely — the
     assets were never built from `crates/`, which is itself deleted
     (tan-cli#269), so publishing `alp-tan-cli` would ship a different program
     under the same name.
   - even on a final tag, publishing is OPT-IN and OFF by default: the job
     reads `NPM_PUBLISH_ENABLED: ${{ vars.TAN_NPM_PUBLISH == 'true' }}`
     (`release.yml`) and, unless that repository *variable* is set to `true`,
     records `published=false` and explains why in the run summary rather
     than attempting a publish — a deliberately loud no-op, not a silent
     skip.

   Arming it needs BOTH steps, not just one: set the repository variable
   `TAN_NPM_PUBLISH` to `true`, **and** replace `NPM_TOKEN` — the current one
   is a classic token on a 2FA account, so `npm publish` answers `EOTP` and
   waits for an OTP that no unattended job can supply; an *automation* (or
   granular) token is exempt. If `TAN_NPM_PUBLISH` is `true` but `NPM_TOKEN`
   is still empty, the job fails loudly rather than reporting a publish that
   did not happen. The other stated blocker is gone: `postinstall.js`'s
   target map is narrowed to the four targets the release publishes, and from
   [#362](https://github.com/alplabai/tan-cli/issues/362) it asks for the
   shape (archive or, for a pre-v0.5.0 tag, raw) those targets actually ship.
