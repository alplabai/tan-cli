// SPDX-License-Identifier: Apache-2.0
//
// The npm shim, install.sh and the VS Code extension all map a host to a release
// asset, and for a while they disagreed: this shim served `-gnu` for Linux while
// install.sh and the extension served `-musl`, and the shim's own comment claimed
// the three were identical. A musl host therefore downloaded a glibc binary that
// cannot execute, and the comment sent the next reader looking somewhere else.
//
// From v0.5.0 the correct answer REVERSED: the release assets are PyInstaller
// freezes, not `cargo` builds, and PyInstaller cannot produce the static,
// any-distro artefact the retired Rust `-musl` target did -- its musllinux
// bootloader is dynamically linked against /lib/ld-musl-x86_64.so.1 and runs
// only on musl distros. So Linux is `-gnu` again (see postinstall.js's
// `TARGETS` comment), and the assertions below were updated to match rather
// than quietly stop testing the invariant.
//
// THE DRIFT IS THE BUG, so fixing the value without pinning it just resets the
// clock. `docs/release-contract.md`'s "Targets published" table is the source of
// truth (it is the document all three consumers cite), so these tests compare the
// shim and install.sh against THAT, not against each other -- two consumers can
// agree and both be wrong about what the release publishes.
//
// The extension lives in another repo and cannot be read from here; the contract
// doc is its proxy, which is precisely what the doc is for. Node's built-in
// runner, no dependencies:
//
//   node --test npm-shim/test/libc-mapping.test.js
//
// The FILE, not the directory: on node 26 `--test <dir>` resolves the argument as
// a module and dies with MODULE_NOT_FOUND, so the directory form silently depends
// on the runner's version.
//
// tan-cli#362 widened the same drift class from the TRIPLE to the whole asset:
// the release switched (from v0.5.0, shipped 2026-08-04) to `.zip`/`.tar.gz`
// archives of a `--onedir` freeze (#349), and this shim started asking for
// ONLY that archive name — at the time, a name no published tag carried yet,
// since every tag then, including v0.5.0-rc4, still shipped the raw
// `tan-<triple>[.exe]` this shim used to ask for. Every tag from v0.5.0 on
// ships only the archive shape now; the raw name survives as a fallback for
// v0.4.1 and earlier. So the asset-name and archive-layout tests below live
// beside the triple ones, against the same contract doc —
// .github/workflows/ci.yml runs this file by name, so a second file would
// not be run at all.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { test } = require("node:test");

const REPO_ROOT = path.join(__dirname, "..", "..");
const {
  TARGETS,
  ARCHIVE_ROOT,
  LIB_DIR,
  assetName,
  rawAssetName,
  exeName,
  tarBin,
  assertSafeEntries,
  unpackArchive,
  installArchive,
  installRaw,
  selectRelease,
} = require("../postinstall.js");

/**
 * A stand-in for a release archive: `tan/{tan[.exe], _internal/…}`, gzipped
 * with the system `tar` — the same archiver the shim unpacks with. Returns its
 * bytes. `marker` goes into the executable so a test can tell two builds apart.
 */
function fakeReleaseArchive(marker) {
  const src = fs.mkdtempSync(path.join(os.tmpdir(), "tan-shim-fixture-"));
  fs.mkdirSync(path.join(src, ARCHIVE_ROOT, "_internal"), { recursive: true });
  fs.writeFileSync(path.join(src, ARCHIVE_ROOT, exeName(process.platform)), `${marker}\n`);
  fs.writeFileSync(path.join(src, ARCHIVE_ROOT, "_internal", "base_library.zip"), "runtime\n");
  // Relative names with `cwd`, never an absolute path: MSYS/Git-Bash GNU tar
  // reads `C:\...` as a REMOTE host spec (`Cannot connect to C:`), so an
  // absolute argument here would fail on exactly the platform this repo is
  // developed on.
  execFileSync("tar", ["-czf", "tan.tar.gz", ARCHIVE_ROOT], { cwd: src });
  const bytes = fs.readFileSync(path.join(src, "tan.tar.gz"));
  fs.rmSync(src, { recursive: true, force: true });
  return bytes;
}

