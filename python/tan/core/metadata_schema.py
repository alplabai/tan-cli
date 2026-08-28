# SPDX-License-Identifier: Apache-2.0
"""The ONE place `soc-spec-v1.schema.json` / `som-preset-v1.schema.json`
validation runs -- tan-cli#964.

Before this module, `_schema_errors` (`tan.commands.new_som_cmd`) ran
`jsonschema.Draft202012Validator` against a generated SoC-spec/SoM-preset
skeleton on the `tan new-som` WRITE path only. Every READ consumer --
`tan build`, `tan generate`, `tan presets`, `tan size`, `tan debug-config`,
`tan bootstrap` -- trusted `metadata/socs/**` / `metadata/e1m_modules/**`
unvalidated, so a schema-invalid field (`cores[].type` a number instead of a
string, most often) reached an `isinstance` guard downstream instead of a
coded, readable message. Five rounds of guards across #957/#962/#965/#969
are the measured cost of validating at the point of USE instead of the point
of READ: `presets_cmd.core_type_lookup`, `core/os_class.py`,
`planner/topology.py`, and twice in `planner/kconfig.py` each independently
rediscovered the same missing gate.

tan-cli#964's decided rule (posted as a comment on the issue, not re-derived
here): **validate on every read, always report, refuse only when the invalid
field feeds something the run puts on disk or on silicon.** This module does
the FIRST two verbs -- it validates and it always returns every violation as
a message that names the file. It does NOT decide refuse vs. warn: that is
each caller's call, made by what it DOES with the returned list (raise an
`OrchestratorError` and refuse, or fold the messages into the envelope's
`issues[]` as warnings and continue). Putting the validator in one place and
leaving the policy to the caller is what a "per-consumer guard" is not --
the five guards above were each written at the point a bad value was ABOUT
TO BE USED, independently rediscovering the same missing check; this module
is written at the point the document is READ, once, so nothing downstream of
a successful validation call needs to guard the field's TYPE again (the
`isinstance` guards stay anyway -- tan-cli#965 proved schema-valid can still
crash a consumer whose contract is narrower than the schema; see
`targets.py`'s `npu['mac_per_cycle']`, filed separately).
"""

from __future__ import annotations

import json
from pathlib import Path


def soc_spec_schema_path(metadata_root: Path | str) -> Path:
    """`<sdk_root>/metadata/schemas/soc-spec-v1.schema.json`.

    Takes the METADATA directory (`<sdk_root>/metadata`), not the SDK root --
    every read-path caller (`planner.loader`, `presets_cmd`, `size_cmd`)
    already has that path bound under a name like `metadata_root` or `root`;
    `new_som_cmd`'s own `_soc_schema_path`/`_som_schema_path` take the SDK
    root instead (their write-path callers have THAT bound) and are left as
    they are rather than reconciled onto this convention -- two path helpers
    that each match their own caller's already-bound variable is simpler
    than a `sdk_root` vs. `metadata_root` footgun on one shared name.
    """
    return Path(metadata_root) / "schemas" / "soc-spec-v1.schema.json"


def som_preset_schema_path(metadata_root: Path | str) -> Path:
    """`<sdk_root>/metadata/schemas/som-preset-v1.schema.json`. See
    `soc_spec_schema_path` for the `metadata_root` convention."""
    return Path(metadata_root) / "schemas" / "som-preset-v1.schema.json"


