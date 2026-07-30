// SPDX-License-Identifier: Apache-2.0
//
// The npm shim, install.sh and the VS Code extension all map a host to a release
// asset, and for a while they disagreed: this shim served `-gnu` for Linux while
// install.sh and the extension served `-musl`, and the shim's own comment claimed
// the three were identical. A musl host therefore downloaded a glibc binary that
// cannot execute, and the comment sent the next reader looking somewhere else.
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
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");

const REPO_ROOT = path.join(__dirname, "..", "..");
const { TARGETS } = require("../postinstall.js");

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

test("both Linux arches resolve to musl, never gnu", () => {
  // Stated separately from the table comparison because this is the invariant
  // with a failure mode: a `-gnu` asset carries a glibc floor and does not
  // execute on a musl distro at all. If the contract doc is ever edited to say
  // `-gnu` here, THIS test fails rather than silently following it.
  assert.equal(TARGETS["linux/x64"], "x86_64-unknown-linux-musl");
  assert.equal(TARGETS["linux/arm64"], "aarch64-unknown-linux-musl");
  for (const [key, triple] of Object.entries(TARGETS)) {
    assert.ok(!triple.includes("linux-gnu"), `${key} must not serve a -gnu asset: ${triple}`);
  }
});

test("install.sh maps Linux to the same musl target as the shim", () => {
  const script = fs.readFileSync(path.join(REPO_ROOT, "install.sh"), "utf8");

  // install.sh composes `tan-${arch_part}-${os_part}`, so its Linux os_part is
  // the whole libc decision.
  assert.match(script, /Linux\)\s*os_part="unknown-linux-musl"/);
  assert.ok(
    !/os_part="unknown-linux-gnu"/.test(script),
    "install.sh must not compose a -gnu asset name",
  );
  for (const arch of ["aarch64", "x86_64"]) {
    assert.ok(
      TARGETS[`linux/${arch === "aarch64" ? "arm64" : "x64"}`] === `${arch}-unknown-linux-musl`,
      `shim and install.sh disagree for linux/${arch}`,
    );
  }
});

test("every asset the shim can name is one the release actually publishes", () => {
  const contract = contractTargets();
  const published = new Set(Object.values(contract).map(([, asset]) => asset));

  for (const [key, triple] of Object.entries(TARGETS)) {
    const asset = `tan-${triple}${key.startsWith("win32/") ? ".exe" : ""}`;
    assert.ok(published.has(asset), `${key} -> ${asset} is not in the published asset table`);
  }
});
