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
