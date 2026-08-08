# SPDX-License-Identifier: Apache-2.0
"""`tan inspect` -- port of `crates/tan-cli/src/commands/inspect.rs`.

Every shape asserted below was measured against a freshly-built oracle
(`cargo build -p alp-tan-cli --bin tan` from THIS worktree's `crates/`, not
the possibly-stale `dev`-branch binary -- see `inspect_cmd`'s module
docstring for why that distinction matters here specifically).

`inspect`/`trace`/`support-bundle` are not yet registered in `tan.cli.app`
(that registration is the orchestrator's, not this unit's, to make -- see
`deferred_cmd.py`'s module docstring), so these tests build a throwaway
local Typer app around the ported command function directly, with a minimal
root callback that reproduces `tan.cli.root`'s `ctx.obj = {"format": ...}`
wiring closely enough to exercise the leading-`--format` path.
"""
from __future__ import annotations

import json

import typer
from typer.testing import CliRunner

from tan.commands import sdk_cmd
from tan.commands.inspect_cmd import (
    ResolvedDebugContext,
    _format_value_text,
    collect_resolved_values,
    filter_resolved_values,
    inspect,
    resolve_debug_project_context,
)
from tan.envelope import Project, SdkInfo


def _local_app():
    """A throwaway Typer app wrapping just [`inspect`], with a minimal root
    callback reproducing `tan.cli.root`'s `ctx.obj = {"format": ...}` wiring --
    `inspect` is not yet registered in the real `tan.cli.app` (that
    registration is the orchestrator's to make, not this unit's; see
    `deferred_cmd.py`'s module docstring), so testing through it would still
    exercise the OLD deferred stub."""
    local = typer.Typer()

    @local.callback(invoke_without_command=True)
    def root(ctx: typer.Context, output_format: str = typer.Option(None, "--format")) -> None:
        ctx.obj = {"format": output_format}

    local.command("inspect")(inspect)
    return local


app = _local_app()
runner = CliRunner()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def sdk_at(root):
    write(root / "scripts" / "alp_project.py", "# stub")


# ---------------------------------------------------------------------------
# The pure model -- collect_resolved_values / filter_resolved_values
# ---------------------------------------------------------------------------


def _context(**overrides) -> ResolvedDebugContext:
    defaults = dict(
        workspace_root="/work/proj",
        sdk_root=None,
        sdk_tier="none",
        board_yaml_path="/work/proj/board.yaml",
        board_yaml_exists=False,
        west_cwd="/work/proj",
        python_binary="python3",
        project=Project.resolved("/work/proj", "/work/proj/board.yaml"),
        sdk=None,
    )
    defaults.update(overrides)
    return ResolvedDebugContext(**defaults)


def test_six_rows_in_oracle_order_with_unresolved_sdk():
    values = collect_resolved_values(_context())
    assert [v["key"] for v in values] == [
        "workspaceRoot",
        "sdkRoot",
        "boardYamlPath",
        "boardYamlExists",
        "westCwd",
        "pythonBinary",
    ]
    sdk_row = next(v for v in values if v["key"] == "sdkRoot")
    assert sdk_row["value"] is None
    assert sdk_row["source"] == "unresolved"
    # tan-cli#381: used to require `tan sdk switch` here -- a subcommand this
    # build refuses. The row now carries the shared `NO_SDK_NEXT_STEPS`, so
    # this pins the mechanism that works and nothing about the wording.
    assert "--sdk-root" in sdk_row["detail"]
    assert sdk_cmd.NO_SDK_NEXT_STEPS in sdk_row["detail"]


def test_resolved_sdk_row_reports_workspace_source():
    values = collect_resolved_values(
        _context(sdk_root="/work/alp-sdk", sdk=SdkInfo("/work/alp-sdk", "sdkRootFlag"))
    )
    sdk_row = next(v for v in values if v["key"] == "sdkRoot")
    assert sdk_row == {
        "key": "sdkRoot",
        "value": "/work/alp-sdk",
        "source": "workspace",
        "detail": "Resolved alp-sdk root used for scripts and schemas.",
    }


def test_board_yaml_exists_flips_source_detail_only():
    missing = collect_resolved_values(_context(board_yaml_exists=False))
    present = collect_resolved_values(_context(board_yaml_exists=True))
    m = next(v for v in missing if v["key"] == "boardYamlExists")
    p = next(v for v in present if v["key"] == "boardYamlExists")
    assert m["value"] is False and "missing" in m["detail"]
    assert p["value"] is True and "exists" in p["detail"]
    assert m["source"] == p["source"] == "runtime"


def test_west_cwd_and_python_binary_are_always_setting_and_default():
    """No `--west-cwd`/`--python-path` flag exists on this CLI -- both rows
    always report the always-populated sources, never `unresolved`."""
    values = collect_resolved_values(_context())
    west = next(v for v in values if v["key"] == "westCwd")
    py = next(v for v in values if v["key"] == "pythonBinary")
    assert west["source"] == "setting"
    assert west["value"] == "/work/proj"
    assert py["source"] == "default"


def test_filter_matches_exact_dotted_and_bracketed_keys():
    values = [
        {"key": "a", "value": 1},
        {"key": "a.b", "value": 2},
        {"key": "a[0]", "value": 3},
        {"key": "ab", "value": 4},
    ]
    assert [v["key"] for v in filter_resolved_values(values, "a")] == ["a", "a.b", "a[0]"]
    assert filter_resolved_values(values, None) == values
    assert filter_resolved_values(values, "nomatch") == []


