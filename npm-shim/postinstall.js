#!/usr/bin/env node
// SPDX-License-Identifier: Apache-2.0
//
// Downloads the platform-specific `tan` release asset from the GitHub release
// that matches this package's version (tag `v<version>`), verifies it, unpacks
// it, and leaves the launcher pointing at the unpacked tree. Runs as the
// package's `postinstall` step.
//
// From v0.5.0 (shipped 2026-08-04) the asset is an ARCHIVE of a PyInstaller
// `--onedir` freeze (`tan-<triple>.zip` on Windows, `tan-<triple>.tar.gz`
// elsewhere), not a raw binary. Every tag published since ships only that
// archive shape; the raw `tan-<triple>[.exe]` this shim also asks for is a
// legacy fallback reachable only on v0.4.1 and earlier -- tan-cli#349 changed
// the RELEASE, install.sh and install.ps1 to handle the archive shape, and
// left this shim behind asking ONLY for the archive name (tan-cli#362), which
// 404'd on every tag published at the time. Like the two installers, this
// shim supports BOTH shapes and asks the release itself (via checksums.txt)
// which one a given tag actually published, rather than assuming from the
// version — see `selectRelease` below.
//
// The archive's one top-level entry is `tan/`, holding `tan` (`tan.exe` on
// Windows) plus `_internal/`, its runtime. THE EXECUTABLE DOES NOT RUN WITHOUT
// THAT SIBLING, so the two are installed together, as one tree, at
// `tan-cli-lib/`; `tan` on PATH stays `bin/tan.js`, a launcher that execs into
// it. Same shape install.sh / install.ps1 use for their hosts — one launcher
// path, one private runtime directory beside it.
//
// Integrity: the `release` workflow publishes a `checksums.txt` (GNU
// `sha256sum` output) alongside the assets. This script fetches it FIRST —
// before choosing an asset name at all, not just before downloading one — and
// verifies the downloaded bytes' SHA-256 against the pinned digest, and FAILS
// CLOSED — a missing checksums.txt, a missing entry for either candidate
// asset name, or a digest mismatch aborts the install BEFORE anything is
// unpacked, chmod +x'd, or put on the launcher's path. It never runs an
// unverified binary, and it never EXTRACTS an unverified archive either:
// extraction writes attacker-named paths to disk, so it belongs after the
// digest check, not before it.
// (Resolves alplabai/tan-cli#11, alplabai/tan-cli#362.)

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { spawnSync } = require("child_process");

const pkg = require("./package.json");

const REPO = "alplabai/tan-cli";
const TAG = `v${pkg.version}`;

// The unpacked freeze: `tan-cli-lib/{tan[.exe], _internal/}`. Named for what
// install.sh and install.ps1 already call it on their hosts, so one name
// describes the layout everywhere. `bin/tan.js` reads LIB_DIR/EXE_NAME from
// here rather than re-deriving the path — the launcher and the installer
// disagreeing about where the binary lives is exactly the drift that produces
// a working install with a broken `tan`.
const LIB_DIR = path.join(__dirname, "tan-cli-lib");
// The archive's single top-level directory (`shutil.make_archive(...,
// base_dir="tan")` in python/scripts/build_binary.sh). Anything else in an
// archive is a layout change or an attack; `assertSafeEntries` rejects both.
const ARCHIVE_ROOT = "tan";

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
        // A CHECKOUT install, not `pip install alp-tan` (tan-cli#436): that was
        // the advice here, and the package does not exist -- `alp-tan` is a
        // RESERVED name with no PyPI publish job, so the command 404s
        // (`README.md`'s "Package managers" section and `python/README.md` both
        // say so). Handing a user a failing command at the exact moment their
        // platform is unsupported is worse than the unsupported platform.
        //
        // Not `cargo install alp-tan-cli` either: it still resolves from the
        // v0.4.1 era, but the crates.io publish job was deleted at v0.5.0, so
        // it would install a DIFFERENT, stale program under the same `tan`
        // name (docs/release-contract.md).
        `Install from a checkout instead: git clone https://github.com/${REPO} && python3 -m pip install ./tan-cli/python ` +
        `(needs Python 3.12+; see https://github.com/${REPO}).`,
    );
  }
  return target;
}

