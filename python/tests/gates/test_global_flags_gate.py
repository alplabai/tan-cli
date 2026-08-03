# SPDX-License-Identifier: Apache-2.0
"""tan-cli#261: every registered command must PARSE the oracle's global
argument surface (`tan.core.global_flags.GLOBAL_FLAGS`), even where a
command's own logic never reads a given flag's value.

This is the gate the issue itself asked for, verbatim: "the right shape is a
shared registration mechanism so a global flag is declared once and applies
everywhere... write the gate that keeps it true: a test that enumerates the
command surface and asserts every command accepts the global set." Before
`tan.core.global_flags.accept_global_flags` existed, 99 (flag, command) pairs
across 17 commands raised Click's own "No such option" -- measured against
the v0.4.1 oracle, which accepts every one of them (`crates/tan-cli/src/
cli.rs`'s `GlobalArgs`, `#[arg(long, global = true, ...)]`). This test is
what stops an eighteenth command from being added the same way: a NEW command
missing a flag here fails THIS test, not a fresh re-measurement someone has
to remember to run.

Probed with a trailing `--help` on every invocation, never a bare run: a
command that would otherwise dial a real Zephyr build, spawn `west`, or write
a project file must never run for real just because this gate exercised its
flag surface. `--help` is Click's own EAGER short-circuit, but -- measured,
both directions -- it does not paper over a genuinely unrecognised option:
`tan validate --bogus --help` and `tan validate --help --bogus` both still
report "No such option: --bogus" rather than silently printing help, so a
trailing `--help` is not a weaker probe than a bare invocation would be, only
a SAFE one.
"""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from tan.cli import _SUBCOMMAND_NAMES, app
from tan.core.global_flags import GLOBAL_FLAG_ARITY, GLOBAL_FLAGS

runner = CliRunner()

#: Case-insensitive substrings Click's own usage-error rendering uses for "you
#: gave me a flag I do not know" -- the SAME set `cli.py`'s own docstrings and
#: this port's oracle-comparison scripts key off. `--help` never emits any of
#: these on its own, so a match unambiguously means the FLAG was rejected, not
#: that help text happened to mention the word.
_REJECTION_MARKERS = ("no such option", "unexpected argument", "unrecognized argument")


def _rejected(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in _REJECTION_MARKERS)


def _probe_argv(command: str, flag: str) -> list[str]:
    argv = [command, flag]
    if GLOBAL_FLAG_ARITY[flag] == 1:
        argv.append("dummy-value")
    argv.append("--help")
    return argv


@pytest.mark.parametrize("command", sorted(_SUBCOMMAND_NAMES))
@pytest.mark.parametrize("flag", GLOBAL_FLAGS)
def test_every_registered_command_accepts_every_global_flag(command: str, flag: str):
    argv = _probe_argv(command, flag)
    result = runner.invoke(app, argv)
    assert not _rejected(result.output), (
        f"`tan {command} {flag}` is rejected as an unrecognised option:\n"
        f"{result.output}\n"
        "Every command tan registers must parse the oracle's global argument "
        "surface (tan-cli#261) -- add the missing flag by calling "
        f"`{command} = accept_global_flags({command})` at the end of its "
        "*_cmd.py module (tan.core.global_flags), not by hand-declaring it."
    )


def test_global_flags_gate_actually_covers_the_full_registered_surface():
    """A parametrised gate that silently shrank to zero cases would report
    green while checking nothing -- this is the canary for exactly that."""
    assert len(_SUBCOMMAND_NAMES) >= 30, (
        f"only {len(_SUBCOMMAND_NAMES)} commands registered; expected the "
        "full ~32-command surface (tan.cli._SUBCOMMAND_NAMES). If a command "
        "was intentionally removed, update this floor in the same change."
    )
    assert len(GLOBAL_FLAGS) == len(GLOBAL_FLAG_ARITY) >= 10, (
        "tan.core.global_flags.GLOBAL_FLAGS / GLOBAL_FLAG_ARITY shrank below "
        "the oracle's known GlobalArgs field count -- see cli.rs:24-73."
    )
