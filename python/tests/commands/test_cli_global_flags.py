# SPDX-License-Identifier: Apache-2.0
"""tan-cli#378: `--format` is `#[arg(long, global = true, ...)]` on the v0.4.1
oracle, so `tan --format json <command>` parses there for EVERY command. This
port accepted it before the subcommand name on 12 of the 32 registered
commands and refused the other 20 at exit 2 with a `command: "cli"` /
`cli.parse-error` envelope -- the wrong command in the envelope, on argv the
oracle runs, for machine callers (`alp-sdk-vscode/src/alpCli/vscodeAdapter.ts`
puts global options ahead of the subcommand) that then key off it. Measured
before the fix: `tan --format json doctor`, `... model list`, `... sdk
current`, `... validate`.

The 12/32 split came from a hand-written allowlist in `cli.py` (deleted with
the fix), so every case here is DERIVED from `tan.cli.app`'s own `app.command(...)`
registration table rather than listed: a command registered later is covered
by the line that registers it, with nothing for anyone to remember. Listing
the commands here would rebuild the same defect one level up -- a longer
hand-kept list, silently short by whatever was added last.

Both checks are safe by construction. The first never invokes a command at
all (Click parsing only, `resilient_parsing=True`); the second appends
`--help`, Click's eager short-circuit, so nothing dials a Zephyr build, spawns
`west`, or writes a project file -- the same probe
`tests/gates/test_global_flags_gate.py` documents for the tan-cli#261 surface.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.main import get_command

from tan.cli import _reorder_global_flags, app, main

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

#: The registration table itself. `get_command(app)` is what `main()` runs, so
#: its `.commands` is the same mapping Click dispatches on -- derived from the
#: `app.command(...)` calls, never a copy of them.
_GROUP = get_command(app)
_COMMANDS = sorted(_GROUP.commands)


def _parse(argv: list[str]) -> tuple[str, object, list[str]]:
    """Push `argv` through the rewrite `main()` applies and report what the
    COMMAND's own parser ends up with: `(command name, the value bound to its
    own `--format` parameter, whatever is still stranded ahead of the command
    name)`.

    The third element is the half a value check alone would miss: Click hands
    a subcommand only the tokens AFTER its name and parses everything before
    it against the group callback, so a token left stranded there is the exact
    "No such option"/refusal shape this issue is about, even when the command
    would have accepted it one position later.

    `resilient_parsing=True` so a command with required options
    (`faultdecode --cfsr`, `init --destination`) can be parsed without
    supplying them and without running: Click then swallows the
    missing/invalid-value errors instead of raising, which is fine here --
    nothing in this file asserts on validation, only on WHERE the flag lands.
    """
    rewritten = _reorder_global_flags(list(argv))
    boundary = next(i for i, token in enumerate(rewritten) if token in _GROUP.commands)
    name = rewritten[boundary]
    command = _GROUP.commands[name]
    ctx = command.make_context(name, rewritten[boundary + 1 :], resilient_parsing=True)
    # By the FLAG STRING, not the Python parameter name: it is `output_format`
    # in most command modules but the port has never guaranteed that, and the
    # oracle's contract is about the flag.
    param = next(p for p in command.params if "--format" in p.opts)
    return name, ctx.params.get(param.name), rewritten[:boundary]


@pytest.mark.parametrize("command", _COMMANDS)
def test_format_json_lands_identically_on_either_side_of_the_subcommand(command: str):
    """The port-wide invariant, per registered command: the two positions are
    interchangeable. Both must resolve to the SAME command and leave its own
    `--format` holding `"json"` -- which is the envelope mode, since every
    command body derives `json_mode` from that one parameter."""
    before = _parse(["--format", "json", command])
    after = _parse([command, "--format", "json"])

    assert before == after, (
        f"`tan --format json {command}` and `tan {command} --format json` do not "
        f"parse the same way: {before} vs {after}"
    )
    assert before == (command, "json", []), (
        f"`tan --format json {command}` must reach `{command}` with its own "
        f"--format set to 'json' and nothing stranded before the command name; "
        f"got {before}. `--format` is relocated across the subcommand boundary "
        "by `cli._reorder_global_flags` (tan-cli#378) -- if this command is not "
        "reached, the boundary was not recognised."
    )


@pytest.mark.parametrize("command", _COMMANDS)
def test_no_command_turns_a_pre_subcommand_format_into_a_parse_error(
    command: str, monkeypatch, capsys
):
    """The end-to-end half, through `main()` itself: the parse check above
    never runs the GROUP callback, and the 12/32 refusal lived precisely
    there (`root` failed the whole invocation before Click ever resolved the
    subcommand). A regression that re-added a root-level refusal would slip
    past a parse-only test.

    Both argv shapes must produce the byte-identical stdout envelope. Under
    `--format json`, `--help` is intercepted by `_emit_help_envelope` and
    lands as one envelope carrying the rendered help as `data.message`, so
    "same envelope" here means the same command's help at the same exit code
    -- and a refusal is a different message at exit 2, which is what this
    caught before the fix (measured: 20 of 32 commands)."""
    pre = _run_main(["--format", "json", command, "--help"], monkeypatch, capsys)
    post = _run_main([command, "--format", "json", "--help"], monkeypatch, capsys)

    assert pre == post, f"`--format json` before `{command}` diverges from after it"
    code, out = pre
    assert code == 0, f"`tan --format json {command} --help` exited {code}: {out}"
    assert "cli.parse-error" not in out, out
    assert f"Usage: tan {command}" in out, out


def _run_main(argv: list[str], monkeypatch, capsys) -> tuple[int | None, str]:
    """`main()` end to end on a real argv -- `sys.argv` is what it reads (and
    rewrites), and `_version_callback` re-reads it too, so patching that is
    the only faithful way to drive it in-process. Always raises `SystemExit`
    on this path: the `--format json` + `--help` route ends in
    `sys.exit(_emit_help_envelope(argv))`."""
    monkeypatch.setattr(sys, "argv", ["tan", *argv])
    with pytest.raises(SystemExit) as exit_info:
        main()
    return exit_info.value.code, capsys.readouterr().out


#: Values the oracle's global `--format` does NOT have. Measured against
#: `target/debug/tan.exe`: each answers `error: invalid value '<x>' for
#: '--format <FORMAT>'  [possible values: text, json]` at exit 2, in BOTH argv
#: positions, having run nothing.
#:
#: `sarif`/`diagnostic-v1` are not arbitrary stand-ins for "a bad value":
#: `validate` really does implement both AFTER its own name, a deliberate port
#: extension past the oracle. That makes them exactly the values a
#: relocate-unconditionally rule promotes to GLOBAL by accident -- measured on
#: the first cut of tan-cli#378, `tan --format sarif validate` printed a full
#: SARIF 2.1.0 document where the oracle printed a usage error. `bogus` is the
#: same rule seen from the plain side, and is the one that matters for a
#: command like `new-som` that WRITES: reaching a command body at all means the
#: value was accepted, and `new-som` then ran on to creating metadata files.
_NON_GLOBAL_FORMAT_VALUES = ("sarif", "diagnostic-v1", "bogus")


