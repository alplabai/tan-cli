# SPDX-License-Identifier: Apache-2.0
"""The single place a command opts into the oracle's global-argument surface
(tan-cli#261).

The v0.4.1 oracle's clap `GlobalArgs` (`crates/tan-cli/src/cli.rs` lines
24-73) marks every field `#[arg(long, global = true, ...)]`: clap attaches
the WHOLE struct to every subcommand, so `tan <any-command> --verbose` parses
on the oracle even for a command whose own Rust handler never reads
`args.verbose`. Measured against this port (tan-cli#261, re-measured before
this file existed): 99 registration sites across 17 already-ported commands
raised Click's own "No such option" instead -- not because the flag did the
wrong thing, but because nobody had declared it there at all, an
eighteenth-command-repeats-the-mistake defect this module exists to remove
structurally rather than patch site by site.

`accept_global_flags` closes exactly that gap and nothing else: it adds a
`typer.Option` for whichever of `_GLOBAL_FLAG_SPECS` a command's own
signature does not ALREADY declare, detected by the CLI flag STRING itself
(each parameter's `typer.Option(...).param_decls`) rather than by Python
parameter name, which already varies command to command for the identical
flag (`all_cores` in `clean_cmd.clean`, `all_targets` in `build_cmd.build`,
both `--all`). A flag a command already reads for real keeps doing exactly
that -- this module never touches an existing parameter.

Every flag this module ADDS -- arity-0 or value-carrying alike -- is accepted
and then DROPPED before the wrapped command runs. That was always true for a
boolean (an accepted-and-ignored `--verbose` changes nothing a caller can
observe); tan-cli#398 tried to narrow it for a value-carrying flag instead,
refusing the flag outright the moment a value was actually supplied
(`typer.BadParameter`, exit 2), reasoning that dropping the value silently
serves the command's own default in its place -- measured harmful on exactly
one flag, `model`'s injected `--board-yaml` (see `model_cmd.py`'s own
`--board`, which now declares `--board-yaml` as a second spelling of the SAME
option instead, so it is never one of the flags this module injects for
`model` at all; `_declared_flags` below already skips anything a command
declares for real).

Measured against the oracle (tan-cli#403 postmortem), the refusal was wrong
everywhere else it fired: 17 other (command, flag) pairs -- `--board-yaml` on
`examples`/`explain`/`sdk`, `--target` on 14 more -- accept the SAME flag on
`target/debug/tan.exe` and ignore it (it is `global = true` there too, but
each command's own Rust handler simply never reads it), so refusing it here
was a NEW divergence: `tan doctor --target zephyr` went from matching the
oracle's exit 4 to a `typer.BadParameter` exit 2 with the WRONG command in the
JSON envelope (`main`'s generic `command: "cli"` fallback, since the refusal
happens before the wrapped command -- and therefore before `emit()` -- ever
runs). A command that genuinely computes a wrong answer from a dropped value
needs the SAME fix `model` got -- read the real flag, whatever its
oracle-parity alias is called -- not a blanket refusal that punishes every
command the oracle itself treats as accept-and-ignore.

This is the same "declared once, accepted everywhere, read only where a
command already reads it" shape `clean_cmd.clean` hand-wrote for six flags
before this module existed -- see its own comment there: "the shared fix
(one decorator for every command) belongs with whoever owns the global-flag
surface." This is that decorator, generalised and applied to the rest of the
surface.

`--format` is deliberately NOT one of `_GLOBAL_FLAG_SPECS`: unlike the
arity-0 flags here, a command that merely ACCEPTED `--format json` without
reading it would silently run in text mode for a caller who asked for JSON --
exit 0, human text on stderr, nothing on stdout, an envelope-less
`--format json` run.
`--format` is instead made global by RELOCATION (`RELOCATABLE_FLAG_ARITY`
below, tan-cli#378) -- every registered command already declares and READS its
own `--format`, so moving the token past the subcommand name hands it to a
parameter that acts on it rather than to one that drops it.
"""
from __future__ import annotations

