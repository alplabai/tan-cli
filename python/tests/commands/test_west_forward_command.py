# SPDX-License-Identifier: Apache-2.0
"""`tan migrate` / `tan lock` / `tan quality` -- the shared `west`-forwarding
plumbing (`west_forward_cmd.py`), ported from
`crates/tan-cli/src/commands/build/workspace.rs`. No real `west` is spawned:
every test monkeypatches `west_forward_cmd.subprocess.run` to a stub, mirroring
`test_monitor_command.py`'s guard-not-assumed discipline.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from tan.cli import _reorder_global_flags
from tan.commands import west_forward_cmd
from tan.commands.west_forward_cmd import (
    FORWARD_CONTEXT_SETTINGS,
    _west_argv,
    _west_workspace_dir,
    lock,
    migrate,
    quality,
)
from tan.core.global_flags import GLOBAL_FLAGS

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
    # `--check`: tan-cli#454 made `alp-migrate`'s mode REQUIRED on tan's own
    # surface too, so a bare `migrate` no longer reaches `west` at all (see
    # `test_missing_mode_refuses_before_west_is_ever_spawned` below) -- this
    # test is about the LAUNCH failure, a different concern, and needs a real
    # mode supplied to reach it.
    result = runner.invoke(
        app, ["migrate", "--project", str(tmp_path), "--check", "--format", "json"]
    )
    assert result.exit_code == 1
    env = envelope(result)
    assert env["command"] == "migrate"
    assert env["ok"] is False
    assert env["exitCode"] == 1
    assert env["data"]["westCommand"] == "alp-migrate"
    # tan-cli#391: `alp-migrate` is targeted by the `--board` flag it really
    # declares, and `data.args` reports the argv as handed to the child rather
    # than only the tokens the caller typed -- so a caller with no args of their
    # own still sees exactly what tan asked `west` to do.
    assert env["data"]["args"] == [
        "--board",
        str(tmp_path / "board.yaml").replace("\\", "/"),
        "--check",
    ]
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
    assert env["data"]["args"] == [
        # tan's own targeting, ahead of the caller's tokens so a caller's later
        # `--board` is the one argparse keeps (tan-cli#391).
        "--board",
        str(tmp_path / "board.yaml").replace("\\", "/"),
        "--core",
        "m55_hp",
        "--sequential",
        "-b",
        "some_board",
    ]
    assert env["data"]["westCommand"] == "alp-lock"


def test_text_mode_reports_the_failure_on_stderr_and_writes_no_stdout(
    _stub_west_missing, tmp_path
):
    # `--profile quick`: tan-cli#454 made this REQUIRED on tan's own surface --
    # see `test_missing_profile_refuses_before_west_is_ever_spawned` below.
    result = runner.invoke(app, ["quality", "--project", str(tmp_path), "--profile", "quick"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "west not found on PATH" in result.output


def test_success_exits_zero_and_reports_ok(monkeypatch, tmp_path):
    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(west_forward_cmd.subprocess, "run", lambda *a, **k: _Ok())
    result = runner.invoke(
        app, ["migrate", "--project", str(tmp_path), "--check", "--format", "json"]
    )
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


# --- the argv, measured against the child's REAL surface (tan-cli#391) -------
#
# Every test above this line stubs `subprocess.run` and never looks at the argv
# it was handed, which is exactly how tan-cli#391 shipped: `_run_forward` used
# to insert the resolved app path as `argv[1]` whenever every forwarded token
# started with a dash, and NONE of `alp-lock` / `alp-migrate` / `alp-quality`
# declares a positional. `west alp-build` did, and it was retired by ADR-0020
# Phase 4, so the injection outlived its only valid target and every realistic
# `tan migrate` / `tan lock` invocation died on
# `west alp-migrate: error: unexpected arguments: ['<project>']`.
#
# The replicas below are the three children's argparse surfaces as recorded in
# tan-cli#391 (from the SDK's `scripts/west_commands/`), reproduced here so the
# argv this port builds is checked against a real parser rather than against a
# stub that accepts anything. They are deliberately a COPY: alp-sdk is not a
# test dependency of tan-cli, and a copy that drifts is still infinitely more
# than the nothing these tests asserted before.


def _alp_lock_parser() -> argparse.ArgumentParser:
    """`scripts/west_commands/alp_lock.py`: flags only, no positional."""
    parser = argparse.ArgumentParser(prog="west alp-lock")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--workspace")
    parser.add_argument("--board")
    return parser


def _alp_migrate_parser() -> argparse.ArgumentParser:
    """`scripts/west_commands/alp_migrate.py`. The two groups are the usage
    line tan-cli#391 measured verbatim: `(--check | --preview | --apply)` is
    required, and `[--all | --board BOARD]` is MUTUALLY EXCLUSIVE -- which is
    why `_target_args` must not add its `--board` behind a caller's `--all`."""
    parser = argparse.ArgumentParser(prog="west alp-migrate")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--apply", action="store_true")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--all", action="store_true")
    target.add_argument("--board")
    parser.add_argument("--no-verify", action="store_true")
    return parser