/**
 * CRC-32 (the zip local/central-directory checksum field), bit-by-bit --
 * fine for the few hundred bytes these fixtures are. No table, no
 * dependency: this and `buildZip` below exist because Node's stdlib has no
 * zip WRITER (only `zlib`'s raw deflate, which is a compression algorithm,
 * not a container format), and this package -- postinstall.js included --
 * deliberately carries none either (see its header comment). A STORED
 * (uncompressed) entry needs no compression codec at all, only this.
 */
function crc32(buf) {
  let crc = ~0;
  for (const byte of buf) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) {
      crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
    }
  }
  return ~crc >>> 0;
}

/**
 * A genuine ZIP archive (PK local file headers + central directory + EOCD,
 * STORED/uncompressed entries) built from raw bytes, not by renaming a
 * `.tar.gz` or shelling out to a zip-capable `tar`. `entries` is
 * `[{name, data: Buffer}]`.
 *
 * Exists for exactly one reason: a Windows e2e that names a `.tar.gz`
 * `foo.zip` also "passes" -- bsdtar CONTENT-SNIFFS, so it reads the gzip
 * stream anyway -- which proves the shim's Windows branch reaches `tar`, not
 * that `tar` can read a real `shutil.make_archive(..., format="zip")`
 * archive (tan-cli#362's adversarial review). Hand-rolling the container
 * format is the only way to get PK bytes on disk without either a new
 * dependency or a zip-writer this host may not have.
 */
function buildZip(entries) {
  const localParts = [];
  const centralParts = [];
  let offset = 0;
  for (const { name, data } of entries) {
    const nameBuf = Buffer.from(name, "utf8");
    const crc = crc32(data);

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0); // local file header signature
    local.writeUInt16LE(20, 4); // version needed to extract
    local.writeUInt16LE(0, 6); // general purpose bit flag
    local.writeUInt16LE(0, 8); // compression method: 0 = stored
    local.writeUInt16LE(0, 10); // last mod file time
    local.writeUInt16LE(0x0021, 12); // last mod file date: 1980-01-01 (DOS epoch)
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(data.length, 18); // compressed size == uncompressed (stored)
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBuf.length, 26);
    local.writeUInt16LE(0, 28); // extra field length
    localParts.push(local, nameBuf, data);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0); // central directory file header signature
    central.writeUInt16LE(20, 4); // version made by
    central.writeUInt16LE(20, 6); // version needed to extract
    central.writeUInt16LE(0, 8);
    central.writeUInt16LE(0, 10);
    central.writeUInt16LE(0, 12);
    central.writeUInt16LE(0x0021, 14);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(data.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(nameBuf.length, 28);
    central.writeUInt16LE(0, 30); // extra field length
    central.writeUInt16LE(0, 32); // file comment length
    central.writeUInt16LE(0, 34); // disk number start
    central.writeUInt16LE(0, 36); // internal file attributes
    central.writeUInt32LE(0, 38); // external file attributes
    central.writeUInt32LE(offset, 42); // offset of local header
    centralParts.push(central, nameBuf);

    offset += 30 + nameBuf.length + data.length;
  }
  const centralDir = Buffer.concat(centralParts);
  const centralStart = offset;

  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0); // end of central directory signature
  eocd.writeUInt16LE(0, 4); // this disk
  eocd.writeUInt16LE(0, 6); // disk with central directory start
  eocd.writeUInt16LE(entries.length, 8); // records on this disk
  eocd.writeUInt16LE(entries.length, 10); // total records
  eocd.writeUInt32LE(centralDir.length, 12);
  eocd.writeUInt32LE(centralStart, 16);
  eocd.writeUInt16LE(0, 20); // comment length

  return Buffer.concat([...localParts, centralDir, eocd]);
}

/** A stand-in for a release ZIP: the same `tan/{tan[.exe], _internal/…}` shape `fakeReleaseArchive` builds, as a genuine zip. */
function fakeReleaseZip(marker) {
  return buildZip([
    { name: `${ARCHIVE_ROOT}/${exeName(process.platform)}`, data: Buffer.from(`${marker}\n`) },
    { name: `${ARCHIVE_ROOT}/_internal/base_library.zip`, data: Buffer.from("runtime\n") },
  ]);
}

