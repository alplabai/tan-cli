# SPDX-License-Identifier: Apache-2.0
"""`tan build` -- the one command that turns a build plan into firmware.

Composition, not logic: this module resolves the project, ACQUIRES a plan
(from `--plan-from`, else by invoking the SDK planner), then hands it in turn
to the four sub-project-1 libraries -- `parse_build_plan`,
`apply_plan_token_substitution`, `materialise_plan`, `execute_slices` -- and
folds the result into one envelope. Every decision those modules own stays
theirs; what lives here is the ORDER they run in, the mapping from their
`(code, message)` failures to an exit code, and the guarantee that not one of
them can escape as a traceback.

Three properties this file exists to hold:

**Order is the contract (I-20).** `materialise_plan` writes every
`sharedArtefacts` entry AND every slice's `configArtefacts` before
`execute_slices` is called at all. The plan carries no `sequential` flag and
no `inputHash`; the only thing making its slices independent is that all their
inputs are on disk before the first one starts. `tests/commands/
test_build_command.py` proves it by making the first slice's own command
assert the OTHER slice's config artefact already exists.

**Nothing but the envelope on stdout.** Slice output streams to stderr (see
`_stream`), the text-mode recap goes to stderr, and the JSON envelope is the
only thing ever written to stdout. The extension parses stdout whole; one
stray byte and it renders nothing, with no error on either side.

**The OS is not an option (I-01/I-02).** There is deliberately no `--os` and
no `--backend` flag. A core's runtime is derived from its Cortex class by the
planner (`scripts/alp_orchestrate/topology.py`), and `slices[].backend`
arrives already resolved. Nor does this command filter the plan's slices: a
one-core `board.yaml` legitimately plans three (I-04), and "helpfully"
dropping the ones the customer did not name is how a Yocto slice silently
stops being built.
"""
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import typer

from tan.commands.build.execute import KNOWN_BACKENDS, SliceOutcome, execute_slices
from tan.commands.build.materialise import MaterialiseError, materialise_plan
from tan.commands.build.token_substitution import (
    TokenSubstitutionError,
    apply_plan_token_substitution,
)
from tan.core.build_plan import BuildPlan, PlanParseError, parse_build_plan
from tan.core.plan_exec import PolicyAction, resolve_action
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode

#: `scripts/alp_project.py` is THE marker for an alp-sdk checkout -- the same
#: literal `tan_core::project` hardcodes (I-31). Renaming or relocating it
#: breaks every `--sdk-root` resolution in the CLI, so it is spelled once.
SDK_MARKER = ("scripts", "alp_project.py")

#: Envelope `data.slices[].status`, from `SliceOutcome.status`. The wire
#: vocabulary is the Rust one (`ok` / `skipped` / `failed`), NOT the executor's
#: internal spelling -- `succeeded` on the wire would break a consumer written
#: against the shipped binary. `cancelled` is unreachable from this command
#: (nothing here supplies a cancellation source) but maps to `failed` rather
#: than falling through to a KeyError if one is ever wired up.
_WIRE_STATUS = {"succeeded": "ok", "skipped": "skipped"}


class BuildError(Exception):
    """A build failure with its issue code and exit code already decided --
    the shape every step in `_build` reports through, so the caller never has
    to guess a code from an exception type."""

    def __init__(self, code: str, message: str, exit_code: ExitCode) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _stream(line: str) -> None:
    """Slice output goes to stderr -- ALWAYS, both formats. stdout is the
    envelope channel and carries nothing else; stderr carries no contract at
    all, so there is nothing to keep separate between the two modes."""
    print(line, file=sys.stderr, flush=True)


def _is_sdk_root(path: Path) -> bool:
    return path.joinpath(*SDK_MARKER).is_file()


