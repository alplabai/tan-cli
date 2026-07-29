# SPDX-License-Identifier: Apache-2.0
"""Pure build-plan execution decisions (ADR-0020): turn the plan's
envAppendPath and executionPolicy into concrete env values and skip/fail
dispositions. No IO, no spawning -- the executor calls these and owns the IO."""
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum


class PolicyAction(StrEnum):
    SKIP = "skip"
    FAIL = "fail"


@dataclass(frozen=True)
class ExecutionPolicy:
    unknown_backend: PolicyAction | None = None
    missing_tool: PolicyAction | None = None
    null_command: PolicyAction | None = None


def sep_for_key(var: str) -> str:
    """The join separator for ONE envAppendPath var -- per-key, not uniformly
    os.pathsep. EXTRA_ZEPHYR_MODULES is a CMake list that Zephyr's
    zephyr_module.py splits on ';' on EVERY platform (never an OS path list);
    joining it with ':' on Linux/WSL fails `west build` configure with
    "is not a valid zephyr module"."""
    return ";" if var == "EXTRA_ZEPHYR_MODULES" else os.pathsep


def apply_env_append(base: list[tuple[str, str]], append: dict[str, list[str]]) -> None:
    """Append each value to its var using that var's separator, skipping a value
    already present segment-wise. A var absent from `base` is seeded from the
    appended values (the plan owns it). Mutates `base` in place."""
    for var, values in append.items():
        sep = sep_for_key(var)
        current = next((v for k, v in base if k == var), None)
        segments = current.split(sep) if current else []
        for val in values:
            if val not in segments:
                segments.append(val)
        joined = sep.join(segments)
        for i, (k, _) in enumerate(base):
            if k == var:
                base[i] = (var, joined)
                break
        else:
            base.append((var, joined))


def assemble_slice_env(
    slice_env: dict[str, str],
    env_append_path: dict[str, list[str]],
    inherited: Callable[[str], str | None],
    gap_fillers: Sequence[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Start from the slice's verbatim env, seed any envAppendPath var the slice
    doesn't pin from `inherited` (so an append EXTENDS rather than replaces an
    inherited PYTHONPATH), apply the appends, then merge the CLI's
    consumer-mechanism gap fillers -- "plan wins / CLI fills gaps"."""
    env: list[tuple[str, str]] = list(slice_env.items())
    for key in env_append_path:
        if not any(k == key for k, _ in env):
            value = inherited(key)
            if value is not None:
                env.append((key, value))
    apply_env_append(env, env_append_path)
    for key, value in gap_fillers:
        for i, (k, _) in enumerate(env):
            if k == key:
                env[i] = (key, value)
                break
        else:
            env.append((key, value))
    return env


def resolve_action(
    policy: ExecutionPolicy | None, key: str, default: PolicyAction
) -> PolicyAction:
    """Honour executionPolicy's entry when present, else the CLI's built-in
    behaviour for an older plan that omits it."""
    if policy is None:
        return default
    picked = getattr(policy, key, None)
    return picked if picked is not None else default


class SdkStampAction(StrEnum):
    KEEP = "keep"
    PRISTINE = "pristine"


def sdk_stamp_action(
    cached: str | None,
    current: str | None,
    cache_configured: bool,
    build_dir_overridden: bool,
    cwd_under_build_root: bool,
) -> SdkStampAction:
    """Whether to wipe a slice's build dir because it was configured against a
    different SDK root. West refuses to reconfigure a build dir whose CMake cache
    is bound to another source tree ("FATAL ERROR: refusing to proceed without
    --force"), which reaches the user as a bare "terminated with exit code: 1".

    A missing stamp on an already-configured dir reads as stale DELIBERATELY --
    it is the only way a build dir predating this feature ever self-heals."""
    if build_dir_overridden or not cwd_under_build_root or not cache_configured:
        return SdkStampAction.KEEP
    if current is None:
        return SdkStampAction.KEEP
    return SdkStampAction.KEEP if cached == current else SdkStampAction.PRISTINE
