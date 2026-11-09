# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1522: a batched calibration .npy is N samples, not one (numpy only)."""
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from tan.model.prep import load_calibration  # noqa: E402


def test_a_batched_file_counts_as_one_sample_per_image(tmp_path: Path):
    np.save(tmp_path / "b.npy", np.zeros((16, 3, 8, 8), dtype=np.float32))
    samples = load_calibration(tmp_path, [-1, 3, 8, 8])
    assert len(samples) == 16
    assert all(s.shape == (1, 3, 8, 8) for s in samples)


def test_single_images_are_unchanged(tmp_path: Path):
    np.save(tmp_path / "one.npy", np.zeros((1, 3, 8, 8), dtype=np.float32))
    np.save(tmp_path / "two.npy", np.zeros((3, 8, 8), dtype=np.float32))
    assert len(load_calibration(tmp_path, [-1, 3, 8, 8])) == 2
