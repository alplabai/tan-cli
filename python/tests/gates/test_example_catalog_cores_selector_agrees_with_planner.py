# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1001 review: `tan/core/example_catalog.py::find_example_by_cores`
is a deliberate second, standalone implementation of `tan/planner/
template.py::find_template_by_cores` -- both resolve a `cores:` hardware
topology to a catalog record, and `example_catalog.py`'s own module
docstring explains why it cannot just call the planner copy (`tan.planner`
binds `REPO = sdk_root()` at MODULE import time, so importing it before
`bind_sdk_root` has run raises `PlannerRootError`, and `tan init`'s SDK-free
path, invariant I-32, must keep working with no checkout bound at all).

That duplication was previously unguarded: `HAND_PORT_HASHES` pins the
planner copy against its alp-sdk source (`scripts/alp_template.py`), but
nothing pinned the SECOND copy against the FIRST, so a re-port of the
upstream selector could move one and not the other with no gate noticing.
Measured, they already disagree in one respect: `find_example_by_cores`
excludes a catalog record with no declared `example` field from its match
set (`example_catalog.py:211`, `r.get("example")`), while
`find_template_by_cores` does not filter on that field at all
(`template.py:168-169`) -- on a hand-edited or corrupted catalog record that
matches a requested topology but carries no `example`, the two would answer
differently (one "not found" or "the other candidates only", the other
"found" or "ambiguous, including this record").

