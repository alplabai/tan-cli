<!-- SPDX-License-Identifier: Apache-2.0 -->
# tan release-asset contract

The `release` workflow (`.github/workflows/release.yml`) builds the `tan` binary
for each supported platform on a version-tag push and publishes them as GitHub
release assets. The **alp-sdk-vscode** extension downloads the matching asset on
activation, so the tag scheme and asset names are a **stable contract** — change
them only in lockstep with the extension's `releaseAssetForTarget`.

## Tag scheme

```
v<major>.<minor>.<patch>        e.g. v0.1.0
```

- SemVer, `v`-prefixed.
- The tag (minus the `v`) MUST equal the `tan` crate version in the workspace
  `Cargo.toml` (`[workspace.package] version`). The `verify-version` job fails
  the release if they differ, so a mismatched tag never publishes assets.

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

## Targets published

| VS Code `process.platform` | `process.arch` | Target triple                | Asset name                          |
| -------------------------- | -------------- | ---------------------------- | ----------------------------------- |
| `win32`                    | `x64`          | `x86_64-pc-windows-msvc`     | `tan-x86_64-pc-windows-msvc.exe`    |
| `win32`                    | `arm64`        | `aarch64-pc-windows-msvc`    | `tan-aarch64-pc-windows-msvc.exe`   |
| `linux`                    | `x64`          | `x86_64-unknown-linux-gnu`   | `tan-x86_64-unknown-linux-gnu`      |
| `linux`                    | `arm64`        | `aarch64-unknown-linux-gnu`  | `tan-aarch64-unknown-linux-gnu`     |
| `darwin`                   | `x64`          | `x86_64-apple-darwin`        | `tan-x86_64-apple-darwin`           |
| `darwin`                   | `arm64`        | `aarch64-apple-darwin`       | `tan-aarch64-apple-darwin`          |

Windows ships **both** x64 and arm64 assets — the extension picks by
`process.arch` (`win32`+`x64` vs `win32`+`arm64`). After download on a Unix host
the extension must `chmod +x` the raw binary.

### Reference `releaseAssetForTarget` (vscode side)

```ts
function releaseAssetForTarget(platform: NodeJS.Platform, arch: string): string {
  const triple = {
    "win32:x64": "x86_64-pc-windows-msvc",
    "win32:arm64": "aarch64-pc-windows-msvc",
    "linux:x64": "x86_64-unknown-linux-gnu",
    "linux:arm64": "aarch64-unknown-linux-gnu",
    "darwin:x64": "x86_64-apple-darwin",
    "darwin:arm64": "aarch64-apple-darwin",
  }[`${platform}:${arch}`];
  if (!triple) throw new Error(`unsupported platform ${platform}/${arch}`);
  return platform === "win32" ? `tan-${triple}.exe` : `tan-${triple}`;
}
```

## Decisions

- **Raw binary, not an archive.** The stripped release `tan` is small; a raw
  asset means the downloader fetches one file and (on Unix) `chmod +x`s it — no
  unzip step, no archive-layout assumption.
- **Six targets.** The four requested + `aarch64-pc-windows-msvc` (arm64 Windows)
  + `aarch64-unknown-linux-gnu`. Both Windows arm64 and macOS x64 cross-compile
  from their sibling runner via `rustup target add`; aarch64 linux uses the
  `gcc-aarch64-linux-gnu` cross-linker — no extra runner types.
- **Race-free publish.** Matrix jobs upload artifacts; a single `release` job
  collates and creates the release, so parallel jobs never race on release
  creation.
- **No secrets.** Same-repo release uses the default `GITHUB_TOKEN` only.
