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

THE MEMBERSHIP BAR, AMENDED (tan-cli#1133, PR #1160 review)
-----------------------------------------------------------

The bar stated below -- "a shape RUN BY more than one consumer module that
can be unified without changing any consumer's pinned contract" -- is a
SUFFICIENT condition, not a necessary one, and #1133 is where that
distinction had to be made explicit. `require_yaml_mapping_doc`,
`read_yaml_mapping` and `require_readable_bytes` each have exactly ONE
consumer module today (`tan/planner/template.py`; `example_catalog.py` reads
JSON only and decodes nothing). Read literally, the bar excludes all three,
and the first cut of #1133 duly defined the two YAML ones as module-level
functions inside `template.py`.

They are here instead, on a SECOND sufficient condition this module already
embodied without ever writing down -- FORMAT SYMMETRY: **when this register
already answers a question for one serialisation, the same question for
another serialisation belongs beside it, not in a consumer.**
`read_catalog_document` is read + parse + shape-check for JSON.
`read_yaml_mapping` is read + parse + shape-check for YAML. Splitting those
across two modules would have meant a reader asking "where does this repo
decide whether a document is the shape it claims to be?" getting two
different answers depending on the file extension -- which is a smaller,
quieter version of exactly the two-implementations-of-one-read problem
tan-cli#1084 created this module to end.

Two arguments that were made for keeping them in `template.py`, recorded
because one of them was simply WRONG and a future round should not re-derive
it as if it were new:

* "The register is standard-library-only, so `import yaml` would put PyYAML
  in the import closure of `tan init`'s SDK-free path." FALSE as stated: it
  is true only of a MODULE-SCOPE import, and this package has an established
  idiom that defeats it. Seven `tan/core/**` modules already defer `import
  yaml` into a function body -- `board_context.py`, `bootstrap.py`,
  `doctor_libraries.py`, `som_buildability.py`, `scaffold.py`,
  `system_manifest.py`, `flash_plan.py` -- and `som_buildability.py:109`
  documents that exact reasoning. `require_yaml_mapping_doc` does the same,
  so the property this module claims stays true and is now enforced by a
  test rather than by the absence of the import.
* "One consumer, so extraction would be for its own sake." Answered by the
  format-symmetry condition above, and by SIZE, which is not a tiebreaker
  here but a real cost: at the time, `template.py` was 2206 lines -- the
  SIXTH-largest module under `python/tan/` (`bootstrap_cmd.py` 4450,
  `doctor_cmd.py` 4415, `flash_cmd.py` 3497, `bootstrap.py` 2558,
  `flash_plan.py` 2273 all sat above it, measured at the commit that first
  wrote this paragraph -- not the fourth-largest, nor the most oversized,
  two claims earlier revisions of this same correction each made and each
  got wrong the same way: naming only some of what actually sat above it)
  -- and `_module_size_budget_core.MIRRORED_PREFIX`'s comment
  ("upstream's to split, not this repo's") was read as barring a split of it
  here. WRONG, corrected in tan-cli#1142 alongside the split that comment was
  misread as forbidding: `MIRRORED_PREFIX` bars a `PINNED_HASHES` module's
  shape from diverging from a moving upstream file it is 3-way-merged
  against; `template.py` is `HAND_PORT_SOURCES` instead, flagged against
  upstream and never merged, so it has no such cost (see
  `tan/planner/template_pins.py`'s own docstring). This module is 400-odd
  lines, well under the cap, and splittable regardless. Keeping 71 lines of
  shared-shape code in a file everyone at the time believed could never shed
  them, to satisfy a bar written to prevent gratuitous extraction, would have
  been the letter of the rule against its purpose.

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
  tan-cli#1133 replaced it -- `targets.py:307-313` still carries it (pre-flight at :307, the raise at
  :308, `yaml.safe_load` at :311, the mapping check at :312-313), and
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
from collections.abc import Callable
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
        self, path: Any, *, what: str,
        absent: str | Callable[[], str] | None = None,
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

        @absent may also be a zero-argument CALLABLE, resolved only if the
        `FileNotFoundError` branch is taken -- see `_unreadable` below.

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
            raise self._unreadable(exc, path, what, absent) from exc

    def require_readable_bytes(
        self, path: Any, *, what: str,
        absent: str | Callable[[], str] | None = None,
    ) -> bytes:
        """@path's RAW BYTES, or the injected curated error -- the same
        contract as `require_readable_text` above for a caller that must not
        decode (tan-cli#1133).

        Its one real caller is `tan/planner/template.py::_rendered_bytes`,
        which copies a template's `files.user_owned` entries verbatim so that
        `alp_template.py render` and `--emit scaffold` hand back the same
        bytes -- a template asset is not required to be text at all, and
        `render()` writes whatever it read. Measured before this fix, on
        3.12.3, 3.13.15 and 3.14.7 alike: a `chmod 000` source escaped
        `emit_scaffold` as a raw `PermissionError`, a deleted one as a raw
        `FileNotFoundError`, and a directory in its place as a raw
        `IsADirectoryError`.

        NOT expressible as `require_readable_bytes` -> `.decode()` for the
        text case, which is why these are two methods rather than one:
        `Path.read_text` opens in TEXT mode and applies universal-newline
        translation, so a CRLF document reaches the caller with `\\n`, while
        `read_bytes().decode("utf-8")` preserves `\\r\\n`. Folding one into
        the other would silently change what every existing caller of the
        text half sees on a CRLF checkout -- exactly the kind of quiet
        behaviour change this family exists to prevent.

        `UnicodeDecodeError` cannot arise here (nothing is decoded), so the
        `except` is `OSError` alone -- narrower than the text half's on
        purpose, and narrow BECAUSE the failure surface is smaller, not
        because the shape was copied without thinking.
        """
        try:
            return path.read_bytes()
        except OSError as exc:
            raise self._unreadable(exc, path, what, absent) from exc

    def _unreadable(
        self, exc: BaseException, path: Any, what: str,
        absent: str | Callable[[], str] | None,
    ) -> Exception:
        """The curated error for a failed read -- ONE definition of the
        message, shared by both read halves above.

        @absent, when the caller passed one, is used for `FileNotFoundError`
        and nothing else: every other failure means the path is there in some
        form and could not be read, so `cannot read ...` is true where "no
        such file" would not be.

        @absent may be a zero-argument CALLABLE as well as a string. The
        rationale lives HERE, in the one function that resolves it and the
        one both read halves and all four widened signatures funnel into,
        rather than on `require_readable_text` above: on the signature it
        would be four copies of one paragraph, and each copy would be a
        place for the next edit to fall out of step with the resolution
        rule it describes.

        It is resolved HERE too -- inside the `FileNotFoundError` branch -- so a
        caller whose absent-message is expensive or itself fallible does not
        pay for it, or trip over it, on the paths that never use it
        (tan-cli#1171 review). `libraries.load_manifest` is the caller that
        needs it: its message ends `Available: <every manifest stem>`, and
        building that list means LISTING `metadata/libraries/`. Eagerly, that
        directory walk ran ahead of the guarded read on every call -- so with
        `metadata/` itself denied it raised a raw `PermissionError` out of
        `Path.is_dir()` on 3.12.3 and 3.13.15 while the guarded read gave the
        curated message on 3.14.7, reinstating the exact tan-cli#1127 split
        this family exists to remove (all three measured). Passed lazily, the
        listing happens only after the read has already answered "not there",
        which is the one branch it describes.
        """
        if absent is not None and isinstance(exc, FileNotFoundError):
            return self.error(absent() if callable(absent) else absent)
        return self.error(
            f"cannot read {what} at {path}: "
            f"{getattr(exc, 'strerror', None) or exc}")

    def require_yaml_mapping_doc(
        self, text: str, *, path: Any, what: str,
    ) -> dict[str, Any]:
        """@text parsed as YAML and known to be a mapping, or the injected
        curated error -- the YAML twin of `read_catalog_document`'s
        `json.JSONDecodeError` arm (tan-cli#1133).

        `yaml.YAMLError` is neither an `OSError` nor a `ValueError`, so no
        `except` clause on any of `tan/planner/template.py`'s three
        outer-document YAML reads had ever covered it: measured through the
        real `emit_scaffold` on 3.12.3, 3.13.15 and 3.14.7, a malformed
        `metadata/e1m_modules/<sku>.yaml`, `metadata/boards/<board>.yaml` or
        template-example `board.yaml` each raised a raw
        `yaml.parser.ParserError` past a caller catching `TemplateError` and
        nothing else.

        The message is `read_catalog_document`'s verbatim with the format
        name swapped -- `malformed {what} at {path}: not valid YAML
        ({problem}, line L column C)` -- because the two answer the same
        question about two serialisations of the same kind of document.
        `problem`/`problem_mark` belong to `MarkedYAMLError` (a scanner or
        parser error); a plain `YAMLError` with neither degrades to its own
        one-line text rather than folding a multi-line dump into a curated
        message.

        `import yaml` is FUNCTION-LOCAL, not module-scope, and that is the
        whole of what keeps this module's stated property true: nothing in
        `tan init`'s SDK-free path calls this method, so PyYAML stays out of
        that path's import closure. The idiom is this package's own, not an
        invention here -- `som_buildability.py`, `board_context.py`,
        `bootstrap.py`, `doctor_libraries.py`, `scaffold.py`,
        `system_manifest.py` and `flash_plan.py` all defer it the same way,
        and `som_buildability.py:109` documents the reasoning verbatim.
        """
        import yaml
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            problem = getattr(exc, "problem", None) or str(exc).replace("\n", " ")
            mark = getattr(exc, "problem_mark", None)
            where = ("" if mark is None else
                     f", line {mark.line + 1} column {mark.column + 1}")
            raise self.error(
                f"malformed {what} at {path}: not valid YAML "
                f"({problem}{where})") from exc
        return self.require_mapping_doc(doc or {}, path=path, what=what)

    def read_yaml_mapping(
        self, path: Any, *, what: str,
        absent: str | Callable[[], str] | None = None,
    ) -> dict[str, Any]:
        """A YAML document at @path, read AND parsed AND known to be a
        mapping -- the exact composite `read_catalog_document` below already
        is for JSON (tan-cli#1133).

        `tan/planner/template.py`'s two `metadata/**` reads used to spell
        this as a pre-flight `if not path.is_file(): raise` followed by a
        completely bare `yaml.safe_load(path.read_text(encoding="utf-8"))` --
        no `try` at any point, which is why the too-narrow-a-`try` sweep
        could not see them.

        The pre-flight is gone rather than supplemented: `Path.is_file()` is
        itself the tan-cli#1127 trap -- against a `chmod 000` PARENT
        directory it raises `PermissionError` on 3.12.3 and 3.13.15 and
        returns `False` on 3.14.7, so one unreadable file was a raw traceback
        on two interpreters and, on the third, a curated but FALSE "no such
        file" (all four cells measured). Classifying on the real exception
        gives one answer on all three: `FileNotFoundError` -> @absent,
        every other read failure -> `cannot read ...`, an unparseable
        document -> `malformed ...`.
        """
        return self.require_yaml_mapping_doc(
            self.require_readable_text(path, what=what, absent=absent),
            path=path, what=what)

    def read_optional_text(self, path: Any, *, what: str) -> str | None:
        """@path's text, or `None` when the document is NOT THERE AT ALL --
        every other read failure still raises the injected curated error
        (tan-cli#1162).

        The one shape `require_readable_text` cannot express: a caller for
        whom an absent document is a legal branch rather than a failure.
        `tan/planner/topology.py::_core_os_choices` is its consumer -- a
        `metadata_root` that carries no `schemas/` of its own (a synthetic
        test root) falls back to the in-tree `BOARD_SCHEMA`, and that
        fallback is the function's own documented contract, not an error
        path.

        Absent is `FileNotFoundError` OR `NotADirectoryError`, and the
        second is not a widening for its own sake: `ENOTDIR` means an
        ancestor of @path is a regular file, so the directory the caller
        was looking in does not exist as a directory -- the same "this root
        carries no such document" fact `ENOENT` reports, arrived at one
        level up. Every OTHER `OSError` (`PermissionError`,
        `IsADirectoryError`, `ELOOP`) and `UnicodeDecodeError` means the
        document IS there and could not be read, which is a failure and
        must not degrade to a silent fallback onto a DIFFERENT document.

        That distinction is the whole reason this is a `try` rather than
        the `is_file()` pre-flight it replaces. `Path.is_file()` answers
        `False` to all of those alike where it answers at all, and on a
        `chmod 000` parent it does not answer at all on two of the three
        interpreters this repo supports (tan-cli#1127) -- so the pre-flight
        version of this branch raised a raw `PermissionError` on 3.12.3 and
        3.13.15 and silently read the wrong schema on 3.14.7.
        """
        try:
            return path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise self._unreadable(exc, path, what, None) from exc

    def require_json_mapping_doc(
        self, text: str, *, path: Any, what: str, noun: str = "a JSON object",
    ) -> dict[str, Any]:
        """@text parsed as JSON and known to be a mapping, or the injected
        curated error -- `read_catalog_document`'s own parse+shape arm,
        pulled out so it is not the catalog's private property
        (tan-cli#1162).

        Extracted for the same FORMAT-SYMMETRY reason the module docstring
        gives for `require_yaml_mapping_doc`, and in the same direction:
        that method already answers "is this text the document it claims to
        be" for YAML, and the JSON half was sitting inline in one
        catalog-specific composite where a second JSON reader could not
        reach it. `tan/planner/zephyr_board.py::_load_soc_spec` and
        `tan/planner/topology.py::_core_os_choices` are that second and
        third reader.

        A MOVE, not a copy: `read_catalog_document` below now calls this,
        so the `not valid JSON` message has one definition rather than two
        that happen to agree. Its own messages are unchanged byte for byte
        -- `what="template catalog"` and the default @noun reproduce
        exactly what tan-cli#1084 landed.

        No `doc or {}` here, deliberately, where `require_yaml_mapping_doc`
        has one: an empty YAML document parses to `None` and legitimately
        means "an empty mapping", while empty JSON text is a
        `json.JSONDecodeError` and a literal `null` is a document that says
        `null` -- refused as `got NoneType`, which is what the catalog has
        always done.
        """
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise self.error(
                f"malformed {what} at {path}: not valid JSON "
                f"({exc.msg}, line {exc.lineno} column {exc.colno})") from exc
        return self.require_mapping_doc(doc, path=path, what=what, noun=noun)

    def read_json_mapping(
        self, path: Any, *, what: str,
        absent: str | Callable[[], str] | None = None,
        noun: str = "a JSON object",
    ) -> dict[str, Any]:
        """A JSON document at @path, read AND parsed AND known to be a
        mapping -- exactly what `read_yaml_mapping` above is for YAML, and
        what `read_catalog_document` below now IS (tan-cli#1162).

        @absent behaves as it does on `require_readable_text`: it is the
        caller's own message for `FileNotFoundError` and nothing else, and
        passing one is what lets a caller drop an `is_file()` pre-flight
        without losing the message that pre-flight used to produce.
        `_load_soc_spec`'s `no SoC spec at <path>` is preserved that way,
        byte for byte.
        """
        return self.require_json_mapping_doc(
            self.require_readable_text(path, what=what, absent=absent),
            path=path, what=what, noun=noun)

    def read_catalog_document(self, path: Any) -> dict[str, Any]:
        """`metadata/templates/catalog-v1.json`, decoded and known to be a
        JSON object -- the read BOTH catalog readers do.

        The file-read half is `require_readable_text` above; `json.
        JSONDecodeError` is a `ValueError` too, so an `except TemplateError`
        never caught it and a half-written catalog reached the user as a
        traceback.

        A one-line call into `read_json_mapping` since tan-cli#1162, which
        pulled the read+parse+shape composite this function WAS out to
        where a second and third JSON reader could use it. Its messages are
        unchanged byte for byte; what changed is that they now have one
        definition. Kept as a named method rather than inlined at its two
        call sites because `what="template catalog"` is the fact those
        callers must not have to spell twice.
        """
        return self.read_json_mapping(path, what="template catalog")

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
