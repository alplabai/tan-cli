#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Bootstrap manifest byte-diff (alp-sdk#917): the FROZEN vendored copy of
alp-sdk's `metadata/bootstrap.json` vs. any alp-sdk checkout's own copy.

`metadata/bootstrap.json` is the SDK's single source of truth for the
workspace-assembly FACTS that `scripts/bootstrap.sh`, `scripts/bootstrap.ps1`
and (the Rust oracle's) `tan bootstrap` all need -- the Zephyr pin, venv
layout, prerequisite lists + Python floor, the `west` pip spec and argv, the
pip package sets, the `env` map and the per-OS native-lib hints. Its own
`_comment` names tan as a real reader of those facts since tan-cli PR #55 --
`crates/tan-core/src/bootstrap/manifest.rs`'s `parse_bootstrap_manifest` is
that reader.

**Not a CI gate.** It byte-diffs the vendored copy at
`contract/fixtures/bootstrap/manifest.json` -- which `manifest.rs` and
`crates/tan-cli/src/commands/bootstrap/mod.rs` both `include_str!` -- against
an alp-sdk checkout's `metadata/bootstrap.json`. `contract/` is frozen
permanently at its own v0.14.0 vendor point (`docs/ROADMAP.md`'s Standing
Rules: "Never edit `crates/` or `contract/`"), while `PINNED_SDK_TAG` moves
forward, so a run against anything newer than v0.14.0 reports a MISMATCH by
design, forever -- that stopped being a signal worth gating a PR on, which is
why `.github/workflows/parity.yml` no longer runs this script. What it is
still good for, run BY HAND: pointing `--sdk` at v0.14.0 itself re-verifies
the frozen fixture has not bit-rotted since it was vendored, and pointing it
at a later ref shows a maintainer exactly what alp-sdk's bootstrap facts have
moved on since the freeze -- informational, never actionable against
`contract/`, which does not move.

The Rust oracle's own drift check survives unaffected: `manifest.rs`'s
`the_fallback_matches_the_real_manifest_field_for_field` `cargo test` still
asserts its hand-ported fallback constants equal `contract/fixtures/
bootstrap/manifest.json` field-for-field, at that fixture's own frozen point
-- this script was never load-bearing for that assertion, only for keeping
the fixture itself current, which no longer applies.

The shipped Python `tan` carries no equivalent vendored copy to diff here:
`tan.core.bootstrap` reads `metadata/bootstrap.json` live off the bound SDK
root (see `fallback_facts`'s own module doc for its hand-ported fallback,
used only for an SDK predating this manifest, and guarded by
`python/tests/commands/test_bootstrap_command.py::
test_the_fallback_constants_match_the_real_manifest_field_for_field` against
this SAME frozen fixture -- the Python-side mirror of the cargo test above).

Optionally self-skipping, same as `scaffold_byte_parity.py` /
`kconfig_fixture_parity.py`: both consumers' own test suites already prove
the vendored fixture parses and round-trips without an SDK checkout, so a
run with no reachable alp-sdk is a clean no-op, not a failure. Reachability
is resolved in the same order: `--sdk`, then `$ALP_SDK_ROOT`, then an
`alp-sdk` checkout next to this tan-cli checkout -- but an explicit `--sdk`
that does not resolve is a hard FAIL, not a fall-through to the other two
(tan-cli#172 review; see `_sdk_checkout.sdk_root_or_exit_code`).

A SECOND non-failure case: the manifest absent at the given ref, for one
predating alp-sdk#917 landing it (e.g. v0.14.0 itself does have it, so this
only fires for something older still).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from _sdk_checkout import sdk_root_or_exit_code

# The paths differ per repo: SDK metadata upstream, a test fixture here.
UPSTREAM_RELPATH = Path("metadata/bootstrap.json")
VENDORED_RELPATH = Path("contract/fixtures/bootstrap/manifest.json")

VENDORED_PATH = Path(__file__).resolve().parent.parent.parent / VENDORED_RELPATH


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
        print(f"DIFFERS: {VENDORED_PATH} differs from upstream {upstream_path}. "
              f"`contract/` is frozen at its v0.14.0 vendor point "
              f"(`docs/ROADMAP.md` Standing Rules: never edit `crates/` or "
              f"`contract/`), so this is EXPECTED for any ref newer than "
              f"v0.14.0 -- not a re-vendor prompt. Point `--sdk` at v0.14.0 "
              f"itself to confirm the fixture still matches its own freeze "
              f"point.")
        return False

    print(f"MATCH: {VENDORED_RELPATH} is byte-identical to upstream "
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

    sdk_root, exit_code = sdk_root_or_exit_code(
        args.sdk,
        self_skip_message=(
            "SKIP: no alp-sdk checkout reachable (--sdk / $ALP_SDK_ROOT / "
            "a sibling alp-sdk checkout); bootstrap manifest byte-parity "
            "not checked this run."
        ),
    )
    if exit_code is not None:
        return exit_code

    return 0 if run(sdk_root) else 1


if __name__ == "__main__":
    raise SystemExit(main())
