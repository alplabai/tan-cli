# SPDX-License-Identifier: Apache-2.0
"""Pure build-plan token substitution (alp-sdk #865, "hermetic build plans").

A **tokened** plan (`planPathMode: "tokened"`) carries literal placeholders --
`${SDK_ROOT}` / `${PROJECT_ROOT}` / `${PYTHON}` / `${TOOLCHAIN_ROOT}` -- in its
path-bearing string fields instead of baking in the emitting machine's
absolute paths. This module is the CONSUMER side: ONE blind string-
substitution pass swapping the tokens for tan's already-resolved values, plus
the guards that keep a wrong substitution from silently building the wrong
image.

Pure -- no IO. The caller (`tan.commands.build.token_substitution`) resolves
the SDK checkout root, the project root (the `board.yaml` directory), the
planner-venv Python and the toolchain root exactly ONCE and hands them in as
`TokenValues`; it also owns invoking `git` for the `sdkCommit` check
(`sdk_commit_mismatches` here only compares strings).

A plan without `planPathMode: "tokened"` is untouched by
`substitute_plan_tokens`: a byte-identical no-op ONLY when the key is ABSENT
(`plan_path_mode is None`, `:438`) -- present-but-other raises
`UnknownPlanPathMode` (`:443-444`) instead. Reachable via `--plan-from`
handing in an older plan with no `planPathMode` key at all -- tan's own
in-process planner (`tan.planner.buildplan.emit_build_plan`) tags every
plan it renders `planPathMode: "tokened"` unconditionally (tan-cli#853), so
an ABSENT `planPathMode` is no longer "every plan the SDK emits today".

tan-cli #89: an unresolved `${TOOLCHAIN_ROOT}` splits into two outcomes
depending on WHERE it survives substitution. `boardYaml` and
`sharedArtefacts[]` have no owning slice -- no dispatch seam to route a
skip/fail decision to -- so they keep the hard `UnresolvedToolchainRoot` this
pass always raised. A slice field, though, has an owning slice AND an owning
dispatch seam (`executionPolicy.missingTool`, the same knob a missing
`bitbake` already uses): this is a HOST-provisioning fact, not a plan/version
bug, so this pass reports it as a `DemotedSlice` instead of erroring the
whole plan, leaving the skip-vs-fail call to the caller at dispatch time.
`LeftoverToken` is unaffected either way -- an unknown token is a
version/bug fact, never demoted, always plan-fatal.

Substituting an unresolved token with an empty string would sail past the
leftover-token guard and build against the wrong tree -- so every unresolved
token is REFUSED (or demoted, per the rule above), never degraded to "".

Ported field-for-field from `crates/tan-core/src/plan_tokens.rs`
(`substitute_plan_tokens` / `substitute_slice` / `substitute_command_lenient`
/ `substitute_artefact` / `substitute_artefact_lenient`), with one field the
Rust `BuildSlice` does not carry: `slices[].appDir`, which the Python
`Slice` (`tan.core.build_plan`) does have. It is substituted immediately
after `buildDir` -- the other slice-level bare path field -- and, like every
other slice field, participates in TOOLCHAIN_ROOT demotion / LeftoverToken
the same as its siblings. `appDir` is nullable per the SDK schema (a Yocto
slice built from the stock-image token has none) -- `None` passes through
untouched, the same as `command.cwd`.
"""
import sys
from dataclasses import dataclass, replace
from typing import Any

from tan.core.build_plan import BuildPlan, Slice, SliceCommand

PLAN_PATH_MODE_TOKENED = "tokened"

TOKEN_SDK_ROOT = "${SDK_ROOT}"
TOKEN_PROJECT_ROOT = "${PROJECT_ROOT}"
TOKEN_PYTHON = "${PYTHON}"
TOKEN_TOOLCHAIN_ROOT = "${TOOLCHAIN_ROOT}"


