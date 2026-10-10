# What `tan bootstrap` verifies about the cross toolchain, and what the stamp means

Read from zephyr `scripts/west_commands/sdk.py` (v4.4.1) and the SDK's `setup.sh`:

- `west sdk install` downloads the release's `sha256.sum`, looks up the one
  minimal-SDK bundle it is about to download, and raises `sha256 mismatched` if
  that archive's digest differs. That is the only archive west verifies, and it
  deletes the bytes afterwards.
- The per-toolchain archives (`arm-zephyr-eabi`, ...) are fetched afterwards by
  the SDK's own `setup.sh` / `setup.cmd` with `wget` and no integrity check.
- West's comparison is against the release's own `sha256.sum`, never against the
  digest alp-sdk pins in `metadata/toolchains.json`.

What `tan bootstrap` does about it (tan-cli#1496):

1. Runs `west sdk install --no-gnu-toolchains --no-hosttools`, so west installs
   only the minimal bundle it verifies.
2. Downloads the pinned `arm-zephyr-eabi` archive itself, hashes it against the
   alp-sdk pin while streaming, and extracts it into `gnu/arm-zephyr-eabi` only
   on a match. A mismatch refuses with `bootstrap.toolchain-pin-mismatch`; a
   download that cannot complete refuses with `bootstrap.toolchain-pin-unverified`.
3. Compares the alp-sdk pins with the release `sha256.sum` before west runs and
   re-fetches it afterwards; the two copies must be byte-identical. The sum parser
   is fail-closed: it refuses marker-prefixed, path-prefixed, uppercase, duplicate
   or otherwise ambiguous entries for a pinned file.

What remains unverified: the minimal bundle (cmake files, `setup.sh`) is checked
only through the release sum, because tan never holds its bytes; and nothing is
re-hashed from disk after install. A stamp with `pinChecked: true` means step 2
passed; a stamp without it was written by a `tan` that never compared the
archives with the pin.