def schema_errors(doc: object, schema_path: Path, *, source: Path | str | None = None) -> list[str]:
    """Validate *doc* against the schema at *schema_path*; return one
    formatted string per violation, sorted by JSON-pointer path, or `[]`
    when it validates clean.

    Moved here from `new_som_cmd._schema_errors` verbatim (tan-cli#964) --
    that function is now a thin alias for this one, so there is exactly one
    `jsonschema.Draft202012Validator(...)` call left in the package, not two
    that could drift on the message format. `new_som_cmd`'s own two call
    sites (the generated-skeleton self-check, run before anything is written)
    pass `source=None` and get the EXACT original message shape back
    (`f"{pointer}: {message}"`) -- unchanged, because that self-check
    describes an in-memory document with no file of its own yet.

    *source*, when given, is the file *doc* was read from, and PREFIXES every
    message (`f"{source}: {pointer}: {message}"`) -- tan-cli#964's CX
    requirement is that a violation name the file, the JSON pointer, and what
    was found, all in one line, with no second lookup to learn which file
    produced it. Every read-path caller (there is more than one file open at
    once in `tan build`'s topology walk) passes a real `source`; only the
    write-path self-check above passes `None`.

    Raises whatever reading/parsing *schema_path* raises (`OSError`,
    `UnicodeDecodeError`, `json.JSONDecodeError`) -- unchanged from the
    original `_schema_errors`, and exactly what `new_som_cmd`'s two call
    sites already catch to report a *schema* problem (`could not read
    <path>`) distinctly from a *document* problem (`_internal_error`).
    Read-path callers that want a schema-file failure folded into the same
    message list instead of an exception use `validate_document` below.
    """
    # tan-cli#810: the only `jsonschema` call in the package (well -- the only
    # one outside `planner/loader.py`'s SEPARATE `board.yaml`-schema pass,
    # which validates a different document against a different schema and
    # was never the gap #964 closes), and it drags in `attr`, `referencing`
    # and `jsonschema_specifications` behind it -- all of which every `tan`
    # invocation used to load for this one line. Deferred here, same as the
    # original. Validating on every READ (not just `new-som`'s write) makes
    # this import land on more invocations than before -- `tan presets`/`tan
    # size`/`tan build`/`tan generate` now pay it too -- but it stays deferred
    # to the point of use rather than promoted to module scope, so a `tan`
    # invocation that never reaches a SoC/SoM read (`tan --version`, `tan
    # doctor` before any metadata walk, ...) still does not pay for it.
    import jsonschema  # noqa: PLC0415

    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    prefix = f"{source}: " if source is not None else ""
    return [
        f"{prefix}{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    ]


def validate_document(doc: object, schema_path: Path, source: Path | str) -> list[str]:
    """Read-path `schema_errors`: never raises.

    Two different "I could not check this" shapes, given two different
    answers, and this is the one place that split lives (tan-cli#964's own
    "must not fire on a checkout that is simply older ... in a way that
    blocks a legitimate customer" AND "a missing or unreadable schema file
    must not become 'everything is valid'" both apply here, to two different
    causes):

    * The schema file is simply ABSENT (`schema_path` does not exist). This
      is the ORDINARY shape of an SDK checkout that predates this schema, or
      a synthetic/partial metadata root a test built -- `presets_cmd.py`'s
      own `_os_choices()` already treats a missing `board.schema.json` the
      same way ("a checkout with no schema of its own answers `None`
      (degrade), not ... a DIFFERENT checkout's enum"), silently, with no
      warning. Matching that existing precedent: `[]`, no message. Measured
      against this file's own tests: without this half, EVERY synthetic
      metadata root the test suite builds (routinely no `metadata/schemas/`
      at all) would carry a `*.metadata-schema-invalid` warning on every
      single read, which is not "the document is wrong", it is "this test
      fixture is deliberately minimal" -- exactly the false-positive noise
      that would erode the signal a REAL violation is supposed to carry.
    * The schema file EXISTS but cannot be read or parsed (permissions,
      truncated, invalid JSON). This is a genuine anomaly on an otherwise-real
      checkout -- unlike the absent case, there is no legitimate reason for
      it, so it becomes ONE synthetic message rather than silently degrading
      to "nothing to report". This is the half that keeps the promise: a
      customer whose checkout's schema file itself is corrupt still gets
      told their document could not be validated, not a false "clean".

    `source` is required (unlike `schema_errors`'s optional one): every
    caller of this function has a real file on disk to name; the one caller
    that does not (`new_som_cmd`'s in-memory self-check) calls `schema_errors`
    directly instead, precisely because it has no `source` to give.
    """
    if not Path(schema_path).is_file():
        return []
    try:
        return schema_errors(doc, schema_path, source=source)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return [f"{source}: could not validate against {schema_path}: {exc}"]
