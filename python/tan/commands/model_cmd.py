# SPDX-License-Identifier: Apache-2.0
"""`tan model build` -- compile + package `board.yaml`'s `models:` block into
`.alpmodel` packages.

Port of `scripts/alp_cli/model.py` (51 lines): the board.yaml discovery,
per-model source/compile-option path resolution, and the `built <path>`
summary all move here, in-process, exactly as they read there. Also
in-process, since ADR-0028: `tan.model.build.build_model` itself -- the
compiler-adapter engine (CPU/Vela/DRP-AI/DeepX) that does the actual work,
relocated verbatim from alp-sdk's `scripts/alp_model/` into `tan.model`. This
command calls it directly, per model, no subprocess involved.

Earlier revisions of this file spawned a `python -c` driver under the SDK
checkout's own interpreter, on the premise that the vendor NPU-compiler
tooling was only reachable from that checkout's Python environment. That
premise was never accurate: every adapter resolves its own tool with
`shutil.which` and spawns an external binary -- `adapters/ethos_u.py`
(`shutil.which("vela")`, `cmd = ["vela", ...]`), `adapters/deepx.py`
(`shutil.which("dxcom")`, `cmd = ["dxcom", "-m", ...]`), `adapters/drpai.py`
likewise -- so what building a model actually needs is `vela`/`dxcom` on
PATH, a host fact, not a checkout fact. The one genuine Python-environment
dependency, `adapters/ethos_u.py`'s `_vela_version()`, already degrades
gracefully (`except PackageNotFoundError: return "vela"`), costing a less
precise `compiler_version` string and nothing else. **This module still
resolves the SDK root** (via `resolve_metadata_sdk_root`) -- not for its
Python, but because `tan.model.build.build_model` reads alp-sdk's
`metadata/**` at call time (ADR-0017: metadata stays in alp-sdk).

This is a REAL implementation, not a forward: it never spawns `python -m
alp_cli`, so `alp_cli` stops being load-bearing for `tan model` (the point of
this port -- see `crates/tan-cli/src/commands/sdk_cli.rs`'s module doc for
what it is replacing).

**Deliberate divergence 1 from the oracle survives ADR-0028**: `alp_cli/
model.py` has no try/except around `build_model()` at all, so a build failure
(e.g. "no blob compiled for model") tracebacks the whole click command. Every
command in this port instead resolves to a coded issue, never a traceback
(the established rule -- see `generate_cmd`'s module doc) -- so a per-model
failure here is caught around the direct `build_model()` call and reported as
a `model.build-failed` issue, and the run continues to the next model rather
than aborting the whole batch.

**Deliberate divergence 2 from the oracle is RETIRED with the driver it
existed for.** It used to guard against a spawned driver that exited 0 having
silently produced no result for a declared model -- a failure mode only a
subprocess boundary could produce. With `build_model()` called directly,
in-process, per model, there is no boundary across which a result can go
missing: either the call returns a path (recorded as built) or raises
(caught and reported as `model.build-failed`). There is no third outcome left
to guard against, so the guard is gone, not silently dropped.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer

from tan.commands.build_output import (
    ProjectContext,
    resolve_metadata_sdk_root,
    resolve_project_context,
)
from tan.commands.sdk_cmd import NO_SDK_NEXT_STEPS, sdk_resolution_issues
from tan.core.global_flags import accept_global_flags
from tan.core.shapes import rejected_sdk_root_message
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.model.build import build_model
from tan.output_format import FORMAT_HELP, OutputFormat

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"


class ModelError(Exception):
    """A refusal whose issue code and exit code are already decided."""

    def __init__(self, code: str, message: str, exit_code: ExitCode) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


#: Compile-opt keys that name a filesystem path (resolved relative to
#: board.yaml). Not every value in a models[].compile.<backend> block is a
#: path -- e.g. drpai's input_shape ("1,3,224,224"), input_name ("images") and
#: product ("V2N") are opaque strings that must reach the adapter unchanged
#: (alp-sdk#1271: resolving them as paths corrupted a genuine shape string into
#: a filesystem path, which then made the adapter's own shape check misfire).
_PATH_OPT_KEYS = {"config", "calibration", "images", "spec"}


def _resolve_compile(block: dict | None, base: Path) -> dict | None:
    """Port of `model.py::_resolve_compile`: resolve known path-valued keys
    in each per-backend compile block to an absolute path relative to the
    `board.yaml` dir; every other value (shape strings, node names, product
    ids, ...) passes through unchanged (alp-sdk#1271)."""
    if not block:
        return None
    return {
        backend: {
            k: (str((base / v).resolve()) if k in _PATH_OPT_KEYS and isinstance(v, str) else v)
            for k, v in (opts or {}).items()
        }
        for backend, opts in block.items()
    }


def _load_board(path: Path) -> dict[str, Any]:
    """`board.yaml` as a dict, or a `ModelError` for every way that can fail --
    missing file, bad encoding, not YAML, not a mapping. `yaml.safe_load`,
    matching the oracle's own parse exactly (unlike `system_manifest`'s
    core-schema loader, there is no serde_yaml parity requirement here)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise ModelError(
            "model.board-yaml-missing",
            f"board.yaml not found at {path}: {err}",
            ExitCode.VALIDATION_FAILURE,
        ) from err
    except UnicodeDecodeError as err:
        # tan-cli#396: `UnicodeDecodeError` is a `ValueError`, NOT an
        # `OSError`, so `except OSError` alone could never catch it -- one
        # undecodable byte in the customer's own board.yaml escaped this
        # whole command as a traceback: measured `exitCode: 5` /
        # `model.internal-failure` / "model build failed unexpectedly:
        # UnicodeDecodeError: ...", which tells a script "tan broke" rather
        # than "your board.yaml has a bad byte". The file opened and read
        # fine, so `board-yaml-missing` would send the customer looking for
        # the wrong problem -- this is the same unusable-input class the
        # YAML-syntax and not-a-mapping arms below already land on.
        #
        # `model` has no Rust oracle to defer to here (unlike `kconfig`,
        # which folds this same byte-for-byte case into `board-yaml-missing`
        # to mirror `read_to_string`'s single `io::Error` arm) -- a merge
        # briefly folded this arm into the OSError one above on that other
        # command's precedent (tan-cli#415), which made this whole `except`
        # unreachable dead code and silently reverted the tan-cli#396 fix.
        # Restored as its own arm: this file's own established
        # classification, not an oracle's.
        raise ModelError(
            "model.board-yaml-invalid",
            f"{path}: not valid UTF-8: {err}",
            ExitCode.VALIDATION_FAILURE,
        ) from err
    try:
        import yaml  # noqa: PLC0415 (declared dependency, guarded anyway)
    except ImportError as err:
        raise ModelError(
            "model.internal-failure",
            f"no YAML parser available ({err}); install PyYAML (`pip install pyyaml`).",
            ExitCode.INTERNAL_FAILURE,
        ) from err
    try:
        doc = yaml.safe_load(text)
    except Exception as err:  # noqa: BLE001 -- any PyYAML failure is bad input
        raise ModelError(
            "model.board-yaml-invalid", f"{path}: {err}", ExitCode.VALIDATION_FAILURE
        ) from err
    if not isinstance(doc, dict):
        raise ModelError(
            "model.board-yaml-invalid",
            f"{path}: expected a YAML mapping at the top level.",
            ExitCode.VALIDATION_FAILURE,
        )
    return doc


def _run_build(
    *,
    context: ProjectContext,
    out: str,
    metadata_root: str | None,
    sdk_root: str | None,
) -> tuple[Project, SdkInfo | None, dict, list[Issue], ExitCode]:
    """`--board` plays the role `--board-yaml` does everywhere else, so the
    SAME project-context resolution applies (I-31); the envelope's `project`
    and `sdk` fields come from that ONE resolution, matching `size`/`image`
    (`build_output.resolve_project_context`'s own doc: the envelope's `sdk`
    block must be what THAT resolution produced, never a second lookup).

    tan-cli#497 defect 3: the `resolve_project_context` call used to live at
    the top of this function, which meant its `broken_project_pin` /
    `foreign_global_default_for` were unreachable from `model`'s own
    `except ModelError` handler -- so the pin warning was dropped on the
    refusal paths as well as the happy one. It is now resolved by the caller
    and handed in, so the ONE resolution feeds every exit."""
    workspace_root = Path(context.workspace_root)
    board_path = Path(context.board_yaml)
    reported_project = context.project()
    sdk_info = context.sdk

    # The metadata-reading resolution is DELIBERATELY separate and wider
    # (`build_output.resolve_metadata_sdk_root`'s own doc): a child/sibling
    # checkout the project-context tier chain does not consider can still
    # supply `metadata/`, in which case `sdk_info` stays absent while the
    # build still runs against it -- same divergence `tan size`'s budget
    # resolution already allows.
    resolved_sdk = resolve_metadata_sdk_root(sdk_root, context.workspace_root)
    if resolved_sdk is None:
        raise ModelError(
            "model.sdk-root-unresolved",
            # tan-cli#497 defect 7: a REJECTED `--sdk-root` names the value.
            # The no-flag message below opens with "Use --sdk-root" -- exactly
            # the flag the caller just typed -- and the rejected path appeared
            # nowhere else in the envelope, since `resolve_metadata_sdk_root`
            # returning `None` is also what leaves the `sdk` block absent.
            rejected_sdk_root_message(sdk_root, "No models were built.")
            if sdk_root
            # `tan sdk switch` refuses in this build (tan-cli#305) -- kept the
            # two mechanisms that actually work here (`--sdk-root`, placing
            # the project near a checkout) and swapped the third for
            # NO_SDK_NEXT_STEPS's honest "how to get one at all".
            else "alp-sdk root is unresolved. Use --sdk-root, place the project near an "
            f"alp-sdk checkout, or {NO_SDK_NEXT_STEPS}.",
            ExitCode.VALIDATION_FAILURE,
        )

    board_doc = _load_board(board_path)
    som = board_doc.get("som")
    sku = som.get("sku") if isinstance(som, dict) else None
    if not isinstance(sku, str) or not sku:
        raise ModelError(
            "model.board-yaml-invalid",
            f"{board_path}: som.sku is missing.",
            ExitCode.VALIDATION_FAILURE,
        )
    models = board_doc.get("models") or []
    if not isinstance(models, list):
        raise ModelError(
            "model.board-yaml-invalid",
            f"{board_path}: `models:` must be a list.",
            ExitCode.VALIDATION_FAILURE,
        )

    data: dict[str, Any] = {"schemaVersion": DATA_SCHEMA_VERSION, "sku": sku, "built": []}
    if not models:
        return reported_project, sdk_info, data, [], ExitCode.SUCCESS

    base = board_path.parent
    out_dir = Path(out)
    if not out_dir.is_absolute():
        out_dir = workspace_root / out_dir
    metadata_dir = (
        Path(metadata_root) if metadata_root else resolved_sdk / "metadata"
    )
    if metadata_root and not metadata_dir.is_absolute():
        metadata_dir = workspace_root / metadata_dir

    # Validate + prepare every model BEFORE building any of them -- an invalid
    # `models:` entry refuses the whole run with no partial build, matching
    # the pre-ADR-0028 shape where this same loop only assembled the driver
    # payload and never itself built anything.
    prepared: list[tuple[str, Path, dict | None]] = []
    for m in models:
        if not isinstance(m, dict) or "name" not in m or "source" not in m:
            raise ModelError(
                "model.board-yaml-invalid",
                f"{board_path}: every `models:` entry needs `name` and `source`.",
                ExitCode.VALIDATION_FAILURE,
            )
        source = (base / m["source"]).resolve()
        prepared.append((m["name"], source, _resolve_compile(m.get("compile"), base)))

    # Deliberate divergence 1 from the oracle (see module doc): a per-model
    # `build_model()` failure is caught here and reported as a coded
    # `model.build-failed` issue, and the loop continues to the next model
    # rather than aborting the whole batch.
    issues: list[Issue] = []
    built: list[str] = []
    for name, source, compile_opts in prepared:
        try:
            out_path = build_model(
                sku=sku,
                name=name,
                source=source,
                out_dir=out_dir,
                metadata_root=metadata_dir,
                compile_opts=compile_opts,
            )
        except Exception as err:  # noqa: BLE001 -- a per-model failure is a coded issue, not a traceback
            issues.append(
                Issue(
                    "model.build-failed",
                    "error",
                    f"model '{name}': {type(err).__name__}: {err}",
                )
            )
        else:
            built.append(str(out_path))
    data["built"] = built
    exit_code = ExitCode.SUCCESS if not issues else ExitCode.WRITE_FAILURE
    return reported_project, sdk_info, data, issues, exit_code


def model(
    subcommand: str = typer.Argument(None, metavar="SUBCOMMAND", help="build."),
    board: str = typer.Option(
        # tan-cli#398: `--board-yaml` is a REAL second spelling of this one
        # option, not a second option -- it is what `build`, `run`, `kconfig`,
        # `validate`, `generate` and `inspect` all call the board file, and
        # what `docs/CLI.md`'s "Common flags" tells a caller every command
        # supports. Declared here so `accept_global_flags` sees it as already
        # covered and stops injecting an inert twin behind this one: a caller
        # who used the surface-wide spelling was served `./board.yaml`'s
        # `som.sku` instead of the file they named, and `som.sku` is what
        # picks the silicon `build_model` compiles for.
        "board.yaml", "--board", "--board-yaml", metavar="PATH", help="Path to board.yaml."
    ),
    out: str = typer.Option(
        "build/models", "--out", metavar="PATH", help="Output directory."
    ),
    metadata_root: str = typer.Option(
        None,
        "--metadata-root",
        metavar="PATH",
        help="Path to the metadata/ root (default: <sdk-root>/metadata).",
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
) -> None:
    """Compile + package board.yaml `models:` into `.alpmodel` packages."""
    json_mode = output_format == "json"

    def finish(
        project_: Project,
        sdk: SdkInfo | None,
        data: dict,
        issues: list[Issue],
        exit_code: ExitCode,
    ) -> None:
        if json_mode:
            emit(Envelope("model", project_, data, issues, exit_code, sdk=sdk))
        else:
            # tan-cli#497 defect 3: warnings LEAD. `issues` now carries the
            # SDK-resolution pair, which is a fact about the whole run --
            # which checkout answered -- not a per-model outcome, so it must
            # be readable before the result lines rather than buried under
            # them. `{severity}: {message}`, the shape `build`/`run`/`kconfig`
            # print a resolution warning with, so the same workspace reads
            # the same way whichever command a developer runs.
            warnings = [i for i in issues if i.severity != "error"]
            errors = [i for i in issues if i.severity == "error"]
            for issue in warnings:
                print(f"{issue.severity}: {issue.message}", file=sys.stderr)
            for path in data.get("built", []):
                print(f"built {path}", file=sys.stderr)
            # `not issues` before -- the same thing until `issues` started
            # carrying those warnings. A board with no `models:` and a broken
            # project pin must still be told nothing was declared; a warning
            # is not a reason to withhold the only line this run had to say.
            if not data.get("built") and not errors:
                print(
                    "model: no `models:` declared in board.yaml; nothing to build.",
                    file=sys.stderr,
                )
            for issue in errors:
                print(f"model: {issue.message}", file=sys.stderr)
        raise typer.Exit(int(exit_code))

    if subcommand != "build":
        finish(
            Project(root=None, board_yaml=None),
            None,
            {"schemaVersion": DATA_SCHEMA_VERSION, "sku": None, "built": []},
            [
                Issue(
                    "model.unknown-subcommand",
                    "error",
                    f"Unknown model subcommand: {'(none)' if subcommand is None else subcommand}. "
                    "Available: build.",
                )
            ],
            ExitCode.RUNTIME_FAILURE,
        )
        return

    # tan-cli#497 defect 3: `model build` was the only `resolve_project_context`
    # caller that read `.workspace_root`/`.board_yaml`/`.project()`/`.sdk` off
    # the returned context and NONE of the three resolution facts -- so it
    # alone dropped BOTH `sdk.project-pin-unresolved` (tan-cli#263) and
    # `sdk.global-default-foreign-project` (tan-cli#464), in JSON and text
    # alike, while `size` and `image` reported them from the same directory
    # through the same resolver. That matters most here: the `.alpmodel`
    # packages are compiled against `<resolved checkout>/metadata`, i.e.
    # against that checkout's target/backend table for `som.sku`.
    #
    # Resolved HERE rather than inside `_run_build` so the warnings survive a
    # `ModelError` too -- `project_pin_issue`'s contract is "shared by EVERY
    # caller of `resolve_sdk_tiered`", not "every caller that got as far as a
    # successful build".
    sdk_issues: list[Issue] = []
    try:
        context = resolve_project_context(project, board, sdk_root)
        sdk_issues = sdk_resolution_issues(
            context.broken_project_pin,
            context.sdk_source_tier,
            context.foreign_global_default_for,
        )
        project_, sdk, data, issues, exit_code = _run_build(
            context=context,
            out=out,
            metadata_root=metadata_root,
            sdk_root=sdk_root,
        )
    except ModelError as err:
        finish(
            Project(root=None, board_yaml=None),
            None,
            {"schemaVersion": DATA_SCHEMA_VERSION, "sku": None, "built": []},
            [*sdk_issues, Issue(err.code, "error", err.message)],
            err.exit_code,
        )
        return
    except Exception as err:  # noqa: BLE001 -- the envelope IS the error contract
        finish(
            Project(root=None, board_yaml=None),
            None,
            {"schemaVersion": DATA_SCHEMA_VERSION, "sku": None, "built": []},
            [
                *sdk_issues,
                Issue(
                    "model.internal-failure",
                    "error",
                    f"model build failed unexpectedly: {type(err).__name__}: {err}",
                ),
            ],
            ExitCode.INTERNAL_FAILURE,
        )
        return

    finish(project_, sdk, data, [*sdk_issues, *issues], exit_code)


# tan-cli#261: adds the seven oracle `GlobalArgs` flags this command is
# missing entirely (`--all`/`--ci`/`--no-color`/`--non-interactive`/
# `--quiet`/`--target`/`--verbose`); see `tan.core.global_flags`.
#
# `--board-yaml` is NOT among them any more (tan-cli#398). It used to be, and
# the note here claimed that was harmless because "`model`'s own `--board`
# already plays `--board-yaml`'s role for real" -- but the two spellings were
# never wired together, so the injected one was accepted, dropped, and the
# default `./board.yaml` packaged instead of the file the caller named, at
# `exitCode: 0` with `issues: []`. It is now a second decl on `--board`
# itself (above), so both spellings are one option and read one file.
#
# The six arity-0 flags above stay accepted-and-dropped; the injected
# `--target` is now REFUSED when supplied rather than ignored, per the same
# issue -- `model` has no emit target to honour it with.
model = accept_global_flags(model)