/** The "Targets published" table as `{ "<platform>/<arch>": [triple, asset] }`. */
function contractTargets() {
  const doc = fs.readFileSync(path.join(REPO_ROOT, "docs", "release-contract.md"), "utf8");
  const section = doc.split("## Targets published")[1];
  assert.ok(section, "docs/release-contract.md has no '## Targets published' section");

  const rows = {};
  for (const line of section.split("\n")) {
    // | `win32` | `x64` | `x86_64-pc-windows-msvc` | `tan-x86_64-pc-windows-msvc.exe` |
    const m = line.match(
      /^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|/,
    );
    if (m) {
      rows[`${m[1]}/${m[2]}`] = [m[3], m[4]];
    }
    // Stop at the next heading so the "Additional Linux assets (glibc)" table
    // below -- which lists the -gnu assets the shim must NOT serve -- cannot be
    // read as part of the platform mapping.
    if (line.startsWith("#") && Object.keys(rows).length) break;
  }
  return rows;
}

test("the shim serves exactly the contract's platform -> triple mapping", () => {
  const contract = contractTargets();
  const expected = Object.fromEntries(
    Object.entries(contract).map(([key, [triple]]) => [key, triple]),
  );

  assert.deepEqual(TARGETS, expected);
});

test("linux/x64 resolves to gnu, never musl", () => {
  // Stated separately from the table comparison because this is the invariant
  // with a failure mode: a PyInstaller `-musl` freeze is dynamically linked
  // against /lib/ld-musl-x86_64.so.1 and is not the static, any-distro
  // artefact the retired Rust `-musl` target was -- and the Python-era release
  // does not even publish one, so naming it here would 404. If the contract
  // doc is ever edited to say `-musl` here, THIS test fails rather than
  // silently following it.
  assert.equal(TARGETS["linux/x64"], "x86_64-unknown-linux-gnu");
  assert.equal(
    TARGETS["linux/arm64"],
    undefined,
    "linux/arm64 is not published from v0.5.0 -- it must not be in TARGETS at all, so resolveTarget() gives its pip-install fallback instead of a 404",
  );
  for (const [key, triple] of Object.entries(TARGETS)) {
    assert.ok(!triple.includes("musl"), `${key} must not serve a -musl asset: ${triple}`);
  }
});

test("install.sh maps Linux to the same gnu target as the shim", () => {
  const script = fs.readFileSync(path.join(REPO_ROOT, "install.sh"), "utf8");

  // install.sh composes `tan-${arch_part}-${os_part}`, so its Linux os_part is
  // the whole libc decision.
  assert.match(script, /Linux\)\s*os_part="unknown-linux-gnu"/);
  assert.ok(
    !/os_part="unknown-linux-musl"/.test(script),
    "install.sh must not compose a -musl asset name",
  );
  assert.equal(
    TARGETS["linux/x64"],
    "x86_64-unknown-linux-gnu",
    "shim and install.sh disagree for linux/x64",
  );
});

test("every asset the shim can name is one the release actually publishes", () => {
  const contract = contractTargets();
  const published = new Set(Object.values(contract).map(([, asset]) => asset));

  for (const [key, triple] of Object.entries(TARGETS)) {
    // The shim's own `assetName`, never a copy of its rule composed here: the
    // #362 bug WAS the composition (`tan-<triple>[.exe]`), so a test that
    // re-composes the name can only ever agree with whichever version of the
    // rule it was written beside.
    const asset = assetName(key.split("/")[0], triple);
    assert.ok(published.has(asset), `${key} -> ${asset} is not in the published asset table`);
  }
});

test("the shim names the contract's asset for every published target", () => {
  // Stronger than the membership check above, which a shim that served every
  // host the SAME published asset would still pass. tan-cli#362: the release
  // ships `.zip` (Windows) / `.tar.gz` (Unix) archives of a --onedir freeze
  // from v0.5.0 on (shipped 2026-08-04, #349), and this shim's ARCHIVE name
  // composer (`assetName`) must agree with the contract doc's archive column
  // even on a tag published before that shape existed (see `selectRelease`
  // in postinstall.js for the fallback that makes a pre-v0.5.0 tag, e.g.
  // v0.5.0-rc4, install anyway).
  const contract = contractTargets();
  const expected = Object.fromEntries(
    Object.entries(contract).map(([key, [, asset]]) => [key, asset]),
  );
  const actual = Object.fromEntries(
    Object.entries(TARGETS).map(([key, triple]) => [key, assetName(key.split("/")[0], triple)]),
  );

  assert.deepEqual(actual, expected);
});

