# SPDX-License-Identifier: Apache-2.0
"""`tan trace` -- port of `crates/tan-cli/src/commands/trace.rs`.

Every shape asserted below was measured against a freshly-built oracle
(`cargo build -p alp-tan-cli --bin tan` from THIS worktree's `crates/`) --
including the mixed-separator `outputPath`/command-line shape, which is a
genuine byte-for-byte requirement, not a stylistic choice (see
`trace_cmd`'s module docstring).

`trace` is not yet registered in `tan.cli.app` (the orchestrator's to wire,
per `deferred_cmd.py`'s module docstring), so these tests build a throwaway
local Typer app around the ported command function directly.
"""
from __future__ import annotations

import json
import os

import pytest
import typer
from typer.testing import CliRunner

from tan.commands.trace_cmd import (
    BUILD_CONFIG_EMIT_MODES,
    TraceTargetError,
    _loader_plan,
    build_trace_decisions,
    resolve_trace_targets,
    trace,
)


def _local_app():
    local = typer.Typer()

    @local.callback(invoke_without_command=True)
    def root(ctx: typer.Context, output_format: str = typer.Option(None, "--format")) -> None:
        ctx.obj = {"format": output_format}

    local.command("trace")(trace)
    return local


app = _local_app()
runner = CliRunner()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def sdk_at(root):
    write(root / "scripts" / "alp_project.py", "# stub")


# ---------------------------------------------------------------------------
# Target resolution -- deliberately the narrower build-config set
# ---------------------------------------------------------------------------


def test_default_targets_are_exactly_the_build_config_set_in_order():
    assert resolve_trace_targets(None) == (
        "zephyr-conf",
        "dts-overlay",
        "cmake-args",
        "yocto-conf",
    )
    assert BUILD_CONFIG_EMIT_MODES == resolve_trace_targets(None)


def test_a_generate_only_target_is_not_a_valid_trace_target():
    """`carrier-netlist` is a real `tan generate --target`, but a build never
    materialises it -- `tan trace` must still refuse it (tan-cli#165 review
    finding 1)."""
    with pytest.raises(TraceTargetError) as excinfo:
        resolve_trace_targets("carrier-netlist")
    assert str(excinfo.value) == (
        "Unsupported trace target 'carrier-netlist'. Allowed values: "
        "zephyr-conf, dts-overlay, cmake-args, yocto-conf."
    )


def test_a_known_target_narrows_to_one():
    assert resolve_trace_targets("cmake-args") == ("cmake-args",)


# ---------------------------------------------------------------------------
# _loader_plan -- the exact mixed-separator join shape
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="the mixed-separator shape is Windows-specific")
def test_loader_plan_matches_the_oracles_single_join_shape_on_windows():
    output_path, command_line = _loader_plan(
        "C:/proj", "C:/sdk", "C:/proj/board.yaml", "python", "zephyr-conf"
    )
    assert output_path == "C:/proj\\build/generated/alp.conf"
    assert command_line == (
        "python C:/sdk\\scripts\\alp_project.py --input C:/proj/board.yaml "
        "--emit zephyr-conf --output C:/proj\\build/generated/alp.conf"
    )


def test_loader_plan_output_path_and_command_line_shape_is_platform_consistent():
    """Regardless of platform, `os.path.join` used exactly once per Rust
    `.join()` call reproduces the oracle -- on POSIX this is simply forward
    slashes throughout, no divergence to pin."""
    output_path, command_line = _loader_plan(
        "/proj", "/sdk", "/proj/board.yaml", "python3", "dts-overlay"
    )
    assert output_path == os.path.join("/proj", "build/generated/alp.overlay")
    assert "alp_project.py" in command_line
    assert "--emit dts-overlay" in command_line


# ---------------------------------------------------------------------------
# build_trace_decisions
# ---------------------------------------------------------------------------


def test_decisions_carry_one_entry_per_target_plus_a_focus_entry():
    decisions = build_trace_decisions(
        "/proj", "/sdk", "/proj/board.yaml", "python3", ("cmake-args",), "som.sku"
    )
    assert [d["key"] for d in decisions] == [
        "generation.target.cmake-args",
        "config.path.som.sku",
    ]
    assert decisions[0]["outcome"] == "planned"
    assert decisions[0]["outputPath"].endswith("alp-cmake-args.txt")
    assert "outputPath" not in decisions[1]  # the focus decision carries none
    assert decisions[1]["detail"] == (
        "Path-level tracing is currently static and reports planning context only."
    )


