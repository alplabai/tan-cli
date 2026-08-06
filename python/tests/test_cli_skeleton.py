# SPDX-License-Identifier: Apache-2.0
import json, os, subprocess, sys
from pathlib import Path

import pytest
from typer.main import get_command

from tan.cli import app
from tests.parity.oracle import empty_tool_inventory

#: `python/` -- `python -m tan` resolves the package off `os.getcwd()` (`-m`
#: prepends the CURRENT WORKING DIRECTORY to `sys.path`, not the script's own
#: location), so a case that passes a scratch `cwd` needs this pinned onto the
#: child's `PYTHONPATH` or it cannot find the package at all.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def run(*argv, cwd=None):
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    return subprocess.run([sys.executable, "-m", "tan", *argv],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=cwd, env=env)


def test_bare_invocation_exits_2_with_help_on_stderr():
    p = run()
    assert p.returncode == 2
    assert p.stdout == ""          # stdout is the envelope channel; help is not an envelope
    assert p.stderr.strip() != ""


def test_version_first_line_matches_the_extension_probe():
    p = run("--version")
    assert p.returncode == 0
    assert p.stdout.splitlines()[0].startswith("tan ")


def test_version_under_format_json_is_an_envelope_not_a_bare_line():
    """Rust routes clap's `--version` output through `emit_parse_error`
    (main.rs): exit 0, no issues, the rendered line as `data.message`. stdout is
    the envelope channel under `--format json`, `--version` included -- a bare
    `tan X.Y.Z` there is not JSON and a consumer parsing stdout gets nothing."""
    for argv in (["--format", "json", "--version"], ["--format=json", "--version"]):
        p = run(*argv)
        assert p.returncode == 0, p.stderr
        env = json.loads(p.stdout)
        assert env["command"] == "cli" and env["issues"] == []
        assert env["data"]["message"].startswith("tan ")


def test_version_does_not_execute_a_following_subcommand(tmp_path):
    """tan-cli#326: the Rust oracle treats `--version` as eager and exits
    before ever looking at a following subcommand; the pre-fix Python root
    callback handled `--version` with a plain `return`, which does not stop
    Click's own group dispatch, so `init` ran anyway and created a project.
    Asserting the version STRING alone (`test_version_first_line_matches_the_
    extension_probe`, above) would keep passing while that kept happening --
    this asserts the observable filesystem side effect instead: nothing is
    created."""
    destination = tmp_path / "project"
    p = run("--version", "init", "--template", "zephyr-app", "--destination", str(destination))
    assert p.returncode == 0, p.stderr
    assert p.stdout.splitlines()[0].startswith("tan ")
    assert "created" not in p.stdout, p.stdout  # `init`'s own success line
    assert not destination.exists(), sorted(str(x) for x in destination.rglob("*"))


def test_version_before_a_subcommand_writes_exactly_one_stdout_document():
    """The JSON-framing half of tan-cli#326: pre-fix, `--version sdk current
    --format json` printed a bare `tan X.Y.Z` line (from the eager-in-name-only
    `--version` handling) followed by `sdk current`'s own JSON envelope --
    two stdout documents, which breaks any consumer that parses stdout as one.
    The oracle folds the trailing `--format json` into a single JSON version
    envelope instead (verified against `target/debug/tan.exe --version sdk
    current --format json`); `sdk current` must never run at all here, on
    either side of the subcommand boundary."""
    p = run("--version", "sdk", "current", "--format", "json")
    assert p.returncode == 0, p.stderr
    assert len(p.stdout.splitlines()) == 1, p.stdout  # exactly one document
    env = json.loads(p.stdout)
    assert env["command"] == "cli", env  # not "sdk" -- sdk current never ran
    assert env["data"]["message"].startswith("tan ")


def test_unknown_command_exits_2_and_emits_an_envelope_in_json_mode():
    p = run("definitely-not-a-command", "--format", "json")
    assert p.returncode == 2
    env = json.loads(p.stdout)
    assert env["ok"] is False and env["exitCode"] == 2
    assert "sdk" not in env or env["sdk"] is not None


