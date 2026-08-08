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

**YAML 1.1 vs 1.2 boolean literals.** PyYAML's default `SafeLoader` resolves
YAML 1.1's full loose bool vocabulary (`on`/`off`/`yes`/`no`/`y`/`n`, any
case) to `bool`; `serde_yaml` (YAML 1.2 core schema) resolves only the six
canonical `true`/`True`/`TRUE`/`false`/`False`/`FALSE` spellings and leaves
everything else a plain string -- measured against the oracle:
`schemaVersion: 1` + `os: on` is `changes: [{"path":"libraries",...}]` at exit
0 there (`os` is a `String` field, untouched at schema version 1), but the
stock loader hands `_parse_fields` a Python `bool` for `os` and every
`_typed_field(..., str, ...)` check refuses it as exit 2
`diff.schema-violation` -- a live false-refusal for every YAML-1.1-only
boolean spelling in ANY string-typed field (`os`, `preset`), not just this
one example. `_load_document` therefore parses with `_Yaml12BoolLoader`, a
`SafeLoader` subclass with the YAML 1.1 bool resolver's `on`/`off`/`yes`/`no`/
`y`/`n` patterns removed, rather than plain `yaml.safe_load`.

**Scope of the structural checks below.** `_parse_fields` validates the
TOP-LEVEL type of every known `BoardModel` field (is `cores:` a mapping, is
`ipc:` a list, ...) because a real Rust type mismatch anywhere in the document
fails the WHOLE typed parse (`ParseError::Yaml`), and reporting success with a
wrong diff would be a worse defect than an over-eager refusal. It does NOT
validate the shape of values `diff` never reads (`cores.*.peripherals`,
`e1m_routes.*`, ...) -- those fields are never touched by
`normalize_board_model` and never enter this module's output; going a level
deeper than "top-level key has the right YAML kind" would just be more parser
tan does not need for the one question this command answers.

`iot`'s four toggles and `inference.backend`/`inference.default_arena_kib` are
the one exception: they gate `compute_diff_entries`'s pruning decision
directly, so an unchecked wrong type there does not just under-refuse -- it
mis-classifies pruning and fabricates a diff entry the oracle never emits
(measured: `iot: {wifi: "yes"}` used to report `ok:true` with a manufactured
`{"path":"iot","kind":"removed",...}` entry; the oracle is exit 2
`diff.schema-violation`). `iot`'s four toggles must each be `bool` or absent.
`inference.default_arena_kib` must be a non-negative integer (`u32` range) or
absent. `inference.backend` is the one `String` field checked here at all --
and, matching the `os`/`preset` leniency above, it is checked only for the
compound shapes (`list`/`dict`) no `String` field can ever hold; any other
scalar PyYAML resolves it to (even a bare `5` or `true`) is accepted as
non-empty, exactly as the oracle's own String-field coercion treats it
(measured: `inference: {backend: 5}` is `unchanged: true` at exit 0 on the
oracle, never pruned) -- only `_inference_is_empty`'s stringification needed
fixing to stop miscounting a non-`str` truthy `backend` as blank.

`som:`'s own shape (must be a mapping, not a bare SKU string) is checked with
the exact oracle wording via Python's own `repr()` -- which is actually the
*more* correct implementation of the two: the Rust's `python_repr` hand-mimics
Python's `repr()` from a `serde_yaml` (YAML 1.2) value and has one known gap
against real Python semantics on YAML 1.1-vs-1.2 boolean/null resolution
(`tan_core::validate` module docs); this module calls `repr()` on a value
`_Yaml12BoolLoader` (YAML 1.2's narrower bool vocabulary, the same rules
`serde_yaml` uses) actually parsed, so there is nothing left to approximate.

