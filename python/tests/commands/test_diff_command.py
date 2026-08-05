# SPDX-License-Identifier: Apache-2.0
"""`tan diff` -- CLI surface tests.

`diff` is not registered in `tan.cli.app` by this change (the shared
`cli.py` registration point is owned by the orchestrator wiring commands in
parallel), so these tests mount the command on a throwaway `typer.Typer()`
rather than importing `tan.cli.app`, matching
`test_faultdecode_command.py`/`test_kconfig_command.py`'s own note for the
same situation.

Every wire-shape assertion below (exit code, issue code, `data.unchanged`,
the exact message text for `board-yaml-missing`/the `som:`-shape check) was
measured directly against `target/debug/tan.exe` (tan 0.4.1-dev) -- see the
module docstring in `tan/commands/diff_cmd.py` for the one place this port
knowingly diverges (a non-string `e1m_routes` mapping key: PyYAML raises a
`ConstructorError` at parse time, which this port reports as
`diff.schema-violation`, where the oracle's more permissive `serde_yaml` first
parses it and then fails at the JSON-serialize boundary as
`diff.board-model-not-representable`; both are exit 2). That one case is
intentionally NOT pinned here as a byte-exact oracle match -- it is covered
instead by `test_e1m_routes_non_string_key_is_a_schema_violation`, which
only pins THIS port's own (documented, self-consistent) behaviour.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tan.commands import diff_cmd
from tan.commands.diff_cmd import (
    ParseFailure,
    _inference_is_empty,
    _iot_any_enabled,
    _load_document,
    _parse_fields,
    compute_diff_entries,
)
from tan.commands.diff_cmd import diff as diff_command

from tests.parity.oracle import resolve_oracle_for_skipif

app = typer.Typer()
app.command("diff")(diff_command)

runner = CliRunner()

#: The oracle binary, resolved by the ONE resolver in the suite -- see the
#: fuller comment on the same line in `test_pinmux_command.py`. This module
#: carried the second, byte-identical copy of the release-first walk. Its own
#: oracle case passed against BOTH binaries, so the stale pick was invisible
#: here: a real `diff` envelope divergence introduced between `tan 0.3.1` and
#: the pinned `tan 0.4.1` would have been measured against the wrong baseline
#: and reported green (tan-cli#393).
_ORACLE = resolve_oracle_for_skipif()
_ORACLE_REQUIRED = pytest.mark.skipif(
    _ORACLE is None,
    reason="needs a built Rust tan (cargo build --bin tan) to measure the divergence",
)


def _run_oracle(argv: list[str], cwd: Path) -> tuple[int, dict]:
    proc = subprocess.run(
        [_ORACLE, *argv], capture_output=True, text=True, encoding="utf-8", cwd=cwd
    )
    return proc.returncode, json.loads(proc.stdout)


def _project(tmp_path: Path, board_yaml_text: str) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "board.yaml").write_text(board_yaml_text, encoding="utf-8")
    return proj


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_help_lists_quiet_and_format() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--quiet" in result.output
    assert "--format" in result.output


def test_missing_board_yaml_is_a_validation_failure(tmp_path: Path) -> None:
    proj = tmp_path / "empty"
    proj.mkdir()
    result = runner.invoke(app, ["--project", str(proj), "--format", "json"])
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is False
    assert envelope["project"]["boardYaml"] is None
    assert envelope["data"]["boardYamlPath"].endswith("board.yaml")
    assert envelope["data"]["unchanged"] is False
    assert envelope["issues"] == [
        {
            "code": "diff.board-yaml-missing",
            "severity": "error",
            "message": "board.yaml path could not be resolved or the file does not exist.",
        }
    ]


def test_v2_board_yaml_with_no_stray_fields_is_unchanged(tmp_path: Path) -> None:
    proj = _project(
        tmp_path,
        "som:\n  sku: E1M-AEN801\ncores:\n  m55_he:\n    app: ./src\n",
    )
    result = runner.invoke(app, ["--project", str(proj), "--format", "json"])
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is True
    assert envelope["data"] == {
        "schemaVersion": "1",
        "boardYamlPath": envelope["data"]["boardYamlPath"],
        "unchanged": True,
        "changeCount": 0,
        "changes": [],
    }
    assert envelope["issues"] == []


def test_v1_board_yaml_prunes_empty_libraries_iot_inference_sorted_by_path(
    tmp_path: Path,
) -> None:
    proj = _project(
        tmp_path,
        "som:\n  sku: E1M-AEN701\n"
        "libraries: []\n"
        "iot:\n  wifi: false\n  mqtt: false\n"
        "inference: {}\n",
    )
    result = runner.invoke(app, ["--project", str(proj), "--format", "json"])
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert envelope["data"]["changeCount"] == 3
    assert [c["path"] for c in envelope["data"]["changes"]] == ["inference", "iot", "libraries"]
    assert envelope["data"]["changes"][0] == {
        "path": "inference",
        "kind": "removed",
        "before": {},
    }
    assert envelope["data"]["changes"][1] == {
        "path": "iot",
        "kind": "removed",
        "before": {"wifi": False, "mqtt": False},
    }
    assert envelope["data"]["changes"][2] == {
        "path": "libraries",
        "kind": "removed",
        "before": [],
    }


def test_v2_board_yaml_drops_a_stray_top_level_os(tmp_path: Path) -> None:
    proj = _project(
        tmp_path,
        "schemaVersion: 2\nos: zephyr\nsom:\n  sku: E1M-AEN701\n"
        "cores:\n  m55_he:\n    app: ./src\n",
    )
    result = runner.invoke(app, ["--project", str(proj), "--format", "json"])
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert envelope["data"]["changes"] == [
        {"path": "os", "kind": "removed", "before": "zephyr"}
    ]


def test_som_scalar_is_a_schema_violation_with_the_oracle_wording(tmp_path: Path) -> None:
    proj = _project(tmp_path, "som: E1M-AEN701\n")
    result = runner.invoke(app, ["--project", str(proj), "--format", "json"])
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["issues"][0]["code"] == "diff.schema-violation"
    assert envelope["issues"][0]["message"] == (
        "board.yaml is not valid: `som:` must be a mapping carrying a `sku:` key, but "
        "a scalar was given ('E1M-AEN701'). Write it as:\n  som:\n    sku: <SKU>"
    )


def test_e1m_routes_non_string_key_is_a_schema_violation(tmp_path: Path) -> None:
    """Measured against the oracle: exit 2 both sides. The issue CODE and
    exact message diverge (`diff.board-model-not-representable` there,
    `diff.schema-violation` here) -- see the module docstring for why PyYAML's
    eager `ConstructorError` makes the Rust's later JSON-serialize failure
    unreachable through this port. Pinned to this port's own behaviour only.
    """
    proj = _project(tmp_path, "e1m_routes:\n  usb0:\n    ? [d_p, d_n]\n    : pads\n")
    result = runner.invoke(app, ["--project", str(proj), "--format", "json"])
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["issues"][0]["code"] == "diff.schema-violation"


# ---------------------------------------------------------------------------
# Oracle divergences fixed this round (tan-cli diff/pinmux batch) -- byte
# match confirmed against `target/debug/tan.exe` for every case below except
# `test_iot_wrong_type_message_is_a_known_divergence_from_the_oracle`, which
# is the one still-approximate message.
# ---------------------------------------------------------------------------


def test_yaml_1_1_only_bool_literal_is_a_string_not_a_type_error(tmp_path: Path) -> None:
    """BLOCKER regression: PyYAML's stock `SafeLoader` resolves YAML 1.1's
    `on`/`off`/`yes`/`no`/`y`/`n` to `bool`; `_Yaml12BoolLoader` narrows that
    to the YAML 1.2 core-schema set (`true`/`True`/`TRUE`/`false`/`False`/
    `FALSE` only), matching `serde_yaml`. Byte-matches the oracle: `os: on`
    at `schemaVersion: 1` never even reaches the `os` check (v1 leaves `os`
    alone), so the only visible effect here is `libraries: []` still pruning
    -- exactly what used to exit 2 `diff.schema-violation` before this fix.
    """
    proj = _project(tmp_path, "schemaVersion: 1\nos: on\nlibraries: []\n")
    result = runner.invoke(app, ["--project", str(proj), "--format", "json"])
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is True
    assert envelope["data"]["changes"] == [{"path": "libraries", "kind": "removed", "before": []}]


def test_yaml_1_1_only_bool_literal_survives_into_a_v2_os_diff(tmp_path: Path) -> None:
    """The same narrowing, exercised on the field it actually guards: at
    `schemaVersion: 2`, `os` IS read, and `on` must survive as the string
    `"on"` in the emitted diff entry, not a boolean. Byte-matches the oracle.
    """
    proj = _project(tmp_path, "schemaVersion: 2\nos: on\nsom:\n  sku: E1M-AEN701\n")
    result = runner.invoke(app, ["--project", str(proj), "--format", "json"])
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert envelope["data"]["changes"] == [{"path": "os", "kind": "removed", "before": "on"}]


def test_iot_wrong_type_is_a_schema_violation_not_a_false_accept(tmp_path: Path) -> None:
    """`iot: {wifi: "yes"}` used to false-ACCEPT with a FABRICATED `iot`
    removal entry: `_typed_field(doc, "iot", dict, ...)` only checked `iot`
    itself was a mapping, never that its four toggles were `bool`, so
    `_iot_any_enabled`/`_iot_pruned` treated the wrong-typed `wifi` as just
    another falsy-but-present value and pruned the whole group.
    `_check_iot_field_types` now rejects it before `compute_diff_entries`
    ever asks whether the group is prunable."""
    proj = _project(tmp_path, 'schemaVersion: 1\niot:\n  wifi: "yes"\n')
    result = runner.invoke(app, ["--project", str(proj), "--format", "json"])
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is False
    assert envelope["data"]["changes"] == []
    assert envelope["issues"][0]["code"] == "diff.schema-violation"
    assert envelope["issues"][0]["message"] == (
        "board.yaml is not valid YAML: iot.wifi: expected a boolean, got a string"
    )


@_ORACLE_REQUIRED
def test_iot_wrong_type_message_is_a_known_divergence_from_the_oracle(tmp_path: Path) -> None:
    """Exit code and issue CODE now match the oracle exactly (see
    `test_iot_wrong_type_is_a_schema_violation_not_a_false_accept` for the
    behavioural fix). The MESSAGE does not, and is not expected to:
    `_typed_nested` reports the same generic `expected X, got Y` shape every
    OTHER `_typed_field` check in this module uses (see its module
    docstring's scope note -- none of them claims to reproduce `serde_yaml`'s
    exact wording), where the oracle's struct-typed deserialize embeds the
    offending value and a line/column. Pinned literally on BOTH sides'
    message, per this repo's own convention for a deliberate divergence
    (`tests/parity/test_oracle_parity.py`'s `..._is_a_known_divergence_from_
    the_oracle` cases) -- a change to either wording, or the two converging,
    must fail this test rather than pass it silently.
    """
    proj = _project(tmp_path, 'schemaVersion: 1\niot:\n  wifi: "yes"\n')
    argv = ["--project", str(proj), "--format", "json"]
    result = runner.invoke(app, argv)
    p_out = json.loads(result.stdout)
    r_code, r_out = _run_oracle(["diff", *argv], tmp_path)

    assert result.exit_code == r_code == 2
    assert p_out["issues"][0]["code"] == r_out["issues"][0]["code"] == "diff.schema-violation"
    assert r_out["issues"][0]["message"] == (
        'board.yaml is not valid YAML: iot.wifi: invalid type: string "yes", '
        "expected a boolean at line 3 column 9"
    )
    assert p_out["issues"][0]["message"] != r_out["issues"][0]["message"]
    # Everything OUTSIDE the message is a real match, not coincidentally
    # unchecked -- exit code (asserted above), the issue code (asserted
    # above), and `data` (unchanged: false, no changes, same schema version).
    assert p_out["data"] == r_out["data"]


def test_inference_backend_non_string_scalar_is_not_falsely_pruned(tmp_path: Path) -> None:
    """`inference: {backend: 5}` used to false-ACCEPT with a FABRICATED
    `inference` removal entry: `_inference_is_empty` defaulted any non-`str`
    `backend` to `""` for its emptiness check, treating a present, non-empty
    `backend` as blank. The oracle's `backend` is a `String` field that
    coerces ANY scalar to non-empty text (`5` -> `"5"`), so it is never
    prunable here -- `unchanged: true`, matching the oracle exactly.
    """
    proj = _project(tmp_path, "schemaVersion: 1\ninference:\n  backend: 5\n")
    result = runner.invoke(app, ["--project", str(proj), "--format", "json"])
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert envelope["data"]["unchanged"] is True
    assert envelope["data"]["changes"] == []


def test_inference_default_arena_kib_wrong_type_is_a_schema_violation(tmp_path: Path) -> None:
    """Unlike `backend`, `default_arena_kib` is a real `u32` field: a
    non-integer, a bool, or a value outside `[0, u32::MAX]` is a genuine type
    mismatch on the oracle, not a leniently-coerced string. Byte-matches the
    oracle's exit code and issue code (message approximated, as elsewhere)."""
    proj = _project(tmp_path, 'schemaVersion: 1\ninference:\n  default_arena_kib: "512"\n')
    result = runner.invoke(app, ["--project", str(proj), "--format", "json"])
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["issues"][0]["code"] == "diff.schema-violation"


def _stub_sdk(tmp_path: Path, *, validator_body: str | None) -> Path:
    """A stand-in alp-sdk checkout: the loader marker `resolve_sdk` (via
    `resolve_sdk_tiered`) validates a `--sdk-root` against, plus (when given)
    the `validate_board_yaml.py` the new cross-check spawns. Passing `None`
    reproduces a stub/incomplete SDK with no validator script at all -- the
    exact shape `test_diff_sdk_root_populates_sdk_block_on_success` hands
    `diff`, which the SDK cross-check must leave passing unmodified."""
    sdk = tmp_path / "alp-sdk"
    scripts = sdk / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "alp_project.py").write_text("", encoding="utf-8")
    if validator_body is not None:
        (scripts / "validate_board_yaml.py").write_text(validator_body, encoding="utf-8")
    return sdk


def test_diff_sdk_root_populates_sdk_block_on_success(tmp_path: Path) -> None:
    """BLOCKER regression: `--sdk-root` used to be accepted and silently
    dropped -- `diff` now resolves it and echoes `sdk.root`/`sdk.sourceTier`
    on the success envelope, matching the oracle (byte-matched, including the
    `"sdkRootFlag"` source tier spelling `resolve_sdk` already shares with
    `pinmux`). Also the "no validate_board_yaml.py at all" shape
    `_reject_if_sdk_validator_disagrees` must leave a no-op."""
    sdk = _stub_sdk(tmp_path, validator_body=None)
    proj = _project(tmp_path, "som:\n  sku: E1M-AEN701\n")
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert envelope["sdk"] == {
        "root": str(sdk).replace("\\", "/"),
        "sourceTier": "sdkRootFlag",
    }


def test_diff_sdk_root_populates_sdk_block_on_board_yaml_missing_failure(tmp_path: Path) -> None:
    """The same fix, on the FAILURE envelope -- measured against the oracle:
    `diff --sdk-root <path>` against a missing board.yaml still reports the
    `sdk` block on the exit-2 envelope, not just on success."""
    sdk = _stub_sdk(tmp_path, validator_body=None)
    empty = tmp_path / "empty"
    empty.mkdir()
    result = runner.invoke(
        app, ["--project", str(empty), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 2
    envelope = json.loads(result.stdout)
    assert envelope["sdk"] == {
        "root": str(sdk).replace("\\", "/"),
        "sourceTier": "sdkRootFlag",
    }
    assert envelope["issues"][0]["code"] == "diff.board-yaml-missing"


# ---------------------------------------------------------------------------
# tan-cli#455: `diff` must refuse a board.yaml the SDK's own validator
# rejects, rather than reporting a normalization comparison it could not
# meaningfully perform. Reproduced directly against a real alp-sdk checkout
# before this fix (measured): `som.sku: E1M-NOSUCHSKU` + `preset:
# not-a-real-preset` + a `notaperipheral` peripheral token -- `tan validate`
# refuses at exit 2 with four `jsonschema` violations, `tan diff` on the
# IDENTICAL project/`--sdk-root` reported exit 0 `unchanged: true`. These
# tests drive a STAND-IN `scripts/validate_board_yaml.py`, mirroring
# `test_validate_command.py`'s own precedent, so they need no real alp-sdk
# checkout to run in CI.
# ---------------------------------------------------------------------------


def test_sdk_schema_violation_refuses_the_diff_instead_of_reporting_clean(
    tmp_path: Path, monkeypatch
) -> None:
    """THE regression: before this fix, `compute_diff_entries` never asked
    whether the SDK's own validator considers the board usable at all, so a
    board.yaml well-SHAPED enough for `diff`'s own structural checks (every
    field it reads is the right YAML type) but semantically invalid per the
    SDK's schema sailed through as a clean, meaningless comparison. On the
    pre-fix tree this stand-in reproduces that exactly: exit 0, `ok: true`,
    `unchanged: true` -- contradicting a `validate` run against the same
    stand-in, which would refuse. Post-fix: `diff` reuses `validate_cmd`'s own
    spawn-and-analyze functions and refuses too, at the SAME exit code
    `validate` would report for this outcome.
    """
    proj = _project(tmp_path, "som:\n  sku: E1M-AEN701\n")
    sdk = _stub_sdk(
        tmp_path,
        validator_body=(
            "import sys\n"
            "sys.stderr.write(\"FAIL board.yaml: 'notaperipheral' is not one of the "
            "known peripherals\\n\")\n"
            "sys.exit(1)\n"
        ),
    )
    monkeypatch.setattr(diff_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 2, result.output
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is False
    assert envelope["data"]["unchanged"] is False
    assert envelope["data"]["changes"] == []
    assert envelope["issues"][0]["code"] == "diff.schema-violation"
    assert "notaperipheral" in envelope["issues"][0]["message"]
    # The `sdk` block is still carried through to the failure envelope, the
    # same guarantee `board-yaml-missing` already gives (see the test above).
    assert envelope["sdk"]["sourceTier"] == "sdkRootFlag"


def test_sdk_clean_verdict_leaves_a_real_comparison_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    """The other half: an SDK-validated CLEAN board must still get diff's own
    comparison, not a false refusal just because the cross-check ran."""
    proj = _project(tmp_path, "libraries: []\n")
    sdk = _stub_sdk(
        tmp_path, validator_body="import sys\nsys.stdout.write('board.yaml: clean\\n')\n"
    )
    monkeypatch.setattr(diff_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is True
    assert envelope["data"]["changes"] == [{"path": "libraries", "kind": "removed", "before": []}]


# ---------------------------------------------------------------------------
# tan-cli#455 review round: three MAJOR findings against the cross-check
# above. All three shared one root shape -- a spawn that STARTS and then
# fails to reach a verdict (or must never be allowed to start at all) used to
# either report a clean diff (reopening #455 for a narrower trigger) or get
# flattened into `diff.schema-violation` (misreporting a broken validator
# ENVIRONMENT as an invalid board). Mirrors `test_validate_command.py`'s own
# precedent for the identical guards (`test_a_wedged_validator_times_out_...`,
# `test_a_validator_that_cannot_be_started_is_runtime_failure`,
# `test_validate_refuses_before_spawning_a_too_old_interpreter` -- see that
# file for the exact names).
# ---------------------------------------------------------------------------


def test_sdk_validator_timeout_refuses_instead_of_reporting_clean(
    tmp_path: Path, monkeypatch
) -> None:
    """MAJOR (finding 1): the first cut's blanket `except (OSError, ValueError,
    subprocess.SubprocessError): return` also caught `TimeoutExpired` (a
    `SubprocessError` subclass), so a wedged validator silently fell back to
    a clean diff -- reopening #455 for a narrower trigger. Post-fix: a
    timeout refuses as `diff.failed`, matching `validate.failed`'s own
    tan-cli#262 shape (exit 2, not a launch failure)."""
    proj = _project(tmp_path, "som:\n  sku: E1M-AEN701\n")
    sdk = _stub_sdk(tmp_path, validator_body="import time\ntime.sleep(30)\n")
    monkeypatch.setattr(diff_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    monkeypatch.setattr(diff_cmd, "VALIDATOR_TIMEOUT_S", 1)
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 2, result.output
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is False
    assert envelope["data"]["unchanged"] is False
    assert envelope["issues"][0]["code"] == "diff.failed"
    assert "did not finish within 1s" in envelope["issues"][0]["message"]


def test_sdk_validator_unstartable_interpreter_refuses_instead_of_reporting_clean(
    tmp_path: Path, monkeypatch
) -> None:
    """MAJOR (finding 1): the same blanket `except` also caught a plain
    `OSError` (no interpreter at the resolved path), so this reached a clean
    diff too. Post-fix: `diff.spawn-failed` at `RUNTIME_FAILURE` (exit 1),
    mirroring `validate.spawn-failed` -- never a clean verdict from a spawn
    that never started."""
    proj = _project(tmp_path, "som:\n  sku: E1M-AEN701\n")
    sdk = _stub_sdk(tmp_path, validator_body="import sys\nsys.exit(0)\n")
    absent = str(tmp_path / "no-such-interpreter")
    monkeypatch.setattr(diff_cmd, "_planner_python", lambda *_a, **_k: absent)
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 1, result.output
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is False
    assert envelope["data"]["unchanged"] is False
    assert envelope["issues"][0]["code"] == "diff.spawn-failed"
    assert absent in envelope["issues"][0]["message"]


def test_sdk_validator_crash_is_reported_as_failed_not_schema_violation(
    tmp_path: Path, monkeypatch
) -> None:
    """MAJOR (finding 2): the first cut hardcoded
    `ParseFailure("schema-violation", ...)` for EVERY non-clean outcome, so a
    validator ENVIRONMENT crash (exit 1 WITH a Python traceback --
    `analyze_validator_output` already reclassifies this off
    `schema-violation` to `failed`, exactly to stop a broken environment
    reading as a board defect) still reached the wire as
    `diff.schema-violation`. Post-fix: `_reject_if_sdk_validator_disagrees`
    raises `ParseFailure(result.outcome, ...)`, so the real outcome
    (`failed`) is what reaches the wire."""
    proj = _project(tmp_path, "som:\n  sku: E1M-AEN701\n")
    sdk = _stub_sdk(
        tmp_path,
        validator_body=(
            "raise TypeError(\"dataclass() got an unexpected keyword argument "
            "'slots'\")\n"
        ),
    )
    monkeypatch.setattr(diff_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 2, result.output
    envelope = json.loads(result.stdout)
    assert envelope["issues"][0]["code"] == "diff.failed"
    assert "unexpected keyword argument 'slots'" in envelope["issues"][0]["message"]


def test_sdk_validator_below_the_floor_refuses_before_spawning(
    tmp_path: Path, monkeypatch
) -> None:
    """MAJOR (finding 3): the first cut never ported `validate_cmd`'s guard 3
    (`resolve_manifest_python_floor` + `_python_too_old`), so an interpreter
    below the SDK's declared floor reached the spawn and crashed inside
    alp-sdk instead of getting the friendly `python-too-old` refusal --
    exactly the traceback finding 2's test reproduces directly. Post-fix:
    the guard refuses BEFORE the spawn. `_python_too_old` is stubbed rather
    than a real old interpreter being hunted for on the host, mirroring
    `test_validate_command.py`'s own precedent for the identical guard; the
    validator writes a marker file if it ever actually runs, which it must
    not."""
    proj = _project(tmp_path, "som:\n  sku: E1M-AEN701\n")
    marker = tmp_path / "validator-ran"
    sdk = _stub_sdk(tmp_path, validator_body=f"open({str(marker)!r}, 'w').close()\n")
    monkeypatch.setattr(diff_cmd, "_planner_python", lambda *_a, **_k: sys.executable)
    monkeypatch.setattr(
        diff_cmd,
        "_python_too_old",
        lambda _python, _floor: "Python 3.9 found at `python`, but alp-sdk requires "
        "Python 3.10+. Put a newer `python` first on PATH.",
    )
    result = runner.invoke(
        app, ["--project", str(proj), "--sdk-root", str(sdk), "--format", "json"]
    )
    assert result.exit_code == 2, result.output
    envelope = json.loads(result.stdout)
    assert envelope["issues"][0]["code"] == "diff.python-too-old"
    assert "requires Python 3.10+" in envelope["issues"][0]["message"]
    assert not marker.exists(), "guard 3 must refuse BEFORE the spawn"


def test_text_mode_reports_no_differences(tmp_path: Path) -> None:
    proj = _project(tmp_path, "som:\n  sku: E1M-AEN701\n")
    result = runner.invoke(app, ["--project", str(proj)])
    assert result.exit_code == 0
    assert "diff: no effective-config differences detected." in result.output


def test_quiet_suppresses_per_change_lines_but_keeps_the_summary(tmp_path: Path) -> None:
    proj = _project(tmp_path, "libraries: []\n")
    loud = runner.invoke(app, ["--project", str(proj)])
    quiet = runner.invoke(app, ["--project", str(proj), "--quiet"])
    assert "REMOVED libraries" in loud.output
    assert "REMOVED libraries" not in quiet.output
    assert "diff: 1 differences in" in quiet.output


def test_pyyaml_unavailable_refuses_with_runtime_failure(tmp_path: Path, monkeypatch) -> None:
    proj = _project(tmp_path, "som:\n  sku: E1M-AEN701\n")
    monkeypatch.setitem(sys.modules, "yaml", None)
    result = runner.invoke(app, ["--project", str(proj), "--format", "json"])
    assert result.exit_code == 1
    envelope = json.loads(result.stdout)
    assert envelope["issues"][0]["code"] == "diff.pyyaml-unavailable"


# ---------------------------------------------------------------------------
# Pure-function unit tests (mirrors `crates/tan-core/src/model.rs`'s and
# `diff.rs`'s own `#[cfg(test)]` modules)
# ---------------------------------------------------------------------------


def test_iot_any_enabled_requires_an_explicit_true():
    assert _iot_any_enabled({}) is False
    assert _iot_any_enabled({"wifi": False, "mqtt": False}) is False
    assert _iot_any_enabled({"wifi": True}) is True


def test_inference_is_empty_treats_absent_and_blank_backend_alike():
    assert _inference_is_empty({}) is True
    assert _inference_is_empty({"backend": ""}) is True
    assert _inference_is_empty({"backend": "cpu"}) is False
    assert _inference_is_empty({"default_arena_kib": 0}) is False


def test_compute_diff_entries_is_removed_only_and_version_gated():
    # v1: os is left alone even when present.
    assert compute_diff_entries(1, "zephyr", None, None, None) == []
    # v2: libraries/iot/inference are left alone even when prunable.
    assert compute_diff_entries(2, None, [], {}, {}) == []
    # v2 clears a present os.
    entries = compute_diff_entries(2, "zephyr", None, None, None)
    assert len(entries) == 1
    assert entries[0].path == "os"
    assert entries[0].kind == "removed"


def test_load_document_wraps_yaml_errors_as_schema_violation():
    try:
        _load_document(": : not yaml : :")
    except ParseFailure as failure:
        assert failure.code == "schema-violation"
        assert failure.message.startswith("board.yaml is not valid YAML: ")
    else:
        raise AssertionError("expected a ParseFailure")


def test_parse_fields_rejects_wrong_typed_iot():
    try:
        _parse_fields({"iot": "notadict"})
    except ParseFailure as failure:
        assert failure.code == "schema-violation"
        assert "iot" in failure.message
    else:
        raise AssertionError("expected a ParseFailure")


def test_parse_fields_default_document_is_v1_with_nothing_to_prune():
    assert _parse_fields(None) == (1, None, None, None, None)
    assert _parse_fields({}) == (1, None, None, None, None)