def test_pre_subcommand_sdk_root_runs_doctor_not_a_parse_error():
    """`cli._reorder_global_flags` relocates a leading `--sdk-root` to right
    after the subcommand -- but only avoids a parse error if `doctor` also
    declares a matching LOCAL option, which it did not before this fix
    (`doctor_cmd.doctor` had no `--project`, and the extension's
    `alpCli/vscodeAdapter.ts` `withSdkRoot` prepends `--sdk-root` ahead of
    nearly every command it runs). `--sdk-root X` is never a real SDK, so
    `doctor`'s own `sdk` check fails deterministically regardless of host
    state -- rc is exactly `ExitCode.DOCTOR_FAILURE` (4) on every machine,
    matching the oracle (`target/debug/tan.exe --sdk-root X doctor
    --format json`, verified rc=4 with `command: "doctor"`)."""
    p = run("--sdk-root", "X", "doctor", "--format", "json")
    env = json.loads(p.stdout)
    assert env["command"] == "doctor", env  # not "cli" -- i.e. not a parse error
    assert p.returncode == 4, (p.returncode, env)
    assert env["exitCode"] == 4


def test_format_json_help_exits_zero_with_empty_issues():
    """`tan --format json --help` renders real help (exit 0), which must stay
    the process exit code AND the envelope's `exitCode` -- matching the oracle
    (rc=0, `issues: []`)."""
    p = run("--format", "json", "--help")
    assert p.returncode == 0, p.stderr
    env = json.loads(p.stdout)
    assert env["exitCode"] == 0
    assert env["issues"] == []


def test_json_usage_error_message_carries_clicks_specific_reason_on_both_channels():
    """`tan build --bogus --format json`: a Click-level usage error. Pre-fix,
    the JSON envelope's `data.message` was always the generic "invalid command
    line invocation" (`_usage_error_envelope` took no argument), and the
    `finally` block only re-wrote the captured stderr `if envelope_emitted()`
    -- never true on this path -- so the specific reason ("No such option:
    --bogus") existed on NEITHER channel. Both must carry it now: stdout via
    the tee'd capture folded into the envelope, stderr via the tee writing
    through live."""
    p = run("build", "--bogus", "--format", "json")
    assert p.returncode == 2
    env = json.loads(p.stdout)
    assert "No such option" in env["data"]["message"], env
    assert "No such option" in env["issues"][0]["message"], env
    assert "No such option" in p.stderr, repr(p.stderr)


def test_tee_stderr_writes_through_synchronously_and_keeps_a_copy():
    """Unit pin on `_TeeStderr` itself: every write must reach the real stream
    immediately (a customer watching a real Zephyr build must see output as it
    happens, not all at once when the process exits -- the pre-fix bug wrapped
    the whole run in `contextlib.redirect_stderr(io.StringIO())`, which
    buffers everything until the process is about to exit) while still
    keeping a copy for `_usage_error_envelope` to read back."""
    import io as _io

    from tan.cli import _TeeStderr

    real = _io.StringIO()
    tee = _TeeStderr(real)
    tee.write("hello ")
    assert real.getvalue() == "hello ", "must reach the real stream synchronously"
    tee.write("world")
    assert real.getvalue() == "hello world"
    assert tee.getvalue() == "hello world"


def test_format_json_bad_command_help_process_exit_matches_envelope_exit_code():
    """`tan --format json badcmd --help` renders help for an UNKNOWN command,
    which both the oracle and Click exit 2 for. Pre-fix, `_emit_help_envelope`
    printed an `exitCode: 2` envelope but `main()` then `return`ed, so the
    PROCESS exited 0 -- contradicting the envelope on its own stdout (the
    invariant Rust's `json_exit_code` doc comment states). Reproduced by
    reverting `main()` to `_emit_help_envelope(argv); return` instead of
    `sys.exit(_emit_help_envelope(argv))`."""
    p = run("--format", "json", "badcmd", "--help")
    env = json.loads(p.stdout)
    assert env["exitCode"] == 2, env
    assert p.returncode == env["exitCode"], (p.returncode, env["exitCode"])


