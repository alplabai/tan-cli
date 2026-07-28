#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Toolchain-lock byte-parity gate (tan-cli#172): the vendored copy of
alp-sdk's `metadata/toolchains.json` vs. the pinned alp-sdk checkout's own
copy.

`metadata/toolchains.json` is alp-sdk's single source of truth for the
pinned Zephyr SDK release (issue #949 item 3) -- its `_comment` names
`scripts/check_toolchain_lock.py` as the drift gate that keeps every CI
*workflow* copy of that pin in lockstep. That gate's own scope is workflows
under `.github/workflows/*.yml` -- it does not, and cannot, see a tan-cli
checkout, so `crates/tan-core/src/host_env.rs`'s `ZEPHYR_SDK_INSTALL_VERSION`
(the version `tan doctor`'s `zephyrSdk` check prints in its `west sdk
install --version <..>` remedy, tan-cli#160) is a hand-ported copy of the
SAME fact, on the side that gate cannot see. This script is the tan-cli-side
half. It byte-diffs the vendored copy at
`contract/fixtures/toolchains/toolchains.json` -- which
`crates/tan-core/src/host_env.rs` `include_str!`s -- against the pinned
alp-sdk checkout's `metadata/toolchains.json`. Note the relative paths
deliberately DIFFER between the two repos (unlike `kconfig_fixture_parity.py`,
where they match): the vendored copy is a test FIXTURE here, not SDK
metadata, so it lives under `contract/fixtures/`, the same convention
`bootstrap_manifest_parity.py` uses for `metadata/bootstrap.json`.

What the byte-diff buys, together with `cargo test`: `host_env.rs`'s
`zephyr_sdk_install_version_matches_the_real_toolchain_lock` asserts
`ZEPHYR_SDK_INSTALL_VERSION` equals the vendored fixture's `zephyrSdk.version`
field. So this gate failing means the FIXTURE went stale, and re-vendoring it
will then fail that cargo test until `ZEPHYR_SDK_INSTALL_VERSION` is updated
too -- exactly the chain that keeps a released `tan` honest about an
SDK-side pin bump.

Optionally self-skipping, same shape as `bootstrap_manifest_parity.py` /
`kconfig_fixture_parity.py`: `tan-core`'s own `cargo test` already proves the
vendored copy parses and carries a version matching the constant, without an
SDK checkout -- a local dev-loop run with no reachable alp-sdk is a clean
no-op, not a failure. Reachability is resolved in the same order: `--sdk`,
then `$ALP_SDK_ROOT`, then an `alp-sdk` checkout next to this tan-cli
checkout.

A byte MISMATCH (vendored copy differs from the pinned upstream file) always
fails -- that is the actual drift this gate exists to catch. There is no
"absent upstream / predates the feature" branch the way the bootstrap/kconfig
gates have: `metadata/toolchains.json` already exists at every ref this
repo's `PINNED_SDK_TAG` has ever pointed at, so an upstream file missing at
the pinned ref would itself be a real regression, not a legitimate skip.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

# The paths differ per repo: SDK metadata upstream, a test fixture here.
UPSTREAM_RELPATH = Path("metadata/toolchains.json")
VENDORED_RELPATH = Path("contract/fixtures/toolchains/toolchains.json")

VENDORED_PATH = Path(__file__).resolve().parent.parent.parent / VENDORED_RELPATH


def _looks_like_sdk_checkout(path: Path) -> bool:
    return (path / "scripts" / "alp_orchestrate").is_dir()


def resolve_sdk_root(explicit: Path | None) -> Path | None:
    """Find a reachable alp-sdk checkout: `--sdk`, then `$ALP_SDK_ROOT`, then a
    `../alp-sdk` sibling of this tan-cli checkout. `None` if none resolves --
    the caller treats that as a clean skip, not a failure."""
    candidates = []
    if explicit is not None:
        candidates.append(explicit)
    env_root = os.environ.get("ALP_SDK_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(Path(__file__).resolve().parent.parent.parent.parent / "alp-sdk")

    for candidate in candidates:
        candidate = candidate.resolve()
        if _looks_like_sdk_checkout(candidate):
            return candidate
    return None


def run(sdk_root: Path) -> bool:
    upstream_path = sdk_root / UPSTREAM_RELPATH

    if not VENDORED_PATH.is_file():
        print(f"FAIL: vendored toolchain lock missing at {VENDORED_PATH}")
        return False
    if not upstream_path.is_file():
        print(f"FAIL: no {UPSTREAM_RELPATH} in this alp-sdk checkout ({upstream_path}) -- "
              f"metadata/toolchains.json (alp-sdk issue #949 item 3) is expected to exist at "
              f"every ref this repo's PINNED_SDK_TAG points at.")
        return False

    vendored = VENDORED_PATH.read_bytes()
    upstream = upstream_path.read_bytes()
    if vendored != upstream:
        print(f"FAIL: {VENDORED_PATH} differs from upstream {upstream_path} "
              f"-- re-vendor the toolchain lock, then re-run `cargo test -p tan-core "
              f"host_env::` (ZEPHYR_SDK_INSTALL_VERSION in "
              f"crates/tan-core/src/host_env.rs is asserted against this fixture and will "
              f"need a matching update if zephyrSdk.version changed)")
        return False

    print(f"PASS: {VENDORED_RELPATH} is byte-identical to upstream "
          f"{UPSTREAM_RELPATH} ({len(vendored)} bytes)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=None,
                         help="Path to the alp-sdk checkout to compare the "
                              "vendored toolchain lock against. Falls back to "
                              "$ALP_SDK_ROOT, then an alp-sdk checkout next "
                              "to this tan-cli checkout.")
    args = parser.parse_args(argv)

    sdk_root = resolve_sdk_root(args.sdk)
    if sdk_root is None:
        print("SKIP: no alp-sdk checkout reachable (--sdk / $ALP_SDK_ROOT / "
              "a sibling alp-sdk checkout); toolchain-lock byte-parity not "
              "checked this run.")
        return 0

    return 0 if run(sdk_root) else 1


if __name__ == "__main__":
    raise SystemExit(main())
