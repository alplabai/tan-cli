# SPDX-License-Identifier: Apache-2.0
"""What to tell a `tan init --from-example` user whose copied example reads a
pre-generated `generated/alp.conf` (alp-sdk#866).

alp-sdk#866 deleted the example `CMakeLists.txt` bridge that wrote the
per-core `alp.conf` at configure time. An example's twister scenarios and
bare `west build` lines now pass `EXTRA_CONF_FILE=generated/alp.conf`, and
its prose points at alp-sdk's `scripts/gen_example_alp_conf.py` to write it.
That script only walks alp-sdk's own `examples/`: run against a project
`tan init --from-example` copied out, it exits 0 having written nothing.
`tan build` needs none of it (the plan writes and passes its own `alp.conf`),
but a bare `west build`/twister run in the copied project names a missing
overlay, which Zephyr refuses.

`--from-example` copies the example verbatim -- the prose is the SDK's, at
the bound SDK's revision, and tan does not rewrite it. What tan CAN do
honestly is say, once, which command writes the file in this project:
`tan generate --target zephyr-conf --core <id> --output <dir>/generated/alp.conf`
writes exactly the bytes the build plan's `configArtefacts` carry for that
core. Pure: takes the planned files, returns the message or `None`.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from typing import Protocol

#: The reference the message is about. `(?<![\w])` rejects Zephyr's
#: per-image `<image>_EXTRA_CONF_FILE=` spelling.
_READS_GENERATED = re.compile(r"(?<![\w])EXTRA_CONF_FILE=generated/alp\.conf(?![\w./])")

#: Core OSes that have no Zephyr `alp.conf` at all.
_NON_ZEPHYR_OS = frozenset({"off", "yocto", "baremetal", "linux"})

_CORE_PLACEHOLDER = "<core-id>"


class _File(Protocol):
    relative_path: str
    content: str


def _dir_of(path: str) -> str:
    return posixpath.dirname(path)


def _app_dirs(cores: object, paths: set[str]) -> dict[str, list[str]]:
    """`{project-relative west app dir: [core ids]}` for every Zephyr-capable
    core. Mirrors the planner's `_zephyr_app_dir` rule: the `app:` directory
    itself when it carries a `CMakeLists.txt`, else its parent."""
    out: dict[str, list[str]] = {}
    if not isinstance(cores, dict):
        return out
    for core_id, cfg in cores.items():
        if not isinstance(core_id, str) or not isinstance(cfg, dict):
            continue
        if str(cfg.get("os", "")).lower() in _NON_ZEPHYR_OS:
            continue
        app = cfg.get("app")
        if not isinstance(app, str) or not app.strip():
            continue
        app_dir = posixpath.normpath(app.strip())
        app_dir = "" if app_dir == "." else app_dir
        if posixpath.join(app_dir, "CMakeLists.txt") not in paths:
            app_dir = _dir_of(app_dir)
        out.setdefault(app_dir, []).append(core_id)
    return out


def pregeneration_message(
    files: Iterable[_File], board_cores: object, sdk_display: str, subject: str
) -> str | None:
    """The one-paragraph instruction, or `None` when no copied file reads
    `generated/alp.conf`. *board_cores* is the copied board.yaml's parsed
    `cores:` mapping (anything else degrades to the `<core-id>` placeholder).
    """
    files = list(files)
    readers = sorted(f.relative_path for f in files if _READS_GENERATED.search(f.content))
    if not readers:
        return None
    by_dir = _app_dirs(board_cores, {f.relative_path for f in files})
    commands = []
    for directory in sorted({_dir_of(r) for r in readers}):
        candidates = by_dir.get(directory, [])
        core = candidates[0] if len(candidates) == 1 else _CORE_PLACEHOLDER
        output = posixpath.join(directory, "generated/alp.conf")
        commands.append(
            f"tan generate --target zephyr-conf --core {core} "
            f"--sdk-root {sdk_display} --output {output}"
        )
    return (
        f"{subject} reads generated/alp.conf ({', '.join(readers)}): `tan build` "
        "writes its own and does not need it, but a bare `west build` or twister "
        "run in this project does. Write it with: "
        + "; ".join(f"`{c}`" for c in commands)
        + ". The example's own pointer, alp-sdk's scripts/gen_example_alp_conf.py, "
        "only writes inside alp-sdk's examples/ (alp-sdk#866)."
    )
