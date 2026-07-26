#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Kconfig fixture byte-parity gate (alp-sdk#893/#894): the vendored copy of
alp-sdk's canonical `--emit kconfig` contract anchor vs. the pinned alp-sdk
checkout's own copy.

`crates/tan-core/src/kconfig.rs` and `crates/tan-cli/src/commands/kconfig.rs`
both `include_str!` a vendored byte-copy of alp-sdk's
`tests/fixtures/kconfig-contract/emit-kconfig.golden.json` (this repo's own
copy lives at the same relative path,
`tests/fixtures/kconfig-contract/emit-kconfig.golden.json`) to test
`parse_kconfig`/`Envelope<KconfigData>` against the SDK's real field
contract without a Zephyr/west workspace. That vendored copy can drift from
upstream exactly like the wizard-scaffold vendoring `scaffold_byte_parity.py`
guards (see that script + `tests/parity/README.md`'s "Scaffold byte-parity"
section) — a future SDK field rename would go unnoticed here forever, the
same class of silent cross-repo drift ADR-0020 exists to kill. This script
is the tan-cli-side gate: byte-diff the vendored copy against the pinned
alp-sdk checkout's own copy of the same fixture.

Unlike `seam1_field_diff.py` (which hard-requires `--sdk`), this gate is
optionally self-skipping, same as `scaffold_byte_parity.py`: `tan-core`'s own
`cargo test` (`parse_kconfig_accepts_the_canonical_alp_sdk_fixture` /
`canonical_alp_sdk_fixture_round_trips_into_the_kconfig_envelope`) already
proves the vendored copy is internally consistent (deserializes, round-trips
through the envelope) without an SDK checkout — a local dev-loop run with no
reachable alp-sdk checkout is a clean no-op, not a failure. Reachability is
checked in the same order as `scaffold_byte_parity.py`: `--sdk`, then
`$ALP_SDK_ROOT`, then an `alp-sdk` checkout next to this tan-cli checkout.

A SECOND, narrower non-failure case: the fixture existing on disk in the
resolved alp-sdk checkout but NOT at the pinned ref `parity.yml`'s
`seam1-plan-shape` job checks out. A ref that never had the fixture is "not
yet applicable", logged as a NOTICE and treated as passing. `PINNED_SDK_TAG`
is now past alp-sdk#893/#894 (the `--emit kconfig` feature) and #897 (its
fixture), so that branch is dead in CI today and only fires for an OLDER ref
used locally; against the current pin a fixture missing upstream is a real
removal and fails loudly. A byte MISMATCH (fixture
present upstream, content differs) always fails regardless of the pin --
that is the actual drift this gate exists to catch.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Relative to each repo's root -- identical on both sides by construction, so
# the only thing this script does is read the same relative path from two
# checkouts and byte-compare.
FIXTURE_RELPATH = Path("tests/fixtures/kconfig-contract/emit-kconfig.golden.json")

VENDORED_PATH = Path(__file__).resolve().parent.parent.parent / FIXTURE_RELPATH


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
    upstream_path = sdk_root / FIXTURE_RELPATH

    if not VENDORED_PATH.is_file():
        print(f"FAIL: vendored fixture missing at {VENDORED_PATH}")
        return False
    if not upstream_path.is_file():
        # NOT a fail: `PINNED_SDK_TAG` (parity.yml) is a hand-bumped fixed ref,
        # and a ref predating alp-sdk#897 landing this fixture has no such file
        # at all. A ref that never HAD this fixture is "not yet applicable",
        # not drift -- unlike a byte MISMATCH below, which means the fixture
        # existed and something about it changed, always a real fail. The
        # current pin is past #897, so in CI this branch is dead; it only fires
        # for an older ref used locally.
        print(f"NOTICE: no fixture at {upstream_path} in this alp-sdk ref -- "
              f"pinned ref predates alp-sdk#893/#894/#897 landing the "
              f"canonical `--emit kconfig` contract anchor; not treated as "
              f"drift until the pin is bumped past it.")
        return True

    vendored = VENDORED_PATH.read_bytes()
    upstream = upstream_path.read_bytes()
    if vendored != upstream:
        print(f"FAIL: {VENDORED_PATH} differs from upstream {upstream_path} "
              f"-- re-vendor the fixture (tan-cli's `Option<String>` field "
              f"contract in crates/tan-core/src/kconfig.rs may also need a "
              f"matching update if a field was added/removed/renamed)")
        return False

    print(f"PASS: {FIXTURE_RELPATH} is byte-identical to upstream "
          f"({len(vendored)} bytes)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=None,
                         help="Path to the alp-sdk checkout to compare the "
                              "vendored fixture against. Falls back to "
                              "$ALP_SDK_ROOT, then an alp-sdk checkout next "
                              "to this tan-cli checkout.")
    args = parser.parse_args(argv)

    sdk_root = resolve_sdk_root(args.sdk)
    if sdk_root is None:
        print("SKIP: no alp-sdk checkout reachable (--sdk / $ALP_SDK_ROOT / "
              "a sibling alp-sdk checkout); kconfig fixture byte-parity not "
              "checked this run.")
        return 0

    return 0 if run(sdk_root) else 1


if __name__ == "__main__":
    raise SystemExit(main())