**The SDK cross-check (tan-cli#455).** Everything above only asks whether
`diff` can PARSE the document well enough to normalize it -- a real type at
each field it reads. It has no notion of the SDK's own semantic rules (a SoM
SKU pattern, whether a preset actually exists, the closed `peripherals:`
enum) -- those live in `metadata/schemas/board.schema.json` and are enforced
only by the SDK's own `scripts/validate_board_yaml.py`, which `validate`
spawns and this module, until now, never did. That let a board.yaml
`validate` refuses outright (e.g. an unknown SoM SKU, a nonexistent preset, an
unlisted peripheral token -- four real `jsonschema` violations, measured)
still reach `compute_diff_entries` and report a clean, meaningless
`unchanged: true` -- the same file, the same project, two commands
disagreeing about whether it is usable at all. Fixed by REUSING `validate`'s
own spawn-and-analyze machinery (`analyze_validator_output`, the same
`scripts/validate_board_yaml.py` argv, imported from `validate_cmd` rather
than re-checked here) whenever an SDK has actually resolved: a non-clean
verdict becomes a `ParseFailure("schema-violation", ...)`, flowing through
the exact same failure path a structural mismatch already does. `diff` still
never requires an SDK on its own -- with none resolved (or a stub checkout
missing `validate_board_yaml.py` -- some of this module's own tests hand it
exactly that) it falls back to the structural checks alone, exactly as
before. This is a deliberate divergence from the oracle, not a port gap: the
oracle's own `diff.exe` has the identical contradiction (measured against
`target/debug/tan.exe`) -- `diff.rs` has never called into the SDK's
validator either, so this fix does not chase parity, it changes behaviour.