def _discover_sdk_root(workspace_root: Path) -> Path | None:
    """Find an alp-sdk checkout near `workspace_root`, mirroring Rust's
    `discover_sdk_root` (`crates/tan-cli/src/util.rs`) candidate for
    candidate: the root itself, then its CHILD `alp-sdk/`, then the sibling
    `../alp-sdk`, then `../alp-sdk-upstream`, and only if none of those hit,
    the nearest ENCLOSING checkout.

    The child comes before the siblings deliberately (tan-cli #218): `tan
    bootstrap` clones into `<ws>/alp-sdk`, so at that moment the checkout is a
    CHILD of the cwd -- it only becomes the documented sibling once the user
    has cd'd into a project. Discovery that checks root, siblings and
    ancestors but not a child reports "alp-sdk root is unresolved" with the
    checkout sitting right there.

    A fixed candidate list, never a directory scan: probing `iterdir()` up the
    whole ancestor chain reads a developer's entire home and drive root, which
    is both slow and non-hermetic (it lets an unrelated checkout elsewhere on
    the machine decide what a test resolves).

    Deliberately NOT the full Rust ladder -- `tan sdk switch`'s workspace pin
    and machine-global default (`~/.alp/sdk-default`) are tiers this port has
    no `sdk` command to write yet, and half a precedence chain that silently
    picks the wrong checkout is worse than an honest `--sdk-root`.
    """
    parent = workspace_root.parent
    for candidate in (
        workspace_root,
        workspace_root / "alp-sdk",
        parent / "alp-sdk",
        parent / "alp-sdk-upstream",
    ):
        if _is_sdk_root(candidate):
            return candidate
    for ancestor in workspace_root.parents:
        if _is_sdk_root(ancestor):
            return ancestor
    return None


def _planner_python() -> str:
    """The interpreter the SDK planner runs under.

    A PATH name, mirroring `tan_core::project::resolve_python_binary`
    (`python3` off Windows, `python` on it) -- NOT `sys.executable`. Two
    reasons, and either alone is decisive: frozen by PyInstaller,
    `sys.executable` is `tan` itself, so `-m alp_orchestrate` would just
    re-enter this CLI; and this value is also the `${PYTHON}` substituted into
    the plan, which the planner bakes into every Zephyr slice as
    `-DPython3_EXECUTABLE` (alp-sdk#787) -- it has to name an interpreter the
    slice can find, not this process.

    NOT YET PORTED: Rust prefers the west-capable workspace venv's python
    (`venv_python`) and falls back to this only when no venv resolves. Without
    that, a host whose PATH `python` lacks the `west` module gets the planner's
    own ImportError surfaced through `build.plan-unavailable` rather than a
    working build.
    """
    return "python" if os.name == "nt" else "python3"


def _emit_plan(sdk_root: str | None, board_yaml: str | None) -> str:
    """Ask the SDK for the plan: `<python> -m alp_orchestrate --input
    <board.yaml> --emit build-plan`, with `<sdk>/scripts` prepended to
    PYTHONPATH.

    Module invocation (`-m alp_orchestrate`), not a script path: that resolves
    both the package layout (`scripts/alp_orchestrate/`) and the legacy flat
    `scripts/alp_orchestrate.py`, so the CLI works against any SDK release.
    And the planner's own flag is `--input`; `--board-yaml` is tan's spelling
    of the same fact and is not accepted there.
    """
    if sdk_root is None:
        raise BuildError(
            "build.plan-unavailable",
            "no alp-sdk checkout found -- pass `--sdk-root <PATH>` or run from a project "
            "beside one. The build-plan comes from the SDK's `alp_orchestrate --emit "
            "build-plan`.",
            ExitCode.RUNTIME_FAILURE,
        )
    if board_yaml is None:
        raise BuildError(
            "build.plan-unavailable",
            "no board.yaml found -- pass `--board-yaml <PATH>` or run from a project.",
            ExitCode.RUNTIME_FAILURE,
        )
    scripts = Path(sdk_root) / "scripts"
    if not (
        (scripts / "alp_orchestrate.py").is_file()
        or (scripts / "alp_orchestrate" / "__init__.py").is_file()
    ):
        raise BuildError(
            "build.plan-unavailable",
            f"the SDK at `{sdk_root}` has no `alp_orchestrate` planner under scripts/ -- "
            f"pin to an SDK release that ships `--emit build-plan`.",
            ExitCode.RUNTIME_FAILURE,
        )

    inherited = os.environ.get("PYTHONPATH")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(scripts), *([inherited] if inherited else [])]),
    }
    python = _planner_python()
    argv = [python, "-m", "alp_orchestrate", "--input", board_yaml, "--emit", "build-plan"]
    try:
        out = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )
    except OSError as err:
        # No interpreter of that name on PATH, or it is not executable. The
        # single most likely first-run failure, and it must not be a traceback.
        raise BuildError(
            "build.plan-unavailable",
            f"failed to run `{python} -m alp_orchestrate --emit build-plan`: {err}",
            ExitCode.RUNTIME_FAILURE,
        ) from err
    if out.returncode != 0:
        stderr = out.stderr.strip()
        raise BuildError(
            "build.plan-unavailable",
            f"the SDK build-plan emit failed (rc {out.returncode})"
            + (f": {stderr}" if stderr else ""),
            ExitCode.RUNTIME_FAILURE,
        )
    return out.stdout


