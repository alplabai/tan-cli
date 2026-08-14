# SPDX-License-Identifier: Apache-2.0
"""One wrapping seam every text-mode command can share.

Measured across the 24 read-only commands: 13 emit at least one line over
100 columns (`doctor` 557, `monitor` 362, `size` 267, ...), and only
`build_cmd` (via stdlib `textwrap`) wraps anything at all. `doctor` is the
first command to be fixed; this module is the reusable seam the rest of that
sweep points at afterwards, so it carries no `doctor` concept -- a caller
supplies its own prefix and hanging indent, this just wraps text under them.
"""
from __future__ import annotations

import textwrap


def wrap_block(body: str, width: int, initial_indent: str, hanging_indent: str) -> list[str]:
    """Wrap `body` to `width` columns, `initial_indent` prefixing the first
    line and `hanging_indent` every continuation line -- so a continuation
    never starts at column 0 and reads as subordinate to the line above it.

    `width` is a target, not a hard ceiling: `break_long_words=False` (see
    below) means a single token that, plus whichever indent frames it, is
    alone longer than `width` -- a SKU, a long flag, a path -- comes back
    over-length and intact on its own line rather than being cut mid-
    character. Every OTHER line this returns does fit `width`; a caller that
    needs a hard cap on every line cannot assume that of this one.

    Always returns at least one line (`[initial_indent]` for an empty
    `body`), so a caller building `"{prefix}{first_wrapped_chunk}"` never has
    to special-case an empty result.
    """
    # `break_on_hyphens`/`break_long_words` default True, which is wrong for
    # this seam: this repo's house rule is that SKUs, part numbers, flags and
    # paths are reproduced verbatim, and stdlib's defaults mangle exactly
    # those -- `arm-zephyr-eabi` splits into `arm-` / `zephyr-eabi`,
    # `--sdk-root` into `--sdk-` / `root`. Both off trades a hard `width`
    # ceiling for that guarantee -- see the docstring above for the resulting
    # overflow contract.
    wrapped = textwrap.wrap(
        body,
        width=width,
        initial_indent=initial_indent,
        subsequent_indent=hanging_indent,
        break_on_hyphens=False,
        break_long_words=False,
    )
    return wrapped or [initial_indent]


def wrap_lines(lines: list[str], width: int | None, hanging_indent: str = "  ") -> list[str]:
    """Wrap every line in `lines` independently through `wrap_block`, no
    prefix added to a first line (a caller that wants a bullet or label puts
    it in the line's own text before calling this) and `hanging_indent` on
    any continuation line it produces.

    `width is None` -- stderr is not a terminal, see `tan.env.wrap_width` --
    returns `lines` UNCHANGED: piped/redirected output must reproduce a
    command's report byte-for-byte, which is what every existing
    subprocess/golden test for `explain`/`sdk current` depends on. There is
    no per-line classification here (an earlier version of this seam kept a
    table of "record-shaped" lines exempt from wrapping, on the theory that
    a piped reader might grep them) -- that reader can never observe a
    wrapped line in the first place, since a pipe makes stderr not a tty and
    this function returns `lines` verbatim in that case. Wrapping applies
    uniformly instead; `break_long_words=False` on `wrap_block` already
    keeps a long path/id/SKU intact on its own line rather than mangled.
    """
    if width is None:
        return lines
    out: list[str] = []
    for line in lines:
        out.extend(wrap_block(line, width, "", hanging_indent))
    return out
