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

// The platform -> triple table, and it must equal the "Targets published"
// table in docs/release-contract.md, which install.sh also follows.
//
// From v0.5.0 the release is a PyInstaller freeze of `python/`, not a `cargo`
// build, and that flips the Linux answer: LINUX IS gnu, NOT musl. The Rust
// releases published a genuinely static `-musl` asset that ran on any distro,
// which is why this table (and install.sh, and the VS Code extension) used to
// map Linux there. PyInstaller cannot produce that artefact -- its musllinux
// bootloader is dynamically linked against /lib/ld-musl-x86_64.so.1 and runs
// ONLY on musl distros -- so the Python-era release ships only `-gnu`, frozen
// in `python:3.12-slim-bullseye` (glibc 2.31), and a `-musl` entry here would
// 404 on every v0.5.0+ tag while being wrong for the Ubuntu/Debian/Fedora hosts
// that asked for it.
//
// win32/arm64 and linux/arm64 are OMITTED entirely, not mapped to a triple
// that 404s: PyInstaller cannot cross-compile (every asset must be frozen on
// the architecture it runs on), and this release freezes on four runners, not
// six. A host in neither list hits `resolveTarget()`'s "no prebuilt binary"
// branch below -- one clear message and a `pip install` fallback, not a raw
// HTTP 404 mid-download.
//
// npm-shim/test/libc-mapping.test.js pins this table against the contract doc
// and install.sh, because the drift -- not the wrong value -- was the bug both
// times.
const TARGETS = {
  "win32/x64": "x86_64-pc-windows-msvc",
  "linux/x64": "x86_64-unknown-linux-gnu",
  "darwin/x64": "x86_64-apple-darwin",
  "darwin/arm64": "aarch64-apple-darwin",
};

/** Map the host platform/arch to the release target triple. */
function resolveTarget() {
  const key = `${process.platform}/${process.arch}`;
  const targets = TARGETS;
  const target = targets[key];
  if (!target) {
    throw new Error(
      `@alplabai/tan: no prebuilt binary for ${key}. Supported: ${Object.keys(targets).join(", ")}. ` +
        // `pip install alp-tan`, NOT `cargo install`: from 0.5.0 the released
        // binaries are PyInstaller freezes of the Python package (`alp-tan` on
        // PyPI), so there is no crate at this version to build from source. The
        // pip path needs no prebuilt asset for this platform at all -- it is the
        // real answer for a host this release's four-asset matrix does not cover.
        `Install from PyPI instead: pip install alp-tan (needs Python 3.12+; see https://github.com/${REPO}).`,
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

// `require.main === module` so the mapping can be imported by
// test/libc-mapping.test.js without DOWNLOADING a binary as a side effect of
// being read. Without the guard the pin test would have to re-parse this file as
// text, i.e. test a copy of the table rather than the table.
if (require.main === module) {
  main().catch((error) => {
    console.error(error && error.message ? error.message : String(error));
    process.exit(1);
  });
}

module.exports = { TARGETS, resolveTarget, parseChecksum };
