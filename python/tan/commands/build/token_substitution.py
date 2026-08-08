# SPDX-License-Identifier: Apache-2.0
"""The IO/glue side of build-plan token substitution (alp-sdk #865): resolves
the SDK root, project root and planner python, drives the pure
`tan.core.plan_tokens` pass, and runs the one guard that pass cannot do
itself -- comparing the plan's `sdkCommit` against the resolved SDK
checkout's actual git HEAD (a subprocess call, so it belongs here, not in the
pure module).

`board_yaml_path`, `sdk_root`, `python` and `toolchain_root` are each
resolved exactly ONCE by the caller and handed in -- never re-resolved here
per-slice or on retry, the same "one resolution for the whole plan" contract
`tan_core::plan_tokens::TokenValues` documents."""
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tan.commands.sdk_cmd import NO_SDK_NEXT_STEPS
from tan.core.build_plan import BuildPlan
from tan.core.plan_tokens import (
    LeftoverToken,
    PLAN_PATH_MODE_TOKENED,
    TokenValues,
    UnknownPlanPathMode,
    UnresolvedToolchainRoot,
    project_root_diverges_from_exec_base,
    sdk_commit_mismatches,
    substitute_plan_tokens,
)


class TokenSubstitutionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SliceDemotion:
    """A slice demoted by `substitute_plan_tokens` (tan-cli #89): its fields
    still name a literal `${TOOLCHAIN_ROOT}` because this host has none
    resolved, but the demotion has an owning slice AND an owning dispatch
    seam (`executionPolicy.missingTool`) to route to, so it did not fail the
    whole plan. `reason` is the full sentence the executor surfaces verbatim
    at dispatch."""

    slice_index: int
    core_id: str
    reason: str


_NO_TOOLCHAIN_ADVICE = (
    "no toolchain install is detectable on this host -- install the Zephyr SDK "
    "(`west sdk install`) or set `ZEPHYR_SDK_INSTALL_DIR` to an existing install"
)