def _acquire_plan(
    plan_from: str | None, sdk_root: str | None, board_yaml: str | None
) -> BuildPlan:
    if plan_from is not None:
        try:
            text = Path(plan_from).read_text(encoding="utf-8")
        except OSError as err:
            raise BuildError(
                "build.plan-unavailable",
                f"failed to read plan file `{plan_from}`: {err}",
                ExitCode.RUNTIME_FAILURE,
            ) from err
    else:
        text = _emit_plan(sdk_root, board_yaml)

    try:
        return parse_build_plan(text)
    except PlanParseError as err:
        # Exit 1, NOT 2. A malformed plan reads like a validation failure and
        # the semantic pull toward `ValidationFailure` is real -- but the
        # shipped binary reports RuntimeFailure here
        # (`crates/tan-cli/src/commands/build/plan_modes.rs:140-146` and
        # `native.rs:110-116` both call `plan_error_run(..., "build.plan-
        # invalid", ..., ExitCode::RuntimeFailure)`), and the exit ladder is a
        # frozen contract. The consumer makes it worse than cosmetic:
        # `alp-sdk-vscode/src/alpCli/service.ts:253-259` renders exit 2 as
        # `severity: "warning"` and exit 1 as `"error"`, so "2" would downgrade
        # a plan that will not parse -- a hard failure -- to a yellow banner.
        # 2 stays for a genuine validation failure in Rust's own terms, e.g.
        # `validate`'s schema violation, whose fixture pins exit 2.
        raise BuildError(err.code, err.message, ExitCode.RUNTIME_FAILURE) from err


def _slice_result(core_id: str, backend: str, outcome: SliceOutcome) -> dict:
    """One `data.slices[]` entry. `rc`/`reason` are OMITTED when absent, not
    null -- Rust's `skip_serializing_if = "Option::is_none"`."""
    result = {
        "coreId": core_id,
        "backend": backend,
        "status": _WIRE_STATUS.get(outcome.status, "failed"),
    }
    if outcome.exit_code is not None:
        result["rc"] = outcome.exit_code
    if outcome.message is not None:
        result["reason"] = outcome.message
    return result


