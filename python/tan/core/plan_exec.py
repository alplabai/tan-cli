# SPDX-License-Identifier: Apache-2.0
"""Pure build-plan execution decisions (ADR-0020): turn the plan's
envAppendPath and executionPolicy into concrete env values and skip/fail
dispositions. No IO, no spawning -- the executor calls these and owns the IO."""
import ntpath
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


def normalize_path(path: Path) -> Path:
    """Lexical `.`/`..` collapse, no filesystem access -- port of
    `tan_core::path_guard::normalize` (crates/tan-core/src/path_guard.rs).
    Used (joined onto a resolved workspace root first) so a relative and an
    absolute spelling of the SAME `--sdk-root` normalize to the identical
    string before [`sdk_stamp_key`] compares them -- otherwise a relative
    `--sdk-root ../alp-sdk` byte-mismatches the absolute pointer `tan sdk
    switch` already pinned for the SAME checkout, thrashing a full wipe on
    every alternating invocation between the two forms (tan-cli#163).

    A `..` past the anchor (the drive/root, or nothing left on a relative
    path) is dropped rather than kept literal, matching `PathBuf::pop`'s own
    "false, does nothing" behaviour at the top."""
    anchor = path.anchor
    parts: list[str] = []
    for part in (path.parts[1:] if anchor else path.parts):
        if part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    if anchor:
        return Path(anchor).joinpath(*parts)
    return Path(*parts) if parts else Path(".")


class PolicyAction(StrEnum):
    SKIP = "skip"
    FAIL = "fail"


@dataclass(frozen=True)
class ExecutionPolicy:
    unknown_backend: PolicyAction | None = None
    missing_tool: PolicyAction | None = None
    null_command: PolicyAction | None = None


def sep_for_key(var: str) -> str:
    """The join separator for ONE envAppendPath var -- per-key, not uniformly
    os.pathsep. EXTRA_ZEPHYR_MODULES is a CMake list that Zephyr's
    zephyr_module.py splits on ';' on EVERY platform (never an OS path list);
    joining it with ':' on Linux/WSL fails `west build` configure with
    "is not a valid zephyr module"."""
    return ";" if var == "EXTRA_ZEPHYR_MODULES" else os.pathsep


def apply_env_append(base: list[tuple[str, str]], append: dict[str, list[str]]) -> None:
    """Append each value to its var using that var's separator, skipping a value
    already present segment-wise. A var absent from `base` is seeded from the
    appended values (the plan owns it). Mutates `base` in place."""
    for var, values in append.items():
        sep = sep_for_key(var)
        current = next((v for k, v in base if k == var), None)
        segments = current.split(sep) if current else []
        for val in values:
            if val not in segments:
                segments.append(val)
        joined = sep.join(segments)
        for i, (k, _) in enumerate(base):
            if k == var:
                base[i] = (var, joined)
                break
        else:
            base.append((var, joined))


