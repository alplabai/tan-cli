# SPDX-License-Identifier: Apache-2.0
"""tan-cli#964, REFUSE half: `load_board_yaml` (the `tan build`/`tan
generate` front door) refuses a `board.yaml`-bound run whose SoM preset or
SoC JSON does not validate against `som-preset-v1.schema.json` /
`soc-spec-v1.schema.json` -- the read-path gate `_refuse_on_schema_errors`
(`tan/planner/loader.py`) adds, backed by the one shared validator in
`tan.core.metadata_schema`.

Same requirement as every other `tan.planner`-touching test in this tree
(`test_board_schema_enforcement.py`, `test_topology_nonstring_core_type.py`,
...): `tan.planner` cannot be imported before `bind_sdk_root(<checkout>)` has
run, so this needs a real `ALP_SDK_ROOT`/`ALP_SDK_PARITY_ROOT` and skips,
loudly, without one -- never a silent pass. The BOUND root itself is only
what satisfies that import-time requirement; every document this file reads
comes from a fully synthetic `metadata_root=` override (tan-cli#573,
`test_metadata_root_override.py`'s own precedent) so this file's coverage
does not depend on the shape of any real SoM/SoC file in the bound checkout.

Mutation-proven (see the docstring on each test): reverting
`_refuse_on_schema_errors`'s call sites in `_resolve_board_impl` (or the
function itself, made a no-op) turns the REFUSE case's `pytest.raises` red;
restoring turns it green. Byte copy taken before mutating, `git checkout`
never used (the working tree may carry unrelated uncommitted work).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.conftest import sdk_root as _bound_probe

SDK = _bound_probe()

_SKIP_REASON = (
    "set ALP_SDK_ROOT to an alp-sdk checkout so tan.planner can bind a "
    "root and import (same requirement as test_board_schema_enforcement.py)"
)


@pytest.fixture(scope="module")
def loader():
    """`tan.planner.loader`, with the SDK root bound first -- see the module
    docstring for why the bound root itself is not what this file's test data
    comes from."""
    if SDK is None:
        pytest.skip(reason=_SKIP_REASON)
    from tan.planner_root import bind_sdk_root

    bind_sdk_root(SDK)
    from tan.planner import loader as loader_mod

    return loader_mod


#: A minimal, fully permissive board schema: this file is not testing
#: `board.yaml`'s OWN schema gate (`test_board_schema_enforcement.py` already
#: does), so nothing here should refuse on it.
_BOARD_SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}

#: Deliberately narrower than the real `som-preset-v1.schema.json`: enough to
#: exercise the gate (`schema_version`/`sku`/`silicon` required, `topology`
#: typed), not a byte-for-byte mirror of the real file, which would make this
#: file's coverage depend on the real schema never changing shape.
_SOM_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema_version", "sku", "silicon"],
    "properties": {
        "schema_version": {"const": 1},
        "sku": {"type": "string"},
        "silicon": {"type": "string"},
        "topology": {"type": "object"},
    },
}

#: Same narrowing rationale as `_SOM_SCHEMA`; `cores[].type` is the shape the
#: whole tan-cli#957/#962/#965/#969/#964 family is about.
_SOC_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "cores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "type": {"type": "string"}},
            },
        }
    },
}

_SKU = "E1M-TEST"
_SILICON = "vendor:family:part"


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _posix(path: Path) -> str:
    """Posix-normalise a `Path` for comparison against a message the module
    ALREADY posix-normalises internally (tan-cli#964 review, blocker 2) --
    `str(Path(...))` alone renders with the platform's native separator,
    which would only ever match on POSIX."""
    return str(path).replace("\\", "/")


def _som_preset_yaml(sku: str = _SKU, silicon: str = _SILICON) -> str:
    return (
        f"schema_version: 1\nsku: {sku}\nsilicon: \"{silicon}\"\n"
        "topology:\n  a:\n    board: something\n"
    )


def _build_tree(tmp_path: Path, *, soc_core_type) -> tuple[Path, Path]:
    """A self-contained `(board_yaml, metadata_root)` pair. *soc_core_type*
    is written verbatim as `cores[0].type` in the SoC JSON -- a string is
    schema-valid, anything else (a number, a list, ...) is not."""
    metadata_root = tmp_path / "metadata"
    _write(metadata_root / "schemas" / "board.schema.json", _BOARD_SCHEMA)
    _write(metadata_root / "schemas" / "som-preset-v1.schema.json", _SOM_SCHEMA)
    _write(metadata_root / "schemas" / "soc-spec-v1.schema.json", _SOC_SCHEMA)
    (metadata_root / "e1m_modules" / f"{_SKU}.yaml").parent.mkdir(parents=True, exist_ok=True)
    (metadata_root / "e1m_modules" / f"{_SKU}.yaml").write_text(
        _som_preset_yaml(), encoding="utf-8"
    )
    soc_path = metadata_root / "socs" / "vendor" / "family" / "part.json"
    _write(soc_path, {"cores": [{"id": "a", "type": soc_core_type}]})

    board_yaml = tmp_path / "board.yaml"
    board_yaml.write_text(
        f"som:\n  sku: {_SKU}\ncores:\n  a:\n    app: ./src\n", encoding="utf-8"
    )
    return board_yaml, metadata_root


def test_a_schema_invalid_soc_spec_refuses_load_board_yaml(loader, tmp_path):
    """The REFUSE half of tan-cli#964's decided rule: `tan build`/`tan
    generate` (both funnel through `load_board_yaml`) refuse a run whose SoC
    JSON does not validate, instead of the `cores[].type` value silently
    degrading to `""` three call sites downstream (the #957/#962 family) or
    crashing one of them outright.

    Mutation-proven: commenting out the `_refuse_on_schema_errors(soc_spec,
    ...)` call in `tan/planner/loader.py::_resolve_board_impl` (byte copy
    restored after, never `git checkout`) turns this RED -- `load_board_yaml`
    then returns a `BoardProject` instead of raising, because the pre-existing
    `isinstance` guards three call sites downstream degrade the `7` to `""`
    rather than surfacing it here. Restoring the call turns it GREEN.
    """
    board_yaml, metadata_root = _build_tree(tmp_path, soc_core_type=7)

    with pytest.raises(loader.OrchestratorError) as excinfo:
        loader.load_board_yaml(board_yaml, metadata_root=metadata_root)

    message = str(excinfo.value)
    assert message.startswith(f"SoC spec for {_SILICON} does not validate against soc-spec-v1:")
    # Names the file, the JSON pointer, and what was found -- tan-cli#964's
    # own CX requirement, not the bare "schema validation failed".
    soc_path = metadata_root / "socs" / "vendor" / "family" / "part.json"
    assert f"  - {_posix(soc_path)}: cores/0/type: 7 is not of type 'string'" in message.splitlines()


def test_a_schema_invalid_som_preset_refuses_before_the_soc_spec_is_even_read(loader, tmp_path):
    """The SoM-preset half of the same gate, and proof of ordering: a
    schema-invalid `silicon:` type never reaches `_silicon_to_soc_path`, whose
    `parts = silicon.split(':')` would otherwise raise `AttributeError` on a
    non-string -- a DIFFERENT crash the schema gate now pre-empts rather than
    papering over.
    """
    board_yaml, metadata_root = _build_tree(tmp_path, soc_core_type="cortex-m33")
    preset_path = metadata_root / "e1m_modules" / f"{_SKU}.yaml"
    preset_path.write_text(
        "schema_version: 1\nsku: E1M-TEST\nsilicon: 7\ntopology:\n  a:\n    board: something\n",
        encoding="utf-8",
    )

    with pytest.raises(loader.OrchestratorError) as excinfo:
        loader.load_board_yaml(board_yaml, metadata_root=metadata_root)

    message = str(excinfo.value)
    assert message.startswith(f"SoM preset {_SKU} does not validate against som-preset-v1:")
    assert f"  - {_posix(preset_path)}: silicon: 7 is not of type 'string'" in message.splitlines()


def test_a_schema_valid_document_pair_is_untouched(loader, tmp_path):
    """The control: a valid SoM preset + SoC JSON load cleanly all the way
    through -- storage, cross-field validation, the lot -- exactly as before
    this change. Proves the new gate does not fire on the common case.
    """
    board_yaml, metadata_root = _build_tree(tmp_path, soc_core_type="cortex-m33")

    project = loader.load_board_yaml(board_yaml, metadata_root=metadata_root)

    assert project.sku == _SKU
    assert set(project.cores) == {"a"}
    assert project.soc_spec["cores"][0]["type"] == "cortex-m33"


def test_a_missing_schema_file_degrades_silently_not_as_a_refusal(loader, tmp_path):
    """A checkout whose bound `metadata/schemas/soc-spec-v1.schema.json` is
    simply ABSENT (an SDK predating this schema, or -- as here -- a
    synthetic/partial metadata root) must not refuse the run: refusing would
    fail every such checkout for tan's inability to check it, not for
    anything wrong with the checkout's own document (see
    `tan.core.metadata_schema.validate_document`'s own docstring). This is
    the "must not fire on a checkout that is simply older... in a way that
    blocks a legitimate customer" half of tan-cli#964's own verification list
    -- silently skipping validation is NOT the same failure `validate_document`
    exists to close: nothing here claims the document IS valid, it is simply
    never checked, and `load_board_yaml` proceeds exactly as it did before
    this change existed.
    """
    board_yaml, metadata_root = _build_tree(tmp_path, soc_core_type=7)
    (metadata_root / "schemas" / "soc-spec-v1.schema.json").unlink()

    project = loader.load_board_yaml(board_yaml, metadata_root=metadata_root)

    assert project.sku == _SKU
    # The unvalidated raw value passes through untouched -- there is no
    # schema present to have caught it, and the existing isinstance guards
    # three call sites downstream (not exercised by this loader-level test)
    # are what remain the last line of defence for it.
    assert project.soc_spec["cores"][0]["type"] == 7


def test_a_missing_schema_file_discloses_the_skip_when_asked(loader, tmp_path):
    """tan-cli#964 review (major 6, 'skip-but-disclose'): the identical
    fixture as the silent-degrade test above, but with `skip_advisories=`
    given -- `load_board_yaml` must NOT refuse (unchanged from that test),
    but MUST collect a disclosure note naming the absent schema file, so a
    caller (`tan build`/`tan generate`) that wants to surface "validated
    against nothing" rather than silence can.

    Mutation-proven: reverting `_refuse_on_schema_errors`'s
    `skip_advisories.append(note)` call (byte copy restored after, never
    `git checkout`) turns this test's `skip_advisories` assertion red;
    restoring turns it green.
    """
    board_yaml, metadata_root = _build_tree(tmp_path, soc_core_type=7)
    (metadata_root / "schemas" / "soc-spec-v1.schema.json").unlink()
    soc_path = metadata_root / "socs" / "vendor" / "family" / "part.json"

    skip_advisories: list[str] = []
    project = loader.load_board_yaml(
        board_yaml, metadata_root=metadata_root, skip_advisories=skip_advisories
    )

    assert project.sku == _SKU
    assert project.soc_spec["cores"][0]["type"] == 7
    # Posix-normalised (`\` -> `/`), same as every other field this module
    # puts on the wire (tan-cli#964 review, blocker 2) -- `_posix`, not the
    # native `str(Path)`, so this passes identically on Windows and POSIX.
    schema_path = metadata_root / "schemas" / "soc-spec-v1.schema.json"
    assert skip_advisories == [
        f"{_posix(soc_path)}: not validated -- no schema at "
        f"{_posix(schema_path)} in this checkout"
    ]


def test_a_present_and_valid_schema_discloses_nothing(loader, tmp_path):
    """The control: when both schemas are present (whether the document
    validates or not), `skip_advisories` stays empty -- nothing was skipped."""
    board_yaml, metadata_root = _build_tree(tmp_path, soc_core_type="cortex-m33")

    skip_advisories: list[str] = []
    loader.load_board_yaml(
        board_yaml, metadata_root=metadata_root, skip_advisories=skip_advisories
    )

    assert skip_advisories == []
