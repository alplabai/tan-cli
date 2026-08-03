# SPDX-License-Identifier: Apache-2.0
"""`tan diff` -- show how `normalize_board_model` changes the parsed
board.yaml (tan-cli#260).

Mirrors `crates/tan-cli/src/commands/diff.rs` plus the two `tan-core` helpers
it composes (`model::{parse_board_model, normalize_board_model}`,
`diff::{collect_diff_entries, prune_nulls}`).

**Why this is NOT a generic recursive JSON differ, unlike the Rust.** The Rust
parses the WHOLE `board.yaml` into a typed `BoardModel`, normalizes it, and
diffs the two full trees with a generic recursive walk
(`tan_core::diff::collect_recursive`). `normalize_board_model`
(`crates/tan-core/src/model.rs:217-237`) only ever CLEARS four top-level
fields, and only ever to `None`/absent -- it never adds a key and never
changes one that survives:

* schema version < 2: `libraries` if it deserialized to an EMPTY list, `iot`
  if none of its four toggles is `true`, `inference` if both its fields are
  empty/absent.
* schema version >= 2: `os` unconditionally (v2 moves it into `cores:`).

Every other known field (`som`, `preset`, `cores`, `ipc`, `diagnostics`,
`populated`, `chips`, `e1m_routes`) is IDENTICAL between the parsed and
normalized model, so a full recursive diff would recurse into each, find
`before == after`, and contribute zero entries -- the same outcome this
module reaches directly, without building or comparing either side's full
tree. A `DiffEntry.kind` is therefore always `"removed"` here; `"added"`/
`"changed"` are unreachable through `normalize_board_model` and are kept only
so the wire shape (`DiffKind`: `added`/`removed`/`changed`) stays the
contract's, not because this module can produce them today.

**PyYAML is required.** Unlike the shallow top-level-shape scanners in
`validate_cmd`/`presets_cmd` (scalar-vs-block only), computing this diff needs
real nested values -- is `iot:` a mapping, is any of its four toggles `true`,
is `inference:`'s `backend` empty -- which a line-oriented fallback cannot
answer. tan ships no YAML dependency of its own (`typer` + `rich` only), so a
build with no PyYAML installed refuses with `diff.pyyaml-unavailable`
(`RUNTIME_FAILURE`, matching `validate_cmd`'s `spawn-not-implemented`
precedent for "this build cannot do that yet") rather than guessing.

**Scope of the structural checks below.** `_parse_fields` validates the
TOP-LEVEL type of every known `BoardModel` field (is `cores:` a mapping, is
`ipc:` a list, ...) because a real Rust type mismatch anywhere in the document
fails the WHOLE typed parse (`ParseError::Yaml`), and reporting success with a
wrong diff would be a worse defect than an over-eager refusal. It does NOT
validate the shape of values `diff` never reads (`cores.*.peripherals`,
`e1m_routes.*`, ...) -- those fields are never touched by
`normalize_board_model` and never enter this module's output; going a level
deeper than "top-level key has the right YAML kind" would just be more parser
tan does not need for the one question this command answers. This means a
narrow class of nested-only type errors (e.g. `iot: {wifi: "yes"}`, a string
where a bool belongs) that the oracle refuses is not caught here -- it
diverges by silently passing the value through the way `prune_nulls` treats
any non-null value.

`som:`'s own shape (must be a mapping, not a bare SKU string) is checked with
the exact oracle wording via Python's own `repr()` -- which is actually the
*more* correct implementation of the two: the Rust's `python_repr` hand-mimics
Python's `repr()` from a `serde_yaml` (YAML 1.2) value and has one known gap
against real Python semantics on YAML 1.1-vs-1.2 boolean/null resolution
(`tan_core::validate` module docs); this module calls `repr()` on a value
PyYAML (YAML 1.1, the same rules Python's ecosystem uses) actually parsed, so
there is nothing left to approximate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from tan.commands.presets_cmd import resolve_project_paths
from tan.envelope import Envelope, Issue, Project, emit
from tan.exit_codes import ExitCode

#: `data.schemaVersion` for this command's payload -- the envelope payload's
#: own version, unrelated to `board.yaml`'s `schemaVersion:`.
DATA_SCHEMA_VERSION = "1"

#: Serialized `Iot`/`Inference` field order (`crates/tan-core/src/model.rs`'s
#: struct declaration order) -- `preserve_order` serde_json keeps this order
#: for the wire `before` value, and it is fixed regardless of the YAML
#: source's own key order, so it is spelled out here rather than derived from
#: dict iteration.
_IOT_FIELDS = ("wifi", "mqtt", "ble", "tls")
_INFERENCE_FIELDS = ("backend", "default_arena_kib")


class ParseFailure(Exception):
    """A `board.yaml` this offline diff cannot process. `code` is the
    `diff.<code>` issue suffix; `message` already carries the oracle's
    `board.yaml is not valid[ YAML]: ...` prefix so callers pass it straight
    through."""

    def __init__(self, code: str, message: str, exit_code: ExitCode = ExitCode.VALIDATION_FAILURE):
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


@dataclass(frozen=True)
class DiffEntry:
    path: str
    kind: str  # "added" | "removed" | "changed" -- see module docstring
    before: Any = None
    after: Any = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "kind": self.kind}
        if self.before is not None:
            out["before"] = self.before
        if self.after is not None:
            out["after"] = self.after
        return out


def _load_document(text: str) -> Any:
    """The raw YAML document, or a `ParseFailure` matching `ParseError`'s two
    reachable variants on this path (`Yaml`, and the `som:`-shape pre-check).
    `EmptyDocument`/`NotAMapping` are NOT reachable here -- those are
    `validate_cmd`'s `reject_non_mapping_document`, which `diff` never calls;
    a null or bare-scalar document degrades to the default (empty) model here,
    matching `parse_board_model`'s own TS-parity leniency.
    """
    try:
        import yaml  # noqa: PLC0415  (optional at runtime, by design)
    except ImportError as err:
        raise ParseFailure(
            "pyyaml-unavailable",
            "this build of tan has no YAML support installed, so `tan diff` cannot "
            "compute a normalization diff.",
            ExitCode.RUNTIME_FAILURE,
        ) from err
    try:
        return yaml.safe_load(text)
    except Exception as err:  # noqa: BLE001 -- yaml.YAMLError and anything a loader raises
        raise ParseFailure("schema-violation", f"board.yaml is not valid YAML: {err}") from err


def _yaml_kind(value: Any) -> str:
    """A short YAML-ish type name for an error message -- not a claim of
    matching serde's exact wording (see the module docstring's scope note)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "a sequence"
    if isinstance(value, dict):
        return "a mapping"
    return type(value).__name__