test("assetName (the ARCHIVE composer) never emits a raw-binary asset name", () => {
  // The failure mode has no error of its own: a raw-binary name is a plain
  // HTTP 404 at download time against a v0.5.0+ tag, which reads like a
  // network problem rather than a naming bug. Pin the SHAPE so the next
  // reader cannot reintroduce it by editing one branch. This is a claim about
  // `assetName` specifically -- the shim's ACTUAL install picks between this
  // and `rawAssetName` per release via `selectRelease` (tan-cli#356), tested
  // separately below.
  for (const [key, triple] of Object.entries(TARGETS)) {
    const asset = assetName(key.split("/")[0], triple);
    assert.match(
      asset,
      /\.(zip|tar\.gz)$/,
      `${key} -> ${asset} is a raw-binary asset name; assetName() must compose the archive shape only (#349)`,
    );
  }
  assert.equal(assetName("win32", "x86_64-pc-windows-msvc"), "tan-x86_64-pc-windows-msvc.zip");
  assert.equal(
    assetName("linux", "x86_64-unknown-linux-gnu"),
    "tan-x86_64-unknown-linux-gnu.tar.gz",
  );
});

test("the shim unpacks the archive layout the contract documents", () => {
  const doc = fs
    .readFileSync(path.join(REPO_ROOT, "docs", "release-contract.md"), "utf8")
    .replace(/\s+/g, " ");

  // The doc's sentence and the shim's constants have to say the same thing:
  // one top-level `tan/`, holding the executable AND `_internal/`. The
  // executable does not run without that sibling, so "which entry do I keep"
  // is not a free choice for a consumer.
  assert.match(doc, /one top-level entry is `tan\/`/);
  assert.match(doc, /`_internal\/`/);
  assert.equal(ARCHIVE_ROOT, "tan");
  assert.equal(exeName("win32"), "tan.exe");
  assert.equal(exeName("linux"), "tan");
  assert.equal(exeName("darwin"), "tan");
  // The launcher (`bin/tan.js`, this package's `bin` entry) imports LIB_DIR
  // from the installer, so the stable path is single-sourced; assert it still
  // sits inside the package rather than somewhere npm will not ship or clean.
  assert.equal(path.basename(LIB_DIR), "tan-cli-lib");
  assert.equal(path.dirname(LIB_DIR), path.join(REPO_ROOT, "npm-shim"));
});

test("extraction refuses anything that escapes the destination", () => {
  // Unit-level on purpose: `tar` REWRITES these names at create time (GNU tar
  // strips a leading `../` when packing), so an archive fixture cannot carry
  // them without a tar-specific escape hatch that differs between GNU tar and
  // bsdtar. The listing is what the check consumes, so drive the listing.
  const layout = ["tan/", "tan/tan", "tan/_internal/", "tan/_internal/base_library.zip"];
  assert.doesNotThrow(() => assertSafeEntries(layout));
  assert.doesNotThrow(() => assertSafeEntries(layout.map((e) => `./${e}`)));

  for (const evil of [
    "../evil",
    "tan/../../evil",
    "tan/_internal/../../../evil",
    "/etc/cron.d/evil", // absolute, POSIX — must be rejected while running on Windows
    "C:\\Windows\\System32\\evil.dll", // absolute, Windows — rejected while running on POSIX
    "\\\\server\\share\\evil",
    "not-tan/tan", // a second top-level tree is a layout change, not our archive
  ]) {
    assert.throws(
      () => assertSafeEntries([evil]),
      /refusing to extract|unexpected entry/,
      `${evil} was accepted`,
    );
  }
});

test("a real archive unpacks to the launcher's executable plus its runtime", () => {
  // End-to-end over the system archiver, which is the whole reason this shim
  // has no dependency: node's stdlib reads neither tar nor zip.
  const dest = fs.mkdtempSync(path.join(os.tmpdir(), "tan-shim-dest-"));
  try {
    const archive = path.join(dest, "tan-x86_64-unknown-linux-gnu.tar.gz");
    fs.writeFileSync(archive, fakeReleaseArchive("build-1"));

    const unpacked = unpackArchive(archive, dest);
    assert.equal(unpacked, path.join(dest, ARCHIVE_ROOT, exeName(process.platform)));
    assert.ok(fs.existsSync(unpacked), "executable missing after extraction");
    assert.ok(
      fs.existsSync(path.join(dest, ARCHIVE_ROOT, "_internal", "base_library.zip")),
      "_internal/ runtime missing after extraction — the executable cannot run without it",
    );
  } finally {
    fs.rmSync(dest, { recursive: true, force: true });
  }
});

