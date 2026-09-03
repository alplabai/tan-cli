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

tan-cli#1085 closed the CROSS-MODULE half of this family that was still
duplicated by hand (`require_readable_text`, the read-half shape
`render_to_envelope`'s example `board.yaml` read shared with
`read_catalog_document`). It did not shrink `template.py` back toward its
pre-family size: **1548** lines at tan-cli#1001 (`156582e`, before #1025
opened this family at all) -> **1658** at the end of #1025/#1034/#1037/
#1048 -> `1780` (#1073, +122) -> `2156` (#1082, +376) -> `2020` (#1084,
-136) -> `2051 -> 2066` (smaller rounds since) -> `2063` (#1085, -3) ->
`2125` (rounds since) -> **`2219` (tan-cli#1133, +94)**. Net **+671** over
the true 1548-line baseline, re-measured rather than carried forward: the
`+515` this paragraph recorded at #1085 was true then and is not now, and a
count nobody re-measures is the same defect as a caller count nobody
re-measures (below). #1084 remains the only round that ever removed
anything. This module is the piece of that growth two CONSUMER modules
(`template.py` and `example_catalog.py`) both call into through the same
curated-raise contract, and no more:

* `_require_constraints` stays in `template.py`. It guards `$defs/parameter`'s
  `constraints:` bounds, which only the planner's parameter resolution reads;
  `example_catalog.py` has no parameter path at all, so moving it would be
  extraction for its own sake with no second consumer to prove it shared.
  tan-cli#1087 (PR #1123) kept `_check_constraints`'s `ParameterError` local
  for the identical reason.
* `_require_mapping_doc`/`_require_field`/`_require_key`/`require_readable_
  text` belong here. The bar is not "more than one caller" (this module's
  OWN methods calling each other satisfy that trivially and prove nothing)
  and not "a caller in a different module" either (`resolve_targets` below
  never calls `_load_som_doc` at all -- it reimplements it -- so a rule
  keyed on CALLING would wrongly admit that as sharing). The bar is a
  MEMBERSHIP property: **a shape RUN BY more than one consumer module that
  can be unified without changing any consumer's pinned contract.** All
  four here clear it: `template.py` and `example_catalog.py` both run
  `require_mapping_doc` (`template.py` directly; `example_catalog.py` only
  via `read_catalog_document`/`catalog_templates`, which it calls directly
  -- the delegation is load-bearing, confirmed by neutering `require_
  mapping_doc` alone and watching `example_catalog`'s own suite red through
  that path and no other). `require_readable_text` is the newest member and
  the clearest case: the day before this change it was two INLINE copies,
  one in this module and one in `template.py`, both already on the exact
  same `self.error`-injected contract -- nothing to change to unify them,
  which is why they did. tan-cli#1133 gave it a THIRD and FOURTH caller
  (`template.py`'s two `metadata/**` reads, through `_read_yaml_mapping`)
  and one optional argument, `absent`, which only `template.py` passes.
  That asymmetry is deliberate and is NOT a second definition creeping
  back in: the read itself, the `except` tuple and the `cannot read {what}
  at {path}` message stay single-definition, and `absent` only chooses
  which of two messages the ONE `FileNotFoundError` case gets -- those two
  callers have a better one, naming the SKU or the board being resolved,
  and `read_catalog_document` does not.
* `_load_som_doc` and `_board_route_entries` do NOT clear that bar, but not
  because they lack a second caller -- they have two each, WITHIN
  `template.py` (`_load_som_doc`: `_default_preset_for_sku` and
  `_topology_for_sku`; `_board_route_entries`: `_board_alias_to_entry` and
  `_resolve_pin_target`), which is exactly why each is its own function
  instead of two inlined reads. The documents they read ARE read
  independently elsewhere, but "independently" is the operative word --
  none of the rivals below is RUN from this module or from `template.py`;
  each is its own, separately-evolved implementation with its own
  contract, so the membership bar excludes all of them even before the
  "unify without changing a pinned contract" half is reached:
  `tan/model/targets.py::resolve_targets` (`template.py`'s `_load_som_doc`
  comment already names it) reimplements the `is_file()`-then-
  `yaml.safe_load`-then-mapping-check shape `_load_som_doc` HAD until
  tan-cli#1133 replaced it -- `targets.py:307-311` still carries it, and
  the audit script's shape 3 does not report it, because its raises are
  builtins (`FileNotFoundError`/`ValueError`) rather than a curated class;
  a separate contract, not covered by this change -- and it raises a
  bare `FileNotFoundError` for the missing-file case and a bare `ValueError`
  for the malformed case. Only the `ValueError` half is pinned by a test
  (`tests/model/test_targets_malformed_preset.py`, `match="expected a YAML
  mapping"`/`match="malformed SoM preset"`); `grep -rn "no SoM preset for
  SKU" python/tests/` is empty, so the `FileNotFoundError` half's exact
  message is NOT test-pinned today -- only its TYPE is implied by
  `resolve_targets`'s own signature and callers, so this claim covers the
  half that is actually proven and no more. `tan/core/som_buildability.py`'s
  `_safe_load_mapping`-backed read is a THIRD implementation, contracted to
  return `None` on every failure instead ("Never raises"), seeded into
  `tests/gates/test_never_raises_contract_holds.py`. `tan/planner/
  loader.py` (the same eight-section `_ROUTE_SECTIONS` tuple, inline) and
  `tan/planner/project_emit/bom_netlist.py` (the same `e1m_routes:`
  flatten) reimplement `_board_route_entries`'s read too, each to a FOURTH
  contract: neither raises OR returns `None` on a wrong shape -- both
  silently SKIP the one malformed entry and keep the rest
  (`bom_netlist.py:76,79,82`'s three `if not isinstance(...): continue`
  filters; `loader.py:1078`'s `if isinstance(e1m, str) and isinstance(
  macro, str):` value-level filter) -- because both consume a project
  document already schema-validated upstream, a different situation from
  `template.py`'s scaffold-time raw-metadata read. A silent per-entry skip
  is not "no guard at all"; it is its own distinct contract, and one MORE
  reason four independently-evolved readers cannot fold into one shared
  primitive without changing at least one of them: `targets.py`'s
  `FileNotFoundError`/`ValueError`, `som_buildability.py`'s whole-function
  `None`, and `loader.py`/`bom_netlist.py`'s per-entry silent skip are
  four genuinely different answers to "what happens on a malformed read",
  and this register offers exactly one (`self.error`, injected once per
  caller). Left local, with the real rivals named here instead of an
  unqualified "no other module reads it" claim, for a follow-up to weigh
  on purpose rather than by default. `_docs_ref` stays for a third reason
  again: it has one caller and degrades to `"main"` rather than
  curated-raising at all, a different shape from everything else in this
  file.

  Staying local is a scope claim, not a soundness one -- and tan-cli#1085
  left that distinction load-bearing rather than theoretical. At #1085 both
  functions still guarded their OWN absence with a pre-flight `is_file()`
  and then read+parsed UNGUARDED (no `try` at all, so `audit_narrow_except_
  contracts.py`'s too-narrow-a-`try` sweep could not see them) -- a real,
  live gap of the SAME #1116 class `require_readable_text` closes for the
  catalog and the example `board.yaml`, reachable through `emit_scaffold`'s
  `except TemplateError` and nothing else. tan-cli#1133 CLOSED it: both now
  read through `template.py::_read_yaml_mapping`, which calls
  `require_readable_text` here (with `absent=`) and then a local YAML parse
  guard, and neither carries a pre-flight any more. They are still local,
  for the membership reason above and no other; what is gone is the
  soundness debt that used to sit behind that scope claim. The audit script
  grew a third shape, `absent-try`, in the same change, so a fourth
  instance of this is findable rather than invisible.
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

    def require_readable_text(
        self, path: Any, *, what: str, absent: str | None = None,
    ) -> str:
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

        @absent (tan-cli#1133) is the caller's OWN message for the one
        failure a pre-flight `is_file()` used to answer for itself: the file
        is not there. `_load_som_doc`'s `no metadata/e1m_modules/<sku>.yaml
        for sku <sku>` and `_board_route_entries`' `no metadata/boards/
        <board>.yaml for board <board>` are both pinned by live tests
        (`tests/planner/test_render_to_envelope_malformed_example_board.py`),
        and both are more useful than a generic `cannot read`, because they
        name the SKU or board the caller was resolving rather than only the
        path it derived. Passing @absent is therefore what lets a caller DROP
        its pre-flight -- the tan-cli#1127 trap, where `Path.is_file()`
        itself raises `PermissionError` on 3.12.3/3.13.15 and returns `False`
        on 3.14.7, so the same unreadable file was a raw traceback on two
        interpreters and a curated-but-UNTRUE "no such file" on the third
        (both measured on this tree before the fix).

        ONLY `FileNotFoundError` takes @absent, deliberately. Every other
        `OSError` means the path is there in some form and could not be read
        as text -- a directory (`IsADirectoryError`), a parent that is a
        regular file (`NotADirectoryError`), a denied mode
        (`PermissionError`), a symlink loop (`ELOOP`) -- and for all of them
        `cannot read {what} at {path}: {strerror}` is true where "no such
        file" would not be. An `is_file()` pre-flight could not draw that
        line at all: it answered `False` to every one of them.
        """
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            if absent is not None and isinstance(exc, FileNotFoundError):
                raise self.error(absent) from exc
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