import inspect
import typing
from collections.abc import Callable

import typer

from tan.core.inert import PARITY, inert_help

#: One entry per oracle `GlobalArgs` field this port must be able to PARSE on
#: every command (`crates/tan-cli/src/cli.rs:24-73`), as
#: `(flag, python_name, is_bool, metavar)`. `metavar` is `None` for a bool
#: flag (arity 0). `--project`/`--sdk-root` are included even though every
#: command touched by tan-cli#261 already declares them (measured) -- the
#: NEXT command to be added is exactly who this guards.
_GLOBAL_FLAG_SPECS: tuple[tuple[str, str, bool, str | None], ...] = (
    ("--project", "project", False, "PATH"),
    ("--board-yaml", "board_yaml", False, "PATH"),
    ("--sdk-root", "sdk_root", False, "PATH"),
    ("--target", "target", False, "EMIT"),
    ("--all", "all_", True, None),
    ("--verbose", "verbose", True, None),
    ("--quiet", "quiet", True, None),
    ("--no-color", "no_color", True, None),
    ("--non-interactive", "non_interactive", True, None),
    ("--ci", "ci", True, None),
)

#: `{flag: arity}` -- `cli.py`'s `_reorder_global_flags` reads this (arity 1
#: = takes a value, 0 = boolean) to relocate a leading global flag across the
#: subcommand boundary. Derived from `_GLOBAL_FLAG_SPECS` so the reorder
#: table and this module's own injection list cannot drift apart the way
#: tan-cli#261's own second comment flagged `_GLOBAL_FLAG_ARITY` could.
GLOBAL_FLAG_ARITY: dict[str, int] = {
    flag: (0 if is_bool else 1) for flag, _name, is_bool, _metavar in _GLOBAL_FLAG_SPECS
}

#: Every flag this module knows how to inject -- what the port-wide gate
#: (`tests/gates/test_global_flags_gate.py`) walks to assert the whole
#: registered command surface accepts it.
GLOBAL_FLAGS: tuple[str, ...] = tuple(flag for flag, *_rest in _GLOBAL_FLAG_SPECS)

#: What `cli.py`'s `_reorder_global_flags` actually relocates across the
#: subcommand boundary: the injectable set above PLUS `--format` (tan-cli#378).
#:
#: `--format` is `#[arg(long, global = true, ...)]` on the oracle exactly like
#: the ten above, so `tan --format json <anything>` parses there; the port used
#: to instead declare `--format` on the ROOT callback and hand-list which
#: commands were allowed to precede it, which shipped as global on 12 of 32
#: commands and a `cli.parse-error` (exit 2, `command: "cli"` -- the WRONG
#: command in the envelope) on the other 20. Relocating is what makes it
#: uniform with no list to forget: the flag lands on the command's own
#: `--format` parameter, the position every golden and every command's
#: validation already uses.
#:
#: Separate from `_GLOBAL_FLAG_SPECS` on purpose, NOT merged into it: that
#: tuple is the INJECTION list, and injecting `--format` would give a future
#: command that does not declare one an accepted-and-dropped `--format json`
#: -- the silent-text-mode defect the module docstring rules out. Keeping the
#: two apart also leaves `GLOBAL_FLAGS`/`GLOBAL_FLAG_ARITY` (and the gate that
#: walks them) meaning exactly what they meant before.
RELOCATABLE_FLAG_ARITY: dict[str, int] = {**GLOBAL_FLAG_ARITY, "--format": 1}

#: Shown nowhere (every injected option is `hidden=True`, matching the
#: precedent `clean_cmd.clean` already set for its six) -- kept as a real
#: string anyway so a `--help -v` or future un-hiding does not surface a bare
#: `None`.
_ACCEPTED_NOT_READ_HELP = inert_help(
    "Accepted for oracle parity; not read by this command.", PARITY, "tan-cli#261"
)