/**
 * The ARCHIVE release asset for a triple (the shape from v0.5.0 on). `.zip`
 * on Windows / `.tar.gz` elsewhere is the release's own split
 * (python/scripts/build_binary.sh picks the format from `$OS`), not this
 * shim's preference — take both from the contract doc's asset column, which
 * the test pins against.
 *
 * Takes `platform` rather than reading `process.platform` so the test can
 * check every published target from one host; the extension in the other repo
 * has to make the same choice and gets it wrong from the same distance.
 */
function assetName(platform, triple) {
  return `tan-${triple}${platform === "win32" ? ".zip" : ".tar.gz"}`;
}

/**
 * The RAW release asset for a triple — every tag published up to and
 * including v0.5.0-rc4 (tan-cli#356), no extension on Unix, `.exe` on
 * Windows. Mirrors install.sh's `raw_asset="tan-${arch_part}-${os_part}"` and
 * install.ps1's `$rawAsset = "tan-$archPart-pc-windows-msvc.exe"` — the three
 * consumers must compose this name identically, which is what
 * test/libc-mapping.test.js checks.
 */
function rawAssetName(platform, triple) {
  return `tan-${triple}${platform === "win32" ? ".exe" : ""}`;
}

/** The executable inside the archive (and inside LIB_DIR after install). */
function exeName(platform) {
  return platform === "win32" ? "tan.exe" : "tan";
}

// Node's stdlib has no tar reader and no zip reader, and this package
// deliberately has no dependencies (it is what npm runs BEFORE the user's own
// install finishes — a dependency here is a supply-chain edge on every
// consumer). So extraction shells out to the system archiver, the same call
// install.sh makes. `tar` is present on every supported host: Windows ships
// bsdtar as System32\tar.exe from Win10 1803, macOS ships bsdtar, Linux ships
// GNU tar.
//
// On Windows the System32 path is resolved EXPLICITLY, never a bare `tar` off
// PATH. Under Git Bash / MSYS — a very normal place to run npm on Windows —
// PATH's first `tar` is GNU tar (1.34 measured on a dev box here), and GNU tar
// cannot read the `.zip` this platform downloads: it answers "This does not
// look like a tar archive" and the install dies after a successful, verified
// download. bsdtar reads `.zip` AND `.tar.gz`, which is why one code path
// serves both asset shapes instead of a second Expand-Archive branch.
function tarBin() {
  if (process.platform !== "win32") return "tar";
  const sys32 = path.join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe");
  return fs.existsSync(sys32) ? sys32 : "tar";
}

/** Run the system archiver, surfacing its own stderr — never a guess. */
function runTar(args) {
  const bin = tarBin();
  const result = spawnSync(bin, args, { encoding: "utf8" });
  if (result.error) {
    throw new Error(
      `@alplabai/tan: could not run ${bin} (needed to unpack the release archive): ${result.error.message}`,
    );
  }
  if (result.status !== 0) {
    throw new Error(
      `@alplabai/tan: ${bin} ${args.join(" ")} exited ${result.status}: ${(result.stderr || "").trim()}`,
    );
  }
  return result.stdout || "";
}

/**
 * Reject a listing that would write outside the destination, or that is not
 * the single `tan/` tree the contract promises. Throws; the caller aborts
 * before extracting.
 *
 * Checked here rather than trusting the archiver: bsdtar and GNU tar both
 * strip a leading `/` and skip `..` members by default, but that is a DEFAULT
 * (`-P`/`--absolute-names` turns it off) and the two differ in what they do
 * with the remainder — silently dropping a member on one host and writing it
 * on another is the worst of the available behaviours. Ten lines here are
 * host-independent and are the thing the test can drive directly.
 *
 * Absolute-path detection does not use `path.isAbsolute` alone: that answers
 * per the HOST, and the archive comes from another one. A `C:\…` member must
 * be rejected while running on Linux, and a `/etc/…` member while running on
 * Windows.
 *
 * Ceiling: this sees NAMES, so it does not catch a symlink member whose target
 * escapes (the `tan/link -> /etc` + `tan/link/x` trick). Writing that archive
 * means controlling the release, and the SHA-256 check above is what stands in
 * the way of that; both tars also refuse to follow a symlink they just
 * extracted. Parse `-tvf` if that ever stops being true.
 */
