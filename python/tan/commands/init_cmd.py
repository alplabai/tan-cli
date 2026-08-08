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

**`--template`'s id space is NOT the SDK's example catalog.** `TEMPLATE_IDS`
(`tan.core.scaffold`) is tan's own curated, vendored subset -- six starter
templates, five of which map onto five entries of the broader SDK catalog
(`metadata/templates/catalog-v1.json`, alp-sdk) under DIFFERENT ids (its
`minimal`/`sensor`/`iot`/`edge-ai`/`diagnostics` are this file's
`zephyr-app`/`sensor-starter`/`iot-starter`/`edge-ai-starter`/
`board-diagnostics`); `minimal-app` has no catalog counterpart at all, and the
catalog's `peripheral`/`multicore-rpmsg`/`gateway` entries have no `--template`
counterpart here -- reach them (and any other SDK example) with
`--from-example` instead, which copies the example's own tree verbatim
(I-32: no vendoring needed, but an SDK checkout is). Deliberately different
vocabularies, not a bug to "fix" by renaming one to match the other. The
`--template` help STRING is the one place this port knowingly diverges from
the oracle's own wording (tan-cli#1): the oracle's clap help is silent on the
catalog split, and a customer hitting it (e.g. trying `--template peripheral`)
gets no pointer to `--from-example` -- so the help text here says more than
`crates/tan-cli/src/cli.rs`'s does, on purpose, tracked, and pinned by
`test_help_distinguishes_template_ids_from_the_sdk_example_catalog`. This is
NOT the same claim as the paragraph below: that one is about which FLAGS are
listed at all, not about every flag's help wording matching byte-for-byte.

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
unlike `clean_cmd.clean`'s equivalent block) so `tan init --help` LISTS all
five, matching the oracle -- docs/cli.md tabulates all five as `tan init`
flags, and a hidden option renders identically to a missing one there. (Their
own help STRINGS are still free to be tan's plain-English wording rather than
clap's doc-comment text; only their presence is the contract.)
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import typer

from tan.commands.build_cmd import resolve_sdk_root_wide, sdk_ladder_divergence_issue
from tan.commands.sdk_cmd import global_default_foreign_project_issue
from tan.core.fs_confine import PathEscapeError, resolve_confined
from tan.core.global_flags import accept_global_flags
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
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat

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
    """A completed (non-error) run: preview, overwrite-guard refusal, or write.

    `destination` and `project_root` are DELIBERATELY two different fields:
    `destination` is where the caller pointed tan (`--destination`/`--project`,
    unchanged by `--name`) and is what the JSON envelope's `project.root` /
    `data.destination` pin -- `project_root` is `destination` + the resolved
    `--name` subdirectory, i.e. where files actually landed. Text mode's
    "created" line must report `project_root`: reporting `destination` there
    (tan-cli#4) told an operator running `--name blink-e2e` that tan had
    created `proj` when it had actually created `proj/blink-e2e`.
    """

    template_id: str
    destination: str
    project_root: str
    preview: bool
    file_changes: list[FileChange]
    files: list[PlannedFile]
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    sdk_pinned: str | None = None
    #: The resolved SDK `_finish` was called with, carried through so
    #: `_emit_outcome` can report `sdk.{root,sourceTier}` on the wire
    #: (tan-cli#491) -- distinct from `sdk_pinned`, which is only the string
    #: that landed in `.alp/sdk-path` (`None` on every path that never wrote,
    #: including a successful run whose checkout was not a real alp-sdk).
    sdk: "_Sdk | None" = None
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


def _sdk_info(sdk: "_Sdk | None") -> SdkInfo | None:
    """`_Sdk` -> the wire shape `Envelope(sdk=...)` takes, or `None` when
    nothing was resolved yet (tan-cli#491). The one seam every `init`
    envelope-emitting site now goes through, rather than five call sites each
    re-deriving `SdkInfo(sdk.display, sdk.tier)` -- `examples_cmd.py`'s
    `examples()` already carries this exact conversion inline at its own one
    call site; `init` has five (`_emit_outcome`, and `_emit_error` from both
    of `init()`'s except arms), so a shared helper is what keeps them from
    drifting the way `init` simply never doing it at all was allowed to."""
    return SdkInfo(sdk.display, sdk.tier) if sdk is not None else None


def _emit_error(json_mode: bool, err: InitError, sdk: "_Sdk | None" = None) -> None:
    """The error envelope. Note the asymmetry with `_emit_outcome`, which is
    contract, not an oversight: an error reports `project.root: null` and an
    EMPTY `templateId`/`destination` even when the CLI had already resolved
    both, because nothing was created and there is no project to point a
    consumer at. `contract/envelopes/init-invalid-template` pins it.

    `sdk` (tan-cli#491) is NOT similarly nulled: which alp-sdk checkout a
    refusal was evaluated against is exactly the fact a caller needs to
    retry sensibly, and the oracle reports it on every refusal path that
    resolved one. `None` here only when the SDK genuinely was never resolved
    -- an error raised before `_resolve_sdk_root` ever ran (e.g.
    `--name`/`--template` validation).
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
                sdk=_sdk_info(sdk),
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
                sdk=_sdk_info(outcome.sdk),
            )
        )
    elif outcome.preview:
        _stderr(f"init: preview for template '{outcome.template_id}'")
        for issue in outcome.issues:
            _stderr(f"init: {issue.message}")
        _stderr(scaffold_tree_preview(outcome.files).rstrip("\n"))
    elif outcome.exit_code != ExitCode.SUCCESS:
        # Refused (the overwrite guard): nothing was written, so there is no
        # "created" line to print, only the issue(s) that explain why.
        for issue in outcome.issues:
            _stderr(f"init: {issue.message}")
    else:
        # A successful write may still carry a warning (e.g.
        # `init.example-missing-board-yaml`) -- print it, then the "created"
        # line: files DID land, so unlike the refusal branch above this must
        # never suppress it.
        for issue in outcome.issues:
            _stderr(f"init: {issue.message}")
        # `project_root`, not `destination`: see `_Outcome`'s docstring.
        _stderr(f"init: created '{outcome.project_root}' from template '{outcome.template_id}'")
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


def _join_display(dest: str, name: str) -> str:
    """Mirror `PathBuf::from(dest).join(name)`'s `.display()` text
    (`from_example.rs:70-74`): literal string concatenation with the native
    separator, never pathlib's silent drop of a leading `.` component. This is
    for the TEXT-mode "created" line only -- `project_root` (the `Path`) still
    does the real filesystem work; `pathlib.Path(".") / "my-app"` normalises
    away the `.`, so `--name my-app` in `.` would otherwise print `'my-app'`
    where the oracle prints `'./my-app'` (`'.\\my-app'` on Windows)."""
    if not name:
        return dest
    if dest.endswith(("/", "\\")):
        return f"{dest}{name}"
    return f"{dest}{os.sep}{name}"


@dataclass(frozen=True)
class _Sdk:
    """A resolved alp-sdk checkout: the `path` to use, and the `display` string
    to RECORD (`data.sdkPinned`, `.alp/sdk-path`) -- always the SAME absolute
    directory, `path` as a `Path` for filesystem calls and `display` as its
    posix-separator string for the wire/pointer file.

    Both branches of [`_resolve_sdk_root`] build this from an already-absolute
    path (an explicit `--sdk-root` is anchored against the real cwd; the
    resolved-ladder branch was absolute already, being built from
    `workspace_root`). tan-cli#263: an EARLIER revision recorded an explicit
    `--sdk-root` verbatim -- `.\\alp-sdk` stayed `.\\alp-sdk` -- on the theory
    that the oracle's `resolve_sdk_root` also returns the flag as-is and the two
    binaries must write identical bytes into the same pointer file. That theory
    only held for the value's IN-PROCESS use (resolved against THIS invocation's
    cwd, in THIS run); persisted into `.alp/sdk-path`, the same relative string
    is read back later by an unrelated invocation with a DIFFERENT cwd (a
    `tan sdk current` run from inside the new project itself, typically), where
    it silently resolves to the wrong directory or nowhere at all -- exactly the
    layout the maintainer hit (`--sdk-root .\\alp-sdk` from the parent, read back
    from the child). A persisted pointer has no cwd of its own to inherit, so it
    must be self-contained: absolute. (A project-root-relative form was also
    considered -- `.alp/sdk-path` lives inside the project it describes, and
    that would survive the project being moved as a whole. Rejected here because
    nothing on the READ side re-anchors a relative pointer against the project
    root today -- `_pointer_target`/`_has_loader_script` hand it straight to
    `Path()`, which is exactly the cwd-dependent behaviour this fix removes --
    and because absolute matches what every OTHER tier already stores, with no
    reader change required.)
    """

    path: Path
    display: str
    #: `SdkRootResolution.tier` carried through (tan-cli#491): `"sdkRootFlag"`
    #: on the `--sdk-root` branch, else whatever tier `resolve_sdk_root_wide`
    #: actually resolved through (`projectPin` / `globalDefault` /
    #: `discovery`). Reported on the wire as `sdk.sourceTier` -- see
    #: `_sdk_info` below for why this was missing entirely before: `init`
    #: never passed `sdk=` to `Envelope(...)` at all, on any of its four
    #: outcome paths, so the field was silently absent rather than merely
    #: wrong.
    tier: str
    #: `SdkRootResolution.foreign_global_default_for` carried through
    #: (tan-cli#464). `None` on the `--sdk-root` branch (an explicit flag is
    #: never "foreign" -- there is nothing to fall through from) and on every
    #: tier above `globalDefault`; set only when THIS run resolved through the
    #: machine-global `~/.alp/sdk-default` pointer and it was last written for
    #: a DIFFERENT project. `init` is the command this fact matters most for
    #: (more than `tan build`'s own tan-cli#464 fix): a pin written here is
    #: PERMANENT, so silently baking a foreign checkout's path into a brand
    #: new project's `.alp/sdk-path` is worse than one build silently using
    #: the wrong SDK once -- every later command in the new project inherits
    #: it until someone notices and re-pins by hand.
    foreign_global_default_for: str | None = None


def _resolve_sdk_root(sdk_root: str | None, workspace_root: Path) -> _Sdk | None:
    """`--sdk-root` when given, else `.alp/sdk-path` project pin >
    machine-global default (`~/.alp/sdk-default`) > the WIDE positional walk.
    No `ALP_SDK_ROOT` tier (tried and reverted -- see
    `resolve_sdk_root_ladder`'s own docstring).

    `build_cmd.resolve_sdk_root_wide`, not the narrow ladder its thirteen
    sibling commands take (tan-cli#263, measured against the oracle): where a
    workspace holds both a child `<ws>/alp-sdk` and a competing sibling
    `../alp-sdk`, the oracle's `init` pins the CHILD. The narrow ladder pinned
    the sibling -- and `init` does not merely resolve an SDK, it WRITES the
    answer to `.alp/sdk-path`, which then outranks discovery for every later
    command in that project. Getting it wrong here binds the wrong checkout
    permanently, so this one call site is worth a second helper.

    An explicit `--sdk-root` is expanded (`~`/`~user`) then anchored against
    the process's real cwd with `os.path.abspath` -- lexical only (matching
    `build_cmd._abs_posix`'s own reasoning), never `Path.resolve()`, so a
    project reached through a symlink keeps the name the user typed and a
    not-yet-existing path still resolves. `expanduser` first: `abspath` alone
    does not expand `~`, so `--sdk-root ~/alp-sdk` would otherwise anchor to
    `<cwd>/~/alp-sdk` -- a pre-existing gap, but persisting the wrong path into
    `.alp/sdk-path` (rather than just misusing it for this one invocation)
    makes it worth closing here. Anchored against the true cwd, not
    `workspace_root`: `--project` picks which workspace `init` operates ON, it
    does not rebase every OTHER relative CLI argument onto it, and no sibling
    option in this command does that either."""
    if sdk_root:
        resolved = Path(os.path.abspath(os.path.expanduser(sdk_root)))
        return _Sdk(resolved, posix(resolved), "sdkRootFlag")
    # `_broken_pin` unused: `init` is what WRITES `.alp/sdk-path`, so a broken
    # pin already sitting in the parent workspace is superseded by this run's
    # own resolution rather than something for `init` itself to disclose --
    # `sdk current`/`build`/`doctor` (tan-cli#263 review) are what customers
    # see it through after the fact. `foreign_global_default_for` is NOT
    # dropped the same way (tan-cli#464 review): unlike a broken pin, which
    # this run's own resolution supersedes, a foreign `globalDefault` is
    # exactly what THIS run is about to resolve through and permanently pin
    # -- `init()` surfaces it via `global_default_foreign_project_issue`
    # before `_pin_sdk` ever writes.
    resolution = resolve_sdk_root_wide(None, workspace_root)
    found = resolution.path
    if found is None:
        return None
    return _Sdk(found, posix(found), resolution.tier, resolution.foreign_global_default_for)


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
    files: list[PlannedFile],
    subject_label: str,
    board_yaml_path: str | None,
    *,
    allow_add: bool = False,
) -> list[PlannedFile]:
    """Honor `--board-yaml`: swap the planned board.yaml for the caller's file
    verbatim -- this lets Alp Studio adopt `tan init` as its project render.
    Shared by the template and `--from-example` paths (mirrors `init/mod.rs`
    step 5b and `init/from_example.rs`'s identical block).

    `allow_add` (set only on the `--from-example` path) lets this ADD the
    caller's file as the project's board.yaml when the example plans none --
    the real escape hatch the `init.example-missing-board-yaml` warning names.
    Every registered TEMPLATE plans its own board.yaml, so a plan with none on
    the template path is still a hard error rather than a silent no-op that
    would drop the caller's file."""
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
        if allow_add:
            return [*files, PlannedFile("board.yaml", content)]
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


def overwrite_refusal_message(changes: list[FileChange]) -> str:
    """Name the files that actually collide, and offer the non-destructive
    option first. `--force` is the only escape the old wording named, which
    made overwriting look like the intended next step rather than the last
    resort."""
    colliding = [c.relative_path for c in changes if c.kind == "update"]
    listed = ", ".join(sorted(colliding))
    return (
        f"{len(colliding)} file(s) would be overwritten: {listed}. "
        "Run with --preview to see the full plan, --destination <DIR> or "
        "--name <NAME> to write somewhere else, or --force to overwrite."
    )


def _finish(
    template_id: str,
    destination: str,
    project_root: Path,
    project_root_display: str,
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
        return _Outcome(
            template_id=template_id,
            destination=destination,
            project_root=project_root_display,
            preview=True,
            file_changes=changes,
            files=files,
            sdk=sdk,
        )

    if any(c.kind == "update" for c in changes) and not force:
        return _Outcome(
            template_id=template_id,
            destination=destination,
            project_root=project_root_display,
            preview=False,
            file_changes=changes,
            files=files,
            issues=[
                Issue(
                    "init.would-overwrite",
                    "error",
                    overwrite_refusal_message(changes),
                )
            ],
            exit_code=ExitCode.WRITE_FAILURE,
            sdk=sdk,
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

    try:
        sdk_pinned = _pin_sdk(project_root, sdk)
    except (PathEscapeError, OSError, ValueError) as err:
        # The project's own files already landed (`result` above); `partial`
        # carries them so a consumer knows what to clean up rather than
        # reading `written: []` for a project that is really half on disk --
        # same reasoning as the `ScaffoldWriteError` branch just above.
        raise InitError(
            "init.write-failed",
            f"refusing to pin the SDK: {err}",
            ExitCode.WRITE_FAILURE,
            partial=(result.written, result.unchanged),
        ) from err

    return _Outcome(
        template_id=template_id,
        destination=destination,
        project_root=project_root_display,
        preview=False,
        file_changes=changes,
        files=files,
        written=result.written,
        unchanged=result.unchanged,
        sdk_pinned=sdk_pinned,
        sdk=sdk,
    )


def _pin_sdk(project_root: Path, sdk: _Sdk | None) -> str | None:
    """Record the resolved SDK in `<project>/.alp/sdk-path` so the new project is
    reproducible without a separate `tan sdk switch`.

    `None` and "not a real checkout" are a silent skip -- an optional
    convenience pointer is never a reason to fail a `tan init` whose real
    files already landed. Reached only after the write, so `--preview` can
    never trip it.

    A confinement ESCAPE is different: a pre-existing symlinked (or
    junctioned) `.alp`, or a symlinked parent of it, used to carry this write
    to wherever that link pointed while still reporting the logical
    `.alp/sdk-path` as pinned (tan-cli#325, the same defect `scaffold.py`'s
    `write_files` and `generate_cmd`'s default outputs already close). Checked
    with the SAME shared guard those two use (`tan.core.fs_confine`) before
    anything is written, and raised rather than swallowed -- `_finish` turns
    it into a coded `init.write-failed` refusal instead of a silent
    `sdkPinned: null` that would hide bytes landing outside the project. Any
    OTHER write failure (permission denied, a full disk) still returns `None`:
    it is a real filesystem problem, not an escape, and the existing
    silent-skip contract for those is unchanged.
    """
    if sdk is None or not _is_sdk_checkout(sdk.path):
        return None
    sdk_path = sdk.display
    pointer = resolve_confined(project_root, project_root / ".alp" / "sdk-path")
    try:
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
        help=(
            f"Project template: {', '.join(TEMPLATE_IDS)}. tan's own curated "
            f"starter set -- a different, smaller id space than the SDK's "
            f"example catalog; use --from-example for any other SDK example "
            f"(e.g. peripheral-io/gpio-button-led for a button+LED starter)."
        ),
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
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format", help=FORMAT_HELP),
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
    json_mode = output_format == "json"
    # Bound before the try so an `InitError`/`Exception` raised BEFORE the SDK
    # is resolved (`_resolve_name`, e.g.) still has a defined `resolved_sdk`
    # to pass `_emit_error` -- `None`, honestly, since nothing was resolved at
    # that point. Once `_resolve_sdk_root` below runs, every later refusal on
    # this same call carries whatever it found (tan-cli#491).
    resolved_sdk: _Sdk | None = None

    try:
        # `--destination`, then the global `--project`, then `.`. `--name` does
        # NOT answer this: it names a subdirectory INSIDE the destination, so
        # "which directory does my-app go in" is still a real question with
        # `--name` set.
        dest = destination if destination else (project if project else ".")
        resolved_name = _resolve_name(name)
        # `project_root` (the `Path`) does the real filesystem work, so it goes
        # through pathlib; `project_root_display` is the TEXT-mode "created"
        # line's own literal string join (`_join_display`), because pathlib's
        # `Path(dest) / name` silently drops a leading `.` from `dest` that the
        # oracle's `PathBuf::join` keeps (`tan-cli` review, scaffold-cx#5). The
        # destination itself is echoed back verbatim (unjoined) in the envelope.
        project_root = Path(dest) / resolved_name if resolved_name else Path(dest)
        project_root_display = _join_display(dest, resolved_name)

        workspace_root = Path(os.path.abspath(project)) if project else Path.cwd()
        resolved_sdk = _resolve_sdk_root(sdk_root, workspace_root)
        # tan-cli#407, and this is the command where it matters most: `init`
        # does not merely resolve an SDK, it WRITES the answer to
        # `.alp/sdk-path` (`_record_sdk_pin`, below), and that pointer then
        # outranks BOTH discovery tiers for every later command in the new
        # project. So the collision is settled here, permanently, in the user's
        # favour or against it -- and the wide ladder settles it on the CHILD
        # `<ws>/alp-sdk` while thirteen commands would have taken the lateral
        # `../alp-sdk`. Disclosing which one got bound is the whole point:
        # after this run the divergence is genuinely GONE (both ladders read
        # the pin), so this is the last moment the fact is observable at all.
        # Computed BEFORE `_finish` for exactly that reason.
        divergence_issue = sdk_ladder_divergence_issue(sdk_root, workspace_root, wide=True)
        # tan-cli#464 review: `init` is the command a foreign `globalDefault`
        # hurts most of all -- `_pin_sdk` below is about to bake whatever
        # `resolved_sdk` names into `<project>/.alp/sdk-path` PERMANENTLY, so
        # a caller resolving a checkout a DIFFERENT project's bootstrap
        # relocation actually wrote must be told before that pin lands, not
        # after. Computed here, alongside `divergence_issue`, for the same
        # reason: this is the last moment the fact is cheap to disclose --
        # once pinned, every later command in the new project reads the pin
        # tier instead and this warning never fires again.
        foreign_issue = global_default_foreign_project_issue(
            resolved_sdk.foreign_global_default_for if resolved_sdk is not None else None
        )

        if from_example is not None:
            template_id, files = _plan_from_example(from_example, som, resolved_sdk)
            # `--cores` is ignored on this path (the example ships its own
            # board.yaml); `--board-yaml`'s subject names the example, not a
            # template id -- `template_id` here is `"example:<src>"`.
            subject_label = f"example '{template_id[len('example:') :]}'"
        else:
            template_id, files = _plan_from_template(template, som, cores)
            subject_label = f"template '{template_id}'"

        files = _apply_board_yaml_override(
            files, subject_label, board_yaml, allow_add=from_example is not None
        )

        # `tan build` discovers a project's board.yaml at its root; an example
        # tree with none would otherwise succeed here and only fail two
        # commands later at `tan build` ("no board.yaml found"). WARN at
        # scaffold time instead of refusing the copy outright: 57 of 66
        # examples/aen/* (raw west/twister examples, not meant for the `tan
        # build` flow) have no board.yaml, and refusing `tan init
        # --from-example` for nearly the whole AEN family is worse than
        # scaffolding something that still needs one. `--board-yaml` (above,
        # `allow_add=True` on this path) is the real fix, so it never fires
        # when one was given. Every registered TEMPLATE plans its own
        # board.yaml (tan.core.scaffold), so this can only trip on
        # --from-example.
        missing_board_yaml_issue = None
        if from_example is not None and not any(f.relative_path == "board.yaml" for f in files):
            missing_board_yaml_issue = Issue(
                "init.example-missing-board-yaml",
                "warning",
                f"{subject_label} has no board.yaml, so `tan build` will not "
                f"find a board to build here; pass --board-yaml to add one.",
            )

        outcome = _finish(
            template_id,
            dest,
            project_root,
            project_root_display,
            files,
            preview=preview,
            force=force,
            sdk=resolved_sdk,
        )
        if missing_board_yaml_issue is not None:
            outcome.issues.append(missing_board_yaml_issue)
        if divergence_issue is not None:
            outcome.issues.append(divergence_issue)
        if foreign_issue is not None:
            outcome.issues.append(foreign_issue)
    except InitError as err:
        _emit_error(json_mode, err, resolved_sdk)
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
            resolved_sdk,
        )
        return

    _emit_outcome(json_mode, outcome)


# tan-cli#261: adds the two oracle `GlobalArgs` flags this command was still
# missing (`--ci`/`--non-interactive`) on top of the five already declared
# above (`--verbose`/`--quiet`/`--no-color`/`--target`/`--all`, all read for
# real); see `tan.core.global_flags`.
init = accept_global_flags(init)
