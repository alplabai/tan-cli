# SPDX-License-Identifier: Apache-2.0
"""tan-cli#138 / tan-cli#321: the pre-launch-task default/override/opt-out
three-state contract, and the yocto-userspace `--gdbserver-address`
resolution -- the pure-logic half of `tan.core.debug_launch`, exercised
directly (no subprocess, no envelope). The command-level wiring (the CLI
flags, the tan-cli#321 info issue, the "no effect" notes) is covered by
`tests/commands/test_debug_config_command.py`.
"""
from __future__ import annotations

import json

import pytest

from tan.core.debug_launch import (
    BAREMETAL_MCU,
    DEFAULT_PRE_LAUNCH_TASK,
    GDBSERVER,
    JLINK,
    NATIVE_HOST,
    OPENOCD,
    PYOCD,
    SERVER_NONE,
    YOCTO_USERSPACE,
    ZEPHYR_MCU,
    LaunchResolution,
    apply_launch_resolution,
    create_launch_draft,
    programs_device,
)

#: Every (target, server) pair paired with the v0.3.1 default that target
#: restores (tan-cli#138). The default is keyed by TARGET alone, so
#: zephyr-mcu's three servers all share one string; see
#: `test_the_baremetal_default_is_the_same_across_every_server` for the same
#: invariant on the two baremetal servers left implicit here.
#:
#: `YOCTO_USERSPACE` is deliberately ABSENT: three of the four target classes
#: get a restored default, not four (the #138-vs-#321 resolution --
#: `DEFAULT_PRE_LAUNCH_TASK`'s own doc comment in `tan/core/debug_launch.py`
#: has the full quotation). `test_yocto_userspace_gets_no_default_pre_launch_
#: task` below covers that target on its own.
DEFAULTED_PROFILES = [
    (ZEPHYR_MCU, JLINK, "alp: build active target"),
    (ZEPHYR_MCU, OPENOCD, "alp: build active target"),
    (ZEPHYR_MCU, PYOCD, "alp: build active target"),
    (BAREMETAL_MCU, JLINK, "alp: build baremetal target"),
    (NATIVE_HOST, SERVER_NONE, "alp: build native_sim target"),
]


def test_default_pre_launch_task_table_is_the_v031_literals():
    """`DEFAULT_PRE_LAUNCH_TASK` keyed by TARGET alone -- three target
    classes, not four, each holding the exact v0.3.1 string
    (`crates/tan-core/src/debug_launch.rs` before tan-cli#85 made the key
    opt-in). `YOCTO_USERSPACE` is deliberately absent: alp-sdk-vscode
    registers no working task for it (the only one that exists exits 1 by
    design), so restoring that one label would put the "preLaunchTask
    terminated with exit code 1" dialog in front of every F5 -- the
    #138-vs-#321 resolution `DEFAULT_PRE_LAUNCH_TASK`'s own doc comment
    records."""
    assert DEFAULT_PRE_LAUNCH_TASK == {
        ZEPHYR_MCU: "alp: build active target",
        BAREMETAL_MCU: "alp: build baremetal target",
        NATIVE_HOST: "alp: build native_sim target",
    }
    assert YOCTO_USERSPACE not in DEFAULT_PRE_LAUNCH_TASK


# Formerly `no_profile_names_a_pre_launch_task_by_default`
# (`crates/tan-core/src/debug_launch.rs`): that Rust test pinned "no default
# preLaunchTask" as the Bug-1 regression fix (tan-cli#85). tan-cli#138 is a
# MAINTAINER DECISION that inverts the intent for three of the four target
# classes -- alp-sdk-vscode has since registered those three labels as real
# tasks, so the v0.3.1 defaults are restored for them -- so THIS is the
# corrected assertion for those five profiles, not a new, unrelated test. Its
# Rust sibling still asserts the old, now superseded, behaviour: `crates/` is
# a frozen oracle this port no longer tracks (see
# `python/tan/commands/debug_config_cmd.py`'s module docstring).
@pytest.mark.parametrize("target,server,expected_task", DEFAULTED_PROFILES)
def test_every_profile_names_its_v031_pre_launch_task_by_default(target, server, expected_task):
    draft = create_launch_draft(target, server, None)
    assert draft["preLaunchTask"] == expected_task
    # Belt and braces, mirroring the Rust test's own: a `null` would still
    # serialize as a key, so absence from the rendered JSON is the real proof.
    assert '"preLaunchTask"' in json.dumps(draft)


