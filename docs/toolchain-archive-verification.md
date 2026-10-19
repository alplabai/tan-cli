# What `tan bootstrap` verifies about the cross toolchain, and what the stamp means

`tan bootstrap`'s toolchain phase installs two archives that alp-sdk pins by
sha256 in `metadata/toolchains.json`: the Zephyr SDK *minimal bundle*
(`sdk_version`, `cmake/`, `setup.sh`/`setup.cmd`) and the `arm-zephyr-eabi`
toolchain. Since tan-cli#1556 it downloads both itself:

1. It fetches the release's own `sha256.sum` and compares alp-sdk's pins with it
   before downloading anything. A disagreement refuses with
   `bootstrap.toolchain-pin-mismatch`, naming both hashes; an unreachable sum
   refuses with `bootstrap.toolchain-pin-unverified`. The sum parser is
   fail-closed: it refuses marker-prefixed, path-prefixed, uppercase, duplicate
   or otherwise ambiguous entries for a pinned file.
2. It downloads the minimal bundle, hashes it against the pin while streaming,
   extracts it into a scratch directory, requires exactly one top-level
   directory, and moves that into place.
3. It downloads the toolchain archive, hashes it the same way, and extracts it
   into `gnu/arm-zephyr-eabi`, the layout the SDK's own `setup.sh -t` produces.

A hash mismatch refuses with `bootstrap.toolchain-pin-mismatch` and is never
retried. A transport failure refuses with `bootstrap.toolchain-pin-unverified`
after three attempts. An asset upstream does not serve (HTTP 404/410) refuses
with `bootstrap.toolchain-install` and is not retried. After that, the phase
re-reads `sdk_version` and runs `arm-zephyr-eabi-gcc --version` from the new
tree before moving it into the store and stamping it.

What remains unverified: nothing is re-hashed from disk after install.

## What `west sdk install` used to do, and why tan stopped using it

Before tan-cli#1556, tan ran `west sdk install --no-gnu-toolchains
--no-hosttools --install-dir <tmp>`. Read from zephyr
`scripts/west_commands/sdk.py` (v4.4.1) and the SDK's `setup.sh`/`setup.cmd`,
that did four things:

- Listed releases through the rate-limited GitHub API (`fetch_releases`).
- Looked for an already-installed SDK of the same version (`fetch_sdk_info`,
  i.e. CMake's `FindZephyr-sdk`). When it found one, it ignored
  `--install-dir` and used that copy instead (tan-cli#1552, #1556).
- Downloaded the minimal bundle and checked it against the release
  `sha256.sum`, never against alp-sdk's pin. tan never saw its bytes.
- Ran `setup -c` (with those flags, nothing else), which registered the
  temporary install directory in the CMake package registry. That registration
  was dead as soon as tan moved the directory into its store (tan-cli#1553).

tan's own download does the third step against the pin and skips the other
three.

## The stamp

The stamp records what its install was checked against, and `tan doctor` and
`tan bootstrap` describe each generation accordingly
(`toolchain_provision.stamp_provenance`):

| Stamp | Written by | Meaning |
|---|---|---|
| `bundlePinChecked: true` | tan-cli#1556 and later | both archives were hashed against the alp-sdk pin |
| `pinChecked: true` only | tan-cli#1496 to #1556 | the toolchain archive was pin-hashed; the minimal bundle was checked by west against the release sum only |
| neither | before tan-cli#1496 | the archives were never compared with the pin |
