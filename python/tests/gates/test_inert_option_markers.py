# SPDX-License-Identifier: Apache-2.0
"""tan-cli#886: every option tan accepts and does not read must say WHICH KIND
of inert it is, in a token a consumer can switch on.

`alp-sdk-vscode` renders tan's inert options for the customer and has to tell
"not implemented yet, an issue tracks it" apart from "permanent by design" --
a difference that used to live only in per-site prose, so the extension either
pattern-matched English or hand-maintained a copy of tan's own table
(alp-sdk-vscode#577). `tan.core.inert.inert_help` renders the marker; this
gate is what keeps the marker true of the SHIPPING surface rather than of the
four call sites someone remembered to update.

Four properties are checked, and the last two are the ones a new option
actually trips over:

1. every marker parses, and its kind is in the closed vocabulary;
2. every `deferred` marker names a ref (`tan-cli#N`) -- the property that
   makes the kind actionable, and the one thing a gate on either side can
   check (issue #886's own second follow-up);
3. the VISIBLE inert surface matches an explicit census, so adding an inert
   option to `--help` is a declaration here rather than a silent change to
   what the extension records;
4. an option whose help still reads as inert in the OLD prose but carries no
   marker fails -- the "eighteenth command repeats the mistake" case, caught
   structurally instead of by re-measuring.

Read off the built Click tree, not off the source: `--help` is what the
extension records, and a constant that never reaches an option would pass a
source-level check while shipping nothing.
"""
from __future__ import annotations

import re

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
    PARITY,
)

#: `(command path, flag, kind, ref)` for every inert option `tan --help` SHOWS.
#: Hidden options are censused separately below -- they are invisible to the
#: extension's recording, so pinning them here would gate 110 rows nobody reads
#: while saying nothing about the surface the issue is about.
EXPECTED_VISIBLE_INERT: frozenset[tuple[str, str, str, str | None]] = frozenset(
    [
        # tan-cli#427: `--plan`/`--target`/`--all`/`--manifest`/
        # `--manifest-from`/`--verbose`/`--quiet`/`--no-color`/
        # `--non-interactive`/`--ci`/`--pristine`/`--no-auto-bootstrap` all
        # left this census -- `--pristine` is a real, working option now (no
        # marker at all); `--plan`/`--manifest`/`--manifest-from`/
        # `--no-auto-bootstrap` are retired (refused with
        # `build.flag-retired`, naming the replacement or the explicit command
        # -- not "accepted and does nothing", so no `inert_help` marker
        # either); the remaining seven are accept-and-drop via
        # `accept_global_flags`, HIDDEN, so they show up in the PARITY hidden
        # count instead of here. `tan build` now declares NOTHING deferred.
        ("tan doctor", "--build", COMPATIBILITY, "tan-cli#290"),
        ("tan faultdecode", "--project", NOT_APPLICABLE, None),
        ("tan faultdecode", "--sdk-root", NOT_APPLICABLE, None),
    ]
)

#: Prose tan used to mark an option inert with, BEFORE the marker existed.
#: Deliberately narrow: "ignored" is NOT here, because `run --flash` and
#: `model --exact` are conditionally ignored (real options with a documented
#: no-op case), and a gate that called those inert would be wrong about the
#: only distinction this file exists to draw.
_LEGACY_INERT_PROSE = re.compile(r"\bunused\b|not implemented|accepted for|accepted by", re.I)


def _options() -> list[tuple[str, str, bool, str]]:
    """`(command path, flag, hidden, help)` for every option in the tree."""
    group = get_command(app)
    rows: list[tuple[str, str, bool, str]] = []

    def walk(command, path: list[str], ctx) -> None:
        for param in command.params:
            if isinstance(param, TyperOption):
                flag = next((opt for opt in param.opts if opt.startswith("--")), param.opts[0])
                rows.append((" ".join(path), flag, bool(param.hidden), param.help or ""))
        if hasattr(command, "list_commands"):
            for name in sorted(command.list_commands(ctx)):
                sub = command.get_command(ctx, name)
                if sub is not None:
                    walk(sub, [*path, name], type(ctx)(sub, parent=ctx, info_name=name))

    walk(group, ["tan"], type(group).context_class(group, info_name="tan"))
    return rows


ALL_OPTIONS = _options()
MARKED = [
    (path, flag, hidden, MARKER_RE.search(help_))
    for path, flag, hidden, help_ in ALL_OPTIONS
    if MARKER_RE.search(help_)
]


