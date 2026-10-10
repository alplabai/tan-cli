# SPDX-License-Identifier: Apache-2.0
"""board.yaml JSON-Schema pass -> rich diagnostics (ALP-B001..B004, B099).

PORTED from alp-sdk `scripts/alp_cli/validator.py` (`_schema_pass` and its
helpers). The schema file is read from the BOUND SDK checkout's
`metadata/schemas/` (ADR-0017: facts stay in alp-sdk) -- the caller passes the
path; nothing here derives a root from `__file__`.
"""

from __future__ import annotations

import json
import re as _re
from difflib import get_close_matches
from pathlib import Path
from typing import Any

import jsonschema

from tan.core.board_diagnostic import Diagnostic, DiagnosticCollector
from tan.core.board_yaml_pos import node_position


def load_board_schema(schema_path: Path) -> dict[str, Any]:
    """Load board.schema.json from *schema_path*."""
    return json.loads(schema_path.read_text(encoding="utf-8"))


def iter_schema_errors(
    data: dict[str, Any], schema_path: Path
) -> list[jsonschema.ValidationError]:
    """Validate *data* against board.schema.json; errors sorted by path.

    One schema file, one draft dialect (2020-12, matching the schema's own
    `$schema` declaration), one error ordering.
    """
    validator = jsonschema.Draft202012Validator(load_board_schema(schema_path))
    # Stringify path parts: absolute_path mixes ints (array indices) and
    # strs (keys); a raw list comparison would TypeError across siblings.
    return sorted(validator.iter_errors(data),
                  key=lambda e: [str(p) for p in e.absolute_path])


def _schema_pass(
    data: dict[str, Any],
    path: Path,
    collector: DiagnosticCollector,
    *,
    schema_path: Path,
) -> None:
    # Strip __pos__ keys before handing to jsonschema (they're not in the schema).
    clean = _strip_pos(data)
    for err in iter_schema_errors(clean, schema_path):
        diag = _schema_error_to_diagnostic(err, data, path)
        if diag is not None:
            collector.add(diag)


def _strip_pos(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _strip_pos(v)
            for k, v in value.items()
            if not (isinstance(k, str) and k.startswith("__"))
        }
    if isinstance(value, list):
        return [_strip_pos(v) for v in value]
    return value


def _walk(data: dict[str, Any], path_seq: list[Any]) -> dict[str, Any] | None:
    """Walk a path through the position-augmented document."""
    cursor: Any = data
    for step in path_seq:
        if isinstance(cursor, dict) and step in cursor:
            cursor = cursor[step]
        elif isinstance(cursor, list) and isinstance(step, int) and step < len(cursor):
            cursor = cursor[step]
        else:
            return None
    return cursor if isinstance(cursor, dict) else None


def _schema_error_to_diagnostic(
    err: jsonschema.ValidationError, data: dict[str, Any], path: Path
) -> Diagnostic | None:
    abs_path = list(err.absolute_path)
    parent = _walk(data, abs_path[:-1]) if abs_path else data
    line = parent.get("__line__", 1) if parent else 1
    col = parent.get("__column__", 1) if parent else 1
    span = 1

    if err.validator == "required":
        missing = err.message.split("'")[1] if "'" in err.message else "?"
        return Diagnostic(
            severity="error",
            path=path,
            line=line,
            col=col,
            span=span,
            code="ALP-B001",
            message=f"required key '{missing}' is missing",
            hint=f"add a '{missing}:' entry to this block",
        )

    if err.validator == "additionalProperties":
        # jsonschema reports additionalProperties errors AT the object that
        # rejected the extra key: `abs_path` is that object's OWN path, not
        # the offending key's -- so the offending key is only ever
        # recoverable from the message text, never from `abs_path[-1]`.
        # (Nested case: a `server:` block with `additionalProperties:
        # false` rejecting `tls_ca_bundle` reports `abs_path ==
        # ["ota", "server"]`; `abs_path[-1]` is `"server"`, the block
        # itself, not the unknown key -- which used to surface as the
        # misleading "unknown key 'server'".)
        _m = _re.search(r"'([^']+)'", err.message)
        bad_key = _m.group(1) if _m else "?"
        offending = _walk(data, abs_path) if abs_path else data
        if offending and "__keys__" in offending and bad_key in offending["__keys__"]:
            parent = offending
            line, col = node_position(parent, bad_key, target="key")
            span = len(str(bad_key))
        allowed = list(err.schema.get("properties", {}).keys())

        suggestion = get_close_matches(str(bad_key), allowed, n=1)
        hint = f"did you mean '{suggestion[0]}'?" if suggestion else None
        return Diagnostic(
            severity="error",
            path=path,
            line=line,
            col=col,
            span=span,
            code="ALP-B002",
            message=f"unknown key '{bad_key}'",
            hint=hint,
        )

    if err.validator in {"enum", "pattern"}:
        if abs_path and parent and "__keys__" in parent:
            key = abs_path[-1]
            if key in parent["__keys__"]:
                line, col = node_position(parent, key, target="value")
                span = max(1, len(str(parent.get(key, ""))))
        if err.validator == "enum":
            allowed = err.schema.get("enum", [])
            hint = f"expected one of: {', '.join(map(repr, allowed))}"
        else:
            hint = f"value must match pattern: {err.schema.get('pattern')}"
        return Diagnostic(
            severity="error",
            path=path,
            line=line,
            col=col,
            span=span,
            code="ALP-B003",
            message=err.message,
            hint=hint,
        )

    if err.validator == "type":
        if abs_path and parent and "__keys__" in parent:
            key = abs_path[-1]
            if key in parent["__keys__"]:
                line, col = node_position(parent, key, target="value")
        return Diagnostic(
            severity="error",
            path=path,
            line=line,
            col=col,
            span=1,
            code="ALP-B004",
            message=err.message,
            hint=f"expected type: {err.schema.get('type')}",
        )

    # Fallback for any validator we haven't mapped yet.
    return Diagnostic(
        severity="error",
        path=path,
        line=line,
        col=col,
        span=1,
        code="ALP-B099",
        message=err.message,
        hint=None,
    )