@dataclass(frozen=True)
class TokenValues:
    """tan's already-resolved substitution values. Resolve each exactly ONCE
    for the whole plan -- never re-resolved per-slice.

    `toolchain_root` is `None` (or blank) when this host has no toolchain
    root resolved at all. Unresolved is NOT an error by itself: every plan
    the SDK emits today names no `${TOOLCHAIN_ROOT}`, and those must keep
    building on a host with no detectable toolchain install -- resolution is
    lazy, the absence only becomes `UnresolvedToolchainRoot` when a plan
    actually uses the token. Blank is folded into "unresolved" on purpose:
    substituting `""` would turn `${TOOLCHAIN_ROOT}/bin/cmake` into the bare
    `/bin/cmake` and sail past the leftover-token guard with nothing left to
    catch -- the same hole `sdk_root` refuses.
    """

    sdk_root: str
    project_root: str
    python: str
    toolchain_root: str | None


@dataclass(frozen=True)
class DemotedSlice:
    """A slice whose fields still name `${TOOLCHAIN_ROOT}` with no host
    value. Reported, not erred: the CALLER routes it to
    `executionPolicy.missingTool` at dispatch -- this pass has no business
    deciding skip vs fail, only noticing the condition and naming where it
    hit."""

    slice_index: int
    core_id: str
    field: str


class PlanTokenError(Exception):
    """Why `substitute_plan_tokens` refused to hand back a plan."""


class LeftoverToken(PlanTokenError):
    """A `${...}`-shaped token survived substitution -- an unknown token (a
    5th token this CLI doesn't resolve), an unterminated `${` (truncation/
    typo), or a plan bug."""

    def __init__(self, field: str, token: str) -> None:
        super().__init__(
            f"plan field `{field}` still contains an unresolved token `{token}` after substitution"
        )
        self.field = field
        self.token = token


class UnresolvedToolchainRoot(PlanTokenError):
    """The plan names `${TOOLCHAIN_ROOT}` but this host has no toolchain root
    resolved, in a field with no owning slice to demote to."""

    def __init__(self, field: str) -> None:
        super().__init__(
            f"plan field `{field}` names `{TOKEN_TOOLCHAIN_ROOT}` but no toolchain root is "
            f"resolved on this host"
        )
        self.field = field


class UnknownPlanPathMode(PlanTokenError):
    """`planPathMode` is present but isn't the one value this pass knows
    (`"tokened"`)."""

    def __init__(self, mode: str) -> None:
        super().__init__(f'unknown planPathMode `{mode}` (only "tokened" is defined)')
        self.mode = mode


def sdk_commit_mismatches(plan_commit: str, resolved_commit: str) -> bool:
    """The split-brain guard: whether a plan's `sdkCommit` (when present)
    mismatches the resolved SDK checkout's actual HEAD. Compares by common-
    length prefix so a short (`git rev-parse --short HEAD`) and a full
    40-char SHA both compare correctly; case-insensitive. Either side blank
    -- an older plan without `sdkCommit`, or a caller that could not resolve
    `git rev-parse` (no `.git`, `git` missing) -- is "no signal", never a
    mismatch: an SDK checkout with no `.git` (a release tarball) is a
    normal, supported setup."""
    a, b = plan_commit.strip(), resolved_commit.strip()
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return a[:n].lower() != b[:n].lower()


def _normalize(path: str) -> str:
    """Lexically normalize (collapse `.`/`..`, drop empty segments) without
    touching the filesystem -- the Python analogue of tan-core's
    `path_guard::normalize`."""
    posix = path.replace("\\", "/")
    is_absolute = posix.startswith("/")
    parts = posix.split("/")
    out: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            # Matches Rust's `out.pop()`: a no-op on an empty accumulator,
            # never appends a literal "..".
            if out:
                out.pop()
            continue
        out.append(part)
    result = "/".join(out)
    return f"/{result}" if is_absolute else result


def project_root_diverges_from_exec_base(project_root: str, exec_base: str) -> bool:
    """Guard 3: whether `${PROJECT_ROOT}` (the resolved `board.yaml`'s
    directory) diverges from the executor's actual base dir. They're the
    same directory only in the default config -- when a plan is tokened,
    substituting `${PROJECT_ROOT}` from one and executing slices under the
    other would silently build against the wrong tree."""
    a, b = _normalize(project_root), _normalize(exec_base)
    if sys.platform.startswith("win"):
        # Lexical normalize doesn't fold drive-letter case, so
        # `--board-yaml e:/...` vs `--project E:/...` -- the same path --
        # would otherwise false-fail this guard.
        return a.lower() != b.lower()
    return a != b