def _typed_field(doc: dict, key: str, expected: type, label: str) -> Any:
    """`doc[key]` if absent or already `expected`-shaped, else a
    `ParseFailure` mirroring the whole-document failure a struct-typed
    `serde_yaml` deserialize would raise for the same mismatch."""
    value = doc.get(key)
    if value is None or isinstance(value, expected):
        return value
    raise ParseFailure(
        "schema-violation",
        f"board.yaml is not valid YAML: {key}: expected {label}, got {_yaml_kind(value)}",
    )


def _parse_fields(doc: Any) -> tuple[int, str | None, list | None, dict | None, dict | None]:
    """`(effective_schema_version, os, libraries, iot, inference)` -- the only
    values `normalize_board_model` can ever act on. Raises `ParseFailure` for
    every document shape that would fail the Rust's typed parse; see the
    module docstring for exactly how far the type checking goes.
    """
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise ParseFailure(
            "schema-violation",
            f"board.yaml is not valid YAML: invalid type: {_yaml_kind(doc)}, expected a mapping",
        )

    som = doc.get("som")
    if som is not None and not isinstance(som, dict):
        raise ParseFailure(
            "schema-violation",
            "board.yaml is not valid: `som:` must be a mapping carrying a `sku:` key, but "
            f"a scalar was given ({som!r}). Write it as:\n  som:\n    sku: <SKU>",
        )

    schema_version = doc.get("schemaVersion")
    schema_version_ok = isinstance(schema_version, int) and not isinstance(schema_version, bool)
    if schema_version is not None and (not schema_version_ok or schema_version < 0):
        raise ParseFailure(
            "schema-violation",
            f"board.yaml is not valid YAML: schemaVersion: expected a non-negative integer, "
            f"got {_yaml_kind(schema_version)}",
        )
    effective_version = schema_version if schema_version is not None else 1

    os_value = _typed_field(doc, "os", str, "a string")
    libraries = _typed_field(doc, "libraries", list, "a sequence")
    iot = _typed_field(doc, "iot", dict, "a mapping")
    inference = _typed_field(doc, "inference", dict, "a mapping")

    # Fields `diff` never reads (never touched by normalize_board_model, so
    # never contribute a diff entry either way) -- top-level shape checked
    # only, per the module docstring's scope note.
    _typed_field(doc, "preset", str, "a string")
    _typed_field(doc, "cores", dict, "a mapping")
    _typed_field(doc, "ipc", list, "a sequence")
    _typed_field(doc, "diagnostics", dict, "a mapping")
    _typed_field(doc, "populated", dict, "a mapping")
    _typed_field(doc, "chips", list, "a sequence")
    _typed_field(doc, "e1m_routes", dict, "a mapping")

    return effective_version, os_value, libraries, iot, inference


def _iot_any_enabled(iot: dict) -> bool:
    return any(iot.get(k) is True for k in _IOT_FIELDS)


