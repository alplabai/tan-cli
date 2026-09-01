# SPDX-License-Identifier: Apache-2.0
"""`tan.planner.sdk_capability` unit tests (tan-cli#591).

Every probe under test reads only a plain file under a `metadata_root` this
module builds itself with `tmp_path` -- none of it depends on what checkout
`ALP_SDK_ROOT` actually points at. The bound-SDK machinery below exists
solely because `tan.planner.sdk_capability` is a submodule of the `tan.planner`
PACKAGE, whose `__init__` requires a bound root at import time
(`tan/planner_root.py`); binding to the SAME root every other planner test
module binds to (rather than a throwaway path) is what keeps this file from
colliding with `tests/planner/test_zephyr_board_metadata_facts.py` over the
one process-wide binding when both run in the same session.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401 -- `_bound_sdk` imported for its side effect (fixture registration)

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set (or does not point at a real alp-sdk "
           "checkout) -- importing tan.planner.sdk_capability requires SOME "
           "bound root (tan/planner_root.py), even though every probe under "
           "test here reads only a tmp_path fixture, never the bound "
           "checkout's own content. A SKIP about the missing root, not a "
           "pass.",
)


def _mod():
    """Imported inside the call so the module imports before `bind_sdk_root`
    has run (collection order) -- same reason `test_zephyr_board_metadata_facts
    .py`'s `_emit`/`_emit_error` helpers do it lazily."""
    import tan.planner.sdk_capability as m

    return m


def _write_json(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


# ======================================================================
# schema_declares_property
# ======================================================================


def test_schema_declares_property_true_when_present(tmp_path: Path):
    _write_json(
        tmp_path / "schemas" / "soc-spec-v1.schema.json",
        {"properties": {"zephyr_peripherals_dtsi": {"type": "string"}}},
    )
    probe = _mod().schema_declares_property(
        "soc-spec-v1.schema.json", "zephyr_peripherals_dtsi")
    assert probe(tmp_path) is True


def test_schema_declares_property_false_when_absent(tmp_path: Path):
    _write_json(
        tmp_path / "schemas" / "soc-spec-v1.schema.json",
        {"properties": {"part": {"type": "string"}}},
    )
    probe = _mod().schema_declares_property(
        "soc-spec-v1.schema.json", "zephyr_peripherals_dtsi")
    assert probe(tmp_path) is False


def test_schema_declares_property_false_when_schema_file_is_missing(tmp_path: Path):
    probe = _mod().schema_declares_property(
        "soc-spec-v1.schema.json", "zephyr_peripherals_dtsi")
    assert probe(tmp_path) is False


def test_schema_declares_property_false_on_malformed_json(tmp_path: Path):
    path = tmp_path / "schemas" / "soc-spec-v1.schema.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    probe = _mod().schema_declares_property(
        "soc-spec-v1.schema.json", "zephyr_peripherals_dtsi")
    assert probe(tmp_path) is False


# ======================================================================
# quality_task_declared
# ======================================================================


def test_quality_task_declared_true_when_listed(tmp_path: Path):
    _write_json(
        tmp_path / "quality-tasks-v1.json",
        {"tasks": [{"id": "agents-md-generators"}, {"id": "atoc-reservation"}]},
    )
    probe = _mod().quality_task_declared("atoc-reservation")
    assert probe(tmp_path) is True


def test_quality_task_declared_false_when_not_listed(tmp_path: Path):
    _write_json(
        tmp_path / "quality-tasks-v1.json",
        {"tasks": [{"id": "agents-md-generators"}]},
    )
    probe = _mod().quality_task_declared("atoc-reservation")
    assert probe(tmp_path) is False


def test_quality_task_declared_false_when_registry_is_missing(tmp_path: Path):
    probe = _mod().quality_task_declared("atoc-reservation")
    assert probe(tmp_path) is False


def test_quality_task_declared_false_when_tasks_is_not_a_list(tmp_path: Path):
    _write_json(tmp_path / "quality-tasks-v1.json", {"tasks": "atoc-reservation"})
    probe = _mod().quality_task_declared("atoc-reservation")
    assert probe(tmp_path) is False


# ======================================================================
# require_capability / SdkTooOldError
# ======================================================================


def _capability(present: bool):
    return _mod().SdkCapability(
        id="test.capability",
        issue="alp-sdk#9999",
        description="the test capability",
        probe=lambda _root: present,
    )


def test_require_capability_is_a_noop_when_the_probe_is_true(tmp_path: Path):
    _mod().require_capability(tmp_path, _capability(present=True))  # must not raise


def test_require_capability_raises_sdk_too_old_when_the_probe_is_false(tmp_path: Path):
    m = _mod()
    capability = _capability(present=False)
    with pytest.raises(m.SdkTooOldError) as excinfo:
        m.require_capability(tmp_path, capability)
    assert excinfo.value.capability is capability
    message = str(excinfo.value)
    assert "this alp-sdk predates the test capability (alp-sdk#9999)" in message
    assert "upgrade alp-sdk" in message
    assert "pin tan to a release that predates the requirement" in message


def test_sdk_too_old_message_uses_the_capabilitys_own_emit_description():
    m = _mod()
    capability = m.SdkCapability(
        id="test.other",
        issue="alp-sdk#1",
        description="some other fact",
        probe=lambda _root: False,
        emit_description="the widget emit",
    )
    assert "the widget emit needs a checkout containing it" in str(m.SdkTooOldError(capability))