def _declared_flags(func: Callable[..., object]) -> set[str]:
    """Every CLI flag string `func` already declares, keyed by the flag
    string itself rather than by Python parameter name -- the same fact
    under a different name (`all_cores` vs `all_targets` for `--all`) must
    still count as "already declared", or this would hand a command a
    second, silently-ignored `--all` behind its own real one."""
    declared: set[str] = set()
    for param in inspect.signature(func).parameters.values():
        decls = getattr(param.default, "param_decls", None)
        if decls:
            declared.update(decls)
    return declared


def accept_global_flags(func: Callable[..., object]) -> Callable[..., object]:
    """Return a callable Typer can register whose declared options are
    `func`'s own plus whichever of `_GLOBAL_FLAG_SPECS` it was missing, each
    of the added ones accepted and then dropped before `func` ever runs.

    A command that already declares the full set is returned UNCHANGED
    (`func` itself, not a wrapper) -- the common case once every command in
    tan-cli#261 has been swept once.

    Call this right where the command function is defined, in its own
    `*_cmd.py` module (`validate = accept_global_flags(validate)`), not as a
    decorator on the `app.command(...)` call in `cli.py`: by the time
    `cli.py` runs, the click `Command` Typer builds from the function is
    already final, and wrapping there would scatter the fix away from the
    command it changes.
    """
    sig = inspect.signature(func)
    existing = _declared_flags(func)
    to_add = [spec for spec in _GLOBAL_FLAG_SPECS if spec[0] not in existing]
    if not to_add:
        return func

    # Resolve every EXISTING parameter's annotation to the real type object,
    # not whatever bare STRING `from __future__ import annotations` leaves it
    # as: `inspect.signature` alone never evaluates PEP 563 postponed
    # annotations, and every one of these 17 `*_cmd.py` modules has that
    # import at the top. Typer normally resolves the string itself via
    # `typing.get_type_hints(callback)`, using `callback.__globals__` --
    # `func`'s module, where `str`/`bool`/`typer.Context`/... are in scope.
    # Once `func` is wrapped below, Typer would instead resolve hints
    # against `wrapper.__globals__` -- THIS module's globals, not `func`'s --
    # and get nothing back for a name it cannot see; the fallback path then
    # feeds `get_click_type` the literal string `'str'` instead of the type
    # `str`, which fails with `RuntimeError: Type not yet supported: str`
    # (measured: `explain --target zephyr-board` after a first version of
    # this function skipped this step). Resolving here, once, against the
    # ORIGINAL `func` -- exactly what Typer would have done directly -- is
    # what keeps a wrapped command's pre-existing options working at all.
    resolved_hints = typing.get_type_hints(func)
    params = [
        param.replace(annotation=resolved_hints[name]) if name in resolved_hints else param
        for name, param in sig.parameters.items()
    ]

    #: Every injected flag's python param name -- arity-0 or value-carrying
    #: alike, all dropped before `func` runs (see the module docstring for why
    #: refusing a value-carrying one, tried in tan-cli#398, is not this
    #: module's job any more).
    dropped: list[str] = []
    for flag, name, is_bool, metavar in to_add:
        if is_bool:
            option = typer.Option(False, flag, hidden=True, help=_ACCEPTED_NOT_READ_HELP)
            annotation: type = bool
        else:
            option = typer.Option(
                None, flag, metavar=metavar, hidden=True, help=_ACCEPTED_NOT_READ_HELP
            )
            annotation = str
        dropped.append(name)
        params.append(
            inspect.Parameter(
                name, inspect.Parameter.KEYWORD_ONLY, default=option, annotation=annotation
            )
        )

    def wrapper(*args: object, **kwargs: object) -> object:
        for name in dropped:
            kwargs.pop(name, None)
        return func(*args, **kwargs)

    wrapper.__doc__ = func.__doc__
    wrapper.__name__ = getattr(func, "__name__", "wrapper")
    return_annotation = resolved_hints.get("return", sig.return_annotation)
    wrapper.__signature__ = inspect.Signature(params, return_annotation=return_annotation)
    return wrapper