function assertSafeEntries(entries) {
  for (const entry of entries) {
    // Some tars prefix every member with `./`; strip it before judging.
    const name = entry.replace(/^\.[/\\]/, "");
    if (!name) continue;
    if (name.startsWith("/") || name.startsWith("\\") || /^[A-Za-z]:/.test(name)) {
      throw new Error(
        `@alplabai/tan: refusing to extract absolute path ${entry} from the release archive.`,
      );
    }
    const segments = name.split(/[/\\]/).filter(Boolean);
    if (segments.includes("..")) {
      throw new Error(
        `@alplabai/tan: refusing to extract ${entry} from the release archive — it escapes the destination directory.`,
      );
    }
    if (segments[0] !== ARCHIVE_ROOT) {
      throw new Error(
        `@alplabai/tan: unexpected entry ${entry} in the release archive — every entry must live under ${ARCHIVE_ROOT}/ (archive layout changed? see docs/release-contract.md).`,
      );
    }
  }
}

/**
 * Unpack a VERIFIED archive into `destDir` and return the path to the
 * executable inside it (`destDir/tan/tan[.exe]`). Validates the listing first,
 * so nothing is written until every member is known to stay inside `destDir`.
 */
function unpackArchive(archivePath, destDir) {
  const listing = runTar(["-tf", archivePath])
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  if (listing.length === 0) {
    throw new Error(`@alplabai/tan: ${path.basename(archivePath)} is empty — nothing to install.`);
  }
  assertSafeEntries(listing);

  fs.mkdirSync(destDir, { recursive: true });
  runTar(["-xf", archivePath, "-C", destDir]);

  const exe = path.join(destDir, ARCHIVE_ROOT, exeName(process.platform));
  if (!fs.existsSync(exe)) {
    throw new Error(
      `@alplabai/tan: ${path.basename(archivePath)} did not contain ${ARCHIVE_ROOT}/${exeName(process.platform)} after extraction — archive layout changed?`,
    );
  }
  if (!fs.existsSync(path.join(destDir, ARCHIVE_ROOT, "_internal"))) {
    // A freeze without its `_internal/` runtime starts and dies with a
    // PyInstaller loader error, which reads like a broken binary rather than a
    // broken download. Fail here, where the cause is still visible.
    throw new Error(
      `@alplabai/tan: ${path.basename(archivePath)} contained no ${ARCHIVE_ROOT}/_internal/ — the executable cannot run without it.`,
    );
  }
  return exe;
}

/**
 * Atomically replace `libDir` with `newTreeDir` (a fully-populated directory
 * already sitting inside `stage`). Moves whatever is currently at `libDir`
 * aside first rather than deleting it, so a failed second rename (on Windows:
 * EPERM/EBUSY, while a `tan` from this install is still running) restores it
 * instead of losing it — shared by the archive and raw install paths below so
 * they cannot drift on this invariant.
 *
 * INVARIANT, and the reason for `error.preserveStage`: if the rollback rename
 * (`previous -> libDir`) ALSO fails, `previous` (a path under `stage`) is at
 * that point the ONLY surviving copy of the user's pre-install `tan` — the
 * whole reason this function moves the old tree aside instead of deleting it
 * up front. The thrown error is marked `preserveStage: true` so the caller's
 * cleanup (`fs.rmSync(stage, {recursive: true})`) skips deleting `stage`
 * instead of erasing the exact install this dance exists to protect.
 */
