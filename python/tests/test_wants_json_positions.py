# SPDX-License-Identifier: Apache-2.0
"""tan-cli#491: `--format json` is only a request for the envelope channel
when it sits in an OPTION position of tan's OWN parser -- never as some other
option's VALUE, and never past a `--` that hands the rest of argv to a
forwarded child (`west alp-quality`, `west alp-lock`, ...).

`cli._wants_json` used to be a plain adjacent-pair textual scan (`arg ==
"--format" and argv[i+1] == "json"`), with no notion of either boundary --
mirroring the arity-blind bug `_wants_help` already had fixed for `--help` in
tan-cli#394 (see `test_wants_help_positions.py`'s own module docstring for
that defect's shape). `main()` uses `_wants_json`'s answer to decide the
ENTIRE run's output channel, so a false positive replaces a real, coded
refusal (`command: "quality"` / `quality.profile-required`) with an
unrequested JSON document (`command: "cli"` / `cli.parse-error`) on stdout --
for a caller that asked for `text` and got JSON, and whose real refusal
reason (a west child's exit) is mislabelled as a parse error it never was.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tan.cli import _wants_json

#: `python/` -- `python -m tan` resolves the package off `os.getcwd()`, so a
#: child run from a `tmp_path` cwd needs this pinned onto its `PYTHONPATH` or
#: it cannot import the package at all.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def run(*argv, cwd=None):
    """Drive the SOURCE tree's `tan` in a child process -- `main()`'s argv
    routing (`_reorder_global_flags` -> `_wants_json` -> the tee'd dispatch)
    only runs at the process boundary, so a `CliRunner` in-process invoke
    would skip the very code under test."""
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
        ),
    }
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=cwd, env=env,
    )


#: `(argv, wants_json)` -- every row is a POSITION question, never a presence
#: one, mirroring `test_wants_help_positions.py`'s own `_POSITION_CASES`.
_POSITION_CASES = [
    (["--format", "json", "build"], True),           # ordinary pre-subcommand case
    (["build", "--format", "json"], True),            # ordinary post-subcommand case
    (["build", "--format=json"], True),               # the `=` spelling
    (["quality", "--", "--format", "json"], False),   # forwarded past `--`: not tan's own flag
    (["lock", "--", "--format", "json"], False),      # same shape, the sibling forwarder
    (["explain", "--", "--format", "json"], False),   # `--` ends option parsing for Click too
    (["init", "--name", "--format", "--destination", "json"], False),  # "json" is `--destination`'s value, not `--format`'s
    (["build", "--verbose", "--format", "json"], True),  # a BOOLEAN flag consumes nothing
    (["--format", "json", "badcmd"], True),           # unknown command: stay conservative like `_wants_help`
    ([], False),
]


@pytest.mark.parametrize("argv, wants_json", _POSITION_CASES)
def test_wants_json_asks_where_the_token_is_not_whether_it_is_present(argv, wants_json):
    assert _wants_json(argv) is wants_json, argv


def test_forwarded_format_json_past_dash_dash_does_not_hijack_the_channel():
    """The exact tan-cli#491 repro: `tan quality -- --format json` forwards
    `--format json` to `west alp-quality`, so tan's OWN `--format` stays
    `text`. Pre-fix this reported `command:"cli"` / `cli.parse-error` /
    "invalid command line invocation" on STDOUT, in JSON, for a run that
    asked for `text` -- confirmed, byte-for-byte, against the unfixed
    `tan/cli.py` while writing this fix. The honest answer here is `quality`'s
    OWN text-mode refusal (`--profile` is required), on stderr, at exit 2 --
    nothing on stdout, because tan never entered JSON mode for this argv."""
    p = run("quality", "--", "--format", "json")
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == "", "text mode: stdout must carry no envelope"
    assert "--profile" in p.stderr, p.stderr


def test_real_format_json_after_the_subcommand_still_answers_an_envelope():
    """Preserved behaviour: `--format json` in a genuine option position (not
    forwarded past `--`) still answers the coded envelope, at the real
    `quality.*` code -- not the `cli.parse-error` fallback."""
    p = run("quality", "--format", "json")
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    envelope = json.loads(p.stdout)
    assert envelope["command"] == "quality", envelope
    assert envelope["issues"][0]["code"] == "quality.profile-required", envelope