def _is_own_git_checkout(sdk_root: Path) -> bool:
    """Whether `sdk_root` is itself the TOP of a git checkout, not merely
    nested somewhere inside an unrelated ENCLOSING one (tan-cli#488 defect 4,
    mirroring `doctor_cmd._is_own_git_checkout` -- the SAME defect, the SAME
    fix, duplicated here rather than imported because this is the second of
    the two sites the issue names, not a shared library call).

    `git -C <root> ...` discovery walks UPWARD looking for a `.git`, so an SDK
    with no `.git` of its own -- an extracted release archive vendored under a
    customer's own application repository, a setup this port explicitly
    supports (`sdk_cmd.check_sdk_readiness`'s own docstring names exactly this
    shape) -- answers every git query with the ENCLOSING repo's state instead
    of "not a checkout". `git_short_head` below calls this first and returns
    no signal (`""`) rather than misattributing a foreign repository's HEAD as
    the SDK's own -- which, left unguarded, both stamps a build plan's
    `sdkCommit` with the wrong provenance and lets `sdk_commit_mismatches` fire
    a false split-brain refusal (or miss a real one) keyed off the enclosing
    app repo's commit instead of the SDK's.

    Compares the RESOLVED `--show-toplevel` against the RESOLVED `sdk_root`:
    lexical string comparison alone would false-negative on a symlinked or
    differently-cased path to the same directory.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(sdk_root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        return False
    top = out.stdout.strip()
    if not top:
        return False
    try:
        return Path(top).resolve() == Path(sdk_root).resolve()
    except OSError:
        return False


def git_short_head(sdk_root: Path) -> str:
    """`git rev-parse --short HEAD`. Empty string when git is missing, the
    checkout has no `.git` OF ITS OWN (see `_is_own_git_checkout` --
    tan-cli#488 defect 4), or the command otherwise fails -- all NO SIGNAL,
    never a hard failure: an SDK checkout with no `.git` (a release tarball,
    including one vendored inside a customer's own git repository) is a
    normal, supported setup."""
    if not _is_own_git_checkout(sdk_root):
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", str(sdk_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def apply_plan_token_substitution(
    plan: BuildPlan,
    *,
    board_yaml_path: str | None,
    exec_base: str,
    sdk_root: str | None,
    python: str,
    toolchain_root: str | None,
) -> tuple[BuildPlan, list[SliceDemotion]]:
    """Apply the build-plan token-substitution pass to `plan` before
    materialise writes anything or a slice command runs. A no-op unless
    `plan.plan_path_mode` is present; a present-but-unrecognised mode is a
    hard error, never a half-applied guess."""
    if plan.plan_path_mode is None:
        return plan, []
    if plan.plan_path_mode != PLAN_PATH_MODE_TOKENED:
        raise TokenSubstitutionError(
            "build.plan-invalid",
            f'unknown planPathMode `{plan.plan_path_mode}` (only "tokened" is defined) -- '
            f"refusing rather than silently treating it as legacy or applying token "
            f"substitution",
        )

    # Guard: PROJECT_ROOT (board.yaml's directory) vs the executor's actual
    # base dir. They coincide only in the default config -- a tokened plan
    # substituting ${PROJECT_ROOT} from one while slices actually run under
    # the other would silently build against the wrong tree.
    if board_yaml_path is None:
        raise TokenSubstitutionError(
            "build.plan-invalid",
            "a `planPathMode: tokened` plan needs a resolved board.yaml to derive "
            "${PROJECT_ROOT} from -- pass `--board-yaml <PATH>` or run from a project.",
        )
    project_root = str(Path(board_yaml_path).parent).replace("\\", "/")

    if project_root_diverges_from_exec_base(project_root, exec_base):
        raise TokenSubstitutionError(
            "build.project-root-mismatch",
            f"plan is `planPathMode: tokened`, but ${{PROJECT_ROOT}} (`{project_root}`, the "
            f"board.yaml directory) differs from where tan actually runs each slice's "
            f"command (`{exec_base}`) -- refusing to build against the wrong tree. This only "
            f"happens with a non-default `west_cwd`/`--project` split; align them, or drop "
            f"`planPathMode` from the plan.",
        )

    # ONE resolved sdk_root for the whole substitution. A tokened plan NEEDS
    # a real ${SDK_ROOT} value: degrading an unresolved sdk_root to "" would
    # substitute ${SDK_ROOT}/scripts into the bare relative path /scripts,
    # sailing straight past the leftover-token guard -- refuse instead.
    if sdk_root is None:
        # `tan sdk switch` refuses in this build (tan-cli#305) -- kept the
        # two mechanisms `resolve_sdk_root_ladder` (this value's caller)
        # actually walks (`--sdk-root`, the lateral discovery tail) and
        # swapped the third for NO_SDK_NEXT_STEPS's honest "how to get a
        # checkout at all".
        raise TokenSubstitutionError(
            "build.sdk-root-unresolved",
            "plan is `planPathMode: tokened` (needs ${SDK_ROOT} substituted with a real "
            "path), but no alp-sdk checkout resolved -- pass `--sdk-root <PATH>`, run from "
            f"a project near an alp-sdk checkout, or {NO_SDK_NEXT_STEPS}.",
        )

    # The two-SDK split-brain guard: whenever the plan carries a sdkCommit,
    # a mismatch against the resolved SDK checkout's actual HEAD means tan
    # is about to build against a DIFFERENT SDK checkout/SHA than the plan
    # was emitted from.
    if plan.sdk_commit:
        resolved = git_short_head(Path(sdk_root))
        if sdk_commit_mismatches(plan.sdk_commit, resolved):
            raise TokenSubstitutionError(
                "build.sdk-commit-mismatch",
                f"plan was emitted from alp-sdk commit `{plan.sdk_commit}`, but the resolved "
                f"SDK checkout is at `{resolved}` -- building against a different SDK "
                f"checkout than the plan was captured from can silently produce the wrong "
                f"image. Re-emit the plan from the current SDK checkout, or point "
                f"`--sdk-root` at the checkout the plan names.",
            )

    values = TokenValues(
        sdk_root=sdk_root, project_root=project_root, python=python, toolchain_root=toolchain_root
    )
    try:
        out, demoted = substitute_plan_tokens(plan, values)
    except LeftoverToken as e:
        raise TokenSubstitutionError(
            "build.plan-token-unresolved",
            f"plan is `planPathMode: tokened` but field `{e.field}` still names the literal "
            f"token `{e.token}` after substitution -- an SDK-side token this CLI does not "
            f"resolve (only ${{SDK_ROOT}}, ${{PROJECT_ROOT}}, ${{PYTHON}}, ${{TOOLCHAIN_ROOT}} "
            f"are known). Upgrade tan, or check the plan for a bug.",
        ) from e
    except UnresolvedToolchainRoot as e:
        raise TokenSubstitutionError(
            "build.toolchain-root-unresolved",
            f"plan field `{e.field}` names ${{TOOLCHAIN_ROOT}}, but {_NO_TOOLCHAIN_ADVICE}. "
            f"tan refuses rather than substituting an empty path, which would silently build "
            f"against the host root.",
        ) from e
    except UnknownPlanPathMode as e:
        raise TokenSubstitutionError("build.plan-invalid", str(e)) from e

    slice_demotions = [
        SliceDemotion(
            slice_index=d.slice_index,
            core_id=d.core_id,
            reason=f"plan field `{d.field}` names ${{TOOLCHAIN_ROOT}}, but {_NO_TOOLCHAIN_ADVICE}",
        )
        for d in demoted
    ]
    return out, slice_demotions
