"""tan-cli#1429: refuse a `west build` slice up front when nothing will find a
west workspace for it, instead of letting west fail with its own
`west: unknown command "build"; do you need to run this inside a workspace?`.

The shape the issue reproduced: `--sdk-root` names an alp-sdk checkout that is
NOT the manifest project of any west workspace tan can see (a sibling git
worktree such as `~/.cache/sdk-dev-2784`), and the app lives outside every
workspace tree (`/tmp/...`). `tan.core.venv.west_workspace_dir` then returns
`None`, so `execute_slices` keeps the slice's own cwd (tan-cli#307's fallback),
and west -- finding no `.west` above that cwd -- never loads the `build`
extension at all. The envelope carried only the generic `build.slice-failed`.

**Keyed on what WEST will do, not on what tan resolved.** `workspace_dir is
None` alone is not a failure: an app inside some workspace tree still builds,
because west's own ancestor walk finds that `.west` without tan's manifest
guard (the "move it under the workspace and it builds" observation in the
issue), and an ambient `ZEPHYR_BASE` gives west a second place to look. The
refusal therefore fires only when neither the spawn cwd nor the spawn env's
`ZEPHYR_BASE` has a `.west` on any ancestor -- the case where west cannot
succeed. Anything less certain is left to west exactly as before.

**Reusing a workspace is deliberately not attempted.** tan has no record that
maps an SDK checkout to a workspace (`.west/tan-workspace-sdk` maps the other
way and sits inside the workspace), so picking one would be a guess -- the
thing tan-cli#307/#61/#292's manifest guard exists to refuse.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: The marker `build_cmd._workspace_unresolved_issues` matches to promote the
#: refused slice's reason into the coded `build.workspace-unresolved` issue,
#: the same idiom as `tan.core.plan_exec.CROSS_DRIVE_MSG`.
WORKSPACE_UNRESOLVED_MSG = "no west workspace resolves for this build"


@dataclass(frozen=True)
class WorkspaceRefusal:
    """`message` carries absolute paths for this run's stdout and envelope;
    `manifest_message` is the short form `system-manifest.yaml` keeps, without
    the host's directory layout (same split as `CrossDriveRefusal`)."""

    message: str
    manifest_message: str


def west_ancestor(start: Path) -> Path | None:
    """The nearest directory at or above `start` holding a `.west` directory,
    or `None`. Mirrors west's own `west_topdir` walk: unguarded, because west
    does not check which manifest a `.west` belongs to."""
    start = Path(start).absolute()
    for directory in (start, *start.parents):
        if (directory / ".west").is_dir():
            return directory
    return None


def workspace_unresolved_refusal(
    workspace_dir: Path | None,
    args: list[str],
    spawn_cwd: Path,
    zephyr_base: str | None,
    sdk_root: Path | None,
) -> WorkspaceRefusal | None:
    """`None` unless this `west build` slice cannot find a workspace.

    `zephyr_base` is the value the SPAWN's env carries, not `os.environ`'s.
    `sdk_root` absent is never refused: without it there is no checkout to
    name, and no `tan bootstrap --sdk-root` remedy to give.
    """
    if workspace_dir is not None or sdk_root is None or not args or args[0] != "build":
        return None
    if west_ancestor(spawn_cwd) is not None:
        return None
    if zephyr_base and west_ancestor(Path(zephyr_base)) is not None:
        return None
    zephyr_base_fact = (
        f"ZEPHYR_BASE `{zephyr_base}` has no `.west` above it either"
        if zephyr_base
        else "ZEPHYR_BASE is unset"
    )
    sdk_parent = sdk_root.parent
    message = (
        f"{WORKSPACE_UNRESOLVED_MSG} -- `west build` would run from `{spawn_cwd}`, "
        f"which has no `.west` on any ancestor; {zephyr_base_fact}; and neither "
        f"`{sdk_parent}` nor `{sdk_parent / 'zephyrproject'}` (next to --sdk-root "
        f"`{sdk_root}`) holds a `.west`. west would stop with `unknown command \"build\"`. "
        f"Run `tan bootstrap --sdk-root {sdk_root}` to create a workspace for this "
        f"checkout, or set ZEPHYR_BASE to an existing workspace's `zephyr/` -- that "
        f"builds against that workspace's Zephyr revision and patches, which need "
        f"not match this checkout's `west.yml`."
    )
    return WorkspaceRefusal(message, f"{WORKSPACE_UNRESOLVED_MSG} (run `tan bootstrap`)")
