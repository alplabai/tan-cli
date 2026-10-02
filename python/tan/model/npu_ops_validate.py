# SPDX-License-Identifier: Apache-2.0
"""Schema validation for the per-NPU op-support tables `tan model check`
reads (tan-cli#1298).

`metadata/npu_ops/**` used to have no schema, so `analyze.py` carried only
`isinstance` guards. `metadata/schemas/npu-ops-v1.schema.json` now exists on
alp-sdk `dev` (alp-sdk#1801 closed, landed with alp-sdk#1470); a bound SDK may
or may not ship it. This module is the read-path half of tan-cli#964's rule
for that table family: validate on read, always report, and skip-but-disclose
when the schema cannot be used -- the policy lives here, the validator stays
the one in `tan.core.metadata_schema`.

Three outcomes for a table that is about to be used:

* schema usable, table valid -> used.
* schema usable, table violates it -> NOT used (skipped like a malformed
  file, so the backend reads `undetermined` unless another table covers the
  variant), with one note naming the file, JSON pointer and what was found.
* schema absent, unreadable, not valid JSON, or not a valid JSON Schema ->
  the table IS used under `analyze.py`'s isinstance guards, with ONE note per
  backend saying the tables were not validated. A broken schema is no
  evidence against a table, so it is never blamed on one.

Only a table that matches the requested variant is validated; a table for
another variant is neither validated nor mentioned. Selection reads
`applies_to.variant` off a not-yet-validated document, which is safe because
`_table_variant` is isinstance-guarded to return `""` for any wrong shape --
and a table whose variant matches is then validated before it is accepted, so
an unvalidated document can only ever fail to match, never be scored.

The notes ride the static-screen report only. The `--exact` compiled report
and the bench-point report build their own `notes` and do not carry them.
"""

from __future__ import annotations

from pathlib import Path

from tan.core.metadata_schema import (
    missing_schema_note,
    npu_ops_schema_path,
    schema_unusable_reason,
    validate_document,
)

# A badly broken table can violate the schema dozens of times; the first few
# name the problem, the rest just bloat the envelope.
_MAX_VIOLATIONS_SHOWN = 5


def _posix(value: object) -> str:
    return str(value).replace("\\", "/")


class TableValidator:
    """Validates the tables of ONE backend directory against the npu-ops
    schema, working out whether the schema is usable lazily -- on the first
    table that is actually about to be used -- and disclosing "not validated"
    at most once.

    Every path in a note is written relative to the metadata root, with
    forward slashes (`npu_ops/ethos_u/u85@vela-5.2.0.json`,
    `schemas/npu-ops-v1.schema.json`): the note rides the envelope, and an
    absolute host path in it is both noise and a leak. (It also made a test
    whose tmp dir name contained a word the note-scanning tests forbid fail
    on CI, tan-cli#1298.)"""

    def __init__(self, metadata_root: Path | str, table_dir: Path) -> None:
        self._root = Path(metadata_root)
        self._schema_path = npu_ops_schema_path(metadata_root)
        self._table_dir = table_dir
        self._probed = False
        self._usable = False

    def _rel(self, path: Path | str) -> str:
        try:
            return Path(path).relative_to(self._root).as_posix()
        except ValueError:
            return Path(path).name

    def _scrub(self, text: str) -> str:
        """@text with the metadata root's absolute spellings replaced -- a
        defence for a reason string that carries a path despite
        `schema_unusable_reason` already dropping the OS filename."""
        for spelling in {str(self._root), self._root.as_posix(), _posix(self._root)}:
            text = text.replace(spelling, "<metadata>")
        return text

    def _probe(self, notes: list[str]) -> None:
        self._probed = True
        where = f"{self._rel(self._table_dir)}: not validated -- "
        schema = self._rel(self._schema_path)
        reason = schema_unusable_reason(self._schema_path)
        if reason is not None:
            notes.append(f"{where}the schema at {schema} could not be loaded "
                         f"({self._scrub(reason)}); the tables are used under "
                         "the built-in shape checks only")
            return
        try:
            absent = missing_schema_note(self._schema_path, source=self._table_dir) is not None
        except OSError as exc:
            # `Path.is_file()` raises on a permission-denied ancestor on
            # Python 3.12/3.13 (see the comment above `_is_table_file` in
            # analyze.py): "cannot tell" is not "absent".
            notes.append(f"{where}the schema at {schema} could not be inspected "
                         f"({exc.strerror or 'OS error'}); the tables are used "
                         "under the built-in shape checks only")
            return
        if absent:
            notes.append(f"{where}no schema at {schema} in this checkout")
            return
        self._usable = True

    def accepts(self, doc: object, table_path: Path, notes: list[str]) -> bool:
        """True when *table_path* (already parsed to *doc*) may be used.
        Appends to *notes* the not-validated disclosure (once) or this
        table's violation note."""
        if not self._probed:
            self._probe(notes)
        if not self._usable:
            return True
        errors = validate_document(doc, self._schema_path, self._rel(table_path))
        if not errors:
            return True
        shown = "; ".join(errors[:_MAX_VIOLATIONS_SHOWN])
        extra = len(errors) - _MAX_VIOLATIONS_SHOWN
        more = f" (+{extra} more)" if extra > 0 else ""
        notes.append(f"npu-ops table skipped, it fails npu-ops-v1.schema.json: "
                     f"{shown}{more}. This table is not used.")
        return False
