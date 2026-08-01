# SPDX-License-Identifier: Apache-2.0
"""The seam `tan image` and `tan size` share: both consume what a build
PRODUCED, so both resolve the same project context, the same build root, and the
same `build/system-manifest.yaml`.

Written once here rather than twice in the two command modules -- that
duplication is how the two halves of the same rule drift, and I-18 (the
`west build` nesting) is exactly such a rule: it has to hold identically for the
slice `tan size` measures and the slice `tan image` archives.

Ports the CLI-side halves of `crates/tan-cli/src/commands/{size,image}.rs` --
`resolve_cli_project_context`, `resolve_sdk_root`, the `app_path`/`--build-root`
resolution and the manifest load -- all of which are byte-identical between the
two commands in the oracle.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tan.commands.build_cmd import SDK_MARKER, resolve_sdk_root_ladder
from tan.commands.sdk_cmd import resolve_sdk_tiered
from tan.core.system_manifest import (
    MANIFEST_FILE,
    SystemManifest,
    SystemManifestError,
    parse_system_manifest,
)
from tan.envelope import Project, SdkInfo


def to_posix(path: str) -> str:
    """Forward slashes always. Mirrors `tan_core::project::to_posix`: every field
    in the extension/CLI handshake must be platform-identical."""
    return path.replace("\\", "/")


def normalise(path: str) -> str:
    """Rust's `normalize_path(cwd.join(p))`: cwd-anchored and lexically
    normalised, in the platform's own separators.

    `os.path.abspath`, not `Path.resolve()`: abspath is purely lexical, so a
    project reached through a symlink keeps the name the user typed and a path
    that does not exist yet still resolves.
    """
    return os.path.abspath(path)


def _has_loader(root: str) -> bool:
    """`scripts/alp_project.py` is THE marker for an alp-sdk checkout -- the same
    literal `tan_core::project` hardcodes (I-31). Spelled once, in
    `build_cmd.SDK_MARKER`."""
    return Path(root).joinpath(*SDK_MARKER).exists()


@dataclass
class ProjectContext:
    """What both commands report and build their paths from."""

    #: `project.root` -- posix, absolute.
    workspace_root: str
    #: The RESOLVER's own answer -- posix, absolute, joined onto `workspace_root`
    #: unconditionally, matching `project.rs::resolve_board_yaml_path`: the
    #: field names where one WOULD be. Existence is NOT checked here; `project()`
    #: below is the seam that checks it (tan-cli#236).
    board_yaml: str
    #: The envelope's `sdk` block, or `None` when nothing resolved (absent, never
    #: `null`).
    sdk: SdkInfo | None
    #: `ActiveSdk.broken_project_pin` carried through (tan-cli#263 review) --
    #: `size`/`image`'s own `_run` turns this into the shared
    #: `sdk.project-pin-unresolved` warning via `sdk_cmd.project_pin_issue`.
    broken_project_pin: str | None = None
    #: `ActiveSdk.tier`, unconditionally -- unlike `sdk` above, set even when
    #: nothing resolved to a usable checkout (`sdk is None`), which is exactly
    #: the shape `project_pin_issue` needs: a broken pin with NO working
    #: fallback still has a real tier (`"none"`) to name in its message.
    sdk_source_tier: str = "none"

    def project(self) -> Project:
        return Project.resolved(self.workspace_root, self.board_yaml)


def resolve_project_context(
    project_arg: str | None, board_yaml_arg: str | None, sdk_root_arg: str | None
) -> ProjectContext:
    """Port of `util::resolve_cli_project_context` for these two commands.

    The `sdk` block is populated from the SAME resolution that produced
    `workspace_root`, never a second lookup -- that is what `crate::sdk_report`
    exists to guarantee. A `--sdk-root` that is not a checkout resolves to
    NOTHING (core re-validates the loader marker), so the key is then absent
    rather than advertising a path no command could use.
    """
    workspace_root = normalise(project_arg or ".")
    configured = board_yaml_arg or "board.yaml"
    board_yaml = (
        configured
        if os.path.isabs(configured)
        else os.path.join(workspace_root, configured)
    )
    active = resolve_sdk_tiered(sdk_root_arg, Path(workspace_root))
    # `resolve_sdk_tiered` returns `--sdk-root` as-is EVEN WHEN INVALID (I-31,
    # deliberately, so `tan sdk` can report the path the user typed). The
    # envelope's `sdk` must not: `resolve_cli_project_context` only records what
    # core's own loader-marker check accepted.
    sdk = (
        SdkInfo(to_posix(active.path), active.tier)
        if active.path is not None and _has_loader(active.path)
        else None
    )
    return ProjectContext(
        to_posix(workspace_root),
        to_posix(board_yaml),
        sdk,
        active.broken_project_pin,
        active.tier,
    )


def resolve_metadata_sdk_root(
    sdk_root_arg: str | None, workspace_root: str
) -> Path | None:
    """The alp-sdk checkout `tan size` reads `metadata/**` out of -- port of
    `util::resolve_sdk_root`: `--sdk-root` (terminal) > the project's own
    `.alp/sdk-path` pin > the machine-global default (`~/.alp/sdk-default`) >
    the positional walk (`resolve_sdk_root_ladder`, the NARROW ladder shared
    with `build`/`doctor`/the ten other narrowly-resolving commands -- not the
    wide one `init`/`generate`/`examples`/`renode` take). No `ALP_SDK_ROOT` tier
    (tried and reverted -- see `resolve_sdk_root_ladder`'s own docstring).

    Kept separate from [`resolve_project_context`] because the oracle keeps them
    separate: `size.rs` calls BOTH, and they can legitimately disagree -- a
    child checkout supplies the memory budget while the envelope's `sdk` stays
    absent. Collapsing them would change which budget resolves.

    A missing SDK is NOT fatal (the retired Python's `find_sdk_root` die is
    deliberately dropped): the budget resolves `unknown` and measurement still
    runs.
    """
    flag = (sdk_root_arg or "").strip()
    if flag:
        # Terminal: a bad `--sdk-root` must fail loudly as "no budget resolved",
        # not silently fall through to some other checkout.
        return Path(flag) if _has_loader(flag) else None
    # `_broken_pin` unused here: `resolve_project_context` above already
    # resolved (and each caller already reports) the SAME `.alp/sdk-path`
    # pin's own status via `ProjectContext.broken_project_pin`; this walk is
    # the deliberately SEPARATE metadata-budget resolution the module
    # docstring describes, not a second envelope-facing resolution.
    found, _tier, _broken_pin = resolve_sdk_root_ladder(None, Path(workspace_root))
    return found


def read_sdk_som_and_soc(
    metadata_root: str, sku: str
) -> tuple[str, str | None, list[dict], float | None, list[tuple[str, float | None]]] | None:
    """`(silicon, silicon_variant, variants, soc_flash_mb, soc_cores)` for
    `sku`'s SoM preset + SoC JSON under `<sdk>/metadata` -- port of the ONE
    metadata-layout walk `util::read_sdk_som_and_soc` performs on the Rust
    side: `metadata/e1m_modules/<sku>.yaml` -> `silicon.split(':')` ->
    `metadata/socs/<vendor>/<family>/<part>.json`. `None` when the SoM preset
    itself cannot be read (missing, unreadable, wrong `schema_version`).

    `tan size` and `tan debug-config` both resolve a SoC variant off this same
    walk and must call ONE reader, not two that could disagree -- a schema
    with no reader, then two readers that could quietly diverge, is the exact
    drift alp-sdk#1026 itself is about. Leaf parsing stays
    `size_cmd._read_som_preset`/`_read_soc`; this only sequences them. A
    caller wanting a FINER "file missing" vs "file unreadable" split (`tan
    size`'s two different budget notes) still does its own existence check on
    `<metadata_root>/e1m_modules/<sku>.yaml` before calling this.

    Imported locally, not at module level: `size_cmd` already imports this
    module for `resolve_project_context`/`resolve_metadata_sdk_root`, so a
    top-level import the other way would cycle.
    """
    from tan.commands.size_cmd import _read_soc, _read_som_preset  # noqa: PLC0415

    preset_path = os.path.join(metadata_root, "e1m_modules", f"{sku}.yaml")
    preset = _read_som_preset(preset_path)
    if preset is None:
        return None
    silicon, silicon_variant = preset
    if not silicon:
        return silicon, silicon_variant, [], None, []
    parts = silicon.split(":")
    if len(parts) != 3:
        return silicon, silicon_variant, [], None, []
    soc_path = os.path.join(metadata_root, "socs", parts[0], parts[1], f"{parts[2]}.json")
    variants, soc_flash_mb, soc_cores = _read_soc(soc_path)
    return silicon, silicon_variant, variants, soc_flash_mb, soc_cores


def resolve_app_base(app_path: str | None, workspace_root: str) -> str:
    """The directory whose `build/` is the default build root.

    An explicit positional wins and is anchored on the CURRENT DIRECTORY, not on
    `--project` -- it is typed at a shell prompt, so a relative value means what
    the shell means by it (and `tan size app` therefore reports `project.root` as
    the cwd while reading `app/build`, which is what the oracle does). `size`
    spells "no positional" as the default `.`; `image` spells it as an absent
    argument; both then fall back to the resolved workspace.

    A STRING, joined with `os.path.join` -- see the note in
    `tan.core.system_manifest`: the workspace root is posix and `PathBuf::join`
    does not re-render it, so `pathlib` here would change every path this command
    reports.
    """
    if app_path is not None and app_path != ".":
        return os.path.join(os.getcwd(), app_path)
    return workspace_root


def resolve_build_root(build_root_arg: str | None, app_base: str) -> str:
    """`--build-root` absolute as-is, relative against the CURRENT DIRECTORY;
    default `<app_base>/build`."""
    if build_root_arg is None:
        return os.path.join(app_base, "build")
    if os.path.isabs(build_root_arg):
        return build_root_arg
    return os.path.join(os.getcwd(), build_root_arg)


class ManifestUnavailable(Exception):
    """`build/system-manifest.yaml` could not be READ -- absent, unreadable, a
    directory, or not UTF-8. All four are `*.manifest-unavailable` in the oracle,
    including the decode failure (`read_to_string` returns an io error for
    invalid UTF-8, so it never reaches the parser)."""

    def __init__(self, path: str, detail: str) -> None:
        super().__init__(detail)
        self.path = path
        self.detail = detail


class ManifestInvalid(Exception):
    """It was read but is not a v1 system-manifest."""

    def __init__(self, path: str, detail: str) -> None:
        super().__init__(detail)
        self.path = path
        self.detail = detail


def load_manifest(build_root: str) -> tuple[str, SystemManifest]:
    """Read + parse + version-guard the manifest, returning its raw text too --
    `tan image` needs the bytes for the additive-field passthrough that a typed
    projection would discard.

    Every failure is one of the two exceptions above, so neither command can
    ever let an OSError, a decode error or a YAML error escape as a traceback.
    """
    path = os.path.join(build_root, MANIFEST_FILE)
    try:
        # `newline=""`: no universal-newline translation, matching Rust's
        # `read_to_string`. Explicit encoding, never the locale default -- a
        # cp1252 host would raise UnicodeDecodeError on a manifest with any
        # non-ASCII byte (I-27's read-side rule).
        with open(path, encoding="utf-8", newline="") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError, ValueError) as err:
        raise ManifestUnavailable(path, str(err)) from err
    try:
        return text, parse_system_manifest(text)
    except SystemManifestError as err:
        raise ManifestInvalid(path, err.message) from err
