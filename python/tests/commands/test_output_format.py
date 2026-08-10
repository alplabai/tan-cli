# SPDX-License-Identifier: Apache-2.0
"""`--format` is ONE declaration, cross-checked on all three surfaces
(tan-cli#403).

Before this module, `--format` had no type anywhere in the port: every command
declared it `str` and re-validated the value by hand in its own body, 29
byte-identical copies of

    if output_format not in ("text", "json"):
        raise typer.BadParameter(...)

plus a 30th over a wider domain in `validate_cmd.py` and a 31st, spelled
differently, in `cli.py`'s root callback. Three consumer-facing surfaces
answer "what does `--format` take?" -- the parser, `--help`, and the shell
completion scripts `tan completion --shell <bash|zsh|fish>` emits -- and each
one carried its own hand-written copy of the answer. They had already drifted:
`validate` accepted `diagnostic-v1` and `sarif`, while both `--help` prose and
all three completion scripts named only `text json`, so `tan validate --format
<TAB>` actively taught an IDE integrator that the two formats their editor
wants do not exist.

The oracle had none of this: clap derives parser, help and completions from
one `ValueEnum`, and the committed oracle help in
`tests/fixtures/oracle_captures/test_run_oracle_parity.json` still shows the
choice list this port dropped.

`test_run_oracle_parity.py` could not have caught any of it -- it extracted
flag NAMES with `re.findall(r"--[a-zA-Z][a-zA-Z0-9-]*", help_text)` and
compared sets, so a missing or wrong VALUE list was structurally invisible to
it; tan-cli#269 has since deleted that module along with the rest of the
oracle-parity suite, which leaves this file the ONLY thing measuring the port
against the recorded choice list. Hence this module: for every registered
command, the three surfaces are
parsed independently (Click's live parameter type, the rendered `--help`, the
emitted shell scripts) and asserted equal. Adding a fourth format, or a second
command with a wider domain, is a one-line enum edit -- and if any surface
fails to follow, these tests are what says so.
"""

from __future__ import annotations

import json
import re

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from tan.cli import app
from tan.commands.completion_cmd import BASH_SCRIPT, FISH_SCRIPT, ZSH_SCRIPT
from tan.output_format import (
    WIDE_FORMAT_COMMANDS,
    OutputFormat,
    ValidateOutputFormat,
    declared_formats,
    format_values,
    resolve_format,
)
from tests import oracle_captures

runner = CliRunner()

#: The frozen oracle `--help` captures. Read (never written) through
#: `tests.oracle_captures`, the one module that knows where the store lives,
#: so the choice list this port renders is checked against the Rust CLI's own
#: and not against a second hand-copy of it living here. The store outlived
#: the binary (tan-cli#269): a recorded `--help` is still exactly what the
#: shipped v0.4.1 CLI printed.
_ORACLE_HELP_FIXTURE = oracle_captures.CAPTURES_DIR / "test_run_oracle_parity.json"


# ---------------------------------------------------------------------------
# Surface 1 -- the live Click tree (what the parser actually accepts)
# ---------------------------------------------------------------------------
def _registered_commands() -> dict[str, object]:
    """`{subcommand name: live Click command}` for every command `tan`
    registers.

    Duck-typed on `.commands`/`.params`, never `isinstance(x, click.Group)`:
    Typer builds its tree out of its own VENDORED click (`typer._click`), so an
    `isinstance` test against the top-level `click` package matches nothing --
    the same trap `cli._tokens_consumed_after` documents.
    """
    found: dict[str, object] = {}

    def walk(command: object, path: tuple[str, ...]) -> None:
        children = getattr(command, "commands", None)
        if children:
            for name, child in children.items():
                walk(child, (*path, name))
        elif path:
            found[" ".join(path)] = command

    walk(get_command(app), ())
    return found


def _accepted_values(command: object) -> frozenset[str]:
    """The value list `--format`'s declared TYPE enforces, or empty if the
    parameter is an untyped `str` that validates nothing at parse time."""
    for param in getattr(command, "params", ()):
        if "--format" not in getattr(param, "opts", ()):
            continue
        return frozenset(getattr(param.type, "choices", ()) or ())
    return frozenset()


# ---------------------------------------------------------------------------
# Surface 2 -- the rendered `--help`
# ---------------------------------------------------------------------------
#: Click renders a choice list as the option's metavar -- `[text|json]` on a
#: plain console, `<text|json>` through Typer's Rich formatter. Either bracket
#: is accepted so this test pins the VALUES, not the renderer's punctuation.
_HELP_CHOICES_RE = re.compile(r"--format\s+[<\[]([^>\]]+)[>\]]")


def _help_values(name: str) -> frozenset[str]:
    """The value list `tan <name> --help` prints for `--format`.

    Width and colour are pinned in `tests/conftest.py`
    (`_TYPER_FORCE_DISABLE_TERMINAL` / `TERMINAL_WIDTH=200`), so the option
    line is plain text and does not fold mid-metavar.
    """
    result = runner.invoke(app, [*name.split(" "), "--help"])
    assert result.exit_code == 0, f"{name} --help exited {result.exit_code}"
    match = _HELP_CHOICES_RE.search(result.output)
    if match is None:
        return frozenset()
    return frozenset(value.strip() for value in match.group(1).split("|"))


