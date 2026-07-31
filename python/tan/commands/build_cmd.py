# SPDX-License-Identifier: Apache-2.0
"""`tan build` -- the one command that turns a build plan into firmware.

Composition, not logic: this module resolves the project, ACQUIRES a plan (from
`--plan-from`, else by running the planner in-process -- there is no other way any
more; see `_emit_plan`), then hands it in turn to the four sub-project-1
libraries -- `parse_build_plan`, `apply_plan_token_substitution`,
`materialise_plan`, `execute_slices` -- and
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

**`--plan-from` is a plan SOURCE, not a build trigger (v0.4.1 §gate).** The
oracle's `--plan-from` help says "Implies `--plan`", and measured against the
shipped `tan 0.4.1-dev` binary in fresh temp dirs, on
`tests/parity/oracle/multicore_rpmsg-aen.build-plan.json`, it means it -- even
against an EXPLICIT `--native`::

    build --plan-from p.json                -> rc 0, 0 files, data = the plan
    build --plan-from p.json --native       -> rc 0, 0 files, data = the plan
    build --plan-from p.json --materialise  -> rc 0, 6 files,
                                               data = {schemaVersion, baseDir, written}

All three are reproduced here exactly (`_MODE_PLAN` / `_MODE_MATERIALISE`),
`--native` included. Two earlier revisions of this file each redefined one of
those argvs -- a bare `--plan-from` used to materialise AND dispatch, and then
`--plan-from --native` did -- so a v0.4.1 script that only ever INSPECTED a
plan silently began writing six files into its own tree. Changing what a
v0.4.1 argv MEANS is the whole surprise this gate exists to remove.

**`--execute` is a deliberate PORT ADDITION the oracle has no flag for.**
Taking a pinned, reviewed plan file and running it reproducibly is a normal
hermetic-CI request; v0.4.1's inability to do it is a LIMITATION of that CLI,
not a contract worth preserving -- there `--plan-from`'s implied `--plan`
outranks `--native`, so a file-supplied plan cannot be dispatched at all.
`--execute` supplies exactly that capability without changing what any v0.4.1
argv means. It is not a parity bug and not a test hook: a future reader who
"fixes" it by deleting it removes a supported capability. Its `data` is the
ORDINARY build shape -- `{schemaVersion, baseDir, slices[], warnings[]}` --
because it IS a native build and only the plan's SOURCE differs; `written` is
deliberately omitted, being byte-for-byte the pinned plan's own declared
artefact paths, which the one caller who has that file already holds. A
conflicting combination is refused with its own code, never resolved by silent
precedence (`--execute --materialise` -> `build.conflicting-flags`, exit 2;
`--execute` with a deferred flag -> `cli.command-deferred`, exit 1, because a
flag this build does not implement at all cannot be honoured either way).

**The OS is not an option (I-01/I-02).** There is deliberately no `--os` and
no `--backend` flag. A core's runtime is derived from its Cortex class by the
planner (`tan/planner/topology.py`), and `slices[].backend`
arrives already resolved. Nor does this command filter the plan's slices: a
one-core `board.yaml` legitimately plans three (I-04), and "helpfully"
dropping the ones the customer did not name is how a Yocto slice silently
stops being built.
"""
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import typer

from tan.commands.build.execute import (
    KNOWN_BACKENDS,
    SliceOutcome,
    execute_slices,
    last_manifest_write_failure,
    last_sdk_switch_issues,
    reset_last_manifest_write,
)
from tan.commands.build.materialise import MaterialiseError, materialise_plan
from tan.commands.build.token_substitution import (
    TokenSubstitutionError,
    apply_plan_token_substitution,
)
from tan.commands.deferred_cmd import DEFERRED_ISSUE_CODE, DEFERRED_ISSUE_URL
from tan.commands.sdk_cmd import resolve_sdk_tiered
from tan.core.bootstrap import VenvLayout, venv_layout
from tan.core.build_plan import BuildPlan, PlanParseError, parse_build_plan
from tan.core.plan_exec import PolicyAction, normalize_path, resolve_action
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

#: What this run does with the plan it acquires -- the oracle's own precedence,
#: measured flag-combination by flag-combination against `target/debug/tan.exe`
#: on the AEN fixture: `--materialise` outranks everything (`--plan-from
#: --materialise --native` -> six files, `written` shape), the `--plan` implied
#: by `--plan-from` outranks even an explicit `--native` (`--plan-from
#: --native` -> the plan, zero files), and only a run naming NEITHER plan-mode
#: flag builds. `_MODE_NATIVE` is therefore both the default and what the
#: port-added `--execute` selects -- deliberately the SAME mode, so `--execute`
#: reports the same `data` shape a plain `tan build` does rather than a fourth
#: one no consumer is written for.
_MODE_PLAN = "plan"
_MODE_MATERIALISE = "materialise"
_MODE_NATIVE = "native"

