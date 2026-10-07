# SPDX-License-Identifier: Apache-2.0
"""Where tan may take the J-Link Commander binary from (tan-cli#1336).

Flow D spawns the J-Link tool with the user's privileges while a probe is
attached, for the probe listing, the DPIDR preflight, the write and the
read-back. The workspace `.venv` is part of the PROJECT -- a checkout can ship
its own `.venv/bin/JLinkExe` -- so tan's "a west-capable venv is trusted"
rule (right for `west`) must not reach this program. It is resolved ONLY from:

1. an explicit `--jlink <path>` (a CLI input);
2. the `TAN_JLINK` environment variable (an environment input);
3. `PATH` as the user's own environment has it -- never with the project venv
   prepended;
4. a known SEGGER install root: `/opt/SEGGER/*`, `/Applications/SEGGER/*`,
   `%ProgramFiles%` / `%ProgramFiles(x86)%` `\\SEGGER\\*`.

Never a manifest field: `build/system-manifest.yaml` is regenerated from the
checkout and is exactly the surface a hostile project controls.

The ONE resolution is used by every J-Link spawn in a run, so the listing that
verifies the probe, the preflight that verifies the board, the write and the
read-back all execute the same binary, and the envelope reports it as
`jlink.binary`.
"""
from __future__ import annotations

import glob
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from tan.core.tool_lookup import resolve_tool

#: The binary identities, in preference order (same as `flash_plan._JLINK_BINARIES`).
JLINK_NAMES = ("JLinkExe", "JLink")

#: The environment override.
ENV_OVERRIDE = "TAN_JLINK"


@dataclass(frozen=True)
class JlinkBinary:
    """A resolved J-Link binary: absolute `path` and where it came from."""

    path: str
    source: str


def _install_roots(env: Mapping[str, str], platform: str) -> list[str]:
    roots: list[str] = []
    if platform.startswith("win"):
        for var in ("ProgramFiles", "ProgramFiles(x86)"):
            base = env.get(var)
            if base:
                roots += sorted(glob.glob(os.path.join(base, "SEGGER", "JLink*")), reverse=True)
    elif platform == "darwin":
        roots += sorted(glob.glob("/Applications/SEGGER/JLink*"), reverse=True)
    else:
        roots += sorted(glob.glob("/opt/SEGGER/JLink*"), reverse=True)
    return roots


def _in_dir(directory: str, platform: str) -> str | None:
    suffixes = ("", ".exe") if platform.startswith("win") else ("",)
    for name in JLINK_NAMES:
        for suffix in suffixes:
            candidate = os.path.join(directory, name + suffix)
            if os.path.isfile(candidate):
                return candidate
    return None


def resolve_jlink(
    cli_path: str | None,
    env: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
) -> JlinkBinary | None:
    """The J-Link binary tan may spawn, or `None` when none is found. An
    explicit `cli_path` / `TAN_JLINK` that does not name an existing file
    resolves to `None` rather than falling through: the operator named a
    binary, and quietly using a different one is the thing this refuses."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    for value, source in ((cli_path, "the --jlink flag"), (env.get(ENV_OVERRIDE), f"{ENV_OVERRIDE}")):
        if value:
            path = os.path.abspath(value)
            return JlinkBinary(path, source) if os.path.isfile(path) else None
    for name in JLINK_NAMES:
        found = resolve_tool(name, env).resolved
        if found:
            return JlinkBinary(os.path.abspath(found), "PATH")
    for root in _install_roots(env, platform):
        found = _in_dir(root, platform)
        if found:
            return JlinkBinary(found, f"SEGGER install {root}")
    return None
