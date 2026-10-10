# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1405: size/image `--build-root X` finds the manifest `tan build
--build-root X` wrote (`X/build/system-manifest.yaml`) as well as the
back-compat `X/system-manifest.yaml`; a miss names both paths."""
import os

import pytest

from tan.commands.build_output import ManifestUnavailable, load_manifest

MANIFEST = """schema_version: 1
hw_info: {sku: E1M-V2N101}
slices: []
helper_mcus: []
boot_order: []
"""


def test_manifest_at_root(tmp_path):
    (tmp_path / "system-manifest.yaml").write_text(MANIFEST, encoding="utf-8", newline="\n")
    text, _ = load_manifest(str(tmp_path))
    assert text == MANIFEST


def test_manifest_under_build_subdir(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "system-manifest.yaml").write_text(MANIFEST, encoding="utf-8", newline="\n")
    text, _ = load_manifest(str(tmp_path))
    assert text == MANIFEST


def test_root_spelling_wins_over_nested(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "system-manifest.yaml").write_text("nested", encoding="utf-8")
    (tmp_path / "system-manifest.yaml").write_text(MANIFEST, encoding="utf-8", newline="\n")
    text, _ = load_manifest(str(tmp_path))
    assert text == MANIFEST


def test_miss_names_both_paths(tmp_path):
    with pytest.raises(ManifestUnavailable) as info:
        load_manifest(str(tmp_path))
    assert os.path.join(str(tmp_path), "system-manifest.yaml") in info.value.path
    assert os.path.join(str(tmp_path), "build", "system-manifest.yaml") in info.value.path


def test_effective_build_root_is_the_manifest_directory(tmp_path):
    from tan.core.system_manifest import effective_build_root

    assert effective_build_root(str(tmp_path)) == str(tmp_path)  # no manifest: X
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "system-manifest.yaml").write_text(MANIFEST, encoding="utf-8", newline="\n")
    assert effective_build_root(str(tmp_path)) == str(tmp_path / "build")
    (tmp_path / "system-manifest.yaml").write_text(MANIFEST, encoding="utf-8", newline="\n")
    assert effective_build_root(str(tmp_path)) == str(tmp_path)
