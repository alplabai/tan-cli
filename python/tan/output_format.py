# SPDX-License-Identifier: Apache-2.0
"""The one declaration of what `--format` accepts (tan-cli#403).

Before this module the answer was written out 31 times. Every command declared
`output_format: str` with `metavar="FORMAT"` and a prose help sentence, then
re-validated the value in its own body:

    if output_format not in ("text", "json"):
        raise typer.BadParameter(
            f"'{output_format}' (choose from 'text', 'json')", param_hint="--format"
        )

29 byte-identical copies of that, a 30th over a wider domain in
`validate_cmd.py`, a 31st spelled differently in `cli.py`'s root callback
(`ctx.fail(f"'{value}' is not one of 'text', 'json'")`), and a 32nd, 33rd and
34th hardcoded into the bash/zsh/fish scripts `completion_cmd.py` emits. The
copies had already drifted: `validate` really accepts `diagnostic-v1` and
`sarif` -- the two documents an IDE integration wants -- while the root
callback, the help prose and all three completion scripts named only `text`
and `json`. So `tan --format diagnostic-v1 validate --offline` (the
pre-subcommand position `alp-sdk-vscode`'s `withSdkRoot` actually uses) was a
usage error whose message taught that `diagnostic-v1` does not exist, and
`tan validate --format <TAB>` said the same wrong thing.

The oracle never had this problem: clap derives the parser, the `--help`
choice list and the completions from ONE `ValueEnum`, which is why the frozen
oracle help in `tests/fixtures/oracle_captures/test_run_oracle_parity.json`
still prints a `Possible values:` block this port had lost.

So: one enum per domain, declared here, annotated on every `--format`
parameter. Click derives the parse-time value list, the rejection message and
the `--help` metavar from the annotation, `completion_cmd.py` splices the same
values into the scripts it emits, and
`tests/commands/test_output_format.py` asserts all three surfaces agree per
command. There is no body-level check left to drift.

**Why `str` mixin and not `enum.StrEnum`.** A member has to compare equal to
its own wire value: hundreds of call sites downstream of the parameter say
`output_format == "json"`, and every one of them keeps working with a `str`
subclass. `StrEnum` would do that too, but `(str, Enum)` with an explicit
`__str__` is what also makes `f"{fmt}"` render `json` rather than
`OutputFormat.JSON` on every Python this project supports -- and an enum
member that stringifies to its Python name is exactly how a wrong value
reaches a subprocess argv or a JSON payload unnoticed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import TypeVar

import typer


class OutputFormat(str, Enum):
    """What `--format` accepts on every command but `validate`.

    Verbatim from the oracle's own `ValueEnum` (`crates/tan-cli/src/cli.rs`),
    whose `--help` rendering the frozen capture in
    `tests/fixtures/oracle_captures/test_run_oracle_parity.json` shows:
    `text: Human-readable text (default)` / `json: Machine-readable JSON
    envelope`.
    """

    TEXT = "text"
    JSON = "json"

    def __str__(self) -> str:
        return self.value


class ValidateOutputFormat(str, Enum):
    """`validate`'s wider domain -- the same two, plus two IDE-oriented
    documents no other command emits.

    `diagnostic-v1` is alp-sdk's `metadata/schemas/diagnostic-v1.schema.json`
    (mirroring `scripts/alp_cli/diagnostic_format.py:to_machine_json`'s shape
    -- `schemaVersion`/`tool`/`diagnostics[]`, zero-based LSP ranges), `sarif`
    is its SARIF 2.1.0 sibling (`to_sarif`). Both are additions this port
    makes to the v0.4.1 oracle's two-value surface, not parity gaps: `text`
    and `json` are unchanged by them, so only a caller who asks for one of the
    new two sees anything new.

    A SEPARATE enum rather than a wider shared one: every other command emits
    an envelope and nothing else, and an accepted-but-unimplemented
    `tan build --format sarif` would exit 0 having written the wrong document
    -- or none.
    """

    TEXT = "text"
    JSON = "json"
    DIAGNOSTIC_V1 = "diagnostic-v1"
    SARIF = "sarif"

    def __str__(self) -> str:
        return self.value


#: The one `--format` help string. Click appends the choice list itself (it
#: becomes the option's metavar), so naming the values in prose here would put
#: the port straight back to hand-maintaining a second copy that can disagree
#: with the parser -- which is what `Output format: text or json.` on a
#: command accepting four values already was. Wording is the oracle's own
#: (`Output format`), plus this port's trailing full stop.
FORMAT_HELP = "Output format."

#: Commands whose `--format` declares `ValidateOutputFormat`. Consumed by
#: `completion_cmd.py`, which emits text and so cannot read the live Click
#: tree the way `cli.py` can (importing `tan.cli` from a command module is a
#: cycle). `tests/commands/test_output_format.py` pins this tuple to what the
#: tree really declares, so it cannot silently go stale.
WIDE_FORMAT_COMMANDS: tuple[str, ...] = ("validate",)

_FormatT = TypeVar("_FormatT", bound=Enum)


def format_values(choices: type[Enum]) -> tuple[str, ...]:
    """The wire values of one `--format` enum, in declaration order.

    Declaration order is deliberate: it is the order `--help` and the shell
    completions present, and `text` is first because it is the default.
    """
    return tuple(str(member.value) for member in choices)


def invalid_format_message(value: str, choices: Sequence[str]) -> str:
    """Click's OWN wording for a rejected choice, reproduced verbatim.

    `click.types.Choice.get_invalid_choice_message` builds
    `"{value!r} is not one of {choices}."` from `", ".join(map(repr, ...))`.
    Anything that refuses a `--format` value OUTSIDE Click's parser -- i.e.
    `resolve_format` below, for a value inherited across the subcommand
    boundary, and `cli.py`'s root callback, which cannot use a fixed enum at
    all because its domain is whatever the INVOKED subcommand declares -- has
    to say it the same way, or the identical mistake gets two different
    messages depending on where the caller put the flag, which is the defect
    tan-cli#403 was filed about.

    Takes plain strings, not an enum type, precisely so the root callback can
    pass `declared_formats(...)` straight through.
    """
    quoted = ", ".join(map(repr, choices))
    return f"{value!r} is not one of {quoted}."


def resolve_format(
    # `str`, not the enum type: Click hands the body a member (which IS a str
    # by mixin), while a test calling the command function directly passes the
    # wire value. Both are accepted, and both come back as a member.
    local: str | None,
    ctx_obj: Mapping[str, object] | None,
    *,
    choices: type[_FormatT],
) -> _FormatT:
    """`--format` for a command that honours the PRE-subcommand position.

    Precedence, unchanged from the eleven hand-written copies this replaces:
    the command's own `--format` (declared after the subcommand name) wins;
    otherwise the root callback's value off `ctx.obj` (`cli.py` records what
    preceded the subcommand, since Click hands a group only what comes before
    it); otherwise `text`.

    `local is not None` rather than a truthiness test: the local value arrives
    already parsed by Click, and the only falsey member an enum could ever
    have would still be an explicit caller choice, not an absent flag.

    The inherited value is re-validated because it did NOT come through this
    command's parser -- it came through the root callback's, whose domain need
    not be this command's. Passing an out-of-domain value straight through
    would hand the body a format it has no branch for, and the caller would
    get a silent text-mode run with an empty stdout instead of an error.
    """
    if local is not None:
        return choices(local)
    inherited = (ctx_obj or {}).get("format")
    if not inherited:
        return choices("text")
    try:
        return choices(inherited)
    except ValueError:
        raise typer.BadParameter(
            invalid_format_message(str(inherited), format_values(choices)),
            # Quoted INSIDE the string on purpose: Click's own hint comes from
            # `Parameter.get_error_hint`, which is `repr` of the option string,
            # so this renders `Invalid value for '--format': ...` -- byte-identical
            # to what the parser itself would have printed for the same value.
            param_hint="'--format'",
        ) from None


def declared_formats(command: object) -> tuple[str, ...]:
    """The values one live Click command's `--format` accepts, read off the
    command tree rather than any second list.

    This is what lets `cli.py`'s root callback accept whatever the INVOKED
    subcommand declares instead of a frozen pair -- `tan --format
    diagnostic-v1 validate --offline` has to produce the same document as
    `tan validate --offline --format diagnostic-v1`.

    Duck-typed on `.opts`/`.type.choices`, never `isinstance`: Typer builds
    its commands out of its own VENDORED click (`typer._click`), so an
    `isinstance(param, click.Option)` test against the top-level `click`
    package matches nothing -- the trap `cli._tokens_consumed_after` already
    documents. An empty result means the command declares no `--format` at
    all, which a caller must treat as "refuse", never as "anything goes".
    """
    for param in getattr(command, "params", ()):
        if "--format" not in getattr(param, "opts", ()):
            continue
        found: Sequence[str] = getattr(param.type, "choices", ()) or ()
        return tuple(str(choice) for choice in found)
    return ()
