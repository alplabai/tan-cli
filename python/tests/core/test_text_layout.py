# SPDX-License-Identifier: Apache-2.0
"""`tan.core.text_layout.wrap_block` -- the shared wrap seam every text-mode
command is scheduled to adopt (`doctor` first, `monitor`/`size`/`new-som`/
`build`/`kconfig`/`explain`/... after). Its one hard requirement: this repo's
house rule is that SKUs, part numbers, commands, flags and paths are
reproduced verbatim, so a wrapped identifier must never come out split.
stdlib `textwrap.wrap`'s defaults (`break_on_hyphens=True`,
`break_long_words=True`) violate that -- these tests pin the seam against
regressing back to them."""
from __future__ import annotations

import textwrap

from tan.core.text_layout import wrap_block, wrap_lines


def test_hyphenated_identifiers_straddling_the_wrap_boundary_are_not_split():
    """Each (body, width, indent) is picked so plain `textwrap.wrap` -- same
    args, no `break_on_hyphens=False`/`break_long_words=False` -- actually
    breaks the token (confirmed below), then checks `wrap_block` keeps it
    whole on exactly one line instead. The first case is the exact `doctor`
    repro measured at width 46 (tan-cli's own reported bug: `-t arm-` /
    `zephyr-eabi`)."""
    cases = [
        (
            "run west sdk install --version 1.0.1 -t arm-zephyr-eabi to fix this issue",
            46,
            "  ",
            "arm-zephyr-eabi",
        ),
        ("SKU E1M-AEN801 needs the toolchain", 8, "", "E1M-AEN801"),
        ("pass --sdk-root to point at an installed SDK", 6, "", "--sdk-root"),
    ]
    for body, width, indent, token in cases:
        # The naive stdlib default actually mangles this token -- otherwise
        # the case proves nothing.
        naive = textwrap.wrap(body, width=width, initial_indent=indent, subsequent_indent=indent)
        assert not any(token in line for line in naive), (
            f"fixture no longer exercises the bug: {token!r} survived naive wrap {naive}"
        )

        lines = wrap_block(body, width, indent, indent)
        matches = [line for line in lines if token in line]
        assert len(matches) == 1, (token, lines)


def test_a_single_overlong_token_overflows_its_own_line_instead_of_being_chopped():
    """A long path-like token with no spaces, wider than the wrap width: it
    must come out complete, even past `width`, never chopped mid-character."""
    # `dev`, not a made-up-looking name: `test_no_leaked_host_paths` judges a
    # `C:/Users/<home>` by SHAPE, and anything outside its placeholder set --
    # `example` included -- reads as a real account on a public repo.
    token = "C:/Users/dev/very/long/nested/sdk/path/arm-zephyr-eabi/bin"
    body = f"toolchain not found at {token} check your install"

    lines = wrap_block(body, 20, "  ", "  ")
    matches = [line for line in lines if token in line]
    assert len(matches) == 1, lines
    assert len(matches[0]) > 20, "an intact overlong token must overflow, not fit by being cut"


def test_ordinary_prose_still_wraps_at_the_width_unchanged():
    body = " ".join(["word"] * 20)
    lines = wrap_block(body, 20, "  ", "  ")
    assert len(lines) > 1, lines
    for line in lines:
        assert len(line) <= 20, line


# --------------------------------------------------------------------------
# `wrap_lines` -- the small per-line loop `explain`/`sdk current` share,
# moved here from a command-local copy in each (`tan-implementor`'s wrap-
# adoption pass). No per-line classification: every line in `lines` wraps
# the same way, since the reader who could observe a wrapped line (a real
# terminal) is never the reader who could pipe/grep one (`width` is `None`
# exactly when stderr is not a tty, see `tan.env.wrap_width`).
# --------------------------------------------------------------------------


def test_wrap_lines_returns_the_input_object_unchanged_off_a_terminal():
    """`width=None` must hand `lines` back AS-IS, not a rebuilt equal list --
    `tan explain | grep` / `tan sdk current | grep` depend on the piped path
    doing no work at all, not merely reproducing the same text."""
    lines = ["short line", "a line so long it would obviously wrap at any real width " + "x" * 80]
    assert wrap_lines(lines, None) is lines


def test_wrap_lines_wraps_every_line_independently():
    lines = ["short", " ".join(["word"] * 20)]
    wrapped = wrap_lines(lines, 20)
    assert wrapped[0] == "short", "a line already under width is untouched"
    assert len(wrapped) > len(lines), "the long line split into more than one physical line"
    assert all(len(line) <= 20 for line in wrapped[1:])


def test_wrap_lines_hanging_indent_defaults_to_two_spaces():
    wrapped = wrap_lines([" ".join(["word"] * 20)], 20)
    assert len(wrapped) > 1, wrapped
    for line in wrapped[1:]:
        assert line.startswith("  "), line


def test_wrap_lines_keeps_a_long_token_intact_on_its_own_line():
    """The property this whole seam exists for: a caller composes its own
    prefix into the line's text (there is no separate `initial_indent`
    parameter here, unlike `wrap_block`), and a token with no spaces --
    a path, a SKU -- still comes back whole rather than chopped, exactly
    like `wrap_block` itself (`test_a_single_overlong_token...` above)."""
    token = "arm-zephyr-eabi"
    wrapped = wrap_lines([f"- toolchain not found, try {token} instead please"], 20)
    matches = [line for line in wrapped if token in line]
    assert len(matches) == 1, wrapped
