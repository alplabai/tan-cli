# SPDX-License-Identifier: Apache-2.0
"""`_library_alias_table` rejects a duplicate key like alp-sdk does
(alp-sdk#1127, tan-cli#1487): the lenient `json.loads` kept the last value
silently and could bind a different manifest than the SDK's own validator.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.planner._bound_sdk_fixture import SDK, _bound_sdk  # noqa: F401

pytestmark = pytest.mark.skipif(
    SDK is None,
    reason="ALP_SDK_ROOT is not set -- tan.planner needs a bound root to import",
)


def test_duplicate_alias_key_is_rejected(tmp_path: Path):
    from tan.planner.loader import _library_alias_table
    from tan.planner.strict_loaders import DuplicateKeyError

    (tmp_path / "library-aliases-v1.json").write_text(
        '{"aliases": {"a": "x", "a": "y"}}', encoding="utf-8")
    with pytest.raises(DuplicateKeyError):
        _library_alias_table(tmp_path)


def test_clean_alias_table_still_loads(tmp_path: Path):
    from tan.planner.loader import _library_alias_table

    (tmp_path / "library-aliases-v1.json").write_text(
        '{"aliases": {"a": "x"}}', encoding="utf-8")
    assert _library_alias_table(tmp_path) == {"a": "x"}