#: `(argv-tail-after-the-flag, expected-command)`. Each of these five is a
#: pre-subcommand GLOBAL flag position `_reorder_global_flags` already
#: relocates, but pre-fix the target command had no matching LOCAL option
#: declared (`validate`/`explain`/`debug-config` never accepted `--sdk-root`;
#: `doctor`/`flash` never accepted `--project` at all) -- so Click rejected
#: the relocated flag as unrecognised and the run never reached the command:
#: a `cli.parse-error` envelope (exit 2) instead of the command's own. Every
#: shape here is one the extension actually emits (`alpCli/vscodeAdapter.ts`'s
#: `withSdkRoot` prepends `--sdk-root` ahead of nearly every command it runs;
#: `west.ts`'s `alpBuild` invokes `--project <app> build`), verified against
#: the oracle: none of these five is `command: "cli"`.
#: `debug-config` carries `--preview` -- without it a real run of THIS one
#: (unlike the other four, which all refuse before writing anything) would
#: write `launch.json` into whatever directory the test happens to run from.
_GLOBAL_FLAG_PARITY_CASES = [
    (["--sdk-root", "X", "validate"], "validate"),
    (["--sdk-root", "X", "explain"], "explain"),
    (["--sdk-root", "X", "debug-config", "--preview"], "debug-config"),
    (["--project", "app", "doctor"], "doctor"),
    (["--project", "app", "flash"], "flash"),
]


@pytest.mark.parametrize(
    "argv, expected_command",
    _GLOBAL_FLAG_PARITY_CASES,
    ids=[c[1] for c in _GLOBAL_FLAG_PARITY_CASES],
)
def test_pre_subcommand_global_flags_reach_the_real_command(argv, expected_command, tmp_path):
    p = run(*argv, "--format", "json", cwd=tmp_path)
    env = json.loads(p.stdout)
    # Not "cli" -- i.e. this reached the command's own dispatch rather than
    # dying as a Click-level `cli.parse-error` on the relocated flag.
    assert env["command"] == expected_command, env


def test_format_before_another_global_flag_no_longer_aborts_the_whole_reorder(tmp_path):
    """`tan --format json --sdk-root X debug-config --preview`: a leading
    `--format` used to abort `_reorder_global_flags` entirely (it was not in
    `cli._RELOCATABLE_FLAG_ARITY`, and every other unrecognised token aborts
    the rewrite), stranding `--sdk-root` in the unrecognised pre-subcommand
    position. `debug-config` was already one of the commands that honoured a
    pre-subcommand `--format` back when a hand-written allowlist in `cli.py`
    decided that (deleted by tan-cli#378), so this argv isolates the reorder
    fix from that separate, unrelated refusal -- `--preview` so the command
    never writes `launch.json` anywhere."""
    p = run("--format", "json", "--sdk-root", "X", "debug-config", "--preview", cwd=tmp_path)
    env = json.loads(p.stdout)
    assert env["command"] == "debug-config", env
    assert p.returncode == 0, (p.returncode, env)


# --------------------------------------------------------------------------
# tan-cli#394: `--help` in an OPTION position vs as an option's VALUE
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["scaffold", "--help"], True),
        (["scaffold", "--name", "--help"], False),
        (["explain", "--template", "--help"], False),
        (["init", "--format", "json", "--name", "--help", "--destination", "d"], False),
        (["--format", "json", "badcmd", "--help"], True),
        (["validate", "--format", "json", "--help"], True),
    ],
    ids=["bare", "scaffold-name", "explain-template", "init-name", "badcmd", "trailing"],
)
def test_wants_help_is_positional_not_a_substring_scan(argv, expected):
    """`_wants_help` was literally `"--help" in argv`. Click's parser takes the
    next token as a value-carrying option's argument WITHOUT checking whether
    it looks like an option, so a `--help` consumed that way never reaches the
    eager help callback -- the pre-scan believed it had, and `main()` routed
    the whole argv through `_emit_help_envelope`, which RUNS the command under
    `CliRunner` and reports its output as one `command: "cli"` envelope.
    The unit half of the fix; the observable half is below."""
    from tan.cli import _wants_help

    assert _wants_help(list(argv)) is expected


