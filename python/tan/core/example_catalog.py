# SPDX-License-Identifier: Apache-2.0
"""Read the SDK scaffold catalog's declared SoM support for ONE example, and
(tan-cli PR #996 Part 2) select a catalog record BY its `cores:` hardware
topology instead of by id or example path.

`find_example_by_cores` below is `tan init`'s customer-facing counterpart to
alp-sdk#1652's `find_template_by_cores` (`scripts/alp_template.py`,
hand-ported into `tan/planner/template.py` for the freshness-gate audit and
`tan.planner_cli --emit scaffold --cores`'s parity mirror). It is
DELIBERATELY a second, small, standalone implementation rather than a call
into `tan.planner.template.find_template_by_cores` -- same reasoning as
`unsupported_som` below: `tan.planner.paths` binds `REPO = sdk_root()` at
MODULE scope, so importing anything under `tan.planner` before
`bind_sdk_root` has run raises `PlannerRootError`, and `init_cmd.py`'s whole
SDK-free path (`tan init --template ...`, no checkout in sight, invariant
I-32) must keep working. The topology match itself is `json.loads(...)` plus
a list scan and a dict-equality check -- duplicating that one screen of pure
logic costs far less than a bind-then-deferred-import dance for every `tan
init` invocation, template-selecting or not.

**Distinct flag, distinct name, on purpose (tan-cli#996 review).** `tan init`
already has a `--cores` flag (`init_cmd.py`) that SPLICES a companion core
onto an already-chosen template's board.yaml -- a different feature entirely
from choosing WHICH template to use by its hardware topology. The customer
surface for this module's function is `--topology`, never `--cores`: the
same flag spelling meaning two different things depending on whether
`--template`/`--from-example` was also given would be a documentation
problem forever, not a one-time naming choice. See `init_cmd.py`'s `init()`
option help text for both flags' cross-reference to the other.

tan-cli#890. `tan init --from-example` copies an example tree and, when
`--som` is given, retargets the copied `board.yaml` onto that SKU. It never
consulted the catalog, so it answered the opposite of the SDK for the same
question:

    $ alp_project.py --emit scaffold --template multicore-mailbox --sku E1M-AEN301
    alp_project: multicore-mailbox: sku 'E1M-AEN301' is not supported
                 (supported: ['E1M-AEN801'])                            rc=1
    $ tan init --from-example multicore/mproc-mailbox --som E1M-AEN301
    exitCode 0 | ok True | issues 0

Measured worse than same-family: `--som E1M-V2N101` retargets that dual-M55
Alif example onto a Renesas part whose topology is `['a55_cluster', 'm33_sm']`
-- so the written `board.yaml` declares `m55_hp` and `m55_he`, neither of
which exists there. `tan validate` does refuse it a command later
(`ALP-B007`), but `init` reported `ok: true` with no issue at all, and an IDE
scaffolding through this path shows the customer green.

WHY THIS READS THE CATALOG DIRECTLY RATHER THAN THROUGH `tan.planner`
---------------------------------------------------------------------

`tan/planner/template.py` already has `load_catalog` and a
`SkuNotSupportedError`, and it is a hash-audited mirror of alp-sdk's
`scripts/alp_template.py` -- so it must not be edited here. Importing it is
also not free: `tan.planner.paths` binds `REPO = sdk_root()` at MODULE scope,
so any `tan.planner.*` import before `bind_sdk_root` raises

    PlannerRootError: tan.planner was imported before `bind_sdk_root(...)`;
    its metadata paths freeze at import time

(measured). Pulling that into `init_cmd` would either break every SDK-free
`tan init` path or need a bind-then-deferred-import dance, for what is
`json.loads(path.read_text())` plus a list scan. `load_catalog` is exactly
that one line, so nothing here duplicates planner LOGIC -- and the lookup
below is by `example` path, which the mirror does not do at all (it looks
records up by `id`).

WHY BOTH READS GO THROUGH `tan/core/document_guards.py` (tan-cli#1084)
----------------------------------------------------------------------

A second implementation of a read is only safe while the two answer the same
question the same way, and this one had stopped. tan-cli#1073 extracted a
malformed-document register into `tan/planner/template.py` and tan-cli#1077
extended it to the catalog, so THAT reader refuses a malformed
`catalog-v1.json` with a curated error naming the file, the field and the
type -- while this one still decoded it through a bare `json.loads` and then
`.get`/subscripted the result. Re-derived on `dev@be3a44b6` before fixing
(`tests/core/test_example_catalog_malformed_catalog.py` carries the full
table): nine shapes crashed RAW here (`JSONDecodeError`, `AttributeError`,
`TypeError`, `KeyError`, `FileNotFoundError`) where the planner refused
cleanly, four more were SILENTLY MIS-READ, and two of them escaped
`unsupported_som`'s own written "Never raises" contract.

Worse than either: `tests/gates/
test_example_catalog_cores_selector_agrees_with_planner.py` asserts the two
selectors AGREE, and covered only well-formed input -- so the divergence was
invisible to CI. Two implementations of one read that agree on good input and
diverge on bad, with a test asserting they agree.

The register therefore MOVED to `tan/core/document_guards.py` -- a module with
no `tan.planner` in its import closure, so the constraint above still holds
and nothing here is a copy. `template.py` binds the same objects, so its call
sites are byte-identical to what #1073/#1077 landed, and neutering one method
there reds tests on BOTH sides. The message shapes, the exception TYPE each
side raises, and what deliberately did NOT move (tan-cli#1085) are all
documented in that module.

THE ONE REMAINING DIVERGENCE, AND THE TWO NEW REFUSALS (tan-cli#1084)
----------------------------------------------------------------------

`find_example_by_cores` excludes a record with no (or a falsy) `example:`
from its match set; `find_template_by_cores` does not filter on that field at
all, because it returns the RECORD where this returns the PATH. So on a
hand-edited catalog whose matching record has no `example:`, the planner still
answers "found" and this answers "not found". That gap is dev's, is unreachable
against a schema-valid catalog (`example` is `required`, `pattern`
`^examples/...`), and is left ALONE here -- closing it would change this
function's documented contract and is a selection question, not the strictness
question #1084 is about. What changed is that it is no longer undocumented:
`test_the_example_field_is_the_one_documented_divergence` pins it by name.

Two refusals ARE new, both of documents the schema never accepted, and both
taken to MATCH the planner rather than invented here:

* the matched record's `id:` is resolved before the ambiguity test, as
  `find_template_by_cores` has done since tan-cli#1077 -- so a unique match
  with a missing or non-string `id:` refuses where dev returned it happily.
  `id` is `required` on every record.
* the matched record's `example:` must be a string. dev ran it through
  `str(...)`, so a record carrying `example: 3` resolved to the literal src
  `'3'` and `tan init` went looking for a directory of that name -- silent,
  and the worst outcome in the whole re-derived table.

WHY A WARNING AND NOT A REFUSAL
--------------------------------

This path has its own written precedent. `--from-example` used to REFUSE an
example with no `board.yaml`, and that "made `tan init --from-example`
unusable for nearly the whole AEN family, which is worse than the original
defect" -- so it warns and still writes every file
(`init.example-missing-board-yaml`). The same reasoning holds here:
`E1M-AEN301` really does carry both `m55_hp` and `m55_he`, so the scaffold it
would have produced is plausibly fine. What was wrong is that NOTHING
CHECKED, and that is what the warning fixes.

WHY `catalog_unreadable` EXISTS, AND WHAT IT COVERS (tan-cli#1101)
--------------------------------------------------------------------

`unsupported_som`'s own contract folds catalog-absent, catalog-unreadable,
catalog-malformed, unreadable-matching-record, no-record-for-this-example,
and sku-is-supported into one indistinguishable `None` -- exactly right for
"should `--from-example` refuse", wrong for "did the check even run".
`catalog_unreadable` is the second, narrower read that answers the second
question, so `--from-example --som` can warn "this check did not run"
instead of reporting `issues: []` on a check that never happened.

It reports two DIFFERENT shapes, and (tan-cli#1101 review MINOR) says so in
two DIFFERENT sentences rather than one that overstates the second:

  * the catalog DOCUMENT itself could not be decoded, or its top-level shape
    (a JSON object, a `templates:` list) does not match its schema --
    non-UTF-8 bytes, a directory where a file was expected, a permissions
    failure (tan-cli#1101 review BLOCKER: a `PermissionError` on a directory
    the caller cannot even `stat()` used to escape a `catalog.exists()`
    pre-flight raw, undoing the #1096 crash-safety this whole issue is
    about -- there is no pre-flight now, matching
    `document_guards.read_catalog_document`'s own "not a pre-flight
    `is_file()`" reasoning), invalid JSON, a non-object document, a
    non-list `templates:`. This is genuinely "could not be read".
  * the document reads fine and a record's `example:` DOES match, but that
    SAME record's `supported:` / `supported.som_skus:` is not the shape its
    schema declares -- missing entirely, the wrong type, or (tan-cli#1101
    review MINOR) an absent key masquerading as "no `supported.som_skus`"
    in `unsupported_som`'s own docstring, which conflated it with the
    genuinely-silent empty-list case. The document was readable; ONE
    record's declared support was not. Calling that "could not be read"
    sends a customer looking for a corrupt file that does not exist.

In both cases the SoM-support check for THIS example could not run, which
is `catalog_unreadable`'s whole question -- unlike a record for a DIFFERENT
example that cannot be read, still skipped rather than fatal
(`_declared_som_skus`'s own per-record-skip precedent, unchanged: a record
whose OWN `example:` is unreadable cannot be attributed to the example being
asked about, so it is simply not evidence either way), and unlike a
well-formed catalog with simply no record for this example at all, which is
`unsupported_som`'s "COMMON case, not an edge one" silence (66 example
directories, 9 catalogued templates) -- there was nothing to check there,
not a check that failed. A catalog that is ABSENT altogether is the same
silence, for a different reason: `unsupported_som`'s own long-standing "no
catalog in this checkout (an older SDK; --from-example worked there before
this gate and must keep working)" precedent -- warning on it would add noise
to every pre-catalog checkout's `--from-example --som`, not close a gap.
Distinguished from every OTHER `MalformedCatalogError` by `err.__cause__`
rather than a pre-flight stat: `read_catalog_document`'s
`except (OSError, UnicodeDecodeError) as exc: raise self.error(...) from
exc` is the only site in the whole guarded read that chains a
`FileNotFoundError` as the cause, so `isinstance(err.__cause__,
FileNotFoundError)` names exactly "the file was never there" and nothing
else -- a directory, a permissions failure, and every record-level shape
error all chain something else (or nothing).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tan.core.document_guards import DocumentGuards

#: The catalog, relative to an alp-sdk checkout root.
CATALOG_RELATIVE = Path("metadata") / "templates" / "catalog-v1.json"


class CoresTopologyError(Exception):
    """Base for every refusal this module's catalog reads raise -- never a
    bare KeyError/ValueError/TypeError escapes to a caller. `init_cmd.py`
    catches each subclass and turns it into an `InitError` with the right
    issue code for that case."""


class MalformedCatalogError(CoresTopologyError, ValueError):
    """`metadata/templates/catalog-v1.json` is not the shape its schema
    declares -- unreadable, not JSON, not a JSON object, or a record/field
    of the wrong type (tan-cli#1084).

    A `ValueError` as well, so the curated message lands in the same family
    as the register `tan/model/targets.py:312-323` established; a
    `CoresTopologyError` as well, so a caller that catches only this
    module's base still cannot be handed a raw traceback. Measured before
    adding the second base: nothing between `find_example_by_cores` and
    `init()` catches a bare `ValueError` (`init_cmd.py:846` and `:1119`
    both do, and neither encloses `_plan_from_topology`'s call), so the MI
    cannot make a refusal disappear into an unrelated handler --
    `test_a_malformed_catalog_is_not_swallowed_by_an_unrelated_handler`
    pins that.

    `find_example_by_cores` RAISES it; `unsupported_som` catches it and
    returns None, which is that function's whole documented contract.
    """


#: This module's binding of the shared malformed-document register --
#: literally the same three checks + two catalog readers
#: `tan/planner/template.py` uses, differing only in the exception class each
#: side's caller contracts for (`init_cmd._plan_from_topology` catches
#: `CoresTopologyError`; `planner/cli._emit_scaffold` catches
#: `TemplateError`). Every curated message below is therefore byte-identical
#: to the planner's for the same malformed document, which is what
#: `tests/gates/test_example_catalog_cores_selector_agrees_with_planner.py`
#: now asserts directly rather than assuming.
_GUARDS = DocumentGuards(MalformedCatalogError)


def unsupported_som(sdk_root: Path, example_src: str, sku: str) -> tuple[str, ...] | None:
    """The catalog's `supported.som_skus` for `example_src`, when `sku` is
    NOT in it. `None` means "nothing to report" -- and that covers every
    reason, deliberately:

      * no catalog in this checkout (an older SDK; `--from-example` worked
        there before this gate and must keep working),
      * catalog present but unparseable, or not the shape expected,
      * no record for this example -- the COMMON case, not an edge one: the
        catalog declares 9 templates while `examples/aen/` alone holds 66
        directories, so an undeclared example has no support set to check
        and inventing one would be this defect pointed the other way,
      * a record whose `supported.som_skus` is present as an empty list
        (`[]`) -- NOT a missing or malformed `supported`/`som_skus` key
        (tan-cli#1101 review MINOR corrected this bullet, which used to
        read as covering both): those raise `MalformedCatalogError` same as
        any other malformed field, so THIS function still folds them into
        `None`, but `catalog_unreadable` (below, see its module-docstring
        section) reports them as a check that could not run, not silence,
      * `sku` is in the set.

    Never raises: a scaffold must not fail because a catalog could not be
    read. The caller reports the returned set as a warning.

    tan-cli#1084: that contract was WRITTEN but not held -- measured on
    `dev@be3a44b6`, `templates: 3` escaped as `TypeError: 'int' object is
    not iterable` and a record with `supported: 3` as `AttributeError:
    'int' object has no attribute 'get'`. Both are now curated by the
    shared register and caught here, so every listed reason really does
    return None. No outcome that was already `None` or a support set
    changes; only the two that raised.
    """
    catalog = sdk_root / CATALOG_RELATIVE
    # The catalog spells it `examples/<src>`; `--from-example` takes `<src>`.
    # Compared with `/` separators on both sides -- `example_src` is already
    # validated as a plain relative path by the caller, and the catalog is
    # authored with forward slashes regardless of host.
    wanted = f"examples/{example_src.strip().strip('/')}"
    try:
        supported = _declared_som_skus(catalog, wanted)
    except MalformedCatalogError:
        return None
    if not supported or sku in supported:
        return None
    return tuple(str(s) for s in supported)


def _declared_som_skus(catalog: Path, wanted: str) -> list[Any] | None:
    """`supported.som_skus` of the record whose `example` is @wanted, or
    None when no record claims it. Raises `MalformedCatalogError` for a
    catalog-level shape problem, or for a malformed MATCH.

    A record whose own `example` cannot be read is SKIPPED rather than
    fatal -- the scan is looking for one specific path, and a record it
    cannot read is not that record. That is exactly what dev's
    `isinstance(record, dict)` + `str(record.get("example", ""))` coercion
    did, kept deliberately so a malformed record cannot silence a warning
    a well-formed one later in the list would have produced.
    """
    doc = _GUARDS.read_catalog_document(catalog)
    for index, record in enumerate(_GUARDS.catalog_templates(doc, path=catalog)):
        field = f"templates[{index}]"
        try:
            example = _GUARDS.require_key(record, "example", str,
                                          doc=catalog, field=field)
        except MalformedCatalogError:
            continue
        if example.strip().strip("/") != wanted:
            continue
        return _GUARDS.require_key(
            _GUARDS.require_key(record, "supported", dict,
                                doc=catalog, field=field),
            "som_skus", list, doc=catalog, field=f"{field}.supported")
    return None


def catalog_unreadable(sdk_root: Path, example_src: str) -> str | None:
    """Whether the SoM-support check for `example_src` could not run at all
    -- see "WHY `catalog_unreadable` EXISTS, AND WHAT IT COVERS" above for
    the full reasoning (tan-cli#1101). `None` for a catalog that is ABSENT
    or that reads cleanly (whether or not it supports `example_src`); the
    already-composed, correctly-led reason string otherwise -- "the SDK
    scaffold catalog could not be read" only for a genuine document-level
    failure, a different lead for a document that read fine but whose
    matching record did not.
    """
    catalog = sdk_root / CATALOG_RELATIVE
    try:
        doc = _GUARDS.read_catalog_document(catalog)
        _GUARDS.catalog_templates(doc, path=catalog)
    except MalformedCatalogError as err:
        if isinstance(err.__cause__, FileNotFoundError):
            return None
        return f"the SDK scaffold catalog could not be read ({err})"
    wanted = f"examples/{example_src.strip().strip('/')}"
    try:
        _declared_som_skus(catalog, wanted)
    except MalformedCatalogError as err:
        return (
            f"the SDK scaffold catalog's declared support for this "
            f"example is not the shape its schema declares ({err})"
        )
    return None


# ---------------------------------------------------------------------------
# `--topology`: select a catalog record BY its cores: hardware topology
# ---------------------------------------------------------------------------


class CoresTopologyNotFoundError(CoresTopologyError):
    """No catalog record's `cores:` topology exactly equals the requested
    one. `known_topologies` names every topology the catalog DOES offer, the
    same "never a bare error, always the candidates" spirit
    `AmbiguousCoresTopologyError` follows for its own case."""

    def __init__(self, cores: dict[str, str], known_topologies: list[tuple[tuple[str, str], ...]]):
        self.cores = cores
        self.known_topologies = known_topologies
        super().__init__(
            f"no catalog template with cores topology {cores!r} "
            f"(known topologies: {known_topologies})"
        )


class AmbiguousCoresTopologyError(CoresTopologyError):
    """More than one catalog record shares the requested `cores:` topology.

    The correct behaviour is to name every candidate and let the customer
    choose with `--template`/`--from-example` -- never to silently pick the
    first. This is NOT a hypothetical: measured against the live alp-sdk
    v0.16.0 catalog, the single-core `{"m55_hp": "zephyr"}` topology (the
    shape a single-M55 AEN scaffold is most likely to ask for) already
    matches five templates (`diagnostics`, `iot`, `minimal`, `peripheral`,
    `sensor`) -- see `tests/planner/test_find_template_by_cores.py::
    test_the_live_catalog_is_ambiguous_for_a_single_m55_hp_zephyr_topology`.
    """

    def __init__(self, cores: dict[str, str], candidate_ids: list[str]):
        self.cores = cores
        self.candidate_ids = candidate_ids
        super().__init__(
            f"cores topology {cores!r} matches multiple catalog templates "
            f"{candidate_ids} -- use --template or --from-example to "
            f"disambiguate"
        )


def _topology(record: Any, index: int, catalog: Path) -> dict[str, str]:
    """One record's `cores:` topology (core id -> os), every shape checked
    on the shared register.

    A LINE-FOR-LINE mirror of `find_template_by_cores`'s own `_topology`
    (`tan/planner/template.py`), down to the `templates[i].cores[j]` field
    labels -- so a malformed record produces the byte-identical curated
    message on both sides. tan-cli#1084: this was
    `{c["id"]: c["os"] for c in record.get("cores") or ()}`, which raised
    `KeyError`/`TypeError` on four shapes and silently mis-read a
    non-string `id` into a topology that could never match.

    `record.get("cores", [])`, not dev's `or ()`: an ABSENT `cores:` still
    degrades to `{}` exactly as before, but a present `cores: null` is
    refused as `got NoneType` rather than silently emptied -- the planner's
    behaviour, and `cores` is `required` with `minItems: 1` in the schema,
    so no catalog that was ever valid stops loading.
    """
    field = f"templates[{index}]"
    _GUARDS.require_field(record, dict, doc=catalog, field=field)
    entries = _GUARDS.require_field(record.get("cores", []), list,
                                    doc=catalog, field=f"{field}.cores")
    return {
        _GUARDS.require_key(core, "id", str, doc=catalog,
                            field=f"{field}.cores[{j}]"):
        _GUARDS.require_key(core, "os", str, doc=catalog,
                            field=f"{field}.cores[{j}]")
        for j, core in enumerate(entries)}


def find_example_by_cores(sdk_root: Path, cores: dict[str, str]) -> str:
    """The `--from-example`-compatible `src` (e.g. `multicore/mailbox`, no
    leading `examples/`) whose catalog record's `cores:` topology is EXACTLY
    `cores` -- `tan init --topology`'s resolution step.

    Raises `CoresTopologyNotFoundError` (no exact match),
    `AmbiguousCoresTopologyError` (more than one) or
    `MalformedCatalogError` (the catalog is not the shape its schema
    declares) -- all three refuse rather than guess, unlike
    `unsupported_som` above, which is a best-effort warning path. There is
    no project yet to scaffold without a real answer here: "which template"
    is not a fact `init` can degrade silently on the way "is this SoM
    supported" can.

    A record with no declared `example` is excluded from the match set
    rather than raising -- the ONE place the two selectors deliberately
    still differ, and dev's own behaviour, kept rather than changed. It and
    the two new refusals this function does add are set out under THE ONE
    REMAINING DIVERGENCE in the module docstring.
    """
    catalog = sdk_root / CATALOG_RELATIVE
    doc = _GUARDS.read_catalog_document(catalog)
    indexed = list(enumerate(_GUARDS.catalog_templates(doc, path=catalog)))
    topologies = {index: _topology(rec, index, catalog)
                  for index, rec in indexed}
    matches = [(index, rec) for index, rec in indexed
               if rec.get("example") and topologies[index] == cores]
    if not matches:
        known = sorted(
            {tuple(sorted(topo.items())) for topo in topologies.values()}
        )
        raise CoresTopologyNotFoundError(cores, known)
    ids = sorted(
        _GUARDS.require_key(rec, "id", str, doc=catalog,
                            field=f"templates[{index}]")
        for index, rec in matches)
    if len(matches) > 1:
        raise AmbiguousCoresTopologyError(cores, ids)
    index, record = matches[0]
    example = _GUARDS.require_key(record, "example", str, doc=catalog,
                                  field=f"templates[{index}]")
    return example.strip().strip("/").removeprefix("examples/")


def parse_topology_arg(raw: str) -> dict[str, str]:
    """`--topology core_id:os[,core_id:os...]` -- STRICT `id:os` pairs, no OS
    inference (unlike `--cores`' splice parser, `parse_cores`): a selector
    must match a catalog record's topology exactly, so an inferred OS could
    silently look up a topology the caller never actually typed. Mirrors
    alp-sdk's `alp_project._parse_cores_arg` (the same parser
    `find_template_by_cores`'s own CLI front door uses).

    Raises `ValueError` on any malformed entry -- the caller wraps this in
    an `InitError` (`init.invalid-topology`).
    """
    cores: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError(
                f"--topology entry {entry!r} must be core_id:os (e.g. "
                f"m55_hp:zephyr)"
            )
        core_id, _, os_name = entry.partition(":")
        core_id, os_name = core_id.strip(), os_name.strip()
        if not core_id or not os_name:
            raise ValueError(
                f"--topology entry {entry!r} must be core_id:os (e.g. "
                f"m55_hp:zephyr)"
            )
        if core_id in cores:
            raise ValueError(f"--topology names {core_id!r} more than once")
        cores[core_id] = os_name
    if not cores:
        raise ValueError("--topology must name at least one core_id:os pair")
    return cores
