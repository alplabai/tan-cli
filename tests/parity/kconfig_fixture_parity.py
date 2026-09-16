#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Kconfig fixture byte-parity gate (alp-sdk#893/#894): the vendored copy of
alp-sdk's canonical `--emit kconfig` contract anchor vs. the pinned alp-sdk
checkout's own copy.

This repo keeps a vendored byte-copy of alp-sdk's
`tests/fixtures/kconfig-contract/emit-kconfig.golden.json` at the same
relative path, so `tan kconfig`'s parse/envelope path can be tested against
the SDK's real field contract without a Zephyr/west workspace. Its reader is
`python/tests/commands/test_kconfig_command.py` (until tan-cli#269 the retired
Rust oracle `include_str!`d the same file from two places). That vendored copy can drift from
upstream exactly like the wizard-scaffold vendoring `scaffold_byte_parity.py`
guards (see that script + `tests/parity/README.md`'s "Scaffold byte-parity"
section) — a future SDK field rename would go unnoticed here forever, the
same class of silent cross-repo drift ADR-0020 exists to kill. This script
is the tan-cli-side gate: byte-diff the vendored copy against the pinned
alp-sdk checkout's own copy of the same fixture.

Unlike `seam1_field_diff.py` (which hard-requires `--sdk`), this gate is
optionally self-skipping, same as `scaffold_byte_parity.py`:
`python/tests/commands/test_kconfig_command.py` already
proves the vendored copy is internally consistent (deserializes, round-trips
through the envelope) without an SDK checkout — a local dev-loop run with no
reachable alp-sdk checkout is a clean no-op, not a failure. Reachability is
checked in the same order as `scaffold_byte_parity.py`: `--sdk`, then
`$ALP_SDK_ROOT`, then an `alp-sdk` checkout next to this tan-cli checkout --
but an explicit `--sdk` that does not resolve is a hard FAIL, not a
fall-through to the other two (tan-cli#172 review, tan-cli#175; see
`_sdk_checkout.sdk_root_or_exit_code`).

A byte MISMATCH (fixture present upstream, content differs) always fails --
that is the actual drift this gate exists to catch. So does an ABSENT
upstream fixture. There is no "predates the feature / not yet applicable"
branch, close to the rule `toolchain_lock_parity.py` states for
`metadata/toolchains.json`, and on a measurement rather than a
generalisation: of the 31 distinct values `PINNED_SDK_TAG` has held across
`.github/workflows/parity.yml`'s history,
`tests/fixtures/kconfig-contract/emit-kconfig.golden.json` is present at 29.
The two exceptions are `df312cec` and `f04ea42e`, July-2026 pins predating
the fixture upstream -- `f04ea42e` was the pin in force when this script was
first written (tan-cli `ca34090e`, #40), which is what the deleted branch
below was written for. Every pin since carries it, `81a9d515` (the current
one), the released `v0.16.0` and alp-sdk `origin/dev` included. So an
upstream file missing at the ref under test is a removal, not a legitimate
skip.

alp-sdk#893/#894 landed the `--emit kconfig` feature and #897 its fixture.
tan-cli#1261 deleted the NOTICE-and-pass branch that used to stand here
rather than ancestry-gating it on #897's commit (the other option that issue
offered): unconditional, it could not tell an older ref apart from a release
that REMOVED the fixture, so `release-sdk-parity`
(`.github/workflows/parity.yml`) -- which runs this script against whatever
alp-sdk tag `releases/latest` resolves to -- reported PASS for exactly the
removal this gate exists to catch. An ancestry check would buy nothing over
deleting the branch outright: `PINNED_SDK_TAG` is hand-bumped forward-only,
and the other caller resolves `releases/latest`, where every published
alp-sdk release from `v0.13.0` on carries the fixture (`v0.12.0` and older do
not -- measured, not assumed). Reaching a pre-#897 ref would take withdrawing
all four of those releases so `releases/latest` falls back to `v0.12.0`, and
FAIL is the right answer there too: a released `tan` paired with an alp-sdk
predating the `--emit kconfig` contract anchor has no field contract to be
verified against.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from _sdk_checkout import sdk_root_or_exit_code

# Relative to each repo's root -- identical on both sides by construction, so
# the only thing this script does is read the same relative path from two
# checkouts and byte-compare.
FIXTURE_RELPATH = Path("tests/fixtures/kconfig-contract/emit-kconfig.golden.json")

VENDORED_PATH = Path(__file__).resolve().parent.parent.parent / FIXTURE_RELPATH


def run(sdk_root: Path) -> bool:
    upstream_path = sdk_root / FIXTURE_RELPATH

    if not VENDORED_PATH.is_file():
        print(f"FAIL: vendored fixture missing at {VENDORED_PATH}")
        return False
    if not upstream_path.is_file():
        print(f"FAIL: no {FIXTURE_RELPATH} in this alp-sdk checkout "
              f"({upstream_path}) -- the canonical `--emit kconfig` contract "
              f"anchor (alp-sdk#893/#894, fixture #897) was REMOVED or MOVED "
              f"upstream. It is present at every ref PINNED_SDK_TAG has named "
              f"since #897 landed it (the only exceptions in this repo's "
              f"history are two July-2026 pins that predate the fixture), so "
              f"its absence here is a removal, not a 'not yet applicable' "
              f"skip. Either follow it -- re-vendor from its new upstream "
              f"path and update tan's kconfig field contract in "
              f"python/tan/commands/kconfig_cmd.py if the fields moved with "
              f"it -- or the ref under test is wrong and the pin should name "
              f"one that still carries the fixture.")
        return False

    vendored = VENDORED_PATH.read_bytes()
    upstream = upstream_path.read_bytes()
    if vendored != upstream:
        print(f"FAIL: {VENDORED_PATH} differs from upstream {upstream_path} "
              f"-- re-vendor the fixture (tan's kconfig field contract in "
              f"python/tan/commands/kconfig_cmd.py may also need a matching "
              f"update if a field was added/removed/renamed)")
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

    sdk_root, exit_code = sdk_root_or_exit_code(
        args.sdk,
        self_skip_message=(
            "SKIP: no alp-sdk checkout reachable (--sdk / $ALP_SDK_ROOT / "
            "a sibling alp-sdk checkout); kconfig fixture byte-parity not "
            "checked this run."
        ),
    )
    if exit_code is not None:
        return exit_code

    return 0 if run(sdk_root) else 1


if __name__ == "__main__":
    raise SystemExit(main())
