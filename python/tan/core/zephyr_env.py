# SPDX-License-Identifier: Apache-2.0
"""Consumer-mechanism env gap-filler for `tan build`'s native executor: the
`ZEPHYR_BASE` / `EXTRA_ZEPHYR_MODULES` keys the plan deliberately does NOT
carry, filled in only when the plan didn't pin them ("plan wins / CLI fills
gaps"). Port of `zephyr_env_overrides`, `crates/tan-cli/src/commands/build/
execute/env.rs` -- confirmed against the compiled Rust unit tests in that
module (`cargo test -p alp-tan-cli --bin tan commands::build::execute::env::`,
all 5 passing) since the oracle binary's own `--plan-from` implies `--plan`
(v0.4.1 limitation, `build_cmd.py`'s own docstring) and so cannot be driven to
dispatch a synthetic plan end to end without a real `alp_orchestrate.py`
emission.

tan-cli#308: `ZEPHYR_BASE` is per ADR-0020 never carried by the plan at all --
it is pure consumer mechanism, always hand-derived from the west workspace
`tan` itself resolved -- so a stale ambient `$ZEPHYR_BASE` left over from a
`source zephyr-env.sh` (or an older `tan bootstrap` next-steps block, before
tan-cli#301) must not silently win for the spawned build child."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tan.core.plan_exec import apply_env_append


def zephyr_env_overrides(
    zephyr_base: Path | None,
    sdk_root: Path | None,
    slice_env: dict[str, str],
    env_append_path: dict[str, list[str]],
    inherited: Callable[[str], str | None],
) -> list[tuple[str, str]]:
    """Consumer-mechanism env the plan deliberately does NOT carry, filled in
    as a gap-filler so `tan build` runs a plan slice with no manual setup:

    * `ZEPHYR_BASE` -- the resolved workspace's zephyr. Per ADR-0020 the plan
      never emits this; it is pure consumer mechanism, always hand-derived.
    * `EXTRA_ZEPHYR_MODULES` -- the alp-sdk checkout, so `west build -b
      <alp-board>` finds the SDK's boards. This now comes FROM the plan's
      `env_append_path` for an SDK-emitted plan; the hand-derived value here
      is only a FALLBACK for a plan that carries neither the slice-env pin
      nor the `env_append_path` entry (plan wins / CLI fills gaps).

    Never overrides a key THIS slice's env pins.

    `inherited` is the parent-process env lookup (the same one the caller
    already threads through to `assemble_slice_env` for `env_append_path`
    seeding). The `EXTRA_ZEPHYR_MODULES` gap-filler must not just return the
    bare SDK root: the caller's own gap-filler merge OVERWRITES rather than
    appends (see `assemble_slice_env`'s docstring), so returning only the SDK
    root here would silently replace a developer's own
    `export EXTRA_ZEPHYR_MODULES=/my/module` with just the SDK root on any
    plan that doesn't itself pin the key. Seed from `inherited` and append the
    SDK root via the same `apply_env_append` (per-key separator, de-dup) the
    plan-driven path uses, so the two paths agree on whether an inherited
    value survives."""
    out: list[tuple[str, str]] = []
    if "ZEPHYR_BASE" not in slice_env and zephyr_base is not None:
        out.append(("ZEPHYR_BASE", str(zephyr_base)))

    # Plan wins: skip the hand-derived value when the plan carries
    # EXTRA_ZEPHYR_MODULES (as a slice-env pin or an env_append_path entry).
    if "EXTRA_ZEPHYR_MODULES" not in slice_env and "EXTRA_ZEPHYR_MODULES" not in env_append_path:
        if sdk_root is not None:
            sdk = str(sdk_root)
            base: list[tuple[str, str]] = []
            inherited_value = inherited("EXTRA_ZEPHYR_MODULES")
            if inherited_value:
                base.append(("EXTRA_ZEPHYR_MODULES", inherited_value))
            apply_env_append(base, {"EXTRA_ZEPHYR_MODULES": [sdk]})
            if base:
                out.append(("EXTRA_ZEPHYR_MODULES", base[0][1]))
    return out