@pytest.mark.parametrize("value", _NON_GLOBAL_FORMAT_VALUES)
@pytest.mark.parametrize("command", _COMMANDS)
def test_a_format_value_the_oracle_rejects_is_never_relocated(command: str, value: str):
    """The other half of relocation, per registered command: `--format` is
    global only for the values the oracle's `--format` actually HAS. Anything
    else must be left sitting in front of the subcommand name, where `root`'s
    own `_format_callback` refuses it during PARSING -- before the command
    body, and so before any command's writes.

    The rewrite COLLAPSES to the offending `--format <value>` alone rather
    than returning argv untouched (tan-cli#378 residual): leaving the rest in
    place did not actually reach `root`'s refusal -- see
    `test_a_second_format_cannot_smuggle_a_rejected_value_past_root` below --
    so what this asserts is the command name never survives, i.e. nothing can
    run.

    Derived from the same registration table as the cases above, and pure:
    `_reorder_global_flags` is a `list[str] -> list[str]` function, so this
    runs nothing for any of the 32 commands. `tan --format bogus clean` and
    `tan --format bogus flash` are checked as cheaply as `--help` is.
    """
    spaced = _reorder_global_flags(["--format", value, command])
    assert spaced == ["--format", value], (
        f"`tan --format {value} {command}` must collapse to the refusal argv; "
        f"the oracle rejects '{value}' at parse in both positions, so `{command}` "
        f"must not survive the rewrite. Got {spaced}"
    )
    joined = _reorder_global_flags([f"--format={value}", command])
    assert joined == [f"--format={value}"], (
        f"`tan --format={value} {command}` -- the `=` spelling is the same flag "
        f"and must collapse identically. Got {joined}"
    )