# ---------------------------------------------------------------------------
# Surface 3 -- the emitted completion scripts
# ---------------------------------------------------------------------------
#: bash: one `--format` branch with a default and a `case` of per-command
#: overrides, each arm an alternation of subcommand names (`a|b`).
_BASH_DEFAULT_RE = re.compile(r'local formats="([^"]*)"')
_BASH_OVERRIDE_RE = re.compile(r'^\s*([a-z0-9|-]+)\)\s*formats="([^"]*)"', re.MULTILINE)
#: zsh: `global_args` gains its `--format` spec from a `case` on the command.
_ZSH_RE = re.compile(r"(\S+)\)\s*global_args\+=\('--format\[[^\]]*\]:format:\(([^)]*)\)'\)")
#: fish: one `complete` line per domain, selected by `__fish_seen_subcommand_from`.
_FISH_RE = re.compile(r"complete -c tan -n '([^']*)' -l format[^\n]*-a '([^']*)'")


def _expand_arms(pairs: list[tuple[str, str]]) -> dict[str, str]:
    """`[("a|b", "text json")]` -> `{"a": "text json", "b": "text json"}`."""
    return {command: values for arm, values in pairs for command in arm.split("|")}


def _bash_values(name: str) -> frozenset[str]:
    default = _BASH_DEFAULT_RE.search(BASH_SCRIPT)
    if default is None:
        return frozenset()
    overrides = _expand_arms(_BASH_OVERRIDE_RE.findall(BASH_SCRIPT))
    return frozenset(overrides.get(name, default.group(1)).split())


def _zsh_values(name: str) -> frozenset[str]:
    arms = _expand_arms(_ZSH_RE.findall(ZSH_SCRIPT))
    if not arms:
        return frozenset()
    return frozenset(arms.get(name, arms.get("*", "")).split())


def _fish_values(name: str) -> frozenset[str]:
    default = frozenset()
    named: dict[str, frozenset[str]] = {}
    for condition, values in _FISH_RE.findall(FISH_SCRIPT):
        parsed = frozenset(values.split())
        seen = re.search(r"__fish_seen_subcommand_from ([a-z0-9 -]+)", condition)
        if seen is None or condition.startswith("not "):
            default = parsed
            continue
        for command in seen.group(1).split():
            named[command] = parsed
    return named.get(name, default)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(_registered_commands()))
def test_every_surface_offers_exactly_the_values_the_parser_accepts(name):
    """The parser, `--help` and all three completion scripts agree, per command.

    This is the test the issue asks for: it fails on the tree as it was, where
    `--format` is a bare `str` (no parse-time value list at all), and it keeps
    failing for any future command that declares a wider or narrower domain on
    one surface and forgets the other three.
    """
    command = _registered_commands()[name]
    accepted = _accepted_values(command)
    assert accepted, f"{name}: --format declares no value list (an untyped str accepts anything)"
    assert _help_values(name) == accepted, f"{name}: --help disagrees with the parser"
    assert _bash_values(name) == accepted, f"{name}: bash completion disagrees with the parser"
    assert _zsh_values(name) == accepted, f"{name}: zsh completion disagrees with the parser"
    assert _fish_values(name) == accepted, f"{name}: fish completion disagrees with the parser"


def test_wide_format_commands_is_what_the_command_tree_really_declares():
    """`completion_cmd` emits text and so cannot walk the live Click tree
    (importing `tan.cli` from a command module is an import cycle); it splices
    `WIDE_FORMAT_COMMANDS` into the scripts instead. That tuple is therefore
    the one hand-written list left in this surface, and this is what stops it
    going stale the day a second command gains the wider domain."""
    from_tree = {
        name
        for name, command in _registered_commands().items()
        if _accepted_values(command) != frozenset(format_values(OutputFormat))
    }
    assert from_tree == set(WIDE_FORMAT_COMMANDS)


def test_validate_is_the_command_with_the_wider_domain():
    """`validate` really does emit two IDE-oriented documents nothing else
    does, so the four-value domain is a fact about the CLI, not a typo the
    test above would happily freeze if every surface agreed on the wrong set.
    """
    assert _accepted_values(_registered_commands()["validate"]) == frozenset(
        {"text", "json", "diagnostic-v1", "sarif"}
    )
    assert _accepted_values(_registered_commands()["build"]) == frozenset({"text", "json"})


# ---------------------------------------------------------------------------
# The oracle's own choice list
# ---------------------------------------------------------------------------
#: clap prints `--format <FORMAT>` followed by an indented `Possible values:`
#: block, one `- <value>: <description>` line each.
_ORACLE_BLOCK_RE = re.compile(
    r"--format <FORMAT>\n.*?Possible values:\n((?:\s*- [^\n]*\n)+)", re.DOTALL
)
_ORACLE_VALUE_RE = re.compile(r"-\s+([a-z][a-z0-9-]*):")


