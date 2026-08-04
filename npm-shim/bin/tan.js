#!/usr/bin/env node
// SPDX-License-Identifier: Apache-2.0
//
// Thin launcher: forwards argv to the native `tan` executable unpacked by
// postinstall.js, inheriting stdio and the exit code.
//
// From tan-cli#362 the install is a PyInstaller `--onedir` tree, not a lone
// binary: `tan-cli-lib/{tan[.exe], _internal/}`. LIB_DIR and the executable
// name are IMPORTED from postinstall.js (which does nothing on require — it is
// guarded by `require.main === module`) rather than re-derived here, because a
// launcher and an installer that each compute the path separately is how you
// get a successful install with a `tan` that cannot find its binary.

const path = require("path");
const { spawnSync } = require("child_process");

const { LIB_DIR, exeName } = require("../postinstall.js");

const binPath = path.join(LIB_DIR, exeName(process.platform));

const result = spawnSync(binPath, process.argv.slice(2), { stdio: "inherit" });

if (result.error) {
  const hint =
    result.error.code === "ENOENT"
      ? "@alplabai/tan: native binary not found — reinstall the package to re-run its postinstall step."
      : `@alplabai/tan: failed to launch native binary: ${result.error.message}`;
  console.error(hint);
  process.exit(1);
}

process.exit(result.status === null ? 1 : result.status);
