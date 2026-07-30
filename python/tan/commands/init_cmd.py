# SPDX-License-Identifier: Apache-2.0
"""`tan init` -- scaffold a new project. A fresh customer's FIRST command.

Composition, not logic: resolve the template/name/destination, ask
`tan.core.scaffold` for the planned files, diff them against disk, then preview
or write -- and fold whatever comes back into exactly one envelope. Mirrors
`crates/tan-cli/src/commands/init/` (`mod.rs` + `from_example.rs`'s shared
`finish` step + `response.rs`'s envelope builders).

Four properties this file exists to hold:

**Every failure is a coded issue, never a traceback.** An unknown template, an
unwritable destination, a destination that is a file, files already in the way,
a missing SDK checkout for `--from-example`, a vendored template tree that will
not read -- each maps to an `issues[].code` and an exit code. This is the port's
recurring bug class: an uncaught exception replaces the envelope with a Python
traceback on stderr, and the extension parses stdout, so it renders NOTHING with
no error visible on either side. The catch-all at the bottom of `init` is the
backstop for the case nobody enumerated.

**`--preview` writes nothing.** Not one directory, not one file, not even the
`.alp/sdk-path` pin. It is checked BEFORE the overwrite guard, because a
read-only operation has nothing to be guarded against -- the Rust learned that
the hard way: the guard used to run first and reject a preview with
`init.would-overwrite` on a project with local edits, failing the one operation
that could safely have answered.

**Nothing but the envelope on stdout.** Human text goes to stderr in both
formats; the JSON envelope is the only thing ever written to stdout.

**The OS is not an option (I-01/I-02).** No `--os`, no `--backend`. A core's
runtime follows its Cortex class, so a scaffolded `board.yaml` never carries a
top-level `os:` key and nothing here can be asked to put one there.

NOT PORTED, and honest about it: the interactive prompts (a missing `--template`
/`--name`/`--destination` takes the non-interactive default -- `zephyr-app`,
empty, `.` -- exactly as the Rust does with no terminal attached).

**`--board-yaml` and `--cores` are real, not parse-only.** `--board-yaml`
(Alp Studio's "render this resolved board.yaml verbatim") swaps the planned
board.yaml for the caller's file byte-for-byte; a template with no board.yaml
to override is a hard error (`init.board-yaml-unsupported`) rather than a
silent no-op that drops the caller's file. `--cores` (heterogeneous
scaffolding) validates + splices companion cores and a default RPMsg channel
into the SAME board.yaml `plan_template_files` already planned -- see
`tan.core.scaffold.splice_companion_cores`. `--from-example` ignores both
`--som` and `--cores`: the example ships its own board.yaml.

**`--all`, `--target`, `--verbose`, `--quiet`, `--no-color` are accepted and
genuinely IGNORED here -- matching the oracle, not merely tolerated.** Every
one is a member of clap's `GlobalArgs` (`global = true`), so the real `tan
init --quiet`/`--verbose`/... parses, exits 0, AND lists every one of them in
`tan init --help` -- but `crates/tan-cli/src/commands/init/` never READS any
of the five (grep confirms: they appear only in `GlobalArgs` test-struct
literals, never in `init`'s own logic -- unlike `doctor`/`clean`, `init` calls
neither `style::render_report` nor `Theme::from_args`). Rejecting them here
would NOT be more honest, it would be wrong: it would make this port refuse
an argv the shipped binary accepts. Declared visible (not `hidden=True`,
unlike `clean_cmd.clean`'s equivalent block) so `tan init --help`'s TEXT
matches the oracle's too -- docs/cli.md tabulates all five as `tan init`
flags, and a hidden option renders identically to a missing one there.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import typer

from tan.commands.build_cmd import discover_sdk_root
from tan.core.scaffold import (
    DEFAULT_SOM_SKU,
    DEFAULT_TEMPLATE_ID,
    IOT_STARTER_SUPPORTED_SKU,
    TEMPLATE_IDS,
    CoresError,
    ExampleReadError,
    FileChange,
    PlannedFile,
    ScaffoldWriteError,
    TemplateDataError,
    collect_file_changes,
    is_plain_relative,
    parse_cores,
    plan_template_files,
    posix,
    read_example_tree,
    retarget_board_yaml_som,
    scaffold_tree_preview,
    sdk_pointer_json,
    splice_companion_cores,
    vendored_app_core_key,
    vendored_core_ids,
    write_files,
)
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode

#: `data.schemaVersion` for this command's payload.
DATA_SCHEMA_VERSION = "1"

#: `vendored_core_ids`'s `os` value is the raw `os:` child line text, quotes
#: and all (a vendored board.yaml spells an off companion `os: "off"`) --
#: stripped for the collision message so it reads `(os: off)`, not `(os:
#: "off")`. Mirrors Rust's `os.trim_matches('"')`.
DOUBLE_QUOTE = '"'


class InitError(Exception):
    """A failure with its issue code and exit code already decided.

    `partial` carries the files that DID land when a write failed part-way:
    `written: []` for a project that is actually half on disk leaves a consumer
    with no idea what to clean up or reopen.
    """

    def __init__(
        self,
        code: str,
        message: str,
        exit_code: ExitCode,
        *,
        partial: tuple[list[str], list[str]] = ([], []),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.partial = partial


@dataclass
class _Outcome:
    """A completed (non-error) run: preview, overwrite-guard refusal, or write."""

    template_id: str
    destination: str
    preview: bool
    file_changes: list[FileChange]
    files: list[PlannedFile]
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    sdk_pinned: str | None = None
    issues: list[Issue] = field(default_factory=list)
    exit_code: ExitCode = ExitCode.SUCCESS


# ---------------------------------------------------------------------------
# Envelope assembly
# ---------------------------------------------------------------------------


def _data(
    *,
    template_id: str,
    destination: str,
    preview: bool,
    file_changes: list[FileChange],
    written: list[str],
    unchanged: list[str],
    sdk_pinned: str | None,
) -> dict:
    return {
        "schemaVersion": DATA_SCHEMA_VERSION,
        "templateId": template_id,
        "destination": destination,
        "preview": preview,
        "fileChanges": [
            {"relativePath": c.relative_path, "kind": c.kind} for c in file_changes
        ],
        "written": written,
        "unchanged": unchanged,
        "sdkPinned": sdk_pinned,
    }


def _stderr(line: str) -> None:
    print(line, file=sys.stderr)


def _emit_error(json_mode: bool, err: InitError) -> None:
    """The error envelope. Note the asymmetry with `_emit_outcome`, which is
    contract, not an oversight: an error reports `project.root: null` and an
    EMPTY `templateId`/`destination` even when the CLI had already resolved
    both, because nothing was created and there is no project to point a
    consumer at. `contract/envelopes/init-invalid-template` pins it.
    """
    written, unchanged = err.partial
    if json_mode:
        emit(
            Envelope(
                "init",
                Project(root=None, board_yaml=None),
                _data(
                    template_id="",
                    destination="",
                    preview=False,
                    file_changes=[],
                    written=written,
                    unchanged=unchanged,
                    sdk_pinned=None,
                ),
                [Issue(err.code, "error", err.message)],
                err.exit_code,
            )
        )
    else:
        _stderr(f"init: {err.message}")
    raise typer.Exit(int(err.exit_code))


def _emit_outcome(json_mode: bool, outcome: _Outcome) -> None:
    if json_mode:
        emit(
            Envelope(
                "init",
                # The destination, not the nested project root: `project.root`
                # is where the caller pointed tan, and the golden pins `"."`.
                Project(root=outcome.destination, board_yaml=None),
                _data(
                    template_id=outcome.template_id,
                    destination=outcome.destination,
                    preview=outcome.preview,
                    file_changes=outcome.file_changes,
                    written=outcome.written,
                    unchanged=outcome.unchanged,
                    sdk_pinned=outcome.sdk_pinned,
                ),
                outcome.issues,
                outcome.exit_code,
            )
        )
    elif outcome.preview:
        _stderr(f"init: preview for template '{outcome.template_id}'")
        _stderr(scaffold_tree_preview(outcome.files).rstrip("\n"))
    elif outcome.issues:
        for issue in outcome.issues:
            _stderr(f"init: {issue.message}")
    else:
        _stderr(f"init: created '{outcome.destination}' from template '{outcome.template_id}'")
        _stderr(f"  written: {len(outcome.written)}, unchanged: {len(outcome.unchanged)}")
    raise typer.Exit(int(outcome.exit_code))


# ---------------------------------------------------------------------------
# Input resolution
# ---------------------------------------------------------------------------


def _resolve_template(template: str | None) -> str:
    if template is None:
        return DEFAULT_TEMPLATE_ID
    if template not in TEMPLATE_IDS:
        # Message pinned verbatim by `contract/envelopes/init-invalid-template`.
        # Do not append the valid-template list here: the golden diffs
        # `issues[].message` byte-for-byte, and a list that grows with the
        # registry would break it on every new template. `tan explain` is where
        # the catalogue is published.
        raise InitError(
            "init.invalid-template",
            f"Unknown template '{template}'.",
            ExitCode.VALIDATION_FAILURE,
        )
    return template


def _resolve_name(name: str | None) -> str:
    """`--name` names a SUBDIRECTORY under the destination; empty means scaffold
    straight into it. Guarded because this is the input that decides WHERE files
    land -- an unchecked `..`/absolute value put the project root (and, with
    `--force`, an overwrite target) anywhere the process can write."""
    if name is None:
        return ""
    if name == "" or is_plain_relative(name):
        return name
    raise InitError(
        "init.invalid-name",
        f"Invalid --name '{name}': must be a plain relative path (no '..', absolute, "
        f"or drive-rooted segments).",
        ExitCode.VALIDATION_FAILURE,
    )


@dataclass(frozen=True)
class _Sdk:
    """A resolved alp-sdk checkout: the `path` to use, and the `display` string
    to RECORD (`data.sdkPinned`, `.alp/sdk-path`).

    Two fields because they are not interchangeable: `Path("./sdk")` stringifies
    to `sdk`, dropping the `./` the caller typed, and Rust's `resolve_sdk_root`
    returns an explicit `--sdk-root` as-is. Same directory either way -- but the
    two binaries would write different bytes into the same pointer file, which is
    exactly the cross-language drift the conformance suite exists to catch.
    """

    path: Path
    display: str


def _resolve_sdk_root(sdk_root: str | None, workspace_root: Path) -> _Sdk | None:
    """`--sdk-root` when given, else discovery near the workspace. Reuses
    `build_cmd.discover_sdk_root` rather than carrying a second copy of the
    candidate ladder -- two ladders drift, and `tan build` resolving a different
    checkout than `tan init` pinned is the worst possible way to find out."""
    if sdk_root:
        return _Sdk(Path(sdk_root), sdk_root)
    found = discover_sdk_root(workspace_root)
    return _Sdk(found, posix(found)) if found is not None else None


def _is_sdk_checkout(root: Path) -> bool:
    """`scripts/alp_project.py` is THE marker for an alp-sdk checkout (I-31)."""
    return (root / "scripts" / "alp_project.py").is_file()


# ---------------------------------------------------------------------------
# The two planning paths
# ---------------------------------------------------------------------------


def _plan_from_template(
    template: str | None, som: str | None, cores_raw: str | None
) -> tuple[str, list[PlannedFile]]:
    template_id = _resolve_template(template)
    sku = som or DEFAULT_SOM_SKU
    # Checked BEFORE anything is planned: `iot-starter` vendors exactly one SoM
    # family (its Wi-Fi transport is silicon-validated on that SKU alone), so
    # any other `--som` must be refused, never quietly rendered against it.
    if template_id == "iot-starter" and sku != IOT_STARTER_SUPPORTED_SKU:
        raise InitError(
            "init.invalid-som",
            f"Template 'iot-starter' supports only SoM SKU "
            f"'{IOT_STARTER_SUPPORTED_SKU}'; got '{sku}'.",
            ExitCode.VALIDATION_FAILURE,
        )
    try:
        files = plan_template_files(template_id, sku)
    except TemplateDataError as err:
        # tan's own template data is unreadable -- a broken installation (or a
        # frozen binary built without the template `--add-data`), not a project
        # problem, so INTERNAL_FAILURE rather than a validation code that would
        # send the customer looking at their own board.yaml.
        raise InitError(
            "init.template-unreadable", str(err), ExitCode.INTERNAL_FAILURE
        ) from err

    try:
        cores = parse_cores(cores_raw)
    except CoresError as err:
        raise InitError(
            "init.invalid-cores", err.message, ExitCode.VALIDATION_FAILURE
        ) from err
    if cores:
        files = _apply_cores(template_id, files, cores)
    return template_id, files


def _apply_cores(
    template_id: str, files: list[PlannedFile], cores: list[tuple[str, str]]
) -> list[PlannedFile]:
    """Validate `--cores` against the plan's OWN board.yaml, then splice
    companion cores (+ a default RPMsg channel) into it. Reads the app core
    and any pre-declared companions straight off the PLANNED board.yaml
    (`vendored_app_core_key`/`vendored_core_ids`) rather than a second,
    independently-derived per-template lookup, so this check can never
    disagree with what actually gets spliced -- the exact drift the Rust
    port guards against with its `vendored_*_app_core_for_sku` family.
    A plan with no board.yaml (or no `cores:` block) has nothing to validate
    or splice against; every registered template plans one today."""
    board = next((f for f in files if f.relative_path == "board.yaml"), None)
    if board is None:
        return files
    app_core = vendored_app_core_key(board.content)
    if app_core is None:
        return files

    # The app core's runtime is fixed (the scaffolded src/ + prj.conf are
    # Zephyr); reject a contradictory --cores request instead of silently
    # overriding it.
    for core_id, os_value in cores:
        if core_id == app_core and os_value != "zephyr":
            raise InitError(
                "init.invalid-cores",
                f"Core '{app_core}' is this SoM's app core and runs zephyr; "
                f"--cores requested '{os_value}'. Omit the entry or use "
                f"{app_core}:zephyr.",
                ExitCode.VALIDATION_FAILURE,
            )

    # A vendored scaffold may already pre-declare a companion core (e.g.
    # edge-ai ships `a55_cluster`, os: "off") -- a --cores id colliding with
    # one would append a SECOND, duplicate `cores:` mapping key. Reject
    # rather than silently drop the caller's os or silently override the
    # scaffold's.
    existing = dict(vendored_core_ids(board.content))
    for core_id, _os_value in cores:
        if core_id != app_core and core_id in existing:
            raise InitError(
                "init.invalid-cores",
                f"Core '{core_id}' is already declared by the {template_id} "
                f"scaffold (os: {existing[core_id].strip(DOUBLE_QUOTE)}); edit "
                f"board.yaml to change its os instead of passing --cores.",
                ExitCode.VALIDATION_FAILURE,
            )

    spliced = splice_companion_cores(board.content, cores)
    return [
        PlannedFile("board.yaml", spliced) if f.relative_path == "board.yaml" else f
        for f in files
    ]


def _apply_board_yaml_override(
    files: list[PlannedFile], subject_label: str, board_yaml_path: str | None
) -> list[PlannedFile]:
    """Honor `--board-yaml`: swap the planned board.yaml for the caller's file
    verbatim -- this lets Alp Studio adopt `tan init` as its project render.
    Shared by the template and `--from-example` paths (mirrors `init/mod.rs`
    step 5b and `init/from_example.rs`'s identical block). A plan with no
    board.yaml to override (none exists today) is a hard error rather than a
    silent no-op that would drop the caller's file."""
    if board_yaml_path is None:
        return files
    try:
        with open(board_yaml_path, encoding="utf-8", newline="") as handle:
            content = handle.read()
    except (OSError, ValueError) as err:
        raise InitError(
            "init.board-yaml-unreadable",
            f"--board-yaml '{board_yaml_path}' could not be read: {err}",
            ExitCode.VALIDATION_FAILURE,
        ) from err
    if not any(f.relative_path == "board.yaml" for f in files):
        raise InitError(
            "init.board-yaml-unsupported",
            f"--board-yaml was given but {subject_label} has no board.yaml to override.",
            ExitCode.VALIDATION_FAILURE,
        )
    return [
        PlannedFile("board.yaml", content) if f.relative_path == "board.yaml" else f
        for f in files
    ]


def _plan_from_example(
    src: str, som: str | None, sdk: _Sdk | None
) -> tuple[str, list[PlannedFile]]:
    """Copy an SDK example directory verbatim. The ONE init path that needs a
    checkout, because the thing it copies lives in one."""
    src = src.strip()
    if not src:
        raise InitError(
            "init.invalid-example",
            "--from-example requires a non-empty example source directory.",
            ExitCode.VALIDATION_FAILURE,
        )
    if not is_plain_relative(src):
        raise InitError(
            "init.invalid-example",
            f"Invalid example '{src}': must be a relative path under the SDK "
            f"examples/ directory.",
            ExitCode.VALIDATION_FAILURE,
        )
    if sdk is None or not _is_sdk_checkout(sdk.path):
        raise InitError(
            "init.sdk-root-unresolved",
            "alp-sdk root is unresolved. Use --sdk-root or run near an alp-sdk "
            "checkout to copy an example."
            + (f" (tried '{sdk.display}')" if sdk is not None else ""),
            ExitCode.VALIDATION_FAILURE,
        )

    examples_root = sdk.path / "examples"
    example_dir = examples_root / src
    # Containment guard on the RESOLVED paths, on top of the lexical check
    # above: it is what defeats a directory-symlink escape, which no amount of
    # string inspection can see.
    try:
        contained = example_dir.resolve(strict=True).is_relative_to(
            examples_root.resolve(strict=True)
        )
    except OSError:
        contained = False
    if not contained:
        raise InitError(
            "init.example-not-found",
            f"Example '{src}' was not found under the SDK examples/ directory.",
            ExitCode.VALIDATION_FAILURE,
        )

    try:
        files = read_example_tree(example_dir)
    except ExampleReadError as err:
        if err.not_found:
            raise InitError(
                "init.example-not-found",
                f"Example '{src}' was not found under the SDK examples/ directory.",
                ExitCode.VALIDATION_FAILURE,
            ) from err
        raise InitError(
            "init.example-unreadable",
            f"Example '{src}' could not be read: {err}",
            ExitCode.RUNTIME_FAILURE,
        ) from err
    if not files:
        raise InitError(
            "init.example-not-found",
            f"Example '{src}' contains no files to copy.",
            ExitCode.VALIDATION_FAILURE,
        )

    if som:
        # Retarget the copied board.yaml onto the chosen SoM, so an example can
        # be scaffolded onto the customer's own module rather than the example's.
        files = [
            PlannedFile(f.relative_path, retarget_board_yaml_som(f.content, som))
            if f.relative_path == "board.yaml"
            else f
            for f in files
        ]
    return f"example:{src}", files


# ---------------------------------------------------------------------------
# Diff, then preview / guard / write
# ---------------------------------------------------------------------------


def _finish(
    template_id: str,
    destination: str,
    project_root: Path,
    files: list[PlannedFile],
    *,
    preview: bool,
    force: bool,
    sdk: _Sdk | None,
) -> _Outcome:
    changes = collect_file_changes(project_root, files)

    if preview:
        # Before the overwrite guard, deliberately: a preview touches no disk,
        # so there is nothing to guard, and guarding it turned a read-only
        # question into a failure on any project with local edits.
        return _Outcome(template_id, destination, True, changes, files)

    if any(c.kind == "update" for c in changes) and not force:
        return _Outcome(
            template_id,
            destination,
            False,
            changes,
            files,
            issues=[
                Issue(
                    "init.would-overwrite",
                    "error",
                    "One or more files would be overwritten. Use --force to allow updates.",
                )
            ],
            exit_code=ExitCode.WRITE_FAILURE,
        )

    # A destination that exists as a FILE would otherwise surface as an
    # errno-flavoured mkdir/write failure a few frames later, naming a path
    # inside a file. Say it plainly, and before anything is written.
    if project_root.exists() and not project_root.is_dir():
        raise InitError(
            "init.write-failed",
            f"Destination '{posix(project_root)}' exists and is not a directory.",
            ExitCode.WRITE_FAILURE,
        )

    try:
        result = write_files(project_root, files)
    except ScaffoldWriteError as err:
        raise InitError(
            "init.write-failed",
            f"Failed to write files: {err}",
            ExitCode.WRITE_FAILURE,
            partial=(err.partial.written, err.partial.unchanged),
        ) from err

    return _Outcome(
        template_id,
        destination,
        False,
        changes,
        files,
        written=result.written,
        unchanged=result.unchanged,
        sdk_pinned=_pin_sdk(project_root, sdk),
    )


def _pin_sdk(project_root: Path, sdk: _Sdk | None) -> str | None:
    """Record the resolved SDK in `<project>/.alp/sdk-path` so the new project is
    reproducible without a separate `tan sdk switch`.

    A `None`, a path that is not a real checkout, and a failed write are all a
    silent skip -- never a reason to fail a `tan init` whose files already
    landed. Reached only after the write, so `--preview` can never trip it.
    """
    if sdk is None or not _is_sdk_checkout(sdk.path):
        return None
    sdk_path = sdk.display
    try:
        pointer = project_root / ".alp" / "sdk-path"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        with pointer.open("w", encoding="utf-8", newline="") as handle:
            handle.write(sdk_pointer_json(sdk_path))
    except OSError:
        return None
    return sdk_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def init(
    template: str = typer.Option(
        None,
        "--template",
        metavar="ID",
        help=f"Project template: {', '.join(TEMPLATE_IDS)}.",
    ),
    from_example: str = typer.Option(
        None,
        "--from-example",
        metavar="DIR",
        help="Copy an SDK example (e.g. peripheral-io/hello-world) instead of a template.",
    ),
    name: str = typer.Option(
        None, "--name", metavar="NAME", help="Subdirectory to create under the destination."
    ),
    destination: str = typer.Option(
        None, "--destination", metavar="PATH", help="Destination directory (defaults to '.')."
    ),
    som: str = typer.Option(
        None, "--som", metavar="SKU", help="SoM SKU to target in the generated board.yaml."
    ),
    board_yaml: str = typer.Option(
        None,
        "--board-yaml",
        metavar="PATH",
        help="Render this file verbatim as the scaffolded board.yaml instead of the planned one.",
    ),
    cores: str = typer.Option(
        None,
        "--cores",
        metavar="CORES",
        help=(
            "Comma-separated cores for a heterogeneous project, `id[:os]` "
            "(e.g. `m33_sm:zephyr,a55_cluster:yocto`); OS is inferred from "
            "the id when omitted."
        ),
    ),
    preview: bool = typer.Option(
        False, "--preview", help="Show the planned files and write nothing."
    ),
    force: bool = typer.Option(
        False, "--force", help="Allow overwriting files that already exist."
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    sdk_root: str = typer.Option(
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    output_format: str = typer.Option(
        "text", "--format", metavar="FORMAT", help="Output format: text or json."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="Emit additional diagnostic detail."
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress non-essential output."),
    no_color: bool = typer.Option(
        False, "--no-color", help="Disable ANSI color in text output."
    ),
    target: str = typer.Option(
        None,
        "--target",
        metavar="EMIT",
        help="Generation target (e.g. zephyr-conf, dts-overlay, cmake-args, yocto-conf).",
    ),
    all_targets: bool = typer.Option(
        False, "--all", help="Run command against all relevant targets."
    ),
) -> None:
    """Scaffold a new project from a template or an SDK example."""
    # The five options above are `clap`'s `GlobalArgs` members that
    # `crates/tan-cli/src/commands/init/` accepts and never reads (see the
    # module docstring) -- declared here (NOT `hidden=True`, unlike
    # `clean_cmd.clean`'s equivalent block) because the real `tan init --help`
    # DOES list all five: they are `global = true` in clap, so every
    # subcommand's help renders them whether or not that subcommand's own
    # code reads them. Hiding them here would parse identically but make
    # `tan init --help`'s TEXT diverge from the oracle's.
    del verbose, quiet, no_color, target, all_targets
    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = output_format == "json"

    try:
        # `--destination`, then the global `--project`, then `.`. `--name` does
        # NOT answer this: it names a subdirectory INSIDE the destination, so
        # "which directory does my-app go in" is still a real question with
        # `--name` set.
        dest = destination if destination else (project if project else ".")
        resolved_name = _resolve_name(name)
        # String-joined, not `Path(".") / name`: pathlib normalises `.` away,
        # and the destination is echoed back verbatim in the envelope.
        project_root = Path(dest) / resolved_name if resolved_name else Path(dest)

        workspace_root = Path(os.path.abspath(project)) if project else Path.cwd()
        resolved_sdk = _resolve_sdk_root(sdk_root, workspace_root)

        if from_example is not None:
            template_id, files = _plan_from_example(from_example, som, resolved_sdk)
            # `--cores` is ignored on this path (the example ships its own
            # board.yaml); `--board-yaml`'s subject names the example, not a
            # template id -- `template_id` here is `"example:<src>"`.
            subject_label = f"example '{template_id[len('example:') :]}'"
        else:
            template_id, files = _plan_from_template(template, som, cores)
            subject_label = f"template '{template_id}'"

        files = _apply_board_yaml_override(files, subject_label, board_yaml)

        outcome = _finish(
            template_id,
            dest,
            project_root,
            files,
            preview=preview,
            force=force,
            sdk=resolved_sdk,
        )
    except InitError as err:
        _emit_error(json_mode, err)
        return
    except Exception as err:  # noqa: BLE001 -- the backstop; see the module docstring
        # Nothing gets to replace the envelope with a traceback. `typer.Exit`
        # cannot reach here: it is only ever raised from the emit helpers, which
        # run outside this try (or from within the handler above, which
        # propagates).
        _emit_error(
            json_mode,
            InitError(
                "init.internal-failure",
                f"init failed unexpectedly: {err.__class__.__name__}: {err}",
                ExitCode.INTERNAL_FAILURE,
            ),
        )
        return

    _emit_outcome(json_mode, outcome)
