# SPDX-License-Identifier: Apache-2.0
"""tan-cli#415: four `new_som_cmd.py` sites had NO guard at all around a
`.read_text(encoding="utf-8")` (or only caught `yaml.YAMLError`, which does
not cover `UnicodeDecodeError`) -- and `new_som()` has no generic backstop
`except Exception` the way most other command bodies do, so a non-UTF-8 SDK
file genuinely escaped as an unhandled traceback here, not merely the wrong
issue code a backstop elsewhere would have produced.

A separate file from `test_new_som_command.py` on purpose: that whole module
is `pytestmark`-skipped without a real `ALP_SDK_ROOT` checkout (it cross-
validates against the REAL `som-preset-v1`/`soc-spec-v1` schemas), which would
make a decode-guard regression here silently never run in the default
`pytest tests -q` gate. These tests build the minimal synthetic SDK root each
guarded path actually needs -- no real alp-sdk checkout required.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from typer.testing import CliRunner

from tan.commands import new_som_cmd
from tan.commands.new_som_cmd import (
    _family_hw_revisions,
    _known_board_names,
    new_som,
)

app = typer.Typer()
app.command("new-som")(new_som)
runner = CliRunner()


def _minimal_sdk(tmp_path: Path) -> Path:
    """The marker alone (`scripts/alp_project.py`) -- enough for
    `resolve_sdk_tiered` to resolve this as an SDK root, with none of
    `metadata/boards/`, `scripts/alp_project_loader.py`, or a real schema
    present, so `_known_board_names`/`_family_hw_revisions` both take their
    documented "not resolvable" `None` return and every downstream check that
    depends on them is skipped -- isolating the run down to exactly the
    `_current_sku_pattern` read this test corrupts.
    """
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
    return sdk


def test_non_utf8_som_schema_is_a_coded_envelope_not_a_traceback(tmp_path: Path) -> None:
    """`_current_sku_pattern` (new_som_cmd.py) had no try/except at all."""
    sdk = _minimal_sdk(tmp_path)
    schema_path = sdk / "metadata" / "schemas" / "som-preset-v1.schema.json"
    schema_path.parent.mkdir(parents=True)
    schema_path.write_bytes(b'{"properties": {"sku": {"pattern": "^E1M-\xff"}}}')

    result = runner.invoke(
        app,
        [
            "--sku", "E1M-TEST",
            "--soc-ref", "vendor:family:part",
            "--family", "testfam",
            "--sdk-root", str(sdk),
            "--dry-run",
            "--format", "json",
        ],
    )
    assert result.exit_code != 0
    assert result.stdout.count("\n") == 1, result.stdout  # one envelope, not a traceback
    doc = json.loads(result.stdout)
    assert doc["ok"] is False
    assert doc["command"] == "new-som"
    assert doc["issues"][0]["code"] == "new-som.failed"
    assert str(schema_path) in doc["issues"][0]["message"]


def test_known_board_names_skips_a_non_utf8_board_file_instead_of_raising(
    tmp_path: Path,
) -> None:
    """`_known_board_names` caught `yaml.YAMLError` only; a non-UTF-8 board
    file must be skipped the SAME best-effort way a YAML-invalid one already
    is, not raise `UnicodeDecodeError` out of this scan."""
    sdk = tmp_path / "sdk"
    boards_dir = sdk / "metadata" / "boards"
    boards_dir.mkdir(parents=True)
    (boards_dir / "bad.yaml").write_bytes(b"name: Bad\n# \xff\n")
    (boards_dir / "good.yaml").write_text("name: Good\n", encoding="utf-8")

    assert _known_board_names(sdk) == {"Good"}


def test_family_hw_revisions_returns_none_for_a_non_utf8_revisions_file(
    tmp_path: Path, monkeypatch
) -> None:
    """`_family_hw_revisions` caught `yaml.YAMLError` only; a non-UTF-8
    hw-revisions.yaml must resolve to `None` ("not resolvable"), the same as
    a YAML-invalid one, not raise.

    `_resolve_sku_family` (the real implementation) asks the SDK's own
    `alp_project_loader` via a spawned subprocess -- stubbed here so this test
    is hermetic and does not depend on a real `python` on PATH, matching how
    `test_model_command.py` stubs the equivalent spawn-adjacent helpers in its
    non-spawn tests.
    """
    monkeypatch.setattr(new_som_cmd, "_resolve_sku_family", lambda sku, sdk_root: "testfam")
    sdk = tmp_path / "sdk"
    hwrev_path = sdk / "metadata" / "e1m_modules" / "testfam" / "hw-revisions.yaml"
    hwrev_path.parent.mkdir(parents=True)
    hwrev_path.write_bytes(b"hw_revisions:\n  r1: {}\n# \xff\n")

    assert _family_hw_revisions("E1M-TEST", sdk, sdk) is None
