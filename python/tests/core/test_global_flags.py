# SPDX-License-Identifier: Apache-2.0
"""Unit tests for `tan.core.global_flags.accept_global_flags` -- the
tan-cli#261 mechanism. The port-wide behavioural gate lives at
`tests/gates/test_global_flags_gate.py`; these test the MECHANISM itself in
isolation, including the PEP 563 regression it was built to survive
(`explain --target zephyr-board` crashing with `RuntimeError: Type not yet
supported: str` the first time this function wrapped a module with `from
__future__ import annotations`, measured before the fix below existed).
"""
from __future__ import annotations

import pytest
import typer
from typer.main import get_command
from typer.testing import CliRunner

from tan.core.global_flags import GLOBAL_FLAG_ARITY, GLOBAL_FLAGS, accept_global_flags

runner = CliRunner()


def _make_app(command_func) -> typer.Typer:
    """A real two-command Typer app -- Typer collapses a SINGLE registered
    command straight to a bare `click.Command` (no group, no subcommand
    name), which is not the shape any real tan command runs under; a second,
    unrelated command keeps this a `Group` the way `tan.cli.app`'s real 32
    commands do."""
    app = typer.Typer(add_completion=False)
    app.command("probe")(command_func)

    def _other() -> None:
        typer.echo("other")

    app.command("other")(_other)
    return app


def test_global_flags_and_arity_stay_in_lockstep():
    assert set(GLOBAL_FLAGS) == set(GLOBAL_FLAG_ARITY)
    for flag in GLOBAL_FLAGS:
        assert GLOBAL_FLAG_ARITY[flag] in (0, 1)


def test_injects_every_missing_boolean_flag_and_drops_it_before_the_command_runs():
    """The arity-0 half: an injected `--verbose`/`--ci`/... is accepted and
    dropped, and the command runs."""
    seen: dict[str, object] = {}

    def probe() -> None:
        seen["ran"] = True

    wrapped = accept_global_flags(probe)
    app = _make_app(wrapped)

    argv = ["probe", *(flag for flag in GLOBAL_FLAGS if GLOBAL_FLAG_ARITY[flag] == 0)]

    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert seen.get("ran") is True


@pytest.mark.parametrize(
    "flag", [flag for flag in GLOBAL_FLAGS if GLOBAL_FLAG_ARITY[flag] == 1]
)
def test_an_injected_value_carrying_flag_is_accepted_and_dropped_too(flag):
    """tan-cli#398 tried refusing an injected value-carrying flag the moment a
    value was supplied, reasoning that silently dropping it serves the
    command's own default in its place. Measured against the oracle
    (tan-cli#403 postmortem), that refusal was wrong for every one of these
    flags except `model`'s `--board-yaml` -- `target/debug/tan.exe` itself
    ACCEPTS AND IGNORES a value-carrying `GlobalArgs` field a given command's
    own handler never reads, so refusing it here was a NEW divergence, not a
    fix: `tan doctor --target zephyr` went from the oracle's exit 4 to a
    `typer.BadParameter` exit 2, with the wrong command (`cli`, from `main`'s
    generic usage-error fallback) in the JSON envelope, on 17 of the 18
    (command, flag) pairs this mechanism used to refuse.

    A command that genuinely computes a wrong answer from a dropped value
    (only `model`+`--board-yaml`, measured) is fixed by reading the real flag
    under its own oracle-parity spelling -- `model_cmd.py`'s `--board` now
    declares `--board-yaml` as a second name for itself, so it is no longer
    one of the flags this module injects for `model` at all
    (`test_a_value_carrying_flag_a_command_really_implements_is_untouched`,
    below, covers exactly that shape). This module's job for every OTHER
    command stays what it always was for a boolean: accept, drop, run."""
    ran: list[str | None] = []

    def probe(existing: str | None = None) -> None:
        ran.append(existing)

    app = _make_app(accept_global_flags(probe))

    result = runner.invoke(app, ["probe", flag, "some/path"])
    assert result.exit_code == 0, result.output
    assert ran == [None], (
        f"{flag}=some/path must be dropped before the command runs, not refused: "
        f"{result.output}"
    )

    # And the flag stays free when not supplied at all.
    result = runner.invoke(app, ["probe"])
    assert result.exit_code == 0, result.output
    assert ran == [None, None]