@pytest.mark.parametrize(
    "argv, rejected",
    [
        (["--format", "bogus", "--format", "json", "new-som", "--sku", "FOO"], "bogus"),
        (["--format", "sarif", "--format", "json", "validate", "--offline"], "sarif"),
    ],
    ids=["bogus", "sarif"],
)
def test_a_second_format_cannot_smuggle_a_rejected_value_past_root(argv, rejected, tmp_path):
    """The tan-cli#378 RESIDUAL, and a regression against the pre-#378 tree.

    `root`'s `--format` is a plain, non-`multiple` Click option: the parser
    overwrites `state.opts` per occurrence and only ever type-casts the LAST
    one, so `_format_callback` never saw `bogus` when a second `--format json`
    followed it. With the rewrite merely ABORTING on the bad value, both
    tokens were left in front of the subcommand, the later one won, and the
    command RAN -- measured against the oracle:

        ORACLE  tan.exe --format bogus --format json new-som --sku FOO
                -> rc=2, "invalid value 'bogus'", NO WORK DONE
        PRE-FIX py -3.12 -m tan (same argv)
                -> rc=2, but new-som's BODY ran (its SDK-preflight line printed)

    Proved by what is ABSENT, not by the exit code: an unresolved SDK root is
    also exit 2, so the two runs are indistinguishable by `returncode` alone.
    With a resolvable `--sdk-root` the same argv reaches `new-som`'s metadata
    WRITES and `validate`'s skeleton output.

    The envelope channel is a SECOND thing this argv shape has to get right,
    separate from "did the command run" above (tan-cli#403 postmortem). The
    oracle's own `wants_json` (`main.rs`) is a textual scan of the WHOLE
    process argv, so it still notices the second, accepted `--format json`
    even though clap rejects the first `--format <rejected>` before ever
    resolving which occurrence "wins" -- measured, both cases here answer a
    JSON envelope on stdout, at the SAME exit code the assertions above
    already pin. `main()` used to scan the REWRITTEN argv instead, which
    `_reorder_global_flags` had already collapsed down to just the rejected
    `--format <value>` pair (dropping the second `--format json` along with
    everything else) -- so `json_mode` came back `False` here and the process
    fell into the text-mode branch, leaving stdout EMPTY for an argv the
    caller explicitly marked `--format json`. `data.message` is not
    byte-compared against the oracle's clap-rendered text (Typer/Click render
    their own usage errors, box-drawing included); the rejected value showing
    up there, and never the SDK-preflight message a real run would print, is
    what proves the command still did not run.
    """
    proc = _run_tan(argv, tmp_path)

    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert rejected in proc.stderr, proc.stderr
    assert "alp-sdk root is unresolved" not in proc.stderr, (
        "the command body ran on an argv the oracle refuses at parse time -- a "
        f"second `--format json` overrode the rejected '{rejected}'.\n{proc.stderr}"
    )
    assert proc.stdout.strip() != "", (
        "stdout must carry the envelope under `--format json`, even when the "
        f"SECOND `--format` is the one the oracle honours: {proc.stdout!r}"
    )
    env = json.loads(proc.stdout)
    assert env["command"] == "cli"
    assert env["exitCode"] == 2
    assert env["ok"] is False
    assert env["issues"] == [
        {"code": "cli.parse-error", "severity": "error", "message": env["data"]["message"]}
    ]
    assert rejected in env["data"]["message"], env["data"]["message"]
    assert "alp-sdk root is unresolved" not in env["data"]["message"], env["data"]["message"]