def _resolved_toolchain_root(values: TokenValues) -> str | None:
    return values.toolchain_root if values.toolchain_root else None


def _apply(values: TokenValues, raw: str) -> str:
    out = raw.replace(TOKEN_SDK_ROOT, values.sdk_root)
    out = out.replace(TOKEN_PROJECT_ROOT, values.project_root)
    out = out.replace(TOKEN_PYTHON, values.python)
    root = _resolved_toolchain_root(values)
    if root is not None:
        out = out.replace(TOKEN_TOOLCHAIN_ROOT, root)
    return out


def _find_brace_token_from(value: str, offset: int) -> tuple[int, str] | None:
    """First `${...}`-shaped substring in `value` at or after `offset`,
    together with its start offset. An unterminated `${` (no closing `}`)
    still counts and consumes the rest of the string -- nothing after an
    unterminated brace could itself be a well-formed further token."""
    start = value.find("${", offset)
    if start == -1:
        return None
    rest = value[start:]
    end = rest.find("}")
    if end == -1:
        return start, rest
    return start, rest[: end + 1]


def _sub_field_lenient(field: str, raw: str, values: TokenValues) -> tuple[str, bool]:
    """Substitute `values` into `raw`, then scan the WHOLE result for every
    remaining `${...}`-shaped token -- not just the first: a field can carry
    BOTH an unresolved `${TOOLCHAIN_ROOT}` and a genuinely unknown token, and
    the unknown one must still fail loudly even when it comes second -- an
    unknown token is a version/bug fact and outranks a provisioning fact.
    Returns the substituted string plus whether an unresolved
    `${TOOLCHAIN_ROOT}` was seen; the caller decides whether that's
    plan-fatal (`_sub_field`) or demotable (`_substitute_slice` and its
    helpers)."""
    substituted = _apply(values, raw)
    unresolved_toolchain = False
    offset = 0
    while True:
        found = _find_brace_token_from(substituted, offset)
        if found is None:
            break
        start, token = found
        if token != TOKEN_TOOLCHAIN_ROOT:
            raise LeftoverToken(field, token)
        # Reaching here means values.toolchain_root is unresolved: `_apply`
        # already replaced every occurrence when a value WAS resolved.
        unresolved_toolchain = True
        offset = start + len(token)
    return substituted, unresolved_toolchain


def _sub_field(field: str, raw: str, values: TokenValues) -> str:
    """Plan-level sites (`boardYaml`, `sharedArtefacts[]`) have no owning
    slice to route a missing-toolchain skip to, so they keep the hard,
    byte-identical `UnresolvedToolchainRoot` this pass always raised."""
    substituted, unresolved_toolchain = _sub_field_lenient(field, raw, values)
    if unresolved_toolchain:
        raise UnresolvedToolchainRoot(field)
    return substituted


def _record_first(field: str, unresolved: bool, current: str | None) -> str | None:
    """Keep only the FIRST unresolved-toolchain field name; every field is
    still substituted (and LeftoverToken-scanned) regardless, so a bug in a
    LATER field of an already-demoted slice is never masked."""
    return field if unresolved and current is None else current


def _substitute_artefact(field: str, art: dict[str, Any], values: TokenValues) -> dict[str, Any]:
    out = dict(art)
    out["path"] = _sub_field(f"{field}.path", art["path"], values)
    out["contents"] = _sub_field(f"{field}.contents", art["contents"], values)
    return out


def _substitute_artefact_lenient(
    field: str, art: dict[str, Any], values: TokenValues
) -> tuple[dict[str, Any], str | None]:
    """The slice-owned variant of `_substitute_artefact`: `configArtefacts`
    live inside a slice, so an unresolved `${TOOLCHAIN_ROOT}` in one is
    demotable too (the whole artefact list is stripped from a demoted
    slice's output plan by the caller) -- a `${UNKNOWN}`, though, is still
    `LeftoverToken`, scanned for here BEFORE the caller ever gets a chance to
    strip the list."""
    demoted_field: str | None = None

    path_field = f"{field}.path"
    path_sub, path_unresolved = _sub_field_lenient(path_field, art["path"], values)
    demoted_field = _record_first(path_field, path_unresolved, demoted_field)

    contents_field = f"{field}.contents"
    contents_sub, contents_unresolved = _sub_field_lenient(contents_field, art["contents"], values)
    demoted_field = _record_first(contents_field, contents_unresolved, demoted_field)

    out = dict(art)
    out["path"] = path_sub
    out["contents"] = contents_sub
    return out, demoted_field


