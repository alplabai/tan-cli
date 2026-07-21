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
// Integrity: the `release` workflow publishes a `checksums.txt` (GNU
// `sha256sum` output) alongside the binaries. This script fetches it from the
// same release, verifies the downloaded binary's SHA-256 against the pinned
// digest, and FAILS CLOSED — a missing checksums.txt, a missing entry, or a
// mismatch aborts the install — before the binary is ever written and
// chmod +x'd. It never runs an unverified binary. (Resolves alplabai/tan-cli#11.)

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

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
  const bytes = Buffer.from(await response.arrayBuffer());

  // Verify integrity BEFORE writing an executable to disk. Fail closed: a
  // missing checksums.txt, a missing entry, or a digest mismatch aborts the
  // install rather than running an unverified binary.
  await verifyChecksum(bytes, asset);

  // Raw binary asset — write straight to disk, no archive to extract.
  fs.writeFileSync(binPath, bytes);

  if (process.platform !== "win32") {
    fs.chmodSync(binPath, 0o755);
  }
}

/**
 * Fetch the release's checksums.txt and verify `bytes` against the pinned
 * SHA-256 for `asset`. Throws (fails closed) on any fetch error, missing
 * entry, or mismatch — the caller aborts the install.
 */
async function verifyChecksum(bytes, asset) {
  const url = `https://github.com/${REPO}/releases/download/${TAG}/checksums.txt`;
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(
      `@alplabai/tan: could not fetch checksums.txt (HTTP ${response.status}) from ${url} — refusing to run an unverified binary.`,
    );
  }
  const expected = parseChecksum(await response.text(), asset);
  if (!expected) {
    throw new Error(
      `@alplabai/tan: no SHA-256 entry for ${asset} in checksums.txt — refusing to run an unverified binary.`,
    );
  }
  const actual = crypto.createHash("sha256").update(bytes).digest("hex");
  if (actual !== expected) {
    throw new Error(
      `@alplabai/tan: checksum mismatch for ${asset} — expected ${expected}, got ${actual}. Aborting install.`,
    );
  }
  console.log(`@alplabai/tan: verified ${asset} (sha256 ${actual}).`);
}

/**
 * Parse GNU `sha256sum` output ("<64-hex>  <filename>", optional `*` binary
 * marker) and return the lowercase hex digest for `asset`, or null if absent.
 */
function parseChecksum(text, asset) {
  for (const line of text.split("\n")) {
    const m = line.trim().match(/^([0-9a-fA-F]{64})\s+\*?(.+)$/);
    if (m && m[2] === asset) {
      return m[1].toLowerCase();
    }
  }
  return null;
}

main().catch((error) => {
  console.error(error && error.message ? error.message : String(error));
  process.exit(1);
});