def test_a_rejected_format_value_does_not_strand_the_global_flags_ahead_of_it(tmp_path):
    """The other half of the residual: aborting the rewrite also left every
    global flag already scanned in the pre-subcommand position `root` does not
    declare, so `tan --verbose --format sarif validate` answered `No such
    option: --verbose` -- naming a flag this port fully supports, for an argv
    whose actual fault is the `--format` value. The oracle refuses the VALUE."""
    proc = _run_tan(["--verbose", "--format", "sarif", "validate"], tmp_path)

    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert "sarif" in proc.stderr, proc.stderr
    assert "--verbose" not in proc.stderr, (
        f"the refusal blamed a supported global flag instead of the value:\n{proc.stderr}"
    )


def _run_tan(argv: list[str], cwd) -> subprocess.CompletedProcess:
    """`python -m tan` out of process, so `main()`'s argv rewrite, Click's
    standalone dispatch and the real exit code are all the ones a caller
    gets."""
    return subprocess.run(
        [sys.executable, "-m", "tan", *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(PACKAGE_ROOT), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
            ),
        },
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--format", "bogus", "new-som", "--sku", "FOO"],
        ["new-som", "--format", "bogus", "--sku", "FOO"],
    ],
    ids=["pre-subcommand", "post-subcommand"],
)
def test_a_bad_format_value_stops_new_som_before_it_can_write(argv: list[str], tmp_path):
    """End to end, on the command where accepting an unvalidated `--format`
    costs the most: `new-som` CREATES metadata files. The oracle refuses
    `--format bogus` at parse in both positions and does no work; this port
    accepted it in both and ran on -- as far as the SDK-root preflight here,
    and as far as writing skeletons with a resolvable `--sdk-root`.

    The refusal is proved by what is ABSENT, not by the exit code: an
    unresolvable SDK root is ALSO exit 2, so a run that reached the preflight
    and one that never started are indistinguishable by `returncode` alone.
    The preflight's own message is the marker that the body ran.

    Both positions in one case list because they came from different defects
    -- the pre-subcommand one from tan-cli#378's relocation, the
    post-subcommand one older than it -- and one fix (the command reading its
    own `--format`, plus relocation declining a non-global value) has to hold
    for both.
    """
    proc = _run_tan(argv, tmp_path)

    assert proc.returncode == 2, proc.stderr
    assert "alp-sdk root is unresolved" not in proc.stderr, (
        "`new-som`'s body ran on an argv the oracle rejects at parse time; with "
        f"a resolvable --sdk-root this writes metadata files.\n{proc.stderr}"
    )
    assert "bogus" in proc.stderr, proc.stderr
    assert proc.stdout == "", f"a usage error must leave stdout empty: {proc.stdout!r}"


def test_the_derived_case_list_still_covers_the_whole_command_surface():
    """A parametrised gate that silently shrank to zero would report green
    while checking nothing -- and this file's whole premise is that the case
    list is never hand-maintained, so nobody would notice by reading it."""
    assert len(_COMMANDS) >= 30, (
        f"only {len(_COMMANDS)} commands registered on `tan.cli.app`; expected "
        "the full ~32-command surface. If one was intentionally removed, move "
        "this floor in the same change."
    )