def _iot_pruned(iot: dict) -> dict:
    return {k: iot[k] for k in _IOT_FIELDS if iot.get(k) is not None}


def _inference_is_empty(inference: dict) -> bool:
    backend = inference.get("backend")
    backend_str = backend if isinstance(backend, str) else ""
    return backend_str == "" and inference.get("default_arena_kib") is None


def _inference_pruned(inference: dict) -> dict:
    return {k: inference[k] for k in _INFERENCE_FIELDS if inference.get(k) is not None}


def compute_diff_entries(
    effective_version: int,
    os_value: str | None,
    libraries: list | None,
    iot: dict | None,
    inference: dict | None,
) -> list[DiffEntry]:
    """The `normalize_board_model` effect as `DiffEntry` list, sorted by path
    (matching `collect_diff_entries`'s own `sort_by(path)` -- alphabetical
    among `inference`/`iot`/`libraries`/`os` needs no explicit sort since at
    most one of `{inference, iot, libraries}` XOR `{os}` group is ever
    populated, but sorting keeps the guarantee explicit rather than accidental).
    """
    entries: list[DiffEntry] = []
    if effective_version < 2:
        if libraries is not None and len(libraries) == 0:
            entries.append(DiffEntry("libraries", "removed", before=[]))
        if iot is not None and not _iot_any_enabled(iot):
            entries.append(DiffEntry("iot", "removed", before=_iot_pruned(iot)))
        if inference is not None and _inference_is_empty(inference):
            entries.append(DiffEntry("inference", "removed", before=_inference_pruned(inference)))
    else:
        if os_value is not None:
            entries.append(DiffEntry("os", "removed", before=os_value))
    entries.sort(key=lambda e: e.path)
    return entries


_KIND_LABEL = {"added": "ADDED", "removed": "REMOVED", "changed": "CHANGED"}


def _format_value(value: Any) -> str:
    """`<undefined>` for `None`, JSON-quoted for a string, compact JSON
    otherwise, truncated to 117 chars + `...` past 120 -- verbatim from
    `diff.rs`'s `format_value`. `ensure_ascii=False`: `serde_json::to_string`
    emits raw UTF-8, never a `\\uXXXX` escape, for a non-ASCII board.yaml
    string."""
    if value is None:
        return "<undefined>"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(raw) > 120:
        return raw[:117] + "..."
    return raw


def _render_text(entries: list[DiffEntry], board_path: str, quiet: bool) -> list[str]:
    if not entries:
        return ["diff: no effective-config differences detected."]
    lines = [f"diff: {len(entries)} differences in {board_path}"]
    if not quiet:
        for entry in entries:
            lines.append(
                f"{_KIND_LABEL[entry.kind]} {entry.path}: "
                f"{_format_value(entry.before)} -> {_format_value(entry.after)}"
            )
    return lines


def _data(
    board_path: str, entries: list[DiffEntry], *, unchanged: bool | None = None
) -> dict[str, Any]:
    """`unchanged` defaults to `len(entries) == 0` for the success path, but a
    FAILURE envelope's `DiffData` hardcodes `unchanged: false` regardless of
    its (always-empty) `changes` list -- verbatim from `diff.rs`'s `failure()`
    -- so `_emit_failure` passes it explicitly rather than letting an empty
    `changes: []` compute `unchanged: true` for a run that never got far
    enough to answer that question."""
    return {
        "schemaVersion": DATA_SCHEMA_VERSION,
        "boardYamlPath": board_path,
        "unchanged": (len(entries) == 0) if unchanged is None else unchanged,
        "changeCount": len(entries),
        "changes": [e.as_dict() for e in entries],
    }


def _emit_failure(
    *,
    json_mode: bool,
    root: str,
    board_path: str,
    code: str,
    message: str,
    exit_code: ExitCode,
    text_lines: list[str],
) -> None:
    """Mirrors `diff.rs`'s `failure(...)`: the JSON issue message and the
    text-mode lines are independent strings, not one derived from the other
    (`board-yaml-missing`'s text line reads differently from its issue
    message) -- callers supply `text_lines` verbatim, matching the Rust
    call sites' own hand-written `vec![...]`. Unlike the success path's
    `_render_text`, these lines are NOT filtered by `--quiet` -- measured
    against the oracle: `diff --quiet` on every failure prints the identical
    lines a plain `diff` does.
    """
    if json_mode:
        emit(
            Envelope(
                "diff",
                Project.resolved(root, board_path),
                _data(board_path, [], unchanged=False),
                [Issue(f"diff.{code}", "error", message)],
                exit_code,
            )
        )
    else:
        stream = typer.get_text_stream("stderr")
        for line in text_lines:
            stream.write(f"{line}\n")
    raise typer.Exit(int(exit_code))


