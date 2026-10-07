# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1354 (bench round 7): a stale alp-sdk checkout whose SoM presets are
`schema_version: 1` used to make `--ram` / debug-config callers report only an
unexplained "unreadable"/"no readable SoM preset". They now say why."""
from __future__ import annotations

import types

import pytest

from tan.commands import flash_ram
from tan.commands.build_output import read_sdk_som_and_soc
from tan.core.ram_run import RamRunError

NOTE = "unsupported SoM preset schema_version 1 (tan needs 2) -- update alp-sdk"


def _sdk(tmp_path, version):
    presets = tmp_path / "metadata" / "e1m_modules"
    presets.mkdir(parents=True)
    (presets / "E1M-S.yaml").write_text(f"schema_version: {version}\nsilicon: a:b:c\n", encoding="utf-8")
    return str(tmp_path / "metadata")


def test_the_reader_explains_a_v1_preset_when_asked(tmp_path):
    root = _sdk(tmp_path, 1)
    skipped: list[str] = []
    assert read_sdk_som_and_soc(root, "E1M-S", skipped=skipped, explain_unsupported=True) is None
    assert len(skipped) == 1 and skipped[0].endswith(f"E1M-S.yaml: not read -- {NOTE}")


def test_the_reader_stays_quiet_by_default_so_tan_size_keeps_its_own_issue(tmp_path):
    root = _sdk(tmp_path, 1)
    skipped: list[str] = []
    assert read_sdk_som_and_soc(root, "E1M-S", skipped=skipped) is None
    assert skipped == []


def test_other_failures_are_not_mislabelled_as_a_schema_problem(tmp_path):
    skipped: list[str] = []
    assert read_sdk_som_and_soc(
        str(tmp_path / "metadata"), "E1M-NOPE", skipped=skipped, explain_unsupported=True
    ) is None
    assert skipped == []
    root = _sdk(tmp_path, 2)  # a supported version: no note
    read_sdk_som_and_soc(root, "E1M-S", skipped=skipped, explain_unsupported=True)
    assert not any("unsupported SoM preset" in n for n in skipped)  # (a missing-schema note is another matter)


def test_ram_run_says_why_the_apertures_cannot_be_read(tmp_path):
    _sdk(tmp_path, 1)
    ctx = types.SimpleNamespace(sdk_root=str(tmp_path), sku="E1M-S")
    with pytest.raises(RamRunError) as raised:
        flash_ram._load_apertures(ctx, "m55_he")
    assert NOTE in str(raised.value) and "refusing to guess the TCM sizes" in str(raised.value)