def _alp_quality_parser() -> argparse.ArgumentParser:
    """`scripts/west_commands/alp_quality.py`: no `--board`, no positional --
    which is why `quality` is targeted by cwd alone."""
    parser = argparse.ArgumentParser(prog="west alp-quality")
    parser.add_argument("--profile", required=True, choices=["quick", "pr", "full", "release"])
    parser.add_argument("--json")
    parser.add_argument("--junit")
    parser.add_argument("--sarif")
    return parser


_CHILD_PARSERS = {
    "lock": _alp_lock_parser,
    "migrate": _alp_migrate_parser,
    "quality": _alp_quality_parser,
}

#: The child args each verb needs to reach its own `do_run` -- every one of
#: them a flag, which is precisely why tan-cli#391's "no non-dash token"
#: guard fired on every realistic `migrate`/`lock` invocation.
_CHILD_ARGS = {
    "lock": ["--check"],
    "migrate": ["--check"],
    "quality": ["--profile", "quick"],
}


def _child_accepts(verb: str, child_argv: list[str]) -> argparse.Namespace:
    """Parse `child_argv` (the `alp-<verb>` argv MINUS the `alp-<verb>` token)
    with the child's own parser and assert nothing is left over -- the SDK's
    own `do_run` rejects leftovers with `unexpected arguments: [...]`, which is
    the exact string tan-cli#391 measured."""
    namespace, extras = _CHILD_PARSERS[verb]().parse_known_args(child_argv)
    assert extras == [], f"west alp-{verb}: error: unexpected arguments: {extras}"
    return namespace


@pytest.fixture
def workspace_project(monkeypatch, tmp_path):
    """A project INSIDE a real west workspace -- the shape tan-cli#391
    measured. `_west_workspace_dir` resolving non-`None` is what used to arm
    the positional injection, so anything less than this cannot reproduce it.
    `$ZEPHYR_BASE` is dropped for the same reason
    `test_west_workspace_dir_is_none_when_nothing_resolves` drops it: a Zephyr
    dev shell would otherwise resolve a workspace out from under the test."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    (tmp_path / ".west").mkdir()
    project = tmp_path / "app"
    project.mkdir()
    (project / "board.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    return project


@pytest.fixture
def west_argv(monkeypatch):
    """Record the argv `_run_forward` actually hands to `west`, so a test can
    assert on it. `recorded["child"]` drops both the `west` binary and the
    `alp-<verb>` token, leaving exactly what the child's argparse sees."""
    recorded: dict[str, object] = {}

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    def _record(argv, **kwargs):
        recorded["full"] = list(argv)
        recorded["west"] = list(argv[1:])
        recorded["child"] = list(argv[2:])
        recorded["cwd"] = kwargs.get("cwd")
        return _Ok()

    monkeypatch.setattr(west_forward_cmd.subprocess, "run", _record)
    return recorded


