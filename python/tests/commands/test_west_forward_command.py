# SPDX-License-Identifier: Apache-2.0
"""`tan migrate` / `tan lock` / `tan quality` -- the shared `west`-forwarding
plumbing (`west_forward_cmd.py`), ported from
`crates/tan-cli/src/commands/build/workspace.rs`. No real `west` is spawned:
every test monkeypatches `west_forward_cmd.subprocess.run` to a stub, mirroring
`test_monitor_command.py`'s guard-not-assumed discipline.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tan.commands import west_forward_cmd
from tan.commands.west_forward_cmd import (
    FORWARD_CONTEXT_SETTINGS,
    _west_argv,
    _west_workspace_dir,
    lock,
    migrate,
    quality,
)

app = typer.Typer(add_completion=False)
app.command("migrate", context_settings=FORWARD_CONTEXT_SETTINGS)(migrate)
app.command("lock", context_settings=FORWARD_CONTEXT_SETTINGS)(lock)
app.command("quality", context_settings=FORWARD_CONTEXT_SETTINGS)(quality)

runner = CliRunner()


def envelope(result):
    assert result.stdout.count("\n") == 1, result.stdout
    return json.loads(result.stdout)


# --- pure helpers ------------------------------------------------------------


def test_west_argv_prefixes_alp_verb_and_forwards_verbatim():
    assert _west_argv("migrate", ["--core", "m55_hp"]) == ["alp-migrate", "--core", "m55_hp"]
    assert _west_argv("lock", []) == ["alp-lock"]


def test_west_workspace_dir_walks_up_from_start(tmp_path):
    top = tmp_path / "ws"
    (top / ".west").mkdir(parents=True)
    nested = top / "app" / "sub"
    nested.mkdir(parents=True)
    assert _west_workspace_dir(str(nested), None) == top


def test_west_workspace_dir_is_none_when_nothing_resolves(monkeypatch, tmp_path):
    # Step 2 of `_west_workspace_dir` reads the REAL `$ZEPHYR_BASE` -- on a host
    # that exports one (any Zephyr dev shell), that env would resolve a
    # workspace out from under this test and the "nothing resolves" assertion
    # would go red for reasons that have nothing to do with `lonely`.
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    assert _west_workspace_dir(str(lonely), None) is None


def test_west_workspace_dir_falls_back_to_sdk_parent(tmp_path):
    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    (tmp_path / ".west").mkdir()
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    assert _west_workspace_dir(str(lonely), sdk_root) == tmp_path


def test_west_workspace_dir_skips_an_ancestor_whose_manifest_does_not_match(tmp_path):
    """tan-cli#307: an ancestor `.west` ABOVE the project -- a second Zephyr
    checkout, a vendor SDK, an old workspace -- is a real west workspace by
    the bare-directory test alone, but must not shadow the workspace `tan
    bootstrap` actually created purely because it sits closer to `start`.
    Mirrors `test_venv.py`'s `test_find_workspace_venv_refuses_a_manifest_
    mismatched_zephyr_base_venv_when_sdk_root_is_known`, now applied to the
    plain upward walk instead of the `$ZEPHYR_BASE` candidate -- the exact
    scenario a maintainer's dirty-host e2e reproduced: an ancestor `.west`
    whose manifest names `unrelated`, with the real workspace a SIBLING, not
    an ancestor, of the project (so only the sdk-derived fallback below can
    ever find it)."""
    ancestor = tmp_path / "dirty"
    (ancestor / ".west").mkdir(parents=True)
    (ancestor / ".west" / "config").write_text("[manifest]\npath = unrelated\n", encoding="utf-8")

    real_ws = tmp_path / "dirty-ws"
    sdk_root = real_ws / "alp-sdk"
    sdk_root.mkdir(parents=True)
    (real_ws / ".west").mkdir()
    (real_ws / ".west" / "config").write_text("[manifest]\npath = alp-sdk\n", encoding="utf-8")

    project = ancestor / "work" / "proj"
    project.mkdir(parents=True)

    assert _west_workspace_dir(str(project), sdk_root) == real_ws


def test_west_workspace_dir_still_accepts_an_unverified_ancestor_with_no_sdk_root(tmp_path):
    """`sdk_root=None` means nothing to verify a candidate's manifest
    against -- the plain upward walk's old unconditional accept stands here
    too, matching every other candidate's own "nothing to check" fallback
    (`test_west_workspace_dir_walks_up_from_start` above already covers a
    `.west` with no `config` at all; this covers one with a real but
    unrelated manifest, which is the shape tan-cli#307 actually hit)."""
    top = tmp_path / "ws"
    (top / ".west").mkdir(parents=True)
    (top / ".west" / "config").write_text("[manifest]\npath = unrelated\n", encoding="utf-8")
    nested = top / "app" / "sub"
    nested.mkdir(parents=True)
    assert _west_workspace_dir(str(nested), None) == top


# --- CLI-level forwarding, west spawn stubbed --------------------------------


@pytest.fixture
def _stub_west_missing(monkeypatch):
    """No `west` on PATH -- the bare-name fallback then fails to launch."""

    def _raise(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(west_forward_cmd.subprocess, "run", _raise)


def test_json_mode_reports_a_launch_error_envelope(_stub_west_missing, tmp_path):
    result = runner.invoke(app, ["migrate", "--project", str(tmp_path), "--format", "json"])
    assert result.exit_code == 1
    env = envelope(result)
    assert env["command"] == "migrate"
    assert env["ok"] is False
    assert env["exitCode"] == 1
    assert env["data"]["westCommand"] == "alp-migrate"
    assert env["data"]["args"] == []
    assert env["issues"][0]["code"] == "migrate.failed"
    assert "west not found on PATH" in env["issues"][0]["message"]


def test_json_mode_forwards_interspersed_unrecognised_flags_verbatim(_stub_west_missing, tmp_path):
    """NOT oracle parity for this exact argv shape -- documented divergence.

    Click recognises `--project`/`--format` wherever they appear in the token
    stream and only routes genuinely-unrecognised tokens into the `args`
    catch-all. The oracle's clap `WestForwardArgs` (`trailing_var_arg = true`)
    instead swallows EVERYTHING from the first unrecognised token onward,
    including a later, otherwise-known flag. Verified directly against the
    oracle binary: `tan lock --project . --core m55_hp --sequential -b
    some_board --format json` (this test's argv, oracle-ordered) never reaches
    JSON mode there -- `--format json` lands inside `data.args` instead,
    because it trails `--core`. Oracle parity for THIS ordering instead needs
    `--project`/`--format` placed before every forwarded flag (clap's
    `global = true` position, exercised by `test_json_mode_reports_a_launch_
    error_envelope`); this test pins the port's own (Click) catch-all
    semantics for the interspersed-`--format` shape, not a byte-identical
    invocation of the oracle.
    """
    result = runner.invoke(
        app,
        [
            "lock",
            "--project",
            str(tmp_path),
            "--core",
            "m55_hp",
            "--sequential",
            "-b",
            "some_board",
            "--format",
            "json",
        ],
    )
    env = envelope(result)
    assert env["data"]["args"] == ["--core", "m55_hp", "--sequential", "-b", "some_board"]
    assert env["data"]["westCommand"] == "alp-lock"


def test_text_mode_reports_the_failure_on_stderr_and_writes_no_stdout(
    _stub_west_missing, tmp_path
):
    result = runner.invoke(app, ["quality", "--project", str(tmp_path)])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "west not found on PATH" in result.output


def test_success_exits_zero_and_reports_ok(monkeypatch, tmp_path):
    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(west_forward_cmd.subprocess, "run", lambda *a, **k: _Ok())
    result = runner.invoke(app, ["migrate", "--project", str(tmp_path), "--format", "json"])
    assert result.exit_code == 0
    env = envelope(result)
    assert env["ok"] is True
    assert env["issues"] == []


def test_nonzero_exit_is_reported_without_crashing(monkeypatch, tmp_path):
    class _Failed:
        returncode = 3
        stdout = ""
        stderr = ""

    monkeypatch.setattr(west_forward_cmd.subprocess, "run", lambda *a, **k: _Failed())
    result = runner.invoke(app, ["lock", "--project", str(tmp_path), "--format", "json"])
    assert result.exit_code == 1
    env = envelope(result)
    assert env["ok"] is False
    assert env["issues"][0]["code"] == "lock.failed"


def test_bad_format_value_is_rejected(tmp_path):
    result = runner.invoke(app, ["quality", "--project", str(tmp_path), "--format", "xml"])
    assert result.exit_code != 0