def _dispatch(
    plan: BuildPlan, demotions, build_root: Path
) -> tuple[list[SliceOutcome], list[Issue]]:
    """Run the plan's slices, holding back the ones token substitution demoted.

    A demoted slice still names a literal `${TOOLCHAIN_ROOT}` in its own
    fields because this host resolved no toolchain. `execute_slices` does not
    know about demotions, so dispatching one would spawn a command with an
    unsubstituted token in its argv -- a silent wrong-path build, the exact
    failure I-29 says a naive substitution reintroduces. Instead the demotion
    is routed to `executionPolicy.missingTool` (default skip) here, which is
    the seam the Rust executor uses for it too: it is a host-provisioning
    fact, not a plan bug.

    Precedence is preserved: a demoted slice that ALSO names an unknown
    backend or carries no command is left to `execute_slices`, because both of
    those outrank a provisioning fact and are checked first there.
    """
    held = {
        d.slice_index: d
        for d in demotions
        if plan.slices[d.slice_index].backend in KNOWN_BACKENDS
        and plan.slices[d.slice_index].command is not None
    }
    action = resolve_action(plan.execution_policy, "missing_tool", PolicyAction.SKIP)
    failed = action is PolicyAction.FAIL

    runnable = [sl for i, sl in enumerate(plan.slices) if i not in held]
    dispatched = iter(
        execute_slices(
            replace(plan, slices=runnable),
            build_root=build_root,
            env_lookup=os.environ.get,
            # NOT YET PORTED: Rust fills ZEPHYR_BASE and EXTRA_ZEPHYR_MODULES
            # here from the resolved west workspace, so `west build -b
            # <alp-board>` finds the SDK's boards without the user wiring
            # -DEXTRA_ZEPHYR_MODULES. Plans emitted by the SDK carry both on
            # the slice's own envAppendPath, so this is a gap only for a host
            # relying on the CLI to fill them.
            gap_fillers=(),
            on_output=_stream,
        )
    )

    outcomes: list[SliceOutcome] = []
    issues: list[Issue] = []
    for i, sl in enumerate(plan.slices):
        demotion = held.get(i)
        if demotion is None:
            outcomes.append(next(dispatched))
            continue
        outcomes.append(
            SliceOutcome(sl.core_id, "failed" if failed else "skipped", None, demotion.reason)
        )
        issues.append(
            Issue(
                # Same code as the plan-fatal sibling on purpose: the
                # extension needs no new vocabulary, only a severity that says
                # whether this stopped the build.
                "build.toolchain-root-unresolved",
                "error" if failed else "warning",
                f"slice `{sl.core_id}` {'failed' if failed else 'skipped'}: {demotion.reason}",
            )
        )
    return outcomes, issues


def _backend_issues(plan: BuildPlan, outcomes: list[SliceOutcome]) -> list[Issue]:
    """Name the backend string the plan sent that this CLI does not know.
    Recomputed from the slice rather than sniffed out of the outcome message:
    `executionPolicy.unknownBackend` already decided skip-vs-fail inside
    `execute_slices`, and the outcome's status is the honest read of it."""
    issues = []
    for sl, outcome in zip(plan.slices, outcomes, strict=True):
        if sl.backend in KNOWN_BACKENDS:
            continue
        failed = outcome.status == "failed"
        issues.append(
            Issue(
                "build.unknown-backend",
                "error" if failed else "warning",
                f"slice `{sl.core_id}` names unrecognised backend `{sl.backend}`"
                + ("" if failed else " -- skipped"),
            )
        )
    return issues


def _build(
    *, plan_from: str | None, build_root: str, sdk_root: str | None, board_yaml: str | None
) -> tuple[ExitCode, dict, list[Issue]]:
    plan = _acquire_plan(plan_from, sdk_root, board_yaml)

    # Substitution runs on the in-memory plan BEFORE materialise writes
    # anything and before any command is assembled, so an unresolvable token
    # can never reach disk or an argv. A no-op on an untokened plan (every
    # plan the SDK emits today).
    try:
        plan, demotions = apply_plan_token_substitution(
            plan,
            board_yaml_path=board_yaml,
            exec_base=build_root,
            sdk_root=sdk_root,
            python=_planner_python(),
            # NOT YET PORTED: `crate::toolchain::resolve_toolchain_root`. Left
            # unresolved rather than guessed -- resolution is lazy, so a plan
            # that never names ${TOOLCHAIN_ROOT} (every SDK plan today) is
            # unaffected, and one that does is demoted per its own
            # executionPolicy instead of built against the host root.
            toolchain_root=None,
        )
    except TokenSubstitutionError as err:
        # RuntimeFailure for every code this pass raises, `build.plan-invalid`
        # (an unknown `planPathMode`) included -- same ladder position the Rust
        # oracle gives them (`native.rs:132-142`), and the same code must not
        # mean two different exits depending on which module raised it.
        raise BuildError(err.code, err.message, ExitCode.RUNTIME_FAILURE) from err

    # I-20. ALL of them, then dispatch -- never interleaved.
    try:
        materialise_plan(plan, Path(build_root))
    except MaterialiseError as err:
        raise BuildError(
            "build.materialise-failed", err.message, ExitCode.WRITE_FAILURE
        ) from err

    outcomes, issues = _dispatch(plan, demotions, Path(build_root))

    any_failed = any(o.status not in ("succeeded", "skipped") for o in outcomes)
    exit_code = ExitCode.RUNTIME_FAILURE if any_failed else ExitCode.SUCCESS
    if any_failed:
        issues.insert(
            0, Issue("build.slice-failed", "error", "one or more slices failed to build")
        )
    issues.extend(_backend_issues(plan, outcomes))

    data = {
        "schemaVersion": "1",
        "baseDir": build_root,
        "slices": [
            _slice_result(sl.core_id, sl.backend, outcome)
            for sl, outcome in zip(plan.slices, outcomes, strict=True)
        ],
        # The plan's own warnings, verbatim. A `command: null` slice is a
        # legitimate skip whose REASON lives only here (I-11) -- dropping the
        # list leaves the user a slice that silently did not build. Passed
        # through untyped on purpose: the schema says new warning codes may
        # appear without a schemaVersion bump, so a consumer must not treat
        # them as a closed set, and neither may this.
        "warnings": plan.warnings,
    }
    return exit_code, data, issues


