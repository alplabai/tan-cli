# SPDX-License-Identifier: Apache-2.0
"""The malformed-document register, in ONE place both catalog readers import.

tan-cli#1084. `metadata/templates/catalog-v1.json` is read by TWO deliberate
implementations -- `tan/planner/template.py` (`load_catalog` +
`find_template_by_cores`) and `tan/core/example_catalog.py`
(`unsupported_som` + `find_example_by_cores`) -- and they cannot be collapsed
into one: `tan.planner.paths` binds `REPO = sdk_root()` at MODULE scope, so
importing anything under `tan.planner` before `bind_sdk_root` has run raises
`PlannerRootError`, and `tan init`'s whole SDK-free path (invariant I-32) must
keep working with no checkout bound at all. `example_catalog.py`'s own module
docstring carries the long form of that reasoning.

PR #1073 extracted the register (`require_mapping_doc` / `require_field`) and
PR #1082 extended it (`require_key`, plus the catalog's own two readers) --
but only inside `tan/planner/template.py`, because that was the module those
issues named. The result was the more dangerous half of a duplicated read:
after #1082 the planner REFUSED a malformed catalog with a curated error while
`example_catalog.py` still crashed raw (`json.JSONDecodeError`,
`AttributeError`, `TypeError`, `KeyError`, `FileNotFoundError`) or silently
mis-read it -- and `tests/gates/
test_example_catalog_cores_selector_agrees_with_planner.py`, which asserts the
two agree, covered only WELL-FORMED input, so the divergence was invisible to
CI. Two implementations of one read that agree on good input and diverge on
bad, with a test asserting they agree.

So the register moved here, to a module with no `tan.planner` in its import
closure and nothing but the standard library in it. It is a MOVE, not a copy:
`template.py` binds the same objects (`_require_field = _GUARDS.require_field`
and friends) so its ~40 call sites are byte-identical to what #1073/#1082
landed, and neutering one method here reds tests across BOTH readers and all
four documents. That is the proof the two are on one register rather than two
that happen to agree today.

WHAT `error` IS FOR
-------------------

The message shape is shared; the exception TYPE is not, and must not be.
`tan/planner/cli._emit_scaffold` catches `TemplateError` and nothing else,
while `tan/commands/init_cmd._plan_from_topology` catches
`CoresTopologyError` and nothing else -- so a register that raised one fixed
class would leave the other caller's curated error escaping as a traceback,
which is the very defect this family exists to close. `DocumentGuards` is
therefore constructed with the class its caller contracts for, once, at module
scope on each side.

THE MESSAGE REGISTER
--------------------

Three shapes, matching the register `tan/model/targets.py:312-323`
established -- each naming the FILE, the FIELD and the actual TYPE:

    malformed {what} at {path}: expected {noun}, got {type}
    {doc} {field} must be {noun}, got {type}
    {doc} {field} is missing required key {key!r}

`noun` on the first defaults to "a YAML mapping" -- byte-identical to what
tan-cli#1034/#1048/#1052 landed for the three YAML documents `template.py`
reads -- and the catalog's two readers pass "a JSON object", because
`catalog-v1.json` is JSON and calling its top level a YAML mapping would be a
curated message that is not true (tan-cli#1077).

`require_key` adds EXACTLY the check the other two cannot express -- the key
is present -- and DELEGATES both type checks to `require_field`: the container
must be a mapping before `key in` means anything, and the value must be @kind
before the caller indexes it. One definition of the type half, one new message
(the missing-key line). Its @kind is optional because `$defs/parameter`'s
`default:` is the one schema-required key with NO declared type -- any JSON
value is legal there -- so `kind=None` requires presence only, never a shape
the schema does not.

`require_readable_text` (tan-cli#1085) is the FOURTH shape: not a type check
at all, but the curated-raise half of "read this file's bytes", shared by
`read_catalog_document` below and by `tan/planner/template.py::
render_to_envelope`'s template-example `board.yaml` read. Both used to run
the identical `except (OSError, UnicodeDecodeError)` body by hand -- one
inside this module, one still in `template.py` after tan-cli#1116 fixed its
narrowed `except OSError` to match -- naming a different `what` but otherwise
byte-for-byte the same. `read_catalog_document` now calls it instead of
inlining the read, so the two curated "cannot read X at Y: Z" messages this
module produces come from one definition instead of two that happened to
still agree.

HOW AN ABSENT FIELD IS NORMALISED IS THE CALLER'S DECISION (tan-cli#1052
review), and `require_field` deliberately checks EXACTLY what it is handed.
The two callers in `template.py` differ on purpose and both are pinned by live
tests:

* The example `board.yaml` sites normalise **`None` only** --
  `[] if raw is None else raw`. An absent field and an explicit `null` pass; a
  PRESENT but illegal falsy scalar (`pins: 0`, `pins: false`, `pins: ''`,
  `cores: 0`) is refused, because it is just as illegal against
  `board.schema.json` as `pins: 3`, and refusing one while silently emptying
  the other is an asymmetry nothing would pin.
* The SoM-preset and board-metadata sites keep their pre-existing
  `doc.get(field) or <empty>`, which collapses EVERY falsy value before this
  helper sees it, so `topology: 0` / `e1m_routes.gpio: 0` still degrade
  silently. That asymmetry is tan-cli#1048's own recorded decision, pinned by
  `test_board_route_entries_malformed_board.py::
  test_a_falsy_scalar_section_value_still_degrades_silently`; changing it is a
  behaviour change to two documents #1052 does not name. It is also defensible
  on BEHAVIOUR: there a falsy section degrades to `[]` and is then caught by
  `_resolve_pin_target`'s curated `has no unambiguous 'board_alias:'`, so
  nothing silently wrong escapes -- where `pins: 0` reached NO downstream
  guard at all. Remove that catch and the asymmetry stops being correct.

The catalog sites use the sentinel form `record.get(key, [])` they already
had: absent degrades to `[]`, present-but-`null` is refused as `got NoneType`.

NOT STRICTER THAN THE SCHEMAS. Every key `require_key` is used on is
`required` in `metadata/schemas/template-catalog-v1.schema.json`, and
`require_field`'s `pins:`-style sites are `type: array` with no `minItems`, so
an empty list still renders -- rejecting it would be a new refusal, not a
fixed crash.

WHAT IS DELIBERATELY NOT HERE
------------------------------

tan-cli#1085 closed the FULL extraction of this family; this module is the
document-agnostic register both catalog readers import, plus `require_
readable_text` (the read-half shape `render_to_envelope`'s example
`board.yaml` read shares with `read_catalog_document`), and no more:

* `_require_constraints` stays in `template.py`. It guards `$defs/parameter`'s
  `constraints:` bounds, which only the planner's parameter resolution reads;
  `example_catalog.py` has no parameter path at all, so moving it would be
  extraction for its own sake with no second caller to prove it shared.
  tan-cli#1087 (PR #1123) kept `_check_constraints`'s `ParameterError` local
  for the identical reason.
* `_load_som_doc`, `_board_route_entries` and `_docs_ref` stay in
  `template.py` too: each still has exactly one caller, and `_docs_ref`
  degrades to `"main"` rather than curated-raising at all, a different shape
  from everything else in this file. A single caller with no sibling to prove
  the sharing is not what this register is for.
* The `SkuNotSupportedError` / `CoresTopologyNotFoundError` selection
  semantics stay with their own modules. This register answers "is this
  document the shape it claims to be", never "which record did you mean".
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["SHAPE_NOUN", "DocumentGuards"]

#: `isinstance` kind -> the noun the curated message uses. Four shapes cover
#: every field guard on both sides: a mapping, a list, a scalar string, and --
#: since tan-cli#1077 reached `constraints.minimum`/`constraints.maximum`, the
#: only numeric fields anything here subscripts -- an integer. `bool`
#: satisfies `int` in Python, so `minimum: true` passes and behaves as
#: `minimum: 1`; that is a curated error rather than a crash, and JSON `true`
#: is refused upstream by the schema's own `"type": "integer"`.
SHAPE_NOUN: dict[type, str] = {
    dict: "a mapping", list: "a list", str: "a string", int: "an integer"}


class DocumentGuards:
    """The register, bound to the exception class ONE caller contracts for.

    See the module docstring for the message shapes, the schema-strictness
    claim, and why the exception type is a constructor argument rather than a
    fixed class.
    """

    def __init__(self, error: type[Exception]) -> None:
        self.error = error

    def require_mapping_doc(
        self, doc: Any, *, path: Any, what: str, noun: str = "a YAML mapping",
    ) -> dict[str, Any]:
        """The OUTER-document half: @doc parsed, but is it a mapping?

        A document that parses (legal YAML/JSON) but is not a mapping
        (illegal against its schema) must not reach the caller's
        `.get(...)`, which raises a raw `AttributeError` a CLI user sees as
        a traceback instead of a curated error naming the file and the
        actual type.
        """
        if not isinstance(doc, dict):
            raise self.error(
                f"malformed {what} at {path}: expected {noun}, "
                f"got {type(doc).__name__}")
        return doc

    def require_field(self, value: Any, kind: type, *, doc: Any, field: str) -> Any:
        """The NESTED-field half: @field of @doc must be @kind before the
        caller indexes or iterates it.

        An outer `require_mapping_doc` says nothing about what is INSIDE the
        mapping, and every round of this family found its next sibling
        exactly one level in. This checks EXACTLY what it is handed --
        normalising an absent field is the caller's decision, and the two
        deliberately differ (module docstring).
        """
        if not isinstance(value, kind):
            raise self.error(
                f"{doc} {field} must be {SHAPE_NOUN[kind]}, got "
                f"{type(value).__name__}")
        return value

    def require_key(
        self, mapping: Any, key: str, kind: type | None = None, *,
        doc: Any, field: str,
    ) -> Any:
        """The MISSING-KEY third: @key is present on @mapping, and its value
        is @kind (or any value, when @kind is None).

        The catalog is read by BARE SUBSCRIPT, so its failure mode is
        `KeyError` -- exactly as raw a traceback as the
        `AttributeError`/`TypeError` the YAML documents produce, but neither
        other method can express it: one checks a whole document, the other
        a value already in hand. Both type checks DELEGATE to
        `require_field`, so the register keeps one definition of them.
        """
        self.require_field(mapping, dict, doc=doc, field=field)
        if key not in mapping:
            raise self.error(
                f"{doc} {field} is missing required key {key!r}")
        value = mapping[key]
        if kind is None:
            return value
        return self.require_field(value, kind, doc=doc, field=f"{field}.{key}")

    def require_readable_text(self, path: Any, *, what: str) -> str:
        """@path, decoded as UTF-8 text, or the injected curated error --
        the read-half tan-cli#1085 pulled out of `read_catalog_document`
        below once `render_to_envelope`'s example `board.yaml` read
        (`tan/planner/template.py`) turned out to run the identical body by
        hand, naming a different @what.

        `except (OSError, UnicodeDecodeError)`, not a pre-flight `is_file()`,
        so a present-but-unreadable path (a directory, a permissions error)
        is named too, not only a missing one. `UnicodeDecodeError` is a
        `ValueError`, NOT an `OSError` -- catching only `OSError` (tan-cli#1096
        review, and tan-cli#1116's own repeat of the same trap in
        `render_to_envelope`) let a non-UTF-8 document escape past a curated
        `except` clause that thought it already covered every failure.
        """
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise self.error(
                f"cannot read {what} at {path}: "
                f"{getattr(exc, 'strerror', None) or exc}") from exc

    def read_catalog_document(self, path: Any) -> dict[str, Any]:
        """`metadata/templates/catalog-v1.json`, decoded and known to be a
        JSON object -- the read BOTH catalog readers do.

        The file-read half is `require_readable_text` above; `json.
        JSONDecodeError` is a `ValueError` too, so an `except TemplateError`
        never caught it and a half-written catalog reached the user as a
        traceback.
        """
        text = self.require_readable_text(path, what="template catalog")
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise self.error(
                f"malformed template catalog at {path}: not valid JSON "
                f"({exc.msg}, line {exc.lineno} column {exc.colno})") from exc
        return self.require_mapping_doc(
            doc, path=path, what="template catalog", noun="a JSON object")

    def catalog_templates(self, doc: Any, *, path: Any) -> list[Any]:
        """`templates:` as a real list, on a catalog that is really a mapping.

        Every `doc.get("templates", ...)` site on both sides iterated
        whatever was there, and the `.get` itself assumed a mapping:
        `templates: 3` was `TypeError: 'int' object is not iterable` and a
        bare-list catalog was `AttributeError: 'list' object has no
        attribute 'get'`.

        ABSENT still degrades to `[]` -- the pre-existing default, which
        each caller already answers with its own not-found error. Only a
        PRESENT non-list is refused, so nothing that read before stops
        reading.
        """
        self.require_mapping_doc(doc, path=path, what="template catalog",
                                 noun="a JSON object")
        return self.require_field(doc.get("templates", []), list,
                                  doc=path, field="templates")