This gate proves the two AGREE on every topology actually present in the
bound alp-sdk catalog today (`metadata/templates/catalog-v1.json`) -- not
that they can never diverge (the `example`-field gap above is real and
undocumented as a difference, just unreachable against a schema-valid
catalog), but that nothing has drifted them apart on live data. A future
re-port of either selector that stops agreeing reds here, by name, instead
of silently shipping two answers to "which template does this topology
resolve to".
"""

from __future__ import annotations

import pytest

from tan.planner_root import bind_sdk_root
from tests.conftest import sdk_root
from tests.core.test_example_catalog_malformed_catalog import (
    _CORES,
    _catalog_path,
    _record,
    _tree,
)

SDK = sdk_root()

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
    "checkout) -- this gate compares two live selectors against the bound "
    "catalog. A SKIP about the missing root, not a pass.",
)


@pytest.fixture(autouse=True)
def _bound_sdk():
    bind_sdk_root(SDK)
    yield


def _known_topologies(doc: dict) -> list[dict[str, str]]:
    """Every distinct `cores:` topology any catalog record declares,
    de-duplicated. Dict order is not hashable, so topologies are collected
    via their sorted-items tuple form and converted back to dicts."""
    seen: set[tuple[tuple[str, str], ...]] = set()
    out: list[dict[str, str]] = []
    for record in doc.get("templates", []):
        topo = {c["id"]: c["os"] for c in record.get("cores", [])}
        key = tuple(sorted(topo.items()))
        if key not in seen:
            seen.add(key)
            out.append(topo)
    return out


def _planner_outcome(doc: dict, cores: dict[str, str]) -> tuple[str, object]:
    """`("found", record)` / `("ambiguous", sorted_ids)` /
    `("not_found", None)` for `find_template_by_cores`."""
    import tan.planner.template as tmpl

    try:
        record = tmpl.find_template_by_cores(doc, cores)
        return ("found", record)
    except tmpl.AmbiguousCoresError as exc:
        return ("ambiguous", _ids_from(exc))
    except tmpl.TemplateNotFoundError:
        return ("not_found", None)


def _ids_from(exc: Exception) -> tuple[str, ...]:
    """Both `AmbiguousCoresError` (planner) and `AmbiguousCoresTopologyError`
    (example_catalog) format their candidate ids into the message text
    (neither carries a structured `.candidate_ids` on the planner side); the
    example_catalog one DOES expose `.candidate_ids` directly, used instead
    of this parse there. Parsing the message is deliberate here rather than
    adding a public attribute to the planner's relocated-verbatim exception
    class, which HAND_PORT_HASHES pins byte-for-byte against alp-sdk's own
    `scripts/alp_template.py` -- changing its shape would be an unrelated
    upstream-parity edit riding along on this gate."""
    import re

    match = re.search(r"matches multiple templates \[(.*?)\]", str(exc))
    if not match:
        return ()
    return tuple(sorted(name.strip().strip("'\"") for name in match.group(1).split(",")))


def _example_catalog_outcome(cores: dict[str, str]) -> tuple[str, object]:
    """`("found", example_src)` / `("ambiguous", sorted_ids)` /
    `("not_found", None)` for `find_example_by_cores`."""
    import tan.core.example_catalog as ec

    try:
        example = ec.find_example_by_cores(SDK, cores)
        return ("found", example)
    except ec.AmbiguousCoresTopologyError as exc:
        return ("ambiguous", tuple(sorted(exc.candidate_ids)))
    except ec.CoresTopologyNotFoundError:
        return ("not_found", None)


def test_both_selectors_agree_on_every_live_catalog_topology():
    """For every topology the bound catalog actually declares: both
    selectors reach the same VERDICT (found / ambiguous / not-found), and
    for "found" the resolved example matches; for "ambiguous" the candidate
    id sets match. A per-topology assertion, not one aggregate boolean, so a
    failure names the exact topology that disagrees."""
    import tan.planner.template as tmpl

    doc = tmpl.load_catalog()
    topologies = _known_topologies(doc)
    assert topologies, "the bound catalog declares no templates at all -- nothing to compare"

    disagreements = []
    for cores in topologies:
        planner_verdict, planner_payload = _planner_outcome(doc, cores)
        catalog_verdict, catalog_payload = _example_catalog_outcome(cores)

        if planner_verdict != catalog_verdict:
            disagreements.append(
                f"{cores!r}: planner={planner_verdict!r} vs "
                f"example_catalog={catalog_verdict!r}"
            )
            continue

        if planner_verdict == "found":
            planner_example = (
                str(planner_payload["example"]).strip().strip("/").removeprefix("examples/")
            )
            if planner_example != catalog_payload:
                disagreements.append(
                    f"{cores!r}: found different examples -- planner="
                    f"{planner_example!r} vs example_catalog={catalog_payload!r}"
                )
        elif planner_verdict == "ambiguous":
            if planner_payload != catalog_payload:
                disagreements.append(
                    f"{cores!r}: ambiguous with different candidates -- "
                    f"planner={planner_payload!r} vs example_catalog={catalog_payload!r}"
                )

    assert not disagreements, (
        "find_template_by_cores and find_example_by_cores disagree on "
        + str(len(disagreements))
        + " live-catalog topology(ies):\n" + "\n".join(disagreements)
    )


# ---------------------------------------------------------------------------
# tan-cli#1084: the agreement above covered only WELL-FORMED input
# ---------------------------------------------------------------------------
#
# That was the dangerous half. Before tan-cli#1077 both selectors were equally
# unguarded, so this file held trivially on malformed input -- both blew up.
# After it, `find_template_by_cores` refused a malformed catalog with a
# curated `TemplateError` naming the file, the field and the type, while
# `find_example_by_cores` still crashed raw (`json.JSONDecodeError`,
# `AttributeError`, `TypeError`, `KeyError`, `FileNotFoundError`) or silently
# mis-read it -- and nothing here could see that, because nothing here fed
# either selector a document that was not already valid.
#
# tan-cli#1084 put both on ONE register (`tan/core/document_guards.py`), so
# the agreement asserted below is BYTE-IDENTICAL MESSAGES, not merely "both
# raise something". The synthetic catalog is imported from
# `tests/core/test_example_catalog_malformed_catalog.py` rather than rebuilt,
# so the two files can never describe different documents while claiming to
# compare the same one. That file covers `example_catalog.py` alone and needs
# no checkout; THIS file is the comparison, and needs a bound one only because
# importing `tan.planner.template` does.


def _planner_malformed_outcome(catalog_path, cores) -> tuple[str, object]:
    """The planner's verdict on a possibly-malformed catalog at
    @catalog_path, with `("malformed", message)` as a fourth outcome."""
    import tan.planner.template as tmpl

    try:
        doc = tmpl.load_catalog(catalog_path=catalog_path)
        record = tmpl.find_template_by_cores(doc, cores, path=catalog_path)
        return ("found", record)
    except tmpl.AmbiguousCoresError as exc:
        return ("ambiguous", _ids_from(exc))
    except tmpl.TemplateNotFoundError:
        return ("not_found", None)
    except tmpl.TemplateError as exc:
        return ("malformed", str(exc))


def _example_catalog_malformed_outcome(root, cores) -> tuple[str, object]:
    """`find_example_by_cores`'s verdict on the same tree, same fourth
    outcome. The `except` order matters and is asserted by the cases below:
    `MalformedCatalogError` is a `CoresTopologyError`, so it must be caught
    before the base would swallow it."""
    import tan.core.example_catalog as ec

    try:
        return ("found", ec.find_example_by_cores(root, cores))
    except ec.MalformedCatalogError as exc:
        return ("malformed", str(exc))
    except ec.AmbiguousCoresTopologyError as exc:
        return ("ambiguous", tuple(sorted(exc.candidate_ids)))
    except ec.CoresTopologyNotFoundError:
        return ("not_found", None)


#: One edit per row of the re-derived table (`tests/core/
#: test_example_catalog_malformed_catalog.py`'s own docstring carries what
#: each did on `dev@be3a44b6`). `None` writes no catalog file at all; a `str`
#: is written verbatim so a row can be illegal JSON; a `dict` is dumped.
_MALFORMED_CATALOGS: list[tuple[str, object]] = [
    ("the catalog file is absent", None),
    ("the catalog is not valid JSON", "{"),
    ("the catalog is a JSON list", "[1, 2]"),
    ("the catalog is a JSON string", '"nope"'),
    ("the catalog is a JSON null", "null"),
    ("templates: 3", {"templates": 3}),
    ("templates: 'abc'", {"templates": "abc"}),
    ("a templates entry is not a mapping", {"templates": [3]}),
    ("cores: 3", {"templates": [_record(cores=3)]}),
    ("cores: 'abc'", {"templates": [_record(cores="abc")]}),
    ("cores: null", {"templates": [_record(cores=None)]}),
    ("a cores entry is not a mapping", {"templates": [_record(cores=[3])]}),
    ("a cores entry has no id",
     {"templates": [_record(cores=[{"os": "zephyr"}])]}),
    ("a cores entry has no os",
     {"templates": [_record(cores=[{"id": "m33_sm"}])]}),
    ("a cores entry has a non-string id",
     {"templates": [_record(cores=[{"id": 3, "os": "zephyr"}])]}),
    ("a cores entry has a non-string os",
     {"templates": [_record(cores=[{"id": "m33_sm", "os": 3}])]}),
    ("the matched record has no id", {"templates": [_record(id=...)]}),
    ("the matched record has a non-string id", {"templates": [_record(id=3)]}),
]


@pytest.mark.parametrize(
    "catalog", [c for _, c in _MALFORMED_CATALOGS],
    ids=[label for label, _ in _MALFORMED_CATALOGS])
def test_both_selectors_refuse_malformed_input_identically(tmp_path, catalog):
    """Both reach the SAME verdict with the SAME message text -- which is
    only possible because both go through the one register. A per-row
    assertion, so a failure names the exact document that disagrees.

    The message is compared BYTE for byte deliberately. "Both raise" would
    have held on `dev@be3a44b6` for several of these rows too (one a curated
    `TemplateError`, the other a raw `KeyError`), which is precisely the
    divergence this file existed and failed to catch."""
    root = _tree(tmp_path, catalog)
    planner = _planner_malformed_outcome(_catalog_path(root), _CORES)
    catalog_side = _example_catalog_malformed_outcome(root, _CORES)

    assert planner[0] == "malformed", planner
    assert planner == catalog_side, (
        f"planner={planner!r} vs example_catalog={catalog_side!r}")


def test_a_wellformed_catalog_still_agrees_through_the_same_harness(tmp_path):
    """The control for the parametrised case above: the harness itself does
    not make everything look malformed."""
    root = _tree(tmp_path, {"templates": [_record()]})
    planner = _planner_malformed_outcome(_catalog_path(root), _CORES)
    assert planner[0] == "found"
    assert _example_catalog_malformed_outcome(root, _CORES) == (
        "found", "peripheral-io/fake1084")


def test_an_absent_templates_key_still_degrades_identically(tmp_path):
    """ABSENT is not malformed on EITHER side -- both keep the pre-existing
    `.get` default and answer not-found. Pinned so a future round cannot
    quietly turn a degradation into a refusal on one side only."""
    root = _tree(tmp_path, {"schemaVersion": 1})
    assert _planner_malformed_outcome(_catalog_path(root), _CORES) == (
        "not_found", None)
    assert _example_catalog_malformed_outcome(root, _CORES) == (
        "not_found", None)


@pytest.mark.parametrize("example,catalog_verdict", [
    (..., "not_found"),   # excluded from the match set
    ("", "not_found"),    # falsy -- same exclusion
    (3, "malformed"),     # present and truthy, so it reaches the guard
])
def test_the_example_field_is_the_one_documented_divergence(
        tmp_path, example, catalog_verdict):
    """The ONE place the two deliberately still differ, pinned by name
    instead of left undocumented as this file's docstring found it.

    `find_example_by_cores` returns the PATH, so it filters records with no
    usable `example:` out of its match set ("cannot tell means silent",
    scoped to the one record -- its own documented posture).
    `find_template_by_cores` returns the RECORD, so it never reads that field
    at all and answers "found". Both are unreachable against a schema-valid
    catalog (`example` is `required` with pattern `^examples/...`), and
    closing the gap would change one selector's documented contract -- a
    SELECTION question, not the strictness question tan-cli#1084 is about, so
    it is recorded here rather than silently altered.

    The `example: 3` row is not dev's: dev returned the literal src `'3'`.
    It refuses now, which is a divergence in VERDICT but no longer a silent
    wrong answer on either side."""
    root = _tree(tmp_path, {"templates": [_record(example=example)]})
    assert _planner_malformed_outcome(_catalog_path(root), _CORES)[0] == "found"
    assert _example_catalog_malformed_outcome(root, _CORES)[0] == catalog_verdict


def test_both_selectors_are_bound_to_the_same_register_objects():
    """The structural half: not "the messages happen to match today" but
    "there is one definition and both bind it". A copy-paste re-divergence --
    the failure mode this whole issue is about -- reds here even if the two
    copies still produce identical text at the moment it lands."""
    import tan.core.example_catalog as ec
    import tan.planner.template as tmpl
    from tan.core.document_guards import DocumentGuards

    for name in ("require_mapping_doc", "require_field", "require_key",
                 "read_catalog_document", "catalog_templates"):
        assert getattr(type(tmpl._GUARDS), name) is getattr(DocumentGuards, name)
        assert getattr(type(ec._GUARDS), name) is getattr(DocumentGuards, name)

    assert tmpl._require_field.__func__ is DocumentGuards.require_field
    assert tmpl._require_key.__func__ is DocumentGuards.require_key
    assert tmpl._catalog_templates.__func__ is DocumentGuards.catalog_templates

    # One register, two exception classes -- deliberately, because
    # `planner/cli._emit_scaffold` catches `TemplateError` and
    # `init_cmd._plan_from_topology` catches `CoresTopologyError`.
    assert tmpl._GUARDS.error is tmpl.TemplateError
    assert ec._GUARDS.error is ec.MalformedCatalogError
