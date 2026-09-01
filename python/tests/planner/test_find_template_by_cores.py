# SPDX-License-Identifier: Apache-2.0
"""tan-cli PR #996 Part 2: `tan/planner/template.py::find_template_by_cores`
(+ `AmbiguousCoresError`) -- alp-sdk#1652's `--cores` scaffold selector,
ported alongside the six re-pin-only files this same re-sync flagged.

Two kinds of case:

1. Synthetic-`doc` cases (no dependency on the bound checkout's actual
   catalog content, so these stay pinned even as alp-sdk's catalog grows)
   for the not-found / single-match shapes.
2. A REAL-CATALOG regression (`test_the_live_catalog_is_ambiguous_for_a_
   single_m55_hp_zephyr_topology`) proving the ambiguous path is not merely
   a hypothetical this port adds test coverage for out of caution -- the
   catalog this PR's pin advances to (alp-sdk v0.16.0) already has FIVE
   templates (`diagnostics`, `iot`, `minimal`, `peripheral`, `sensor`) that
   share the identical single-core `{"m55_hp": "zephyr"}` topology, so
   `find_template_by_cores` refuses that exact input today, on real data,
   not just a constructed fixture. Every one of the five candidates must be
   named in the refusal -- the whole point of `AmbiguousCoresError` is that
   the customer chooses, never a silent first match.

Importing `tan.planner.template` needs SOME bound alp-sdk root (its package
`__init__` reads `metadata/registries/*` at import time) -- same
requirement as `test_docs_ref_tag_resolution.py` -- even for the synthetic
cases, which never touch that checkout's own catalog content.
"""

from __future__ import annotations

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401 -- `_bound_sdk` imported for its side effect (fixture registration)

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.template requires SOME bound "
           "root (tan/planner_root.py). A SKIP about the missing root, not a "
           "pass.",
)


def _tmpl():
    """Imported inside the call so the module is not imported before
    `bind_sdk_root` has run (collection order)."""
    import tan.planner.template as m
    return m


# ---------------------------------------------------------------------------
# Synthetic-`doc` cases
# ---------------------------------------------------------------------------


def _doc(*records):
    return {"templates": list(records)}


def _rec(rec_id, cores):
    return {"id": rec_id, "cores": [{"id": k, "os": v} for k, v in cores.items()]}


def test_exact_single_match_resolves():
    tmpl = _tmpl()
    doc = _doc(
        _rec("gateway", {"m33_sm": "zephyr"}),
        _rec("multicore-mailbox", {"m55_hp": "zephyr", "m55_he": "zephyr"}),
    )
    record = tmpl.find_template_by_cores(doc, {"m33_sm": "zephyr"})
    assert record["id"] == "gateway"


def test_no_match_names_the_known_topologies():
    tmpl = _tmpl()
    doc = _doc(_rec("gateway", {"m33_sm": "zephyr"}))
    with pytest.raises(tmpl.TemplateNotFoundError, match=r"known topologies"):
        tmpl.find_template_by_cores(doc, {"a55": "yocto"})


def test_ambiguous_synthetic_fixture_names_every_candidate():
    """Two records sharing one topology but doing genuinely different
    things (an RPMsg demo vs a compute-offload demo, the task's own
    example) -- constructed so this assertion does not depend on the
    live catalog ever staying ambiguous the same way."""
    tmpl = _tmpl()
    doc = _doc(
        _rec("rpmsg-demo", {"m55_hp": "zephyr", "a32_cluster": "yocto"}),
        _rec("offload-demo", {"m55_hp": "zephyr", "a32_cluster": "yocto"}),
        _rec("unrelated", {"m33_sm": "zephyr"}),
    )
    with pytest.raises(tmpl.AmbiguousCoresError) as excinfo:
        tmpl.find_template_by_cores(
            doc, {"m55_hp": "zephyr", "a32_cluster": "yocto"})
    message = str(excinfo.value)
    # Every match named -- not just "ambiguous", not just the first hit.
    assert "rpmsg-demo" in message
    assert "offload-demo" in message
    # The genuinely-unrelated third record must NOT be swept in.
    assert "unrelated" not in message
    assert "use --template to disambiguate" in message


def test_ambiguous_cores_error_is_a_template_error():
    """`tan.planner_cli`'s `_emit_scaffold` catches the base `TemplateError`
    for the `--cores` resolution call, matching `alp_project._run_scaffold_
    emit`'s own `except alp_template.TemplateError` -- this only works
    because `AmbiguousCoresError` IS a `TemplateError`, not a sibling."""
    tmpl = _tmpl()
    assert issubclass(tmpl.AmbiguousCoresError, tmpl.TemplateError)


# ---------------------------------------------------------------------------
# Real-catalog regression
# ---------------------------------------------------------------------------


def test_the_live_catalog_is_ambiguous_for_a_single_m55_hp_zephyr_topology():
    """Measured against the bound alp-sdk catalog (`metadata/templates/
    catalog-v1.json`): `{"m55_hp": "zephyr"}` -- the single-core AEN
    topology, the shape a wizard is most likely to ask for -- matches FIVE
    templates today (`diagnostics`, `iot`, `minimal`, `peripheral`,
    `sensor`), not one. This is not a hypothetical the port adds coverage
    for defensively; it is the actual, present behaviour a customer hits
    scaffolding for a single-M55 AEN SoM by topology alone. Every one of the
    five must be named."""
    tmpl = _tmpl()
    doc = tmpl.load_catalog()
    with pytest.raises(tmpl.AmbiguousCoresError) as excinfo:
        tmpl.find_template_by_cores(doc, {"m55_hp": "zephyr"})
    message = str(excinfo.value)
    for expected_id in ("diagnostics", "iot", "minimal", "peripheral", "sensor"):
        assert expected_id in message, (
            f"{expected_id!r} missing from AmbiguousCoresError message: {message}"
        )


def test_the_live_catalog_resolves_an_unambiguous_multicore_topology():
    """The counterpart proof: a topology that only ONE template declares
    (`multicore-mailbox`'s dual-M55 shape) resolves cleanly, so the
    ambiguity above is a property of the single-core shape specifically,
    not of every call into the live catalog."""
    tmpl = _tmpl()
    doc = tmpl.load_catalog()
    record = tmpl.find_template_by_cores(
        doc, {"m55_hp": "zephyr", "m55_he": "zephyr"})
    assert record["id"] == "multicore-mailbox"