#: Flags the oracle's `tan build` declares and this port does not implement,
#: in the order a run naming several of them is refused. Declaring them is the
#: whole point: an undeclared flag is a Click `UsageError` -- exit 2,
#: `cli.parse-error`, and a message indistinguishable from `tan build
#: --pristien`. Registering them turns "known, deferred" into its own coded
#: answer at exit 1, exactly as `deferred_cmd` does for the seven deferred
#: VERBS, whose `cli.command-deferred` code and tan-cli#260 URL are reused
#: rather than forked (a caller special-casing deferral needs one code to
#: match, not two).
#:
#: `--verbose`/`--quiet`/`--no-color`/`--non-interactive`/`--ci` are declared
#: HERE, build-local, only because this port accepts none of them at the root
#: today (measured: `tan --verbose build ...` -> exit 2, "No such option");
#: in the oracle all five are clap `global = true` args declared once on the
#: root and propagated. The moment `cli.py` grows a real root-level
#: implementation these five must be DELETED from here, or a build-local
#: declaration would shadow it.
_DEFERRED_FLAGS = (
    "--plan",
    "--target",
    "--all",
    "--manifest",
    "--manifest-from",
    "--no-auto-bootstrap",
    "--pristine",
    "--verbose",
    "--quiet",
    "--no-color",
    "--non-interactive",
    "--ci",
)

#: `--help` text for every one of them -- one string, because they all report
#: the same fact and a per-flag reason would be twelve things to keep true.
_DEFERRED_HELP = "Deferred to v0.6.0, not implemented in this build (tan-cli#260)."


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


def _abs_posix(path: str) -> str:
    """Rust's `normalize_path(cwd.join(p))` + `to_posix`: cwd-anchored,
    lexically normalised, forward slashes.

    `os.path.abspath`, NOT `Path.resolve()` -- abspath is purely lexical, so a
    project reached through a symlink keeps the name the user typed, and a path
    that does not exist yet still resolves. `resolve()` would rewrite both, and
    the divergence guard compares STRINGS: rewriting one side's symlink but not
    the other's is exactly how that guard starts firing on a healthy project.

    Forward slashes because the result is substituted into `${PROJECT_ROOT}`
    and lands in a CMake `-D` argument, where a Windows backslash is an escape.
    """
    return os.path.abspath(path).replace("\\", "/")


def _is_sdk_root(path: Path) -> bool:
    return path.joinpath(*SDK_MARKER).is_file()


