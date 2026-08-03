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


def test_injects_every_missing_flag_and_drops_them_before_the_command_runs():
    seen: dict[str, object] = {}

    def probe() -> None:
        seen["ran"] = True

    wrapped = accept_global_flags(probe)
    app = _make_app(wrapped)

    argv = ["probe"]
    for flag in GLOBAL_FLAGS:
        argv.append(flag)
        if GLOBAL_FLAG_ARITY[flag] == 1:
            argv.append("some-value")

    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert seen.get("ran") is True


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