def _board_yaml_of(project: Path) -> str:
    return str(project / "board.yaml").replace("\\", "/")


@pytest.mark.parametrize("verb", ["migrate", "lock"])
def test_board_targeted_verbs_pass_board_yaml_by_flag_and_no_positional(
    verb, west_argv, workspace_project
):
    """tan-cli#391: deleting the injection alone is not enough. With cwd set to
    the west TOPDIR (which is what `westCwd` has always been), `alp_migrate.py`'s
    own fallback resolves `Path("board.yaml")` against the workspace root, not
    against the project -- so the project has to be named through the `--board`
    flag both commands really declare."""
    result = runner.invoke(app, [verb, "--project", str(workspace_project), "--check"])
    assert result.exit_code == 0, result.output
    assert west_argv["west"] == [
        f"alp-{verb}",
        "--board",
        _board_yaml_of(workspace_project),
        "--check",
    ]
    assert _child_accepts(verb, west_argv["child"]).board == _board_yaml_of(workspace_project)


def test_quality_gets_no_positional_and_no_board_flag(west_argv, workspace_project):
    """`alp_quality.py` declares no `--board`/positional, so tan adds neither
    (tan-cli#391) -- `--profile` (tan-cli#454) is the one flag it DOES add,
    since the child declares that one REQUIRED."""
    result = runner.invoke(
        app, ["quality", "--project", str(workspace_project), "--profile", "quick"]
    )
    assert result.exit_code == 0, result.output
    assert west_argv["west"] == ["alp-quality", "--profile", "quick"]
    assert _child_accepts("quality", west_argv["child"]).profile == "quick"


def test_quality_equals_form_profile_still_reaches_the_child(west_argv, workspace_project):
    """`--profile=quick` and `--profile quick` are the same option to Click
    once `--profile` is DECLARED (tan-cli#454) -- both spellings bind the same
    parameter, and `_run_forward` always forwards it in the normalised
    two-token form (matching how `--all`/`--board` are already forwarded
    elsewhere in this file), not whichever spelling the caller happened to
    type. Before tan-cli#454 declared `--profile` for real, this was the shape
    that broke while `--profile quick` survived by accident -- see tan-cli#391
    (superseded here, now that both spellings are handled identically by
    construction rather than by an accident of which forwarded token started
    with a dash)."""
    result = runner.invoke(
        app, ["quality", "--project", str(workspace_project), "--profile=quick"]
    )
    assert result.exit_code == 0, result.output
    assert west_argv["west"] == ["alp-quality", "--profile", "quick"]
    assert _child_accepts("quality", west_argv["child"]).profile == "quick"


def test_a_caller_supplied_board_is_not_doubled(west_argv, workspace_project):
    """tan only names the project when the caller did not; `--board=<path>` is
    the same flag to argparse, so both spellings suppress the injection."""
    result = runner.invoke(
        app, ["lock", "--project", str(workspace_project), "--board=/elsewhere/board.yaml"]
    )
    assert result.exit_code == 0, result.output
    assert west_argv["west"] == ["alp-lock", "--board=/elsewhere/board.yaml"]


def test_migrate_all_suppresses_the_injected_board(west_argv, workspace_project):
    """`alp-migrate`'s `--all` and `--board` are mutually exclusive (see the
    usage line in `_alp_migrate_parser`), so injecting `--board` behind a
    caller's `--all` would trade tan-cli#391's usage error for a different
    one."""
    result = runner.invoke(app, ["migrate", "--project", str(workspace_project), "--all", "--check"])
    assert result.exit_code == 0, result.output
    assert west_argv["west"] == ["alp-migrate", "--check", "--all"]
    namespace = _child_accepts("migrate", west_argv["child"])
    assert namespace.all is True
    assert namespace.board is None


