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
    # Not `[]`: `alp-migrate` is targeted at the project's board.yaml through
    # the `--board` flag it declares (tan-cli#391), and `data.args` reports
    # the argv actually handed to west.
    assert env["data"]["args"] == ["--board", (tmp_path / "board.yaml").as_posix()]
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
        "--board",
        (tmp_path / "board.yaml").as_posix(),
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


# --- tan-cli#391: the argv actually handed to the child ----------------------
#
# Every test above stubs `subprocess.run` and throws its argv away. That is
# precisely how a positional NO `alp-*` command accepts shipped and failed
# every real `tan migrate`/`tan lock` invocation while this file stayed green,
# so the cases below read the argv instead of discarding it, and parse it with
# each child's own argument surface.


def _alp_migrate_parser() -> argparse.ArgumentParser:
    """A local mirror of alp-sdk `scripts/west_commands/alp_migrate.py::
    _add_args` -- the mutually-exclusive mode group, the mutually-exclusive
    target group, `--no-verify`, and NO positional.

    Mirrored rather than imported from the SDK on purpose: importing needs a
    resolvable `ALP_SDK_ROOT`, and this suite runs without one, so an import
    would turn the only check of this invariant into a skip. A skip is not a
    pass -- that is the whole failure mode tan-cli#391 came from. The cost is
    that the mirror can drift from the SDK; it is pinned to the surface quoted
    in the issue, and the LITERAL argv assertions alongside it catch a drift
    in the flags tan itself chooses either way.
    """
    p = argparse.ArgumentParser(prog="west alp-migrate", add_help=False)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--apply", action="store_true")
    target = p.add_mutually_exclusive_group()
    target.add_argument("--all", action="store_true")
    target.add_argument("--board")
    p.add_argument("--no-verify", action="store_true")
    return p


def _alp_lock_parser() -> argparse.ArgumentParser:
    """Mirror of `alp_lock.py::_add_args` -- see `_alp_migrate_parser`."""
    p = argparse.ArgumentParser(prog="west alp-lock", add_help=False)
    p.add_argument("--check", action="store_true")
    p.add_argument("--workspace")
    p.add_argument("--board")
    return p


def _alp_quality_parser() -> argparse.ArgumentParser:
    """Mirror of `alp_quality.py`'s parser -- see `_alp_migrate_parser`. Note
    it declares no `--board` at all, which is why `quality` must never be
    handed one."""
    p = argparse.ArgumentParser(prog="west alp-quality", add_help=False)
    p.add_argument("--profile", required=True, choices=["quick", "pr", "full", "release"])
    p.add_argument("--json")
    p.add_argument("--junit")
    p.add_argument("--sarif")
    return p


_SDK_PARSERS = {
    "migrate": _alp_migrate_parser,
    "lock": _alp_lock_parser,
    "quality": _alp_quality_parser,
}


@pytest.fixture
def _record_west(monkeypatch):
    """Stub `subprocess.run` but KEEP the argv it was handed."""
    calls: list[list[str]] = []

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(argv, **kwargs):
        calls.append(list(argv))
        return _Ok()

    monkeypatch.setattr(west_forward_cmd.subprocess, "run", _run)
    return calls


@pytest.mark.parametrize(
    "verb,forwarded,expected_tail",
    [
        # The two verbs whose ENTIRE argument surface is flags -- the ones the
        # old injection broke on every invocation. `--board <project>/
        # board.yaml` because the child runs with cwd set to the west topdir,
        # where `alp_migrate.py`'s no-flag fallback (`Path("board.yaml")
        # .resolve()`) would find the WORKSPACE's board.yaml instead.
        ("migrate", ["--check"], ["--board", "<BOARD>", "--check"]),
        ("lock", ["--check"], ["--board", "<BOARD>", "--check"]),
        # `alp-quality` declares no `--board`; it must be handed exactly what
        # the caller typed. Both `--profile` spellings, because the `=` form
        # is what the old dash-token guard mis-read (the space form survived
        # by accident -- `quick` is a non-dash token).
        ("quality", ["--profile=quick"], ["--profile=quick"]),
        ("quality", ["--profile", "quick"], ["--profile", "quick"]),
        # The caller already chose a target: `--all` and `--board` sit in ONE
        # mutually-exclusive group on `alp-migrate`, so injecting on top of
        # either is a usage error, not a refinement.
        ("migrate", ["--check", "--all"], ["--check", "--all"]),
        ("migrate", ["--check", "--board", "other.yaml"], ["--check", "--board", "other.yaml"]),
        ("migrate", ["--check", "--board=other.yaml"], ["--check", "--board=other.yaml"]),
    ],
)
def test_forwarded_argv_is_accepted_by_the_child_argument_surface(
    verb, forwarded, expected_tail, _record_west, monkeypatch, tmp_path
):
    # A REAL west workspace. Not decoration: the old injection was gated on
    # `workspace is not None`, so without a `.west/` here every all-dash case
    # below would pass vacuously against the very code it exists to catch.
    # `$ZEPHYR_BASE` deleted for the same reason `test_west_workspace_dir_is_
    # none_when_nothing_resolves` deletes it -- a Zephyr dev shell would
    # otherwise resolve a workspace out from under this.
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    (tmp_path / ".west").mkdir()
    board = (tmp_path / "board.yaml").as_posix()
    expected = [t.replace("<BOARD>", board) for t in expected_tail]

    result = runner.invoke(app, [verb, "--project", str(tmp_path), "--format", "json", *forwarded])
    assert result.exit_code == 0, result.output

    argv = _record_west[0]
    # argv[0] is the resolved `west` binary; argv[1] the extension command.
    assert argv[1] == f"alp-{verb}"
    assert argv[2:] == expected

    # And the child's own parser accepts it -- no positional, no flag it never
    # declared. `parse_args` (not `parse_known_args`): an unexpected token must
    # raise, which is exactly what the old injection did in production.
    _SDK_PARSERS[verb]().parse_args(argv[2:])

    # The envelope names the argv that actually ran, injection included --
    # otherwise it cannot explain a failure that argv caused.
    assert envelope(result)["data"]["args"] == argv[2:]