def assemble_slice_env(
    slice_env: dict[str, str],
    env_append_path: dict[str, list[str]],
    inherited: Callable[[str], str | None],
    gap_fillers: Sequence[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Start from the slice's verbatim env, seed any envAppendPath var the slice
    doesn't pin from `inherited` (so an append EXTENDS rather than replaces an
    inherited PYTHONPATH), apply the appends, then merge the CLI's
    consumer-mechanism gap fillers -- "plan wins / CLI fills gaps"."""
    env: list[tuple[str, str]] = list(slice_env.items())
    for key in env_append_path:
        if not any(k == key for k, _ in env):
            value = inherited(key)
            if value is not None:
                env.append((key, value))
    apply_env_append(env, env_append_path)
    for key, value in gap_fillers:
        for i, (k, _) in enumerate(env):
            if k == key:
                env[i] = (key, value)
                break
        else:
            env.append((key, value))
    return env


def resolve_action(
    policy: ExecutionPolicy | None, key: str, default: PolicyAction
) -> PolicyAction:
    """Honour executionPolicy's entry when present, else the CLI's built-in
    behaviour for an older plan that omits it."""
    if policy is None:
        return default
    picked = getattr(policy, key, None)
    return picked if picked is not None else default


class SdkStampAction(StrEnum):
    KEEP = "keep"
    PRISTINE = "pristine"


def sdk_stamp_action(
    cached: str | None,
    current: str | None,
    cache_configured: bool,
    build_dir_overridden: bool,
    cwd_under_build_root: bool,
) -> SdkStampAction:
    """Whether to wipe a slice's build dir because it was configured against a
    different SDK root. West refuses to reconfigure a build dir whose CMake cache
    is bound to another source tree ("FATAL ERROR: refusing to proceed without
    --force"), which reaches the user as a bare "terminated with exit code: 1".

    A missing stamp on an already-configured dir reads as stale DELIBERATELY --
    it is the only way a build dir predating this feature ever self-heals."""
    if build_dir_overridden or not cwd_under_build_root or not cache_configured:
        return SdkStampAction.KEEP
    if current is None:
        return SdkStampAction.KEEP
    return SdkStampAction.KEEP if cached == current else SdkStampAction.PRISTINE


class PristineSkipped(StrEnum):
    """Why an explicit `tan build --pristine` did NOT wipe a slice's build
    dir -- port of the Rust oracle's `PristineSkipped`
    (`crates/tan-core/src/plan_exec.rs`, pre-tan-cli#269 deletion). Every
    member has a fixed `.reason()` sentence below rather than a message
    assembled at each call site, so `build.pristine-skipped` always names the
    SAME fact for the SAME cause.

    `NEVER_CONFIGURED` is not one of the two structural safety guards
    (`BUILD_DIR_OVERRIDDEN`/`CWD_OUTSIDE_BUILD_ROOT`) -- it is the ordinary
    "nothing to wipe" case, and (per the oracle's own comment) the only one
    of the three a stock plan reaches today: `write_sdk_stamp` itself
    creates `<cwd>/build`, so after any prior slice the directory exists
    holding nothing but tan's own stamp file."""

    BUILD_DIR_OVERRIDDEN = "build-dir-overridden"
    CWD_OUTSIDE_BUILD_ROOT = "cwd-outside-build-root"
    NEVER_CONFIGURED = "never-configured"

    def reason(self) -> str:
        """One clause naming what stopped the wipe -- phrased as the reason,
        not as an apology, verbatim from the oracle's own `reason()`."""
        return {
            PristineSkipped.BUILD_DIR_OVERRIDDEN: (
                "the slice redirects west's build dir (`-d`/`--build-dir`), so tan "
                "cannot know which directory to wipe"
            ),
            PristineSkipped.CWD_OUTSIDE_BUILD_ROOT: (
                "the plan cwd is not under `build/`, so the wipe target could hold "
                "files the build never created"
            ),
            PristineSkipped.NEVER_CONFIGURED: (
                "that build dir has no CMakeCache.txt — it was never configured, "
                "so there was nothing to wipe"
            ),
        }[self]


def pristine_suppression(
    force_pristine: bool,
    build_dir_overridden: bool,
    cwd_under_build_root: bool,
    cache_configured: bool,
) -> PristineSkipped | None:
    """Whether an explicit `--pristine` was suppressed, and why -- `None` when
    the wipe actually happened or none was asked for (tan-cli#183, tan-cli#427).

    ONE decision for all THREE suppression paths, rather than a message
    bolted onto whichever branch the caller happens to be in. Order is
    most-specific-first: an overridden build dir is reported even when the
    cwd would also have failed, because that is the fact the user must
    change first."""
    if not force_pristine:
        return None
    if build_dir_overridden:
        return PristineSkipped.BUILD_DIR_OVERRIDDEN
    if not cwd_under_build_root:
        return PristineSkipped.CWD_OUTSIDE_BUILD_ROOT
    if not cache_configured:
        return PristineSkipped.NEVER_CONFIGURED
    return None


def sdk_stamp_key(sdk_root: str | None, sdk_commit: str | None) -> str | None:
    """The identity [`sdk_stamp_action`] actually compares, and the executor
    writes to `.tan-sdk-root`: the resolved SDK root path alone is not enough,
    because a standalone checkout that changes CONTENT at the SAME path (a
    `git pull`/`git checkout <tag>` in a `--sdk-root ../alp-sdk` checkout)
    leaves a path-only stamp matching while the build dir is actually stale.
    Folding the plan's own `sdkCommit` in closes it: a freshly emitted plan
    always reflects the SDK checkout's CURRENT git HEAD, so a content change
    at the same root changes the key.

    `sdk_commit` blank or absent (an older plan, or an SDK checkout with no
    resolvable git HEAD) degrades the key to the root alone -- the historical,
    path-only stamp. `None` when `sdk_root` itself is unresolved -- nothing to
    key on at all."""
    if sdk_root is None:
        return None
    commit = sdk_commit.strip() if sdk_commit is not None else ""
    return f"{sdk_root}@{commit}" if commit else sdk_root


def west_build_source_dir(args: Sequence[str]) -> str | None:
    """The SOURCE_DIR positional `west build`'s own `_sanity_check_source_dir`
    resolves -- `None` when `args` carries no `-b`/`--board` flag (a shape
    `orchestrator.py` never emits for a `west build` slice).

    `orchestrator.py` always emits the source dir as the token immediately
    following `-b`'s own value: `cmd = ["west", "build", "-b", slice_.board,
    app_dir]` (`tan.planner.orchestrator`, the `zephyr` backend's command
    builder). That is NOT the same as `args[-1]`, the guess this replaces --
    a real zephyr slice's command tail is never just that four-element
    prefix. `cmd += ["--", *defines]` always appends at least one CMake
    define (`defines` starts life as `["-DPython3_EXECUTABLE=${PYTHON}"]`
    and is never emptied), and a sysbuild slice additionally inserts
    `--sysbuild` between the source dir and that `--` separator -- so
    neither "the last token" nor "the last token before `--`" is safe.
    Anchoring on `-b`'s own position instead matches exactly how the
    emitter places the source dir, regardless of what is appended after
    it."""
    for i, token in enumerate(args):
        if token in ("-b", "--board") and i + 2 < len(args):
            return args[i + 2]
    return None


def _build_dir_overridden(args: Sequence[str]) -> bool:
    """Verbatim mirror of `tan.commands.build.manifest.build_dir_overridden`
    -- duplicated, not imported: `tan/core` is pure/dependency-free and must
    not import from `tan/commands` (the reverse of the one real dependency
    direction in this codebase). Keep the two definitions in sync by hand if
    `west build`'s own build-dir flag set ever changes."""
    return any(a == "-d" or a == "--build-dir" or a.startswith("--build-dir=") for a in args)


#: tan-cli#697. `_pin_west_workspace` (`tan.commands.build.execute`) redirects
#: `west build`'s own spawned cwd to the resolved Zephyr workspace so west's
#: ancestor-`.west` walk lands on the right one -- but `west build`'s own
#: source-directory sanity check (`_sanity_check_source_dir`, upstream
#: `scripts/west_commands/build.py:534`, frozen third-party code this repo
#: does not own) then calls `os.path.relpath(self.source_dir)` with NO
#: explicit `start`, which defaults to `os.getcwd()` -- the pinned
#: WORKSPACE, not the slice's own project. On Windows, `os.path.relpath`
#: RAISES rather than returning a path when its two arguments live on
#: different drive letters (`ValueError: path is on mount 'E:', start on
#: mount 'C:'`, reproduced verbatim in the issue). There is no fix reachable
#: from tan's own side of the seam -- `west` is an upstream pip dependency,
#: not `crates/`/`tan/planner` -- so the only correct move is to REFUSE,
#: with a coded reason naming both mounts, before spawning a process
#: already known to crash.
CROSS_DRIVE_MSG = "west build cannot span two Windows drives"


#: tan-cli#801: the structural shape both of `tan.commands.build.execute`'s
#: missing-tool refusals emit -- the slice's own `command` precheck
#: (`` tool `{tool}` not found -- searched ... ``, at the START of the
#: message) and `_missing_post_tool`'s post-build-step refusal (`` slice
#: `{core_id}` post-build step N of M (`{label}`) cannot run: tool `{tool}`
#: not found -- ... ``, where the phrase sits mid-string, AFTER "cannot
#: run: "). `build_cmd._missing_tool_issues` used to key on
#: `message.startswith("tool `")`, which only ever matched the first shape
#: -- the #550 post-build-step skip's reason never promoted into
#: `issues[]`, silently, because the phrase is not at position 0 there.
#: Matched with `.search`, never `.match`/`startswith`, for exactly that
#: reason: the marker's POSITION in the message is not part of the
#: contract, only its shape is. Anchored on the backtick-delimited tool
#: name (not two independent "tool `" / "` not found" substring checks
#: with no adjacency requirement between them) so an unrelated message that
#: happens to contain both fragments separately cannot false-positive.
_MISSING_TOOL_PREFIX = "tool `"
_MISSING_TOOL_SUFFIX = "` not found"
MISSING_TOOL_RE = re.compile(
    re.escape(_MISSING_TOOL_PREFIX) + r"[^`]*" + re.escape(_MISSING_TOOL_SUFFIX)
)


def missing_tool_message(tool: str) -> str:
    """The one place `` tool `{tool}` not found `` is spelled. Every producer
    in `tan.commands.build.execute` (the slice's own `command` precheck AND
    `_missing_post_tool`'s post-build-step refusal) MUST build its message
    through this function rather than hand-writing the literal -- MISSING_TOOL_RE
    above is built from the exact same `_MISSING_TOOL_PREFIX`/`_MISSING_TOOL_SUFFIX`
    pair, so a wording edit made only at a call site (not here) silently stops
    `build_cmd._missing_tool_issues` from promoting that producer's refusals
    into `issues[]` again -- the exact #801 regression this coupling exists to
    prevent."""
    return f"{_MISSING_TOOL_PREFIX}{tool}{_MISSING_TOOL_SUFFIX}"


@dataclass(frozen=True)
class CrossDriveRefusal:
    #: Full explanation, naming both mounts and both paths -- this run's
    #: stdout and the envelope's `data.slices[].reason`.
    message: str
    #: Short form for `system-manifest.yaml` `slices[].reason` -- names both
    #: mounts, drops the absolute paths and the upstream-mechanism
    #: explanation. Same split, for the same reason, as the missing-tool
    #: refusal's own `manifest_message` (`tan.commands.build.execute`): a
    #: build ARTEFACT that outlives the run and gets forwarded should not
    #: carry a customer's private directory layout.
    manifest_message: str


def cross_drive_source_refusal(
    workspace_dir: Path | None, args: Sequence[str]
) -> CrossDriveRefusal | None:
    """`None` when `_pin_west_workspace` is safe to redirect `west build`'s
    cwd to `workspace_dir`; else the tan-cli#697 refusal, naming both
    mounts, for a slice that pin would otherwise send straight into the
    `ValueError` [`CROSS_DRIVE_MSG`] documents.

    The no-op conditions mirror `_pin_west_workspace`'s own guard verbatim:
    a slice that pin never rewrites can never hit the crash the pin
    introduces, so it must never be refused here either.

    `ntpath.splitdrive`, never `os.path.splitdrive`: the two-drive-letter
    shape this exists to catch is a WINDOWS filesystem fact, independent of
    whatever platform is running this check right now. `ntpath` parses
    `C:\\...`/`C:/...` identically on every host -- which is what makes
    this function exercisable (and unit-tested) on a POSIX box with no
    two-drive Windows filesystem to reproduce against, unlike the crash it
    heads off, which only ever fires on Windows itself. Either side missing
    a drive letter (a POSIX path, or a Windows UNC share) returns `None`,
    not a refusal -- that combination is not the shape the issue reproduced,
    and a false "no refusal" there only leaves west to run exactly as it
    does today, never a NEW failure this function introduces. The
    comparison itself is case-insensitive (`C:` and `c:` name the same
    mount)."""
    if workspace_dir is None or not args or args[0] != "build" or _build_dir_overridden(args):
        return None
    source = west_build_source_dir(args)
    if source is None:
        return None
    ws_drive, _ = ntpath.splitdrive(str(workspace_dir))
    src_drive, _ = ntpath.splitdrive(source)
    if not ws_drive or not src_drive or ws_drive.lower() == src_drive.lower():
        return None
    message = (
        f"{CROSS_DRIVE_MSG} -- the resolved west workspace `{workspace_dir}` is on "
        f"mount `{ws_drive}`, but this slice's own project source directory `{source}` "
        f"is on mount `{src_drive}`. west build's own source-directory check "
        f"(`os.path.relpath`, upstream `scripts/west_commands/build.py`) raises rather "
        f"than resolving a path across two Windows drives -- move the project or the "
        f"`tan bootstrap`-created workspace so both live on the same drive."
    )
    return CrossDriveRefusal(message, f"{CROSS_DRIVE_MSG} ({ws_drive} vs {src_drive})")
