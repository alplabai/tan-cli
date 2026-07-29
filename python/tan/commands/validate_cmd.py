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
  ADR-0017's doctrine is to consume what exists. Deferred, and it says so.

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

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode

#: `data.schemaVersion` for this command's payload -- the envelope payload's own
#: version, unrelated to `board.yaml`'s `schemaVersion:`.
DATA_SCHEMA_VERSION = "1"

#: Outcome strings, verbatim from `tan_core::validate::Outcome::as_str`. The
#: issue code is `validate.<outcome>`, so these strings are wire contract.
OUTCOME_CLEAN = "clean"
OUTCOME_SCHEMA_VIOLATION = "schema-violation"
OUTCOME_FAILED = "failed"


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


def _emit(
    *,
    json_mode: bool,
    root: str,
    board_path: str,
    outcome: str,
    issues: list[Issue],
    exit_code: ExitCode,
) -> None:
    data = {
        "schemaVersion": DATA_SCHEMA_VERSION,
        "outcome": outcome,
        "issueCount": len(issues),
        "commandLine": "",
        "boardYamlPath": board_path,
    }
    if json_mode:
        emit(
            Envelope(
                "validate",
                Project(root=root, board_yaml=board_path),
                data,
                issues,
                exit_code,
            )
        )
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
    output_format: str = typer.Option(
        "text", "--format", metavar="FORMAT", help="Output format: text or json."
    ),
) -> None:
    """Validate a board.yaml."""
    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = output_format == "json"
    root, board_path = _resolve_board_path(project, board_yaml)

    def fail(code: str, message: str, exit_code: ExitCode) -> None:
        _emit(
            json_mode=json_mode,
            root=root,
            board_path=board_path,
            outcome=OUTCOME_FAILED,
            issues=[Issue(f"validate.{code}", "error", message)],
            exit_code=exit_code,
        )

    if not offline:
        # The SDK owns metadata/schemas/; tan does not reimplement it.
        fail(
            "internal-failure",
            "the full (spawn) validator is not ported yet -- run with --offline, "
            "or use the SDK's scripts/validate_board_yaml.py directly.",
            ExitCode.INTERNAL_FAILURE,
        )
        return

    if not Path(board_path).exists():
        fail(
            "board-yaml-missing",
            "board.yaml path could not be resolved or the file does not exist.",
            ExitCode.VALIDATION_FAILURE,
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
        json_mode=json_mode,
        root=root,
        board_path=board_path,
        outcome=result.outcome,
        issues=issues,
        exit_code=exit_code,
    )