def discover_sdk_root(workspace_root: Path) -> Path | None:
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

    Deliberately just the positional walk, not the full ladder -- the project
    pin and machine-global default tiers that outrank this one live in
    [`resolve_sdk_root_ladder`], below, which every `--sdk-root`-less caller
    should use instead of calling this directly (this function remains that
    ladder's own final fallback tier, and the one place that still calls it
    alone is `tan-cli#218`'s own regression test).
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


def resolve_sdk_root_ladder(
    sdk_root_arg: str | None, workspace_root: Path
) -> tuple[Path | None, str]:
    """`(path, sourceTier)` -- the ONE discovery ladder every command that
    actually reads an SDK checkout shares (`build`, `doctor`, `run`, `flash`,
    `examples`, `generate`, `renode`, `size`, `clean`, `init`'s pin choice):
    `--sdk-root` (terminal, I-31) > the project's own pin (`.alp/sdk-path`,
    written by `tan init` / relocated by `tan bootstrap`) > the machine-global
    default (`~/.alp/sdk-default`) > the wide positional walk
    (`discover_sdk_root`, above) -- exactly the Rust oracle's closed
    `SdkSourceTier` (`crates/tan-core/src/sdk.rs`): `SdkRootFlag`,
    `ProjectPin`, `GlobalDefault`, `Discovery`, `None`. No sixth tier.

    This is the fix for the worst reported CX defect in this port: `tan init`
    writes `.alp/sdk-path` into the project it just scaffolded, but every
    command below used to jump straight from `--sdk-root` to the positional
    walk, skipping `resolve_sdk_tiered` (`sdk_cmd.py`) entirely -- so that
    pointer was silently ignored the moment `tan build` ran in that SAME
    directory, unless the checkout also happened to sit beside the project by
    coincidence. `resolve_sdk_tiered` already IS the oracle's `--sdk-root` >
    project pin > global default > discovery chain; the only thing missing
    from it here is the WIDER positional walk as one more fallback below
    `discovery`, for callers ported before `resolve_sdk_tiered` existed.

    An `ALP_SDK_ROOT` env-var tier was tried here and reverted: the oracle's
    `util.rs::resolve_sdk_root` only ever WRITES that variable into a build
    slice's env, never reads it back for discovery, and `SdkSourceTier` has no
    slot for it -- a sixth `sourceTier` value in the envelope is a wire
    contract change no consumer (the vscode extension, `tan sdk current
    --json`) expects. The project-pin tier above already makes `tan init &&
    tan build` compose without it.

    **The narrow tier short-circuiting the wide one is deliberate, and
    measured** (tan-cli#263, which proposed inverting it): the wide walk puts
    the CHILD `<ws>/alp-sdk` ahead of the lateral `../alp-sdk`, so hoisting it
    above `resolve_sdk_tiered` would flip every workspace holding both. Driven
    through the oracle binary in constructed layouts, thirteen of its commands
    -- `build`, `doctor`, `clean`, `run`, `flash`, `size`, `image`, `kconfig`,
    `validate`, `presets`, `inspect`, `trace`, `sdk current` -- resolve the
    LATERAL one there, i.e. the narrow order this ladder already has; only
    `init`, `generate`, `examples` and `renode` take the child. The oracle
    carries two resolutions, not one, and this ladder mirrors the majority
    (narrow) one plus the wide walk as its tail. Inverting it to match the
    other four would move the SDK root under thirteen commands, and a moved
    root is what `plan_exec.sdk_stamp_action` reads as a switch: every existing
    such workspace would take the `build.sdk-switch-pristine` branch on its
    next build and lose every slice's build dir. The four wide commands are the
    real gap and want a separate wide helper, not a change here.
    """
    flag = (sdk_root_arg or "").strip()
    if flag:
        return Path(flag), "sdkRootFlag"

    tiered = resolve_sdk_tiered(None, workspace_root)
    if tiered.path is not None:
        return Path(tiered.path), tiered.tier

    found = discover_sdk_root(workspace_root)
    if found is not None:
        return found, "discovery"
    return None, "none"


def _has_west(venv: Path, layout: VenvLayout) -> bool:
    return (venv / layout.bin_dir / layout.west).is_file()


def _find_workspace_venv(start: str, sdk_root: str | None) -> Path | None:
    """Locate the west-capable workspace `.venv`, mirroring Rust's
    `find_workspace_venv` (`crates/tan-cli/src/venv.rs`): gated on `west`
    actually being present under the candidate (not just the directory
    existing), in this order:

      1. a `.venv` in the project tree, searched from `start` upward;
      2. the workspace venv derived from `$ZEPHYR_BASE`
         (`<ZEPHYR_BASE>/../.venv`);
      3. the SDK's canonical `<sdk-parent>/.venv` (post-alp-sdk#782) or the
         legacy `<sdk-parent>/zephyrproject/.venv`.

    `None` when none resolve (CI, an activated venv, the contract harness).
    """
    layout = venv_layout(os.name == "nt")

    directory: Path | None = Path(start)
    while directory is not None:
        candidate = directory / ".venv"
        if _has_west(candidate, layout):
            return candidate
        parent = directory.parent
        directory = parent if parent != directory else None

    zephyr_base = os.environ.get("ZEPHYR_BASE")
    if zephyr_base:
        candidate = Path(zephyr_base).parent / ".venv"
        if _has_west(candidate, layout):
            return candidate

    if sdk_root is not None:
        parent = Path(sdk_root).parent
        for workspace in (parent, parent / "zephyrproject"):
            candidate = workspace / ".venv"
            if _has_west(candidate, layout):
                return candidate

    return None


def _venv_python(start: str, sdk_root: str | None) -> str | None:
    """The west-capable workspace venv's `python` (see
    `_find_workspace_venv`), mirroring Rust's `venv_python`. `None` when no
    workspace venv resolves, or the one that does has no `python` binary --
    the caller then keeps its own PATH-name fallback rather than spawning a
    path that doesn't exist.

    Forward-slashed, matching `_abs_posix` and `apply_plan_token_substitution`'s
    own `project_root` handling: this value is substituted verbatim into
    `${PYTHON}`, which lands in a `-DPython3_EXECUTABLE=<...>` CMake `-D`
    argument (`orchestrator.py`), and a Windows backslash there is an escape
    character (alp-sdk#849 -- CMake read `\\U`/`\\N` etc. as invalid character
    escapes). `apply_plan_token_substitution` only forward-slashes
    `project_root`, not `python`, so an un-normalised venv path under
    `C:\\Users\\...` reached CMake unescaped.
    """
    venv = _find_workspace_venv(start, sdk_root)
    if venv is None:
        return None
    layout = venv_layout(os.name == "nt")
    candidate = venv / layout.bin_dir / layout.python
    return str(candidate).replace("\\", "/") if candidate.is_file() else None


def _planner_python(start: str, sdk_root: str | None) -> str:
    """The interpreter a SPAWNED build step runs under.

    Two callers remain now that planning and every `generate` target run
    in-process: `${PYTHON}` token substitution (below), and
    `generate_cmd`'s `TAN_GENERATE_EXECUTOR=subprocess` escape hatch.

    Prefers the west-capable workspace venv's `python` (`_venv_python`,
    mirroring Rust's `venv_python`/`resolved_planner_python`), because the SDK
    planner bakes ITS `sys.executable` into every Zephyr slice as
    `-DPython3_EXECUTABLE=<...>` (alp-sdk#787) -- a bare PATH `python3` may
    lack the `west` module entirely, which surfaced as the planner's own
    `ModuleNotFoundError: No module named 'west'` inside CMake configure
    (tan-cli, the documented-install-path bug) rather than a working build.

    Falls back to a bare PATH name -- `python3` off Windows, `python` on it,
    mirroring `tan_core::project::resolve_python_binary` -- only when no
    workspace venv resolves. NOT `sys.executable`: frozen by PyInstaller,
    `sys.executable` is `tan` itself, so spawning it would just re-enter this
    CLI; and this value is also the `${PYTHON}` substituted into the plan, so
    it has to name an interpreter the slice can find, not this process.
    """
    return _venv_python(start, sdk_root) or ("python" if os.name == "nt" else "python3")


def _emit_plan(sdk_root: str | None, board_yaml: str | None) -> str:
    """Plan `board_yaml` against the SDK at `sdk_root`.

    IN-PROCESS, always. `tan.planner_root.emit` binds the SDK root -- which is
    where `metadata/**` and the fact-reader modules still live (ADR-0017) -- and
    renders through the same `emit_artefact` dispatch `python -m tan.planner`
    uses. No interpreter on PATH, no PYTHONPATH, no process, and nothing under
    `<sdk>/scripts` imported or spawned.

    **The `<python> -m alp_orchestrate --emit build-plan` fallback was RETIRED
    here, deliberately.** It fired when this build of `tan` could not import the
    planner and quietly planned through the SDK's own copy instead -- and it
    reported nothing at all when it did. Three reasons it had to go, any one
    sufficient:

    * It was SILENT. A run that fell back looked exactly like a run that did
      not, which is the same defect class that let the planner relocation be
      believed before it happened.
    * It could only fire on a broken install. `jsonschema` and `PyYAML` are
      declared `dependencies` in `pyproject.toml` and installed into the clean
      venv `scripts/build_binary.sh` freezes, so the trigger is not a supported
      layout -- and a missing dependency deserves `pip install jsonschema`, not
      a different planner.
    * It kept alp-sdk's `scripts/alp_orchestrate/` load-bearing forever. As long
      as any tan path can reach it, it can never be deleted, and deleting it is
      the point.

    What replaces it is a coded refusal naming the missing dependency -- which is
    strictly more actionable than a silent success against a second planner whose
    version nothing checks.
    """
    if sdk_root is None:
        raise BuildError(
            "build.plan-unavailable",
            "no alp-sdk checkout found -- pass `--sdk-root <PATH>` or run from a project "
            "beside one. Planning reads the SDK's `metadata/**`.",
            ExitCode.RUNTIME_FAILURE,
        )
    if board_yaml is None:
        raise BuildError(
            "build.plan-unavailable",
            "no board.yaml found -- pass `--board-yaml <PATH>` or run from a project.",
            ExitCode.RUNTIME_FAILURE,
        )
    # `--sdk-root` is terminal and returned as-is even when wrong (I-31), so the
    # first thing planning reads is checked here rather than surfacing as a
    # loader stack trace. The board schema, not the planner: the planner is ours
    # now, and requiring the SDK to still carry `alp_orchestrate` would refuse
    # precisely the releases this relocation exists to enable.
    if not (Path(sdk_root) / "metadata" / "schemas" / "board.schema.json").is_file():
        raise BuildError(
            "build.plan-unavailable",
            f"the SDK at `{sdk_root}` has no `metadata/schemas/board.schema.json` -- "
            f"that path is not an alp-sdk checkout, or the checkout is incomplete.",
            ExitCode.RUNTIME_FAILURE,
        )

    try:
        from tan.planner_root import emit as _plan_emit
    except ImportError as err:  # pragma: no cover -- planner absent from this build
        raise BuildError(
            "build.plan-unavailable",
            f"this build of `tan` cannot import its own planner ({err}). "
            "Reinstall it, or install the planner's dependencies "
            "(`pip install pyyaml jsonschema`).",
            ExitCode.RUNTIME_FAILURE,
        ) from err
    try:
        return _plan_emit("build-plan", root=sdk_root, board_yaml=Path(board_yaml))
    except ImportError as err:
        # A planner dependency (`jsonschema`, `PyYAML`) is missing from THIS
        # build. Named, not worked around: see the docstring on why the
        # spawn-the-SDK's-planner fallback was retired.
        raise BuildError(
            "build.plan-unavailable",
            f"the planner could not be imported ({err}) -- install its "
            "dependencies (`pip install pyyaml jsonschema`) or reinstall `tan`.",
            ExitCode.RUNTIME_FAILURE,
        ) from err
    except SystemExit as err:
        # `tan.planner.loader` / `kconfig_symbols` call `sys.exit()` on a missing
        # dependency or an unbootstrapped Zephyr workspace. In a subprocess that
        # was a non-zero rc; in-process it would tear down the whole CLI past
        # every envelope guarantee.
        raise BuildError(
            "build.plan-unavailable",
            f"the build-plan emit exited early (code {err.code}).",
            ExitCode.RUNTIME_FAILURE,
        ) from err
    except Exception as err:
        # Every planner failure is an envelope, never a traceback -- the same
        # thing the subprocess boundary used to buy for free. Includes the bare
        # `ValueError`s the Ethos-U sizing path still raises (I-48).
        raise BuildError(
            "build.plan-unavailable",
            f"the build-plan emit failed: {type(err).__name__}: {err}",
            ExitCode.RUNTIME_FAILURE,
        ) from err


def _acquire_plan(
    plan_from: str | None, sdk_root: str | None, board_yaml: str | None
) -> tuple[str, BuildPlan]:
    """`(source text, parsed plan)`. The TEXT comes back too because
    `_MODE_PLAN` echoes it: the oracle's plan output is a raw passthrough, not
    a round-trip through its own structs -- measured by adding an unknown
    top-level key to the fixture, which survives verbatim into `data`. Echoing
    a re-serialised `BuildPlan` would silently drop exactly the forward-compat
    keys a newer SDK adds, which is the drift the version-skew guard exists to
    make loud."""
    if plan_from is not None:
        try:
            text = Path(plan_from).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            # UnicodeDecodeError is a ValueError, NOT an OSError, so `except
            # OSError` alone let it fall through to the catch-all and report a
            # bad INPUT as a tan bug (`build.internal-failure`, exit 5).
            # Reachable on the exact flow `--plan-from` exists for: `tan build
            # --plan --format json > plan.json` in Windows PowerShell 5.1
            # writes UTF-16LE, and this is the replay half of that
            # capture-then-replay loop.
            raise BuildError(
                "build.plan-unavailable",
                f"failed to read plan file `{plan_from}`: {err}",
                ExitCode.RUNTIME_FAILURE,
            ) from err
    else:
        text = _emit_plan(sdk_root, board_yaml)

    try:
        return text, parse_build_plan(text)
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
    plan: BuildPlan,
    demotions,
    build_root: Path,
    sdk_root: str | None,
    sdk_root_for_stamp: str | None,
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

    `sdk_root` is threaded straight through to `execute_slices` -- it is
    THIS run's already-resolved `--sdk-root`/discovered checkout (`_build`'s
    own parameter), the identity `execute_slices`'s sdk-switch-pristine guard
    (issue #52) keys its stamp comparison on. Without it every stamp
    comparison degrades to "unresolved", which never wipes anything -- see
    `tan.core.plan_exec.sdk_stamp_key`. `sdk_root_for_stamp` is the SAME
    checkout, NORMALIZED and anchored on the workspace root (`build()`'s own
    `tan.core.plan_exec.normalize_path(workspace_root / sdk_root)`) -- passed
    separately so a relative `--sdk-root ../alp-sdk` keys the stamp
    IDENTICALLY to the absolute form `tan sdk switch` would pin for the same
    checkout (tan-cli#163), without changing what `sdk_root` itself
    substitutes for `${SDK_ROOT}` or reports as `sdk.root`.

    Held slices are also threaded into `execute_slices` as `held_outcomes`
    (tan-cli #89 MINOR 4, oracle `execute/mod.rs:379-400`): `execute_slices`
    only ever sees the RUNNABLE subset of the plan (`replace(plan,
    slices=runnable)` below), so its own post-build manifest overlay would
    otherwise never learn a demoted slice existed at all -- leaving that
    core's `system-manifest.yaml` entry at its plan-time (or a previous
    run's) `status`/`output_artefact` forever, which `tan size`/
    `tan debug-config` read with no status check of their own. Each held
    outcome's `output_artefact` is the explicit empty string, never `None`:
    the overlay (`overlay_run_results_raw`) treats `None` as "preserve
    whatever is already there", which is correct for a slice this run never
    touched but wrong for one THIS run knows never dispatched.
    """
    held = {
        d.slice_index: d
        for d in demotions
        if plan.slices[d.slice_index].backend in KNOWN_BACKENDS
        and plan.slices[d.slice_index].command is not None
    }
    action = resolve_action(plan.execution_policy, "missing_tool", PolicyAction.SKIP)
    failed = action is PolicyAction.FAIL

    held_outcomes = {
        i: SliceOutcome(
            plan.slices[i].core_id,
            "failed" if failed else "skipped",
            None,
            d.reason,
            output_artefact="",
        )
        for i, d in held.items()
    }

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
            sdk_root=sdk_root,
            sdk_root_for_stamp=sdk_root_for_stamp,
            held_outcomes=held_outcomes.values(),
        )
    )

    outcomes: list[SliceOutcome] = []
    issues: list[Issue] = []
    for i, sl in enumerate(plan.slices):
        demotion = held.get(i)
        if demotion is None:
            outcomes.append(next(dispatched))
            continue
        outcomes.append(held_outcomes[i])
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
    *,
    # Defaulted so `mode` stays optional for the OTHER caller of this engine:
    # `run_cmd._build_then_run` builds unconditionally (there is no `tan run
    # --plan`/`--materialise`) and has no mode to pass.
    mode: str = _MODE_NATIVE,
    plan_from: str | None,
    build_root: str,
    sdk_root: str | None,
    sdk_root_for_stamp: str | None,
    board_yaml: str | None,
) -> tuple[ExitCode, dict, list[Issue]]:
    text, plan = _acquire_plan(plan_from, sdk_root, board_yaml)

    if mode == _MODE_PLAN:
        # Parsed (so a plan that will not load is still refused with its own
        # code -- measured: the oracle rejects `schemaVersion: 2` and an
        # unknown `executionPolicy` action in THIS mode too), then echoed
        # verbatim. No token substitution: the oracle shows a `planPathMode:
        # tokened` plan with no SDK resolvable at exit 0, unsubstituted, and
        # only refuses it once `--materialise` asks for it on disk.
        return ExitCode.SUCCESS, json.loads(text), []

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
            python=_planner_python(build_root, sdk_root),
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

    if mode == _MODE_MATERIALISE:
        # Write and stop -- the oracle dispatches nothing here, and its `data`
        # carries no `slices` at all. `written` is rebuilt from the plan's own
        # (post-substitution) artefact paths in `materialise_plan`'s documented
        # order, shared first then per slice, because those are the RELATIVE
        # strings the oracle reports; `materialise_plan` itself hands back
        # resolved absolute paths, and relativising those back re-introduces
        # every symlink/case difference `_abs_posix` exists to avoid.
        written = [a["path"] for a in plan.shared_artefacts]
        for sl in plan.slices:
            written.extend(a["path"] for a in sl.config_artefacts)
        return (
            ExitCode.SUCCESS,
            {"schemaVersion": "1", "baseDir": build_root, "written": written},
            [],
        )

    # Cleared before dispatch, mirroring `run_cmd.py`'s own pattern, so a
    # leftover signal from an earlier in-process `_build` (e.g. a test
    # harness calling `build()` more than once) never gets misread as THIS
    # run's own write outcome below.
    reset_last_manifest_write()
    outcomes, issues = _dispatch(
        plan, demotions, Path(build_root), sdk_root, sdk_root_for_stamp
    )

    any_failed = any(o.status not in ("succeeded", "skipped") for o in outcomes)
    exit_code = ExitCode.RUNTIME_FAILURE if any_failed else ExitCode.SUCCESS
    if any_failed:
        issues.insert(
            0, Issue("build.slice-failed", "error", "one or more slices failed to build")
        )
    issues.extend(_backend_issues(plan, outcomes))
    # The sdk-switch-pristine wipe (issue #52) must not be stderr-only in
    # JSON mode -- the VS Code extension only ever sees the envelope, not
    # `_stream`'s output. Verbatim oracle codes/severity
    # (`crates/tan-cli/src/commands/build/execute/mod.rs:591,617`):
    # `build.sdk-switch-pristine` for the wipe itself, `-failed` when the
    # wipe could not fully land.
    issues.extend(last_sdk_switch_issues())

    manifest_reason = last_manifest_write_failure()
    if manifest_reason is not None:
        # A failed manifest write used to report only through `on_output`
        # (stderr) -- so the envelope said `ok: true, issues: []` while
        # `system-manifest.yaml` still named the PREVIOUS run's status and
        # artefact, which `tan size` re-derives and `tan flash` would go on
        # to program. Verbatim oracle code/message
        # (`crates/tan-cli/src/commands/build/execute/mod.rs:809-814`):
        # dropping this half is exactly how a stale manifest used to look
        # identical to `ok: true`.
        issues.append(
            Issue(
                "build.manifest-write-failed",
                "warning",
                f"system-manifest.yaml was not updated — {manifest_reason}",
            )
        )

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


def _refuse(code: str, message: str, exit_code: ExitCode, json_mode: bool) -> None:
    """Report a refusal this command makes UP FRONT -- before any project is
    resolved, any plan read, and any byte written -- and exit.

    One helper for both of them (a deferred flag, and a conflicting flag pair)
    because they differ only in code, message and exit code. `project` is left
    unresolved, as `deferred_cmd`'s verb-level stubs leave it: nothing was
    read, so reporting a root would be reporting work this run did not do."""
    issue = Issue(code, "error", message)
    if json_mode:
        emit(
            Envelope(
                "build",
                Project(root=None, board_yaml=None),
                {"message": message},
                [issue],
                exit_code,
            )
        )
    else:
        print(f"error: {message}", file=sys.stderr)
    raise typer.Exit(int(exit_code))


def build(
    plan_from: str = typer.Option(
        None,
        "--plan-from",
        metavar="FILE",
        help="Read the build plan from a JSON file instead of invoking the SDK planner. "
        "Implies --plan: shows the plan and exits unless --materialise or --execute "
        "is given.",
    ),
    materialise: bool = typer.Option(
        False,
        "--materialise",
        help="Write the plan's generated files (shared artefacts + per-slice config) "
        "under the build root and stop, instead of just showing the plan.",
    ),
    # Accepted and INERT, exactly as in the oracle ("This is the default; the
    # flag is kept as an explicit opt-in" -- its own `--native` help). Never
    # read below on purpose: a bare build is already native, and letting it
    # override the `--plan` implied by `--plan-from` is the divergence
    # `test_plan_from_shows_the_plan_and_writes_nothing_even_with_native`
    # exists to keep out. `--execute` is the flag that dispatches a
    # file-supplied plan.
    native: bool = typer.Option(
        False,
        "--native",
        help="Build natively: materialise the plan, then run each slice's command. "
        "The default when no plan-mode flag is given. Like v0.4.1, this does NOT "
        "override the --plan implied by --plan-from -- use --execute for that.",
    ),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Materialise the plan AND run each slice's command, even when the plan "
        "came from --plan-from -- run a pinned, reviewed plan file reproducibly. "
        "Implies --materialise (nothing can run that was never written); reports the "
        "ordinary build result. ADDED BY THIS PORT, not a v0.4.1 flag: there "
        "--plan-from implies --plan and outranks --native, so a file-supplied plan "
        "cannot be dispatched at all. Deliberate, not a parity gap.",
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
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    output_format: str = typer.Option(
        "text", "--format", metavar="FORMAT", help="Output format: text or json."
    ),
    # --- declared so they are refused as DEFERRED, never as a typo ----------
    # See `_DEFERRED_FLAGS`. Each is a real, working flag of the v0.4.1 oracle
    # that this port does not implement. LISTED in `--help` rather than hidden,
    # for the same reason `deferred_cmd` registers its seven verbs in the
    # command list: a reader comparing this surface to v0.4.1's needs to see
    # that tan knows the flag and is refusing it, which absence cannot say.
    plan: bool = typer.Option(False, "--plan", help=_DEFERRED_HELP),
    target: str = typer.Option(None, "--target", metavar="EMIT", help=_DEFERRED_HELP),
    all_targets: bool = typer.Option(False, "--all", help=_DEFERRED_HELP),
    manifest: bool = typer.Option(False, "--manifest", help=_DEFERRED_HELP),
    manifest_from: str = typer.Option(
        None, "--manifest-from", metavar="FILE", help=_DEFERRED_HELP
    ),
    no_auto_bootstrap: bool = typer.Option(False, "--no-auto-bootstrap", help=_DEFERRED_HELP),
    pristine: bool = typer.Option(False, "--pristine", help=_DEFERRED_HELP),
    verbose: bool = typer.Option(False, "--verbose", help=_DEFERRED_HELP),
    quiet: bool = typer.Option(False, "--quiet", help=_DEFERRED_HELP),
    no_color: bool = typer.Option(False, "--no-color", help=_DEFERRED_HELP),
    non_interactive: bool = typer.Option(False, "--non-interactive", help=_DEFERRED_HELP),
    ci: bool = typer.Option(False, "--ci", help=_DEFERRED_HELP),
) -> None:
    """Build every slice of the project's build plan."""
    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = output_format == "json"

    # Before anything is resolved or read: a run naming a flag this port does
    # not implement does no work at all, and says which flag and why.
    given = dict(
        zip(
            _DEFERRED_FLAGS,
            (
                plan,
                target is not None,
                all_targets,
                manifest,
                manifest_from is not None,
                no_auto_bootstrap,
                pristine,
                verbose,
                quiet,
                no_color,
                non_interactive,
                ci,
            ),
            strict=True,
        )
    )
    # FIRST, ahead of the conflict check below: a flag this build does not
    # implement at all cannot be honoured in any combination, so `--execute
    # --plan` gets the accurate answer (that `--plan` is deferred) rather than
    # a conflict message for a flag that would not have worked anyway. Either
    # way it is a coded refusal, never a silent precedence win.
    for flag, was_given in given.items():
        if was_given:
            _refuse(
                DEFERRED_ISSUE_CODE,
                f"`tan build {flag}` is deferred to v0.6.0 and not available in "
                f"this build (see {DEFERRED_ISSUE_URL}).",
                ExitCode.RUNTIME_FAILURE,
                json_mode,
            )

    if execute and materialise:
        # Two different answers to "what does this run leave behind" -- one
        # stops after writing, one goes on to dispatch. Refused with its own
        # code at exit 2 (this CLI's position for an invalid invocation, the
        # same `--format bogus` takes) instead of letting either win quietly:
        # a silent precedence win here is precisely what `--execute` exists to
        # replace.
        _refuse(
            "build.conflicting-flags",
            "`--execute` and `--materialise` cannot be combined: `--materialise` "
            "writes the plan's files and stops, `--execute` writes them and then "
            "runs each slice. Pick one (`--execute` already implies writing).",
            ExitCode.VALIDATION_FAILURE,
            json_mode,
        )

    # The oracle's own precedence, measured, reproduced exactly -- `--native`
    # included: `--plan-from` implies `--plan`, and that implied `--plan` beats
    # an explicit `--native`, so `--plan-from --native` shows the plan and
    # writes nothing here too. `--execute` is the port's ADDED capability (see
    # the module docstring) and the only thing that dispatches a file-supplied
    # plan; it selects the same `_MODE_NATIVE` a plain `tan build` runs, so it
    # reports the same `data` rather than a shape of its own. With no
    # `--plan-from` it is a no-op -- that run already builds.
    if execute:
        mode = _MODE_NATIVE
    elif materialise:
        mode = _MODE_MATERIALISE
    elif plan_from is not None:
        mode = _MODE_PLAN
    else:
        mode = _MODE_NATIVE

    # `util::cli_workspace_root`: `--project` joined to the (real) cwd, THEN
    # everything below anchors on this instead of the bare cwd -- board.yaml
    # discovery, the default build root, and SDK discovery. Was previously
    # missing entirely (this command had no `--project` at all): the extension
    # falls back to it when no project is active in the workspace
    # (`alp-sdk-vscode/src/west.ts`'s `alpBuild`, `--project <example> build`).
    # A Typer option Typer does not declare is a Click parse error (exit 2),
    # so the missing flag REFUSED outright rather than building silently; the
    # actual wrong-project risk is the relative `--board-yaml` anchor fixed
    # below. `project is None` (the overwhelming common case, no flag given)
    # makes `workspace_root == cwd`, byte-for-byte the prior behaviour -- this
    # only changes anything when `--project` is actually given.
    cwd = Path.cwd()
    workspace_root = cwd if project is None else Path(os.path.join(str(cwd), project))

    # Anchor an EXPLICIT `--board-yaml` on `workspace_root`, not the real cwd,
    # before anything derives from it (the default build root below included).
    # `_abs_posix` is purely lexical (`os.path.abspath`, which anchors on
    # `os.getcwd()`), so a relative `--board-yaml` left untouched re-anchors on
    # the real cwd instead -- matching Rust's `resolve_board_yaml_path`
    # (`crates/tan-core/src/project.rs:198-208`, which joins a relative
    # configured path onto `workspace_root`). Verified against the oracle in a
    # scratch tree (`tmp/app/board.yaml`, cwd=`tmp`): `tan --project app build
    # --board-yaml board.yaml --format json` must report `project.root` /
    # `project.boardYaml` under `tmp/app`, not `tmp` -- with a board.yaml also
    # sitting in the real cwd, the pre-fix anchor planned and built the WRONG
    # project without a word, exactly the failure class this port exists to
    # remove.
    if board_yaml is not None and not os.path.isabs(board_yaml):
        board_yaml = os.path.join(str(workspace_root), board_yaml)

    # Resolution, in one place, before anything can fail -- and to ABSOLUTE,
    # BOTH sides, from the same anchor.
    #
    # The divergence guard in `apply_plan_token_substitution` compares these
    # two lexically (as the Rust oracle does), so they must move together: this
    # resolves both or neither, never one. But they must also both be absolute,
    # because `${PROJECT_ROOT}` is substituted from `board_yaml` and then
    # CONSUMED from a different directory -- each slice runs its command in its
    # own `command.cwd` (`<root>/build/<slice>`), and Zephyr resolves
    # `-DEXTRA_CONF_FILE` against the APPLICATION source dir, which for the
    # stock-shim slice is inside the SDK checkout entirely. Left relative, the
    # default `cd <project> && tan build` resolves `${PROJECT_ROOT}` to `"."`
    # and every zephyr slice dies -- `<sdk>/firmware/alp-stock-shim/./build/
    # <slice>/alp.conf: File not found`, and `ERROR: . doesn't contain a
    # CMakeLists.txt` -- which is the whole documented happy path. Rust never
    # hits this because it anchors first: `resolve_cli_project_context_inner`
    # builds `workspace_root = normalize_path(cwd.join(--project))` and derives
    # `board_yaml_path` by joining onto THAT, so both are absolute before the
    # guard ever runs (`crates/tan-cli/src/util.rs`, `crates/tan-core/src/
    # project.rs::resolve_board_yaml_path`).
    if board_yaml is None and (workspace_root / "board.yaml").is_file():
        # Already absolute (anchored on `workspace_root`, not a bare
        # relative "board.yaml") so `_abs_posix` below is a no-op
        # normalisation rather than a re-anchor onto the real cwd -- the
        # divergence that would reappear the moment `--project` differs
        # from cwd.
        board_yaml = str(workspace_root / "board.yaml")
    if build_root is None:
        build_root = str(Path(board_yaml).parent) if board_yaml else str(workspace_root)
    build_root = _abs_posix(build_root)
    if board_yaml is not None:
        board_yaml = _abs_posix(board_yaml)

    # `--sdk-root` > the project's own `.alp/sdk-path` pin > the machine-global
    # default > the positional walk (`resolve_sdk_root_ladder`, above) --
    # previously this skipped straight from `--sdk-root` to the positional
    # walk, so `tan init`'s own pointer went unread the moment `tan build` ran
    # in the same directory.
    resolved_sdk_root, sdk_tier = resolve_sdk_root_ladder(sdk_root, workspace_root)
    sdk_root = str(resolved_sdk_root) if resolved_sdk_root is not None else None
    sdk = SdkInfo(sdk_root, sdk_tier) if sdk_root is not None else None
    # Absolute, `.`/`..`-collapsed, anchored on `workspace_root` -- what the
    # sdk-switch-pristine guard actually compares (tan-cli#163), kept
    # SEPARATE from `sdk_root` itself: an explicit `--sdk-root` is a
    # documented, supported relative form, and `sdk_root` unchanged still
    # feeds `${SDK_ROOT}` token substitution and the `sdk.root` envelope
    # field verbatim, matching the Rust oracle's own split between
    # `normalized_sdk_root_str` (stamp-only) and the raw `resolve_sdk_root`
    # result used everywhere else.
    sdk_root_for_stamp = (
        str(normalize_path(workspace_root / sdk_root)) if sdk_root is not None else None
    )
    project = Project(root=build_root, board_yaml=board_yaml)

    try:
        exit_code, data, issues = _build(
            mode=mode,
            plan_from=plan_from,
            build_root=build_root,
            sdk_root=sdk_root,
            sdk_root_for_stamp=sdk_root_for_stamp,
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
        _text_recap(mode, data)
    raise typer.Exit(int(exit_code))


def _text_recap(mode: str, data: dict | None) -> None:
    """The stderr recap for whichever mode ran. Three shapes because `data`
    has three shapes -- a plan-mode `data.slices[]` entry is a PLAN slice with
    no `status` at all, so the build recap below would `KeyError` on it."""
    if data is None:
        return
    if mode == _MODE_MATERIALISE:
        print(
            f"materialised {len(data['written'])} file(s) under {data['baseDir']}:",
            file=sys.stderr,
        )
        for rel in data["written"]:
            print(f"  {rel}", file=sys.stderr)
        return
    if mode == _MODE_PLAN:
        print(
            f"build plan (schema v{data.get('schemaVersion')}) -- {data.get('sku')}",
            file=sys.stderr,
        )
        print(f"  board.yaml: {data.get('boardYaml')}", file=sys.stderr)
        print(f"  build root: {data.get('buildRoot')}", file=sys.stderr)
        slices = data.get("slices") or []
        print(f"  slices ({len(slices)}):", file=sys.stderr)
        for sl in slices:
            print(
                f"    - {sl.get('coreId')} [{sl.get('backend')}] -> {sl.get('buildDir')}",
                file=sys.stderr,
            )
        print(
            f"  shared artefacts: {len(data.get('sharedArtefacts') or [])}",
            file=sys.stderr,
        )
        print(f"  warnings: {len(data.get('warnings') or [])}", file=sys.stderr)
        return
    for result in data.get("slices", []):
        reason = f" -- {result['reason']}" if "reason" in result else ""
        print(
            f"{result['status']}: {result['coreId']} [{result['backend']}]{reason}",
            file=sys.stderr,
        )