def _substitute_command_lenient(
    base: str, cmd: SliceCommand, values: TokenValues
) -> tuple[SliceCommand, str | None]:
    """`base` is the plan path of the command being substituted --
    `slices[N].command` for the slice's own, `slices[N].postCommands[M]` for
    one of its post-build steps (tan-cli#550). Parameterised rather than
    duplicated so a post-build step's `${SDK_ROOT}` / `${TOOLCHAIN_ROOT}` /
    `${UNKNOWN}` is substituted, demoted and refused by exactly the same rules
    as the slice's own command: those steps are spawned by the same executor,
    so a token this pass left standing in one would be handed to
    `subprocess` verbatim."""
    demoted_field: str | None = None

    new_cwd = cmd.cwd
    if new_cwd is not None:
        cwd_field = f"{base}.cwd"
        new_cwd, cwd_unresolved = _sub_field_lenient(cwd_field, new_cwd, values)
        demoted_field = _record_first(cwd_field, cwd_unresolved, demoted_field)

    new_args: list[str] = []
    for j, arg in enumerate(cmd.args):
        field = f"{base}.args[{j}]"
        sub, unresolved = _sub_field_lenient(field, arg, values)
        new_args.append(sub)
        demoted_field = _record_first(field, unresolved, demoted_field)

    return replace(cmd, cwd=new_cwd, args=new_args), demoted_field


def _substitute_slice(i: int, sl: Slice, values: TokenValues) -> tuple[Slice, str | None]:
    """Substitute every field of one slice, leniently for
    `${TOOLCHAIN_ROOT}`: returns the first field that still names it
    unresolved, if any, so the caller can build a `DemotedSlice` -- but a
    `${UNKNOWN}` anywhere in the slice still raises `LeftoverToken`
    immediately, ending the scan right there.

    Field order mirrors `substitute_slice` in the Rust oracle exactly
    (`buildDir`, then `configArtefacts`, then `env`, then `envAppendPath`,
    then `command`), with `appDir` -- a field the Rust `BuildSlice` doesn't
    carry -- inserted right after `buildDir`, the other bare slice-level path
    field, and `postCommands` -- a field the oracle predates entirely
    (alp-sdk #1344) -- last, immediately after the `command` it follows on the
    wire and in execution order."""
    demoted_field: str | None = None

    build_dir_field = f"slices[{i}].buildDir"
    build_dir, unresolved = _sub_field_lenient(build_dir_field, sl.build_dir, values)
    demoted_field = _record_first(build_dir_field, unresolved, demoted_field)

    # appDir is nullable (a Yocto slice built from the stock-image token has
    # none) -- guarded the same shape as command.cwd below, not substituted
    # when absent.
    app_dir = sl.app_dir
    if app_dir is not None:
        app_dir_field = f"slices[{i}].appDir"
        app_dir, unresolved = _sub_field_lenient(app_dir_field, app_dir, values)
        demoted_field = _record_first(app_dir_field, unresolved, demoted_field)

    new_artefacts: list[dict[str, Any]] = []
    for j, art in enumerate(sl.config_artefacts):
        base = f"slices[{i}].configArtefacts[{j}]"
        new_art, art_demoted = _substitute_artefact_lenient(base, art, values)
        new_artefacts.append(new_art)
        if art_demoted is not None:
            demoted_field = _record_first(art_demoted, True, demoted_field)

    new_env: dict[str, str] = {}
    for key, value in sorted(sl.env.items()):
        field = f"slices[{i}].env.{key}"
        sub, unresolved = _sub_field_lenient(field, value, values)
        new_env[key] = sub
        demoted_field = _record_first(field, unresolved, demoted_field)

    new_env_append: dict[str, list[str]] = {}
    for key, values_list in sorted(sl.env_append_path.items()):
        new_list: list[str] = []
        for k, value in enumerate(values_list):
            field = f"slices[{i}].envAppendPath.{key}[{k}]"
            sub, unresolved = _sub_field_lenient(field, value, values)
            new_list.append(sub)
            demoted_field = _record_first(field, unresolved, demoted_field)
        new_env_append[key] = new_list

    new_command = sl.command
    if new_command is not None:
        new_command, cmd_demoted = _substitute_command_lenient(
            f"slices[{i}].command", new_command, values
        )
        if cmd_demoted is not None:
            demoted_field = _record_first(cmd_demoted, True, demoted_field)

    # tan-cli#550: the post-build steps are spawned by the same executor as
    # `command`, so they are substituted by the same pass. Skipping them
    # would hand `cmake --build ${PROJECT_ROOT}/...` to `subprocess` with the
    # literal token still in it -- the executor has no second substitution
    # pass to catch it, and neither the LeftoverToken nor the
    # ${TOOLCHAIN_ROOT}-demotion guard would ever see the field.
    new_post: list[SliceCommand] = []
    for j, step in enumerate(sl.post_commands):
        new_step, step_demoted = _substitute_command_lenient(
            f"slices[{i}].postCommands[{j}]", step, values
        )
        new_post.append(new_step)
        if step_demoted is not None:
            demoted_field = _record_first(step_demoted, True, demoted_field)

    new_slice = replace(
        sl,
        build_dir=build_dir,
        app_dir=app_dir,
        config_artefacts=new_artefacts,
        env=new_env,
        env_append_path=new_env_append,
        command=new_command,
        post_commands=tuple(new_post),
    )
    return new_slice, demoted_field