def test_the_walk_actually_reached_the_whole_surface():
    """A walk that silently returned nothing would make every assertion below
    vacuously true -- this is the canary for exactly that."""
    assert len(ALL_OPTIONS) >= 400, (
        f"only {len(ALL_OPTIONS)} options walked; expected the full ~455-option "
        "surface. If the tree shrank on purpose, update this floor in the same "
        "change -- do not let a broken walk read as a clean gate."
    )
    assert len(MARKED) >= 100, (
        f"only {len(MARKED)} inert markers found; expected ~121 (1 `build` "
        "deferral + `doctor --build` + 2 on `faultdecode` + the ~117 hidden "
        "oracle-parity flags, tan-cli#427). A collapsed count means the marker "
        "stopped reaching the built tree, not that tan got tidier."
    )


@pytest.mark.parametrize(
    ("path", "flag", "match"),
    [(path, flag, match) for path, flag, _hidden, match in MARKED],
    ids=[f"{path}:{flag}" for path, flag, _hidden, _match in MARKED],
)
def test_every_marker_uses_the_closed_vocabulary_and_refs_a_deferral(path, flag, match):
    kind = match.group("kind")
    ref = match.group("ref")
    assert kind in INERT_KINDS, (
        f"`{path} {flag}` is marked `(inert:{kind})`, which is not one of "
        f"{', '.join(INERT_KINDS)}. Render the marker with "
        "`tan.core.inert.inert_help`, never by hand -- it is what keeps the "
        "vocabulary closed (tan-cli#886)."
    )
    if kind == DEFERRED:
        assert ref is not None, (
            f"`{path} {flag}` is marked `deferred` with no ref. `deferred` "
            "promises the flag will start acting; without an issue tracking "
            "it, the promise has nothing behind it. Use a permanent kind, or "
            "name the issue."
        )


def test_visible_inert_surface_matches_the_census():
    """The pin issue #886 asked for: what `--help` SHOWS as inert, and of
    which kind. A new inert option is a change to what alp-sdk-vscode records,
    so it is declared here rather than discovered downstream."""
    seen = frozenset(
        (path, flag, match.group("kind"), match.group("ref"))
        for path, flag, hidden, match in MARKED
        if not hidden
    )
    assert seen == EXPECTED_VISIBLE_INERT, (
        "the visible inert surface moved.\n"
        f"  added:   {sorted(seen - EXPECTED_VISIBLE_INERT)}\n"
        f"  dropped: {sorted(EXPECTED_VISIBLE_INERT - seen)}\n"
        "Update EXPECTED_VISIBLE_INERT in the same change, and say so in "
        "CHANGELOG.md -- alp-sdk-vscode's surface golden records this set."
    )


def test_every_hidden_marker_is_the_oracle_parity_set():
    """The ~110 hidden markers are all one fact (`tan.core.global_flags`
    injects a single help string). A hidden option of any OTHER kind means a
    command hand-declared an inert flag and hid it, which is how a surface
    change escapes the census above."""
    kinds = {match.group("kind") for _path, _flag, hidden, match in MARKED if hidden}
    assert kinds == {PARITY}, (
        f"hidden inert options carry kinds {sorted(kinds)}; expected only "
        f"`{PARITY}`. A hidden inert option of another kind belongs in the "
        "visible census, or should not be hidden."
    )


def test_no_option_marks_itself_inert_in_prose_alone():
    """Fails on an option written the way all six pre-#886 sites were: the
    reason in English, nothing to switch on.

    NOT parametrised over the offenders: the list is empty when the gate
    passes, and an empty `parametrize` is a SKIP, which reads as "nothing
    checked" in exactly the run where everything is fine."""
    offenders = [
        (path, flag, help_)
        for path, flag, _hidden, help_ in ALL_OPTIONS
        if _LEGACY_INERT_PROSE.search(help_) and not MARKER_RE.search(help_)
    ]
    assert offenders == [], (
        "these options read as inert but carry no marker:\n  "
        + "\n  ".join(f"`{path} {flag}`: {help_}" for path, flag, help_ in offenders)
        + "\nRender their help with `tan.core.inert.inert_help(prose, kind, ref)` "
        "so a consumer reads the kind instead of inferring it (tan-cli#886)."
    )
