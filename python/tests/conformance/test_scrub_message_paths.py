# SPDX-License-Identifier: Apache-2.0
"""tan-cli#1463: `scrub_message_paths` maps a scratch dir in BOTH its lexical
and its symlink-resolved spelling to one token (macOS reports `/private/var/...`
for a `$TMPDIR` of `/var/...`)."""

import os
from pathlib import Path

import pytest

from tests.conformance.test_contract_envelopes import (
    message_scrub_replacements,
    scrub_message_paths,
)


def test_scrub_maps_a_symlinked_scratch_dir_in_both_spellings(tmp_path):
    real = tmp_path / "real"
    (real / "work" / "root").mkdir(parents=True)
    (real / "home" / "root").mkdir(parents=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    work = link / "work" / "root"
    home = link / "home" / "root"
    pairs = message_scrub_replacements(work, home)
    lexical = work.as_posix()
    resolved = Path(os.path.realpath(work)).as_posix()
    assert lexical != resolved
    home_resolved = Path(os.path.realpath(home)).as_posix()
    doc = {
        "issues": [
            {
                "message": (
                    f"a `{lexical}/x` b `{resolved}/y` c `{home.as_posix()}/.alp` "
                    f"d `{home_resolved}/.alp` e `{(link / 'work').as_posix()}/alp-sdk` "
                    f"f `{(real / 'work').as_posix()}/alp-sdk`"
                )
            },
            {"message": "untouched C:\\keeps\\backslashes"},
        ]
    }
    out = scrub_message_paths(doc, pairs)
    assert out["issues"][0]["message"] == (
        "a `__WORKDIR__/x` b `__WORKDIR__/y` c `__HOME__/.alp` d `__HOME__/.alp` "
        "e `__WORKPARENT__/alp-sdk` f `__WORKPARENT__/alp-sdk`"
    )
    assert out["issues"][1]["message"] == "untouched C:\\keeps\\backslashes"
