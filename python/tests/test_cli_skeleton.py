# SPDX-License-Identifier: Apache-2.0
import json, subprocess, sys


def run(*argv):
    return subprocess.run([sys.executable, "-m", "tan", *argv],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


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
