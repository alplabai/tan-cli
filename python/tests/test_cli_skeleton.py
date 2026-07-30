# SPDX-License-Identifier: Apache-2.0
import json, os, subprocess, sys
from pathlib import Path

import pytest

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
    `_GLOBAL_FLAG_ARITY`, and every other unrecognised token aborts the
    rewrite), stranding `--sdk-root` in the unrecognised pre-subcommand
    position. `debug-config` is one of the commands that already honours a
    pre-subcommand `--format` (`cli._HONOURS_ROOT_FORMAT`), so this argv
    isolates the reorder fix from that separate, unrelated refusal --
    `--preview` so the command never writes `launch.json` anywhere."""
    p = run("--format", "json", "--sdk-root", "X", "debug-config", "--preview", cwd=tmp_path)
    env = json.loads(p.stdout)
    assert env["command"] == "debug-config", env
    assert p.returncode == 0, (p.returncode, env)