@pytest.mark.parametrize(
    "argv, expected_command",
    [
        (["explain", "--format", "json", "--template", "--help"], "explain"),
        (["scaffold", "--format", "json", "--name", "--help", "--destination", "out"], "scaffold"),
        (["init", "--format", "json", "--name", "--help", "--destination", "dst"], "init"),
    ],
    ids=["explain", "scaffold", "init"],
)
def test_a_help_used_as_an_option_value_does_not_swap_the_envelope(
    argv, expected_command, tmp_path
):
    """tan-cli#394, end to end. Measured before the fix, all three answered
    `command: "cli"` while the command ran to completion: `scaffold` wrote
    three real files and `init` created a directory literally named `--help`,
    both reported as `{"command":"cli","ok":true,...,"data":{"message":"<the
    escaped inner envelope>"}}` -- so a consumer reading `data.written` saw
    `[]` for a run that wrote, and `explain`'s real `explain.template-unknown`
    refusal was re-labelled `cli.parse-error`, a code
    `contract/issue-codes.json` records as `consumer: "none"`.

    The assertion is on `command`, not on the files: `contract/README.md`
    states the extension dispatches on it, and #394's own acceptance says no
    argv that causes files to be written may return `command: "cli"`. Text
    mode always reported the truth; this is the JSON channel catching up."""
    p = run(*argv, cwd=tmp_path)
    env = json.loads(p.stdout)
    assert env["command"] == expected_command, env


# --------------------------------------------------------------------------
# tan-cli#399: the derived envelope-SHAPE gate over the whole command table
# --------------------------------------------------------------------------
#
# #399 asked for a test that "enumerates the registered command table" and
# asserts every command's `--format json` emits a real
# `{command,ok,exitCode,project,data,issues}` envelope, with a NAMED exemption
# list -- rather than leaving an exemption as prose in `contract/README.md`
# with nothing in the test suite tying it to the code that actually does it.
# This is that gate; before it, nothing walked the registration table and
# asked "does the SHAPE hold", only "does the flag PARSE"
# (`tests/gates/test_global_flags_gate.py`) -- a `--help`-suffixed probe that
# never reaches a command's own success/failure envelope at all (Click's eager
# `--help` short-circuits before ANY command body runs, so a `--help` probe
# cannot distinguish a wrapped command from an unwrapped one; `faultdecode
# --format json --help` answers the SAME generic help envelope every other
# command does).
#
# EMPTY as of #399's own close-out: `faultdecode` was the one exemption this
# set ever carried, and `faultdecode_cmd.py` now gives `--format json` a real
# envelope too (its OWN `--json` stays the unwrapped SDK-report compatibility
# surface -- see that module's docstring -- but `--format json` is a second,
# distinct spelling that wraps the same report). If a future command needs an
# exemption, name it here AND in `contract/README.md`'s "Deliberately outside
# the envelope" section together, or this gate and that doc drift apart the
# way #399 was filed about.
_ENVELOPE_SHAPE_EXEMPT: dict[str, str] = {}

_ENVELOPE_SHAPE_KEYS = {"command", "ok", "exitCode", "project", "data", "issues"}

_ALL_COMMANDS = sorted(get_command(app).commands)


def _run_isolated(*argv, cwd: Path, home: Path) -> subprocess.CompletedProcess:
    """Like `run()` above, but with `HOME`/`USERPROFILE` redirected at a
    scratch directory (so a developer's real `~/.alp/sdk-default` cannot make
    a command resolve an SDK it otherwise would not, the same isolation
    `tests/parity/oracle.py`'s own `_env` applies) and `PATH` pinned to
    `empty_tool_inventory` (so a `west`-forwarding command -- `lock`/
    `migrate`/`quality` -- cannot spawn a REAL `west` this replay host happens
    to have installed; every other command's PATH probes, e.g. `doctor`'s
    toolchain checks, are meant to see "nothing found" here too, which is
    still a valid, shape-complete envelope)."""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PATH": empty_tool_inventory(home),
    }
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=env,
        timeout=30,
    )