**Review round (tan-cli#455 follow-up).** The first cut of the cross-check
above swallowed every reason the validator subprocess could fail to produce a
verdict -- a timeout, an unrunnable interpreter -- as a silent no-op, which
falls back to reporting a clean diff exactly as if no cross-check had ever
run: the #455 bug, reopened for a narrower trigger. It also flattened every
non-clean OUTCOME (`missing-preset`, `hardware-revision`, a crashed
validator's `failed`) into `diff.schema-violation`, the same conflation
`validate_cmd.analyze_validator_output` exists to prevent for `validate`
itself. `_reject_if_sdk_validator_disagrees` now mirrors `validate_cmd`'s own
split in full: guard 3 (`resolve_manifest_python_floor` + `_python_too_old`,
refusing BEFORE the spawn that would otherwise crash), a `TimeoutExpired`
that reclassifies to `failed` rather than a silent skip, an unstartable
subprocess that refuses as `spawn-failed` (`RUNTIME_FAILURE`) rather than a
silent skip, and `ParseFailure(result.outcome, ...)` instead of a hardcoded
`"schema-violation"` -- so `diff.failed`/`diff.missing-preset`/
`diff.hardware-revision`/`diff.spawn-failed`/`diff.python-too-old` are now
real, registered outcomes alongside `diff.schema-violation`. The ONLY
remaining silent no-op is the ORIGINAL one this section already documented:
no `validate_board_yaml.py` at the resolved checkout at all -- there is
nothing there to reuse, which is different in kind from a reuse attempt that
started and failed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from tan.commands.build_cmd import _planner_python
from tan.commands.doctor_cmd import resolve_manifest_python_floor
from tan.commands.generate_cmd import _python_too_old
from tan.commands.presets_cmd import resolve_project_paths, resolve_sdk
from tan.commands.sdk_cmd import sdk_resolution_issues
from tan.commands.validate_cmd import (
    OUTCOME_CLEAN,
    OUTCOME_FAILED,
    VALIDATOR_SCRIPT,
    VALIDATOR_TIMEOUT_S,
    _Finding,
    _synthesised_finding,
    analyze_validator_output,
)
from tan.envelope import Envelope, Issue, Project, SdkInfo, emit
from tan.exit_codes import ExitCode
from tan.output_format import FORMAT_HELP, OutputFormat, resolve_format
from tan.core.shapes import yaml_kind

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


#: YAML 1.2 core schema bool literals -- `true`/`True`/`TRUE`/`false`/`False`/
#: `FALSE` only. Everything YAML 1.1 additionally resolved to bool (`on`/
#: `off`/`yes`/`no`/`y`/`n`, any case) is deliberately absent: those are the
#: exact patterns `_yaml_1_2_bool_loader` strips from PyYAML's own resolver,
#: so `re.compile` never sees them either -- one list, not two that could
#: drift apart.
_YAML_1_2_BOOL_PATTERN = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")


def _yaml_1_2_bool_loader(yaml_module: Any) -> type:
    """A `yaml_module.SafeLoader` subclass with the YAML 1.1-only loose bool
    literals removed from implicit resolution, so a scalar like `on`/`off`/
    `yes`/`no`/`y`/`n` (any case) parses as a plain STRING -- matching
    `serde_yaml`'s YAML 1.2 core-schema bool tag (see the module docstring's
    "YAML 1.1 vs 1.2 boolean literals" note). Takes the imported `yaml`
    module rather than importing it itself, so a build with no PyYAML
    installed never touches `yaml.SafeLoader` at all -- `_load_document`
    only calls this after its own optional import already succeeded.
    """

    class Yaml12BoolLoader(yaml_module.SafeLoader):
        pass

    # `add_implicit_resolver` only APPENDS; the stock YAML-1.1 bool resolver
    # would still match first and win. Copy the resolver table with every
    # existing bool entry stripped, then append the narrower one, so this
    # loader's `tag:yaml.org,2002:bool` entries are exactly the six literals
    # above -- nothing from the base `SafeLoader` (used unmodified everywhere
    # else in tan) is touched.
    Yaml12BoolLoader.yaml_implicit_resolvers = {
        first: [pair for pair in resolvers if pair[0] != "tag:yaml.org,2002:bool"]
        for first, resolvers in yaml_module.SafeLoader.yaml_implicit_resolvers.items()
    }
    Yaml12BoolLoader.add_implicit_resolver(
        "tag:yaml.org,2002:bool", _YAML_1_2_BOOL_PATTERN, list("tTfF")
    )
    return Yaml12BoolLoader


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
        return yaml.load(text, Loader=_yaml_1_2_bool_loader(yaml))
    except Exception as err:  # noqa: BLE001 -- yaml.YAMLError and anything a loader raises
        raise ParseFailure("schema-violation", f"board.yaml is not valid YAML: {err}") from err


#: tan-cli#408: one implementation, in `tan.core.shapes` -- `pinmux_cmd.py`
#: carried a byte-identical copy. The module docstring's scope note (this is
#: not a claim of matching serde's exact wording) moved with it.
_yaml_kind = yaml_kind


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


#: `u32::MAX` -- the upper bound `inference.default_arena_kib` (`u32` in the
#: Rust model) accepts. Measured against the oracle: `4294967295` is exit 0,
#: `4294967296` is exit 2 `inference.default_arena_kib: ... expected u32`.
_U32_MAX = 0xFFFFFFFF


def _typed_nested(mapping: dict, key: str, path: str, expected: type, label: str) -> Any:
    """Like `_typed_field`, but for a key nested one level under an
    already-`dict`-shaped `mapping` -- `path` is the dotted diagnostic path
    (`"iot.wifi"`) rather than a bare top-level key."""
    value = mapping.get(key)
    if value is None or isinstance(value, expected):
        return value
    raise ParseFailure(
        "schema-violation",
        f"board.yaml is not valid YAML: {path}: expected {label}, got {_yaml_kind(value)}",
    )


def _check_iot_field_types(iot: dict | None) -> None:
    """Each of `iot`'s four toggles must be `bool` or absent -- checked
    before `compute_diff_entries` ever asks whether the group is prunable
    (see the module docstring's "scope of the structural checks" note)."""
    if iot is None:
        return
    for field in _IOT_FIELDS:
        _typed_nested(iot, field, f"iot.{field}", bool, "a boolean")


def _check_inference_field_types(inference: dict | None) -> None:
    """`inference.default_arena_kib` must be a non-negative `u32`-range
    integer or absent. `inference.backend` is a `String` field: matching the
    `os`/`preset` leniency documented at the top of the module, only the
    compound shapes (`list`/`dict`) no `String` field can ever hold are
    rejected here -- every other scalar PyYAML resolves it to is accepted,
    same as the oracle's own coercion (`_inference_is_empty` is what needed
    fixing to stop mis-treating a non-`str` truthy `backend` as blank)."""
    if inference is None:
        return
    backend = inference.get("backend")
    if isinstance(backend, (list, dict)):
        raise ParseFailure(
            "schema-violation",
            f"board.yaml is not valid YAML: inference.backend: expected a string, "
            f"got {_yaml_kind(backend)}",
        )
    arena = inference.get("default_arena_kib")
    if arena is not None and (
        isinstance(arena, bool) or not isinstance(arena, int) or not 0 <= arena <= _U32_MAX
    ):
        raise ParseFailure(
            "schema-violation",
            "board.yaml is not valid YAML: inference.default_arena_kib: expected a "
            f"non-negative 32-bit integer, got {_yaml_kind(arena)}",
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
    _check_iot_field_types(iot)
    _check_inference_field_types(inference)

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
    """`backend` is empty only when absent or an explicit empty string --
    any OTHER present scalar (`_check_inference_field_types` has already
    rejected the compound shapes) counts as non-empty regardless of its YAML
    type, matching the oracle's own `String`-field coercion (measured:
    `inference: {backend: 5}` is `unchanged: true`, never pruned). Naively
    defaulting a non-`str` `backend` to `""` here -- as this used to -- is
    exactly the bug: it silently treated a present, non-empty `backend` as
    blank and let `compute_diff_entries` fabricate a diff entry the oracle
    never emits."""
    backend = inference.get("backend")
    if backend is not None and backend != "":
        return False
    return inference.get("default_arena_kib") is None


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
    sdk: SdkInfo | None = None,
    sdk_context_issues: list[Issue] | None = None,
) -> None:
    """Mirrors `diff.rs`'s `failure(...)`: the JSON issue message and the
    text-mode lines are independent strings, not one derived from the other
    (`board-yaml-missing`'s text line reads differently from its issue
    message) -- callers supply `text_lines` verbatim, matching the Rust
    call sites' own hand-written `vec![...]`. Unlike the success path's
    `_render_text`, these lines are NOT filtered by `--quiet` -- measured
    against the oracle: `diff --quiet` on every failure prints the identical
    lines a plain `diff` does.

    `sdk` is the resolved `--sdk-root` block, carried through to the failure
    envelope exactly as the success envelope carries it -- measured against
    the oracle: `diff --sdk-root <path>` against a missing board.yaml still
    reports `sdk.root`/`sdk.sourceTier` on the exit-2 envelope; dropping it
    on the failure path (as this used to) is a real divergence, not just an
    asymmetry with the success path.
    """
    if json_mode:
        emit(
            Envelope(
                "diff",
                Project.resolved(root, board_path),
                _data(board_path, [], unchanged=False),
                [*(sdk_context_issues or []), Issue(f"diff.{code}", "error", message)],
                exit_code,
                sdk=sdk,
            )
        )
    else:
        stream = typer.get_text_stream("stderr")
        # tan-cli#478 review finding 6: the JSON envelope has carried the
        # foreign-default pair since the `Envelope` seam landed, but the
        # DEFAULT text path never read `sdk_context_issues` at all -- a
        # customer running plain `tan diff` (no `--format json`) saw only
        # `text_lines` and never learned another project's checkout decided
        # this. Printed first, matching the JSON list's own ordering.
        for issue in sdk_context_issues or []:
            stream.write(f"{issue.message}\n")
        for line in text_lines:
            stream.write(f"{line}\n")
    raise typer.Exit(int(exit_code))


def _spawn_validator(
    python_binary: str, script: str, board_path: str
) -> tuple[str, tuple[Any, ...]]:
    """`(outcome, findings)` for one run of the SDK's own validator, or a
    `ParseFailure("spawn-failed", ...)` when the subprocess could not even be
    started -- `validate_cmd`'s own split (tan-cli#262/#455 review round),
    reused rather than re-derived: a launch that never happened is not a
    verdict, and must not be swallowed into a silent "nothing to report"."""
    command_line = f"{python_binary} {script} --input {board_path}"
    try:
        out = subprocess.run(
            [python_binary, script, "--input", board_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=VALIDATOR_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # The child STARTED, so this is a verdict that never arrived, not a
        # launch failure -- mirrors validate_cmd's own tan-cli#262 shape.
        return OUTCOME_FAILED, (
            _Finding(
                "error",
                f"the SDK validator did not finish within {VALIDATOR_TIMEOUT_S}s "
                f"and was killed: {command_line}",
            ),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as err:
        raise ParseFailure(
            "spawn-failed",
            f"could not run the SDK validator ({command_line}): {err}",
            ExitCode.RUNTIME_FAILURE,
        ) from err
    result = analyze_validator_output(out.returncode, out.stderr)
    if result.outcome != OUTCOME_CLEAN and not result.findings:
        # A non-clean run must never reach a consumer as "zero issues",
        # which reads as no problem -- `to_cli_issues`' own synthesis.
        return result.outcome, (_synthesised_finding(result.outcome, out.stderr),)
    return result.outcome, result.findings


def _reject_if_sdk_validator_disagrees(sdk_info: SdkInfo, root: str, board_path: str) -> None:
    """Raise `ParseFailure(<outcome>, ...)` when the resolved SDK's own
    `scripts/validate_board_yaml.py` -- the exact script/argv `validate`
    spawns -- finds this board.yaml invalid, or its own environment cannot
    even answer that question (tan-cli#455; see the module docstring's "SDK
    cross-check" / "Review round" sections for the false-clean bug this
    closes and why the outcome, not a hardcoded `"schema-violation"`, is what
    reaches the wire).

    A no-op ONLY when there is nothing to reuse at all: no
    `validate_board_yaml.py` at the resolved checkout (a stub/incomplete SDK
    -- this module's own `--sdk-root` tests hand it exactly that, and must
    keep passing unmodified). Every attempt that STARTS and then fails to
    reach a verdict -- guard 3's interpreter floor, an unstartable
    subprocess, a timeout -- refuses instead.
    """
    script = os.path.join(sdk_info.root, *VALIDATOR_SCRIPT)
    if not os.path.isfile(script):
        return
    python_binary = _planner_python(os.path.abspath(root), sdk_info.root)

    # Guard 3 (validate_cmd.py:1013-1015's own): a spawned interpreter below
    # the SDK's declared pythonMinVersion dies inside alp-sdk's
    # `@dataclass(slots=True)` scripts with a traceback -- refuse with the
    # actual defect before that traceback ever happens, rather than let
    # `_spawn_validator` reclassify it to a generic `failed`.
    floor, _floor_source = resolve_manifest_python_floor(sdk_info.root)
    if (too_old := _python_too_old(python_binary, floor)) is not None:
        raise ParseFailure("python-too-old", too_old)

    outcome, findings = _spawn_validator(python_binary, script, board_path)
    if outcome == OUTCOME_CLEAN:
        return
    # `finding.message`, not a `(severity, message)` unpack: tan-cli#498
    # defect 2 turned `validate_cmd`'s findings into a `_Finding` record so a
    # rich `error[ALP-Bxxx]` block's code, hint and source range survive the
    # walk. This module reuses that parser wholesale, so it follows the shape.
    # The bare `.message` is deliberate here -- `diff` folds every finding into
    # ONE sentence and points the reader at `tan validate` for the full
    # diagnostics, which is where the code and hint are rendered.
    detail = "; ".join(finding.message for finding in findings)
    raise ParseFailure(
        outcome,
        "board.yaml is not valid: the SDK's own validator rejects it -- run "
        f"`tan validate` for full diagnostics: {detail}",
    )


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
    sdk_root: str = typer.Option(
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
    output_format: OutputFormat = typer.Option(None, "--format", help=FORMAT_HELP),
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

    `--target`/`--all`/`--verbose`/`--no-color`/`--non-interactive`/`--ci` are
    declared, not consumed: `diff` reads only the project's own board.yaml
    plus, now, `--sdk-root` -- solely to echo the resolved SDK in the
    envelope's `sdk` block, matching the oracle (measured: `diff --sdk-root
    <path>` reports `sdk.root`/`sdk.sourceTier` on both the success AND the
    board-yaml-missing failure envelope; `diff` still never READS anything
    from the checkout). The oracle's clap `GlobalArgs` are `global = true`,
    so every verb accepts all of them and a caller passing one through
    unconditionally must not get a parse error.
    """
    del target, all_targets, verbose, no_color, non_interactive, ci
    resolved_format = resolve_format(output_format, ctx.obj, choices=OutputFormat)
    json_mode = resolved_format == "json"

    root, board_path = resolve_project_paths(project, board_yaml)
    sdk = resolve_sdk(sdk_root, root)
    sdk_info = SdkInfo.from_resolution(sdk.path, sdk) if sdk is not None else None
    # tan-cli#478: `SdkInfo.from_resolution` above carries the pair, and
    # `Envelope.__init__` appends it to the JSON envelope for every command --
    # that seam alone is enough for `--format json`, and dedupes by code, so
    # computing it again here changes nothing there. But `diff`'s text mode
    # never touches `Envelope` at all (`_emit_failure`'s `else` branch and the
    # success branch below both write straight to stderr), so without this
    # the DEFAULT (non-JSON) path stayed silent -- tan-cli#478 review finding
    # 6. Computed here, once, and threaded into both text branches below.
    sdk_context_issues: list[Issue] = (
        sdk_resolution_issues(sdk.broken_project_pin, sdk.tier, sdk.foreign_global_default_for)
        if sdk is not None
        else []
    )
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
            sdk=sdk_info,
            sdk_context_issues=sdk_context_issues,
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
            sdk=sdk_info,
            sdk_context_issues=sdk_context_issues,
        )
        return

    try:
        doc = _load_document(text)
        effective_version, os_value, libraries, iot, inference = _parse_fields(doc)
        if sdk_info is not None:
            _reject_if_sdk_validator_disagrees(sdk_info, root, board_path)
        entries = compute_diff_entries(effective_version, os_value, libraries, iot, inference)
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
            sdk=sdk_info,
            sdk_context_issues=sdk_context_issues,
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
            sdk=sdk_info,
            sdk_context_issues=sdk_context_issues,
        )
        return

    if json_mode:
        emit(
            Envelope(
                "diff",
                Project.resolved(root, board_path),
                _data(board_path, entries),
                sdk_context_issues,
                ExitCode.SUCCESS,
                sdk=sdk_info,
            )
        )
    else:
        stream = typer.get_text_stream("stderr")
        # tan-cli#478 review finding 6: printed unconditionally, unlike the
        # `--quiet`-suppressed entries below -- a foreign-checkout warning is
        # not the "non-essential" output `--quiet` exists to trim.
        for issue in sdk_context_issues:
            stream.write(f"{issue.message}\n")
        for line in _render_text(entries, board_path, quiet):
            stream.write(f"{line}\n")
    raise typer.Exit(int(ExitCode.SUCCESS))
