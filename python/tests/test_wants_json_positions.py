# SPDX-License-Identifier: Apache-2.0
"""tan-cli#546 (= #491 defect 3): which CHANNEL a run answers on is decided by
the PARSE OUTCOME, not by a textual scan of argv.

`crates/tan-cli/src/main.rs` is shaped like this:

    match Cli::try_parse() {
        Ok(cli) => run(cli),                    // channel = cli.format
        Err(e)  => if wants_json(argv) { ... }  // channel = the textual scan
    }

so the naive `wants_json` scan is consulted ONLY after clap has already failed
and can never affect a run that parsed. The port promoted the same scan to a
process-wide switch, so a `--format json` that is not tan's own flag -- one
forwarded past `--` to `west alp-quality`, or one Click resolves as another
option's VALUE -- flipped the whole run into JSON mode. A text-mode command
emits no envelope, so `main()`'s usage-error fallback then printed an
unrequested `command:"cli"` / `cli.parse-error` document on stdout, replacing
the real coded answer and mislabelling a west child's exit as a parse error.

## Why this is not fixed by a smarter scan

Two earlier attempts rewrote `_wants_json` itself (an arity walk, then a
clap-style hyphen guard) and each reopened the defect a different way; #546
proposed a third textual shape. All three are ruled out by MEASUREMENT, because
the oracle answers two structurally identical argvs differently:

    target/debug/tan quality -- --format json   ->  TEXT  (0 bytes on stdout)
    target/debug/tan build   -- --format json   ->  JSON  (`cli.parse-error`)

The only thing separating them is whether the parser ACCEPTED the argv
(`quality` forwards trailing args, `build` refuses them) -- which no scan of
argv alone can know. So `_wants_json` stays exactly the naive port of Rust's
own scan (pinned below), and `tan.cli._DispatchedCommand` records the parse
outcome instead.

Every expectation here was measured against `target/debug/tan` (`tan 0.4.1`)
from the same cwd with the same argv; the oracle's answer is named per case.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tan.cli import _wants_json

#: `python/` -- see `test_wants_help_positions.py`'s copy of this note.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def run(*argv, cwd=None):
    """Drive the SOURCE tree's `tan` in a child process -- `main()`'s channel
    routing only runs at the process boundary, so a `CliRunner` in-process
    invoke would skip the very code under test."""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=cwd, env=env, timeout=300,
    )


#: `(argv, wants_json)` for the SCAN itself. Pinned deliberately UNCHANGED from
#: `origin/dev`: this function is the faithful port of Rust's `wants_json`, and
#: its whole contract is "adjacent `--format` `json` pair, anywhere" -- it is
#: `--`-blind and arity-blind exactly as Rust's is. Anyone tempted to make it
#: cleverer should read this module's docstring first; the fix for #546 is not
#: here.
_SCAN_CASES = [
    (["--format", "json", "build"], True),
    (["build", "--format", "json"], True),
    (["build", "--format=json"], True),
    (["--sdk-root", "--format", "json", "build"], True),
    (["quality", "--", "--format", "json"], True),  # the scan still says yes ...
    (["build", "--format", "text"], False),
    ([], False),
]


@pytest.mark.parametrize("argv, wants_json", _SCAN_CASES)
def test_the_scan_is_the_unchanged_port_of_rusts_wants_json(argv, wants_json):
    assert _wants_json(argv) is wants_json, argv


def test_forwarded_format_json_past_dash_dash_does_not_hijack_the_channel(tmp_path):
    """#546 acceptance 1. `tan quality -- --format json` forwards the pair to
    `west alp-quality`; tan's OWN `--format` stays `text`, and `quality` really
    runs (its parse succeeds -- it takes trailing args). Pre-fix this answered
    `{"command":"cli",...,"code":"cli.parse-error"}` on STDOUT for a caller
    that asked for text, hiding the real refusal. ORACLE: exit 1, ZERO bytes on
    stdout, the refusal on stderr -- text mode, because clap parsed it too."""
    p = run("quality", "--", "--format", "json", cwd=tmp_path)
    assert p.stdout == "", f"text mode: stdout must carry no envelope, got {p.stdout!r}"
    assert "--profile" in p.stderr, p.stderr


def test_a_forwarded_west_failure_is_not_reported_as_a_parse_error(tmp_path):
    """The sibling forwarder, and the sharper half of the same defect: `lock`'s
    failure is a west child's non-zero exit, which pre-fix was relabelled
    `cli.parse-error` at exit 1. ORACLE: exit 1, zero bytes on stdout."""
    p = run("lock", "--", "--format", "json", cwd=tmp_path)
    assert p.stdout == "", f"text mode: stdout must carry no envelope, got {p.stdout!r}"


def test_a_value_position_format_json_does_not_flip_the_channel(tmp_path):
    """#546 acceptance 2. Click resolves `--profile`'s value as the literal
    `--format` (its parser pops the next token unconditionally, hyphen or not),
    so tan's own `--format` is never set and the run is TEXT mode. ORACLE:
    exit 1, zero bytes on stdout -- same channel."""
    p = run("quality", "--profile", "--format", "json", cwd=tmp_path)
    assert p.stdout == "", f"text mode: stdout must carry no envelope, got {p.stdout!r}"


def test_a_relocated_format_text_after_the_subcommand_wins(tmp_path):
    """#491 defect 3's second vector. Click's last-wins on the relocated flag
    leaves `build` in TEXT mode while the argv still carries an adjacent
    `--format json` pair for the scan to find. ORACLE: exit 2, zero bytes on
    stdout, the readiness report on stderr -- text mode, because clap's own
    global `--format` resolved to `text` and the parse succeeded."""
    p = run("--format", "json", "build", "--format", "text", cwd=tmp_path)
    assert p.stdout == "", f"text mode: stdout must carry no envelope, got {p.stdout!r}"
    assert p.stderr != "", "the answer has to be SOMEWHERE"


def test_a_global_flag_dangling_before_format_json_still_answers_an_envelope(tmp_path):
    """#546 acceptance 3 -- "the case most worth pinning", and the regression
    attempt 1 introduced. `--sdk-root` is a value-taking flag every SUBCOMMAND
    declares but `root` itself does not, so this argv is a genuine Click-level
    parse failure: no command body ever runs, which is Rust's `Err` arm, where
    the naive scan is exactly what decides. ORACLE: exit 2 with
    `{"command":"cli",...,"code":"cli.parse-error"}` -- an envelope, never zero
    bytes."""
    p = run("--sdk-root", "--format", "json", "build", cwd=tmp_path)
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert p.stdout.strip() != "", "stdout must carry the envelope, not be empty"
    envelope = json.loads(p.stdout)
    assert envelope["command"] == "cli", envelope
    assert envelope["exitCode"] == 2, envelope
    assert envelope["issues"][0]["code"] == "cli.parse-error", envelope


def test_a_dash_dash_that_the_parser_refuses_still_answers_an_envelope(tmp_path):
    """The other side of the `--` coin, and why a `--`-terminator in the scan
    would have been wrong: `build` takes no trailing arguments, so this argv is
    a real parse failure and the caller's `--format json` is the only signal
    there is. ORACLE: exit 2 with a `cli.parse-error` envelope (measured for
    `explain`, `validate`, `doctor` and `clean` in this shape too)."""
    p = run("build", "--", "--format", "json", cwd=tmp_path)
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    envelope = json.loads(p.stdout)
    assert envelope["command"] == "cli", envelope
    assert envelope["issues"][0]["code"] == "cli.parse-error", envelope


def test_a_real_format_json_still_answers_the_commands_own_coded_envelope(tmp_path):
    """Preserved behaviour, and the control for every case above: a `--format
    json` in a genuine option position still answers the command's OWN coded
    envelope, not the `cli.parse-error` fallback."""
    p = run("quality", "--format", "json", cwd=tmp_path)
    envelope = json.loads(p.stdout)
    assert envelope["command"] == "quality", envelope
    assert envelope["issues"][0]["code"] == "quality.profile-required", envelope