@pytest.mark.parametrize("command", [c for c in _ALL_COMMANDS if c not in _ENVELOPE_SHAPE_EXEMPT])
def test_every_non_exempt_command_emits_the_real_envelope_shape(command, tmp_path):
    """A BARE `<command> --format json` (no other flags), run for real -- not
    `--help`, which never reaches the command's own envelope at all (see the
    section docstring above). Every one of these 31 commands is cheap and
    side-effect-contained to run this way: with no board.yaml/SDK/`.west`
    workspace resolvable in an isolated cwd and an isolated `HOME`, each
    either refuses fast on its own first guard clause or (`init`/`pinmux`/...)
    does real, but purely LOCAL, work inside `tmp_path` -- never a real
    device, network call, or build. Only the top-level KEY SET is asserted
    (tan-cli#399's own ask): the values are host facts (PATH contents, cwd)
    this gate does not try to pin, that job belongs to the oracle-parity
    cases and the per-command test files."""
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    proc = _run_isolated(command, "--format", "json", cwd=cwd, home=home)
    assert proc.stdout.strip(), (
        f"`tan {command} --format json` printed nothing on stdout "
        f"(rc={proc.returncode}):\n{proc.stderr}"
    )
    doc = json.loads(proc.stdout)
    assert isinstance(doc, dict), doc
    missing = _ENVELOPE_SHAPE_KEYS - doc.keys()
    assert not missing, (
        f"`tan {command} --format json` is missing envelope key(s) {missing}: {doc}\n"
        f"If this command's `--format json` is DELIBERATELY not tan's envelope "
        f"(faultdecode's own reason, above), name it in `_ENVELOPE_SHAPE_EXEMPT` "
        f"AND in `contract/README.md`'s \"Deliberately outside the envelope\" "
        f"section -- never one without the other."
    )


def test_envelope_shape_gate_still_covers_the_full_registered_surface():
    """The canary every derived gate in this suite carries: a parametrised
    list that silently shrinks to zero reports green while checking nothing."""
    assert len(_ALL_COMMANDS) >= 30, (
        f"only {len(_ALL_COMMANDS)} commands registered; expected the full "
        "~32-command surface."
    )
    assert set(_ENVELOPE_SHAPE_EXEMPT) <= set(_ALL_COMMANDS), (
        f"{set(_ENVELOPE_SHAPE_EXEMPT) - set(_ALL_COMMANDS)} is exempted but no "
        "longer a registered command -- stale entry."
    )


def test_every_registered_command_declares_a_help_panel():
    """Drift guard: a command registered without a panel silently falls into
    Typer's default "Commands" box, which is the flat 32-item list this
    grouping exists to replace. Derived from the registration table rather
    than a hand-kept name list, for the same reason `_SUBCOMMAND_NAMES` is."""
    from tan.cli import app

    unpanelled = sorted(
        info.name for info in app.registered_commands if not info.rich_help_panel
    )
    assert unpanelled == []


def test_help_renders_the_six_panels():
    p = run("--help")
    assert p.returncode == 0
    for panel in (
        "Setup",
        "Start a project",
        "Configure",
        "Build & run",
        "Hardware",
        "Inspect & author",
    ):
        assert panel in p.stdout, f"missing panel: {panel}"


def test_bare_invocation_points_a_new_user_somewhere():
    """"a command is required" is true and useless. A first-time user needs
    the three verbs that get them from nothing to a running build."""
    p = run()
    assert p.returncode == 2
    assert p.stdout == ""          # unchanged: stdout is the envelope channel
    assert "tan doctor" in p.stderr
    assert "tan init" in p.stderr
