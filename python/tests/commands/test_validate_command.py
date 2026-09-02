# SPDX-License-Identifier: Apache-2.0
"""`tan validate`: the two extra output formats, and the SPAWN path.

The spawn half (everything below `_make_sdk`) covers tan-cli#376 -- the
default, no-`--offline` invocation the root quickstart documents, which until
#376 could not validate anything: it emitted `validate.spawn-not-implemented`
at exit 2 unconditionally, the same exit code a genuinely invalid board gets.
Those tests drive a STAND-IN `scripts/validate_board_yaml.py` rather than a
real alp-sdk checkout: the contract tan owns is the exit status -> outcome
map, the stderr -> issues parse, and the guards around the subprocess -- not
the SDK validator's own verdicts, which are alp-sdk's tests to write. The
interpreter is monkeypatched to `sys.executable` for the same reason
`test_generate_command.py` does it: `_planner_python` deliberately names a
PATH interpreter, and the stand-ins must run under THIS one.

One test at the bottom of this file is the exception, and it is #376's OWN
acceptance criterion rather than a unit of it: `test_a_real_sdk_backed_board_
passes_without_offline` binds the real checkout through
`tests.conftest.sdk_root()`. Stand-ins can only prove tan's half of the
contract -- that a validator exiting 0 is reported as clean. They cannot
prove the argv tan builds actually starts alp-sdk's real
`validate_board_yaml.py`, which is the half #376 exists for and the half a
`--offline` CI leg cannot reach.

The two extra formats -- `--format diagnostic-v1` / `--format sarif` -- let
alp-sdk's `scripts/check_diagnostic_schema.py` gate point at `tan`
instead of spawning `python -m alp_cli.main validate --format json`.

`text` and `json` (the envelope) are unchanged by this addition and are
already pinned by the `validate-offline-clean` / `validate-offline-schema-
violation` conformance fixtures under `contract/envelopes/` -- this file only
covers the two new formats, which have no fixture (no Rust precedent: see the
`--format` comment block at the top of `validate_cmd.py`, and
`tan.output_format.ValidateOutputFormat`, where the four values are declared
since tan-cli#403).

Full schema conformance (against alp-sdk's actual
`metadata/schemas/diagnostic-v1.schema.json` via `jsonschema`) is proven
out-of-band, not as a committed test here -- that schema lives in a sibling
repo and this suite must stay runnable from a bare tan-cli clone.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tan.cli import app
from tan.commands import sdk_cmd, validate_cmd
from tan.core.uri_reference import root_to_base_uri
from tan.exit_codes import ExitCode
from tan.version import TAN_VERSION
from tests.conftest import sdk_root

runner = CliRunner()

#: Resolved AT MODULE IMPORT, which `conftest.sdk_root`'s own docstring
#: requires: `_scrub_sdk_discovery_env` deletes `ALP_SDK_ROOT` before every
#: test function, so a call made from inside a test body always sees `None`.
SDK: Path | None = sdk_root()

#: `diagnostic-v1.schema.json`'s `$defs/diagnostic.properties.code.pattern`.
_CODE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _write(tmp_path, text):
    (tmp_path / "board.yaml").write_text(text, encoding="utf-8")


#: A board.yaml the OFFLINE structural checks pass -- and, deliberately, one
#: alp-sdk's own `scripts/validate_board_yaml.py` also accepts the shape of.
#:
#: It grew a `cores:` block in tan-cli#498 defect 1. Until then it was
#: `som:` + `preset:` alone, which every test here treated as the canonical
#: clean board while the real validator answered `error[ALP-B001]: required
#: key 'cores' is missing` (measured) -- the exact false pass #498 is about.
#: A fixture that is only valid because the checker was dead is not a fixture.
_CLEAN_BOARD = "som:\n  sku: E1M-AEN701\npreset: e1m-evk\ncores:\n  m55_hp:\n    app: ./src\n"


def test_diagnostic_v1_clean_is_schema_shaped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, _CLEAN_BOARD)
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
    artifact_location = result_entry["locations"][0]["physicalLocation"]["artifactLocation"]
    assert artifact_location["uri"] == "./board.yaml"
    # tan-cli#1097 review round 1 MAJOR 3: a RELATIVE `uri` must carry a
    # `uriBaseId` that resolves against a declared `originalUriBaseIds`
    # entry -- otherwise the base is implementation-defined, which is the
    # "resolves it wrong" failure mode the comment beside the emitting code
    # claims to have closed.
    assert artifact_location["uriBaseId"] == "%SRCROOT%"
    assert run["originalUriBaseIds"] == {
        "%SRCROOT%": {"uri": root_to_base_uri(".")},
    }


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


def test_absolute_board_yaml_uri_is_a_file_uri_but_boardyamlpath_stays_host_native(
    tmp_path, monkeypatch
):
    """tan-cli#1097's whole point, proven as a divergence rather than two
    separate pins: driving the SAME absolute `--board-yaml` through
    `--format json` and `--format diagnostic-v1` must produce documents that
    DISAGREE on this field's spelling. `data.boardYamlPath` stays
    host-native; only the two `uri` fields ([`_issue_to_diagnostic`],
    [`_sarif_document`]) go through
    `tan.core.uri_reference.path_to_uri_reference`. A future sweep that
    "unifies" the two contracts would make `uri` and `boardYamlPath` agree
    again, and this assertion is what catches that.

    This test asserts `data.boardYamlPath` only -- it does NOT exercise
    `--input`'s spawn argv. That is the non-`--offline` SPAWN path, asserted
    separately by `test_a_valid_board_passes_without_offline`'s own
    `command_line.endswith("--input ./board.yaml")`, and is untouched by
    this PR by inspection: the `command_line` string is built directly from
    `board_path` (`f"{python_binary} {script} --input {board_path}"`),
    nowhere near `path_to_uri_reference`."""
    monkeypatch.chdir(tmp_path)
    board = tmp_path / "elsewhere.yaml"
    board.write_text("som: E1M-AEN701\n", encoding="utf-8")

    result_json = runner.invoke(
        app, ["validate", "--offline", "--board-yaml", str(board), "--format", "json"]
    )
    assert result_json.exit_code == int(ExitCode.VALIDATION_FAILURE), result_json.output
    envelope = json.loads(result_json.output)
    assert envelope["data"]["boardYamlPath"] == str(board)

    result_diag = runner.invoke(
        app,
        ["validate", "--offline", "--board-yaml", str(board), "--format", "diagnostic-v1"],
    )
    assert result_diag.exit_code == int(ExitCode.VALIDATION_FAILURE), result_diag.output
    uri = json.loads(result_diag.output)["diagnostics"][0]["uri"]
    assert uri == Path(str(board)).as_uri()
    assert uri != envelope["data"]["boardYamlPath"]

    result_sarif = runner.invoke(
        app, ["validate", "--offline", "--board-yaml", str(board), "--format", "sarif"]
    )
    assert result_sarif.exit_code == int(ExitCode.VALIDATION_FAILURE), result_sarif.output
    sarif_doc = json.loads(result_sarif.output)
    sarif_run = sarif_doc["runs"][0]
    sarif_location = sarif_run["results"][0]["locations"][0]["physicalLocation"][
        "artifactLocation"
    ]
    assert sarif_location["uri"] == uri
    # tan-cli#1097 review round 1 MAJOR 3: `uriBaseId`/`originalUriBaseIds`
    # only have meaning for a RELATIVE reference -- an absolute `file:` URI
    # (this case) must carry neither.
    assert "uriBaseId" not in sarif_location
    assert "originalUriBaseIds" not in sarif_run


def test_relative_board_yaml_uri_stays_the_pinned_relative_reference(tmp_path, monkeypatch):
    """The separator-less default case both `test_diagnostic_v1_schema_
    violation_carries_one_diagnostic` and `test_sarif_shape` already pin at
    `"./board.yaml"` -- restated here as its own test, next to the absolute
    case above, because tan-cli#1097's relative-vs-absolute decision (see
    `tan.core.uri_reference.path_to_uri_reference`'s docstring) is that a
    relative `board_path` is returned UNCHANGED, not resolved to an absolute
    `file:` URI. `"./board.yaml"` is already a legal relative URI reference
    per RFC 3986 SS4.2, and this is the assertion that a future change to
    that decision would have to update deliberately."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "som: E1M-AEN701\n")
    result = runner.invoke(app, ["validate", "--offline", "--format", "diagnostic-v1"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    assert json.loads(result.output)["diagnostics"][0]["uri"] == "./board.yaml"


def test_text_and_json_formats_are_unchanged(tmp_path, monkeypatch):
    """`text`/`json` behaviour is pinned byte-for-byte by the conformance
    fixtures; this only guards that adding two new format values didn't
    disturb the `--format` validation branch itself."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, _CLEAN_BOARD)
    result = runner.invoke(app, ["validate", "--offline", "--format", "json"])
    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    envelope = json.loads(result.output)
    assert envelope["command"] == "validate"
    assert envelope["data"]["outcome"] == "clean"


def test_offline_refuses_a_board_with_no_cores_block(tmp_path, monkeypatch):
    """tan-cli#498 defect 1. `board.schema.json`'s ROOT carries
    `"required": ["som", "cores"]` with NO `schemaVersion` condition, so a
    board without `cores:` is refused by the real validator -- measured, on
    this exact text, as `error[ALP-B001]: required key 'cores' is missing`,
    exit 1. `tan validate --offline` called it `outcome: clean`, exit 0,
    because the check was gated on `schemaVersion >= 2` and alp-sdk pins
    `LATEST = 1` with an empty migration registry: 0 of the 100 board.yaml
    files under `alp-sdk/examples` declare the key, so the gate never opened.

    This is the path `validate.sdk-root-unresolved`'s own message recommends
    to a user with no checkout."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "som:\n  sku: E1M-AEN701\npreset: e1m-evk\n")
    result = runner.invoke(app, ["validate", "--offline", "--format", "json"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    assert envelope["data"]["outcome"] == "schema-violation"
    assert [i["code"] for i in envelope["issues"]] == ["validate.schema-violation"]
    assert "required key 'cores' is missing" in envelope["issues"][0]["message"]


def test_offline_refuses_a_top_level_os_key(tmp_path, monkeypatch):
    """The sibling half of defect 1, and the shape #498's own failure scenario
    names: `som:` + `os: zephyr`, no `schemaVersion:`. The schema's root
    `"not": {"required": ["os"]}` fires on the KEY, so this is checked with
    `in` rather than `is not None` -- a bare `os:` with no value is the same
    violation and used to slip past even once the gate was open.

    Both findings are reported, not just the first: the spawn path against the
    real SDK answers three (`ALP-B002` unknown key, `ALP-B001` missing cores,
    `ALP-B099` the `not` clause) for this input, and reporting one of two
    would send the user round the loop twice."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "som:\n  sku: E1M-AEN801\nos: zephyr\n")
    result = runner.invoke(app, ["validate", "--offline", "--format", "json"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    messages = [i["message"] for i in envelope["issues"]]
    assert len(messages) == 2
    assert "unknown key 'os'" in messages[0]
    assert "required key 'cores' is missing" in messages[1]

    # A valueless `os:` is the same violation.
    valueless = tmp_path / "valueless"
    valueless.mkdir()
    _write(valueless, "som:\n  sku: E1M-AEN801\nos:\ncores:\n  m55_hp:\n    app: ./src\n")
    monkeypatch.chdir(valueless)
    result = runner.invoke(app, ["validate", "--offline", "--format", "json"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    assert "unknown key 'os'" in json.loads(result.output)["issues"][0]["message"]


def test_offline_accepts_every_board_yaml_the_templates_ship(tmp_path, monkeypatch):
    """The over-refusal guard for defect 1: making a dead check live must not
    start rejecting the projects tan itself creates. Every `tan init` template
    board.yaml has to pass its own offline validator, or the first thing a new
    user does after `tan init` is watch `tan validate --offline` fail."""
    from tan import templates

    boards = sorted(Path(templates.__file__).parent.rglob("board.yaml"))
    assert boards, "no template board.yaml found -- the walk is wrong, not the templates"
    for board in boards:
        result = validate_cmd.validate_board_text(board.read_text(encoding="utf-8"))
        assert result.outcome == "clean", f"{board}: {result.findings}"


def test_a_cores_block_that_is_not_a_mapping_is_refused(tmp_path, monkeypatch):
    """`cores:` present but scalar. An EMPTY mapping is deliberately NOT
    refused -- the no-PyYAML reader (`_top_level_shape`) represents every
    block-opening key as `{}` and cannot see inside it, so failing on `{}`
    would refuse every real board on a host without PyYAML."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "som:\n  sku: E1M-AEN801\ncores: m55_hp\n")
    result = runner.invoke(app, ["validate", "--offline", "--format", "json"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    assert "must be a mapping" in json.loads(result.output)["issues"][0]["message"]


def test_validate_without_offline_and_no_sdk_is_sdk_root_unresolved(tmp_path, monkeypatch):
    """A `tan validate` without `--offline`, board.yaml present, no alp-sdk
    checkout anywhere: the spawn path's SDK guard refuses at exit 2 with
    `validate.sdk-root-unresolved`.

    This is the oracle's OWN answer to this scenario, measured on
    `tan 0.4.1-dev` with `--format json` -- so tan-cli#376 turned a divergence
    (this port used to answer `validate.spawn-not-implemented`, a code the
    oracle has no counterpart for) into parity. The exit code is 2 either way,
    which is exactly why the code matters: 2 is also a genuinely invalid
    board, and only `issues[].code` tells "your board is wrong" from "tan
    could not find the validator".

    `conftest.py` scrubs `ALP_SDK_ROOT` and repoints `HOME`/`USERPROFILE`, so
    neither a developer's shell nor a `tan sdk switch --global` pin can
    resolve an SDK here and make this test silently spawn something."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, _CLEAN_BOARD)
    result = runner.invoke(app, ["validate", "--format", "json"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    assert envelope["exitCode"] == int(ExitCode.VALIDATION_FAILURE)
    assert [i["code"] for i in envelope["issues"]] == ["validate.sdk-root-unresolved"]
    # The remedy every sibling guard names, plus the one this command owns.
    message = envelope["issues"][0]["message"]
    assert "--sdk-root" in message
    assert "--offline" in message
    # Nothing ran, so nothing is reported as having run.
    assert envelope["data"]["commandLine"] == ""


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


def test_validate_offline_unreadable_board_yaml_is_the_users_problem_at_exit_2(
    tmp_path, monkeypatch
):
    """tan-cli#498 defect 3, and the direct inversion of what this test used
    to assert. A board.yaml that exists but cannot be READ was reported as
    `validate.internal-failure` at exit 5 -- "tan crashed" -- for an error the
    guard's own inline comment already called the user's to fix, and the exit-5
    assertion here was the only thing pinning it.

    Three measurements said it was wrong: the SAME file on the spawn path
    exits 2 `validate.failed`; the envelope carried `data.outcome: "failed"`
    (a verdict) beside `exitCode: 5`, contradicting the command's own
    outcome->exit mapping; and `docs/bootstrap-manifest-unreadable.md` answers
    the identical input class for `tan bootstrap` at exit 2. The frozen oracle
    shares the flaw and is deliberately diverged from here, in the family of
    tan-cli#262 and commit 49a5fde.

    A directory named `board.yaml` is the portable stand-in: it `exists()` and
    is not readable as text on every host."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "board.yaml").mkdir()
    result = runner.invoke(app, ["validate", "--offline", "--format", "json"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    assert envelope["exitCode"] == int(ExitCode.VALIDATION_FAILURE)
    assert [i["code"] for i in envelope["issues"]] == ["validate.board-yaml-unreadable"]
    assert envelope["data"]["outcome"] == "failed"


def test_a_non_utf8_board_yaml_is_the_same_verdict_on_both_paths(tmp_path, monkeypatch):
    """The #498 defect-3 scenario as filed: a board.yaml saved in cp1252 with
    one non-ASCII byte (`# 100 <b5>A idle rail`), entirely plausible on a
    Windows editor. It used to be a tan CRASH with `--offline` and a
    validation verdict without it -- one file, two stories, from the same
    command.

    Text mode gets its own verdict line for the same reason
    `validate.board-yaml-missing` does (tan-cli#350): nothing was validated,
    so "validation failure" claims a verdict that was never reached."""
    (tmp_path / "board.yaml").write_bytes(
        b"# 100 \xb5A idle rail\nsom:\n  sku: E1M-AEN801\ncores:\n  m55_hp:\n    app: ./src\n"
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["validate", "--offline", "--format", "json"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    assert [i["code"] for i in envelope["issues"]] == ["validate.board-yaml-unreadable"]
    assert "0xb5" in envelope["issues"][0]["message"]

    text = runner.invoke(app, ["validate", "--offline"])
    assert text.exit_code == int(ExitCode.VALIDATION_FAILURE), text.output
    assert "validate: board.yaml could not be read" in text.output
    assert "validate: validation failure" not in text.output


class _OsWithUnresolvableAbspath:
    """A stand-in for the `os` module whose `os.path.abspath` always raises,
    proxying everything else -- including every other `os.path` attribute --
    to the real module.

    Rebinding `validate_cmd.os` to one of these (rather than mutating
    `os.path.abspath` on the real, process-wide `os.path` module `import os`
    hands back everywhere) keeps the fault scoped to this module's own call
    site: `os.path` is shared by the whole interpreter, including pytest's own
    machinery, so a global `abspath` that always raises risks breaking
    reporting for a test that has nothing to do with this one. Mirrors
    `test_doctor_command._FixedOsName`'s identical rationale for `os.name`.
    """

    class _Path:
        def __getattr__(self, attr):
            return getattr(os.path, attr)

        @staticmethod
        def abspath(_path):
            raise FileNotFoundError(2, "No such file or directory")

    def __getattr__(self, attr):
        return getattr(os, attr)

    @property
    def path(self):
        return self._Path()


def test_a_deleted_working_directory_still_produces_an_envelope(monkeypatch):
    """tan-cli#488 defect 8: `validate()`'s prologue -- `_resolve_board_path`
    through `resolve_sdk_root_ladder` -- used to run entirely OUTSIDE any
    guard that turns a raise into an envelope: `os.path.abspath(root)` (feeding
    the SDK-root ladder) throws `FileNotFoundError` when the working directory
    has been removed out from under the process (a cleanup script racing a
    `tan validate` run in the same shell), and that raise used to unwind
    through typer's own traceback renderer instead -- empty stdout, no error
    on either side of the CLI/extension seam. Mirrors `doctor_cmd`'s identical
    regression test; `build_cmd` already had it (its own `Path.cwd()` fix),
    `validate_cmd` did not.

    Mocks `os.path.abspath` itself, not `os.getcwd`: `posixpath.abspath`
    reaches the deleted-cwd `FileNotFoundError` via `os.getcwd()`, but
    `ntpath.abspath` resolves through `nt._getfullpathname` (a Win32 API call)
    FIRST and never calls `os.getcwd()` at all when it succeeds -- mocking
    `os.getcwd` alone left this guard untested on Windows: `abspath` returned
    normally, guard 1 (`validate.board-yaml-missing`) fired instead of the
    internal-failure guard this test exists to prove, and the assertion below
    saw exit 2 where it expected 5. `os.path.abspath` is the call site the
    guard actually wraps (`validate_cmd.py:885`), so mocking it directly is
    correct on every platform, not merely a workaround for this one.
    """
    monkeypatch.setattr(validate_cmd, "os", _OsWithUnresolvableAbspath())

    result = runner.invoke(app, ["validate", "--format", "json"])
    assert result.stdout, "must never be empty stdout"
    payload = json.loads(result.stdout)
    assert payload["command"] == "validate"
    assert payload["ok"] is False
    assert result.exit_code == payload["exitCode"] == int(ExitCode.INTERNAL_FAILURE)
    assert any(i["code"] == "validate.internal-failure" for i in payload["issues"]), payload["issues"]


def test_unknown_format_is_rejected_and_lists_all_four_choices(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "som:\n  sku: E1M-AEN701\n")
    result = runner.invoke(app, ["validate", "--offline", "--format", "bogus"])
    assert result.exit_code != 0
    for choice in ("text", "json", "diagnostic-v1", "sarif"):
        assert choice in result.output


# ─────────────────────────── the spawn path (#376) ───────────────────────────

#: A valid board for the stand-in SDK to be handed. Its CONTENT is irrelevant
#: to every test below -- the stand-in validator decides the verdict -- but it
#: must exist, because the missing-board guard runs first on both paths.
_BOARD = _CLEAN_BOARD


def _make_sdk(root: Path, validator_body: str) -> Path:
    """A stand-in alp-sdk checkout: the loader marker `resolve_sdk_root_ladder`
    validates a `--sdk-root` against, plus the `validate_board_yaml.py` the
    spawn path actually runs, whose body this test supplies."""
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    # The marker only has to EXIST -- `validate` never runs it.
    (scripts / "alp_project.py").write_text("", encoding="utf-8")
    (scripts / "validate_board_yaml.py").write_text(validator_body, encoding="utf-8")
    return root


def _stub(*, stdout: str = "", stderr: str = "", code: int = 0, extra: str = "") -> str:
    return (
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"{extra}"
        f"sys.exit({code})\n"
    )


def _spawn(tmp_path, monkeypatch, validator_body: str, fmt: str = "json"):
    """Run `tan validate` (no `--offline`) against a stand-in SDK, in-process."""
    project = tmp_path / "project"
    project.mkdir(parents=True)
    (project / "board.yaml").write_text(_BOARD, encoding="utf-8")
    sdk = _make_sdk(tmp_path / "alp-sdk", validator_body)
    monkeypatch.chdir(project)
    monkeypatch.setattr(
        validate_cmd, "_planner_python_resolution", lambda *_a, **_k: (sys.executable, True)
    )
    result = runner.invoke(app, ["validate", "--sdk-root", str(sdk), "--format", fmt])
    return result, sdk


def test_a_valid_board_passes_without_offline(tmp_path, monkeypatch):
    """tan-cli#376, THE regression: the default invocation the root quickstart
    documents (`tan validate`, no `--offline`) reaches a real validator and
    reports its verdict. Before the port this exited 2 with
    `validate.spawn-not-implemented` on this exact input -- a valid board
    reported with the same exit code as an invalid one."""
    result, sdk = _spawn(tmp_path, monkeypatch, _stub(stdout="board.yaml: clean\n"))
    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    envelope = json.loads(result.output)
    assert envelope["data"]["outcome"] == "clean"
    assert envelope["issues"] == []
    assert envelope["data"]["issueCount"] == 0
    # `data.commandLine` names what actually ran -- the oracle's own
    # `<python> <script> --input <board>` shape, no longer the empty string
    # every pre-#376 run reported.
    command_line = envelope["data"]["commandLine"]
    assert command_line.startswith(sys.executable)
    assert str(sdk / "scripts" / "validate_board_yaml.py") in command_line
    assert command_line.endswith("--input ./board.yaml")


def test_a_schema_invalid_board_returns_the_validators_own_diagnostics(tmp_path, monkeypatch):
    """The other half of #376's acceptance: a validator that refuses does not
    just set an exit code, it hands back the diagnostic it printed.

    Pins three things the stderr parser exists for, all at once: the ANSI
    escapes a real `validate_board_yaml.py` writes UNCONDITIONALLY (it renders
    with `color=not --no-color`, and `_use_color(True)` never consults the tty)
    are stripped, the `-->` arrow and its indented source/hint continuation are
    consumed as part of the one block, and the message that reaches the wire is
    the diagnostic's own text -- not the seven-line rendered block, and not a
    synthesized "Validation ended with outcome ..." placeholder.

    Since tan-cli#498 defect 2 the block's own `ALP-B005`, its `= hint:` and
    its `= see:` are carried into that message rather than consumed as
    padding; the source excerpt and the caret line still are not."""
    stderr = (
        "\x1b[31merror[ALP-B005]\x1b[0m: SoM SKU 'E1M-NX9999' does not resolve\n"
        "  --> board.yaml:3:8\n"
        "   |\n"
        " 3 | som: {sku: E1M-NX9999}\n"
        "   |          \x1b[31m^^^^^^^^^^^^\x1b[0m\n"
        "   = hint: did you mean E1M-NX9?\n"
        "   = see: docs/diagnostics/ALP-B005.md\n"
    )
    result, _sdk = _spawn(tmp_path, monkeypatch, _stub(stderr=stderr, code=1))
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    assert envelope["data"]["outcome"] == "schema-violation"
    assert [i["code"] for i in envelope["issues"]] == ["validate.schema-violation"]
    issue = envelope["issues"][0]
    assert issue["message"] == (
        "ALP-B005: SoM SKU 'E1M-NX9999' does not resolve\n"
        "  hint: did you mean E1M-NX9?\n"
        "  see: docs/diagnostics/ALP-B005.md"
    )
    assert issue["severity"] == "error"
    assert "\x1b" not in json.dumps(envelope)
    # Still ONE finding, not one per continuation line: the block's skipping
    # is unchanged, only what is read out of it on the way past.
    assert envelope["data"]["issueCount"] == 1


def test_a_clean_board_carrying_warnings_is_still_a_pass(tmp_path, monkeypatch):
    """`validate_board_yaml.py` renders EVERY diagnostic to stderr and only
    exits 1 for `collector.has_errors()` -- so a board whose only findings are
    warnings exits 0 WITH something on stderr. That run is a pass: exit 0,
    `outcome: clean`, `ok: true`, and the warnings carried alongside.

    The text verdict is pinned here too, because it is where this went wrong:
    the renderer used to say "validation failure" whenever `issues` was
    non-empty, which was indistinguishable from a real refusal while printing
    exit 0. Nothing offline could reach it (a clean offline result has no
    messages by construction), so the spawn path is what exposed it."""
    stderr = "warning[ALP-B900]: `iot.wifi` is set but no radio is populated\n"
    result, _sdk = _spawn(tmp_path, monkeypatch, _stub(stderr=stderr, code=0))
    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    envelope = json.loads(result.output)
    assert envelope["ok"] is True
    assert envelope["data"]["outcome"] == "clean"
    assert [(i["code"], i["severity"]) for i in envelope["issues"]] == [
        ("validate.clean", "warning")
    ]

    text, _sdk2 = _spawn(
        tmp_path / "text", monkeypatch, _stub(stderr=stderr, code=0), fmt="text"
    )
    assert text.exit_code == int(ExitCode.SUCCESS), text.output
    assert "is clean" in text.output
    assert "validation failure" not in text.output
    # The warning still has to reach the human -- printing only the verdict
    # would be the other half of the same defect.
    assert "no radio is populated" in text.output


def test_offline_still_never_spawns_the_sdk_validator(tmp_path, monkeypatch):
    """`--offline`'s contract is unchanged by #376 and is covered here in its
    own right: it runs ONLY the structural checks that ship in tan. The
    stand-in validator would fail this board loudly if it were reached -- and
    writes a marker file, so "it did not run" is proven by the filesystem
    rather than inferred from the exit code."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text(_BOARD, encoding="utf-8")
    marker = tmp_path / "validator-ran"
    sdk = _make_sdk(
        tmp_path / "alp-sdk",
        _stub(
            stderr="FAIL som preset: no preset for E1M-AEN701\n",
            code=1,
            extra=f"open({str(marker)!r}, 'w').close()\n",
        ),
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(
        validate_cmd, "_planner_python_resolution", lambda *_a, **_k: (sys.executable, True)
    )

    result = runner.invoke(
        app, ["validate", "--offline", "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    envelope = json.loads(result.output)
    assert envelope["data"]["outcome"] == "clean"
    assert envelope["data"]["commandLine"] == ""
    assert not marker.exists(), "--offline must not spawn the SDK validator"


def test_a_crashed_validator_is_failed_not_a_board_verdict(tmp_path, monkeypatch):
    """issue #38's collision: a validator whose environment lacks `jsonschema`
    dies with a traceback and exits 1 -- the SAME status a real schema
    violation uses. Reclassified to `failed` so a broken validator environment
    is never reported to a consumer as "your board.yaml is invalid".

    Also pins the synthesized message carrying the last line of the child's
    output: the oracle stops at "Validation ended with outcome 'failed'.",
    which on the case that reaches this most often says nothing at all about
    what broke."""
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "/sdk/scripts/validate_board_yaml.py", line 7, in <module>\n'
        "    import jsonschema\n"
        "ModuleNotFoundError: No module named 'jsonschema'\n"
    )
    result, _sdk = _spawn(tmp_path, monkeypatch, _stub(stderr=stderr, code=1))
    # tan-cli#262: exit 2, NOT the oracle's exit 1 for this outcome.
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    assert envelope["data"]["outcome"] == "failed"
    assert [i["code"] for i in envelope["issues"]] == ["validate.failed"]
    assert "No module named 'jsonschema'" in envelope["issues"][0]["message"]


def test_a_missing_module_crash_without_a_workspace_venv_names_bootstrap(tmp_path, monkeypatch):
    """tan-cli#652, the exact defect the issue reports: `tan validate` run
    before a successful `tan bootstrap` shells the SYSTEM interpreter (no
    workspace venv exists yet for `_planner_python_resolution` to prefer),
    which has no `jsonschema` -- and used to surface a raw
    `ModuleNotFoundError` instead of naming the real remedy. Invisible to a
    from-source install, because `jsonschema` is one of tan's own declared
    dependencies and lands on the SAME interpreter there.

    Reproduced without a real frozen binary or a genuinely dependency-less
    interpreter: `_planner_python_resolution` is monkeypatched to report
    exactly what it reports on a fresh project with no workspace venv
    (`used_workspace_venv=False`, the second element of its return), driving
    the identical traceback a bare interpreter without `jsonschema` produces
    (the sibling test above pins that same traceback with the flag left
    `True`, i.e. a workspace venv WAS found and is independently broken --
    the two must not collapse to the same message)."""
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "/sdk/scripts/validate_board_yaml.py", line 7, in <module>\n'
        "    import jsonschema\n"
        "ModuleNotFoundError: No module named 'jsonschema'\n"
    )
    project = tmp_path / "project"
    project.mkdir(parents=True)
    (project / "board.yaml").write_text(_BOARD, encoding="utf-8")
    sdk = _make_sdk(tmp_path / "alp-sdk", _stub(stderr=stderr, code=1))
    monkeypatch.chdir(project)
    monkeypatch.setattr(
        validate_cmd,
        "_planner_python_resolution",
        lambda *_a, **_k: (sys.executable, False),
    )
    result = runner.invoke(app, ["validate", "--sdk-root", str(sdk), "--format", "json"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    assert envelope["data"]["outcome"] == "failed"
    assert [i["code"] for i in envelope["issues"]] == ["validate.failed"]
    message = envelope["issues"][0]["message"]
    # The real remedy, named -- not a raw ModuleNotFoundError quoted at the
    # user with no actionable next step.
    assert "tan bootstrap" in message
    assert "jsonschema" in message
    assert "Last line of validator output" not in message


def test_a_wedged_validator_times_out_into_a_failed_verdict(tmp_path, monkeypatch):
    """The oracle's `Command::output()` has no timeout, so a wedged validator
    wedges `tan` -- and under `--format json` the consumer gets no envelope at
    all, which it cannot tell from a slow project. Bounded here."""
    monkeypatch.setattr(validate_cmd, "VALIDATOR_TIMEOUT_S", 1)
    result, _sdk = _spawn(
        tmp_path, monkeypatch, "import time\ntime.sleep(30)\n"
    )
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    assert envelope["data"]["outcome"] == "failed"
    assert [i["code"] for i in envelope["issues"]] == ["validate.failed"]
    assert "did not finish within 1s" in envelope["issues"][0]["message"]


def test_a_validator_that_cannot_be_started_is_runtime_failure(tmp_path, monkeypatch):
    """The ONE case tan-cli#262 carved out of its own exit-2 rule: the
    subprocess never started, so nothing validated anything and there is no
    verdict to report. Exit 1, and its own issue code -- reusing
    `validate.failed` would put two different exit codes behind one code."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text(_BOARD, encoding="utf-8")
    sdk = _make_sdk(tmp_path / "alp-sdk", _stub())
    monkeypatch.chdir(project)
    absent = str(tmp_path / "no-such-interpreter")
    monkeypatch.setattr(
        validate_cmd, "_planner_python_resolution", lambda *_a, **_k: (absent, True)
    )

    result = runner.invoke(
        app, ["validate", "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == int(ExitCode.RUNTIME_FAILURE), result.output
    envelope = json.loads(result.output)
    assert [i["code"] for i in envelope["issues"]] == ["validate.spawn-failed"]
    assert absent in envelope["issues"][0]["message"]


def test_legacy_fail_and_warn_lines_keep_their_own_severities(tmp_path, monkeypatch):
    """The other stderr shape `validate_board_yaml.py` emits today -- its
    `FAIL consistency: ...` line -- plus the parser rules that go with it: an
    indented continuation folds into the SAME finding (joined by two spaces,
    the oracle's own separator), a `WARN` line is a warning whatever the
    outcome, and a standalone `hint:` is a suggestion.

    The oracle spells that last severity `suggestion`; it is `note` here,
    because tan's `--format diagnostic-v1`/`sarif` documents (which the oracle
    does not have) accept only `error|warning|note`."""
    stderr = (
        "FAIL som preset: no preset for E1M-NX9999\n"
        "     expected shared definition at metadata/boards/...\n"
        "WARN hw_compat: minor version mismatch\n"
        "hint: run `alp presets` to list valid SKUs\n"
    )
    result, _sdk = _spawn(tmp_path, monkeypatch, _stub(stderr=stderr, code=1))
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    assert [(i["severity"], i["message"]) for i in envelope["issues"]] == [
        (
            "error",
            "som preset: no preset for E1M-NX9999  expected shared definition "
            "at metadata/boards/...",
        ),
        ("warning", "hw_compat: minor version mismatch"),
        ("note", "hint: run `alp presets` to list valid SKUs"),
    ]
    assert {i["code"] for i in envelope["issues"]} == {"validate.schema-violation"}


def test_the_exit_status_to_outcome_map_is_the_oracles(tmp_path, monkeypatch):
    """`classify_validation_outcome`, verbatim -- including that 2 and 3 are
    NOT `failed` (they are their own named outcomes) and that everything
    outside 0-3 is. Pinned as a unit, since today's SDK validator only ever
    exits 0 or 1 and no end-to-end test can reach the other rows."""
    assert validate_cmd.classify_validator_status(0) == "clean"
    assert validate_cmd.classify_validator_status(1) == "schema-violation"
    assert validate_cmd.classify_validator_status(2) == "missing-preset"
    assert validate_cmd.classify_validator_status(3) == "hardware-revision"
    assert validate_cmd.classify_validator_status(77) == "failed"
    assert validate_cmd.classify_validator_status(None) == "failed"


def test_an_unparseable_refusal_still_puts_one_issue_on_the_wire(tmp_path, monkeypatch):
    """A non-clean run with nothing the parser recognises must never reach a
    consumer as "exit 2, zero issues" -- which reads as no problem at all.
    `to_cli_issues`' synthesis, ported."""
    result, _sdk = _spawn(
        tmp_path, monkeypatch, _stub(stderr="something went sideways\n", code=1)
    )
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    assert envelope["data"]["issueCount"] == 1
    assert envelope["issues"][0]["message"].startswith(
        "Validation ended with outcome 'schema-violation'."
    )
    assert "something went sideways" in envelope["issues"][0]["message"]


def test_a_bogus_sdk_root_blames_the_flag_not_the_board(tmp_path, monkeypatch):
    """tan-cli#257/#258, at the call site tan-cli#376 created without it.

    `resolve_sdk_root_ladder` returns an explicit `--sdk-root` VERBATIM and
    unvalidated, so a flag pointing at a directory with no
    `scripts/alp_project.py` used to reach the spawn. The child then died with
    `python.exe: can't open file '...\\notansdk\\scripts\\
    validate_board_yaml.py': [Errno 2] No such file or directory` at exit 2 --
    which `_STATUS_OUTCOME` reads as `missing-preset`. MEASURED before the
    fix: outcome `missing-preset`, issue `validate.missing-preset`, i.e. tan
    told the customer their BOARD was wrong about a preset because the
    OPERATOR mistyped a path.

    The oracle's answer to the identical input, measured on `tan 0.4.1-dev`:
    exit 2, `validate.sdk-root-unresolved`, `outcome: "failed"`, and no `sdk`
    key at all."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text(_BOARD, encoding="utf-8")
    not_an_sdk = tmp_path / "notansdk"
    not_an_sdk.mkdir()  # no scripts/alp_project.py -- the I-31 marker
    monkeypatch.chdir(project)

    result = runner.invoke(
        app, ["validate", "--sdk-root", str(not_an_sdk), "--format", "json"]
    )
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    assert [i["code"] for i in envelope["issues"]] == ["validate.sdk-root-unresolved"]
    assert envelope["data"]["outcome"] == "failed"
    # Nothing spawned, so nothing is reported as having spawned -- and no
    # fragment of the child's own "can't open file" reaches the user.
    assert envelope["data"]["commandLine"] == ""
    assert "validate.missing-preset" not in result.output
    assert "No such file or directory" not in result.output
    # An unresolvable root is no root: the oracle reports no `sdk` block here.
    assert "sdk" not in envelope


def test_an_interpreter_below_the_sdks_own_floor_is_refused_before_the_spawn(
    tmp_path, monkeypatch
):
    """The oracle's pre-spawn guard 3 (`validate.rs:124-129`), which #376 did
    not port -- its module docstring claimed three guards while two were
    implemented.

    Without it, an interpreter below the floor reaches the spawn and dies
    INSIDE alp-sdk on `@dataclass(slots=True)`: validator exit 1 with a
    traceback, which this command reclassifies to `validate.failed` and
    reports by quoting the traceback's last line -- a `TypeError` naming
    `dataclass()`, which is the cryptic message the guard exists to replace.

    `_python_too_old` is stubbed rather than a real old interpreter being
    hunted for on the host: what is under test is that `validate` CONSULTS it
    before spawning and turns its message into `validate.python-too-old`, not
    `generate_cmd`'s own version parsing (covered in its own suite)."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text(_BOARD, encoding="utf-8")
    marker = tmp_path / "validator-ran"
    sdk = _make_sdk(
        tmp_path / "alp-sdk",
        _stub(code=0, extra=f"open({str(marker)!r}, 'w').close()\n"),
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(
        validate_cmd, "_planner_python_resolution", lambda *_a, **_k: (sys.executable, True)
    )
    monkeypatch.setattr(
        validate_cmd,
        "_python_too_old",
        lambda _python, _floor: "Python 3.9 found at `python`, but alp-sdk requires "
        "Python 3.10+. Put a newer `python` first on PATH (VS Code users can "
        "instead set alpSdk.pythonPath).",
    )

    result = runner.invoke(app, ["validate", "--sdk-root", str(sdk), "--format", "json"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    assert [i["code"] for i in envelope["issues"]] == ["validate.python-too-old"]
    assert "requires Python 3.10+" in envelope["issues"][0]["message"]
    # A guard, not a verdict: nothing ran, and nothing is reported as having.
    assert envelope["data"]["commandLine"] == ""
    assert not marker.exists(), "guard 3 must refuse BEFORE the spawn"
    # The SDK resolved -- the refusal is about the interpreter, so the block
    # naming the checkout stays, exactly as on the board-yaml-missing refusal.
    assert envelope["sdk"]["sourceTier"] == "sdkRootFlag"


def test_the_sdk_block_is_reported_exactly_where_the_oracle_reports_it(
    tmp_path, monkeypatch
):
    """`sdk: {root, sourceTier}` -- emitted by every other ported command that
    resolves an SDK, and by no branch of this one until now.

    The presence matrix is MEASURED against `tan 0.4.1-dev`, not inferred:
    present for a resolved checkout (`"sourceTier":"sdkRootFlag"`), present on
    the `board-yaml-missing` refusal when the SDK resolved and the board did
    not, and ABSENT for `--offline` even with a valid `--sdk-root` (the oracle
    resolves no SDK on that path at all -- it has no subprocess to point
    anywhere) -- so this is not "the spawn path reports an SDK" but "the
    envelope reports whatever this run resolved"."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "board.yaml").write_text(_BOARD, encoding="utf-8")
    sdk = _make_sdk(tmp_path / "alp-sdk", _stub(code=0))
    monkeypatch.chdir(project)
    monkeypatch.setattr(
        validate_cmd, "_planner_python_resolution", lambda *_a, **_k: (sys.executable, True)
    )

    spawned = json.loads(
        runner.invoke(
            app, ["validate", "--sdk-root", str(sdk), "--format", "json"]
        ).output
    )
    assert spawned["sdk"] == {"root": str(sdk).replace("\\", "/"), "sourceTier": "sdkRootFlag"}

    # `--offline` resolves nothing, so it reports nothing -- and the two
    # committed conformance fixtures are offline runs with no `sdk` key.
    offline = json.loads(
        runner.invoke(
            app, ["validate", "--offline", "--sdk-root", str(sdk), "--format", "json"]
        ).output
    )
    assert "sdk" not in offline

    # The board-yaml-missing refusal still names the checkout it did resolve.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    missing = json.loads(
        runner.invoke(
            app, ["validate", "--sdk-root", str(sdk), "--format", "json"]
        ).output
    )
    assert [i["code"] for i in missing["issues"]] == ["validate.board-yaml-missing"]
    assert missing["sdk"]["sourceTier"] == "sdkRootFlag"


def test_the_sdk_unresolved_remedy_routes_through_the_shared_next_steps(
    tmp_path, monkeypatch
):
    """tan-cli#381's fifth site. This message named `tan sdk switch
    <version|path>`, which refuses outright in this build
    (`sdk_cmd._run_not_ported`, `sdk.not-ported`) -- the #305 dead end,
    written fresh by #376 while #381 was sweeping the other four sites.

    The source-wide guard is `test_sdk_onboarding_dead_end.py`'s AST sweep;
    this pins the replacement's other half, that removing the refused verb
    left the two mechanisms that DO work still named."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, _BOARD)
    result = runner.invoke(app, ["validate", "--format", "json"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    message = json.loads(result.output)["issues"][0]["message"]
    assert "tan sdk switch" not in message
    assert "tan sdk install" not in message
    assert sdk_cmd.NO_SDK_NEXT_STEPS in message
    assert "--sdk-root" in message
    assert "--offline" in message


def _pairs(findings):
    return tuple((f.severity, f.message) for f in findings)


def test_a_rich_header_needs_a_real_alp_code(tmp_path, monkeypatch):
    """`parse_rich_header`'s own narrowness, unit-tested: `error[B005]:` is not
    a diagnostic header (no `ALP-` prefix), so the line is dropped rather than
    turned into a finding whose message is the whole line."""
    parse = validate_cmd.parse_validator_stderr
    assert parse("error[B005]: nope\n", "error") == ()
    assert _pairs(parse("error[ALP-B005]: ok\n", "error")) == (("error", "ok"),)
    assert _pairs(parse("note[ALP-Z9]: hi\n", "error")) == (("note", "hi"),)
    # A header with no arrow line is complete on its own -- the next header
    # must not be swallowed as its continuation.
    assert _pairs(parse("error[ALP-B1]: a\nerror[ALP-B2]: b\n", "error")) == (
        ("error", "a"),
        ("error", "b"),
    )
    # The code itself is retained now (tan-cli#498 defect 2) rather than
    # matched and thrown away.
    assert [f.alp_code for f in parse("error[ALP-B1]: a\nnote[ALP-Z9]: b\n", "error")] == [
        "ALP-B1",
        "ALP-Z9",
    ]


def test_a_rich_block_keeps_its_code_hint_range_and_doc_page():
    """tan-cli#498 defect 2, at the parser. Every field the SDK renders around
    the message -- the `ALP-Bxxx`, the arrow's 1-based `line:col`, the caret
    run's span, the `= hint:` remedy and the `= see:` landing page -- used to
    be consumed as block padding and dropped. Verbatim from a real
    `validate_board_yaml.py` run on alp-sdk's own
    `tests/fixtures/board_yaml_bad/ALP-B005-bad-sku.yaml`."""
    stderr = (
        "error[ALP-B005]: SoM SKU 'E1M-AEN999' does not resolve to a known module\n"
        "  --> tests/fixtures/board_yaml_bad/ALP-B005-bad-sku.yaml:2:8\n"
        "   |\n"
        " 2 |   sku: E1M-AEN999   # not a real SKU\n"
        "   |        ^^^^^^^^^^\n"
        "   = hint: did you mean 'E1M-AEN801'?\n"
        "   = see: docs/diagnostics/ALP-B005.md\n"
    )
    (finding,) = validate_cmd.parse_validator_stderr(stderr, "error")
    assert finding.severity == "error"
    assert finding.message == "SoM SKU 'E1M-AEN999' does not resolve to a known module"
    assert finding.alp_code == "ALP-B005"
    assert finding.hint == "did you mean 'E1M-AEN801'?"
    assert finding.doc_uri == "docs/diagnostics/ALP-B005.md"
    assert (finding.line, finding.col, finding.span) == (2, 8, 10)


def test_the_rich_fields_reach_both_machine_documents(tmp_path, monkeypatch):
    """The consumer-facing half of defect 2. `--format diagnostic-v1` is the
    document alp-sdk's `check_diagnostic_schema.py` is meant to point at
    instead of `alp_cli.main`, and every entry used to arrive as
    `validate-schema-violation` with no `hint`, no `documentationUri` and a
    zeroed range -- strictly less than the document it would replace, whose
    own `_diagnostic_to_json` emits `ALP-B005` plus both fields.

    The LSP range is ZERO-based (`2:8` + a 10-caret span -> line 1, characters
    7..17) and SARIF's is ONE-based, per each schema's own convention."""
    stderr = (
        "error[ALP-B005]: SoM SKU 'E1M-AEN999' does not resolve to a known module\n"
        "  --> board.yaml:2:8\n"
        "   |\n"
        " 2 |   sku: E1M-AEN999\n"
        "   |        ^^^^^^^^^^\n"
        "   = hint: did you mean 'E1M-AEN801'?\n"
        "   = see: docs/diagnostics/ALP-B005.md\n"
    )
    result, _sdk = _spawn(
        tmp_path, monkeypatch, _stub(stderr=stderr, code=1), fmt="diagnostic-v1"
    )
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    (diag,) = json.loads(result.output)["diagnostics"]
    assert diag["code"] == "ALP-B005"
    assert diag["hint"] == "did you mean 'E1M-AEN801'?"
    assert diag["documentationUri"] == "docs/diagnostics/ALP-B005.md"
    assert diag["range"] == {
        "start": {"line": 1, "character": 7},
        "end": {"line": 1, "character": 17},
    }
    # The bare message, not the envelope's human composition: the code, hint
    # and page are structured fields here, and alp-sdk's own document does the
    # same.
    assert diag["message"] == "SoM SKU 'E1M-AEN999' does not resolve to a known module"

    second = tmp_path / "sarif-run"
    second.mkdir()
    result, _sdk = _spawn(second, monkeypatch, _stub(stderr=stderr, code=1), fmt="sarif")
    run = json.loads(result.output)["runs"][0]
    assert run["tool"]["driver"]["rules"] == [
        {"id": "ALP-B005", "helpUri": "docs/diagnostics/ALP-B005.md"}
    ]
    assert run["results"][0]["ruleId"] == "ALP-B005"
    assert run["results"][0]["locations"][0]["physicalLocation"]["region"] == {
        "startLine": 2,
        "startColumn": 8,
        "endLine": 2,
        "endColumn": 18,
    }


# ───────────────── #376's acceptance criterion, on a REAL SDK ─────────────────


@pytest.mark.skipif(
    SDK is None,
    reason="set ALP_SDK_ROOT/ALP_SDK_PARITY_ROOT to a real alp-sdk checkout",
)
def test_a_real_sdk_backed_board_passes_without_offline(tmp_path, monkeypatch):
    """tan-cli#376's acceptance criterion, in the only form that can prove it:
    a valid SDK-backed board passes WITHOUT `--offline`, against alp-sdk's own
    `scripts/validate_board_yaml.py`.

    Every other spawn test above hands the command a stand-in validator, so all
    of them stay green if the argv tan builds names a script that does not
    exist, or an interpreter that cannot import what the real validator
    imports -- the stand-in is written to succeed. The one CI leg that owns a
    real checkout runs `tan validate --offline` (`.github/workflows/
    getting-started.yml`), which reaches none of this code, so before this test
    the ported spawn path had never touched a real alp-sdk anywhere.

    Runs in CI: `parity.yml`'s `python-tests` job binds `ALP_SDK_ROOT` to its
    own alp-sdk clone on ubuntu/windows/macos and runs this file.

    The board comes from `tan init` -- the same scaffold the quickstart's own
    `init` -> `validate` pair produces -- rather than a literal written here,
    so this cannot pass on a board.yaml that only tan's tests believe in.

    `_planner_python` is pinned to `sys.executable` for determinism, NOT to
    dodge a failure: it otherwise falls back to bare PATH `python`/`python3`
    (see its docstring), whose third-party imports are whatever the host
    happens to have. The interpreter running pytest has alp-tan's own
    `jsonschema`/`PyYAML` installed, which is what the real validator needs.
    """
    monkeypatch.chdir(tmp_path)
    init = runner.invoke(app, ["init", "--name", "my-app", "--format", "json"])
    assert init.exit_code == 0, init.output
    project = tmp_path / "my-app"
    assert (project / "board.yaml").is_file()

    monkeypatch.chdir(project)
    monkeypatch.setattr(
        validate_cmd, "_planner_python_resolution", lambda *_a, **_k: (sys.executable, True)
    )
    result = runner.invoke(app, ["validate", "--sdk-root", str(SDK), "--format", "json"])

    assert result.exit_code == int(ExitCode.SUCCESS), result.output
    spawned = json.loads(result.output)
    assert spawned["ok"] is True
    assert spawned["data"]["outcome"] == "clean"
    assert spawned["issues"] == []
    # The three things a stand-in cannot vouch for: a real validator ran, it
    # was alp-sdk's own script, and the checkout is named on the wire.
    command_line = spawned["data"]["commandLine"]
    assert command_line.startswith(sys.executable)
    assert str(SDK / "scripts" / "validate_board_yaml.py") in command_line
    assert spawned["sdk"]["sourceTier"] == "sdkRootFlag"

    # The other half, and the reason the `--offline` CI leg proves nothing
    # about #376: on the SAME board and the SAME checkout, `--offline` reports
    # no command line and no `sdk` block, because it runs none of the above.
    # An assertion suite that only ever exercised `--offline` would be green
    # with the whole spawn path deleted.
    offline = json.loads(
        runner.invoke(
            app, ["validate", "--offline", "--sdk-root", str(SDK), "--format", "json"]
        ).output
    )
    assert offline["data"]["outcome"] == "clean"
    assert offline["data"]["commandLine"] == ""
    assert "sdk" not in offline


def test_a_rejected_sdk_root_flag_is_named_in_the_refusal(tmp_path, monkeypatch):
    """tan-cli#497 defect 7. `--sdk-root` is TERMINAL (I-31) and
    `resolve_sdk_root_ladder` returns it verbatim and unvalidated -- this
    command's own `_is_sdk_root` re-check (see the `sdk_tier == "sdkRootFlag"`
    guard) is the ONLY place that knows the path was rejected, and it dropped
    the value. The refusal carries no `sdk` block, so nothing else in the
    envelope named it either.

    The typo'd root is a real directory missing only `scripts/alp_project.py`,
    so this pins the marker branch rather than "the path does not exist".
    """
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, _CLEAN_BOARD)
    typo = tmp_path / "alp-sdk-typo"
    typo.mkdir()
    result = runner.invoke(app, ["validate", "--sdk-root", str(typo), "--format", "json"])
    assert result.exit_code == int(ExitCode.VALIDATION_FAILURE), result.output
    envelope = json.loads(result.output)
    assert "sdk" not in envelope
    assert [i["code"] for i in envelope["issues"]] == ["validate.sdk-root-unresolved"]
    assert envelope["issues"][0]["message"] == (
        f'alp-sdk root is unresolved: --sdk-root "{typo}" is not an alp-sdk '
        "checkout (scripts/alp_project.py not found under it). Nothing was "
        "validated. `tan validate --offline` runs the structural checks that "
        "need no SDK."
    )
    # Nothing ran, so nothing is reported as having run -- unchanged by #497.
    assert envelope["data"]["commandLine"] == ""
