# SPDX-License-Identifier: Apache-2.0
"""Re-derive the sysbuild image prefix of a slice's per-core `alp.conf` after
token substitution (alp-sdk#866, consumer side).

On a `--sysbuild` slice the planner passes the per-core `alp.conf` as
`-D<image>_EXTRA_CONF_FILE=<build>/alp.conf`, where `<image>` is the
basename of the app directory handed to `west build` -- sysbuild's own name
for the application image (`get_filename_component(app_name ${APP_DIR}
NAME)`). The planner computes that basename from the directory it EMITTED
in. A tokened plan's app dir is `${PROJECT_ROOT}/...`, so the same plan run
from a differently-named project root (`tan build --plan-from plan.json
--execute` in `/b/bar` for a plan emitted in `/a/foo`) names image `foo`
while sysbuild builds image `bar`: Zephyr ignores the unknown
`foo_EXTRA_CONF_FILE` and the image builds WITHOUT its per-core
configuration, with no error. alp-sdk's own orchestrator comment says a
consumer that relocates the project root must re-derive the prefix; this is
that re-derivation. `tan/planner/orchestrator.py` is an upstream mirror and
stays as emitted.

Rewrite, not refuse: the arg is identified by its VALUE (this slice's own
`<buildDir>/alp.conf`), not by its prefix, so retargeting it to the image
sysbuild will actually build is unambiguous, and the right prefix is a pure
function of the substituted command. Refusing would make every relocated
tokened plan for a `boot:`/OTA project unbuildable, when the planner already
said exactly how to fix it. Any other `-D<x>_EXTRA_CONF_FILE` (one naming
`mcuboot`, say) is not this slice's `alp.conf` and is left alone.
"""

from __future__ import annotations

import re
from dataclasses import replace

from tan.core.build_plan import BuildPlan, Slice, SliceCommand

_IMAGE_SCOPED = re.compile(r"^-D(?P<image>[^=]+)_EXTRA_CONF_FILE=(?P<value>.*)$", re.S)

#: `west build` options that take a separate value -- skipped when locating
#: the positional app-dir argument (same set alp-sdk's seam1 comparator uses).
_OPTIONS_WITH_VALUE = frozenset({"-b", "-d", "-p"})


def west_app_dir_basename(args: list[str]) -> str | None:
    """Basename of `west build`'s app-dir argument: the first non-option arg
    after `build`, or `None` when there is none."""
    try:
        i = args.index("build") + 1
    except ValueError:
        return None
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _OPTIONS_WITH_VALUE else 1
    if i >= len(args) or args[i] == "--":
        return None
    name = args[i].replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return name or None


def _is_slice_alp_conf(value: str, build_dir: str) -> bool:
    v = value.replace("\\", "/")
    b = build_dir.replace("\\", "/").rstrip("/")
    return v == f"{b}/alp.conf" or v.endswith(f"/{b}/alp.conf")


def rescope_command(command: SliceCommand, build_dir: str) -> SliceCommand:
    """`command` with its per-core `-D<image>_EXTRA_CONF_FILE` renamed to the
    image `west build` will create; unchanged when not sysbuild, when the app
    dir is unknown, or when the prefix already matches."""
    args = command.args
    if "--sysbuild" not in args:
        return command
    image = west_app_dir_basename(args)
    if image is None:
        return command
    new_args = []
    for arg in args:
        m = _IMAGE_SCOPED.match(arg) if isinstance(arg, str) else None
        if m and m.group("image") != image and _is_slice_alp_conf(m.group("value"), build_dir):
            arg = f"-D{image}_EXTRA_CONF_FILE={m.group('value')}"
        new_args.append(arg)
    if new_args == args:
        return command
    return replace(command, args=new_args)


def rescope_sysbuild_extra_conf(plan: BuildPlan) -> BuildPlan:
    """`plan` with every slice's command passed through `rescope_command`."""
    slices: list[Slice] = []
    for sl in plan.slices:
        if sl.command is None:
            slices.append(sl)
            continue
        cmd = rescope_command(sl.command, sl.build_dir)
        slices.append(sl if cmd is sl.command else replace(sl, command=cmd))
    return replace(plan, slices=slices)