def test_help_choice_list_matches_the_frozen_oracle_help():
    """The port renders the same `--format` values the Rust oracle printed.

    The captures in `test_run_oracle_parity.json` are `tan <command> --help`
    from the reference binary; every one of them lists exactly `text` and
    `json` under `Possible values:`. Rendering differs (clap prints a
    described list, Click a `[text|json]` metavar) -- the VALUES are what
    this asserts, because the values are what a caller types.
    """
    captures = json.loads(_ORACLE_HELP_FIXTURE.read_text(encoding="utf-8"))
    # The file freezes more than help text -- envelopes are recorded as
    # `[exit code, {...}]` pairs under their own keys. Only the help captures
    # are strings, and only they carry a `Possible values:` block.
    blocks = [
        frozenset(_ORACLE_VALUE_RE.findall(block))
        for text in captures.values()
        if isinstance(text, str)
        for block in _ORACLE_BLOCK_RE.findall(text)
    ]
    assert blocks, "no `--format` choice list found in the frozen oracle help"
    for oracle_values in blocks:
        assert oracle_values == _help_values("build")


# ---------------------------------------------------------------------------
# One refusal, either flag position
# ---------------------------------------------------------------------------
_REFUSAL = "'bogus' is not one of 'text', 'json'"


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["build", "--format", "bogus"], id="after-the-subcommand"),
        pytest.param(["--format", "bogus", "build"], id="before-the-subcommand"),
    ],
)
def test_an_invalid_value_is_refused_the_same_way_in_both_positions(argv):
    """The pre-subcommand position is the one alp-sdk-vscode actually uses
    (`alpCli/vscodeAdapter.ts`'s `withSdkRoot`), so a caller who moves the flag
    must not get a differently-worded rejection for the identical mistake. Exit
    code 2 either way -- Click's usage-error code, which `cli.main` folds into
    the registered `cli.parse-error` envelope under `--format json`.
    """
    result = runner.invoke(app, argv)
    assert result.exit_code == 2
    assert _REFUSAL in result.output


# ---------------------------------------------------------------------------
# `resolve_format` -- the precedence the eleven leading-position commands share
# ---------------------------------------------------------------------------
def test_a_commands_own_format_beats_the_leading_one():
    assert (
        resolve_format(OutputFormat.TEXT, {"format": "json"}, choices=OutputFormat)
        is OutputFormat.TEXT
    )


def test_the_leading_format_is_inherited_when_the_command_declares_none():
    assert resolve_format(None, {"format": "json"}, choices=OutputFormat) is OutputFormat.JSON


@pytest.mark.parametrize("ctx_obj", [None, {}, {"format": None}], ids=["none", "empty", "unset"])
def test_text_is_the_default_when_neither_position_carries_a_value(ctx_obj):
    assert resolve_format(None, ctx_obj, choices=OutputFormat) is OutputFormat.TEXT


def test_an_inherited_value_outside_this_commands_domain_is_refused():
    """The inherited value did NOT come through this command's parser, so
    nothing has checked it against THIS domain. Passing it through would hand
    the body a format it has no branch for: exit 0, empty stdout, and a
    caller who asked for a document that never got written.

    Reachable the moment `cli.py`'s root callback stops hardcoding
    `('text', 'json')` and starts accepting whatever the invoked subcommand
    declares (tan-cli#403) -- `tan --format sarif build` is exactly this case.
    """
    with pytest.raises(Exception) as excinfo:
        resolve_format(None, {"format": "sarif"}, choices=OutputFormat)
    assert "'sarif' is not one of 'text', 'json'." in str(excinfo.value)


def test_the_wider_domain_accepts_what_the_narrow_one_refuses():
    assert (
        resolve_format(None, {"format": "sarif"}, choices=ValidateOutputFormat)
        is ValidateOutputFormat.SARIF
    )


def test_declared_formats_reads_the_value_list_off_the_live_command():
    """What lets `cli.py`'s root callback accept whatever the invoked
    subcommand declares instead of a second frozen pair."""
    assert declared_formats(_registered_commands()["validate"]) == (
        "text",
        "json",
        "diagnostic-v1",
        "sarif",
    )
    assert declared_formats(_registered_commands()["build"]) == ("text", "json")
    assert declared_formats(object()) == ()


def test_a_format_member_stringifies_to_its_wire_value():
    """`str()`/`f""` on a bare `(str, Enum)` member renders
    `OutputFormat.JSON`, and a member that stringifies to its Python name is
    how a wrong value reaches a subprocess argv or a JSON payload unnoticed."""
    assert f"{OutputFormat.JSON}" == "json"
    assert str(ValidateOutputFormat.DIAGNOSTIC_V1) == "diagnostic-v1"
    assert json.dumps({"format": OutputFormat.JSON}) == '{"format": "json"}'
