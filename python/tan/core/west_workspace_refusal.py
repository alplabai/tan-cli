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

from tan.core.bootstrap import get_manifest_path

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
    message = (
        f"{WORKSPACE_UNRESOLVED_MSG} -- `west build` would run from `{spawn_cwd}`, "
        f"which has no `.west` on any ancestor; {_unresolved_facts(zephyr_base, sdk_root)}. "
        f"west would stop with `unknown command \"build\"`. {_remedies(sdk_root)}"
    )
    return WorkspaceRefusal(message, f"{WORKSPACE_UNRESOLVED_MSG} (run `tan bootstrap`)")


def _unresolved_facts(zephyr_base: str | None, sdk_root: Path) -> str:
    zephyr_base_fact = (
        f"ZEPHYR_BASE `{zephyr_base}` has no `.west` above it either"
        if zephyr_base
        else "ZEPHYR_BASE is unset"
    )
    sdk_parent = sdk_root.parent
    return (
        f"{zephyr_base_fact}; and neither `{sdk_parent}` nor "
        f"`{sdk_parent / 'zephyrproject'}` (next to --sdk-root `{sdk_root}`) holds a `.west`"
    )


def _remedies(sdk_root: Path) -> str:
    return (
        f"Run `tan bootstrap --sdk-root {sdk_root}` to create a workspace for this "
        f"checkout, or set ZEPHYR_BASE to an existing workspace's `zephyr/` -- that "
        f"builds against that workspace's Zephyr revision and patches, which need "
        f"not match this checkout's `west.yml`."
    )


def manifest_project(topdir: Path) -> Path | None:
    """The manifest project `<topdir>/.west/config` names, or `None` when the
    config is missing or names none."""
    try:
        config = (topdir / ".west" / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    rel = get_manifest_path(config)
    return topdir / rel.strip() if rel else None


def unresolved_workspace_verdict(
    start: Path, zephyr_base: str | None, sdk_root: Path
) -> tuple[str, str]:
    """tan-cli#1432: `tan doctor`'s `workspace` verdict when tan resolved no
    workspace for `sdk_root`, as `(status, detail)`, agreeing with what
    `tan build` will then do from `start`.

    `warn` when west's own unguarded walk still lands on a `.west` -- above
    `start` (west uses it; the build succeeds against THAT workspace's Zephyr),
    or above `zephyr_base` (west's fallback, which is not verified to reach
    the `build` extension, hence "may"). `fail` otherwise, naming the same
    facts and remedies as `build.workspace-unresolved`.
    """
    sdk_root = Path(sdk_root)
    found = west_ancestor(start)
    lead = f"west will use `{found}`, a `.west` above `{Path(start).absolute()}`"
    if found is None and zephyr_base:
        found = west_ancestor(Path(zephyr_base))
        lead = f"west may fall back to `{found}`, a `.west` above ZEPHYR_BASE `{zephyr_base}`"
    if found is None:
        return "fail", (
            f"no Zephyr workspace for --sdk-root `{sdk_root}`: `{Path(start).absolute()}` has "
            f"no `.west` on any ancestor; {_unresolved_facts(zephyr_base, sdk_root)}, so "
            f"a `west build` slice is refused as `build.workspace-unresolved`. {_remedies(sdk_root)}"
        )
    names = manifest_project(found)
    manifest = f"names `{names}` as its manifest" if names else "names no manifest project"
    return "warn", (
        f"no workspace has --sdk-root `{sdk_root}` as its manifest, but {lead}, which "
        f"{manifest} -- a build uses that workspace's Zephyr revision and patches, which "
        f"need not match this checkout's `west.yml`. Run `tan bootstrap --sdk-root "
        f"{sdk_root}` for a workspace of its own."
    )
