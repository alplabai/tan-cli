# What `west sdk install` verifies, and what the toolchain stamp means

Read from zephyr `scripts/west_commands/sdk.py` (v4.4.1), not run:

- west downloads the release's `sha256.sum`, looks up the one minimal-SDK
  bundle it is about to download, and raises `sha256 mismatched` if that
  archive's digest differs.
- The per-toolchain archives (`arm-zephyr-eabi`, ...) are fetched afterwards by
  the SDK's own `setup.sh` / `setup.cmd`, which west runs but does not
  re-verify. West therefore checks the minimal bundle only, not every archive.
- Either way the comparison is against the release's own `sha256.sum`, never
  against the digest alp-sdk pins in `metadata/toolchains.json`.

So the verification stamp `tan bootstrap` writes means "the `sdk_version` file
matches the pin and the compiler runs". It does not mean the archive bytes
match alp-sdk's pinned sha256 (tracked in #1496).
