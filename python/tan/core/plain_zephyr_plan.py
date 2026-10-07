# SPDX-License-Identifier: Apache-2.0
"""A single-slice build plan for a `board.yaml`-less Zephyr example (tan-cli#1359).

alp-sdk's bench-style examples (`examples/aen/aen-inference-latency`, ...) are
plain Zephyr apps built per board target and ship no `board.yaml`, so the
planner has nothing to read. `tan build --project <dir> --board <target>`
synthesises the plan the planner WOULD have emitted for one Zephyr slice and
feeds it through the ordinary `tan build` pipeline -- the same token
substitution, host-Python resolver (#1326), toolchain / west-workspace
resolution, dispatch, pristine policy and envelope as a planned build. Nothing
here spawns or resolves anything; it only shapes data.

The slice mirrors a planner Zephyr slice: `west build -b <board> <app> --`
run with cwd `build/<core>-zephyr` (so west's own `build/` lands under it and
`--pristine`'s wipe guards still apply -- an explicit `-d` would disable them).
"""
from __future__ import annotations

import json
import re

from tan.core.plan_tokens import TOKEN_PYTHON, TOKEN_SDK_ROOT

#: A Zephyr board target: `<board>[@rev][/<soc>[/<cpucluster>[/<variant>]]]`.
_BOARD_TARGET = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*(/[A-Za-z0-9_.-]+){0,3}")
#: `-DNAME=value` / `-DNAME:TYPE=value` / `-DNAME`.
_DEFINE = re.compile(r"-D[A-Za-z_][A-Za-z0-9_]*(:[A-Za-z]+)?(=.*)?", re.DOTALL)


def board_target_problem(board: str) -> str | None:
    if not _BOARD_TARGET.fullmatch(board):
        return (
            f"`--board {board}` is not a Zephyr board target "
            "(e.g. `alp_e1m_aen803_m55_he/ae822fa0e5597ls0/rtss_he`)"
        )
    return None


def normalise_defines(defines: list[str]) -> tuple[list[str], str | None]:
    """`-D` values as CMake args: `FOO=1` and `-DFOO=1` both become `-DFOO=1`."""
    out: list[str] = []
    for raw in defines:
        item = raw if raw.startswith("-D") else f"-D{raw}"
        if not _DEFINE.fullmatch(item):
            return [], f"`-D {raw}` is not a CMake definition (`NAME=VALUE`)"
        out.append(item)
    return out, None


def core_id_for(board: str) -> str:
    """The slice's id: the board target's core part, else its board name."""
    parts = board.split("/")
    name = parts[2] if len(parts) >= 3 else parts[0]
    return re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_").lower() or "app"


def plain_zephyr_plan(app_dir: str, board: str, defines: list[str]) -> str:
    """The build-plan JSON text for one Zephyr slice building `app_dir`."""
    core = core_id_for(board)
    slice_dir = f"build/{core}-zephyr"
    out = f"{slice_dir}/build"
    plan = {
        "schemaVersion": 1,
        "planPathMode": "tokened",
        "generatedBy": "tan build --board",
        "boardYaml": "",
        "sku": "",
        "buildRoot": "build",
        "executionPolicy": {"unknownBackend": "fail", "missingTool": "skip", "nullCommand": "skip"},
        "slices": [
            {
                "coreId": core,
                "backend": "zephyr",
                "buildDir": slice_dir,
                "appDir": app_dir,
                "configArtefacts": [],
                "toolchain": {
                    "targetTriple": "arm-zephyr-eabi",
                    "compiler": "arm-zephyr-eabi-gcc",
                    "sysroot": None,
                    "id": "arm-zephyr-eabi",
                },
                "artifacts": {
                    "elf": f"{out}/zephyr/zephyr.elf",
                    "map": f"{out}/zephyr/zephyr.map",
                    "bin": f"{out}/zephyr/zephyr.bin",
                    "sizeReport": f"{out}/zephyr/zephyr.stat",
                    "symbols": f"{out}/zephyr/zephyr.symbols",
                    "compileCommands": f"{out}/compile_commands.json",
                    "outputDir": None,
                },
                "debug": {"console": "uart", "probe": None},
                "command": {
                    "tool": "west",
                    "args": [
                        "build", "-b", board, app_dir, "--",
                        f"-DPython3_EXECUTABLE={TOKEN_PYTHON}", *defines,
                    ],
                    "cwd": slice_dir,
                },
                "postCommands": [],
                "env": {"ALP_SDK_ROOT": TOKEN_SDK_ROOT},
                "envAppendPath": {
                    "EXTRA_ZEPHYR_MODULES": [TOKEN_SDK_ROOT],
                    "PYTHONPATH": [f"{TOKEN_SDK_ROOT}/scripts"],
                },
            }
        ],
        "sharedArtefacts": [],
        "warnings": [],
    }
    return json.dumps(plan, indent=2)