def test_data_args_reports_the_argv_handed_to_west(west_argv, workspace_project):
    """tan-cli#391: `data.args` used to echo the caller's tokens only, so the
    envelope never named the injected argument the child died on. It now
    carries every argument after the `alp-<verb>` token, which is what
    `data.westCommand` already names."""
    result = runner.invoke(
        app, ["migrate", "--project", str(workspace_project), "--check", "--format", "json"]
    )
    env = envelope(result)
    assert env["data"]["westCommand"] == "alp-migrate"
    assert env["data"]["args"] == ["--board", _board_yaml_of(workspace_project), "--check"]


# --- tan's own globals are consumed, never forwarded (tan-cli#405) -----------
#
# `lock`/`migrate`/`quality` are the only three commands registered with
# `ignore_unknown_options`, and were the only three that never called
# `accept_global_flags`. Seven of the ten flags in `_GLOBAL_FLAG_SPECS` were
# therefore unknown here, and `ignore_unknown_options` means Click does not
# reject an unknown flag -- it drops it into the `ARGS...` catch-all, from
# which it was forwarded verbatim into a child that declares none of them.
# `tests/gates/test_global_flags_gate.py` cannot see this: it probes acceptance
# by asserting Click printed no `no such option` marker, and
# `ignore_unknown_options` guarantees that marker never appears.

#: `--project` / `--board-yaml` / `--sdk-root` were always declared here for
#: real, and `--format` is not a global at all (see `global_flags.py`'s own
#: note). These seven -- six arity-0 flags plus the value-carrying `--target`
#: -- are what leaked into the child and are now consumed and dropped.
#:
#: `--target` sat in a one-off `_REFUSED_GLOBAL` here until tan-cli#403's
#: postmortem: `accept_global_flags` used to REFUSE an injected value-carrying
#: flag outright (`typer.BadParameter`, exit 2, tan-cli#398's fix), but that
#: was measured wrong against the oracle -- `target/debug/tan.exe` accepts and
#: IGNORES `--target` on a command that never declares it (byte-identical
#: `tan doctor --target zephyr --format json` output with and without the
#: flag), so the refusal was a NEW divergence, not a fix. `global_flags.py`
#: now drops a value-carrying global exactly like a boolean one; folded in
#: here so the two generic tests below cover it for free instead of needing a
#: dedicated refusal test.
_DROPPED_GLOBALS = (
    "--target",
    "--all",
    "--verbose",
    "--quiet",
    "--no-color",
    "--non-interactive",
    "--ci",
)

#: The one flag in `_DROPPED_GLOBALS` that carries a value -- `_global_tokens`
#: appends a throwaway one so the generic tests below exercise BOTH the flag
#: and its value being dropped (tan-cli#405 measured the VALUE, not just the
#: flag, leaking into the child argv as a caller-supplied positional).
_VALUE_CARRYING_GLOBAL = "--target"


def _global_tokens(flag: str) -> list[str]:
    return [flag, "cm33"] if flag == _VALUE_CARRYING_GLOBAL else [flag]


def test_the_covered_globals_match_the_shared_spec():
    """An eighth-flag-added-later guard: if `_GLOBAL_FLAG_SPECS` grows, this
    module must grow with it rather than silently stop covering the new flag."""
    assert set(GLOBAL_FLAGS) == {
        "--project",
        "--board-yaml",
        "--sdk-root",
        *_DROPPED_GLOBALS,
    }


@pytest.mark.parametrize("verb", ["migrate", "lock", "quality"])
@pytest.mark.parametrize("flag", _DROPPED_GLOBALS)
def test_a_trailing_global_flag_is_consumed_not_forwarded(
    verb, flag, west_argv, workspace_project
):
    """`tan quality --ci --profile quick`. `--all` on `migrate` is the one
    exception -- there it is the CHILD's own flag, covered by
    `test_migrate_forwards_its_own_all_after_consuming_it` below."""
    if (verb, flag) == ("migrate", "--all"):
        pytest.skip("`--all` is `alp-migrate`'s own flag; see the dedicated test")
    result = runner.invoke(
        app,
        [verb, "--project", str(workspace_project), *_global_tokens(flag), *_CHILD_ARGS[verb]],
    )
    assert result.exit_code == 0, result.output
    for token in _global_tokens(flag):
        assert token not in west_argv["west"]
    _child_accepts(verb, west_argv["child"])