def build(
    plan_from: str = typer.Option(
        None,
        "--plan-from",
        metavar="FILE",
        help="Read the build plan from a JSON file instead of invoking the SDK planner.",
    ),
    build_root: str = typer.Option(
        None,
        "--build-root",
        metavar="DIR",
        help="Project tree the slices run under and artefacts are written below "
        "(default: the board.yaml's directory, else the current directory).",
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    output_format: str = typer.Option(
        "text", "--format", metavar="FORMAT", help="Output format: text or json."
    ),
) -> None:
    """Build every slice of the project's build plan."""
    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = output_format == "json"

    # Resolution, in one place, before anything can fail. `--board-yaml` and
    # `--build-root` are kept in their as-passed form (never resolved to
    # absolute): the divergence guard in `apply_plan_token_substitution`
    # compares them lexically, exactly as the Rust oracle does, and resolving
    # only one side is how that guard starts firing on a project it should not.
    if board_yaml is None and (Path.cwd() / "board.yaml").is_file():
        board_yaml = "./board.yaml"
    if build_root is None:
        build_root = str(Path(board_yaml).parent) if board_yaml else "."

    explicit_sdk = sdk_root is not None
    if sdk_root is None:
        found = _discover_sdk_root(Path.cwd())
        sdk_root = str(found) if found else None
    sdk = (
        SdkInfo(sdk_root, "sdkRootFlag" if explicit_sdk else "discovery")
        if sdk_root is not None
        else None
    )
    project = Project(root=build_root, board_yaml=board_yaml)

    try:
        exit_code, data, issues = _build(
            plan_from=plan_from,
            build_root=build_root,
            sdk_root=sdk_root,
            board_yaml=board_yaml,
        )
    except BuildError as err:
        exit_code, data, issues = err.exit_code, None, [Issue(err.code, "error", err.message)]
    except Exception as err:  # noqa: BLE001 -- see below
        # The port's most-repeated defect class: an uncaught exception escapes
        # as a raw traceback, stdout stays empty, and the extension renders
        # nothing at all with no error on either side. Anything that reaches
        # here is a tan bug, so it is reported as one -- with an envelope.
        exit_code = ExitCode.INTERNAL_FAILURE
        data = None
        issues = [Issue("build.internal-failure", "error", f"{type(err).__name__}: {err}")]

    if json_mode:
        emit(Envelope("build", project, data, issues, exit_code, sdk=sdk))
    else:
        for issue in issues:
            print(f"{issue.severity}: {issue.message}", file=sys.stderr)
        for result in (data or {}).get("slices", []):
            reason = f" -- {result['reason']}" if "reason" in result else ""
            print(
                f"{result['status']}: {result['coreId']} [{result['backend']}]{reason}",
                file=sys.stderr,
            )
    raise typer.Exit(int(exit_code))
