#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Shared alp-sdk-checkout resolution for the optionally-self-skipping
byte-parity gates (`toolchain_lock_parity.py`, `bootstrap_manifest_parity.py`,
`kconfig_fixture_parity.py`, `scaffold_byte_parity.py`).

Both scripts vendored an identical `_looks_like_sdk_checkout`/
`resolve_sdk_root` pair and, with it, an identical gap a #172 review caught:
an explicit `--sdk <path>` was treated as a HINT, not a DEMAND. When it
failed to resolve, `resolve_sdk_root` silently fell through to
`$ALP_SDK_ROOT` and then a sibling `alp-sdk` checkout, and if none of those
resolved either the script printed SKIP and exited 0 -- turning a required CI
gate green without checking anything. The `seam1 -- plan-shape parity` job
passes `--sdk alp-sdk` (the job's own checkout path); a moved checkout or a
pinned alp-sdk that stops shipping `scripts/alp_orchestrate/` should FAIL
that step, not skip it quietly.

[`sdk_root_or_exit_code`] is the fix, shared once instead of twice: an
explicit `--sdk` that does not look like an alp-sdk checkout is a hard FAIL;
only the no-`--sdk` local dev-loop path (nothing passed, `$ALP_SDK_ROOT`
unset, no sibling checkout) is still a clean SKIP.
"""

from __future__ import annotations

import os
from pathlib import Path


def looks_like_sdk_checkout(path: Path) -> bool:
    return (path / "scripts" / "alp_orchestrate").is_dir()


def resolve_sdk_root(explicit: Path | None) -> Path | None:
    """Find a reachable alp-sdk checkout: `--sdk`, then `$ALP_SDK_ROOT`, then a
    `../alp-sdk` sibling of this tan-cli checkout. `None` if none resolves --
    the caller treats that as a clean skip, not a failure.

    Callers with an explicit `--sdk` should validate it with
    [`sdk_root_or_exit_code`] BEFORE calling this: this function treats
    `explicit` as just the first candidate, so on its own it would still let
    an invalid `--sdk` fall through to the env var or sibling checkout."""
    candidates = []
    if explicit is not None:
        candidates.append(explicit)
    env_root = os.environ.get("ALP_SDK_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(Path(__file__).resolve().parent.parent.parent.parent / "alp-sdk")

    for candidate in candidates:
        candidate = candidate.resolve()
        if looks_like_sdk_checkout(candidate):
            return candidate
    return None


def sdk_root_or_exit_code(
    explicit: Path | None, *, self_skip_message: str
) -> tuple[Path | None, int | None]:
    """Resolve the alp-sdk checkout for an optionally self-skipping parity
    gate's `main()`. Returns `(sdk_root, exit_code)`; exactly one is not
    `None`:

    * `explicit` is set and does not look like an alp-sdk checkout -> FAIL,
      `(None, 1)`. A demand, not a hint (tan-cli#172 review) -- printed here
      rather than left to fall through to `$ALP_SDK_ROOT` or a sibling
      checkout.
    * no reachable checkout at all (no `--sdk`, `$ALP_SDK_ROOT` unset, no
      sibling) -> SKIP, `(None, 0)`, printing `self_skip_message`.
    * otherwise -> `(sdk_root, None)`; the caller runs the real check.
    """
    if explicit is not None:
        resolved = explicit.resolve()
        if not looks_like_sdk_checkout(resolved):
            print(
                f"FAIL: --sdk {resolved} does not look like an alp-sdk checkout "
                f"(no scripts/alp_orchestrate/ dir) -- an explicit --sdk must resolve, "
                f"not silently fall through to $ALP_SDK_ROOT or a sibling checkout."
            )
            return None, 1

    sdk_root = resolve_sdk_root(explicit)
    if sdk_root is None:
        print(self_skip_message)
        return None, 0

    return sdk_root, None
