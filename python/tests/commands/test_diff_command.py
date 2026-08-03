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


def test_diff_sdk_root_populates_sdk_block_on_success(tmp_path: Path) -> None:
    """BLOCKER regression: `--sdk-root` used to be accepted and silently
    dropped -- `diff` now resolves it and echoes `sdk.root`/`sdk.sourceTier`
    on the success envelope, matching the oracle (byte-matched, including the
    `"sdkRootFlag"` source tier spelling `resolve_sdk` already shares with
    `pinmux`)."""
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
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
    sdk = tmp_path / "sdk"
    (sdk / "scripts").mkdir(parents=True)
    (sdk / "scripts" / "alp_project.py").write_text("", encoding="utf-8")
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