# --- tan-cli#395: the child's output and real exit code ----------------------


def test_child_stdout_stderr_and_exit_code_reach_the_envelope(monkeypatch, tmp_path):
    """The stub returns NON-EMPTY streams and a non-1 code on purpose: the
    pre-existing stubs returned `stdout = ""`/`stderr = ""`, so no test in
    this file could observe that both were captured and discarded."""

    class _Failed:
        returncode = 7
        stdout = "alp-lock: resolved manifest would be written here\n"
        stderr = "alp-lock: dependency zephyr-fs conflicts with alp-fs\n"

    monkeypatch.setattr(west_forward_cmd.subprocess, "run", lambda *a, **k: _Failed())
    result = runner.invoke(app, ["lock", "--project", str(tmp_path), "--format", "json"])
    env = envelope(result)

    assert env["data"]["stdout"] == "alp-lock: resolved manifest would be written here\n"
    assert env["data"]["stderr"] == "alp-lock: dependency zephyr-fs conflicts with alp-fs\n"
    assert env["data"]["westExitCode"] == 7
    # tan's OWN code stays RUNTIME_FAILURE: `ExitCode` is a fixed vocabulary
    # (2 validation / 3 write / 4 doctor / 5 internal) and relaying a child's
    # code into it would announce a tan-level failure that never happened.
    assert env["exitCode"] == 1
    assert result.exit_code == 1
    # The diagnosis is the child's, not the old "re-run without --format json"
    # sentence pointing a JSON caller at a mode it does not have.
    assert env["issues"][0]["message"] == (
        "`west alp-lock` failed (exit 7): "
        "alp-lock: dependency zephyr-fs conflicts with alp-fs"
    )


def test_a_successful_child_reports_its_product(monkeypatch, tmp_path):
    """Success is where the report IS -- `alp-quality`'s whole purpose. An
    `ok:true, issues:[]` envelope with the report missing is the #395 symptom
    a failure-only assertion would not catch."""

    class _Ok:
        returncode = 0
        stdout = "alp-quality profile=quick: 0/0 passed\n"
        stderr = ""

    monkeypatch.setattr(west_forward_cmd.subprocess, "run", lambda *a, **k: _Ok())
    result = runner.invoke(
        app, ["quality", "--project", str(tmp_path), "--format", "json", "--profile", "quick"]
    )
    env = envelope(result)
    assert result.exit_code == 0
    assert env["data"]["stdout"] == "alp-quality profile=quick: 0/0 passed\n"
    assert env["data"]["stderr"] == ""
    assert env["data"]["westExitCode"] == 0


def test_a_child_that_fails_silently_keeps_a_usable_message(monkeypatch, tmp_path):
    class _Silent:
        returncode = 4
        stdout = "something on stdout\n"
        stderr = "   \n"

    monkeypatch.setattr(west_forward_cmd.subprocess, "run", lambda *a, **k: _Silent())
    result = runner.invoke(app, ["migrate", "--project", str(tmp_path), "--format", "json"])
    env = envelope(result)
    assert env["data"]["westExitCode"] == 4
    assert env["issues"][0]["message"] == (
        "`west alp-migrate` failed (exit 4) and wrote nothing to stderr; see `data.stdout`."
    )


def test_a_launch_failure_reports_no_child_streams_rather_than_empty_ones(
    _stub_west_missing, tmp_path
):
    """`null`, not `""`/`0`: no child ran at all, which is a different fact
    from one that ran and produced nothing. The keys are still present so a
    consumer can read them unconditionally."""
    result = runner.invoke(app, ["lock", "--project", str(tmp_path), "--format", "json"])
    env = envelope(result)
    assert env["data"]["stdout"] is None
    assert env["data"]["stderr"] is None
    assert env["data"]["westExitCode"] is None


def test_text_mode_failure_line_names_the_child_exit_code(monkeypatch, tmp_path):
    """Text mode has no envelope to carry `westExitCode`, so the line does --
    otherwise a `7` from `alp-lock` is indistinguishable from west's own
    unknown-command usage error on the only channel text mode has."""

    class _Failed:
        returncode = 7

    monkeypatch.setattr(west_forward_cmd.subprocess, "run", lambda *a, **k: _Failed())
    result = runner.invoke(app, ["lock", "--project", str(tmp_path)])
    assert result.exit_code == 1
    assert "lock: `west alp-lock` failed (exit 7; see log above)." in result.output
