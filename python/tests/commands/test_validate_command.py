# SPDX-License-Identifier: Apache-2.0
"""`tan validate --format diagnostic-v1` / `--format sarif` -- the two formats
that let alp-sdk's `scripts/check_diagnostic_schema.py` gate point at `tan`
instead of spawning `python -m alp_cli.main validate --format json`.

`text` and `json` (the envelope) are unchanged by this addition and are
already pinned by the `validate-offline-clean` / `validate-offline-schema-
violation` conformance fixtures under `contract/envelopes/` -- this file only
covers the two new formats, which have no fixture (no Rust precedent: see
`validate_cmd.py`'s `_FORMATS` docstring).

Full schema conformance (against alp-sdk's actual
`metadata/schemas/diagnostic-v1.schema.json` via `jsonschema`) is proven
out-of-band, not as a committed test here -- that schema lives in a sibling
repo and this suite must stay runnable from a bare tan-cli clone.
"""
from __future__ import annotations

import json
import re

from typer.testing import CliRunner

from tan.cli import app
from tan.exit_codes import ExitCode
from tan.version import TAN_VERSION

runner = CliRunner()

#: `diagnostic-v1.schema.json`'s `$defs/diagnostic.properties.code.pattern`.
_CODE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _write(tmp_path, text):
    (tmp_path / "board.yaml").write_text(text, encoding="utf-8")


def test_diagnostic_v1_clean_is_schema_shaped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "som:\n  sku: E1M-AEN701\npreset: e1m-evk\n")
    result = runner.invoke(app, ["validate", "--offline", "--format", "diagnostic-v1"])
    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    doc = json.loads(result.output)
    assert doc == {
        "schemaVersion": 1,
        "tool": {"name": "tan", "version": TAN_VERSION},
        "diagnostics": [],
    }


def test_diagnostic_v1_schema_violation_carries_one_diagnostic(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Scalar `som:` -- the same shape as the `validate-offline-schema-
    # violation` conformance fixture, which pins the JSON-envelope form of
    # this exact failure to exit 2 / one `validate.schema-violation` issue.
    _write(tmp_path, "som: E1M-AEN701\n")
    result = runner.invoke(app, ["validate", "--offline", "--format", "diagnostic-v1"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    doc = json.loads(result.output)
    assert doc["schemaVersion"] == 1
    assert doc["tool"] == {"name": "tan", "version": TAN_VERSION}
    assert len(doc["diagnostics"]) == 1
    diag = doc["diagnostics"][0]
    assert diag["uri"] == "./board.yaml"
    assert diag["range"] == {
        "start": {"line": 0, "character": 0},
        "end": {"line": 0, "character": 0},
    }
    assert diag["severity"] == "error"
    assert diag["code"] == "validate-schema-violation"
    assert _CODE_PATTERN.match(diag["code"])
    assert "sku" in diag["message"]
    # No dot survives the envelope-code -> diagnostic-code conversion: a dot
    # is exactly what `diagnostic-v1`'s `code` pattern forbids.
    assert "." not in diag["code"]


def test_diagnostic_v1_two_messages_become_two_diagnostics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        "schemaVersion: 2\nos: zephyr\nsom:\n  sku: E1M-AEN701\n",
    )
    result = runner.invoke(app, ["validate", "--offline", "--format", "diagnostic-v1"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    doc = json.loads(result.output)
    assert len(doc["diagnostics"]) == 2
    for diag in doc["diagnostics"]:
        assert diag["code"] == "validate-schema-violation"
        assert _CODE_PATTERN.match(diag["code"])


_SARIF_SCHEMA_URI = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/"
    "sarif-schema-2.1.0.json"
)


def test_sarif_shape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "som: E1M-AEN701\n")
    result = runner.invoke(app, ["validate", "--offline", "--format", "sarif"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    doc = json.loads(result.output)
    assert doc["$schema"] == _SARIF_SCHEMA_URI
    assert doc["version"] == "2.1.0"
    run = doc["runs"][0]
    driver = run["tool"]["driver"]
    assert driver["name"] == "tan"
    assert driver["informationUri"] == "https://github.com/alplabai/tan-cli"
    assert driver["version"] == TAN_VERSION
    assert [r["id"] for r in driver["rules"]] == ["validate-schema-violation"]
    assert len(run["results"]) == 1
    result_entry = run["results"][0]
    assert result_entry["ruleId"] == "validate-schema-violation"
    assert result_entry["level"] == "error"
    # SARIF regions are ONE-based by spec -- this is the fidelity rule the
    # oracle's own module docstring states twice
    # (scripts/alp_cli/diagnostic_format.py:14-16, :101-103). Do not reuse
    # the LSP zero-based numbers `_ZERO_POSITION` uses for diagnostic-v1.
    assert result_entry["locations"][0]["physicalLocation"]["region"] == {
        "startLine": 1,
        "startColumn": 1,
        "endLine": 1,
        "endColumn": 1,
    }
    assert result_entry["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == (
        "./board.yaml"
    )


def test_sarif_rules_are_deduped_by_code(tmp_path, monkeypatch):
    """Two `validate.schema-violation` issues from the same run collapse to
    one rule entry -- pins `rules.setdefault(code, ...)` (validate_cmd.py)
    against a non-deduping insert, which passed the old assertion-free
    `test_sarif_shape` unnoticed."""
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        "schemaVersion: 2\nos: zephyr\nsom:\n  sku: E1M-AEN701\n",
    )
    result = runner.invoke(app, ["validate", "--offline", "--format", "sarif"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    doc = json.loads(result.output)
    run = doc["runs"][0]
    assert len(run["results"]) == 2
    assert len(run["tool"]["driver"]["rules"]) == 1


def test_text_and_json_formats_are_unchanged(tmp_path, monkeypatch):
    """`text`/`json` behaviour is pinned byte-for-byte by the conformance
    fixtures; this only guards that adding two new format values didn't
    disturb the `--format` validation branch itself."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "som:\n  sku: E1M-AEN701\npreset: e1m-evk\n")
    result = runner.invoke(app, ["validate", "--offline", "--format", "json"])
    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    envelope = json.loads(result.output)
    assert envelope["command"] == "validate"
    assert envelope["data"]["outcome"] == "clean"


def test_unknown_format_is_rejected_and_lists_all_four_choices(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "som:\n  sku: E1M-AEN701\n")
    result = runner.invoke(app, ["validate", "--offline", "--format", "bogus"])
    assert result.exit_code != 0
    for choice in ("text", "json", "diagnostic-v1", "sarif"):
        assert choice in result.output