@pytest.mark.parametrize("verb", ["migrate", "lock", "quality"])
@pytest.mark.parametrize("flag", _DROPPED_GLOBALS)
def test_a_leading_global_flag_is_consumed_not_forwarded(
    verb, flag, west_argv, workspace_project
):
    """`tan --ci quality --profile quick`. `cli.py::_reorder_global_flags` is
    what moves a leading global across the subcommand boundary before Click
    ever sees the argv -- run here explicitly, because this module registers
    its own bare `typer.Typer` rather than `tan.cli.app` and would otherwise
    never exercise the extension's own calling convention."""
    if (verb, flag) == ("migrate", "--all"):
        pytest.skip("`--all` is `alp-migrate`'s own flag; see the dedicated test")
    argv = [*_global_tokens(flag), verb, "--project", str(workspace_project), *_CHILD_ARGS[verb]]
    result = runner.invoke(app, _reorder_global_flags(argv))
    assert result.exit_code == 0, result.output
    for token in _global_tokens(flag):
        assert token not in west_argv["west"]
    _child_accepts(verb, west_argv["child"])


def test_migrate_forwards_its_own_all_after_consuming_it(west_argv, workspace_project):
    """tan-cli#405's collision: `--all` is BOTH a tan global and a real
    `alp-migrate` flag. A bare `accept_global_flags(migrate)` would consume and
    drop it -- which is what the v0.4.1 oracle does, measured in the issue --
    so `migrate` declares `--all` for real and hands it straight back to the
    child."""
    result = runner.invoke(app, ["migrate", "--project", str(workspace_project), "--check", "--all"])
    assert result.exit_code == 0, result.output
    assert "--all" in west_argv["west"]
    assert _child_accepts("migrate", west_argv["child"]).all is True


def test_lock_does_not_forward_all_because_alp_lock_has_no_such_flag(
    west_argv, workspace_project
):
    """The collision is `migrate`-only: `alp_lock.py` declares `--check`,
    `--workspace`, `--board` and nothing else, so `--all` there is tan's global
    and is dropped."""
    result = runner.invoke(app, ["lock", "--project", str(workspace_project), "--all", "--check"])
    assert result.exit_code == 0, result.output
    assert west_argv["west"] == ["alp-lock", "--board", _board_yaml_of(workspace_project), "--check"]


def test_target_global_does_not_change_the_argv_shape(west_argv, workspace_project):
    """tan-cli#405 measured `--target cm33` leaking its VALUE as a positional,
    which then read as "the caller supplied their own positional" and silently
    changed the argv shape. Whatever shape `quality` has without the flag it
    must have with it."""
    runner.invoke(app, ["quality", "--project", str(workspace_project), "--profile", "quick"])
    without = list(west_argv["west"])
    runner.invoke(
        app,
        ["quality", "--project", str(workspace_project), "--target", "cm33", "--profile", "quick"],
    )
    assert west_argv["west"] == without


# --- a required child flag with no sensible default (tan-cli#454) -----------
#
# `alp_quality.py` declares `--profile {quick,pr,full,release}` REQUIRED;
# `alp_migrate.py` declares a REQUIRED mutually-exclusive `(--check | --preview
# | --apply)` group. Neither was ever declared on tan's own Typer surface
# before this fix, so `tan quality`/`tan migrate` reached `west` regardless and
# died on the CHILD's own argparse usage error -- on a correctly bootstrapped
# workspace, where every other west-backed command works, and with no flag
# combination that avoided it (nothing in tan's own `--help` even named the
# flag to supply). PRE-FIX, every test below fails: `_never_spawn_west`'s
# `AssertionError` fires (the old code forwarded straight to `west` with an
# incomplete argv), and the exit code observed on the unfixed tree is `1`
# (`<verb>.failed`, from the child's own exit 2 folded into tan's
# `RUNTIME_FAILURE`), never the `quality.profile-required` /
# `migrate.mode-required` this fix adds.