function swapIntoPlace(stage, newTreeDir, libDir) {
  let previous = null;
  if (fs.existsSync(libDir)) {
    previous = path.join(stage, "previous");
    fs.renameSync(libDir, previous);
  }
  try {
    fs.renameSync(newTreeDir, libDir);
  } catch (error) {
    if (!previous) throw error;
    try {
      fs.renameSync(previous, libDir);
    } catch (rollbackError) {
      const combined = new Error(
        `@alplabai/tan: install failed (${error.message}) AND could not restore the previous install ` +
          `(${rollbackError.message}). The previous install survives at ${previous} — move it to ${libDir} by hand.`,
      );
      combined.preserveStage = true;
      throw combined;
    }
    throw error;
  }
}

/**
 * Write, unpack and install the verified ARCHIVE bytes as `libDir` (`LIB_DIR`,
 * i.e. the launcher's target, unless a test says otherwise).
 *
 * The whole tree is assembled in a staging directory and moved into place with
 * renames, so `libDir` is never a half-extracted freeze: it is the previous
 * install, or the new one. Staging is created BESIDE `libDir`, not in
 * `os.tmpdir()`, because `fs.renameSync` is atomic only within one filesystem
 * and answers EXDEV across two — on Windows the temp dir is routinely on
 * another volume.
 */
function installArchive(bytes, asset, libDir = LIB_DIR) {
  fs.mkdirSync(path.dirname(libDir), { recursive: true });
  const stage = fs.mkdtempSync(path.join(path.dirname(libDir), ".tan-install-"));
  let preserveStage = false;
  try {
    const archivePath = path.join(stage, asset);
    fs.writeFileSync(archivePath, bytes);
    const exe = unpackArchive(archivePath, stage);
    if (process.platform !== "win32") {
      // tar preserves the mode build_binary.sh set, but an archive repacked by
      // hand may not; chmod unconditionally, as both installers do.
      fs.chmodSync(exe, 0o755);
    }
    swapIntoPlace(stage, path.join(stage, ARCHIVE_ROOT), libDir);
  } catch (error) {
    preserveStage = Boolean(error && error.preserveStage);
    throw error;
  } finally {
    // See swapIntoPlace's doc comment: `preserveStage` is set only when the
    // rollback itself failed, i.e. `stage/previous` is the sole surviving
    // copy of the pre-install `tan`. Deleting `stage` unconditionally here is
    // exactly the bug that would destroy it on the one path it must survive.
    if (!preserveStage) fs.rmSync(stage, { recursive: true, force: true });
  }
}

/**
 * Write and install verified RAW executable bytes as `libDir` — the shape
 * every tag up to and including v0.5.0-rc4 publishes (tan-cli#356). Staged as
 * a one-file directory (`stage/new/tan[.exe]`) rather than writing straight
 * to `libDir`, so it lands through the exact same `swapIntoPlace` atomic swap
 * `installArchive` uses and gets the same crash-safety and rollback
 * invariant, and so `libDir` ends up shaped identically either way — a
 * directory holding the executable — which is what lets `bin/tan.js` exec
 * `LIB_DIR/exeName(...)` unconditionally, with no "was this tag raw or
 * archived" branch of its own.
 */
function installRaw(bytes, libDir = LIB_DIR) {
  fs.mkdirSync(path.dirname(libDir), { recursive: true });
  const stage = fs.mkdtempSync(path.join(path.dirname(libDir), ".tan-install-"));
  let preserveStage = false;
  try {
    const newTree = path.join(stage, "new");
    fs.mkdirSync(newTree);
    const exe = path.join(newTree, exeName(process.platform));
    fs.writeFileSync(exe, bytes);
    if (process.platform !== "win32") fs.chmodSync(exe, 0o755);
    swapIntoPlace(stage, newTree, libDir);
  } catch (error) {
    preserveStage = Boolean(error && error.preserveStage);
    throw error;
  } finally {
    if (!preserveStage) fs.rmSync(stage, { recursive: true, force: true });
  }
}

