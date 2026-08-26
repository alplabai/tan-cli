# SPDX-License-Identifier: Apache-2.0
"""`--metadata-root` threading in `topology.py`/`validate.py` (alp-sdk#1485,
tan-cli#909).

alp-sdk `85b6b905a` (#1485, "resolve every alp_orchestrate site against
--metadata-root") fixed `_core_os_choices()` reading a fixed in-tree
`BOARD_SCHEMA` regardless of a project's `--metadata-root` override. The
upstream delta landed in `tan/planner/topology.py` + `tan/planner/validate.py`
via tan-cli#868's earlier resync (934e74ee) -- `_core_os_choices(metadata_root)`
now takes the root as a REQUIRED, `lru_cache`-keyed parameter, falls back to
the in-tree `BOARD_SCHEMA` only when *metadata_root* carries no `schemas/` of
its own, and raises `OrchestratorError` (not a raw `FileNotFoundError`) when
neither resolves; `_allowed_os_for_core` and `_enforce_loader_rules` (in
`validate.py`) both gained the same parameter and thread it through.

That port shipped with no dedicated test: `python/tests/core/
test_metadata_root_override.py` covers OTHER `--metadata-root` call sites
(the storage/memory-map resolvers) but not these three, and
`tests/parity/test_planner_emit_parity.py`'s `core_os_topology` equality check
compares tan against the oracle on the SAME (bound) root both times, so it
cannot catch a silent fall-through to the wrong tree either. This file is that
missing proof, at the unit level: a synthetic `metadata_root` whose
`schemas/board.schema.json` declares a `core_entry.os` enum the BOUND SDK's
own schema does not, so a correct answer can only come from the parameter
actually being read.

Mutation-proven: reverting `_core_os_choices` to ignore *metadata_root* (read
`BOARD_SCHEMA` unconditionally) turns every "the alternate tree wins" assertion
below red; restoring the current source turns them green.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _sdk_root() -> Path | None:
    for var in ("ALP_SDK_PARITY_ROOT", "ALP_SDK_ROOT"):
        raw = os.environ.get(var)
        if raw and (Path(raw) / "scripts" / "alp_project.py").is_file():
            return Path(raw).resolve()
    return None


SDK = _sdk_root()

_SKIP_REASON = (
    "set ALP_SDK_ROOT to an alp-sdk checkout so tan.planner can bind a "
    "root and import (same requirement as test_metadata_root_override.py)"
)

#: An `os:` value the bound SDK's real board schema does not enumerate today
#: (`['zephyr', 'yocto', 'baremetal', 'off']`, measured against alp-sdk
#: v0.16.0) -- picking a value the fixture ADDS, rather than one already
#: legal, is what makes "the alternate tree's answer differs from the bound
#: tree's" provable at all.
_EXTRA_OS = "quarantine"


@pytest.fixture(scope="module")
def planner():
    if SDK is None:
        pytest.skip(reason=_SKIP_REASON)
    from tan.planner_root import bind_sdk_root

    bind_sdk_root(SDK)
    import tan.planner as planner_pkg

    return planner_pkg


@pytest.fixture()
def alternate_schema_root(tmp_path: Path) -> Path:
    """A metadata root carrying ONLY `schemas/board.schema.json`, copied from
    the bound SDK's real schema with one extra `core_entry.os` enum value
    spliced in.

    Deliberately not a full `metadata/` copy (unlike `test_metadata_root_
    override.py`'s `alternate_tree`): `_core_os_choices` reads nothing else,
    so a minimal root keeps the fixture's failure surface -- and the reader's
    job of confirming what changed -- to exactly the file under test.
    """
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    src = SDK / "metadata" / "schemas" / "board.schema.json"
    doc = json.loads(src.read_text(encoding="utf-8"))
    enum = doc["$defs"]["core_entry"]["properties"]["os"]["enum"]
    assert _EXTRA_OS not in enum, (
        f"{_EXTRA_OS!r} is already a legal os: value in the bound SDK's "
        "board schema -- the fixture's premise moved; pick another sentinel"
    )
    enum.append(_EXTRA_OS)
    (schemas / "board.schema.json").write_text(
        json.dumps(doc, indent=2), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture()
def schemaless_root(tmp_path: Path) -> Path:
    """A metadata root with no `schemas/` directory at all -- the fallback
    case (`loader._validate_board` accepts the same shape)."""
    return tmp_path


def test_core_os_choices_reads_the_given_root_not_the_bound_one(
    planner, alternate_schema_root
):
    from tan.planner.paths import METADATA_ROOT
    from tan.planner.topology import _core_os_choices

    bound = _core_os_choices(METADATA_ROOT)
    alt = _core_os_choices(alternate_schema_root)
    assert _EXTRA_OS not in bound
    assert _EXTRA_OS in alt
    assert set(alt) - set(bound) == {_EXTRA_OS}


def test_core_os_choices_falls_back_to_the_in_tree_schema_when_root_has_none(
    planner, schemaless_root
):
    """A metadata root with no `schemas/board.schema.json` of its own (a
    synthetic/test root) must resolve via the in-tree `BOARD_SCHEMA` rather
    than raising -- matching `loader._validate_board`'s own fallback."""
    from tan.planner.paths import BOARD_SCHEMA, METADATA_ROOT
    from tan.planner.topology import _core_os_choices

    assert _core_os_choices(schemaless_root) == _core_os_choices(METADATA_ROOT)
    # And that the fallback really is BOARD_SCHEMA's content, not a fluke of
    # the two schemas happening to agree:
    schema = json.loads(BOARD_SCHEMA.read_text(encoding="utf-8"))
    assert tuple(schema["$defs"]["core_entry"]["properties"]["os"]["enum"]) == (
        _core_os_choices(schemaless_root)
    )


def test_core_os_choices_raises_orchestrator_error_not_file_not_found(
    planner, schemaless_root, monkeypatch
):
    """When NEITHER *metadata_root* nor the in-tree fallback has a schema,
    the failure must be a coded `OrchestratorError` a caller can report,
    never a raw `FileNotFoundError` escaping to the top level."""
    from tan.planner import topology
    from tan.planner.models import OrchestratorError
    from tan.planner.topology import _core_os_choices

    _core_os_choices.cache_clear()
    monkeypatch.setattr(topology, "BOARD_SCHEMA", schemaless_root / "nonexistent.json")
    with pytest.raises(OrchestratorError):
        _core_os_choices(schemaless_root / "also-nonexistent")
    _core_os_choices.cache_clear()


def test_allowed_os_for_core_carries_the_root_through(planner, alternate_schema_root):
    """`_allowed_os_for_core` is `_core_os_choices` minus the other core
    class's runtime -- confirms the parameter survives that second hop
    (issue #1485 also touched this call site)."""
    from tan.planner.paths import METADATA_ROOT
    from tan.planner.topology import _allowed_os_for_core

    # Cortex-M: yocto (the OTHER class's runtime) is excluded either way;
    # `_EXTRA_OS` is neither class's runtime, so it survives that exclusion
    # and is visible ONLY through the alternate root.
    bound = _allowed_os_for_core("cortex-m33", METADATA_ROOT)
    alt = _allowed_os_for_core("cortex-m33", alternate_schema_root)
    assert "yocto" not in bound and "yocto" not in alt
    assert _EXTRA_OS not in bound
    assert _EXTRA_OS in alt


def test_enforce_loader_rules_accepts_an_os_only_the_alternate_root_declares(
    planner, alternate_schema_root
):
    """The loader-rules gate (`validate._enforce_loader_rules`) is the actual
    consumer `load_board_yaml` calls. An `os:` value that is legal ONLY under
    the alternate root must be accepted when *metadata_root* names that root,
    and rejected -- as an unknown os -- against the bound SDK's own tree.
    Before alp-sdk#1485's fix (a fixed in-tree default `_core_os_choices()`
    call), the alternate-root case below would have raised too."""
    from tan.planner.models import OrchestratorError, Slice
    from tan.planner.paths import METADATA_ROOT
    from tan.planner.validate import _enforce_loader_rules

    slice_ = Slice(core_id="m33_sm", os=_EXTRA_OS)

    _enforce_loader_rules(slice_, alternate_schema_root)  # must not raise

    with pytest.raises(OrchestratorError, match="unknown os"):
        _enforce_loader_rules(slice_, METADATA_ROOT)


def test_enforce_loader_rules_still_refuses_a_genuinely_unknown_os(
    planner, alternate_schema_root
):
    """A value NEITHER root declares stays refused everywhere -- the
    alternate root must widen what is accepted, not disable the check."""
    from tan.planner.models import OrchestratorError, Slice
    from tan.planner.paths import METADATA_ROOT
    from tan.planner.validate import _enforce_loader_rules

    slice_ = Slice(core_id="m33_sm", os="not-a-real-os")
    for root in (METADATA_ROOT, alternate_schema_root):
        with pytest.raises(OrchestratorError, match="unknown os"):
            _enforce_loader_rules(slice_, root)