@pytest.fixture
def _never_spawn_west(monkeypatch):
    """The fix's whole point: neither refusal may spawn a child at all. A stub
    that raises tells the two apart from a stub that just returns a stale
    fixed exit code, which a `quality.failed`/`migrate.failed` fallback could
    still coincidentally satisfy."""

    def _forbidden(argv, **kwargs):
        raise AssertionError(f"west must not be spawned for a missing required flag: {argv}")

    monkeypatch.setattr(west_forward_cmd.subprocess, "run", _forbidden)


def test_missing_profile_refuses_before_west_is_ever_spawned(_never_spawn_west, tmp_path):
    result = runner.invoke(app, ["quality", "--project", str(tmp_path), "--format", "json"])
    assert result.exit_code == 2
    env = envelope(result)
    assert env["command"] == "quality"
    assert env["ok"] is False
    assert env["exitCode"] == 2
    assert env["issues"] == [
        {
            "code": "quality.profile-required",
            "severity": "error",
            "message": "`--profile` is required (`west alp-quality --profile "
            "{quick,pr,full,release}`).",
        }
    ]


def test_missing_profile_refuses_in_text_mode_too(_never_spawn_west, tmp_path):
    result = runner.invoke(app, ["quality", "--project", str(tmp_path)])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "`--profile` is required" in result.output


def test_missing_migrate_mode_refuses_before_west_is_ever_spawned(_never_spawn_west, tmp_path):
    result = runner.invoke(app, ["migrate", "--project", str(tmp_path), "--format", "json"])
    assert result.exit_code == 2
    env = envelope(result)
    assert env["command"] == "migrate"
    assert env["ok"] is False
    assert env["exitCode"] == 2
    assert env["issues"] == [
        {
            "code": "migrate.mode-required",
            "severity": "error",
            "message": "one of `--check`, `--preview`, `--apply` is required "
            "(`west alp-migrate`).",
        }
    ]


def test_missing_migrate_mode_refuses_in_text_mode_too(_never_spawn_west, tmp_path):
    result = runner.invoke(app, ["migrate", "--project", str(tmp_path)])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "one of `--check`, `--preview`, `--apply` is required" in result.output


def test_escape_hatch_profile_is_not_falsely_refused(west_argv, workspace_project):
    """A caller who spells `--profile` themselves past tan's own declared
    option, through the documented `ARGS...` escape hatch
    (`tan quality -- --profile quick`), already satisfies `alp_quality.py`'s
    requirement -- pre-fix, `_resolve_quality_profile` only ever looked at its
    OWN `--profile` parameter (bound by Typer, unset here) and refused a flag
    that was already present, one token later, in `west_args`."""
    result = runner.invoke(
        app, ["quality", "--project", str(workspace_project), "--", "--profile", "quick"]
    )
    assert result.exit_code == 0, result.output
    assert west_argv["west"] == ["alp-quality", "--profile", "quick"]


def test_escape_hatch_migrate_mode_is_not_falsely_refused(west_argv, workspace_project):
    """Same false refusal, `migrate`'s side: `tan migrate -- --check`."""
    result = runner.invoke(app, ["migrate", "--project", str(workspace_project), "--", "--check"])
    assert result.exit_code == 0, result.output
    assert west_argv["west"] == [
        "alp-migrate",
        "--board",
        _board_yaml_of(workspace_project),
        "--check",
    ]


