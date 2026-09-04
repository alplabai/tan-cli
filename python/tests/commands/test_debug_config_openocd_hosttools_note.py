# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1179: `tan debug-config --server openocd` on an SDK installed with
`--no-hosttools`, on a host with no system OpenOCD either.

Zephyr writes `config.openocd` / `config.openocd_search` into `runners.yaml`
only inside `if(OPENOCD)` (`cmake/flash/CMakeLists.txt`), from an optional
`find_program(OPENOCD openocd)` that normally reaches the SDK's copy through
`hosttools/`. With `--no-hosttools` (tan-cli#1176/#1178) and no system copy,
both keys are simply ABSENT -- so `serverpath`/`searchDir`, which are ADDITIVE
and not `<resolved-...>` placeholders, are never inserted and the "Placeholder
fields" note never fires. The output was structurally valid, `ok: true`, and
silent; the developer learnt about it from the debug adapter failing to launch
a GDB server, with tan nowhere in the loop.

**These drive the real path, not a mock of the note.** Every case writes a real
`system-manifest.yaml` + `runners.yaml`, runs the command's own
`_resolve_from_build`, applies the resolution to a real draft with
`apply_launch_resolution`, and asks `_preview_notes_for` -- the same four
functions `tan debug-config` itself calls -- with `PATH` seeded to declare the
host's OpenOCD inventory. The `runners.yaml` bodies are the ones CMake really
writes for each of the three `if(OPENOCD)` / `if(OPENOCD_DEFAULT_PATH)` states.

**The three controls are the point.** A note that fires on a
correctly-provisioned host is the defect `doctor_cmd.west_check`'s docstring
records ("a warning that fires on every correct install trains users to ignore
warnings"), one severity down. So: OpenOCD on PATH is silent, a resolved
`serverpath` is silent, and the MIXED host measured in tan-cli#1179 -- system
OpenOCD on PATH, no `hosttools/`, so `set_ifndef(OPENOCD_DEFAULT_PATH ...)`
still writes an `openocd_search` pointing at a directory that does not exist --
is silent too. That last one is measured benign upstream (`openocd -s
/nonexistent/does/not/exist -f interface/jlink.cfg` still loads off OpenOCD's
built-in scripts directory, reaching "session transport was not selected"), and
warning on it would be a false alarm on a host that debugs fine today.
"""
import os
import sys
from pathlib import Path

import pytest

from tan.commands.debug_config_cmd import (
    OPENOCD_NO_HOSTTOOLS_NOTE,
    _preview_notes_for,
    _resolve_from_build,
)
from tan.core.debug_launch import OPENOCD, ZEPHYR_MCU, apply_launch_resolution, create_launch_draft
from tests.conftest import empty_tool_inventory

#: The `runners.yaml` `config:` block CMake writes when `find_program(OPENOCD
#: openocd)` found NOTHING: `if(OPENOCD)` is false, so neither key exists --
#: never `OPENOCD-NOTFOUND`, which is the whole reason nothing downstream can
#: tell this apart from "this build never asked".
CONFIG_NO_OPENOCD = "config:\n  gdb: /zephyr-sdk/arm-zephyr-eabi-gdb\n"

#: `if(OPENOCD)` true and `if(OPENOCD_DEFAULT_PATH)` true: the ordinary
#: hosttools-bearing install.
CONFIG_OPENOCD_RESOLVED = (
    "config:\n"
    "  gdb: /zephyr-sdk/arm-zephyr-eabi-gdb\n"
    "  openocd: /zephyr-sdk/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin/openocd\n"
    "  openocd_search:\n"
    "  - /zephyr-sdk/hosttools/sysroots/x86_64-pokysdk-linux/usr/share/openocd/scripts\n"
)

#: The MIXED host: `find_program` found the SYSTEM OpenOCD, so `if(OPENOCD)`
#: fired, while `set_ifndef(OPENOCD_DEFAULT_PATH ${HOST_TOOLS_HOME}/usr/share/
#: openocd/scripts)` still pointed `openocd_search` inside an SDK that has no
#: `hosttools/` at all.
CONFIG_OPENOCD_SYSTEM_SEARCH_MISSING = (
    "config:\n"
    "  gdb: /zephyr-sdk/arm-zephyr-eabi-gdb\n"
    "  openocd: /usr/bin/openocd\n"
    "  openocd_search:\n"
    "  - /zephyr-sdk/hosttools/sysroots/x86_64-pokysdk-linux/usr/share/openocd/scripts\n"
)


def _project_with(tmp_path: Path, config_block: str) -> str:
    """A workspace whose one Zephyr slice has really been built, carrying
    `config_block` as its `runners.yaml` `config:`."""
    pytest.importorskip("yaml")
    root = str(tmp_path).replace("\\", "/")
    build_dir = f"{root}/build/m55_hp-zephyr/build"
    Path(tmp_path, "build").mkdir(parents=True, exist_ok=True)
    Path(tmp_path, "build", "system-manifest.yaml").write_text(
        "schema_version: 1\nslices:\n- core_id: m55_hp\n  os: zephyr\n"
        f"  board: alp_e1m_aen701_m55_hp\n  build_dir: {build_dir}\n"
        f"  output_artefact: {build_dir}/zephyr/zephyr.elf\n",
        encoding="utf-8",
    )
    zephyr_dir = Path(build_dir, "zephyr")
    zephyr_dir.mkdir(parents=True, exist_ok=True)
    zephyr_dir.joinpath("runners.yaml").write_text(
        "runners:\n- jlink\n- openocd\n"
        f"{config_block}"
        "args:\n  openocd:\n  - --config=/zephyr/boards/alp/alp_e1m_aen701/support/openocd.cfg\n",
        encoding="utf-8",
    )
    return root


def _path_without_openocd(scratch: Path) -> str:
    """A `PATH` on which nothing resolves at all -- see
    `conftest.empty_tool_inventory` for why it is seeded with `which` rather
    than being literally empty."""
    scratch.mkdir(parents=True, exist_ok=True)
    return empty_tool_inventory(scratch)


def _path_with_openocd(scratch: Path) -> str:
    """[`_path_without_openocd`] plus a real, executable `openocd` file.

    A file, not a monkeypatched predicate: the probe under test resolves
    through `doctor_cmd.on_path` -> `tool_lookup.resolve_tool`, which walks
    `%PATH%` by hand on Windows and uses `shutil.which` on POSIX -- patching
    either out would stop the test exercising the lookup this note depends on.
    `.exe` on Windows because `resolve_tool` deliberately never tries the bare,
    extension-less name there (`windows_candidate_names`); the exec bit on
    POSIX because `shutil.which` requires `X_OK`.
    """
    base = _path_without_openocd(scratch)
    tools = scratch / "openocd-bin"
    tools.mkdir(exist_ok=True)
    name = "openocd.exe" if sys.platform == "win32" else "openocd"
    binary = tools / name
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    return os.pathsep.join([str(tools), base])


def _notes(root: str) -> list[str]:
    """`tan debug-config --server openocd`'s own notes for this project, via
    the same calls the command makes."""
    resolution, registered_runners, _core_id = _resolve_from_build(
        root, ZEPHYR_MCU, OPENOCD, None
    )
    draft = create_launch_draft(ZEPHYR_MCU, OPENOCD, None)
    apply_launch_resolution(draft, resolution)
    return _preview_notes_for(draft, registered_runners, OPENOCD)


def test_no_serverpath_and_no_openocd_on_path_says_so(tmp_path, monkeypatch):
    """THE CASE. Neither key in `runners.yaml`, nothing on `PATH`: the note
    fires, names `--no-hosttools`, and says OpenOCD has to come from the
    system."""
    root = _project_with(tmp_path, CONFIG_NO_OPENOCD)
    monkeypatch.setenv("PATH", _path_without_openocd(tmp_path / "inventory"))

    notes = _notes(root)

    assert OPENOCD_NO_HOSTTOOLS_NOTE in notes
    assert "--no-hosttools" in OPENOCD_NO_HOSTTOOLS_NOTE
    assert "serverpath" in OPENOCD_NO_HOSTTOOLS_NOTE


def test_no_serverpath_but_openocd_on_path_is_silent(tmp_path, monkeypatch):
    """CONTROL 1. The build predates the OpenOCD install, so `runners.yaml`
    carries neither key -- but cortex-debug's own `PATH` lookup will find one,
    so there is nothing to tell this developer."""
    root = _project_with(tmp_path, CONFIG_NO_OPENOCD)
    monkeypatch.setenv("PATH", _path_with_openocd(tmp_path / "inventory"))

    assert OPENOCD_NO_HOSTTOOLS_NOTE not in _notes(root)


def test_a_resolved_serverpath_is_silent(tmp_path, monkeypatch):
    """CONTROL 2. The ordinary hosttools-bearing install. `PATH` is stripped
    bare deliberately: `serverpath` resolving is on its own sufficient to keep
    this quiet, and a host whose OpenOCD lives only inside the SDK is exactly
    the correctly-provisioned host that must never see this note."""
    root = _project_with(tmp_path, CONFIG_OPENOCD_RESOLVED)
    monkeypatch.setenv("PATH", _path_without_openocd(tmp_path / "inventory"))

    notes = _notes(root)

    assert OPENOCD_NO_HOSTTOOLS_NOTE not in notes


def test_the_mixed_host_is_silent(tmp_path, monkeypatch):
    """CONTROL 3, tan-cli#1179's already-measured case: system OpenOCD on
    `PATH`, no `hosttools/`, so `searchDir` is copied through pointing at a
    directory that does not exist. Measured benign upstream, and this host
    debugs fine -- warning on it would be the false alarm this note exists to
    avoid becoming."""
    root = _project_with(tmp_path, CONFIG_OPENOCD_SYSTEM_SEARCH_MISSING)
    monkeypatch.setenv("PATH", _path_with_openocd(tmp_path / "inventory"))

    resolution, registered_runners, _core_id = _resolve_from_build(
        root, ZEPHYR_MCU, OPENOCD, None
    )
    draft = create_launch_draft(ZEPHYR_MCU, OPENOCD, None)
    apply_launch_resolution(draft, resolution)

    # The nonexistent search directory really is carried into the profile --
    # that is the shape being declared silent, not an absence of one.
    assert draft["searchDir"] == [
        "/zephyr-sdk/hosttools/sysroots/x86_64-pokysdk-linux/usr/share/openocd/scripts"
    ]
    assert draft["serverpath"] == "/usr/bin/openocd"
    assert OPENOCD_NO_HOSTTOOLS_NOTE not in _preview_notes_for(
        draft, registered_runners, OPENOCD
    )


def test_a_non_openocd_server_never_sees_the_note(tmp_path, monkeypatch):
    """CONTROL 4. `serverpath` is a cortex-debug/OpenOCD field; a J-Link
    profile has no OpenOCD `servertype` and no missing key to warn about."""
    from tan.core.debug_launch import JLINK

    root = _project_with(tmp_path, CONFIG_NO_OPENOCD)
    monkeypatch.setenv("PATH", _path_without_openocd(tmp_path / "inventory"))
    resolution, registered_runners, _core_id = _resolve_from_build(root, ZEPHYR_MCU, JLINK, None)
    draft = create_launch_draft(ZEPHYR_MCU, JLINK, None)
    apply_launch_resolution(draft, resolution)

    assert OPENOCD_NO_HOSTTOOLS_NOTE not in _preview_notes_for(
        draft, registered_runners, JLINK
    )


def test_the_note_rides_in_the_envelope_and_does_not_move_the_exit_code(tmp_path):
    """END TO END, through the real command: `ok: true`, exit 0, no issue --
    a note, not a failure. The suite-wide `_probe_tools_are_a_property_of_the
    -test` fixture already removes `openocd` from this process's `PATH`, and
    the child inherits that, so this is the no-hosttools host by construction.
    """
    import json
    import subprocess

    root = _project_with(tmp_path, CONFIG_NO_OPENOCD)
    package_root = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "SOURCE_DATE_EPOCH": "0",
        "PYTHONPATH": os.pathsep.join(
            [str(package_root), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tan",
            "debug-config",
            "--target-kind",
            ZEPHYR_MCU,
            "--server",
            OPENOCD,
            "--preview",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=root,
        env=env,
    )

    assert proc.stderr.strip() == "", proc.stderr
    envelope = json.loads(proc.stdout)
    assert proc.returncode == 0
    assert envelope["ok"] is True
    assert envelope["issues"] == []
    configuration = envelope["data"]["configuration"]
    assert "serverpath" not in configuration and "searchDir" not in configuration
    assert OPENOCD_NO_HOSTTOOLS_NOTE in envelope["data"]["notes"]
