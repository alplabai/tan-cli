#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Bootstrap manifest byte-parity gate (alp-sdk#917): the vendored copy of
alp-sdk's `metadata/bootstrap.json` vs. the pinned alp-sdk checkout's own copy.

`metadata/bootstrap.json` is the SDK's single source of truth for the
workspace-assembly FACTS that `scripts/bootstrap.sh`, `scripts/bootstrap.ps1`
and `tan bootstrap` all need -- the Zephyr pin, venv layout, prerequisite
lists + Python floor, the `west` pip spec and argv, the pip package sets, the
`env` map and the per-OS native-lib hints. Its own `_comment` names tan as a
real reader of those facts since tan-cli PR #55 --
`crates/tan-core/src/bootstrap/manifest.rs`'s `parse_bootstrap_manifest` is
that reader.

alp-sdk polices its two scripts against that manifest with
`scripts/check_bootstrap_manifest.py`, but that gate cannot see a tan-cli
checkout: nothing on the alp-sdk side catches
`crates/tan-core/src/bootstrap/manifest.rs` drifting from the producer. This
script is the tan-cli-side half. It byte-diffs the vendored copy at
`contract/fixtures/bootstrap/manifest.json` -- which `manifest.rs` and
`crates/tan-cli/src/commands/bootstrap/mod.rs` both `include_str!` -- against
the pinned alp-sdk checkout's `metadata/bootstrap.json`. Note the relative
paths deliberately DIFFER between the two repos (unlike
`kconfig_fixture_parity.py`, where they match): the vendored copy is a test
FIXTURE here, not SDK metadata, so it lives under `contract/fixtures/`.

What the byte-diff buys, together with `cargo test`: `manifest.rs`'s
`the_fallback_matches_the_real_manifest_field_for_field` asserts the
hand-ported fallback constants (used against any SDK predating #917) equal
the vendored fixture field-for-field. So this gate failing means the FIXTURE
went stale, and re-vendoring it will then fail that cargo test until the
fallback constants are updated too -- which is exactly the chain that keeps a
released `tan` honest about an SDK-side pin bump.

Optionally self-skipping, same as `scaffold_byte_parity.py` /
`kconfig_fixture_parity.py`: `tan-core`'s own `cargo test` already proves the
vendored copy parses and round-trips without an SDK checkout, so a local
dev-loop run with no reachable alp-sdk is a clean no-op, not a failure.
Reachability is resolved in the same order: `--sdk`, then `$ALP_SDK_ROOT`,
then an `alp-sdk` checkout next to this tan-cli checkout.

A SECOND non-failure case, same shape as the kconfig gate's: the manifest
absent at the ref `parity.yml` pins. `PINNED_SDK_TAG` is now well past
alp-sdk#917 landing this manifest (it is at #967, which with #961 rewrote
`manualInstallHints.windows.note` and bumped `zephyr.version` to v4.4.1), so
this gate byte-diffs for real in CI today; this branch is dead against that pin
and only fires for an OLDER ref used locally (e.g. one predating #917). A byte
MISMATCH (manifest present upstream, content differs) always fails regardless
of the pin -- that is the actual drift this gate exists to catch.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

# The paths differ per repo: SDK metadata upstream, a test fixture here.
UPSTREAM_RELPATH = Path("metadata/bootstrap.json")
VENDORED_RELPATH = Path("contract/fixtures/bootstrap/manifest.json")

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
        print(f"FAIL: vendored manifest missing at {VENDORED_PATH}")
        return False
    if not upstream_path.is_file():
        # NOT a fail: `PINNED_SDK_TAG` (parity.yml) is a hand-bumped fixed ref,
        # and a ref predating alp-sdk#917 introducing this manifest has no
        # such file at all. That is "not yet applicable", not drift -- unlike
        # a byte MISMATCH below, which means it existed and something
        # changed. `parity.yml`'s current pin is past #917, so in CI this
        # branch is dead; it only fires for an older ref used locally.
        print(f"NOTICE: no manifest at {upstream_path} in this alp-sdk ref -- "
              f"pinned ref predates alp-sdk#917 landing "
              f"metadata/bootstrap.json; not treated as drift until the pin "
              f"is bumped past it.")
        return True

    vendored = VENDORED_PATH.read_bytes()
    upstream = upstream_path.read_bytes()
    if vendored != upstream:
        print(f"FAIL: {VENDORED_PATH} differs from upstream {upstream_path} "
              f"-- re-vendor the manifest, then re-run `cargo test -p "
              f"tan-core bootstrap::manifest` (the hand-ported fallback "
              f"constants in crates/tan-core/src/bootstrap/manifest.rs are "
              f"asserted field-for-field against this fixture and will need "
              f"the same update; a new or renamed manifest FIELD also needs "
              f"parse_bootstrap_manifest and BootstrapFacts extending)")
        return False

    print(f"PASS: {VENDORED_RELPATH} is byte-identical to upstream "
          f"{UPSTREAM_RELPATH} ({len(vendored)} bytes)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=None,
                         help="Path to the alp-sdk checkout to compare the "
                              "vendored manifest against. Falls back to "
                              "$ALP_SDK_ROOT, then an alp-sdk checkout next "
                              "to this tan-cli checkout.")
    args = parser.parse_args(argv)

    sdk_root = resolve_sdk_root(args.sdk)
    if sdk_root is None:
        print("SKIP: no alp-sdk checkout reachable (--sdk / $ALP_SDK_ROOT / "
              "a sibling alp-sdk checkout); bootstrap manifest byte-parity "
              "not checked this run.")
        return 0

    return 0 if run(sdk_root) else 1


if __name__ == "__main__":
    raise SystemExit(main())
