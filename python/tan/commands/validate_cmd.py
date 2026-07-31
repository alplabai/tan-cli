# SPDX-License-Identifier: Apache-2.0
"""`tan validate` -- is this `board.yaml` structurally sound?

Two paths, mirroring `crates/tan-cli/src/commands/validate.rs`:

* ``--offline`` runs only the structural checks that ship inside tan. No SDK
  checkout, no subprocess, no network. This is the path the two committed
  conformance fixtures exercise, which is why their ``data.commandLine`` is
  ``""``.
* without ``--offline`` the real validator is the SDK's own
  ``scripts/validate_board_yaml.py``, spawned as a subprocess. tan does not
  reimplement alp-sdk's schema: the SDK owns ``metadata/schemas/`` and
  ADR-0017's doctrine is to consume what exists. Not ported yet, and it says
  so -- at exit 1 (``RuntimeFailure``), not exit 5.

  **Exit 1 here is a DEFERRAL, not a match to the oracle.** An earlier revision
  of this docstring claimed it matched; that claim was never measured and was
  wrong. Measured directly, running ``target/debug/tan.exe`` (tan 0.4.1-dev)
  with ``--format json``:

  - empty directory -> exit **2**, ``validate.board-yaml-missing``
  - ``board.yaml`` present, no SDK root -> exit **2**,
    ``validate.sdk-root-unresolved``

  Exit 1 is reachable in the oracle only AFTER a validator actually spawns and
  returns an unexpected status. So the oracle draws a line this port does not
  yet: pre-spawn guards at 2, post-spawn failure at 1. The ``board.yaml``
  -missing guard below is now aligned with it. The remaining divergence --
  ``board.yaml`` present but no SDK, where the oracle says 2
  ``sdk-root-unresolved`` and this port says 1 ``spawn-not-implemented`` -- is
  the genuine v0.6.0 decision, tracked in tan-cli#262.

  Exit 5 is wrong on every one of these paths regardless: reporting "not ported
  yet" as ``InternalFailure`` would tell CI/the extension this is a tan crash,
  the same mistake the Rust comment on the offline path (below) was written to
  stop repeating.

  Re-measure before changing any of this. Do not infer the oracle's behaviour
  from a report, a docstring, or ``crates/`` -- run the binary.

  **The wire string itself, ``validate.spawn-not-implemented``, has no oracle
  counterpart -- decided KEEP, not renamed.** The oracle has no "not ported"
  state at all; the closest thing it emits is ``validate.failed``. Measured
  the FULL validator-exit-status -> outcome map against ``target/debug/tan.exe``
  with a contrived SDK (``scripts/alp_project.py`` marker +
  ``scripts/validate_board_yaml.py`` exiting a chosen code), ``board.yaml``
  present, ``--sdk-root`` pointed at it, no ``--offline``:

  =============== =============================== ===
  validator exit   outcome                          rc
  =============== =============================== ===
  0                clean                            0
  1                schema-violation                 2
  2                missing-preset                    2
  3                hardware-revision                2
  5, 77            failed                            1
  =============== =============================== ===

  So ``validate.failed`` is specifically the "anything outside the 0-3 range
  the resolver maps by number" case -- NOT every non-clean status, and in
  particular not exit 2 or 3, which have their own named outcomes. A reader
  must not infer "any nonzero -> failed" from this docstring. That is a
  DIFFERENT failure shape from this port's: the oracle spawned and got back
  nonsense, this port never spawns at all.
  Reusing ``validate.failed`` here would conflate "we attempted validation
  and the subprocess misbehaved" with "this code path does not exist yet"
  under one string, which is a worse signal for the same reason the exit-code
  choice above is. ``validate.spawn-not-implemented`` is therefore kept as a
  deliberate, PORT-ONLY code: it is not in ``contract/issue-codes.json``
  (nothing there mentions ``validate.*`` at all) and no v0.4.1 consumer has
  ever seen it, since v0.4.1 is the Rust CLI and this spelling exists only in
  this port. It becomes dead code the moment the real spawn path lands
  (tan-cli#262) and this branch is deleted in favour of actually spawning, at
  which point the resulting failures naturally become ``validate.failed``
  like the oracle's.

**A wrong-shaped board.yaml is the USER's problem, not a tan crash.** The Rust
carries a comment earned the hard way: routing a malformed file through
``InternalFailure`` (exit 5) "told CI/the extension this was a tan crash" and
disagreed with the spawn path, which reports the identical file as exit 2
``schema-violation``. So a file that parses as YAML but does not fit the model
is a *validation* failure -- exit 2 -- and the one place in this port where
``ValidationFailure`` is genuinely the right code. (Contrast `tan build`, where a
malformed *plan* is exit 1: the consumer renders 2 as a warning and 1 as an
error, and an unbuildable plan is not a warning.)

The structural checks are deliberately about the CONTRACT's shape -- is there a
top-level ``os:``, is there a ``cores:`` block -- never about hardware. No SKU
list, no addresses, no pin names live here: the OS is derived from each core's
Cortex class by the SDK planner and is never selectable, so tan has no business
knowing which SKUs exist.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode
from tan.version import TAN_VERSION

#: `data.schemaVersion` for this command's payload -- the envelope payload's own
#: version, unrelated to `board.yaml`'s `schemaVersion:`.
DATA_SCHEMA_VERSION = "1"

#: The `--format` values this command accepts. `diagnostic-v1` and `sarif` are
#: NOT part of the shared `Format::Text`/`Format::Json` enum every other
#: command mirrors from `crates/tan-cli/src/cli.rs` -- there is no Rust
#: precedent for them yet. They exist so alp-sdk's quality gate
#: (`scripts/check_diagnostic_schema.py`) can point at `tan` instead of
#: spawning `python -m alp_cli.main validate --format json`.
#:
#: WARNING -- `--format json` does NOT mean the same thing in the two CLIs.
#: In alp-sdk (`scripts/alp_cli/validate.py:20,34`), `--format json` IS the
#: diagnostic-v1 document. In tan, `--format json` is the envelope
#: (`{command,ok,exitCode,project,data,issues}`; see `_emit` below) and the
#: diagnostic-v1 document moved to its own `--format diagnostic-v1`. The
#: obvious find-and-replace swap -- pointing the gate's argv at `tan` while
#: keeping `"--format", "json"` -- validates the envelope against
#: `diagnostic-v1.schema.json` (whose root is `additionalProperties: false`)
#: and fails with something like "Additional properties are not allowed
#: ('command', 'ok', 'exitCode', ...)", which names nothing about the real
#: cause. The gate-side edit `check_diagnostic_schema.py` needs is therefore
#: NOT a repoint of the same argv -- it must spawn
#: `tan validate --offline --format diagnostic-v1`, not
#: `tan validate --offline --format json`. `--format sarif` keeps its name
#: unchanged in both CLIs, so only the `json` case silently diverges.
#:
#: `diagnostic-v1` is alp-sdk's `metadata/schemas/diagnostic-v1.schema.json`
#: (mirroring `scripts/alp_cli/diagnostic_format.py:to_machine_json`'s shape --
#: `schemaVersion`/`tool`/`diagnostics[]`, zero-based LSP ranges), `sarif` is
#: its SARIF 2.1.0 sibling (`to_sarif`). `text` is unchanged by this addition.
FORMAT_TEXT = "text"
FORMAT_JSON = "json"
FORMAT_DIAGNOSTIC_V1 = "diagnostic-v1"
FORMAT_SARIF = "sarif"
_FORMATS = (FORMAT_TEXT, FORMAT_JSON, FORMAT_DIAGNOSTIC_V1, FORMAT_SARIF)

#: `diagnostic-v1.schema.json`'s own `schemaVersion` -- an integer `const: 1`,
#: unrelated to `DATA_SCHEMA_VERSION` above (a different document, a different
#: field, a different type).
_DIAGNOSTIC_SCHEMA_VERSION = 1

_SARIF_SCHEMA_URI = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/"
    "sarif-schema-2.1.0.json"
)

#: Outcome strings, verbatim from `tan_core::validate::Outcome::as_str`. The
#: issue code is `validate.<outcome>`, so these strings are wire contract.
OUTCOME_CLEAN = "clean"
OUTCOME_SCHEMA_VIOLATION = "schema-violation"
OUTCOME_FAILED = "failed"

#: `Issue.severity` -> SARIF `level`, mirroring
#: `scripts/alp_cli/diagnostic_format.py:_SARIF_LEVEL`. A deliberate explicit
#: table, not a passthrough: an `Issue.severity` outside this set must raise
#: (KeyError) rather than silently emit a SARIF log whose `level` is not one
#: of the spec's three values. Unreachable today -- every `Issue` this file
#: builds hardcodes `"error"` -- but the guard is the point, mirroring the
#: oracle's own dict rather than trusting the string straight through.
_SARIF_LEVEL = {"error": "error", "warning": "warning", "note": "note"}


@dataclass(frozen=True)
class _Result:
    outcome: str
    messages: tuple[str, ...]


class BoardShapeError(Exception):
    """`board.yaml` parsed as YAML but does not fit the board model."""


def _load_yaml(text: str) -> Any:
    """Parse YAML using PyYAML when present, else a minimal top-level reader.

    tan ships no YAML dependency of its own (`typer` + `rich` only), and the
    offline path must work with nothing installed. PyYAML is used when it
    happens to be importable -- it usually is, since a Zephyr workspace needs
    it -- and otherwise we fall back to reading only what the structural checks
    actually consult: which top-level keys exist and whether each is a scalar
    or a block. That is enough to distinguish `som: <scalar>` from
    `som:` + an indented mapping, which is exactly what the checks below ask.
    """
    try:
        import yaml  # noqa: PLC0415  (optional at runtime, by design)
    except ImportError:
        return _top_level_shape(text)
    try:
        return yaml.safe_load(text)
    except Exception as err:  # yaml.YAMLError and anything a loader raises
        raise BoardShapeError(f"could not be parsed as YAML: {err}") from err


def _top_level_shape(text: str) -> dict[str, Any]:
    """Map each unindented `key:` to a scalar string or a nested-block marker.

    Deliberately not a YAML parser. It answers one question -- is this key a
    scalar or does it open a block -- because that is all the structural checks
    need when PyYAML is absent.
    """
    shape: dict[str, Any] = {}
    pending: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[:1].isspace() or raw.lstrip().startswith("-"):
            if pending is not None:
                shape[pending] = {}  # the key opened a block
                pending = None
            continue
        pending = None
        key, sep, rest = raw.partition(":")
        if not sep:
            continue
        name = key.strip()
        value = rest.strip()
        if value:
            shape[name] = value
        else:
            shape[name] = None
            pending = name
    return shape


def _effective_schema_version(doc: dict[str, Any]) -> int:
    raw = doc.get("schemaVersion")
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return 1


def validate_board_text(text: str) -> _Result:
    """The offline structural validator. Pure: text in, outcome out."""
    doc = _load_yaml(text)
    if doc is None:
        raise BoardShapeError("the document is empty")
    if not isinstance(doc, dict):
        raise BoardShapeError(
            f"the top level must be a mapping of keys, got {type(doc).__name__}"
        )

    som = doc.get("som")
    if som is not None and not isinstance(som, dict):
        raise BoardShapeError(
            "`som:` must be a mapping carrying a `sku:` key, but a scalar was "
            f"given ({som!r}). Write it as:\n  som:\n    sku: <SKU>"
        )

    messages: list[str] = []
    if _effective_schema_version(doc) >= 2:
        # I-02: the OS is derived from each core's Cortex class and is never
        # selectable, so a top-level `os:` is rejected outright.
        if doc.get("os") is not None:
            messages.append(
                "board.yaml v2: top-level 'os:' is not valid; move it into a 'cores:' block"
            )
        cores = doc.get("cores")
        if not isinstance(cores, dict) or not cores:
            messages.append(
                "board.yaml v2: 'cores:' block is required and must have at least one entry"
            )

    outcome = OUTCOME_CLEAN if not messages else OUTCOME_SCHEMA_VIOLATION
    return _Result(outcome=outcome, messages=tuple(messages))


def _resolve_board_path(project: str | None, board_yaml: str | None) -> tuple[str, str]:
    """Return `(project_root, board_yaml_path)`, both as the CLI reports them.

    Mirrors `resolve_offline_board_path`: the root defaults to the literal `"."`
    and the board path stays RELATIVE, which the conformance fixtures pin
    (`project.root == "."`, `boardYamlPath == "./board.yaml"`).
    """
    root = project if project else "."
    if board_yaml and os.path.isabs(board_yaml):
        return root, board_yaml
    leaf = board_yaml or "board.yaml"
    # Joined as STRINGS, not via pathlib: `Path(".") / "board.yaml"` normalises
    # to `board.yaml`, but Rust's `Path::new(".").join("board.yaml")` keeps the
    # `./`, and the conformance fixtures pin `"./board.yaml"`.
    sep = "" if root.endswith(("/", "\\")) else "/"
    return root, f"{root}{sep}{leaf}"


#: A single point in the offline structural checks' output: `board.yaml`
#: parses fine, so every issue they raise is document-level rather than tied to
#: a source line/column tan tracks today (unlike the SDK's own
#: `Diagnostic.line`/`.col`, which the deferred spawn path would carry). Zero
#: satisfies `diagnostic-v1.schema.json`'s `required: [start, end]` honestly --
#: it is not a claim about *where* on the line, only that no better position
#: is known.
_ZERO_POSITION = {"line": 0, "character": 0}


def _issue_to_diagnostic(issue: Issue, board_path: str) -> dict[str, Any]:
    """One `issues[].code` (e.g. `validate.schema-violation`) as one
    `$defs/diagnostic` entry. The dot is stripped because `diagnostic-v1`'s
    `code` pattern (`^[A-Za-z][A-Za-z0-9_-]*$`) forbids it -- this is a
    different wire, not a rename of the envelope code.

    Deliberate omission: the oracle (`diagnostic_format.py:_diagnostic_to_
    json`) sets `documentationUri` unconditionally via `_doc_url`, which
    builds `docs/diagnostics/<code>.md`. tan ships no such landing pages, so
    there is no honest URL to put there; the field is left out rather than
    invented. It is optional in `diagnostic-v1.schema.json`, so the document
    still validates."""
    return {
        "uri": board_path,
        "range": {"start": _ZERO_POSITION, "end": _ZERO_POSITION},
        "severity": issue.severity,
        "code": issue.code.replace(".", "-"),
        "message": issue.message,
    }


def _diagnostic_v1_document(issues: list[Issue], board_path: str) -> dict[str, Any]:
    """`metadata/schemas/diagnostic-v1.schema.json`, mirroring
    `scripts/alp_cli/diagnostic_format.py:to_machine_json`'s shape."""
    return {
        "schemaVersion": _DIAGNOSTIC_SCHEMA_VERSION,
        "tool": {"name": "tan", "version": TAN_VERSION},
        "diagnostics": [_issue_to_diagnostic(i, board_path) for i in issues],
    }


