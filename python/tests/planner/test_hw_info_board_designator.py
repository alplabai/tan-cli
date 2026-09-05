# SPDX-License-Identifier: Apache-2.0
"""`--emit hw-info-h`'s `ALP_HW_BUILD_SOM_HW_REV` must carry the COMPOSED
`<board_datecode>-<hw_rev>` form (`"2626-r2"`), not the bare revision key
(alp-sdk#1964, tan-cli#1156's hand-port of
`scripts/alp_project_emit/hw_info.py`).

Before this port, `_emit_hw_info_h` baked the bare `hw_rev` straight into the
header. `scripts/program_eeprom.py` (unchanged, out of tan's scope) already
writes the composed form into a provisioned module's manifest, and the boot
banner compares the two -- so a firmware built with the pre-port header
disagreed with its own EEPROM on every AEN unit with a declared
`board_datecode:`, tripping `CONFIG_ALP_SDK_HW_REV_MISMATCH_FATAL` for a
reason that looked like a hardware fault and was really a stale generator.

Hermetic: every case here supplies its OWN `metadata_root` (a synthetic
`hw-revisions.yaml` under `tmp_path`), so this needs no bound alp-sdk
checkout -- `bound_sdk_root` (imported for its side effect, same idiom
`tests/planner/_baremetal_support.py`'s own consumers use) only satisfies
`tan/planner/paths.py`'s "some root must be bound before `tan.planner`
imports" requirement, with a throwaway stub when none is bound; which root
wins is inert here because `metadata_root` is always passed explicitly.

Mutation-proven: reverting `_emit_hw_info_h` to the pre-port body (drop the
`board_designator(load_family_table(...), som_hw_rev)` call) turns
`test_hw_info_h_composes_the_board_designator_from_the_family_table` red --
it asserts `"2626-r2"`, which the reverted body cannot produce. The other two
cases are the no-regression controls: a family with no `board_datecode:`, and
a family with no `hw-revisions.yaml` on disk at all, must both still emit the
bare key, exactly as before this port.
"""

from __future__ import annotations

# `bound_sdk_root` is a pytest fixture, imported for its side effect -- see
# this file's own docstring and `_baremetal_support.py`'s "Imported (not
# redefined) by each consumer module" note.
from tests.planner._baremetal_support import bound_sdk_root  # noqa: F401

_PROJECT = {"som": {"sku": "E1M-AEN801", "hw_rev": "r2"}}


def _write_hw_revisions(tmp_path, *, body: str) -> None:
    family_dir = tmp_path / "e1m_modules" / "aen"
    family_dir.mkdir(parents=True)
    (family_dir / "hw-revisions.yaml").write_text(body, encoding="utf-8")


def test_hw_info_h_composes_the_board_designator_from_the_family_table(
    tmp_path, bound_sdk_root
):
    _write_hw_revisions(
        tmp_path,
        body=(
            "family: aen\n"
            'display_name: "E1M-AEN"\n'
            'board_datecode: "2626"\n'
            "hw_revisions:\n"
            "  r2:\n"
            "    status: production\n"
        ),
    )
    from tan.planner.project_emit.hw_info import _emit_hw_info_h

    header = _emit_hw_info_h(_PROJECT, {}, None, metadata_root=tmp_path)
    assert 'ALP_HW_BUILD_SOM_HW_REV      "2626-r2"' in header


def test_hw_info_h_leaves_the_bare_rev_when_the_family_declares_no_datecode(
    tmp_path, bound_sdk_root
):
    _write_hw_revisions(
        tmp_path,
        body=(
            "family: aen\n"
            'display_name: "E1M-AEN"\n'
            "hw_revisions:\n"
            "  r2:\n"
            "    status: production\n"
        ),
    )
    from tan.planner.project_emit.hw_info import _emit_hw_info_h

    header = _emit_hw_info_h(_PROJECT, {}, None, metadata_root=tmp_path)
    assert 'ALP_HW_BUILD_SOM_HW_REV      "r2"' in header


def test_hw_info_h_tolerates_a_family_with_no_hw_revisions_file_at_all(
    tmp_path, bound_sdk_root
):
    from tan.planner.project_emit.hw_info import _emit_hw_info_h

    # No metadata/e1m_modules/aen/hw-revisions.yaml under tmp_path at all --
    # load_family_table() returns {} for an in-development family, and
    # board_designator() must pass the bare key through unchanged.
    header = _emit_hw_info_h(_PROJECT, {}, None, metadata_root=tmp_path)
    assert 'ALP_HW_BUILD_SOM_HW_REV      "r2"' in header