# ---------------------------------------------------------------------------
# resolve_debug_project_context
# ---------------------------------------------------------------------------


def test_context_resolution_posix_paths_and_absolute_board_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "som:\n  sku: E1M-X\n")
    ctx = resolve_debug_project_context(None, None, None)
    assert ctx.workspace_root == str(tmp_path).replace("\\", "/")
    assert ctx.board_yaml_path == f"{ctx.workspace_root}/board.yaml"
    assert ctx.board_yaml_exists is True
    assert ctx.west_cwd == ctx.workspace_root
    assert ctx.sdk_root is None
    assert ctx.sdk is None

    # An explicit absolute --board-yaml is reported as given, not re-joined.
    elsewhere = tmp_path / "elsewhere.yaml"
    write(elsewhere, "x")
    ctx2 = resolve_debug_project_context(None, str(elsewhere), None)
    assert ctx2.board_yaml_path == str(elsewhere).replace("\\", "/")


def test_context_resolves_sdk_root_via_the_narrow_ladder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sdk = tmp_path / "alp-sdk"
    sdk_at(sdk)
    ctx = resolve_debug_project_context(None, None, str(sdk))
    assert ctx.sdk_root == str(sdk).replace("\\", "/")
    assert ctx.sdk_tier == "sdkRootFlag"
    assert ctx.sdk == SdkInfo(ctx.sdk_root, "sdkRootFlag")


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_json_envelope_reports_all_six_values_and_no_issues(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "som:\n  sku: E1M-X\n")
    result = runner.invoke(app, ["inspect", "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["command"] == "inspect"
    assert doc["ok"] is True
    assert doc["exitCode"] == 0
    assert doc["project"]["boardYaml"] is not None
    assert "sdk" not in doc  # nothing resolved
    assert len(doc["data"]["resolvedValues"]) == 6
    assert doc["issues"] == []


def test_missing_board_yaml_is_a_warning_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["inspect", "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["ok"] is True
    assert doc["project"]["boardYaml"] is None
    assert doc["issues"] == [
        {
            "code": "inspect.board-yaml-missing",
            "severity": "warning",
            "message": "board.yaml path could not be resolved or the file does not exist.",
        }
    ]


def test_path_filter_narrows_and_warns_when_nothing_matches(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "x")
    ok = runner.invoke(app, ["inspect", "--path", "sdkRoot", "--format", "json"])
    doc = json.loads(ok.stdout)
    assert [v["key"] for v in doc["data"]["resolvedValues"]] == ["sdkRoot"]
    assert doc["issues"] == []

    empty = runner.invoke(app, ["inspect", "--path", "bogus.path", "--format", "json"])
    doc2 = json.loads(empty.stdout)
    assert doc2["data"]["resolvedValues"] == []
    assert doc2["issues"] == [
        {
            "code": "inspect.path-not-found",
            "severity": "warning",
            "message": "No resolved values match --path 'bogus.path'.",
        }
    ]
    # Still exit 0 -- inspect has no failure exit in the oracle.
    assert empty.exit_code == 0


def test_text_mode_writes_nothing_to_stdout_and_a_count_line_to_stderr(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "x")
    result = runner.invoke(app, ["inspect"])
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "inspect: resolved values=6" in result.stderr


def test_quiet_suppresses_per_value_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["inspect", "--quiet"])
    assert "inspect: resolved values=" in result.stderr
    assert "workspaceRoot=" not in result.stderr


def test_show_origin_adds_source_and_detail_to_text_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["inspect", "--show-origin"])
    assert "source=workspace" in result.stderr
    assert "detail=" in result.stderr


def test_leading_format_json_before_the_subcommand_reaches_the_command(tmp_path, monkeypatch):
    """clap makes `--format` global; `tan --format json inspect` must reach the
    envelope path, not a Click usage error."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--format", "json", "inspect"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["command"] == "inspect"


def test_hidden_global_flags_are_accepted_without_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["inspect", "--verbose", "--no-color", "--non-interactive", "--ci", "--all"]
    )
    assert result.exit_code == 0


def test_a_bad_format_is_a_usage_error_not_a_traceback():
    result = runner.invoke(app, ["inspect", "--format", "yaml"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_format_value_text_prints_non_ascii_raw_not_as_a_u_escape():
    # tan-cli#499: `json.dumps`'s default (`ensure_ascii=True`) mangles any
    # non-ASCII path into `\uXXXX` escapes -- no longer copy-pasteable back
    # onto the real path on disk, and no longer equal to it byte-for-byte.
    # `serde_json::to_string` (what the oracle text-renders with) never does
    # this; matches `diff_cmd._format_value`'s identical, already-fixed case.
    rendered = _format_value_text("/scratch/josé-proj")
    assert rendered == '"/scratch/josé-proj"'
    assert "\\u" not in rendered


def test_text_mode_prints_a_non_ascii_workspace_root_unescaped(tmp_path, monkeypatch):
    proj = tmp_path / "josé-proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    result = runner.invoke(app, ["inspect"])
    assert result.exit_code == 0
    assert "josé-proj" in result.stderr
    assert "\\u" not in result.stderr
