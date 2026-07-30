# SPDX-License-Identifier: Apache-2.0
"""Post-build output seam for `execute_slices`: write the `system-
manifest.yaml` the downstream `tan run`/`tan flash`/`tan size`/`tan image`
contract reads, and resolve the real on-disk `zephyr.elf` a slice produced.

Port of `crates/tan-cli/src/commands/build/execute/manifest.rs`, trimmed to
this port's current scope the same way `tan.commands.build.execute` already
is (see that module's docstring): no Zephyr-boilerplate-loaded guard. What IS
ported: the post-build `write_post_build_manifest` seam and its two in-memory
signals (`write_failed_reason` / `native_sim_target`), `resolve_zephyr_artefact`'s
default-nested-build-dir artefact resolution, and the SDK-identity stamp
(`sdk_stamp_path`/`read_sdk_stamp`/`write_sdk_stamp`/`cmake_cache_configured`)
the sdk-switch-pristine guard in `execute.py` reads and writes (issue #52).

**Why `sdk_root`/`board_yaml` stay ACCEPTED overrides here rather than always
required.** The Rust oracle's `write_post_build_manifest` takes a
`ProjectContext` its caller (`crates/tan-cli/src/commands/build/execute/mod.rs`)
already resolved. This port's real caller, `tan.commands.build_cmd._dispatch` /
`_build`, now threads its own already-resolved `--sdk-root` straight through
(`execute_slices(..., sdk_root=sdk_root)` -> [`write_post_build_manifest`]) --
mirroring the oracle's `ProjectContext` exactly. The fallback below (the plan's
own `boardYaml` field plus [`tan.commands.build_cmd.discover_sdk_root`]'s exact
candidate ladder) stays for any OTHER caller of `execute_slices` that passes no
`sdk_root` at all (every test in this port's own suite does exactly that), so
the write is never permanently unreachable just because a caller omitted the
override.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tan.commands.build.materialise import MaterialiseError, confine_to_build_root
from tan.core.system_manifest import (
    SliceRunResult,
    SystemManifestError,
    overlay_run_results_raw,
    parse_system_manifest,
    parse_system_manifest_raw,
    serialize_system_manifest_raw,
)

__all__ = [
    "PostBuildManifest",
    "build_dir_overridden",
    "cmake_cache_configured",
    "read_sdk_stamp",
    "resolve_zephyr_artefact",
    "sdk_stamp_path",
    "write_post_build_manifest",
    "write_sdk_stamp",
]


@dataclass(frozen=True)
class PostBuildManifest:
    """Outcome of [`write_post_build_manifest`] -- the two independent,
    IN-MEMORY signals `tan run` decides host-vs-hardware and flash-safety
    from (see `tan.core.run`'s module doc). Neither field is ever derived by
    re-reading the on-disk file afterward. Port of `crates/tan-cli/.../
    execute/manifest.rs::PostBuildManifest`."""

    #: `None` when the on-disk file write succeeded; otherwise why it didn't.
    #: Never flips the build's own exit code -- only gates `tan run --flash`.
    write_failed_reason: str | None
    #: Whether THIS run's freshly emitted SDK system-manifest projection
    #: declares a `native_sim` slice -- independent of `write_failed_reason`
    #: (the emit and the file write are separate steps; a write failure must
    #: not erase a target the emit already established). `None` only when the
    #: emit itself failed or didn't parse -- no signal at all, not "not
    #: native_sim".
    native_sim_target: bool | None


def _native_sim_target_from_yaml(yaml_text: str) -> bool | None:
    """Whether THIS run's freshly emitted system-manifest YAML declares a
    `native_sim` slice. `None` only when `yaml_text` doesn't parse (no signal
    at all); a manifest with no `native_sim` slice is `False`, not `None`.
    Port of `crates/tan-cli/.../execute/manifest.rs::native_sim_target_from_yaml`."""
    from tan.core.run import native_sim_slice  # local: avoids a core<->core cycle at import time

    try:
        manifest = parse_system_manifest(yaml_text)
    except SystemManifestError:
        return None
    return native_sim_slice(manifest) is not None


def write_post_build_manifest(
    *,
    sdk_root: str | None,
    board_yaml: str | None,
    base: str,
    plan_build_root: str,
    results: Sequence[SliceRunResult],
) -> PostBuildManifest:
    """Write `<base>/<plan_build_root>/system-manifest.yaml` after a build:
    fetch the plan-time projection from the SDK (`--emit system-manifest`),
    overlay this run's per-slice status, serialize, and write. Best-effort --
    a failure here never flips the build's own exit code (the slices already
    ran; this is a post-build convenience write) -- but every failure branch
    reports `write_failed_reason` rather than failing silently.

    `native_sim_target` is derived from the SDK emit BEFORE the file write is
    even attempted, so it survives a write failure intact -- this is what lets
    `tan run` decide from THIS build's own outcome instead of a later disk
    read. Port of `crates/tan-cli/.../execute/manifest.rs::write_post_build_manifest`.
    """
    try:
        dest_dir = confine_to_build_root(Path(base), plan_build_root)
    except MaterialiseError as err:
        # KNOWN STRING DIVERGENCE (deliberate, not fixed): the oracle
        # (`manifest.rs::write_post_build_manifest`) guards `plan.build_root`
        # with the narrower `tan_core::is_plain_relative` shape check and
        # reports `"unsafe build_root `{plan.build_root}`"` on failure. This
        # port reuses `confine_to_build_root` -- the SAME resolve-and-contain
        # guard `materialise_plan` and this module's own `execute.py` slice-
        # cwd confinement already share -- which is a STRONGER check (it
        # also catches drive-relative/rooted Windows shapes a syntactic
        # `is_plain_relative` component scan would reject differently) but
        # reports a different message: `"path `{rel}` would escape the build
        # root `{root}`"`. No caller asserts the exact wording (verified:
        # no test in this port's suite matches either string), so the
        # divergence is recorded here rather than forking a third
        # `plan_build_root`-only guard duplicating `confine_to_build_root`'s
        # logic for a Minor, message-only difference.
        return PostBuildManifest(write_failed_reason=err.message, native_sim_target=None)

    effective_sdk_root = sdk_root
    effective_board_yaml = board_yaml
    if effective_sdk_root is None and effective_board_yaml:
        # Local import: `build_cmd.py` imports `tan.commands.build.execute`,
        # which imports this module, at module level -- a module-level import
        # of `discover_sdk_root` here would be circular. Same pattern as the
        # `tan.planner_root` import a few lines below and `tan.core.run` in
        # `_native_sim_target_from_yaml`, deferred to call time instead of
        # duplicating the candidate ladder.
        from tan.commands.build_cmd import discover_sdk_root

        discovered = discover_sdk_root(Path(effective_board_yaml).parent)
        effective_sdk_root = str(discovered) if discovered else None

    if effective_sdk_root is None or not effective_board_yaml:
        return PostBuildManifest(
            write_failed_reason=(
                "no alp-sdk checkout resolved for the post-build system-manifest emit"
            ),
            native_sim_target=None,
        )

    try:
        from tan.planner_root import emit as _planner_emit
    except ImportError as err:  # pragma: no cover -- planner absent from this build
        return PostBuildManifest(
            write_failed_reason=f"planner unavailable: {err}", native_sim_target=None
        )
    try:
        yaml_text = _planner_emit(
            "system-manifest", root=effective_sdk_root, board_yaml=Path(effective_board_yaml)
        )
    except Exception as err:  # noqa: BLE001 -- best-effort write, never escapes
        return PostBuildManifest(
            write_failed_reason=f"{type(err).__name__}: {err}", native_sim_target=None
        )

    # THIS run's own target signal -- computed from the emit, independent of
    # whether the file write below then succeeds.
    native_sim_target = _native_sim_target_from_yaml(yaml_text)

    try:
        raw = parse_system_manifest_raw(yaml_text)
        overlay_run_results_raw(raw, results)
        out = serialize_system_manifest_raw(raw)
    except SystemManifestError as err:
        return PostBuildManifest(
            write_failed_reason=err.message, native_sim_target=native_sim_target
        )
    except Exception as err:  # noqa: BLE001 -- best-effort write, never escapes
        # `serialize_system_manifest_raw` (`yaml.safe_dump`) is called OUTSIDE
        # any `try` in the oracle's own terms too -- `rewrite_manifest_yaml`
        # returns `Result<String, String>` for every branch, including the
        # dumper's own failure (e.g. a `!!binary` node PyYAML's SafeDumper
        # cannot represent). Uncaught here, that would escape
        # `write_post_build_manifest`, escape `execute_slices`, and abort a
        # build whose slices already ran -- breaking the documented
        # best-effort contract this function's own docstring states.
        return PostBuildManifest(
            write_failed_reason=f"{type(err).__name__}: {err}",
            native_sim_target=native_sim_target,
        )

    dest = dest_dir / "system-manifest.yaml"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        # newline="" -- PRESERVE the LF `yaml.safe_dump` always emits, rather
        # than Python's default universal-newline translation (CRLF on
        # Windows), which made this contract file diverge byte-for-byte from
        # the oracle's `std::fs::write` (raw bytes, no translation). Same
        # convention already documented at materialise.py:63 and
        # image_cmd.py:319-330.
        dest.write_text(out, encoding="utf-8", newline="")
    except OSError as err:
        return PostBuildManifest(
            write_failed_reason=str(err), native_sim_target=native_sim_target
        )

    return PostBuildManifest(write_failed_reason=None, native_sim_target=native_sim_target)


def build_dir_overridden(cmd_args: Sequence[str]) -> bool:
    """True when the slice's argv redirects west's build dir away from the
    default nested `<cwd>/build` (`-d <dir>`, `--build-dir <dir>`, or
    `--build-dir=<dir>`) -- the one case [`resolve_zephyr_artefact`]'s default
    path can't follow. Port of `crates/tan-cli/.../execute/manifest.rs::
    build_dir_overridden`."""
    return any(
        a == "-d" or a == "--build-dir" or a.startswith("--build-dir=") for a in cmd_args
    )


def resolve_zephyr_artefact(
    slice_cwd: Path, cmd_args: Sequence[str]
) -> tuple[str | None, str | None]:
    """After a slice builds `ok`, resolve the real `zephyr.elf` west produced
    and its build dir. West's default output is a nested `build/` under the
    slice's run cwd, so the elf lands at `<cwd>/build/zephyr/zephyr.elf`.
    Returned ABSOLUTE (lexically, via `os.path.abspath` -- matching
    `_abs_posix`'s convention elsewhere in this port -- never resolved through
    a symlink) so every downstream consumer uses the path verbatim. `(None,
    None)` when the slice's own command redirects the build dir
    (`-d`/`--build-dir`) -- west then wrote somewhere other than the default
    nested path, so the default location can't be trusted.

    Deliberately NOT mtime-gated -- see the Rust oracle's own docstring on
    why: a repeat `west build` with nothing to rebuild leaves `zephyr.elf`'s
    mtime unchanged, so an mtime gate would drop a perfectly valid artefact on
    every no-op/incremental rebuild. `is_file()` at the default path is
    sufficient on its own. Port of `crates/tan-cli/.../execute/manifest.rs::
    resolve_zephyr_artefact`."""
    if build_dir_overridden(cmd_args):
        return None, None
    west_build = slice_cwd / "build"
    elf = west_build / "zephyr" / "zephyr.elf"
    if elf.is_file():
        return os.path.abspath(str(elf)), os.path.abspath(str(west_build))
    return None, None


#: Filename of the tan-owned SDK-identity stamp (issue #52). Lives INSIDE
#: west's own nested build dir (`<cwd>/build`, not `<cwd>` itself), so wiping
#: that dir on a mismatch retires the stamp atomically -- it can never outlive
#: or misdescribe a build dir that no longer exists. Port of
#: `crates/tan-cli/.../execute/manifest.rs::sdk_stamp_path`'s hard-coded name.
_SDK_STAMP_FILE = ".tan-sdk-root"


def sdk_stamp_path(slice_cwd: Path) -> Path:
    """Path of the tan-owned SDK-identity stamp for one slice."""
    return slice_cwd / "build" / _SDK_STAMP_FILE


def read_sdk_stamp(slice_cwd: Path) -> str | None:
    """The SDK identity key recorded in the stamp file, if any. `None` covers
    "never stamped", "unreadable", AND "unreadable as UTF-8" -- all three
    read as "no signal", which `sdk_stamp_action` treats as a mismatch on an
    already-configured dir. The value is whatever [`write_sdk_stamp`] wrote
    -- the bare `--sdk-root` path on an older stamp, or
    `tan.core.plan_exec.sdk_stamp_key`'s `<root>@<sdkCommit>` form once the
    plan carries a commit -- this function does not itself parse or care
    which. Port of `manifest.rs::read_sdk_stamp`, whose `std::fs::
    read_to_string` folds invalid-UTF-8 into the SAME `io::Error` a missing
    file raises, one `.ok()`; `UnicodeDecodeError` is a `ValueError`, not an
    `OSError`, so `except OSError` alone let a corrupt (non-UTF-8)
    `.tan-sdk-root` escape this function as an uncaught exception -- verified
    to reach past `execute_slices`, whose own module docstring promises no
    escaping exception."""
    try:
        return sdk_stamp_path(slice_cwd).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def write_sdk_stamp(slice_cwd: Path, key: str) -> None:
    """Write the stamp BEFORE the tool is spawned (see the executor call
    site): a mid-configure failure still stamped correctly, since the dir
    really was configured against `key` regardless of whether the build
    finishes. `key` is `tan.core.plan_exec.sdk_stamp_key`'s output. Raises
    `OSError` on failure -- the caller treats the write as best-effort (a
    write failure just means the next run may treat this dir as unstamped:
    fails toward a spurious rebuild, not toward trusting a stale one). Port
    of `manifest.rs::write_sdk_stamp`."""
    path = sdk_stamp_path(slice_cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key, encoding="utf-8")


def cmake_cache_configured(slice_cwd: Path) -> bool:
    """Whether west has ever configured this slice's build dir (`<cwd>/build/
    CMakeCache.txt`) -- the "was this dir ever configured" signal
    `sdk_stamp_action` needs before it treats a missing stamp as stale. Port
    of `manifest.rs::cmake_cache_configured`."""
    return (slice_cwd / "build" / "CMakeCache.txt").is_file()