async function main() {
  const target = resolveTarget();

  // Fetch checksums.txt BEFORE choosing an asset name, not just before
  // downloading one: it is the release's own manifest of what it published,
  // and asking it costs nothing extra since it is fetched unconditionally
  // anyway as the integrity source (tan-cli#356; mirrors install.sh's
  // digest_for / install.ps1's Get-DigestFor).
  const checksumsText = await fetchChecksums();
  const { asset, layout, digest } = selectRelease(checksumsText, process.platform, target);

  const url = `https://github.com/${REPO}/releases/download/${TAG}/${asset}`;
  console.log(`@alplabai/tan: downloading ${asset} (${TAG})…`);
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(
      `@alplabai/tan: failed to download ${asset} (HTTP ${response.status}) from ${url}`,
    );
  }
  const bytes = Buffer.from(await response.arrayBuffer());

  // Verify integrity BEFORE unpacking/installing anything. Fail closed: the
  // digest already came from checksums.txt above; a mismatch here aborts
  // rather than extracting — let alone running — an unverified binary.
  verifyDigest(bytes, asset, digest);

  if (layout === "archive") {
    installArchive(bytes, asset);
  } else {
    installRaw(bytes);
  }
  console.log(`@alplabai/tan: installed ${asset} → ${LIB_DIR} (launcher: bin/tan.js).`);
}

/**
 * Fetch the release's checksums.txt as text. Throws (fails closed) on any
 * fetch error — the caller aborts the install rather than guessing an asset
 * shape or running anything unverified.
 */
async function fetchChecksums() {
  const url = `https://github.com/${REPO}/releases/download/${TAG}/checksums.txt`;
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(
      `@alplabai/tan: could not fetch checksums.txt (HTTP ${response.status}) from ${url} — refusing to run an unverified binary.`,
    );
  }
  return response.text();
}

/**
 * Which asset `TAG` actually publishes for `platform`/`triple`, and its
 * pinned digest — asked of `checksumsText` itself rather than assumed from
 * this shim's own version (tan-cli#356; the bug this whole item exists to
 * fix). Archive name first (the shape from v0.5.0), the raw name as fallback
 * (every tag up to and including v0.5.0-rc4) — the same order install.sh's
 * `digest_for` / install.ps1's `Get-DigestFor` try them in, so a release
 * carrying both installs in the current shape and the three consumers cannot
 * disagree about which one a given tag has.
 *
 * Throws, naming BOTH candidate names, if checksums.txt lists neither: that
 * is either "no asset for this platform" or "the release shipped an asset and
 * forgot to check-sum it", and only the release page can tell them apart —
 * see docs/release-contract.md's "Which shape a release publishes".
 */
function selectRelease(checksumsText, platform, triple) {
  const archive = assetName(platform, triple);
  let digest = parseChecksum(checksumsText, archive);
  if (digest) return { asset: archive, layout: "archive", digest };

  const raw = rawAssetName(platform, triple);
  digest = parseChecksum(checksumsText, raw);
  if (digest) return { asset: raw, layout: "raw", digest };

  throw new Error(
    `@alplabai/tan: ${TAG} lists no asset for ${triple} in its checksums.txt — neither ${archive} nor ${raw}. ` +
      `Check what ${TAG} publishes: https://github.com/${REPO}/releases — refusing to install; there is nothing here to verify against.`,
  );
}

/** Verify `bytes`' SHA-256 against `expected`. Throws (fails closed) on a mismatch. */
function verifyDigest(bytes, asset, expected) {
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
// test/libc-mapping.test.js — and the launcher path by bin/tan.js — without
// DOWNLOADING a binary as a side effect of being read. Without the guard the
// pin test would have to re-parse this file as text, i.e. test a copy of the
// table rather than the table.
if (require.main === module) {
  main().catch((error) => {
    console.error(error && error.message ? error.message : String(error));
    process.exit(1);
  });
}

module.exports = {
  TARGETS,
  ARCHIVE_ROOT,
  LIB_DIR,
  resolveTarget,
  assetName,
  rawAssetName,
  exeName,
  tarBin,
  assertSafeEntries,
  unpackArchive,
  installArchive,
  installRaw,
  selectRelease,
  parseChecksum,
};
