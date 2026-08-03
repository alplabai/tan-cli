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


def test_validate_without_offline_is_validation_failure_not_internal(tmp_path, monkeypatch):
    """A `tan validate` without `--offline`, on a project whose board.yaml
    EXISTS, cannot produce a real verdict -- the spawn path is not ported yet
    -- but that is not a tan bug, so it must not exit 5.

    tan-cli#262 (v0.6.0, TAKEN): exit 2, not exit 1. "No verdict available" is
    still the VALIDATOR's problem, not a tan runtime crash -- alp-sdk-vscode
    renders exit 2 as "warning" and exit 1 as "error", so a failing project
    used to show red instead of yellow. This is a DELIBERATE divergence from
    the oracle's own `Outcome::Failed -> RuntimeFailure` mapping
    (`crates/tan-cli/src/commands/validate.rs:60`), not a parity claim -- see
    `validate_cmd.py`'s module docstring for the full measured comparison.
    Measured on `tan 0.4.1-dev` with `--format json`: board.yaml present but
    no SDK root exits 2 `validate.sdk-root-unresolved` (a different guard this
    port doesn't implement yet; not what this test pins). What IS aligned with
    the oracle is the missing-board.yaml guard -- see the test below."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "som:\n  sku: E1M-AEN701\npreset: e1m-evk\n")
    result = runner.invoke(app, ["validate", "--format", "json"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    assert envelope["exitCode"] == int(ExitCode.VALIDATION_FAILURE)
    assert [i["code"] for i in envelope["issues"]] == ["validate.spawn-not-implemented"]


def test_missing_board_yaml_is_validation_failure_on_both_paths(tmp_path, monkeypatch):
    """An empty directory reports `validate.board-yaml-missing` at exit 2
    whether or not `--offline` was passed: the guard runs BEFORE the not-ported
    stub can short-circuit.

    Measured on the oracle (`tan 0.4.1-dev`, `--format json`) in an empty dir:
    exit 2, `validate.board-yaml-missing`, `data.outcome == "failed"`. Until
    #262 the non-offline path answered "not ported yet" at exit 1 here -- which
    is the very first thing a brand-new user sees from `tan validate`, and the
    one place a gratuitous divergence is most expensive.

    tan-cli#350: the exit code (2) and issue CODE
    (`validate.board-yaml-missing`) are pinned here unchanged -- the fix for
    #350 is wording-only (see the two tests directly below), a DELIBERATE
    divergence from the oracle's message/verdict text, not from its exit
    code or issue code."""
    monkeypatch.chdir(tmp_path)
    for args in (
        ["validate", "--format", "json"],
        ["validate", "--offline", "--format", "json"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), (args, result.output)
        envelope = json.loads(result.output)
        assert envelope["exitCode"] == int(ExitCode.VALIDATION_FAILURE)
        assert envelope["data"]["outcome"] == "failed"
        assert [i["code"] for i in envelope["issues"]] == ["validate.board-yaml-missing"]


def test_missing_board_yaml_message_names_where_and_remedy(tmp_path, monkeypatch):
    """tan-cli#350 defects 1+2: the old wording,
    "board.yaml path could not be resolved or the file does not exist." (still
    the oracle's, byte-identical), names no remedy. The message carried by
    `issues[].code == validate.board-yaml-missing` (shared verbatim with text
    mode) must now name WHERE tan looked and BOTH remedies every sibling
    guard names for its own missing input (`tan init`, `--board-yaml
    <path>`) -- mirroring `doctor_cmd.py`'s own `board.yaml not found -- run
    \\`tan init\\` or pass \\`--board-yaml <path>\\`` wording for the identical
    guard."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["validate", "--format", "json"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    message = envelope["issues"][0]["message"]
    assert "./board.yaml" in message
    assert "tan init" in message
    assert "--board-yaml <path>" in message
    # This is not a "validation failure" -- nothing was validated.
    assert "validation failure" not in message


def test_missing_board_yaml_text_mode_verdict_is_not_validation_failure(tmp_path, monkeypatch):
    """tan-cli#350 defect 1: `validate` with no board.yaml at all must not
    print "validate: validation failure" -- that VERDICT implies something
    was checked and found wrong, but nothing was validated. Measured on the
    oracle (`tan 0.4.1-dev`): byte-identical wrong wording, hence this is a
    deliberate divergence, not a parity gap."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["validate", "--offline"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    assert "validate: no board.yaml to validate" in result.output
    assert "validate: validation failure" not in result.output
    assert "tan init" in result.output


def test_found_but_invalid_board_yaml_keeps_validation_failure_text(tmp_path, monkeypatch):
    """The #350 fix is scoped to the missing-file guard only -- a board.yaml
    that exists but does not fit the model is still, correctly, a
    "validation failure": something WAS checked and found wrong. Regression
    guard for the sibling branch touched by the fix above."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "som: E1M-AEN701\n")
    result = runner.invoke(app, ["validate", "--offline"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    assert "validate: validation failure" in result.output
    assert "no board.yaml to validate" not in result.output


def test_validate_offline_unreadable_board_yaml_is_still_internal_failure(tmp_path, monkeypatch):
    """A genuine internal failure -- board.yaml exists but cannot be read --
    is a real tan-can't-cope case, not a validation verdict, and must stay at
    exit 5. Mirrors the oracle's own offline path
    (`crates/tan-cli/src/commands/validate.rs::run_offline`), which reports
    the identical situation as `InternalFailure`."""
    monkeypatch.chdir(tmp_path)
    # A directory named `board.yaml` exists() but is not readable as text.
    (tmp_path / "board.yaml").mkdir()
    result = runner.invoke(app, ["validate", "--offline", "--format", "json"])
    assert result.exit_code == int(ExitCode.INTERNAL_FAILURE), result.output
    envelope = json.loads(result.output)
    assert envelope["exitCode"] == int(ExitCode.INTERNAL_FAILURE)
    assert [i["code"] for i in envelope["issues"]] == ["validate.internal-failure"]


def test_unknown_format_is_rejected_and_lists_all_four_choices(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "som:\n  sku: E1M-AEN701\n")
    result = runner.invoke(app, ["validate", "--offline", "--format", "bogus"])
    assert result.exit_code != 0
    for choice in ("text", "json", "diagnostic-v1", "sarif"):
        assert choice in result.output
