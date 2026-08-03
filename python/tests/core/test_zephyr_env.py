# SPDX-License-Identifier: Apache-2.0
"""tan-cli#308: `zephyr_env_overrides` -- port of `crates/tan-cli/src/
commands/build/execute/env.rs`'s `zephyr_env_overrides`, confirmed against
that module's own compiled unit tests (`cargo test -p alp-tan-cli --bin tan
commands::build::execute::env::`) since the oracle binary's `--plan-from`
implies `--plan` and so cannot dispatch a synthetic plan end to end without a
real `alp_orchestrate.py` emission -- see this module's own docstring."""
from pathlib import Path

from tan.core.plan_exec import apply_env_append
from tan.core.zephyr_env import zephyr_env_overrides

# `zephyr_env_overrides` takes real `Path`s and emits `str(path)`, so on
# Windows `Path("/sdk")` renders `\sdk`, not `/sdk`. Comparing against a
# POSIX literal made all five of these fail on `test (windows-latest)` --
# a test-only defect (production feeds real resolved paths, which render
# correctly on both platforms), but a red REQUIRED gate all the same. Derive
# the expectations through the same `str(Path(...))` the code under test
# uses so each assertion means what it says on either platform.
SDK = str(Path("/sdk"))
WS_ZEPHYR = str(Path("/ws/zephyr"))
#: An inherited env var is a raw string the user exported -- NOT round-tripped
#: through `Path` by the code under test, so it stays literal on both platforms.
MY_MODULE = "/home/u/my-module"


def no_inherited(_key: str) -> str | None:
    return None


def test_fills_base_and_modules_when_absent():
    got = zephyr_env_overrides(
        Path("/ws/zephyr"), Path("/sdk"), slice_env={"ALP_SDK_ROOT": "/sdk"},
        env_append_path={}, inherited=no_inherited,
    )
    assert got == [("ZEPHYR_BASE", WS_ZEPHYR), ("EXTRA_ZEPHYR_MODULES", SDK)]


def test_respects_plan_pinned_keys():
    """The plan already pins both -- nothing is overridden."""
    got = zephyr_env_overrides(
        Path("/ws/zephyr"), Path("/sdk"),
        slice_env={"ZEPHYR_BASE": "/pinned", "EXTRA_ZEPHYR_MODULES": "/pinned-mod"},
        env_append_path={}, inherited=no_inherited,
    )
    assert got == []


def test_skips_extra_modules_when_plan_appends_it():
    """Plan wins / CLI fills gaps: the plan carries EXTRA_ZEPHYR_MODULES in
    envAppendPath, so the CLI must NOT hand-derive it -- but ZEPHYR_BASE
    (which the plan never carries) is still filled in."""
    got = zephyr_env_overrides(
        Path("/ws/zephyr"), Path("/sdk"), slice_env={},
        env_append_path={"EXTRA_ZEPHYR_MODULES": ["/plan/sdk"]}, inherited=no_inherited,
    )
    assert got == [("ZEPHYR_BASE", WS_ZEPHYR)]


def test_empty_when_nothing_resolved():
    assert zephyr_env_overrides(None, None, {}, {}, no_inherited) == []


def test_extends_rather_than_replaces_an_inherited_extra_zephyr_modules():
    """Regression: an earlier shape of this gap-filler returned the bare SDK
    root, and the caller's gap-filler merge OVERWRITES the var outright -- so
    a developer's own `export EXTRA_ZEPHYR_MODULES=/home/u/my-module` vanished
    from the build on any plan that didn't itself pin the key."""
    got = zephyr_env_overrides(
        None, Path("/sdk"), slice_env={}, env_append_path={},
        inherited=lambda k: MY_MODULE if k == "EXTRA_ZEPHYR_MODULES" else None,
    )
    # EXTRA_ZEPHYR_MODULES is a Zephyr CMake list: joined with ';' on EVERY
    # platform (plan_exec.sep_for_key), not os.pathsep.
    assert got == [("EXTRA_ZEPHYR_MODULES", f"{MY_MODULE};{SDK}")]


def test_inherited_value_already_containing_the_sdk_root_is_not_duplicated():
    """`apply_env_append`'s own de-dup applies here too -- confirmed by
    reusing the exact same helper the plan-driven envAppendPath path uses,
    not a re-implementation."""
    got = zephyr_env_overrides(
        None, Path("/sdk"), slice_env={}, env_append_path={},
        inherited=lambda k: SDK if k == "EXTRA_ZEPHYR_MODULES" else None,
    )
    assert got == [("EXTRA_ZEPHYR_MODULES", SDK)]


def test_matches_apply_env_append_directly_for_the_fallback_case():
    """The gap-filler's EXTRA_ZEPHYR_MODULES value must be reachable via the
    SAME machinery `assemble_slice_env`'s own envAppendPath handling uses --
    not a parallel join implementation that could drift from it."""
    base = [("EXTRA_ZEPHYR_MODULES", MY_MODULE)]
    apply_env_append(base, {"EXTRA_ZEPHYR_MODULES": [SDK]})
    got = zephyr_env_overrides(
        None, Path("/sdk"), slice_env={}, env_append_path={},
        inherited=lambda k: MY_MODULE if k == "EXTRA_ZEPHYR_MODULES" else None,
    )
    assert got == [base[0]]
