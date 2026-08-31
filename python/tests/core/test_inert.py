# SPDX-License-Identifier: Apache-2.0
"""`tan.core.inert` -- the rendered marker, and the four ways it refuses.

The gate in `tests/gates/test_inert_option_markers.py` covers the SHIPPING
surface; this file covers the renderer itself, including the refusals no
current call site triggers. A validation branch nothing exercises is a
validation branch that stops working silently.
"""
from __future__ import annotations

import contextlib
import io

import pytest
from typer.core import TyperOption
from typer.main import get_command

from tan.cli import app
from tan.core.inert import (
    COMPATIBILITY,
    DEFERRED,
    INERT_KINDS,
    MARKER_RE,
    NOT_APPLICABLE,
    PERMANENT_KINDS,
    inert_help,
)


def test_a_permanent_kind_renders_without_a_ref():
    assert inert_help("Project root.", NOT_APPLICABLE) == "Project root. (inert:not-applicable)"


def test_a_ref_is_rendered_into_the_marker_not_left_in_the_prose():
    rendered = inert_help("Not implemented yet.", DEFERRED, "tan-cli#427")
    assert rendered == "Not implemented yet. (inert:deferred:tan-cli#427)"


def test_a_permanent_kind_may_still_carry_a_ref():
    """`compatibility` and `parity` both name the issue that explains the
    history. A consumer must read the KIND, never "has a ref", to decide
    whether the flag will ever act."""
    rendered = inert_help("Kept for callers.", COMPATIBILITY, "tan-cli#290")
    assert MARKER_RE.search(rendered).group("kind") == COMPATIBILITY
    assert MARKER_RE.search(rendered).group("ref") == "tan-cli#290"


@pytest.mark.parametrize("kind", INERT_KINDS)
def test_every_kind_in_the_vocabulary_round_trips_through_the_regex(kind: str):
    ref = "tan-cli#1" if kind == DEFERRED else None
    match = MARKER_RE.search(inert_help("Prose.", kind, ref))
    assert match is not None and match.group("kind") == kind


def test_a_deferred_marker_with_no_ref_is_refused():
    with pytest.raises(ValueError, match="must name the issue tracking its arrival"):
        inert_help("Not implemented yet.", DEFERRED)


@pytest.mark.parametrize("kind", PERMANENT_KINDS)
def test_a_permanent_kind_is_never_forced_to_carry_a_ref(kind: str):
    assert inert_help("Prose.", kind).endswith(f"(inert:{kind})")


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="unknown inert kind 'someday'"):
        inert_help("Prose.", "someday")


@pytest.mark.parametrize("ref", ["#427", "427", "tan-cli#0", "alp-sdk#427", "tan-cli#4 27"])
def test_a_ref_that_is_not_tan_cli_hash_n_is_refused(ref: str):
    with pytest.raises(ValueError, match="is not of the form"):
        inert_help("Prose.", DEFERRED, ref)


@pytest.mark.parametrize("prose", ["", "   ", "\n"])
def test_a_marker_with_no_prose_of_its_own_is_refused(prose: str):
    with pytest.raises(ValueError, match="needs prose of its own"):
        inert_help(prose, NOT_APPLICABLE)


def test_the_marker_survives_rich_rendering_of_real_help():
    """The reason the marker is parenthesised, measured rather than assumed.

    Typer runs this app with `rich_markup_mode="rich"`, so `--help` prose is
    rich MARKUP. A square-bracketed `[inert:compatibility:tan-cli#290]` parses
    as a style tag and renders as nothing at all -- the whole token disappears
    from the customer's terminal and from the surface alp-sdk-vscode records.
    This asserts the shipped form does NOT do that, on real rendered help.

    The specimen is `tan doctor --build`. It used to be `tan build
    --no-auto-bootstrap`, whose `(inert:deferred:tan-cli#427)` marker went away
    when tan-cli#427 retired that flag instead of implementing it -- `tan build`
    now carries no inert-marked option at all, so this test had no specimen left
    there. Measured before repointing, not assumed: `tan doctor --help` renders
    `(inert:compatibility:tan-cli#290)` and `tan faultdecode --help` renders two
    `(inert:not-applicable)`, which is the whole live population.

    `doctor` is the better of the two anyway: it carries an ISSUE NUMBER, so it
    exercises the `#` and the digits as well as the colons -- `not-applicable`
    has neither, and would leave the longer form untested.
    """
    group = get_command(app)
    ctx = type(group).context_class(group, info_name="tan")
    doctor = group.get_command(ctx, "doctor")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        doctor.get_help(type(ctx)(doctor, parent=ctx, info_name="doctor"))
    rendered = buffer.getvalue()
    assert "(inert:compatibility:tan-cli#290)" in rendered, (
        "the marker did not survive rich rendering of `tan doctor --help`:\n" + rendered
    )


def test_every_marked_option_still_says_something_a_human_can_read():
    """The marker names the KIND, never what the option would have meant. An
    option whose help is only a marker reads as nameless in `--help`."""
    group = get_command(app)
    ctx = type(group).context_class(group, info_name="tan")
    build = group.get_command(ctx, "build")
    marked = [
        p
        for p in build.params
        if isinstance(p, TyperOption) and MARKER_RE.search(p.help or "")
    ]
    assert marked, "expected `tan build` to carry the twelve deferred options"
    for param in marked:
        prose = MARKER_RE.sub("", param.help).strip()
        assert len(prose) > 20, f"{param.opts} is marker-only: {param.help!r}"