def test_a_value_carrying_flag_a_command_really_implements_is_untouched():
    """The refusal above is about INJECTED flags only. A command that declares
    `--board-yaml` itself keeps reading it -- `accept_global_flags` never
    touches an existing parameter, and 28 of the 32 registered commands
    implement this one for real."""
    seen: list[str | None] = []

    def probe(board_yaml: str = typer.Option(None, "--board-yaml")) -> None:
        seen.append(board_yaml)

    app = _make_app(accept_global_flags(probe))

    result = runner.invoke(app, ["probe", "--board-yaml", "real/board.yaml"])
    assert result.exit_code == 0, result.output
    assert seen == ["real/board.yaml"]


def test_a_flag_already_declared_under_a_different_python_name_is_not_duplicated():
    """`--all` under the python name `all_cores` (the real name `clean_cmd`
    uses) must be recognised as ALREADY covering `--all` -- detected by the
    CLI flag string itself, not by the Python parameter name, which varies
    command to command for the identical flag."""
    calls: list[bool] = []

    def probe(
        all_cores: bool = typer.Option(False, "--all", help="the command's own --all"),
    ) -> None:
        calls.append(all_cores)

    wrapped = accept_global_flags(probe)
    app = _make_app(wrapped)

    result = runner.invoke(app, ["probe", "--all"])
    assert result.exit_code == 0, result.output
    assert calls == [True], "the command's OWN --all must still be the one that ran"

    # And the pre-existing flag was not silently swallowed by a second,
    # injected `--all` shadowing it: passing it exactly once still reaches
    # the real parameter with the real value.
    result = runner.invoke(app, ["probe"])
    assert result.exit_code == 0, result.output
    assert calls[-1] is False


def test_a_command_declaring_the_full_set_is_returned_unchanged():
    def probe(
        project: str = typer.Option(None, "--project"),
        board_yaml: str = typer.Option(None, "--board-yaml"),
        sdk_root: str = typer.Option(None, "--sdk-root"),
        target: str = typer.Option(None, "--target"),
        all_: bool = typer.Option(False, "--all"),
        verbose: bool = typer.Option(False, "--verbose"),
        quiet: bool = typer.Option(False, "--quiet"),
        no_color: bool = typer.Option(False, "--no-color"),
        non_interactive: bool = typer.Option(False, "--non-interactive"),
        ci: bool = typer.Option(False, "--ci"),
    ) -> None:
        pass

    assert accept_global_flags(probe) is probe


def test_pep563_stringised_annotations_on_the_original_parameters_still_resolve():
    """The tan-cli#261 regression: every real `*_cmd.py` module has `from
    __future__ import annotations` at the top, which makes
    `inspect.signature(func).parameters[name].annotation` a bare STRING
    (`'str'`), not the type `str`. A first version of `accept_global_flags`
    copied those Parameter objects verbatim into the wrapper's `__signature__`
    and Typer could no longer resolve them (it looks in the WRAPPER's
    `__globals__`, not the original function's) -- `RuntimeError: Type not
    yet supported: str` on every command that had even one PRE-EXISTING
    string-typed option, discovered via `explain --target zephyr-board`.
    This module reproduces that shape directly (its own
    `from __future__ import annotations`, at the top) rather than special-
    casing it away."""

    seen: dict[str, object] = {}

    def probe(
        existing: str = typer.Option(None, "--existing", metavar="TEXT"),
    ) -> None:
        seen["existing"] = existing

    wrapped = accept_global_flags(probe)
    app = _make_app(wrapped)

    # Building the click Command at all is the crash site -- unwrapped, this
    # raises `RuntimeError: Type not yet supported: str` if the annotation
    # was left as the literal string instead of being resolved.
    get_command(app)

    result = runner.invoke(app, ["probe", "--existing", "hello", "--verbose"])
    assert result.exit_code == 0, result.output
    assert seen["existing"] == "hello"
