#!/usr/bin/env node
// SPDX-License-Identifier: Apache-2.0
//
// Downloads the platform-specific `tan` binary from the GitHub release that
// matches this package's version (tag `v<version>`). tan's release ships a
// RAW, uncompressed binary per target triple (no .tar.gz — see
// docs/release-contract.md in the main repo), so this writes the download
// straight to disk and chmods it: no archive to unpack. Runs as the
// package's `postinstall` step.
//
// No checksum is verified: the `release` workflow does not yet publish a
// SHA-256 / checksums file alongside the binaries, so there's nothing to pin
// against. Tracked as a follow-up — see alplabai/tan-cli#11's open question
// ("should the npm shim verify a release SHA-256, and should the release
// start publishing a checksums file for both the shim and install.sh to
// consume?"). Once that lands, verify the digest here before chmod +x.

const fs = require("fs");
const path = require("path");

const pkg = require("./package.json");

const REPO = "alplabai/tan-cli";
const TAG = `v${pkg.version}`;
const BINARY_DIR = path.join(__dirname, "binary");

/** Map the host platform/arch to the release target triple. */
function resolveTarget() {
  const key = `${process.platform}/${process.arch}`;
  // Same platform -> triple table as install.sh/install.ps1 and the VS Code
  // extension's releaseAssetForTarget (docs/release-contract.md, main repo).
  const targets = {
    "win32/x64": "x86_64-pc-windows-msvc",
    "win32/arm64": "aarch64-pc-windows-msvc",
    "linux/x64": "x86_64-unknown-linux-gnu",
    "linux/arm64": "aarch64-unknown-linux-gnu",
    "darwin/x64": "x86_64-apple-darwin",
    "darwin/arm64": "aarch64-apple-darwin",
  };
  const target = targets[key];
  if (!target) {
    throw new Error(
      `@alplabai/tan: no prebuilt binary for ${key}. Supported: ${Object.keys(targets).join(", ")}. ` +
        `Build from source: cargo install alp-tan-cli --locked (see https://github.com/${REPO}).`,
    );
  }
  return target;
}

async function main() {
  const target = resolveTarget();
  const ext = process.platform === "win32" ? ".exe" : "";
  const asset = `tan-${target}${ext}`;
  const url = `https://github.com/${REPO}/releases/download/${TAG}/${asset}`;

  fs.mkdirSync(BINARY_DIR, { recursive: true });
  const binName = process.platform === "win32" ? "tan.exe" : "tan";
  const binPath = path.join(BINARY_DIR, binName);

  console.log(`@alplabai/tan: downloading ${asset} (${TAG})…`);
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(
      `@alplabai/tan: failed to download ${asset} (HTTP ${response.status}) from ${url}`,
    );
  }
  // Raw binary asset — write straight to disk, no archive to extract.
  fs.writeFileSync(binPath, Buffer.from(await response.arrayBuffer()));

  if (process.platform !== "win32") {
    fs.chmodSync(binPath, 0o755);
  }
}

main().catch((error) => {
  console.error(error && error.message ? error.message : String(error));
  process.exit(1);
});