def test_yocto_userspace_gets_no_default_pre_launch_task():
    """The flip side of `test_default_pre_launch_task_table_is_the_v031_
    literals` at the draft level: yocto-userspace's only registered task
    exits 1 by design (alp-sdk-vscode#406), so a plain run with no
    `--pre-launch-task` must NOT reach for a default the way the other three
    targets do -- the trailing `del` in `create_launch_draft` fires on `None`
    here exactly as it did for every target before tan-cli#138 restored the
    other three.

    An explicit `--pre-launch-task ''` reaches the identical `del`, not a
    distinct path, now that yocto-userspace has no default entry left to opt
    out of -- checked here too rather than only via the trimmed
    `DEFAULTED_PROFILES` parametrize above, which no longer names this
    target at all.
    """
    draft = create_launch_draft(YOCTO_USERSPACE, GDBSERVER, None)
    assert "preLaunchTask" not in draft
    assert '"preLaunchTask"' not in json.dumps(draft)

    draft_explicit_empty = create_launch_draft(YOCTO_USERSPACE, GDBSERVER, "")
    assert "preLaunchTask" not in draft_explicit_empty


def test_the_baremetal_default_is_the_same_across_every_server():
    """The task's own six-line table names only baremetal-mcu+jlink; the
    default is keyed by TARGET alone (tan-cli#138), so OpenOCD and pyOCD
    baremetal profiles must carry the identical string, not their own."""
    for server in (OPENOCD, PYOCD):
        draft = create_launch_draft(BAREMETAL_MCU, server, None)
        assert draft["preLaunchTask"] == "alp: build baremetal target"


def test_an_opted_in_pre_launch_task_overrides_the_default_verbatim():
    """The `--pre-launch-task <name>` override, unchanged by tan-cli#138: any
    non-empty string wins over the restored default, emitted in place."""
    draft = create_launch_draft(ZEPHYR_MCU, JLINK, "alpRun: build")
    assert draft["preLaunchTask"] == "alpRun: build"
    # …in its ORIGINAL position, not appended at the end -- the key order the
    # module docstring calls contract.
    keys = list(draft.keys())
    assert keys.index("preLaunchTask") == keys.index("runToEntryPoint") + 1


@pytest.mark.parametrize("target,server,_expected", DEFAULTED_PROFILES)
def test_an_empty_string_opts_out_of_the_restored_default(target, server, _expected):
    """tan-cli#138's explicit opt-out: `--pre-launch-task ''` must still reach
    the trailing `del` in `create_launch_draft` -- now that every target has a
    non-`None` default, `None` alone can no longer get there; this is the one
    remaining way to drop the key. `drop_absent_pre_launch_task`'s Python twin
    stays dead code without a caller that reaches it, which this is."""
    draft = create_launch_draft(target, server, "")
    assert "preLaunchTask" not in draft
    assert '"preLaunchTask"' not in json.dumps(draft)


def test_apply_launch_resolution_fills_the_gdbserver_address_placeholder():
    """tan-cli#321 direction 2: `--gdbserver-address` is the ONLY source of
    `miDebuggerServerAddress`'s resolution -- nothing else (a build, SDK-
    published metadata) can ever know where the board ends up after deploy."""
    draft = create_launch_draft(YOCTO_USERSPACE, GDBSERVER, None)
    assert draft["miDebuggerServerAddress"] == "<host>:<port>"

    apply_launch_resolution(draft, LaunchResolution(gdbserver_address="192.168.10.42:3333"))

    assert draft["miDebuggerServerAddress"] == "192.168.10.42:3333"


def test_apply_launch_resolution_leaves_the_placeholder_with_no_address_given():
    draft = create_launch_draft(YOCTO_USERSPACE, GDBSERVER, None)
    apply_launch_resolution(draft, LaunchResolution())
    assert draft["miDebuggerServerAddress"] == "<host>:<port>"


def test_gdbserver_address_has_no_effect_on_a_draft_without_the_key():
    """The same "only replace a key the draft already carries" rule
    `apply_launch_resolution`'s own docstring states for every other field --
    a zephyr-mcu draft has no `miDebuggerServerAddress` key to fill."""
    draft = create_launch_draft(ZEPHYR_MCU, JLINK, None)
    apply_launch_resolution(draft, LaunchResolution(gdbserver_address="192.168.10.42:3333"))
    assert "miDebuggerServerAddress" not in draft