def test_migrate_two_modes_refuses_before_west_is_ever_spawned(_never_spawn_west, tmp_path):
    """`tan migrate --check --preview` used to build the contradictory argv
    itself, spawn `west`, and report the opaque `migrate.failed` the
    REQUIRED-group refusal exists to remove -- for the mutual-exclusion
    violation, not merely a missing mode."""
    result = runner.invoke(
        app, ["migrate", "--project", str(tmp_path), "--check", "--preview", "--format", "json"]
    )
    assert result.exit_code == 2
    env = envelope(result)
    assert env["command"] == "migrate"
    assert env["ok"] is False
    assert env["exitCode"] == 2
    assert env["issues"] == [
        {
            "code": "migrate.mode-required",
            "severity": "error",
            "message": "only one of `--check`, `--preview`, `--apply` may be given "
            "(`west alp-migrate`).",
        }
    ]


def test_migrate_two_modes_refuses_in_text_mode_too(_never_spawn_west, tmp_path):
    result = runner.invoke(app, ["migrate", "--project", str(tmp_path), "--check", "--preview"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "only one of `--check`, `--preview`, `--apply` may be given" in result.output


def test_an_out_of_choice_profile_value_is_rejected_without_spawning_west(
    _never_spawn_west, tmp_path
):
    """Absence (tan-cli#454's own bug) and an invalid VALUE are different
    failures: the choice list is still enforced, just at tan's own CLI-parse
    layer (`typer.BadParameter`, mirroring `new_som_cmd.py`'s established
    pattern for a choice-shaped flag) rather than round-tripped through a
    spawned `west` to find out."""
    result = runner.invoke(
        app, ["quality", "--project", str(tmp_path), "--profile", "bogus", "--format", "json"]
    )
    assert result.exit_code == 2
    assert "not one of" in result.output


def test_quality_help_names_the_required_profile_flag():
    """The other half of tan-cli#454: `--profile` was undiscoverable from
    `--help`, not only unenforced."""
    result = runner.invoke(app, ["quality", "--help"])
    assert result.exit_code == 0
    assert "--profile" in result.output


def test_migrate_help_names_the_required_mode_flags():
    result = runner.invoke(app, ["migrate", "--help"])
    assert result.exit_code == 0
    for flag in ("--check", "--preview", "--apply"):
        assert flag in result.output


# --- the child's output and exit code reach the envelope (tan-cli#395) -------


class _Child:
    """A `subprocess.run` result with output that is NOT empty -- the stubs
    that shipped set `stdout = ""` and `stderr = ""` on both branches, which is
    exactly why a total discard of both streams went unnoticed."""

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_child(monkeypatch, child: _Child) -> None:
    monkeypatch.setattr(west_forward_cmd.subprocess, "run", lambda *a, **k: child)


def test_child_stdout_stderr_and_exit_code_reach_the_envelope(monkeypatch, tmp_path):
    """tan-cli#395: JSON mode already spawned the child with
    `capture_output=True` and then read neither stream, and flattened every
    non-zero code to 1. A stub exiting 7 with real diagnostics on both streams
    is the case the old stubs could not express."""
    _stub_child(
        monkeypatch,
        _Child(
            7,
            "alp-lock: resolved manifest would be written here\n",
            "alp-lock: dependency zephyr-fs conflicts with alp-fs\n",
        ),
    )
    result = runner.invoke(app, ["lock", "--project", str(tmp_path), "--format", "json"])
    env = envelope(result)
    assert env["data"]["westExitCode"] == 7
    assert env["data"]["stdout"] == "alp-lock: resolved manifest would be written here\n"
    assert env["data"]["stderr"] == "alp-lock: dependency zephyr-fs conflicts with alp-fs\n"
    assert env["issues"][0]["code"] == "lock.failed"
    assert "alp-lock: dependency zephyr-fs conflicts with alp-fs" in env["issues"][0]["message"]


def test_envelope_exit_code_stays_one_while_the_child_code_is_preserved(monkeypatch, tmp_path):
    """The decision tan-cli#395 asked to be stated: the envelope's own
    `exitCode` does NOT propagate. `tan/exit_codes.py` fixes tan's vocabulary
    (0/1/2/3/4/5), so a child's `2` would arrive indistinguishable from tan's
    own VALIDATION_FAILURE. The child's real code lives in `data.westExitCode`,
    which is what makes `7` distinguishable from west's own usage error."""
    _stub_child(monkeypatch, _Child(2, "", "west alp-lock: error: unrecognized arguments: --bogus\n"))
    result = runner.invoke(app, ["lock", "--project", str(tmp_path), "--format", "json"])
    assert result.exit_code == 1
    env = envelope(result)
    assert env["exitCode"] == 1
    assert env["ok"] is False
    assert env["data"]["westExitCode"] == 2


def test_failure_message_falls_back_when_the_child_says_nothing(monkeypatch, tmp_path):
    """A child that fails silently still has to produce a message a JSON-only
    caller can act on."""
    _stub_child(monkeypatch, _Child(3, "", ""))
    result = runner.invoke(
        app, ["migrate", "--project", str(tmp_path), "--check", "--format", "json"]
    )
    env = envelope(result)
    assert env["issues"][0]["message"] == (
        "`west alp-migrate` failed with exit code 3; the child wrote nothing to "
        "stderr -- re-run without --format json to see the log."
    )


def test_a_very_long_stderr_line_is_truncated_in_the_message_but_not_in_data(
    monkeypatch, tmp_path
):
    """`data.stderr` is the payload and stays verbatim; `issues[].message` is a
    one-line summary and is capped, so a child that dumps a megabyte cannot
    turn one issue into an unreadable envelope."""
    line = "x" * 4096
    _stub_child(monkeypatch, _Child(1, "", line))
    result = runner.invoke(
        app, ["quality", "--project", str(tmp_path), "--profile", "quick", "--format", "json"]
    )
    env = envelope(result)
    assert env["data"]["stderr"] == line
    assert env["issues"][0]["message"].endswith("...")
    assert len(env["issues"][0]["message"]) < 600


def test_success_branch_also_carries_the_child_output(monkeypatch, tmp_path):
    """`alp-quality` exists to produce a report; on the success branch that
    report was the one thing the envelope did not contain (tan-cli#395)."""
    _stub_child(monkeypatch, _Child(0, "alp-quality profile=quick: 0/0 passed\n", ""))
    result = runner.invoke(
        app, ["quality", "--project", str(tmp_path), "--profile", "quick", "--format", "json"]
    )
    assert result.exit_code == 0
    env = envelope(result)
    assert env["ok"] is True
    assert env["issues"] == []
    assert env["data"]["westExitCode"] == 0
    assert env["data"]["stdout"] == "alp-quality profile=quick: 0/0 passed\n"
    assert env["data"]["stderr"] == ""


def test_launch_error_reports_a_null_west_exit_code(_stub_west_missing, tmp_path):
    """No child ever ran, so there is no code to report -- `null`, not a
    fabricated 1, and the keys stay present so a consumer sees one shape."""
    result = runner.invoke(
        app, ["migrate", "--project", str(tmp_path), "--check", "--format", "json"]
    )
    env = envelope(result)
    assert env["data"]["westExitCode"] is None
    assert env["data"]["stdout"] == ""
    assert env["data"]["stderr"] == ""
    assert "west not found on PATH" in env["issues"][0]["message"]


def test_text_mode_names_the_child_exit_code(monkeypatch, tmp_path):
    """Text mode collapsed 7 to 1 too, and said only "see log above". The log
    is right there, but the code the child chose is worth naming."""
    _stub_child(monkeypatch, _Child(7, "", ""))
    result = runner.invoke(app, ["lock", "--project", str(tmp_path)])
    assert result.exit_code == 1
    assert "lock: `west alp-lock` failed with exit code 7 (see log above)." in result.output
