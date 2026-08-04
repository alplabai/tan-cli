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
that -- this module never touches an existing parameter. A flag added here is
accepted and then DROPPED before the wrapped command ever runs, so a
command's own body is never handed a value it was not already written to
expect: accepting is not the same as reading, and this module only ever does
the former for a flag it adds.

This is the same "declared once, accepted everywhere, read only where a
command already reads it" shape `clean_cmd.clean` hand-wrote for six flags
before this module existed -- see its own comment there: "the shared fix
(one decorator for every command) belongs with whoever owns the global-flag
surface." This is that decorator, generalised and applied to the rest of the
surface.

`--format` is deliberately NOT one of the specs below, matching `cli.py`'s own
`_HONOURS_ROOT_FORMAT`: a command that merely ACCEPTED `--format json`
without reading it would silently run in text mode for a caller who asked for
JSON -- exactly the defect `_HONOURS_ROOT_FORMAT` exists to prevent by
refusing until a command is actually taught to read `ctx.obj["format"]`.

**Six of the ten flags below have no such failure mode; FOUR do**
(tan-cli#398, correcting what this paragraph asserted before it): an
accepted-and-ignored `--verbose` changes nothing about which channel a caller
reads, so dropping it is safe -- but `--project`, `--board-yaml`,
`--sdk-root` and `--target` each carry a VALUE, and dropping one does not
lose a no-op, it substitutes the command's own default for the file or target
the caller named. Measured on this port before the split: `tan model build
--board-yaml ../other/board.yaml` packaged `./board.yaml`'s `som.sku`
(`E1M-AEN801` where the named file said `E1M-V2N101`) at `exitCode: 0` with
`issues: []`, so `build_model` compiled `.alpmodel` artefacts for the wrong
silicon and nothing refused or warned. The leading form
(`tan --board-yaml <path> model build`) was swallowed identically --
`cli.py`'s `_reorder_global_flags` relocates the flag across the subcommand
boundary and straight into the drop.

So the specs are split (`_VALUE_FLAG_SPECS` / `_BOOL_FLAG_SPECS`): the
arity-0 half is still accepted-and-dropped, and an INJECTED value-carrying
flag that a caller actually supplies is REFUSED -- a Click usage error (exit
2), the same treatment `--format` gets from `_HONOURS_ROOT_FORMAT`, which
`cli.main` folds into the registered `cli.parse-error` envelope under
`--format json`. A flag a command declares for real is untouched either way;
this only ever refuses one it was about to ignore.
"""
from __future__ import annotations

import inspect
import typing
from collections.abc import Callable

import typer

#: The VALUE-carrying oracle `GlobalArgs` fields (`crates/tan-cli/src/
#: cli.rs:24-73`), as `(flag, python_name, metavar)`. Kept apart from the
#: arity-0 half below because the two need opposite handling once INJECTED:
#: one of these accepted-and-dropped is a wrong answer, not a lost no-op
#: (tan-cli#398 -- see the module docstring for the measured `tan model build
#: --board-yaml` run). `--project`/`--sdk-root` are inert on no command today
#: (measured across all 32 registrations); they are listed because the NEXT
#: command to be added is exactly who this guards.
_VALUE_FLAG_SPECS: tuple[tuple[str, str, str], ...] = (
    ("--project", "project", "PATH"),
    ("--board-yaml", "board_yaml", "PATH"),
    ("--sdk-root", "sdk_root", "PATH"),
    ("--target", "target", "EMIT"),
)

#: The arity-0 oracle `GlobalArgs` fields, as `(flag, python_name)`. Safe to
#: accept and drop: a caller cannot read back whether an ignored `--verbose`
#: was honoured, so nothing downstream can be wrong about it.
_BOOL_FLAG_SPECS: tuple[tuple[str, str], ...] = (
    ("--all", "all_"),
    ("--verbose", "verbose"),
    ("--quiet", "quiet"),
    ("--no-color", "no_color"),
    ("--non-interactive", "non_interactive"),
    ("--ci", "ci"),
)

#: The two halves rejoined, in the oracle's own field order, as
#: `(flag, python_name, is_bool, metavar)` -- `metavar` is `None` for a bool
#: flag. Derived, never hand-listed a third time: the injection loop, the
#: arity table and the gate's flag list must all be the SAME fact.
_GLOBAL_FLAG_SPECS: tuple[tuple[str, str, bool, str | None], ...] = (
    *((flag, name, False, metavar) for flag, name, metavar in _VALUE_FLAG_SPECS),
    *((flag, name, True, None) for flag, name in _BOOL_FLAG_SPECS),
)

#: The value-carrying flags alone -- what a caller (the port-wide gate at
#: `tests/gates/test_global_flags_gate.py`, in particular) needs to know
#: WHICH flags must be honoured-or-refused rather than merely parsed.
VALUE_GLOBAL_FLAGS: tuple[str, ...] = tuple(flag for flag, *_rest in _VALUE_FLAG_SPECS)

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

#: Shown nowhere (every injected option is `hidden=True`, matching the
#: precedent `clean_cmd.clean` already set for its six) -- kept as a real
#: string anyway so a `--help -v` or future un-hiding does not surface a bare
#: `None`.
_ACCEPTED_NOT_READ_HELP = "Accepted for oracle parity (tan-cli#261); not read by this command."

#: The same, for an injected VALUE-carrying flag, which is parsed for oracle
#: parity and then refused rather than dropped (tan-cli#398).
_ACCEPTED_THEN_REFUSED_HELP = (
    "Parsed for oracle parity (tan-cli#261); refused by this command, "
    "which does not implement it (tan-cli#398)."
)

#: What a caller sees when they pass an injected value-carrying flag. Names
#: the remedy, not just the refusal: the flag is real everywhere else on the
#: surface, so "no such option" would be a lie and silence was the bug.
_REFUSAL = (
    "accepted for oracle parity (tan-cli#261) but not implemented by this "
    "command. Honouring it is impossible here and dropping it would silently "
    "substitute this command's own default for the value you named "
    "(tan-cli#398) -- pass the flag this command really reads, or drop this one."
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
    `func`'s own plus whichever of `_GLOBAL_FLAG_SPECS` it was missing.

    An added ARITY-0 flag is accepted and then dropped before `func` ever
    runs. An added VALUE-carrying flag is accepted, and then refused with a
    Click usage error the moment a caller actually supplies one -- never
    dropped (tan-cli#398; see the module docstring for what dropping cost).
    `func` does not run at all in that case: half-running on the wrong inputs
    IS the defect.

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

    injected: list[str] = []
    #: `(python_name, flag)` for the injected VALUE-carrying flags only --
    #: what `wrapper` checks before it drops anything.
    injected_values: list[tuple[str, str]] = []
    for flag, name, is_bool, metavar in to_add:
        if is_bool:
            option = typer.Option(False, flag, hidden=True, help=_ACCEPTED_NOT_READ_HELP)
            annotation: type = bool
        else:
            option = typer.Option(
                None, flag, metavar=metavar, hidden=True, help=_ACCEPTED_THEN_REFUSED_HELP
            )
            annotation = str
            injected_values.append((name, flag))
        params.append(
            inspect.Parameter(
                name, inspect.Parameter.KEYWORD_ONLY, default=option, annotation=annotation
            )
        )
        injected.append(name)

    def wrapper(*args: object, **kwargs: object) -> object:
        # `is not None` is the whole test: every injected value-carrying
        # option defaults to `None`, so anything else came off the command
        # line. Refused BEFORE the drop, and before `func` is called at all.
        supplied = [flag for name, flag in injected_values if kwargs.get(name) is not None]
        if supplied:
            # `typer.BadParameter` -> Click's own usage error, exit 2, which
            # `cli.main` turns into the registered `cli.parse-error` envelope
            # under `--format json`. Deliberately NOT a bespoke envelope here:
            # this module knows nothing about a given command's `data` payload
            # or its project/sdk blocks, and inventing a second refusal shape
            # for the one case `_HONOURS_ROOT_FORMAT` already has an answer for
            # is how two divergent contracts start.
            raise typer.BadParameter(_REFUSAL, param_hint=supplied[0])
        forwarded = {key: value for key, value in kwargs.items() if key not in injected}
        return func(*args, **forwarded)

    wrapper.__doc__ = func.__doc__
    wrapper.__name__ = getattr(func, "__name__", "wrapper")
    return_annotation = resolved_hints.get("return", sig.return_annotation)
    wrapper.__signature__ = inspect.Signature(params, return_annotation=return_annotation)
    return wrapper