def substitute_plan_tokens(
    plan: BuildPlan, values: TokenValues
) -> tuple[BuildPlan, list[DemotedSlice]]:
    """ONE blind string-substitution pass over every path-bearing string
    field of `plan`, swapping the four literal tokens for `values`. A no-op
    -- byte-identical, empty demotion list -- when `plan.plan_path_mode` is
    absent. `UnknownPlanPathMode` when it's present but not exactly
    `"tokened"`.

    After substitution, any leftover `${...}`-shaped token anywhere touched
    fails the whole pass loudly -- EXCEPT a leftover `${TOOLCHAIN_ROOT}`
    confined to a slice's own fields, which is reported via the returned
    `DemotedSlice` list and has its `configArtefacts` stripped (nothing will
    ever consume them this run), but the plan as a whole still succeeds. The
    same token surviving in `boardYaml`/`sharedArtefacts[]` (no owning
    slice) is still the hard `UnresolvedToolchainRoot` this pass always
    raised.

    Ordering matches the Rust oracle: `boardYaml` first (hard site), then
    every slice in order (each slice's own `configArtefacts` substituted
    -- and stripped on demotion -- WITH it), then `sharedArtefacts` last
    (hard site, cross-slice)."""
    if plan.plan_path_mode is None:
        # A fresh top-level object, matching Rust's `plan.clone()` -- returning
        # `plan` itself would let a downstream mutation of the "output" plan
        # silently alias back into the caller's input.
        return replace(plan), []
    if plan.plan_path_mode != PLAN_PATH_MODE_TOKENED:
        raise UnknownPlanPathMode(plan.plan_path_mode)

    # Hard site 1/2: no owning slice to demote to.
    board_yaml = _sub_field("boardYaml", plan.board_yaml, values)

    demoted: list[DemotedSlice] = []
    new_slices: list[Slice] = []
    for i, sl in enumerate(plan.slices):
        new_slice, demoted_field = _substitute_slice(i, sl, values)
        if demoted_field is not None:
            demoted.append(DemotedSlice(slice_index=i, core_id=sl.core_id, field=demoted_field))
            # Strip AFTER the slice's fields (including these artefacts' own
            # contents) have been fully scanned -- a ${UNKNOWN} inside a
            # demoted slice's configArtefact contents must still hard-fail
            # as LeftoverToken before it is ever cleared here.
            new_slice = replace(new_slice, config_artefacts=[])
        new_slices.append(new_slice)

    # Hard site 2/2: sharedArtefacts are cross-slice -- same "no owning
    # slice" reasoning as boardYaml above. Substituted AFTER all slices.
    new_shared = [
        _substitute_artefact(f"sharedArtefacts[{i}]", art, values)
        for i, art in enumerate(plan.shared_artefacts)
    ]

    return (
        replace(plan, board_yaml=board_yaml, slices=new_slices, shared_artefacts=new_shared),
        demoted,
    )