def _sarif_document(issues: list[Issue], board_path: str) -> dict[str, Any]:
    """SARIF 2.1.0 (`runs[].results[]`), mirroring
    `scripts/alp_cli/diagnostic_format.py:to_sarif`. A separate artefact from
    `diagnostic-v1.schema.json` -- SARIF `region` is one-based by spec, so it
    does not reuse `_ZERO_POSITION`.

    Deliberate omission: the oracle's `_sarif_rules` gives every rule a
    `helpUri` via the same `_doc_url`-backed `documentationUri` as above. Same
    reasoning as `_issue_to_diagnostic`: tan has no `docs/diagnostics/<code>.md`
    to point at, so `helpUri` is left off each rule rather than faked.
    `helpUri` is optional in the SARIF 2.1.0 schema."""
    rules: dict[str, dict[str, str]] = {}
    results = []
    for issue in issues:
        code = issue.code.replace(".", "-")
        rules.setdefault(code, {"id": code})
        results.append(
            {
                "ruleId": code,
                "level": _SARIF_LEVEL[issue.severity],
                "message": {"text": issue.message},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": board_path},
                            "region": {
                                "startLine": 1,
                                "startColumn": 1,
                                "endLine": 1,
                                "endColumn": 1,
                            },
                        }
                    }
                ],
            }
        )
    return {
        "$schema": _SARIF_SCHEMA_URI,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "tan",
                        "informationUri": "https://github.com/alplabai/tan-cli",
                        "version": TAN_VERSION,
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def _emit(
    *,
    output_format: str,
    root: str,
    board_path: str,
    outcome: str,
    issues: list[Issue],
    exit_code: ExitCode,
) -> None:
    if output_format == FORMAT_JSON:
        data = {
            "schemaVersion": DATA_SCHEMA_VERSION,
            "outcome": outcome,
            "issueCount": len(issues),
            "commandLine": "",
            # `data.boardYamlPath` is UNTOUCHED by tan-cli#236 -- it names where
            # tan looked even on the missing-board refusal below; only
            # `project.boardYaml` (Rust's starkest instance of the bug: the
            # refusal one line below says the file does not exist, in the same
            # envelope that used to still name it) is existence-filtered.
            "boardYamlPath": board_path,
        }
        emit(
            Envelope(
                "validate",
                Project.resolved(root, board_path),
                data,
                issues,
                exit_code,
            )
        )
    elif output_format == FORMAT_DIAGNOSTIC_V1:
        # indent=2, matching scripts/alp_cli/validate.py:34's
        # `json.dumps(to_machine_json(collector), indent=2)` -- these two
        # formats are ported documents, not the envelope (which is
        # deliberately compact; see `tan.envelope.emit`'s `separators`).
        typer.echo(json.dumps(_diagnostic_v1_document(issues, board_path), indent=2))
    elif output_format == FORMAT_SARIF:
        # indent=2, matching scripts/alp_cli/validate.py:36.
        typer.echo(json.dumps(_sarif_document(issues, board_path), indent=2))
    else:
        stream = typer.get_text_stream("stderr")
        if issues:
            stream.write("validate: validation failure\n")
            for issue in issues:
                stream.write(f"{issue.message}\n")
        else:
            stream.write(f"validate: {board_path} is clean\n")
    raise typer.Exit(int(exit_code))


def validate(
    offline: bool = typer.Option(
        False, "--offline", help="Run only the structural checks that ship in tan."
    ),
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to '.')."
    ),
    board_yaml: str = typer.Option(
        None, "--board-yaml", metavar="PATH", help="Explicit board.yaml path."
    ),
    sdk_root: str = typer.Option(  # accepted, not read; see below
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    output_format: str = typer.Option(
        "text",
        "--format",
        metavar="FORMAT",
        help="Output format: text, json, diagnostic-v1, or sarif.",
    ),
) -> None:
    """Validate a board.yaml.

    `--sdk-root` is declared, not consumed: clap makes it `global = true` in
    Rust (`crates/tan-cli/src/cli.rs`), so `tan --sdk-root X validate` must not
    be a parse error even though `validate`'s own checks never touch it
    (verified against the oracle: an arbitrary `--sdk-root` value changes
    nothing in the envelope). Without a same-named local option here,
    `cli._reorder_global_flags` relocates the flag to right after `validate`
    and Click rejects it there as unrecognised -- the pre-subcommand position
    the extension actually uses (`alpCli/vscodeAdapter.ts`'s `withSdkRoot`).
    """
    if output_format not in _FORMATS:
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json', 'diagnostic-v1', 'sarif')",
            param_hint="--format",
        )
    root, board_path = _resolve_board_path(project, board_yaml)

    def fail(code: str, message: str, exit_code: ExitCode) -> None:
        _emit(
            output_format=output_format,
            root=root,
            board_path=board_path,
            outcome=OUTCOME_FAILED,
            issues=[Issue(f"validate.{code}", "error", message)],
            exit_code=exit_code,
        )

    if not Path(board_path).exists():
        # Ordered BEFORE the not-ported stub, deliberately. MEASURED against the
        # oracle (`target/debug/tan.exe`, tan 0.4.1-dev) in an empty directory:
        # exit 2, `validate.board-yaml-missing`, `outcome: "failed"` -- and the
        # oracle reaches this guard on both paths, `--offline` or not. A stub
        # that short-circuits above this check would answer "not ported yet" to
        # a question the oracle answers "your board.yaml is missing", in the one
        # case a brand-new user hits first. Cheap to keep compatible; keep it.
        fail(
            "board-yaml-missing",
            "board.yaml path could not be resolved or the file does not exist.",
            ExitCode.VALIDATION_FAILURE,
        )
        return

    if not offline:
        # The SDK owns metadata/schemas/; tan does not reimplement it. The spawn
        # path is NOT ported, so this reports a deferral. See the module
        # docstring for why exit 1 (RuntimeFailure) rather than exit 5, and for
        # the one divergence from the oracle it knowingly keeps (tan-cli#262).
        fail(
            "spawn-not-implemented",
            "the full (spawn) validator is not ported yet -- run with --offline, "
            "or use the SDK's scripts/validate_board_yaml.py directly.",
            ExitCode.RUNTIME_FAILURE,
        )
        return

    try:
        text = Path(board_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        # Not a tan bug: an unreadable or non-UTF-8 file is the user's to fix.
        fail(
            "internal-failure",
            f"could not read board.yaml: {err}",
            ExitCode.INTERNAL_FAILURE,
        )
        return

    try:
        result = validate_board_text(text)
    except BoardShapeError as err:
        fail(
            "schema-violation",
            f"board.yaml is not valid: {err}",
            ExitCode.VALIDATION_FAILURE,
        )
        return
    except Exception as err:  # never a bare traceback; the envelope is the contract
        fail(
            "internal-failure",
            f"validator failed unexpectedly: {err}",
            ExitCode.INTERNAL_FAILURE,
        )
        return

    issues = [
        Issue(f"validate.{result.outcome}", "error", message)
        for message in result.messages
    ]
    exit_code = (
        ExitCode.SUCCESS
        if result.outcome == OUTCOME_CLEAN
        else ExitCode.VALIDATION_FAILURE
    )
    _emit(
        output_format=output_format,
        root=root,
        board_path=board_path,
        outcome=result.outcome,
        issues=issues,
        exit_code=exit_code,
    )