test("an archive without the freeze's runtime is rejected, not installed", () => {
  // A `tan` with no `_internal/` starts and dies inside PyInstaller's loader,
  // which reads as a broken binary rather than a broken download. Catch it
  // while the cause is still on screen.
  const src = fs.mkdtempSync(path.join(os.tmpdir(), "tan-shim-broken-"));
  const dest = fs.mkdtempSync(path.join(os.tmpdir(), "tan-shim-broken-dest-"));
  try {
    fs.mkdirSync(path.join(src, ARCHIVE_ROOT));
    fs.writeFileSync(path.join(src, ARCHIVE_ROOT, exeName(process.platform)), "#!/bin/sh\n");
    execFileSync("tar", ["-czf", "tan.tar.gz", ARCHIVE_ROOT], { cwd: src });

    assert.throws(() => unpackArchive(path.join(src, "tan.tar.gz"), dest), /_internal/);
  } finally {
    fs.rmSync(src, { recursive: true, force: true });
    fs.rmSync(dest, { recursive: true, force: true });
  }
});

test("install lands the whole tree at the launcher's path, and replaces it on upgrade", () => {
  // Installs into a temp dir rather than the package's real `tan-cli-lib/`:
  // `installArchive` takes the destination precisely so this can be exercised
  // without a ~40 MB freeze appearing in a working tree.
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tan-shim-install-"));
  const libDir = path.join(home, "tan-cli-lib");
  const exe = path.join(libDir, exeName(process.platform));
  try {
    installArchive(fakeReleaseArchive("build-1"), "tan-x86_64-apple-darwin.tar.gz", libDir);
    assert.equal(fs.readFileSync(exe, "utf8").trim(), "build-1");
    assert.ok(fs.existsSync(path.join(libDir, "_internal")), "runtime not installed beside the exe");

    // The upgrade path is where a delete-then-extract implementation loses the
    // working install: the second install must REPLACE the tree, not merge
    // into it or fail on the existing directory.
    installArchive(fakeReleaseArchive("build-2"), "tan-x86_64-apple-darwin.tar.gz", libDir);
    assert.equal(fs.readFileSync(exe, "utf8").trim(), "build-2");

    // Nothing left behind: no staging dir, no `tan/` beside `tan-cli-lib/`,
    // no downloaded archive. `tan-cli-lib` is the only thing installed.
    assert.deepEqual(fs.readdirSync(home), ["tan-cli-lib"]);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// tan-cli#356 (shape selection) -- item 1 of the adversarial review that
// found the shim 404ing at its own pinned version, v0.5.0-rc4: `assetName`
// alone can only ever ask for the archive shape, which no tag published today
// carries. `selectRelease` is what actually decides, from checksums.txt, the
// same way install.sh's `digest_for` / install.ps1's `Get-DigestFor` do.
// ---------------------------------------------------------------------------

test("selectRelease prefers the archive asset, falls back to the raw one, per checksums.txt", () => {
  const platform = "linux";
  const triple = "x86_64-unknown-linux-gnu";
  const archive = assetName(platform, triple);
  const raw = rawAssetName(platform, triple);
  const archiveDigest = "a".repeat(64);
  const rawDigest = "b".repeat(64);

  // v0.5.0-rc4 and earlier: checksums.txt lists only the raw name.
  let result = selectRelease(`${rawDigest}  ${raw}\n`, platform, triple);
  assert.deepEqual(result, { asset: raw, layout: "raw", digest: rawDigest });

  // v0.5.0 and later: both names could in principle appear (a release that
  // ships an archive still names the triple the same way); archive wins, the
  // same order install.sh's digest_for tries them in.
  result = selectRelease(`${archiveDigest}  ${archive}\n${rawDigest}  ${raw}\n`, platform, triple);
  assert.deepEqual(result, { asset: archive, layout: "archive", digest: archiveDigest });

  // Neither name listed: refuse, naming BOTH candidates -- not just the one
  // this shim happened to try first.
  assert.throws(
    () => selectRelease("", platform, triple),
    (error) => error.message.includes(archive) && error.message.includes(raw),
    "error must name both the archive and the raw candidate",
  );
});

test("rawAssetName matches install.sh's raw_asset / install.ps1's rawAsset composition", () => {
  // Textual cross-check, same style as the Linux-libc test above: a rename on
  // either side that the other doesn't follow is exactly the class of drift
  // this file exists to catch (tan-cli#356, mirroring #362's triple-drift).
  const sh = fs.readFileSync(path.join(REPO_ROOT, "install.sh"), "utf8");
  const ps1 = fs.readFileSync(path.join(REPO_ROOT, "install.ps1"), "utf8");
  assert.match(sh, /raw_asset="tan-\$\{arch_part\}-\$\{os_part\}"/);
  assert.match(ps1, /\$rawAsset\s*=\s*"tan-\$archPart-pc-windows-msvc\.exe"/);
  // And install.sh tries the archive name before falling back to the raw one
  // (`asset="$archive_asset"` ... `asset="$raw_asset"`, in that order) --
  // selectRelease above must try them in the same order.
  const archiveIdx = sh.indexOf('asset="$archive_asset"');
  const rawIdx = sh.indexOf('asset="$raw_asset"');
  assert.ok(archiveIdx >= 0 && rawIdx >= 0 && archiveIdx < rawIdx);

  assert.equal(rawAssetName("linux", "x86_64-unknown-linux-gnu"), "tan-x86_64-unknown-linux-gnu");
  assert.equal(rawAssetName("darwin", "aarch64-apple-darwin"), "tan-aarch64-apple-darwin");
  assert.equal(rawAssetName("win32", "x86_64-pc-windows-msvc"), "tan-x86_64-pc-windows-msvc.exe");
});

test("install lands a raw asset directly at the launcher's path (pre-v0.5.0 tags, e.g. v0.5.0-rc4)", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tan-shim-raw-install-"));
  const libDir = path.join(home, "tan-cli-lib");
  const exe = path.join(libDir, exeName(process.platform));
  try {
    installRaw(Buffer.from("raw-build-1\n"), libDir);
    assert.equal(fs.readFileSync(exe, "utf8").trim(), "raw-build-1");

    // Upgrading a raw install (rc -> rc) must replace it cleanly too.
    installRaw(Buffer.from("raw-build-2\n"), libDir);
    assert.equal(fs.readFileSync(exe, "utf8").trim(), "raw-build-2");
    assert.deepEqual(fs.readdirSync(home), ["tan-cli-lib"]);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test("upgrading from a raw install to an archive install replaces it cleanly (v0.5.0-rc4 -> v0.5.0)", () => {
  // The real transition a `tan-cli-lib` on disk goes through: today's raw
  // pre-release, then the archive shape once v0.5.0 cuts. `swapIntoPlace`
  // must not care that the shapes differ.
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tan-shim-shape-upgrade-"));
  const libDir = path.join(home, "tan-cli-lib");
  const exe = path.join(libDir, exeName(process.platform));
  try {
    installRaw(Buffer.from("rc4-build\n"), libDir);
    installArchive(fakeReleaseArchive("final-build"), "tan-x86_64-apple-darwin.tar.gz", libDir);
    assert.equal(fs.readFileSync(exe, "utf8").trim(), "final-build");
    assert.ok(fs.existsSync(path.join(libDir, "_internal")), "the archive install must land its runtime too");
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// Item 5 of the adversarial review: a failed rollback must not delete the
// only surviving copy of the previous install.
// ---------------------------------------------------------------------------

test("a failed rollback preserves the previous install instead of deleting it", (t) => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "tan-shim-rollback-"));
  const libDir = path.join(home, "tan-cli-lib");
  try {
    // A previous, working install already in place.
    installArchive(fakeReleaseArchive("good-build"), "tan-x86_64-apple-darwin.tar.gz", libDir);

    const originalRename = fs.renameSync.bind(fs);
    let call = 0;
    t.mock.method(fs, "renameSync", (...args) => {
      call += 1;
      // call 1: libDir -> stage/previous (moving the old tree aside). Let it
      // happen for real, so there is something to protect.
      if (call === 1) return originalRename(...args);
      // call 2: stage/tan -> libDir (the swap itself). Simulate the target
      // being locked, e.g. a running `tan` from the old install still has
      // libDir/tan open (the real EBUSY/EPERM this dance exists for).
      if (call === 2) throw new Error("simulated EBUSY");
      // call 3: the rollback (stage/previous -> libDir). Simulate it ALSO
      // failing -- the case this test exists for.
      throw new Error("simulated EPERM (rollback)");
    });

    let thrown;
    try {
      installArchive(fakeReleaseArchive("new-build"), "tan-x86_64-apple-darwin.tar.gz", libDir);
    } catch (error) {
      thrown = error;
    }
    assert.ok(thrown, "installArchive did not throw when both the swap and the rollback failed");
    assert.match(thrown.message, /could not restore the previous install/);

    // The surviving copy: wherever call 1 actually renamed the old libDir to.
    const previousPath = fs.renameSync.mock.calls[0].arguments[1];
    assert.ok(
      thrown.message.includes(previousPath),
      "the error must name where the surviving install actually is",
    );
    assert.ok(
      fs.existsSync(previousPath),
      "the only surviving copy of the pre-upgrade install was deleted by cleanup",
    );
    assert.equal(
      fs.readFileSync(path.join(previousPath, exeName(process.platform)), "utf8").trim(),
      "good-build",
      "the surviving copy is not the pre-upgrade install",
    );
  } finally {
    t.mock.restoreAll();
    fs.rmSync(home, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// Item 6 of the adversarial review: a real zip fixture, not a renamed
// tar.gz -- and a cross-check that the shape rule and layout constants agree
// with install.sh's.
// ---------------------------------------------------------------------------

test("fakeReleaseZip produces a genuine PK zip, not a gzip stream wearing the extension", () => {
  // Runs on every platform/CI runner, unlike the extraction test below, which
  // needs a zip-capable tar. This is the assertion that would have caught
  // #362's own gap directly: a renamed `.tar.gz` starts with gzip's `1f 8b`,
  // never with a zip local-file-header signature.
  const bytes = fakeReleaseZip("marker");
  assert.deepEqual([...bytes.subarray(0, 4)], [0x50, 0x4b, 0x03, 0x04], "missing PK\\x03\\x04 zip signature");
  assert.notDeepEqual([...bytes.subarray(0, 2)], [0x1f, 0x8b], "fixture is a gzip stream, not a zip");
});

test("a real .zip archive extracts through the exact tar binary the shim resolves", (t) => {
  // Deliberately uses `tarBin()` -- the shim's own resolution, System32's
  // bsdtar on Windows rather than whatever `tar` PATH turns up first -- not a
  // hardcoded binary name, so this exercises the actual codepath a Windows
  // install runs, not a stand-in for it.
  const bin = tarBin();
  let versionOut = "";
  try {
    versionOut = execFileSync(bin, ["--version"], { encoding: "utf8" });
  } catch (error) {
    t.skip(`could not run ${bin} --version: ${error.message}`);
    return;
  }
  if (!/bsdtar/i.test(versionOut)) {
    // GNU tar -- the Linux CI runner's PATH `tar` -- cannot read zip AT ALL;
    // it answers "This does not look like a tar archive". That is a real,
    // already-documented limitation (see postinstall.js's `tarBin()`
    // comment), not a shim bug: Linux never downloads a `.zip` in
    // production, only Windows does, which is exactly why `tarBin()` picks
    // bsdtar there specifically. Skip rather than fail or fake a pass.
    t.skip(`${bin} is not bsdtar (zip-capable): ${versionOut.split("\n")[0]}`);
    return;
  }

  const dest = fs.mkdtempSync(path.join(os.tmpdir(), "tan-shim-zip-dest-"));
  try {
    const archive = path.join(dest, "tan-x86_64-pc-windows-msvc.zip");
    fs.writeFileSync(archive, fakeReleaseZip("zip-1"));

    const unpacked = unpackArchive(archive, dest);
    assert.equal(fs.readFileSync(unpacked, "utf8").trim(), "zip-1");
    assert.ok(
      fs.existsSync(path.join(dest, ARCHIVE_ROOT, "_internal", "base_library.zip")),
      "_internal/ runtime missing after extracting a real zip",
    );
  } finally {
    fs.rmSync(dest, { recursive: true, force: true });
  }
});
