# SPDX-License-Identifier: Apache-2.0
"""tan-cli#394: `--help` is only help when it sits in an OPTION position.

`cli._wants_help` used to be `return "--help" in argv`, and `main()` routes the
whole run through `_emit_help_envelope` (a second, `CliRunner`-driven dispatch)
whenever it says yes. A `--help` that Click consumes as some option's VALUE
never reaches Click's eager help callback, so the textual scan and Click
disagreed -- and under `--format json` the disagreement was invisible to the
caller: the command still ran (writing files, on `scaffold`/`init`) while the
envelope on stdout was swapped for a `command: "cli"` one whose `data` is a
single `message` string. `contract/README.md:31-38` records that both
consumer-side string matches FAIL OPEN, so alp-sdk-vscode renders "no files
changed" for a `scaffold` that wrote three, without erroring, logging or
warning.

The subprocess cases below are the ones that would have caught it: they assert
JSON mode and text mode agree about what happened, which is exactly what the
swap broke.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tan.cli import _wants_help

#: `python/` -- `python -m tan` resolves the package off `os.getcwd()` (`-m`
#: prepends the CURRENT working directory to `sys.path`, not the script's own
#: location), so a child run from a `tmp_path` cwd needs this pinned onto its
#: `PYTHONPATH` or it cannot import the package at all.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def run(*argv, cwd=None):
    """Drive the SOURCE tree's `tan` in a child process -- `main()`'s argv
    routing (`_reorder_global_flags` -> `_wants_help` -> `_emit_help_envelope`)
    only runs at the process boundary, so a `CliRunner` in-process invoke would
    skip the very code under test."""
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


#: `(argv, is_help)` -- every row is a POSITION question, never a presence one.
#: The first two are the issue's own acceptance criterion verbatim; the rest pin
#: the shapes that must keep working around it.
_POSITION_CASES = [
    (["scaffold", "--name", "--help"], False),   # value of `--name`
    (["scaffold", "--help"], True),              # option position
    (["explain", "--format", "json", "--template", "--help"], False),
    (["init", "--name", "--help", "--destination", "out"], False),
    (["scaffold", "--name=--help"], False),      # `--opt=value` form, no bare token
    (["--format", "json", "--help"], True),      # root help past a value-taking `--format`
    (["build", "--verbose", "--help"], True),    # a BOOLEAN flag consumes nothing
    (["--format", "json", "badcmd", "--help"], True),   # unknown command: stay conservative
    (["explain", "--", "--help"], False),        # `--` ends option parsing for Click too
    ([], False),
]


@pytest.mark.parametrize("argv, is_help", _POSITION_CASES)
def test_wants_help_asks_where_the_token_is_not_whether_it_is_present(argv, is_help):
    assert _wants_help(argv) is is_help, argv


#: `(subcommand, value_taking_flag)`. Each flag takes a value, so the `--help`
#: after it is that value and never Click's eager help -- `explain` refuses it
#: (`explain.template-unknown`, exit 1), `scaffold`/`init` accept it as a name
#: and WRITE files. All three must report the same thing on both channels.
_VALUE_FLAG_CASES = [("explain", "--template"), ("scaffold", "--name"), ("init", "--name")]


def _argv_for(subcommand: str, flag: str, destination: Path) -> list[str]:
    """`explain --template` (the one case exercised here) reads nothing from
    disk and writes nothing -- unlike `--code`, which reads a checkout, this
    case never reaches that path; the two write commands need somewhere to
    put their output."""
    tail = [] if subcommand == "explain" else ["--destination", str(destination)]
    return [subcommand, flag, "--help", *tail]


def _written_files(destination: Path) -> set[str]:
    """The files actually on disk, spelled the way the ENVELOPE spells them.

    `data.written` carries POSIX separators on every host -- `tan` normalises
    before emitting, so a consumer parsing the envelope sees
    `include/modules/help.h` whatever it is running on. `Path.relative_to`
    renders the HOST's separator, so comparing the two raw sets is green on
    POSIX and red on Windows alone -- measured on windows-latest as
    `assert {'include/mod.../help/help.c'} == {'include\\mo...help\\help.c'}`.

    Normalise the filesystem side, never the envelope side: the envelope is the
    contract, and a test that rewrote it would be asserting the wrong thing.
    """
    if not destination.exists():
        return set()
    return {
        p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize("subcommand, flag", _VALUE_FLAG_CASES)
def test_json_mode_and_text_mode_agree_when_help_is_an_option_value(subcommand, flag, tmp_path):
    """The core of tan-cli#394. Pre-fix, JSON mode answered `command: "cli"`
    with `data: {"message": "<the real envelope, escaped>"}` while text mode
    answered the truth -- and for `scaffold`/`init` the files landed on disk
    either way, so `ok: true`, rc 0 and an empty-looking `data` described a run
    that had genuinely written three (and six) files."""
    text_dir = tmp_path / "text"
    json_dir = tmp_path / "json"
    text = run(*_argv_for(subcommand, flag, text_dir))
    js = run(*_argv_for(subcommand, flag, json_dir), "--format", "json")

    envelope = json.loads(js.stdout)
    assert envelope["command"] == subcommand, envelope
    assert js.returncode == text.returncode, (js.returncode, text.returncode, js.stdout)
    assert js.returncode == envelope["exitCode"], (js.returncode, envelope)
    assert _written_files(json_dir) == _written_files(text_dir)


def test_explain_keeps_its_namespaced_issue_code_when_the_template_is_dash_dash_help():
    """`cli.parse-error` is `status: "reserved"`, `consumer: "none"` in
    `contract/issue-codes.json`, so an extension matching frozen codes with
    `===` got NO verdict for a refusal that did happen -- the real
    `explain.template-unknown` was buried as an escaped string inside
    `data.message`."""
    p = run("explain", "--format", "json", "--template", "--help")
    envelope = json.loads(p.stdout)
    assert envelope["command"] == "explain", envelope
    assert envelope["issues"][0]["code"] == "explain.template-unknown", envelope
    assert p.returncode == 1, (p.returncode, envelope)

    text = run("explain", "--template", "--help")
    assert text.stderr.strip() + text.stdout.strip() != ""
    assert "unknown template '--help'" in (text.stdout + text.stderr), (text.stdout, text.stderr)


def test_scaffold_reports_the_files_it_actually_wrote(tmp_path):
    """The fail-open path from `contract/README.md:31-38` made concrete: every
    `data.written` / `data.fileChanges` read fell back to `[]` because `data`
    was `{"message": ...}`, so the extension rendered "no files changed" for a
    module it had just created on disk."""
    destination = tmp_path / "out"
    p = run("scaffold", "--format", "json", "--name", "--help", "--destination", str(destination))
    envelope = json.loads(p.stdout)
    assert envelope["command"] == "scaffold", envelope
    # As SETS -- `written` is in the emitter's own order, which the envelope
    # contract does not promise to be sorted; what matters is that it names
    # every file that reached disk instead of falling back to `[]`.
    assert set(envelope["data"]["written"]) == _written_files(destination), envelope
    assert envelope["data"]["written"], envelope


def test_real_help_after_a_subcommand_still_yields_one_zero_exit_envelope():
    """Preserved behaviour: `--help` in a genuine option position is still
    intercepted by `_emit_help_envelope` -- exit 0, `issues: []`, the rendered
    help as `data.message` (Rust's `emit_parse_error` on clap's DisplayHelp)."""
    p = run("generate", "--format", "json", "--help")
    assert p.returncode == 0, p.stderr
    envelope = json.loads(p.stdout)
    assert envelope["exitCode"] == 0 and envelope["issues"] == [], envelope
    assert "Usage: tan generate" in envelope["data"]["message"], envelope


def test_unknown_command_with_help_still_exits_2_with_exactly_one_envelope():
    """Preserved behaviour: an unknown command name is not something this scan
    can resolve options against, so it stays conservative and hands the argv to
    `_emit_help_envelope` exactly as before -- exit 2, one document on
    stdout."""
    p = run("--format", "json", "badcmd", "--help")
    assert p.returncode == 2, (p.returncode, p.stdout)
    envelope = json.loads(p.stdout)
    assert envelope["exitCode"] == 2, envelope