# tan-cli#945: a consumer must be able to tell, from the written profile
# alone, whether starting it programs the attached target -- see this
# module's own `PROGRAMS_DEVICE`/`programs_device` doc comments for why.
@pytest.mark.parametrize(
    "target,server,executable",
    [
        (ZEPHYR_MCU, JLINK, "${workspaceFolder}/build/app/zephyr/zephyr.elf"),
        (ZEPHYR_MCU, OPENOCD, "${workspaceFolder}/build/app/zephyr/zephyr.elf"),
        (ZEPHYR_MCU, PYOCD, "${workspaceFolder}/build/app/zephyr/zephyr.elf"),
        (BAREMETAL_MCU, JLINK, "${workspaceFolder}/build/baremetal/app.elf"),
        (BAREMETAL_MCU, OPENOCD, "${workspaceFolder}/build/baremetal/app.elf"),
        (BAREMETAL_MCU, PYOCD, "${workspaceFolder}/build/baremetal/app.elf"),
    ],
)
def test_cortex_debug_drafts_carry_an_explicit_load_files_key(target, server, executable):
    """Every cortex-debug draft states `loadFiles` explicitly, naming the same
    artefact as `executable`, instead of relying on the adapter's own
    undocumented-on-the-wire schema default (`marus25.cortex-debug`'s
    `loadFiles`: "if this property does not exist, then the executable is
    used to program the device")."""
    draft = create_launch_draft(target, server, None)
    assert draft["loadFiles"] == [executable]
    assert draft["executable"] == executable


@pytest.mark.parametrize("target,server", [(YOCTO_USERSPACE, GDBSERVER), (NATIVE_HOST, SERVER_NONE)])
def test_non_cortex_debug_drafts_carry_no_load_files_key(target, server):
    """`loadFiles` is a cortex-debug-only concept (cppdbg/lldb have none);
    a non-cortex-debug draft must not grow the key."""
    draft = create_launch_draft(target, server, None)
    assert "loadFiles" not in draft


def test_apply_launch_resolution_updates_load_files_with_the_resolved_executable():
    """A real build's own resolved per-core ELF must replace `loadFiles`
    alongside `executable` -- otherwise the file cortex-debug reports and the
    file it actually programs could silently diverge."""
    draft = create_launch_draft(ZEPHYR_MCU, JLINK, None)
    apply_launch_resolution(
        draft, LaunchResolution(executable="${workspaceFolder}/build/m55_hp/zephyr/zephyr.elf")
    )
    assert draft["loadFiles"] == ["${workspaceFolder}/build/m55_hp/zephyr/zephyr.elf"]
    assert draft["executable"] == draft["loadFiles"][0]


def test_load_files_has_no_effect_on_a_draft_without_the_key():
    """Same "only replace a key the draft already carries" rule as
    `miDebuggerServerAddress` above -- a yocto-userspace draft has no
    `loadFiles` key to fill even when `resolution.executable` resolves (it
    fills `program` instead)."""
    draft = create_launch_draft(YOCTO_USERSPACE, GDBSERVER, None)
    apply_launch_resolution(draft, LaunchResolution(executable="/deploy/app"))
    assert "loadFiles" not in draft
    assert draft["program"] == "/deploy/app"


@pytest.mark.parametrize(
    "target,expected",
    [
        (ZEPHYR_MCU, True),
        (BAREMETAL_MCU, True),
        (YOCTO_USERSPACE, False),
        (NATIVE_HOST, False),
    ],
)
def test_programs_device_matches_the_target_class(target, expected):
    """The two cortex-debug target classes program real hardware on every
    launch this module can produce; yocto-userspace attaches to an
    already-deployed gdbserver and native-host runs on the host -- neither
    writes to a device via this session."""
    assert programs_device(target) is expected


def test_programs_device_defaults_to_false_for_an_unrecognised_target():
    """Understating "programs a device" is merely confusing; overstating it
    is the class of bug tan-cli#945 exists to close -- an unknown target must
    never read as `True`."""
    assert programs_device("not-a-real-target-kind") is False