def test_no_focus_means_no_config_path_entry():
    decisions = build_trace_decisions(
        "/proj", "/sdk", "/proj/board.yaml", "python3", BUILD_CONFIG_EMIT_MODES, None
    )
    assert len(decisions) == 4
    assert all(d["key"].startswith("generation.target.") for d in decisions)


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_sdk_root_unresolved_is_a_validation_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "board.yaml", "x")
    result = runner.invoke(app, ["trace", "--format", "json"])
    assert result.exit_code == 2
    doc = json.loads(result.stdout)
    assert doc["ok"] is False
    assert doc["project"] == {"root": None, "boardYaml": None}
    assert "sdk" not in doc
    assert doc["data"]["decisions"] == []
    assert doc["issues"] == [
        {
            "code": "trace.sdk-root-unresolved",
            "severity": "error",
            # tan-cli#381: was "pin one with `tan sdk switch <version|path>`",
            # a subcommand this build refuses. Now the shared
            # `sdk_cmd.NO_SDK_NEXT_STEPS` tail, same as generate/model/kconfig.
            "message": (
                "alp-sdk root is unresolved. Use --sdk-root, place the project near "
                "an alp-sdk checkout, or get an alp-sdk checkout (`git clone "
                "https://github.com/alplabai/alp-sdk`), then point tan at it with "
                "`--sdk-root <path>`."
            ),
        }
    ]


def test_missing_board_yaml_is_a_validation_failure_with_sdk_still_reported(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    sdk = tmp_path / "alp-sdk"
    sdk_at(sdk)
    result = runner.invoke(app, ["trace", "--sdk-root", str(sdk), "--format", "json"])
    assert result.exit_code == 2
    doc = json.loads(result.stdout)
    assert doc["ok"] is False
    # sdk WAS resolved -- still reported on this failure path, unlike project.
    assert doc["sdk"]["sourceTier"] == "sdkRootFlag"
    assert doc["project"] == {"root": None, "boardYaml": None}
    assert doc["issues"][0]["code"] == "trace.board-yaml-missing"


def test_unknown_target_is_an_internal_failure_with_null_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sdk = tmp_path / "alp-sdk"
    sdk_at(sdk)
    write(tmp_path / "board.yaml", "x")
    result = runner.invoke(
        app, ["trace", "--sdk-root", str(sdk), "--target", "bogus", "--format", "json"]
    )
    assert result.exit_code == 5
    doc = json.loads(result.stdout)
    assert doc["data"]["target"] is None
    assert doc["issues"][0]["code"] == "trace.internal-failure"
    assert "bogus" in doc["issues"][0]["message"]


def test_default_traces_all_four_targets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sdk = tmp_path / "alp-sdk"
    sdk_at(sdk)
    write(tmp_path / "board.yaml", "x")
    result = runner.invoke(app, ["trace", "--sdk-root", str(sdk), "--format", "json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["ok"] is True
    assert doc["data"]["target"] is None  # null when more than one target ran
    assert len(doc["data"]["decisions"]) == 4
    assert doc["issues"] == []


def test_single_target_reports_target_and_one_decision(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sdk = tmp_path / "alp-sdk"
    sdk_at(sdk)
    write(tmp_path / "board.yaml", "x")
    result = runner.invoke(
        app, ["trace", "--sdk-root", str(sdk), "--target", "cmake-args", "--format", "json"]
    )
    doc = json.loads(result.stdout)
    assert doc["data"]["target"] == "cmake-args"
    assert len(doc["data"]["decisions"]) == 1


def test_all_flag_is_inert_target_still_wins(tmp_path, monkeypatch):
    """Measured against the oracle: `--target X --all` still narrows to `X`;
    `--all` alone matches the bare default. `resolve_targets` never reads it."""
    monkeypatch.chdir(tmp_path)
    sdk = tmp_path / "alp-sdk"
    sdk_at(sdk)
    write(tmp_path / "board.yaml", "x")
    both = runner.invoke(
        app,
        ["trace", "--sdk-root", str(sdk), "--target", "cmake-args", "--all", "--format", "json"],
    )
    assert json.loads(both.stdout)["data"]["target"] == "cmake-args"

    all_only = runner.invoke(app, ["trace", "--sdk-root", str(sdk), "--all", "--format", "json"])
    assert len(json.loads(all_only.stdout)["data"]["decisions"]) == 4


def test_text_mode_decision_count_and_quiet(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sdk = tmp_path / "alp-sdk"
    sdk_at(sdk)
    write(tmp_path / "board.yaml", "x")
    verbose = runner.invoke(app, ["trace", "--sdk-root", str(sdk)])
    assert verbose.stdout == ""
    assert "trace: decisions=4" in verbose.stderr
    assert "[planned] generation.target.zephyr-conf" in verbose.stderr

    quiet = runner.invoke(app, ["trace", "--sdk-root", str(sdk), "--quiet"])
    assert "trace: decisions=4" in quiet.stderr
    assert "generation.target" not in quiet.stderr


def test_a_bad_format_is_a_usage_error_not_a_traceback():
    result = runner.invoke(app, ["trace", "--format", "yaml"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output