def diff(
    ctx: typer.Context,
    project: str = typer.Option(
        None, "--project", metavar="PATH", help="Project root (defaults to current directory)."
    ),
    board_yaml: str = typer.Option(
        None,
        "--board-yaml",
        metavar="PATH",
        help="Explicit board.yaml path (overrides project resolution).",
    ),
    sdk_root: str = typer.Option(  # accepted, not read; diff never resolves an SDK
        None, "--sdk-root", metavar="PATH", help="alp-sdk checkout root."
    ),
    target: str = typer.Option(  # accepted, not read
        None,
        "--target",
        metavar="EMIT",
        help="Generation target (e.g. zephyr-conf, dts-overlay, cmake-args, yocto-conf).",
    ),
    all_targets: bool = typer.Option(  # accepted, not read
        False, "--all", help="Run command against all relevant targets."
    ),
    output_format: str = typer.Option(
        None, "--format", metavar="FORMAT", help="Output format: text or json."
    ),
    verbose: bool = typer.Option(  # accepted, not read
        False, "--verbose", help="Emit additional diagnostic detail."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", help="Suppress non-essential output (omits the per-change lines)."
    ),
    no_color: bool = typer.Option(  # accepted, not read; diff emits no ANSI color
        False, "--no-color", help="Disable ANSI color in text output."
    ),
    non_interactive: bool = typer.Option(  # accepted, not read; diff never prompts
        False, "--non-interactive", help="Never prompt."
    ),
    ci: bool = typer.Option(  # accepted, not read
        False, "--ci", help="CI mode: implies non-interactive and disables color."
    ),
) -> None:
    """Show how board.yaml normalization changes the effective config.

    `--sdk-root`/`--target`/`--all`/`--verbose`/`--no-color`/`--non-interactive`/
    `--ci` are declared, not consumed: `diff` reads only the project's own
    board.yaml (`crates/tan-cli/src/commands/diff.rs` never touches
    `GlobalArgs::sdk_root`/`target`/`all`/`verbose`), but the oracle's clap
    `GlobalArgs` are `global = true`, so every verb accepts all of them and a
    caller passing one through unconditionally must not get a parse error.
    """
    del sdk_root, target, all_targets, verbose, no_color, non_interactive, ci
    resolved_format = (
        output_format if output_format is not None else (ctx.obj or {}).get("format") or "text"
    )
    if resolved_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{resolved_format}' (choose from 'text', 'json')", param_hint="--format"
        )
    json_mode = resolved_format == "json"

    root, board_path = resolve_project_paths(project, board_yaml)
    board_file = Path(board_path)

    if not board_file.exists():
        _emit_failure(
            json_mode=json_mode,
            root=root,
            board_path=board_path,
            code="board-yaml-missing",
            message="board.yaml path could not be resolved or the file does not exist.",
            exit_code=ExitCode.VALIDATION_FAILURE,
            text_lines=["diff: board.yaml path is unresolved or missing."],
        )
        return

    try:
        text = board_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        _emit_failure(
            json_mode=json_mode,
            root=root,
            board_path=board_path,
            code="internal-failure",
            message=f"could not read board.yaml: {err}",
            exit_code=ExitCode.INTERNAL_FAILURE,
            text_lines=["diff: internal failure", str(err)],
        )
        return

    try:
        doc = _load_document(text)
        effective_version, os_value, libraries, iot, inference = _parse_fields(doc)
    except ParseFailure as failure:
        header = (
            "diff: internal failure"
            if failure.exit_code == ExitCode.INTERNAL_FAILURE
            else "diff: validation failure"
            if failure.exit_code == ExitCode.VALIDATION_FAILURE
            else "diff: runtime failure"
        )
        _emit_failure(
            json_mode=json_mode,
            root=root,
            board_path=board_path,
            code=failure.code,
            message=failure.message,
            exit_code=failure.exit_code,
            text_lines=[header, failure.message],
        )
        return
    except Exception as err:  # noqa: BLE001 -- the envelope IS the error contract
        message = f"diff failed unexpectedly: {err.__class__.__name__}: {err}"
        _emit_failure(
            json_mode=json_mode,
            root=root,
            board_path=board_path,
            code="internal-failure",
            message=message,
            exit_code=ExitCode.INTERNAL_FAILURE,
            text_lines=["diff: internal failure", message],
        )
        return

    entries = compute_diff_entries(effective_version, os_value, libraries, iot, inference)

    if json_mode:
        emit(
            Envelope(
                "diff",
                Project.resolved(root, board_path),
                _data(board_path, entries),
                [],
                ExitCode.SUCCESS,
            )
        )
    else:
        stream = typer.get_text_stream("stderr")
        for line in _render_text(entries, board_path, quiet):
            stream.write(f"{line}\n")
    raise typer.Exit(int(ExitCode.SUCCESS))
