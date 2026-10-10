# SPDX-License-Identifier: Apache-2.0
"""The plain route's manifest validates against alp-sdk's REAL schema (tan-cli#1370).

`system-manifest-v1.schema.json` requires `flash_method` + `flash_args` on every
slice and forbids unknown keys; tan carries no vendored copy, so these skip
without a bound `ALP_SDK_ROOT`."""
import json

import pytest
import yaml

from tests.conftest import sdk_root
from tests.core.test_elf_load import elf32

SDK = sdk_root()
pytestmark = pytest.mark.skipif(SDK is None, reason="ALP_SDK_ROOT is not set")

HE = "alp_e1m_aen803_m55_he/ae822fa0e5597ls0/rtss_he"


def _manifest(tmp_path, board, lma=None):
    from tan.planner_root import bind_sdk_root

    bind_sdk_root(SDK)
    from tan.planner.plain_slice import plain_core_id, plain_system_manifest

    elf = None
    if lma is not None:
        elf = tmp_path / "zephyr.elf"
        elf.write_bytes(elf32([(1, lma, 16, 16), (1, 0x200000C0, 0, 0x100)]))
    core = plain_core_id(board, SDK / "metadata") or "x"
    return yaml.safe_load(plain_system_manifest(
        board, SDK / "metadata", fallback_core_id=core, elf_path=str(elf) if elf else None))


@pytest.mark.parametrize("board,lma,method", [
    (HE, 0x80010000, "zephyr_west_flash"),
    (HE, 0x0, "ram_run_only"),
    (HE, None, "ram_run_only"),
    ("native_sim", None, "none"),
    ("foo/bar", None, "none"),
])
def test_plain_manifest_validates_against_the_sdk_schema(tmp_path, board, lma, method):
    import jsonschema

    schema = json.loads(
        (SDK / "metadata" / "schemas" / "system-manifest-v1.schema.json").read_text(encoding="utf-8"))
    doc = _manifest(tmp_path, board, lma)
    jsonschema.Draft202012Validator(schema).validate(doc)
    assert doc["slices"][0]["flash_method"] == method
